"""Conversation agent: Jev-mode decision first (action + device, chosen only from exposed devices), fallback agent
for everything the decision doesn't cover confidently."""

from __future__ import annotations

import logging
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
    CONF_ACTION_THRESHOLD,
    CONF_DECISION_URL,
    CONF_DEVICE_THRESHOLD,
    CONF_FALLBACK_AGENT,
    DEFAULT_ACTION_THRESHOLD,
    DEFAULT_DECISION_URL,
    DEFAULT_DEVICE_THRESHOLD,
    DEFAULT_FALLBACK_AGENT,
    DOMAIN,
    EXCLUDED_COVER_CLASSES,
)

_LOGGER = logging.getLogger(__name__)

INSTRUCTIONS = (
    "You are the voice assistant of a home. Read what the user said and answer each question. "
    "If the user asks to switch, open or close exactly one device from the list, pick that action and device. "
    "If it is a question, a chat, a brightness/temperature/colour change, several devices or a whole room, "
    "or a device that is not in the list, answer action 'other' and device 'none'."
)
DONE = {"turn_on": "Turned on {}.", "turn_off": "Turned off {}.", "open": "Opening {}.", "close": "Closing {}."}


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
        allowed = {d for m in ACTIONS.values() for d in m}
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
                "action": {"type": "enum", "choices": ["turn_on", "turn_off", "open", "close", "other"],
                           "description": "What does the user want done?"},
                "device": {"type": "enum", "choices": [*devices.keys(), "none"],
                           "description": "Which single device from the list is the user talking about?"},
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
            service = ACTIONS.get(action, {}).get(eid.split(".")[0]) if eid else None
            ok = (service is not None and p_action >= self._opt(CONF_ACTION_THRESHOLD, DEFAULT_ACTION_THRESHOLD)
                  and p_device >= self._opt(CONF_DEVICE_THRESHOLD, DEFAULT_DEVICE_THRESHOLD))
            _LOGGER.info("'%s' -> %s %.2f / %s (%s) %.2f -> %s in %.0f ms", user_input.text, action, p_action,
                         device, eid, p_device, f"{service}" if ok else "fallback", (time.monotonic() - start) * 1000)
            if ok:
                domain = eid.split(".")[0]
                await self.hass.services.async_call(domain, service, {"entity_id": eid}, blocking=True,
                                                    context=user_input.context)
                response = intent.IntentResponse(language=user_input.language)
                response.async_set_speech(DONE[action].format(device))
                return conversation.ConversationResult(response=response, conversation_id=user_input.conversation_id)
        # not a confident single-device command: let the fallback agent handle it as before
        return await conversation.async_converse(
            self.hass, user_input.text, user_input.conversation_id, user_input.context,
            language=user_input.language, agent_id=self._opt(CONF_FALLBACK_AGENT, DEFAULT_FALLBACK_AGENT),
            device_id=user_input.device_id, satellite_id=user_input.satellite_id,
            extra_system_prompt=user_input.extra_system_prompt,
        )
