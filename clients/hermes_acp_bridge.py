#!/usr/bin/env python3
"""ACP (Agent Client Protocol) bridge: lets T3 Code on another machine talk to your Hermes
on the gaming PC over the tailnet (HTTPS, tailscale serve :8445 -> Hermes API server).

T3 runs this as an ACP agent over stdio (JSON-RPC 2.0, one message per line). Each ACP session is
a Hermes API session (/api/sessions); prompts stream through /api/sessions/{id}/chat/stream and
come back as session/update chunks. Dangerous-command approvals become ACP permission requests.

Stdlib only, Python 3.9+. Config (env or defaults):
  HERMES_URL       https://<pc>.<your-tailnet>.ts.net:8445
  HERMES_KEY_FILE  ~/.config/hermes-remote/key   (Hermes API_SERVER_KEY, chmod 600)
  HERMES_MODEL     openai-codex:gpt-6.1-sol
"""
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

def _arg(name, default=None):
    if name in sys.argv[1:-1]:
        return sys.argv[sys.argv.index(name) + 1]
    return default


# --profile work -> https://.../p/work and ~/.config/hermes-remote/work.key (separate Hermes, separate memory)
PROFILE = _arg("--profile", os.environ.get("HERMES_PROFILE", ""))
BASE = os.environ.get("HERMES_URL", "https://<pc>.<your-tailnet>.ts.net:8445").rstrip("/")
URL = BASE + (f"/p/{PROFILE}" if PROFILE else "")
KEY_FILE = os.path.expanduser(_arg("--key-file", os.environ.get(
    "HERMES_KEY_FILE", f"~/.config/hermes-remote/{PROFILE}.key" if PROFILE else "~/.config/hermes-remote/key")))
LABEL = f"Hermes {PROFILE.capitalize()}" if PROFILE else "Hermes"
# First entry is the default: the local model. ChatGPT only when picked explicitly.
MODELS = [("custom:home-4090:swift-1.5-125b-strata", f"{LABEL} · Local Swift 1.5 125B (Strata)"),
          ("openai-codex:gpt-6.1-sol", f"{LABEL} · GPT-6.1 Sol (ChatGPT, cloud)")]
DEFAULT_MODEL = os.environ.get("HERMES_MODEL", MODELS[0][0])
HOST = socket.gethostname()
SYSTEM = (f"This conversation comes from T3 Code on the user's machine '{HOST}' through a remote bridge. "
          "Your tools run on the gaming PC, "
          f"not on '{HOST}'; you cannot see or change files on '{HOST}'. Say so if asked to work on its files.")

_out_lock = threading.Lock()
_next_id = [1000]
_pending = {}        # our request id -> [Event, result]
_sessions = {}       # sessionId -> {"model":..., "effort": level or None, "run": run_id or None, "cancel": bool}
# Hermes per-request reasoning levels (model_options.reasoning_effort); "none" disables thinking.
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def session(sid):
    s = _sessions.setdefault(sid, {})
    s.setdefault("model", DEFAULT_MODEL)
    s.setdefault("effort", None)
    s.setdefault("run", None)
    s.setdefault("cancel", False)
    return s


def default_effort(model):
    # mirrors the Hermes server defaults: per-model override for the local Strata model, else medium
    return "low" if "swift-1.5-125b-strata" in model else "medium"


def log(*a):
    print("[hermes-bridge]", *a, file=sys.stderr, flush=True)


def send(msg):
    msg["jsonrpc"] = "2.0"
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def notify(sid, update):
    send({"method": "session/update", "params": {"sessionId": sid, "update": update}})


def request_client(method, params, timeout=3600):
    _next_id[0] += 1
    rid = _next_id[0]
    ev = threading.Event()
    _pending[rid] = [ev, None]
    send({"id": rid, "method": method, "params": params})
    ev.wait(timeout)
    return _pending.pop(rid, [None, None])[1]


def key():
    with open(KEY_FILE, encoding="utf-8") as f:
        return f.read().strip()


def http(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json", "Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def http_json(method, path, body=None, timeout=60):
    try:
        with http(method, path, body, timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Hermes HTTP {e.code} on {path}: {e.read().decode('utf-8', 'replace')[:400]}")


def config_options(sid):
    s = session(sid)
    cur = s["model"]
    return [{"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": cur,
             "options": [{"value": v, "name": n} for v, n in MODELS]},
            {"id": "reasoning_effort", "name": "Reasoning effort", "category": "thought_level", "type": "select",
             "currentValue": s["effort"] or default_effort(cur),
             "options": [{"value": v, "name": v} for v in EFFORTS]}]


def to_hermes_content(blocks):
    parts = []
    for b in blocks or []:
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image" and b.get("data"):
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{b.get('mimeType', 'image/png')};base64,{b['data']}"}})
        elif t == "resource":
            r = b.get("resource") or {}
            if r.get("text"):
                parts.append({"type": "text", "text": f"[{r.get('uri', 'attached file')}]\n{r['text']}"})
        elif t == "resource_link":
            parts.append({"type": "text", "text": f"[file on {HOST}: {b.get('uri') or b.get('name')}]"})
    if all(p["type"] == "text" for p in parts):
        return "\n\n".join(p["text"] for p in parts)
    return parts


def sse(resp):
    event, data = None, []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield event, json.loads("\n".join(data))
                except ValueError:
                    pass
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


APPROVAL_KIND = {"once": ("allow_once", "Allow once"), "session": ("allow_always", "Allow for this session"),
                 "always": ("allow_always", "Always allow"), "deny": ("reject_once", "Deny")}


def handle_approval(sid, ev):
    run_id = ev.get("run_id") or _sessions[sid]["run"]
    choices = ev.get("choices") or ["once", "deny"]
    desc = ev.get("description") or ""
    cmd = ev.get("command") or ""
    tcid = "approval-" + str(ev.get("request_id") or ev.get("seq") or "")
    res = request_client("session/request_permission", {
        "sessionId": sid,
        "toolCall": {"toolCallId": tcid, "title": (desc or cmd or "Hermes needs approval")[:200],
                     "kind": "execute", "status": "pending",
                     "content": [{"type": "content", "content": {"type": "text", "text": f"{desc}\n{cmd}".strip()}}]},
        "options": [{"optionId": c, "name": APPROVAL_KIND.get(c, ("allow_once", c))[1],
                     "kind": APPROVAL_KIND.get(c, ("allow_once", c))[0]} for c in choices]})
    outcome = ((res or {}).get("outcome") or {})
    choice = outcome.get("optionId") if outcome.get("outcome") == "selected" else "deny"
    body = {"choice": choice}
    if ev.get("request_id"):
        body["request_id"] = ev["request_id"]
    try:
        http_json("POST", f"/v1/runs/{run_id}/approval", body)
    except Exception as e:  # noqa: BLE001
        log("approval post failed:", e)


def prompt(sid, params):
    s = session(sid)
    s["cancel"] = False
    body = {"message": to_hermes_content(params.get("prompt")), "system_message": SYSTEM,
            "model": s["model"].rsplit(":", 1)[1], "provider": s["model"].rsplit(":", 1)[0]}
    if s["effort"]:
        body["model_options"] = {"reasoning_effort": s["effort"]}
    tools = {}
    stop = "end_turn"
    streamed = False
    try:
        with http("POST", f"/api/sessions/{sid}/chat/stream", body, timeout=1800) as resp:
            for name, ev in sse(resp):
                if name == "run.started":
                    s["run"] = ev.get("run_id")
                elif name == "assistant.delta" and ev.get("delta"):
                    streamed = True
                    notify(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": ev["delta"]}})
                elif name == "assistant.commentary" and not ev.get("already_streamed") and ev.get("text"):
                    notify(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": ev["text"] + "\n\n"}})
                elif name == "tool.progress" and ev.get("delta"):
                    notify(sid, {"sessionUpdate": "agent_thought_chunk",
                                 "content": {"type": "text", "text": ev["delta"]}})
                elif name == "tool.started":
                    tn = ev.get("tool_name") or "tool"
                    tcid = f"{tn}-{ev.get('seq')}"
                    tools.setdefault(tn, []).append(tcid)
                    notify(sid, {"sessionUpdate": "tool_call", "toolCallId": tcid,
                                 "title": f"{tn}: {(ev.get('preview') or '')[:120]}".rstrip(": "),
                                 "kind": "execute" if tn in ("terminal", "execute_code") else "other",
                                 "status": "in_progress", "rawInput": ev.get("args")})
                elif name in ("tool.completed", "tool.failed"):
                    tn = ev.get("tool_name") or "tool"
                    q = tools.get(tn) or []
                    tcid = q.pop(0) if q else f"{tn}-{ev.get('seq')}"
                    notify(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tcid,
                                 "status": "completed" if name == "tool.completed" else "failed"})
                elif name == "approval.request":
                    threading.Thread(target=handle_approval, args=(sid, ev), daemon=True).start()
                elif name == "assistant.completed":
                    if not streamed and ev.get("content"):
                        notify(sid, {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": ev["content"]}})
                elif name == "run.cancelled":
                    stop = "cancelled"
                elif name == "error":
                    notify(sid, {"sessionUpdate": "agent_message_chunk",
                                 "content": {"type": "text", "text": f"\n\n[Hermes error: {ev.get('message')}]"}})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Hermes HTTP {e.code}: {detail}")
    finally:
        s["run"] = None
    return {"stopReason": "cancelled" if s["cancel"] else stop}


def replay(sid):
    try:
        msgs = http_json("GET", f"/api/sessions/{sid}/messages")
    except Exception as e:  # noqa: BLE001
        log("history fetch failed:", e)
        return
    items = msgs.get("data") or msgs.get("messages") or []
    for m in items:
        c = m.get("content")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
        if not c or m.get("role") not in ("user", "assistant"):
            continue
        kind = "user_message_chunk" if m["role"] == "user" else "agent_message_chunk"
        notify(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": c}})


def handle(msg):
    method, params, mid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method is None and mid in _pending:          # response to our request
        _pending[mid][1] = msg.get("result")
        _pending[mid][0].set()
        return
    try:
        if method == "initialize":
            result = {"protocolVersion": 1, "agentInfo": {"name": "hermes-remote" + (f"-{PROFILE}" if PROFILE else ""), "version": "1.3"},
                      "agentCapabilities": {"loadSession": True, "promptCapabilities": {
                          "image": True, "embeddedContext": True}}, "authMethods": []}
        elif method == "session/new":
            s = http_json("POST", "/api/sessions", {"source": "api_server"})
            sid = (s.get("session") or s).get("id")
            session(sid)
            result = {"sessionId": sid, "configOptions": config_options(sid)}
        elif method in ("session/load", "session/resume"):
            sid = params["sessionId"]
            session(sid)
            if method == "session/load":
                replay(sid)
            result = {"configOptions": config_options(sid)}
        elif method == "session/set_config_option":
            sid = params["sessionId"]
            cid, val = params.get("configId"), params.get("value")
            if cid == "model":
                if val not in dict(MODELS):
                    raise RpcError(-32602, f"Invalid model: {val}")
                session(sid)["model"] = val          # effort is kept across model changes
            elif cid == "reasoning_effort":
                if val not in EFFORTS:
                    raise RpcError(-32602, f"Invalid reasoning_effort: {val} (expected one of {', '.join(EFFORTS)})")
                session(sid)["effort"] = val
            else:
                raise RpcError(-32602, f"Unknown configId: {cid}")
            result = {"configOptions": config_options(sid)}
        elif method == "session/set_model":
            sid = params["sessionId"]
            if params.get("modelId") in dict(MODELS):
                session(sid)["model"] = params["modelId"]
            result = {}
        elif method == "session/prompt":
            result = prompt(params["sessionId"], params)
        elif method == "session/cancel":
            s = _sessions.get(params.get("sessionId")) or {}
            s["cancel"] = True
            if s.get("run"):
                try:
                    http_json("POST", f"/v1/runs/{s['run']}/stop", {})
                except Exception as e:  # noqa: BLE001
                    log("stop failed:", e)
            return
        elif method == "authenticate":
            result = {}
        else:
            if mid is not None:
                send({"id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        if mid is not None:
            send({"id": mid, "result": result})
    except RpcError as e:
        log(method, "rejected:", e)
        if mid is not None:
            send({"id": mid, "error": {"code": e.code, "message": str(e)[:500]}})
    except Exception as e:  # noqa: BLE001
        log(method, "failed:", e)
        if mid is not None:
            send({"id": mid, "error": {"code": -32603, "message": str(e)[:500]}})


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    if "--check" in sys.argv:
        print(json.dumps(http_json("GET", "/health")))
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        # prompts block for minutes; run them off the reader thread so cancel/permission replies get through
        if msg.get("method") in ("session/prompt", "session/new", "session/load", "session/resume"):
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
        else:
            handle(msg)


if __name__ == "__main__":
    main()
