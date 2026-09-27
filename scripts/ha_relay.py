"""Relay that runs Wristotle's Home Assistant commands through a real Assist pipeline.

Wristotle posts to {base}/api/conversation/process without an agent_id, so Home Assistant
answers with its built-in agent and skips the Assist pipeline (LLM agent, "prefer handling
commands locally", ...). This relay accepts the same request and runs it with
`assist_pipeline/run` over HA's websocket API instead, then returns the pipeline's
intent output, which has the same shape as the conversation API's response.

The caller's own bearer token is passed through to Home Assistant; nothing is stored.
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
            if self.path.rstrip("/") in ("/api", "/health"):
                self._send(200, {"message": "API running."})
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self):
            if self.path.split("?")[0].rstrip("/") != "/api/conversation/process":
                self._send(404, {"message": "not found"})
                return
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                self._send(401, {"message": "missing bearer token"})
                return
            token = auth[7:].strip()
            key = hashlib.sha256(token.encode()).hexdigest()
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                text = str(req.get("text", "")).strip()
                if not text:
                    self._send(400, {"message": "missing text"})
                    return
                conv_id = req.get("conversation_id")
                if not conv_id and key in _conversations and time.time() - _conversations[key][1] < CONTINUE_SECONDS:
                    conv_id = _conversations[key][0]
                started = time.perf_counter()
                out = asyncio.run(run_pipeline(args.ha, token, text, req.get("language"), conv_id, args.pipeline))
                if out.get("conversation_id"):
                    _conversations[key] = (out["conversation_id"], time.time())
                speech = out.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
                _LOGGER.info("%r -> %r (%.0f ms)", text, speech, (time.perf_counter() - started) * 1000)
                self._send(200, out)
            except AuthError as e:
                self._send(401, {"message": str(e)})
            except Exception as e:  # noqa: BLE001 - report any failure to the watch
                _LOGGER.exception("pipeline run failed for %r", locals().get("text"))
                self._send(502, {"message": f"Assist pipeline failed: {e}"})

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
