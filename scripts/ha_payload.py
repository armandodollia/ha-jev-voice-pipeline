"""Rebuild the exact /v1/decision payload homellm_assist sends, from live Home Assistant (exposed devices, aliases,
groups, areas). The gateway uses it to re-warm Home Assistant's cached prompt; eval\\run_eval.py scores models with it.
Writes state\\ha_payload.json (contexts empty), state\\ha_device_map.json (every spoken name -> entity_id) and
state\\ha_secure.json (locks and garage/gate/door covers).
Settings: config.json homeAssistant.url and homeAssistant.tokenFile (a long-lived access token of an admin user:
reading the entity and area registries needs admin rights).
Mirrors conversation.py: keep the two in step when the decision schema changes."""
import asyncio
import json
import sys
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "homeassistant" / "custom_components" / "homellm_assist"
sys.path.insert(0, str(COMP))
import const  # noqa: E402  (pure constants, no Home Assistant imports)

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
SRC = (COMP / "conversation.py").read_text(encoding="utf-8")
INSTRUCTIONS = eval("(" + SRC.split("INSTRUCTIONS = (", 1)[1].split("\n)\n", 1)[0] + "\n)")
TOKEN = (ROOT / CFG["homeAssistant"].get("tokenFile", "hass_token.txt")).read_text(encoding="utf-8").strip()
WS = CFG["homeAssistant"]["url"].rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


async def call(ws, i, msg):
    await ws.send(json.dumps({"id": i, **msg}))
    while True:
        r = json.loads(await ws.recv())
        if r.get("id") == i:
            return r["result"]


async def main():
    async with websockets.connect(WS, max_size=50_000_000) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise SystemExit("Home Assistant rejected the token in homeAssistant.tokenFile")
        states = await call(ws, 1, {"type": "get_states"})
        full = await call(ws, 2, {"type": "config/entity_registry/list"})
        expose = await call(ws, 3, {"type": "homeassistant/expose_entity/list"})
        areas = await call(ws, 4, {"type": "config/area_registry/list"})
        # entity_registry/list leaves aliases out; get_entries has them
        detail = await call(ws, 5, {"type": "config/entity_registry/get_entries", "entity_ids": [e["entity_id"] for e in full]})
    ent = {e["entity_id"]: {**e, **((detail or {}).get(e["entity_id"]) or {})} for e in full}
    exposed = {eid for eid, v in expose["exposed_entities"].items() if v.get("conversation")}
    allowed = {d for m in const.ACTIONS.values() for d in m} | {"light"}
    by_id = {s["entity_id"]: s for s in states}

    # _devices(): every spoken name (name + aliases) -> entity_id, first entity wins a duplicate name
    names = {}
    for s in states:
        eid = s["entity_id"]
        if eid.split(".")[0] not in allowed or eid not in exposed:
            continue
        aliases = sorted(a for a in (ent.get(eid, {}).get("aliases") or []) if isinstance(a, str))
        own = s["attributes"].get("friendly_name") or eid.split(".", 1)[1].replace("_", " ")
        for name in [own, *aliases]:
            key = " ".join(str(name).split())
            if key and key.lower() not in {n.lower() for n in names} and key.lower() != "none":
                names[key] = eid
    devices = dict(list(names.items())[:250])

    # _canonical(): one main name per entity; _alias_note(): the others
    first, others = {}, {}
    for n, e in devices.items():
        if e not in first:
            first[e] = n
        else:
            others.setdefault(e, []).append(n)
    # _group_note()
    parts = []
    for e, n in sorted(first.items(), key=lambda x: x[1]):
        members = by_id.get(e, {}).get("attributes", {}).get("entity_id")
        if isinstance(members, list) and members:
            mn = [by_id[m]["attributes"].get("friendly_name", m) if m in by_id else m for m in members]
            parts.append(f"{n} ({', '.join(mn)})")
    note = (" Groups (one name that controls several devices together): " + "; ".join(parts) + ".") if parts else ""
    alias_parts = [f"{', '.join(ns)} = {first[e]}" for e, ns in sorted(others.items(), key=lambda x: first[x[0]])]
    note += (" Other names for devices (answer with the device's main name): " + "; ".join(alias_parts) + ".") if alias_parts else ""
    rooms = [a["name"] for a in areas][:250]

    payload = {
        "model": CFG["llm"].get("alias", "home"), "instructions": INSTRUCTIONS + note,
        "schema": {
            "action": {"type": "enum", "choices": ["turn_on", "turn_off", "open", "close", "lock", "unlock",
                                                   *const.LIGHT_ACTIONS, "other"],
                       "description": "What does the user want done?"},
            "device": {"type": "enum", "choices": [*first.values(), "none"],
                       "description": "Which single device from the list is the user talking about?"},
            "scope": {"type": "enum", "choices": const.SCOPES,
                      "description": "One named device, this room, a room the user names, or the whole house?"},
            "kind": {"type": "enum", "choices": [*const.KINDS, "none"],
                     "description": "Which kind of device (for this_room / named_room / whole_house)"},
            "room": {"type": "enum", "choices": [*rooms, "none"], "description": "The room the user named, if any"},
            "brightness": {"type": "enum", "choices": [*(f"{n}%" for n in const.BRIGHTNESS_STEPS), "none"],
                           "description": "Brightness percentage the user asked to set (not a relative change)"},
            "step": {"type": "enum", "choices": [*const.STEP_WORDS, *(f"{a}%" for a in const.STEP_AMOUNTS), "none"],
                     "description": "How much brighter, dimmer, warmer or cooler (a_little, a_lot, or brightness "
                                    "percentage points); none if the user didn't say"},
            "color": {"type": "enum", "choices": [*const.COLORS, "none"], "description": "Colour the user asked for"},
            "temperature": {"type": "enum", "choices": [*const.KELVIN_WORDS, *const.KELVIN_STEPS, "none"],
                            "description": "White temperature the user asked for (word or Kelvin)"},
        },
        "contexts": [],
    }
    state = ROOT / "state"
    state.mkdir(exist_ok=True)
    (state / "ha_payload.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    (state / "ha_device_map.json").write_text(json.dumps(devices, indent=1), encoding="utf-8")
    # _is_secure(): locks and garage/gate/door covers (the eval applies the security threshold to these)
    secure = sorted(e for e in set(devices.values()) if e.startswith("lock.") or (
        e.startswith("cover.") and by_id.get(e, {}).get("attributes", {}).get("device_class") in const.EXCLUDED_COVER_CLASSES))
    (state / "ha_secure.json").write_text(json.dumps(secure, indent=1), encoding="utf-8")
    print(f"{len(first)} devices ({len(devices)} names), {len(rooms)} rooms -> {state / 'ha_payload.json'}")


if __name__ == "__main__":
    asyncio.run(main())
