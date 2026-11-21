"""Config flow for Music Assistant Bridge: one entry, nothing to fill in."""

from __future__ import annotations

import secrets
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DATA_SECRET, DOMAIN


class MaBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the bridge."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user")
        # Signs image proxy links; kept in the entry so links (and browser caches) survive restarts.
        return self.async_create_entry(title="Music Assistant Bridge", data={DATA_SECRET: secrets.token_hex(32)})
