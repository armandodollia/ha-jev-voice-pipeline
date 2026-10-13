"""Config flow for Home LLM Assist (one entry, settings editable under Configure)."""

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACTION_THRESHOLD,
    CONF_DECISION_URL,
    CONF_DEVICE_THRESHOLD,
    CONF_FALLBACK_AGENT,
    DEFAULT_ACTION_THRESHOLD,
    DEFAULT_DECISION_URL,
    DEFAULT_DEVICE_THRESHOLD,
    DEFAULT_FALLBACK_AGENT,
    DOMAIN,
)


def _schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_DECISION_URL, default=d.get(CONF_DECISION_URL, DEFAULT_DECISION_URL)): str,
        vol.Required(CONF_FALLBACK_AGENT, default=d.get(CONF_FALLBACK_AGENT, DEFAULT_FALLBACK_AGENT)):
            selector.EntitySelector(selector.EntitySelectorConfig(domain="conversation")),
        vol.Required(CONF_ACTION_THRESHOLD, default=d.get(CONF_ACTION_THRESHOLD, DEFAULT_ACTION_THRESHOLD)):
            vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
        vol.Required(CONF_DEVICE_THRESHOLD, default=d.get(CONF_DEVICE_THRESHOLD, DEFAULT_DEVICE_THRESHOLD)):
            vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
    })


class HomeLlmAssistConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Home LLM Assist", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HomeLlmAssistOptionsFlow()


class HomeLlmAssistOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_schema({**self.config_entry.data, **self.config_entry.options}))
