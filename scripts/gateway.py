"""Model gateway: one OpenAI-compatible endpoint (with an API key) for every home model and mode, so coding agents
(OpenCode / T3 Code), Hermes and other machines on the tailnet can pick them by name. README > Model gateway.

Everything comes from config.json "gateway":
  port, keyFile       listen on 127.0.0.1:<port>; every request needs "Authorization: Bearer <key>"
                      (install.ps1 creates keyFile). tailscale.serve.gateway publishes it over tailnet HTTPS.
  keyDir              optional folder of per-machine keys: each <label>.txt holds one key, logged as [<label>].
                      Keys are re-read on every request, so deleting a file cuts that machine off at once.
  hold                {maxHours, keepaliveSec, pollSec} for models with whenDown "hold" (defaults 4 / 15 / 5)
  decisionUrl         where Jev /v1/decision goes (default: the router's decision port, else the LLM port)
  rewarm              {enabled, delaySec, rebuildEverySec}: after a decision from anyone but Home Assistant, re-send
                      Home Assistant's exact decision payload (rebuilt from live HA by ha_payload.py) so its prompt is
                      cached again on the decision GPU. Measured: next voice command 1.0 s -> 0.3 s.
  models[]            one entry per selectable model/mode:
    id, name, ctx     model id clients send, display name, context window
    kind              "chat" (proxied to url) or "jev" (the last user message becomes a /v1/decision call)
    url               backend base URL (llama-server, Strata, ...), for kind chat
    effort            thinking level forced on the request: none|low|medium|high (Strata / Qwen-style templates).
                      Leave it out to pass the client's own reasoning_effort / chat_template_kwargs through (one model
                      id with an effort selector in the client, e.g. OpenCode variants or Hermes' reasoning_effort)
    think             true/false: chat_template_kwargs.enable_thinking (Gemma / llama.cpp templates)
    reasoningBudget   hard cap on thinking tokens (Strata's reasoning_budget_tokens)
    chatOnly          drop tools, replace a long agent system prompt and trim old turns (small-context models)
    promptBudget, maxReply   with chatOnly: estimated prompt tokens to keep, and the reply cap
    requireTier       answer 503 unless state\\status.json says this tier is loaded (e.g. "strata": idle-only models)
    requireModel      answer 503 unless the backend's /props model_path contains this text (e.g. "gemma")
    healthCheck       also require GET <url>/health to answer 200 before the model counts as up
    fallback          id of another chat model that takes a request while this one is down, if the request has no
                      tools and its estimated prompt fits that model's promptBudget (e.g. Gemma on a worker GPU)
    whenDown          "fail" (default: 503) or "hold": keep the client's connection open with keepalives until the
                      model is up again (at most hold.maxHours), then run the request. Nothing is loaded for it: the
                      supervisor brings the tier back on its own schedule (after a game ends, idle delays).
Runs as the SYSTEM task "HomeLLM Gateway" with the kit's voice Python (needs aiohttp).
"""
import asyncio
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
GW = CFG.get("gateway") or {}
KEY_FILE = ROOT / GW.get("keyFile", "gateway-key.txt")
KEY_DIR = (ROOT / GW["keyDir"]) if GW.get("keyDir") else None
HOLD = {"maxHours": 4, "keepaliveSec": 15, "pollSec": 5, **(GW.get("hold") or {})}
STATUS_FILE = ROOT / "state" / "status.json"
LOG = ROOT / "logs" / "gateway.log"
HA_PAYLOAD = ROOT / "state" / "ha_payload.json"
HA_BUILDER = ROOT / "scripts" / "ha_payload.py"
_cl = CFG.get("cluster") or {}
DECISION = (GW.get("decisionUrl") or
            f"http://127.0.0.1:{_cl.get('decisionPort') or CFG['llm']['port']}").rstrip("/")
REWARM = {"enabled": True, "delaySec": 1.5, "rebuildEverySec": 900, **(GW.get("rewarm") or {})}
MODELS = {m["id"]: m for m in GW.get("models", [])}
SHORT_SYSTEM = ("You are a helpful assistant running locally on the user's home GPU. You cannot use tools or see "
                "files in this mode; answer from the conversation. Be concise.")


def load_keys():
    """key -> label. keyFile is "default"; every keyDir/<label>.txt adds one key per machine."""
    keys = {}
    try:
        keys[KEY_FILE.read_text(encoding="utf-8").strip()] = "default"
    except OSError:
        pass
    if KEY_DIR and KEY_DIR.is_dir():
        for f in KEY_DIR.glob("*.txt"):
            k = f.read_text(encoding="utf-8").strip()
            if k:
                keys[k] = f.stem
    keys.pop("", None)
    return keys


def log(msg):
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def tier():
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8-sig")).get("tier")
    except Exception:
        return None


def err(status, message, etype="unavailable"):
    return web.json_response({"error": {"message": message, "type": etype, "code": status}}, status=status)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def est_tokens(messages):
    return sum(len(text_of(m.get("content"))) for m in messages) // 3 + 8 * len(messages)


@web.middleware
async def auth(request, handler):
    if request.path in ("/health", "/"):
        return await handler(request)
    h = request.headers.get("Authorization", "")
    label = load_keys().get(h[7:].strip()) if h.startswith("Bearer ") else None
    if not label:
        return err(401, "missing or wrong API key", "auth")
    request["client"] = label
    return await handler(request)


async def health(_):
    return web.json_response({"status": "ok", "tier": tier()})


async def list_models(_):
    now = int(time.time())
    return web.json_response({"object": "list", "data": [
        {"id": mid, "object": "model", "created": now, "owned_by": "home", "name": m.get("name", mid),
         "context_length": m.get("ctx")} for mid, m in MODELS.items()]})


async def proxy(request, url, body):
    """POST body to url and pass the reply through, streaming when the client asked for it."""
    async with request.app["session"].post(url, json=body) as up:
        if not body.get("stream"):
            return web.Response(body=await up.read(), status=up.status, content_type=up.content_type)
        resp = web.StreamResponse(status=up.status, headers={
            "Content-Type": up.headers.get("Content-Type", "text/event-stream"), "Cache-Control": "no-cache"})
        await resp.prepare(request)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


async def backend_model(request, url):
    try:
        async with request.app["session"].get(f"{url}/props", timeout=ClientTimeout(total=3)) as r:
            return (await r.json(content_type=None)).get("model_path", "")
    except Exception:
        return ""


def chat_only(body, m):
    """Fit a coding agent's request into a small-context chat model: no tools, short system prompt, recent turns."""
    for k in ("tools", "tool_choice", "parallel_tool_calls"):
        body.pop(k, None)
    msgs = [dict(x) for x in body.get("messages", []) if x.get("role") in ("system", "user", "assistant")]
    for x in msgs:
        x["content"] = text_of(x.get("content"))
        x.pop("tool_calls", None)
    msgs = [x for x in msgs if x["content"].strip() or x["role"] == "user"]
    sys_text = "\n".join(x["content"] for x in msgs if x["role"] == "system")
    rest = [x for x in msgs if x["role"] != "system"]
    sys_msg = {"role": "system", "content": sys_text if 0 < len(sys_text) <= 2000 else SHORT_SYSTEM}
    while len(rest) > 1 and est_tokens([sys_msg] + rest) > int(m.get("promptBudget", 2600)):
        rest.pop(0)
    if rest and rest[0]["role"] == "assistant":
        rest.pop(0)
    body["messages"] = [sys_msg] + rest
    cap = int(m.get("maxReply", 1024))
    asked = body.pop("max_completion_tokens", None) or body.get("max_tokens")
    body["max_tokens"] = min(int(asked) if asked else cap, cap)


async def backend_health(request, url):
    try:
        async with request.app["session"].get(f"{url}/health", timeout=ClientTimeout(total=3)) as r:
            return r.status == 200
    except Exception:
        return False


async def is_up(request, m):
    """(up, reason): requireTier, requireModel and healthCheck all hold."""
    url = m["url"].rstrip("/")
    if m.get("requireTier") and tier() != m["requireTier"]:
        return False, f"tier is {tier()}, needs '{m['requireTier']}'"
    if m.get("requireModel") and m["requireModel"].lower() not in (await backend_model(request, url)).lower():
        return False, f"no '{m['requireModel']}' model loaded on its machine"
    if m.get("healthCheck") and not await backend_health(request, url):
        return False, "backend /health failed"
    return True, ""


def prepare(body, m):
    """Model name and thinking settings for the backend. Without effort/think the client's own settings pass."""
    body["model"] = CFG["llm"].get("alias", "home")
    if "effort" in m:
        kwargs = body.setdefault("chat_template_kwargs", {})
        body["reasoning_effort"] = m["effort"]
        kwargs["enable_thinking"] = m["effort"] != "none"
        kwargs["reasoning_effort"] = m["effort"]
    if "think" in m:
        body.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(m["think"])
        body.pop("reasoning_effort", None)
        body.pop("reasoning", None)
    if m.get("reasoningBudget"):
        body["reasoning_budget_tokens"] = int(m["reasoningBudget"])
    if m.get("chatOnly"):
        chat_only(body, m)


async def chat_model(request, body, m):
    up, why = await is_up(request, m)
    if not up:
        fb = MODELS.get(m.get("fallback") or "")
        if (fb and not body.get("tools") and est_tokens(body.get("messages", [])) <= int(fb.get("promptBudget", 2600))
                and (await is_up(request, fb))[0]):
            log(f"[{request.get('client')}] {m['id']} down ({why}): small request without tools -> {fb['id']}")
            m = fb
        elif m.get("whenDown") == "hold":
            return await hold(request, body, m, why)
        else:
            return err(503, f"{m.get('name', m['id'])} isn't loaded right now ({why}). Pick another model or try "
                            "again later.")
    prepare(body, m)
    return await proxy(request, f"{m['url'].rstrip('/')}/v1/chat/completions", body)


async def hold(request, body, m, why):
    """Keep the client's connection open until the model is up (or hold.maxHours pass), then answer from it. A
    streaming reply gets an SSE comment line, a plain one a leading space, every hold.keepaliveSec; clients ignore
    both. Clients need a request timeout longer than maxHours (README > Client templates)."""
    client, stream = request.get("client"), bool(body.get("stream"))
    max_h, beat, poll = float(HOLD["maxHours"]), float(HOLD["keepaliveSec"]), float(HOLD["pollSec"])
    log(f"[{client}] HOLD {m['id']}: down ({why}), ~{est_tokens(body.get('messages', []))} tokens, "
        f"tools={len(body.get('tools') or [])}: waiting up to {max_h:g} h")
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream" if stream else "application/json", "Cache-Control": "no-cache",
        "X-Home-Queued": f"waiting for {m['id']}"})
    await resp.prepare(request)
    t0 = last = time.monotonic()
    try:
        while not (await is_up(request, m))[0]:
            now = time.monotonic()
            if now - t0 > max_h * 3600:
                log(f"[{client}] HOLD {m['id']} gave up after {(now - t0) / 60:.0f} min")
                msg = {"error": {"message": f"{m.get('name', m['id'])} stayed unavailable for {max_h:g} h; try "
                                            "again later", "type": "unavailable", "code": 503}}
                await resp.write((f"data: {json.dumps(msg)}\n\ndata: [DONE]\n\n" if stream
                                  else json.dumps(msg)).encode())
                await resp.write_eof()
                return resp
            if now - last >= beat:
                await resp.write(f": queued, waiting for {m['id']} ({(now - t0) / 60:.1f} min)\n\n".encode()
                                 if stream else b" ")
                last = now
            await asyncio.sleep(poll)
    except (ConnectionResetError, asyncio.CancelledError):
        log(f"[{client}] HOLD {m['id']}: the client left after {(time.monotonic() - t0) / 60:.1f} min")
        raise
    log(f"[{client}] HOLD {m['id']} over after {(time.monotonic() - t0) / 60:.1f} min: running the request")
    prepare(body, m)
    async with request.app["session"].post(f"{m['url'].rstrip('/')}/v1/chat/completions", json=body) as up:
        if up.status != 200:
            log(f"[{client}] after HOLD {m['id']} answered {up.status}")
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
    await resp.write_eof()
    return resp


OPTION_LINE = re.compile(r"^\s*(?:options?|choices?)\s*:\s*(.+)$", re.I | re.M)
BRACKET = re.compile(r"\[([^\[\]]*\|[^\[\]]*)\]")
BULLETS = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.M)


def parse_jev(text):
    """A chat message -> /v1/decision body. Choices: 'options: a | b | c', a [a|b|c] bracket, '- a' bullets, else
    yes/no. A pasted JSON object with instructions/schema/contexts is used as is."""
    s = text.strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            if {"instructions", "schema", "contexts"} <= obj.keys():
                return obj
        except json.JSONDecodeError:
            pass
    choices, question = None, s
    if (m := OPTION_LINE.search(s)):
        choices = [c.strip() for c in re.split(r"\s*\|\s*|\s*,\s*", m.group(1)) if c.strip()]
        question = (s[:m.start()] + s[m.end():]).strip()
    elif (b := BRACKET.search(s)):
        choices = [c.strip() for c in b.group(1).split("|") if c.strip()]
        question = (s[:b.start()] + s[b.end():]).strip()
    elif len(bullets := BULLETS.findall(s)) >= 2:
        choices, question = bullets, BULLETS.sub("", s).strip()
    field = ({"type": "enum", "choices": choices[:64], "description": question[:500] or "Pick the best answer."}
             if choices and len(choices) >= 2 else {"type": "boolean", "description": question[:500]})
    return {"model": CFG["llm"].get("alias", "home"), "schema": {"answer": field}, "contexts": [question or s],
            "instructions": "Read the user's message and answer its question as well as you can."}


async def jev_model(request, body, m):
    users = [x for x in body.get("messages", []) if x.get("role") == "user"]
    if not users:
        return err(400, "send a question", "invalid_request_error")
    t0 = time.time()
    async with request.app["session"].post(f"{DECISION}/v1/decision", json=parse_jev(text_of(users[-1].get("content")))) as up:
        data = await up.json(content_type=None)
    schedule_rewarm(request.app)
    if "results" not in data:
        return err(502, f"decision failed: {json.dumps(data)[:300]}")
    lines = []
    for i, res in enumerate(data["results"]):
        if len(data["results"]) > 1:
            lines.append(f"**Context {i + 1}**")
        for name, f in res["fields"].items():
            v = "yes" if f["value"] is True else "no" if f["value"] is False else f["value"]
            lines.append(f"- **{name}: {v}** ({f['probability'] * 100:.1f}% sure)")
    lines.append(f"\n_Jev mode, {(time.time() - t0) * 1000:.0f} ms. Add 'options: a | b | c' to choose between "
                 "answers; without it you get yes/no._")
    answer = "\n".join(lines)
    cid, created, model = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time()), m["id"]
    pt = data.get("usage", {}).get("prompt_tokens", 0)
    usage = {"prompt_tokens": pt, "completion_tokens": len(answer) // 4, "total_tokens": pt + len(answer) // 4}
    if not body.get("stream"):
        return web.json_response({"id": cid, "object": "chat.completion", "created": created, "model": model, "usage": usage,
                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                                               "finish_reason": "stop"}]})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}
    for delta, finish in (({"role": "assistant", "content": answer}, None), ({}, "stop")):
        await resp.write(f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n".encode())
    if (body.get("stream_options") or {}).get("include_usage"):
        await resp.write(f"data: {json.dumps({**base, 'choices': [], 'usage': usage})}\n\n".encode())
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


def schedule_rewarm(app):
    """Debounced: one Home Assistant re-warm rewarm.delaySec after the last other decision."""
    if not REWARM["enabled"]:
        return
    task = app.get("rewarm")
    if task and not task.done():
        task.cancel()
    app["rewarm"] = asyncio.create_task(rewarm(app))


async def rewarm(app):
    try:
        await asyncio.sleep(float(REWARM["delaySec"]))
        payload = app.get("ha_payload")
        if not payload:
            return
        t0 = time.time()
        async with app["session"].post(f"{DECISION}/v1/decision",
                                       json=dict(payload, contexts=['The user said: "status check"'])) as r:
            u = (await r.json(content_type=None)).get("usage", {})
        log(f"re-warmed HA prompt: {u.get('cached_tokens')}/{u.get('prompt_tokens')} cached before, "
            f"{(time.time() - t0) * 1000:.0f} ms")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log(f"re-warm failed: {e!r}")


async def ha_payload_loop(app):
    """Rebuild Home Assistant's decision payload from live HA now and then; keep the last good one."""
    while True:
        try:
            r = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", str(HA_BUILDER)], capture_output=True,
                                        timeout=120)
            if r.returncode != 0:
                log(f"payload rebuild failed: {r.stderr.decode(errors='replace')[-300:]}")
            app["ha_payload"] = json.loads(HA_PAYLOAD.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"payload rebuild error: {e!r}")
        await asyncio.sleep(float(REWARM["rebuildEverySec"]))


async def decision(request):
    """Plain /v1/decision passthrough for Hermes and other tools (any instructions/schema), then an HA re-warm."""
    try:
        body = await request.json()
    except Exception:
        return err(400, "body must be JSON", "invalid_request_error")
    body.setdefault("model", CFG["llm"].get("alias", "home"))
    async with request.app["session"].post(f"{DECISION}/v1/decision", json=body) as up:
        data, status = await up.read(), up.status
    schedule_rewarm(request.app)
    return web.Response(body=data, status=status, content_type="application/json")


async def chat(request):
    try:
        body = await request.json()
    except Exception:
        return err(400, "body must be JSON", "invalid_request_error")
    mid = str(body.get("model", "")).split("/", 1)[-1]     # tolerate "<provider>/<model>"
    m = MODELS.get(mid)
    if not m:
        return err(404, f"unknown model {mid!r}; choose one of: {', '.join(MODELS)}", "invalid_request_error")
    log(f"[{request.get('client')}] {mid} stream={bool(body.get('stream'))} msgs={len(body.get('messages', []))} tools={len(body.get('tools') or [])}")
    try:
        return await (jev_model if m.get("kind") == "jev" else chat_model)(request, body, m)
    except asyncio.TimeoutError:
        return err(504, "the home model took too long to answer")
    except Exception as e:
        log(f"{mid} error: {e!r}")
        return err(502, f"backend error: {e}")


async def on_startup(app):
    # no total limit: a held request and a long 256K prefill plus reply can take far longer than 30 min
    app["session"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=5, sock_read=3600))
    if REWARM["enabled"]:
        app["payload_task"] = asyncio.create_task(ha_payload_loop(app))


async def on_cleanup(app):
    if app.get("payload_task"):
        app["payload_task"].cancel()
    await app["session"].close()


def main():
    app = web.Application(middlewares=[auth], client_max_size=64 * 1024 * 1024)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", list_models)
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_post("/v1/decision", decision)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    port = int(GW.get("port", 8090))
    log(f"gateway started on 127.0.0.1:{port} ({len(MODELS)} models, decisions -> {DECISION})")
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None)


if __name__ == "__main__":
    main()
