"""Score a Jev-mode server on homellm_assist's real decision payload, the way the integration would act on it.
Usage:  <voice python> scripts\\ha_payload.py              (once: rebuild the payload from live Home Assistant)
        <voice python> eval\\run_eval.py <base_url> <label> [cases.json]
base_url is a llama-server with /v1/decision (e.g. http://127.0.0.1:8081, or a worker's http://<ip>:8080).
cases.json (default eval\\cases.json; start from cases.example.json): [["what the user said", {expected fields}], ...]
with your own device and room names. Prints misses, 'correct and confident' (what the integration would execute, with
its thresholds) and latency; saves eval\\results_<label>.json. Mirrors the integration: a spoken percentage overrides
the scored number, an alias counts as its device, room/house commands need confident scope/kind/room."""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE.parent / "state"
base, label = sys.argv[1].rstrip("/"), sys.argv[2]
cases_file = Path(sys.argv[3]) if len(sys.argv) > 3 else HERE / "cases.json"
tmpl = json.loads((STATE / "ha_payload.json").read_text(encoding="utf-8"))
DEVMAP = json.loads((STATE / "ha_device_map.json").read_text(encoding="utf-8"))
SECURE = set(json.loads((STATE / "ha_secure.json").read_text(encoding="utf-8")))
cases = json.loads(cases_file.read_text(encoding="utf-8"))
_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}


def spoken_percent(text):
    t = text.lower().replace("-", " ")
    m = re.search(r"(\d{1,3})\s*(?:%|percent|per cent)", t)
    if m:
        return int(m.group(1)) if int(m.group(1)) <= 100 else None
    m = re.search(r"([a-z]+)\s+(?:percent|per cent)", t)
    return _WORDS.get(m.group(1)) if m else None


def decide(text):
    body = dict(tmpl, contexts=[f'The user said: "{text}"'])
    req = urllib.request.Request(f"{base}/v1/decision", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.load(r)["results"][0]
    return res, time.time() - t


decide("turn on the lights")     # warm the prompt cache (the first call processes the instructions)
fields_ok = fields_n = acts = 0
lat, out = [], []
for text, exp in cases:
    res, dt = decide(text)
    lat.append(dt)
    got = {k: res["fields"][k]["value"] for k in res["fields"]}
    prob = {k: res["fields"][k]["probability"] for k in res["fields"]}
    spoken = spoken_percent(text)
    if spoken is not None and got.get("action") in ("set_brightness", "brighten", "dim") and " by " not in text.lower():
        got["action"], got["brightness"], prob["brightness"] = "set_brightness", f"{spoken}%", 1.0
    if "device" in exp and DEVMAP.get(got.get("device")) and DEVMAP.get(got.get("device")) == DEVMAP.get(exp["device"]):
        got["device"] = exp["device"]
    miss = {k: (v, got.get(k)) for k, v in exp.items() if got.get(k) != v}
    fields_ok += len(exp) - len(miss)
    fields_n += len(exp)
    thr = 0.9 if DEVMAP.get(exp.get("device")) in SECURE else None
    if exp.get("action") == "other":
        ok = not miss
    else:
        a_thr = thr or (0.5 if exp.get("action") in ("set_brightness", "brighten", "dim") and spoken is not None else 0.7)
        ok = not miss and prob["action"] >= a_thr and ("device" not in exp or prob["device"] >= (thr or 0.6))
        if exp.get("scope") not in (None, "one_device"):
            ok = ok and prob["scope"] >= 0.7 and prob["kind"] >= 0.7 and ("room" not in exp or prob["room"] >= 0.6)
    acts += ok
    out.append({"text": text, "ok": ok, "miss": miss, "got": got, "prob": prob, "sec": round(dt, 3)})
    if not ok:
        print(f"  MISS {text!r}: " + ", ".join(f"{k} want {w!r} got {g!r} ({prob.get(k, 0):.2f})" for k, (w, g) in miss.items())
              + ("" if miss else f"  (right, but low confidence: action {prob['action']:.2f} device {prob['device']:.2f})"))
lat.sort()
print(f"{label}: correct-and-confident {acts}/{len(cases)}, fields {fields_ok}/{fields_n}, "
      f"latency median {lat[len(lat) // 2]:.2f}s p90 {lat[int(len(lat) * 0.9)]:.2f}s max {lat[-1]:.2f}s")
(HERE / f"results_{label}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
