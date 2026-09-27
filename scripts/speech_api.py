"""OpenAI-compatible speech endpoints in front of the Wyoming Whisper and Piper servers.

POST /v1/audio/transcriptions (multipart: file=WAV, model, language, response_format)
-> forwards the audio to Wyoming faster-whisper -> {"text": "..."}

POST /v1/audio/speech (JSON: input, voice, model, response_format)
-> synthesizes with Wyoming Piper -> audio/wav

For phone/watch apps that speak the OpenAI audio API (e.g. Wristotle for Pebble). Reuses the
already-loaded models, so it costs no extra VRAM. The model field is ignored. OpenAI voice names
(alloy, nova, ...) use Piper's default voice; a Piper voice name ("en_US-amy-medium") selects it.

Usage: python speech_api.py [--port 10310] [--stt-port 10300] [--tts-port 10200]
"""
import argparse
import asyncio
import io
import json
import logging
import wave
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice

WYOMING_HOST = "127.0.0.1"
PIPER_VOICE_PREFIXES = ("ar_", "ca_", "cs_", "da_", "de_", "el_", "en_", "es_", "fa_", "fi_", "fr_", "hu_", "is_",
                        "it_", "ka_", "kk_", "lb_", "nl_", "no_", "pl_", "pt_", "ro_", "ru_", "sk_", "sl_", "sr_",
                        "sv_", "sw_", "tr_", "uk_", "vi_", "zh_")
CHUNK_BYTES = 2048 * 2

_LOGGER = logging.getLogger("speech_api")
ARGS: argparse.Namespace


async def transcribe(wav_bytes: bytes, language: str) -> str:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    async with AsyncTcpClient(WYOMING_HOST, ARGS.stt_port) as client:
        await client.write_event(Transcribe(language=language or None).event())
        await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        for i in range(0, len(pcm), CHUNK_BYTES):
            await client.write_event(AudioChunk(rate=rate, width=width, channels=channels,
                                                audio=pcm[i:i + CHUNK_BYTES]).event())
        await client.write_event(AudioStop().event())
        while True:
            event = await client.read_event()
            if event is None:
                raise ConnectionError("Whisper closed the connection")
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text.strip()


async def synthesize(text: str, voice: str) -> bytes:
    use_voice = SynthesizeVoice(name=voice) if voice.startswith(PIPER_VOICE_PREFIXES) else None
    pcm = bytearray()
    rate, width, channels = 22050, 2, 1
    async with AsyncTcpClient(WYOMING_HOST, ARGS.tts_port) as client:
        await client.write_event(Synthesize(text=text, voice=use_voice).event())
        while True:
            event = await client.read_event()
            if event is None:
                raise ConnectionError("Piper closed the connection")
            if AudioStart.is_type(event.type):
                start = AudioStart.from_event(event)
                rate, width, channels = start.rate, start.width, start.channels
            elif AudioChunk.is_type(event.type):
                pcm += AudioChunk.from_event(event).audio
            elif AudioStop.is_type(event.type):
                break
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


def parse_multipart(content_type: str, body: bytes) -> dict:
    msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[name] = part.get_payload(decode=True) or b""
    return parts


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": "whisper", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/audio/speech"):
            self._speech()
        elif path.endswith("/audio/transcriptions"):
            self._transcription()
        else:
            self._send(404, {"error": {"message": "not found"}})

    def _transcription(self):
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            parts = parse_multipart(self.headers.get("Content-Type", ""), body)
            if "file" not in parts:
                self._send(400, {"error": {"message": "missing 'file' part"}})
                return
            language = parts.get("language", b"").decode().strip()
            text = asyncio.run(transcribe(parts["file"], language))
            _LOGGER.info("%s: %r", self.client_address[0], text)
            if parts.get("response_format", b"json").decode().strip() == "text":
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(200, {"text": text})
        except wave.Error as e:
            self._send(400, {"error": {"message": f"audio must be PCM WAV: {e}"}})
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            _LOGGER.exception("transcription failed")
            self._send(502, {"error": {"message": f"transcription failed: {e}"}})

    def _speech(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            text = str(req.get("input", "")).strip()
            if not text:
                self._send(400, {"error": {"message": "missing 'input'"}})
                return
            fmt = str(req.get("response_format", "wav")).lower()
            if fmt not in ("wav", "pcm"):
                self._send(400, {"error": {"message": f"response_format '{fmt}' not supported; use wav"}})
                return
            wav_bytes = asyncio.run(synthesize(text, str(req.get("voice", ""))))
            _LOGGER.info("%s: speak %r (%d bytes)", self.client_address[0], text[:80], len(wav_bytes))
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.end_headers()
            self.wfile.write(wav_bytes)
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "body must be JSON"}})
        except Exception as e:  # noqa: BLE001 - report any failure to the client
            _LOGGER.exception("speech failed")
            self._send(502, {"error": {"message": f"speech failed: {e}"}})

    def log_message(self, fmt, *args):
        _LOGGER.debug(fmt, *args)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=10310)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--stt-port", type=int, default=10300)
    p.add_argument("--tts-port", type=int, default=10200)
    ARGS = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _LOGGER.info("listening on %s:%d -> Whisper :%d, Piper :%d", ARGS.bind, ARGS.port, ARGS.stt_port, ARGS.tts_port)
    ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler).serve_forever()
