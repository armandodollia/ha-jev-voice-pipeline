"""Model router for a multi-PC setup: the orchestrator PC keeps the public ports (LLM 8080, Wyoming STT 10300) and
forwards each connection to its own llama-server/Whisper/Piper or to a worker PC's. Clients (Home Assistant, the
speech API, phone apps) keep using the orchestrator's address and never see the switch. Kinds: llm, stt (Wyoming
Whisper) and, when configured, tts (Wyoming Piper) and decision (a second LLM port for Jev-mode /v1/decision, so
decisions can prefer a worker's model while chat stays on the orchestrator's bigger one; it uses the same llama-server
backends as llm unless "local"/worker "decision" addresses are given).

Routing, per new connection (TCP level, so streaming, Jev-mode /v1/decision and Wyoming all pass through untouched):
  - while a game/stream runs on the orchestrator (status.json activity) and a worker is healthy -> the worker,
    so inference doesn't compete with the game for the GPU
  - otherwise the local backend if it's healthy, else the first healthy worker, else the local backend anyway
  - kinds listed in preferWorkers (e.g. ["stt", "tts"]) always go to a healthy worker first, so the orchestrator's GPU
    is free for a bigger LLM; the local backend is the fallback. Workers are kept asked for them all the time, but
    Wake-on-LAN is only sent for real need (game/stream, local down), so a worker that was shut down stays off.
While no backend is healthy (a worker still loading), new connections wait up to waitForBackendSec instead of failing.
Idle keep-alive connections to a backend that is no longer preferred are closed so clients reconnect to the new one.

Worker assignment: workers poll GET /assignment on the control port and load models only while asked:
  - asked as soon as a game/stream starts, or once the local backend has been down for requestAfterSec
  - released once nothing runs and the local backend has been healthy for releaseAfterSec
A worker still applies its own VRAM tiers and game rules on top (see homellm.ps1, cluster.role = worker).

Usage: router.py <config.json>   (see the "cluster" section of config.example.json)
"""
import asyncio
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CFG = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
if "listen" not in CFG:
    # the kit's config.json (cluster.role = orchestrator): derive the router settings from it
    kit, root = CFG, Path(sys.argv[1]).resolve().parent
    cl, public_llm, public_stt = kit["cluster"], kit["llm"]["port"], kit["voice"]["ports"]["wyomingStt"]
    CFG = {
        "listen": {"llm": public_llm, "stt": public_stt, "control": cl.get("controlPort", 8079),
                   **({"tts": kit["voice"]["ports"]["wyomingTts"]} if cl["localPorts"].get("tts") else {})},
        "local": {"llm": f"127.0.0.1:{cl['localPorts']['llm']}", "stt": f"127.0.0.1:{cl['localPorts']['stt']}",
                  **({"tts": f"127.0.0.1:{cl['localPorts']['tts']}"} if cl["localPorts"].get("tts") else {})},
        "workers": [{"name": w["name"], "mac": w.get("mac"), "llm": f"{w['host']}:{w.get('llmPort', public_llm)}",
                     "stt": f"{w['host']}:{w.get('sttPort', public_stt)}",
                     **({"tts": f"{w['host']}:{w.get('ttsPort', kit['voice']['ports']['wyomingTts'])}"}
                        if cl["localPorts"].get("tts") else {})} for w in cl.get("workers", [])],
        "statusFile": str(root / "state" / "status.json"), "stateFile": str(root / "state" / "router.json"),
        "logFile": str(root / "logs" / "router.log"),
        "requestAfterSec": cl.get("requestAfterSec", 15), "releaseAfterSec": cl.get("releaseAfterSec", 60),
        "preferWorkers": cl.get("preferWorkers", []),
        **({"decisionPort": cl["decisionPort"]} if cl.get("decisionPort") else {}),
        "waitForBackendSec": cl.get("waitForBackendSec", 30),
    }
if CFG.get("decisionPort"):
    CFG["listen"]["decision"] = CFG["decisionPort"]
if "decision" in CFG["listen"]:
    CFG["local"].setdefault("decision", CFG["local"]["llm"])
    for w in CFG.get("workers", []):
        if w.get("llm"):
            w.setdefault("decision", w["llm"])
LOG = Path(CFG["logFile"])
STATUS_FILE = Path(CFG["statusFile"])
STATE_FILE = Path(CFG["stateFile"])
REQUEST_AFTER = CFG.get("requestAfterSec", 15)
RELEASE_AFTER = CFG.get("releaseAfterSec", 60)
WAIT_FOR_BACKEND = CFG.get("waitForBackendSec", 30)   # hold a new connection this long while no backend is up
KINDS = (("llm", "stt") + (("tts",) if "tts" in CFG["listen"] and "tts" in CFG["local"] else ())
         + (("decision",) if "decision" in CFG["listen"] else ()))
LLM_KINDS = ("llm", "decision")             # llama-server backends (HTTP /health); the rest speak Wyoming
PREFER = set(CFG.get("preferWorkers", [])) & set(KINDS)   # kinds that go to a healthy worker first


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def hostport(s):
    h, p = s.rsplit(":", 1)
    return h, int(p)


class Backend:
    def __init__(self, name, kind, addr):
        self.name, self.kind = name, kind
        self.host, self.port = hostport(addr)
        self.healthy = False
        self.changed = time.monotonic()   # when healthy last flipped


BACKENDS = {k: [Backend("local", k, CFG["local"][k])] +
               [Backend(w["name"], k, w[k]) for w in CFG.get("workers", []) if w.get(k)] for k in KINDS}
WANTED = {k: False for k in KINDS}       # what workers are asked to load (need, or preferWorkers)
NEEDED = {k: False for k in KINDS}       # real need: game/stream or local down (drives Wake-on-LAN)
ROUTE = {k: "local" for k in KINDS}
WORKER_SEEN = {}                          # worker name -> last /assignment poll (time, query)
WOL = {w["name"]: w["mac"] for w in CFG.get("workers", []) if w.get("mac")}
WOL_SENT = {}                             # worker name -> (last send time, sends this episode)
WOL_EVERY, WOL_MAX = 60, 5                # resend every minute, at most 5 times per episode


def send_wol(mac):
    raw = bytes.fromhex(mac.replace("-", "").replace(":", ""))
    packet = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in ("255.255.255.255", CFG.get("wolBroadcast", "255.255.255.255")):
            for port in (9, 7):
                s.sendto(packet, (addr, port))


def wake_workers():
    """While workers are wanted, wake any that hasn't polled in 20 s (asleep or off): Wake-on-LAN, resent every
    minute, up to WOL_MAX times until it shows up again."""
    now = time.monotonic()
    wanted = NEEDED["llm"] or NEEDED["stt"] or NEEDED.get("decision", False)
    for name, mac in WOL.items():
        seen = WORKER_SEEN.get(name, (-1e9, None))[0]
        if not wanted or now - seen < 20:
            WOL_SENT.pop(name, None)
            continue
        last, count = WOL_SENT.get(name, (-1e9, 0))
        if count < WOL_MAX and now - last >= WOL_EVERY:
            try:
                send_wol(mac)
                log(f"{name}: not responding, sent Wake-on-LAN ({count + 1}/{WOL_MAX})")
            except Exception as e:
                log(f"{name}: Wake-on-LAN failed: {e}")
            WOL_SENT[name] = (now, count + 1)
CONNS = set()


class Conn:
    def __init__(self, kind, backend, cw):
        self.kind, self.backend, self.cw = kind, backend, cw
        self.last, self.last_dir = time.monotonic(), "c2s"
activity = "idle"
pending = None                             # tier upgrade the local supervisor is waiting to do (status.json)
pending_seen = -1e9                        # last time an upgrade was pending
SWAP_GRACE = 30                            # keep preferring workers this long after the local model swap


async def check_llm(b):
    r, w = await asyncio.wait_for(asyncio.open_connection(b.host, b.port), 1.5)
    try:
        w.write(f"GET /health HTTP/1.1\r\nHost: {b.host}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 1.5)
        return b" 200 " in line          # llama-server answers 503 while a model is loading
    finally:
        w.close()


async def check_stt(b):
    r, w = await asyncio.wait_for(asyncio.open_connection(b.host, b.port), 1.5)
    try:
        w.write(b'{"type": "describe"}\n')
        await w.drain()
        line = await asyncio.wait_for(r.readline(), 2.0)
        return b'"info"' in line
    finally:
        w.close()


async def probe(b):
    try:
        ok = await (check_llm(b) if b.kind in LLM_KINDS else check_stt(b))   # stt and tts both speak Wyoming
    except Exception:
        ok = False
    if ok != b.healthy:
        b.healthy, b.changed = ok, time.monotonic()
        log(f"{b.kind} {b.name} {'up' if ok else 'down'}")


def read_status():
    try:
        st = json.loads(STATUS_FILE.read_text(encoding="utf-8-sig"))
        return st.get("activity") or "idle", st.get("pending")
    except Exception:
        return activity, pending           # file mid-write: keep the last values


def order(kind):
    local, workers = BACKENDS[kind][0], BACKENDS[kind][1:]
    up = [w for w in workers if w.healthy]
    # workers first while a game/stream runs, and while the local supervisor is about to swap its model (and a bit
    # after), so nobody hits the local server mid-swap
    swapping = kind in LLM_KINDS and time.monotonic() - pending_seen < SWAP_GRACE
    if (activity != "idle" or swapping or kind in PREFER) and up:
        return up + [local] + [w for w in workers if not w.healthy]
    return sorted(BACKENDS[kind], key=lambda b: not b.healthy)   # stable: local first among equals


def update_wanted(kind):
    if kind == "tts":
        return                              # Piper runs on the CPU all the time; nothing to load or wake for
    local, now = BACKENDS[kind][0], time.monotonic()
    busy = activity != "idle"
    if busy or (not local.healthy and now - local.changed >= REQUEST_AFTER):
        want = True
    elif local.healthy and now - local.changed >= RELEASE_AFTER and not (kind in LLM_KINDS and pending):
        # (an upgrade still pending means the local model is about to be swapped: keep the worker through it)
        want = False
    else:
        want = NEEDED[kind]
    NEEDED[kind] = want
    want = want or kind in PREFER
    if want != WANTED[kind]:
        WANTED[kind] = want
        log(f"{kind}: {'asking workers to load' if want else 'releasing workers'} "
            f"(activity {activity}, local {'up' if local.healthy else 'down'}"
            f"{', preferred on workers' if kind in PREFER else ''})")


async def monitor():
    global activity, pending, pending_seen
    while True:
        new, pending = read_status()
        if pending:
            pending_seen = time.monotonic()
        if new != activity:
            log(f"activity: {new}")
            activity = new
        await asyncio.gather(*(probe(b) for k in KINDS for b in BACKENDS[k]))
        now = time.monotonic()
        for k in KINDS:
            update_wanted(k)
            first = order(k)[0].name
            if first != ROUTE[k]:
                log(f"{k} route: {ROUTE[k]} -> {first}")
                ROUTE[k] = first
            # close idle keep-alive connections that point at a backend that's no longer preferred
            for c in [c for c in CONNS if c.kind == k and c.backend != first]:
                if c.last_dir == "s2c" and now - c.last > 5:
                    c.cw.close()
        wake_workers()
        STATE_FILE.write_text(json.dumps({
            "time": datetime.now().isoformat(timespec="seconds"), "activity": activity, "route": ROUTE,
            "wanted": WANTED, "needed": NEEDED, "prefer_workers": sorted(PREFER), "local_pending_upgrade": pending,
            "backends": {k: {b.name: b.healthy for b in BACKENDS[k]} for k in KINDS},
            "workers": {n: {"last_poll_s": round(now - t), **q} for n, (t, q) in WORKER_SEEN.items()},
            "connections": len(CONNS)}, indent=2), encoding="utf-8")
        await asyncio.sleep(1)


async def pipe(src, dst, conn, direction):
    try:
        while data := await src.read(65536):
            conn.last, conn.last_dir = time.monotonic(), direction
            dst.write(data)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


def proxy_handler(kind):
    async def handle(cr, cw):
        # nothing up yet (e.g. the worker is still loading after a game started): wait instead of failing
        # and retry if every backend refuses (one was just stopped and the health check hasn't noticed yet)
        deadline = time.monotonic() + WAIT_FOR_BACKEND
        b = None
        while b is None:
            # (only worth waiting for if a worker is alive: it polled /assignment recently)
            alive = any(time.monotonic() - t < 30 for t, _ in WORKER_SEEN.values())
            while alive and not any(x.healthy for x in BACKENDS[kind]) and time.monotonic() < deadline:
                await asyncio.sleep(0.25)
            for x in order(kind):
                try:
                    br, bw = await asyncio.wait_for(asyncio.open_connection(x.host, x.port), 2)
                    b = x
                    break
                except Exception:
                    continue
            if b is None:
                if time.monotonic() >= deadline or not alive:
                    cw.close()
                    return
                await asyncio.sleep(0.5)
        conn = Conn(kind, b.name, cw)
        CONNS.add(conn)
        try:
            await asyncio.gather(pipe(cr, bw, conn, "c2s"), pipe(br, cw, conn, "s2c"))
        finally:
            CONNS.discard(conn)
    return handle


async def control(cr, cw):
    try:
        line = (await asyncio.wait_for(cr.readline(), 5)).decode("latin-1")
        while (await asyncio.wait_for(cr.readline(), 5)) not in (b"\r\n", b"\n", b""):
            pass
        url = urlsplit(line.split(" ")[1] if " " in line else "/")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/assignment":
            if "worker" in q:
                WORKER_SEEN[q.pop("worker")] = (time.monotonic(), q)
            body, code = {"llm": WANTED["llm"] or WANTED.get("decision", False), "whisper": WANTED["stt"], "activity": activity}, "200 OK"
        elif url.path == "/status":
            body, code = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}, "200 OK"
        else:
            body, code = {"error": "not found"}, "404 Not Found"
        data = json.dumps(body).encode()
        cw.write(f"HTTP/1.1 {code}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + data)
        await cw.drain()
    except Exception:
        pass
    finally:
        cw.close()


async def main():
    lst = CFG["listen"]
    servers = [await asyncio.start_server(proxy_handler(k), "0.0.0.0", lst[k]) for k in KINDS]
    servers.append(await asyncio.start_server(control, "0.0.0.0", lst["control"]))
    log(f"router started: " + ", ".join(f"{k} :{lst[k]}" for k in KINDS) + f", control :{lst['control']}; "
        f"preferred on workers: {', '.join(sorted(PREFER)) or 'none'}; workers: "
        + ", ".join(w["name"] for w in CFG.get("workers", [])))
    await asyncio.gather(monitor(), *(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
