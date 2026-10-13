"""Conversation agent: Jev-mode decision first (action + device, chosen only from exposed devices), fallback agent
for everything the decision doesn't cover confidently."""

from __future__ import annotations

import logging
import re
import time
from typing import Literal

import aiohttp

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ACTIONS,
    BRIGHTNESS_STEPS,
    COLORS,
    DEFAULT_STEP,
    CONF_ACTION_THRESHOLD,
    CONF_DECISION_URL,
    CONF_DEVICE_THRESHOLD,
    CONF_FALLBACK_AGENT,
    CONF_VALUE_THRESHOLD,
    DEFAULT_ACTION_THRESHOLD,
    DEFAULT_DECISION_URL,
    DEFAULT_DEVICE_THRESHOLD,
    DEFAULT_FALLBACK_AGENT,
    DEFAULT_VALUE_THRESHOLD,
    DOMAIN,
    EXCLUDED_COVER_CLASSES,
    KELVIN_STEPS,
    KELVIN_WORDS,
    LIGHT_ACTIONS,
    STEP_AMOUNTS,
    STEP_WORDS,
)

_LOGGER = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are the voice assistant of a home. Read what the user said and answer each question. "
    "If the user asks to switch, open or close exactly one device from the list, or to set the brightness, colour "
    "or white temperature of exactly one light from the list, pick that action and device, and the value they asked "
    "for. Brightness is a percentage. Use brighten or dim for relative changes (brighter, dimmer, turn it up/down) "
    "and give the amount if the user said one (a little, a lot, or percentage points). "
    "White temperature is warm, soft, neutral, cool, daylight or a number of Kelvin. "
    "If it is a question, a chat, a warmer/cooler change, several devices or a whole room, "
    "or a device that is not in the list, answer action 'other' and device 'none'. "
    "Answer 'none' for values the user did not ask for."
)
DONE = {"turn_on": "Turned on {}.", "turn_off": "Turned off {}.", "open": "Opening {}.", "close": "Closing {}.",
        "set_brightness": "Set {} to {}%.",
        "brighten": "Brightened {} to {}%.", "dim": "Dimmed {} to {}%.", "set_color": "Set {} to {}.", "set_temperature": "Set {} to {} K."}
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _spoken_percent(text: str, after: str | None = None) -> int | None:
    """The percentage actually said ("5%", "5 percent", "twenty five percent", "a hundred percent"), optionally
    only after a word such as "by". Exact, unlike scoring digit strings (the model confuses 5 with 50)."""
    t = text.lower().replace("-", " ")
    if after:
        idx = t.find(f" {after} ")
        if idx < 0:
            return None
        t = t[idx + len(after) + 2:]
    m = re.search(r"(\d{1,3})\s*(?:%|percent|per cent)", t)
    if m:
        return int(m.group(1)) if int(m.group(1)) <= 100 else None
    m = re.search(r"((?:[a-z]+\s+){1,3})(?:percent|per cent)", t)
    if not m:
        return None
    words = m.group(1).split()
    if words[-1] == "hundred":
        return 100
    total, seen = 0, False
    for w in words:
        if w in _TENS:
            total, seen = _TENS[w], True
        elif w in _ONES:
            total, seen = total + _ONES[w], True
        elif seen:
            break
        else:
            total = 0
    return total if seen and total <= 100 else None


# light colour modes that accept each setting
BRIGHTNESS_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([HomeLlmAssistAgent(entry)])


class HomeLlmAssistAgent(conversation.ConversationEntity, conversation.AbstractConversationAgent):
    """Decision-first conversation agent."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL

    def __init__(self, entry: ConfigEntry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}, "name": "Home LLM Assist"}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    def _opt(self, key, default):
        return {**self.entry.data, **self.entry.options}.get(key, default)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    def _devices(self) -> dict[str, str]:
        """Spoken name -> entity_id for every device the fast path may control (exposed to Assist, supported
        domain, not security-sensitive). Names and aliases; the first entity wins on a duplicate name."""
        reg = er.async_get(self.hass)
        names: dict[str, str] = {}
        allowed = {d for m in ACTIONS.values() for d in m} | {"light"}
        for state in self.hass.states.async_all():
            eid = state.entity_id
            domain = eid.split(".")[0]
            if domain not in allowed or not async_should_expose(self.hass, conversation.DOMAIN, eid):
                continue
            if domain == "cover" and state.attributes.get("device_class") in EXCLUDED_COVER_CLASSES:
                continue
            entry = reg.async_get(eid)
            # aliases may also hold a marker for "the entity's own name" (not a str): skip it, state.name covers it
            aliases = sorted(a for a in (entry.aliases if entry else []) if isinstance(a, str))
            for name in [state.name, *aliases]:
                key = " ".join(str(name).split())
                if key and key.lower() not in {n.lower() for n in names} and key.lower() != "none":
                    names[key] = eid
        return dict(list(names.items())[:250])   # the engine allows up to 255 values per field

    async def _decide(self, text: str, devices: dict[str, str]) -> dict | None:
        payload = {
            "model": "home",
            "instructions": INSTRUCTIONS,
            "schema": {
                "action": {"type": "enum",
                           "choices": ["turn_on", "turn_off", "open", "close", *LIGHT_ACTIONS, "other"],
                           "description": "What does the user want done?"},
                "device": {"type": "enum", "choices": [*devices.keys(), "none"],
                           "description": "Which single device from the list is the user talking about?"},
                "brightness": {"type": "enum", "choices": [*(f"{n}%" for n in BRIGHTNESS_STEPS), "none"],
                               "description": "Brightness percentage the user asked to set (not a relative change)"},
                "step": {"type": "enum", "choices": [*STEP_WORDS, *(f"{a}%" for a in STEP_AMOUNTS), "none"],
                         "description": "How much brighter or dimmer (a_little, a_lot, or percentage points); "
                                        "none if the user didn't say"},
                "color": {"type": "enum", "choices": [*COLORS, "none"],
                          "description": "Colour the user asked for"},
                "temperature": {"type": "enum", "choices": [*KELVIN_WORDS, *KELVIN_STEPS, "none"],
                                "description": "White temperature the user asked for (word or Kelvin)"},
            },
            "contexts": [f'The user said: "{text}"'],
        }
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(self._opt(CONF_DECISION_URL, DEFAULT_DECISION_URL), json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    _LOGGER.warning("decision endpoint returned %s", resp.status)
                    return None
                data = await resp.json()
                return data["results"][0]
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError) as err:
            _LOGGER.warning("decision endpoint failed: %s", err)
            return None

    def _plan(self, action: str, eid: str, result: dict, text: str) -> tuple[str, str, dict, str] | None:
        """(domain, service, extra data, spoken value) for a confident, supported command, else None."""
        domain = eid.split(".")[0]
        if action not in LIGHT_ACTIONS:
            service = ACTIONS.get(action, {}).get(domain)
            return (domain, service, {}, "") if service else None
        state = self.hass.states.get(eid)
        if domain != "light" or state is None:
            return None
        modes = set(state.attributes.get("supported_color_modes") or [])
        if action in ("brighten", "dim"):
            if not modes & BRIGHTNESS_MODES:
                return None
            step_word = result["decision"].get("step")
            confident = result["fields"]["step"]["probability"] >= self._opt(CONF_VALUE_THRESHOLD, DEFAULT_VALUE_THRESHOLD)
            step = _spoken_percent(text, after="by")
            target = _spoken_percent(text) if step is None else None
            if target is not None:   # "dim the lights to 20 percent" is an absolute level
                return "light", "turn_on", {"brightness_pct": max(1, target)}, str(max(1, target))
            if step is None:
                step = (STEP_WORDS.get(step_word) or int(step_word.rstrip("%")))                     if confident and step_word not in (None, "none") else DEFAULT_STEP
            current = round((state.attributes.get("brightness") or 0) / 2.55) if state.state == "on" else 0
            # dimming stops at 1% (never off); brightening an off light turns it on at the step
            pct = min(100, current + step) if action == "brighten" else max(1, current - step)
            return "light", "turn_on", {"brightness_pct": pct}, str(pct)
        field = {"set_brightness": "brightness", "set_color": "color", "set_temperature": "temperature"}[action]
        value = result["decision"].get(field)
        if action == "set_brightness":
            spoken = _spoken_percent(text)
            if spoken is not None:   # a number was said: use it exactly
                value = str(spoken)
                result["fields"][field]["probability"] = 1.0
        if value in (None, "none") or result["fields"][field]["probability"] < self._opt(
                CONF_VALUE_THRESHOLD, DEFAULT_VALUE_THRESHOLD):
            return None
        if action == "set_brightness" and modes & BRIGHTNESS_MODES:
            pct = int(str(value).rstrip("%"))
            return ("light", "turn_off", {}, "0") if pct == 0 else ("light", "turn_on", {"brightness_pct": pct}, str(pct))
        if action == "set_color" and modes & COLOR_MODES:
            return "light", "turn_on", {"color_name": value}, value
        if action == "set_temperature" and "color_temp" in modes:
            kelvin = KELVIN_WORDS.get(value) or int(value)
            lo = state.attributes.get("min_color_temp_kelvin") or kelvin
            hi = state.attributes.get("max_color_temp_kelvin") or kelvin
            kelvin = max(lo, min(hi, kelvin))
            return "light", "turn_on", {"color_temp_kelvin": kelvin}, str(kelvin)
        return None   # the light can't do that: let the fallback agent explain

    async def async_process(self, user_input: conversation.ConversationInput) -> conversation.ConversationResult:
        start = time.monotonic()
        devices = self._devices()
        result = await self._decide(user_input.text, devices) if devices else None
        if result:
            action = result["decision"].get("action")
            device = result["decision"].get("device")
            p_action = result["fields"]["action"]["probability"]
            p_device = result["fields"]["device"]["probability"]
            eid = devices.get(device)
            call = self._plan(action, eid, result, user_input.text) if eid else None
            action_threshold = self._opt(CONF_ACTION_THRESHOLD, DEFAULT_ACTION_THRESHOLD)
            if action in ("set_brightness", "brighten", "dim") and _spoken_percent(user_input.text) is not None:
                # with a spoken number the model often splits between set/dim/brighten; all of them end in the
                # same exact level, so a weaker majority is enough
                action_threshold = min(action_threshold, 0.5)
            ok = (call is not None and p_action >= action_threshold
                  and p_device >= self._opt(CONF_DEVICE_THRESHOLD, DEFAULT_DEVICE_THRESHOLD))
            _LOGGER.info("'%s' -> %s %.2f / %s (%s) %.2f / %s -> %s in %.0f ms", user_input.text, action, p_action,
                         device, eid, p_device,
                         {k: result["decision"].get(k) for k in ("brightness", "step", "color", "temperature")},
                         call if ok else "fallback", (time.monotonic() - start) * 1000)
            if ok:
                domain, service, data, shown = call
                await self.hass.services.async_call(domain, service, {"entity_id": eid, **data}, blocking=True,
                                                    context=user_input.context)
                response = intent.IntentResponse(language=user_input.language)
                response.async_set_speech(DONE[action].format(device, shown))
                return conversation.ConversationResult(response=response, conversation_id=user_input.conversation_id)
        # not a confident single-device command: let the fallback agent handle it as before
        return await conversation.async_converse(
            self.hass, user_input.text, user_input.conversation_id, user_input.context,
            language=user_input.language, agent_id=self._opt(CONF_FALLBACK_AGENT, DEFAULT_FALLBACK_AGENT),
            device_id=user_input.device_id, satellite_id=user_input.satellite_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )
