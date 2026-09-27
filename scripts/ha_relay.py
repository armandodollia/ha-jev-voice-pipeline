"""Relay that runs Wristotle's Home Assistant commands through a real Assist pipeline.

Wristotle posts to {base}/api/conversation/process without an agent_id, so Home Assistant
answers with its built-in agent and skips the Assist pipeline (LLM agent, "prefer handling
commands locally", ...). This relay accepts the same request and runs it with
`assist_pipeline/run` over HA's websocket API instead, then returns the pipeline's
intent output, which has the same shape as the conversation API's response.

It also speaks the OpenAI chat API (POST /v1/chat/completions, non-streaming), so an app's
"LLM agent" (e.g. Wristotle's Ask Agent) can use the same pipeline: the newest user message
is run through Assist and the spoken reply comes back as choices[0].message.content. The
app's system prompt, history and tools are ignored; Home Assistant keeps the conversation.

The caller's own bearer token (a Home Assistant long-lived access token; for the OpenAI API
it goes in the app's "API key" field) is passed through to Home Assistant; nothing is stored.
Follow-up questions keep their context: the last conversation_id per token is reused
for CONTINUE_SECONDS.

Usage: python ha_relay.py [--port 10320] [--ha ws://homeassistant.local:8123] [--pipeline <id>]
"""
import argparse
import asyncio
import hashlib
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

CONTINUE_SECONDS = 300
RUN_TIMEOUT = 60

_LOGGER = logging.getLogger("ha_relay")
_conversations: dict[str, tuple[str, float]] = {}   # token hash -> (conversation_id, last used)


class AuthError(Exception):
    pass


async def run_pipeline(ha_ws: str, token: str, text: str, language: str | None,
                       conversation_id: str | None, pipeline: str | None) -> dict:
    async with websockets.connect(f"{ha_ws}/api/websocket", max_size=None, open_timeout=10) as ws:
        await ws.recv()                                             # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise AuthError("access token rejected")

        run = {"id": 1, "type": "assist_pipeline/run", "start_stage": "intent", "end_stage": "intent",
               "input": {"text": text}}
        if conversation_id:
            run["conversation_id"] = conversation_id
        if pipeline:
            run["pipeline"] = pipeline
        await ws.send(json.dumps(run))

        intent_output = None
        async with asyncio.timeout(RUN_TIMEOUT):
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "result" and not msg.get("success"):
                    raise RuntimeError(msg.get("error", {}).get("message", "pipeline run failed"))
                if msg.get("type") != "event":
                    continue
                event = msg["event"]
                if event["type"] == "intent-end":
                    intent_output = event["data"]["intent_output"]
                elif event["type"] == "error":
                    raise RuntimeError(f"{event['data'].get('code')}: {event['data'].get('message')}")
                elif event["type"] == "run-end":
                    break
        if intent_output is None:
            raise RuntimeError("pipeline finished without an answer")
        return intent_output


def last_user_text(messages: list) -> str:
    """Text of the newest user message; content may be a string or a list of OpenAI content parts."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return str(content or "").strip()
    return ""


def make_handler(args):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path in ("/api", "/health"):
                self._send(200, {"message": "API running."})
            elif path == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": "assist", "object": "model", "owned_by": "home-assistant"}]})
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            if path not in ("/api/conversation/process", "/v1/chat/completions"):
                self._send(404, {"message": "not found"})
                return
            openai_style = path == "/v1/chat/completions"
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                self._error(401, "missing bearer token (use a Home Assistant long-lived access token)", openai_style)
                return
            token = auth[7:].strip()
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if openai_style:
                    text = last_user_text(req.get("messages", []))
                    conv_id, language = None, None
                else:
                    text = str(req.get("text", "")).strip()
                    conv_id, language = req.get("conversation_id"), req.get("language")
                if not text:
                    self._error(400, "missing text", openai_style)
                    return
                out = self._converse(token, text, language, conv_id)
                if not openai_style:
                    self._send(200, out)
                    return
                speech = out.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
                self._send(200, {
                    "id": f"chatcmpl-{int(time.time() * 1000)}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": req.get("model") or "assist",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": speech}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
            except AuthError as e:
                self._error(401, str(e), openai_style)
            except Exception as e:  # noqa: BLE001 - report any failure to the watch
                _LOGGER.exception("pipeline run failed for %r", locals().get("text"))
                self._error(502, f"Assist pipeline failed: {e}", openai_style)

        def _converse(self, token: str, text: str, language, conv_id) -> dict:
            key = hashlib.sha256(token.encode()).hexdigest()
            if not conv_id and key in _conversations and time.time() - _conversations[key][1] < CONTINUE_SECONDS:
                conv_id = _conversations[key][0]
            started = time.perf_counter()
            out = asyncio.run(run_pipeline(args.ha, token, text, language, conv_id, args.pipeline))
            if out.get("conversation_id"):
                _conversations[key] = (out["conversation_id"], time.time())
            speech = out.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
            _LOGGER.info("%r -> %r (%.0f ms)", text, speech, (time.perf_counter() - started) * 1000)
            return out

        def _error(self, status: int, message: str, openai_style: bool) -> None:
            if openai_style:
                self._send(status, {"error": {"message": message, "type": "relay_error", "code": status}})
            else:
                self._send(status, {"message": message})

        def log_message(self, fmt, *a):
            _LOGGER.debug(fmt, *a)

    return Handler


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=10320)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--ha", default="ws://homeassistant.local:8123")
    p.add_argument("--pipeline", default=None, help="pipeline id; default = HA's preferred pipeline")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _LOGGER.info("listening on %s:%d -> %s (pipeline: %s)", args.bind, args.port, args.ha, args.pipeline or "preferred")
    ThreadingHTTPServer((args.bind, args.port), make_handler(args)).serve_forever()
