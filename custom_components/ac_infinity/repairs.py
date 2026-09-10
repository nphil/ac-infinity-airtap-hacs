"""Escalating recovery wizard behind the device_unreachable repair's Fix button.

The household already heals a dead BLE link automatically
(``script.ble_heal_device`` plus an hourly re-home).  This wizard is the
fallback for when that machinery has failed: it offers the same escalation an
operator would perform by hand, cheapest rung first, and after every rung it
waits and re-reads the integration's own link state instead of claiming
success.

Ladder, in menu order:

* ``recheck``       — nothing at all, just look again (the heal script may
                      have landed while the repair was still on screen);
* ``reload``        — reload the config entry, which rebuilds the hold
                      supervisor and asks bleak-retry-connector to re-score
                      every proxy that can see the fan;
* ``restart_proxy`` — reboot the ESPHome proxy that last carried the link,
                      which clears a ghost link its Bluedroid stack is
                      holding.  Offered only when a proxy is known AND that
                      proxy actually exposes the action;
* ``power_cycle``   — cut mains to the fan through a switch entity.  Last
                      resort, and the only rung that touches the fan itself.

Nothing here blocks the event loop: every wait is ``asyncio.sleep`` in small
increments, re-reading the link state between them.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ADDRESS,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig
from homeassistant.util import slugify

from .const import CONF_LAST_HOLDING_PROXY, CONF_RECOVERY_OUTLET, DOMAIN
from .coordinator import async_holding_proxy_node, unreachable_issue_id
from .hold import STATE_CONNECTED, STATE_DISCONNECTED
from .models import ACInfinityData

_LOGGER = logging.getLogger(__name__)

ESPHOME_DOMAIN = "esphome"
SWITCH_DOMAIN = "switch"

# Suffix ESPHome gives the user-defined restart action on each proxy node:
# `esphome.<node name slug>_restart_proxy`.
RESTART_PROXY_ACTION = "_restart_proxy"

# How long to wait for the link to come back after a rung. A reload's first
# connect goes through the full bleak-retry-connector ladder, which can take
# tens of seconds against an RSSI -91 fan.
RECOVERY_TIMEOUT = 45

# A power-cycled fan has to boot before it advertises again, so it gets the
# longer window, on top of the time its mains is off.
POWER_CYCLE_OFF_SECONDS = 10
POWER_CYCLE_TIMEOUT = 60

# Granularity of the wait loop. Small enough that a fan that comes straight
# back closes the repair promptly, large enough not to spin.
POLL_INTERVAL = 1.0


class BleRecoveryFixFlow(RepairsFlow):
    """Walk the operator up the recovery ladder for one fan."""

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id
        # Shown in the menu description. Empty on first entry — never the
        # string "None", which is what a bare placeholder default renders as.
        self._last_result = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_menu()

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Offer the ladder, cheapest rung first."""
        entry = self._entry
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        menu_options = ["recheck", "reload"]
        # Only offered when the action really exists: pointing the operator at
        # a proxy restart that would fail is worse than not offering it.
        if self._proxy_action is not None:
            menu_options.append("restart_proxy")
        menu_options.append("power_cycle")

        return self.async_show_menu(
            step_id="menu",
            menu_options=menu_options,
            description_placeholders={
                "name": entry.title,
                "link": STATE_CONNECTED if self._healthy else STATE_DISCONNECTED,
                "last_result": self._last_result,
            },
        )

    async def async_step_recheck(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Change nothing; give the link a window to come back on its own."""
        return await self._async_settle(
            RECOVERY_TIMEOUT, "Waited without changing anything"
        )

    async def async_step_reload(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Reload the config entry, rebuilding the hold from scratch."""
        if self._entry_id is None:
            return self.async_abort(reason="entry_not_loaded")
        await self.hass.config_entries.async_reload(self._entry_id)
        return await self._async_settle(RECOVERY_TIMEOUT, "Reloaded the integration")

    async def async_step_restart_proxy(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Ask the proxy that last carried this fan to reboot.

        Honest reporting matters here: the proxy firmware refuses a restart
        while its own uptime is under 20 minutes (it logs ``refused: up only
        N s``), so a successful action call does NOT mean it rebooted.
        """
        discovered = self._proxy_action
        if discovered is None:
            self._last_result = (
                "No Bluetooth proxy restart action is available for this fan"
            )
            return await self.async_step_menu()
        proxy, action = discovered
        try:
            await self.hass.services.async_call(
                ESPHOME_DOMAIN, action, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not ask %s to restart: %s", proxy, err)
            self._last_result = f"Asking {proxy} to restart failed: {err}"
            return await self.async_step_menu()
        return await self._async_settle(
            RECOVERY_TIMEOUT,
            f"Asked {proxy} to restart (it refuses if it booted recently, "
            "in which case nothing happened)",
        )

    async def async_step_power_cycle(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Cut and restore mains power to the fan through a switch entity."""
        entry = self._entry
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is None:
            stored = entry.options.get(CONF_RECOVERY_OUTLET)
            # No default when nothing is stored: the fans are not on smart
            # plugs today, so pre-filling anything would be a guess.
            key = (
                vol.Required(CONF_RECOVERY_OUTLET, default=stored)
                if stored
                else vol.Required(CONF_RECOVERY_OUTLET)
            )
            return self.async_show_form(
                step_id="power_cycle",
                data_schema=vol.Schema(
                    {key: EntitySelector(EntitySelectorConfig(domain=SWITCH_DOMAIN))}
                ),
                description_placeholders={
                    "name": entry.title,
                    "seconds": str(POWER_CYCLE_OFF_SECONDS),
                },
            )

        outlet: str = user_input[CONF_RECOVERY_OUTLET]
        # Remembered so the last resort is one click next time. Bookkeeping
        # only: the entry update listener recognises it and does not reload.
        if entry.options.get(CONF_RECOVERY_OUTLET) != outlet:
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_RECOVERY_OUTLET: outlet}
            )

        try:
            await self.hass.services.async_call(
                SWITCH_DOMAIN,
                SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: outlet},
                blocking=True,
            )
        except HomeAssistantError as err:
            # Nothing was cut, so this is the safe failure.
            _LOGGER.warning("Could not switch %s off: %s", outlet, err)
            self._last_result = f"Switching {outlet} off failed: {err}"
            return await self.async_step_menu()

        # Restoring power is structural, not a happy path: the operator can
        # close the repair dialog while we are waiting (which cancels this
        # flow task), and this is the one rung that would then leave the fan
        # with no mains indefinitely. The turn-on is therefore issued from a
        # finally, and shielded so it still completes if the await it is
        # sitting in gets cancelled.
        restore_error: str | None = None
        try:
            await asyncio.sleep(POWER_CYCLE_OFF_SECONDS)
        finally:
            restore = asyncio.shield(
                self.hass.services.async_call(
                    SWITCH_DOMAIN,
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: outlet},
                    blocking=True,
                )
            )
            try:
                await restore
            except HomeAssistantError as err:
                restore_error = str(err)

        if restore_error is not None:
            # Say so loudly: the fan is sitting there with no mains.
            _LOGGER.error(
                "Switched %s off but could not switch it back on (%s); "
                "the fan has no power",
                outlet,
                restore_error,
            )
            self._last_result = (
                f"Switched {outlet} off but could not switch it back on "
                f"({restore_error}). The fan currently has NO power — turn "
                "that switch back on yourself."
            )
            return await self.async_step_menu()

        return await self._async_settle(
            POWER_CYCLE_TIMEOUT,
            f"Power cycled the fan through {outlet} (power restored)",
        )

    @property
    def _entry(self) -> ConfigEntry | None:
        if self._entry_id is None:
            return None
        return self.hass.config_entries.async_get_entry(self._entry_id)

    @property
    def _runtime_data(self) -> ACInfinityData | None:
        """This entry's runtime data, looked up fresh every time.

        A reload replaces the whole object, so nothing here may be cached
        across a rung.
        """
        if self._entry_id is None:
            return None
        return self.hass.data.get(DOMAIN, {}).get(self._entry_id)

    @property
    def _healthy(self) -> bool:
        """The integration's own verdict on the link — no second opinion."""
        data = self._runtime_data
        return data is not None and data.coordinator.link_healthy

    @property
    def _proxy_action(self) -> tuple[str, str] | None:
        """(proxy node, esphome action) that can restart this fan's proxy.

        The current holder if there is one, otherwise the one written down
        while the link was last up — an unreachable fan is held by nobody, so
        without that record there is nothing to restart.  Both are the bare
        ESPHome node name (``async_holding_proxy_node``), never the
        ``"<node> (<MAC>)"`` display name the Connection sensor shows: the
        action is registered off the node name, and a wrongly derived one
        resolves to nothing and silently drops this rung.
        """
        entry = self._entry
        if entry is None:
            return None
        proxy = async_holding_proxy_node(
            self.hass, entry.data[CONF_ADDRESS].upper()
        ) or entry.options.get(CONF_LAST_HOLDING_PROXY)
        if not proxy:
            return None
        # Records written before the node-name cutover hold the display name
        # — and an unreachable fan cannot rewrite its record until the link
        # is back, which is exactly when this is asked.
        proxy = proxy.split(" (")[0]
        # `plant-room-bluetooth-proxy` -> `plant_room_bluetooth_proxy_restart_proxy`
        action = f"{slugify(proxy)}{RESTART_PROXY_ACTION}"
        # has_service, not async_services(): core's own docstring warns that
        # async_services() deep-copies the whole registry, and the menu
        # re-derives this on every render.
        if not self.hass.services.has_service(ESPHOME_DOMAIN, action):
            return None
        return proxy, action

    async def _async_settle(
        self, timeout: float, attempted: str
    ) -> data_entry_flow.FlowResult:
        """Wait for the link, then either finish or go back to the menu."""
        if await self._async_wait_for_link(timeout):
            # Belt and braces: finishing the flow makes Home Assistant drop
            # the issue, and the entry's own watchdog deletes it too, so the
            # repair cannot outlive the fault whichever side notices first.
            if (data := self._runtime_data) is not None:
                data.watchdog.async_link_changed()
            return self.async_create_entry(data={})
        self._last_result = f"{attempted}; the fan is still unreachable."
        return await self.async_step_menu()

    async def _async_wait_for_link(self, timeout: float) -> bool:
        """Poll the health predicate for up to ``timeout`` seconds."""
        waited = 0.0
        while True:
            if self._healthy:
                return True
            if waited >= timeout:
                return False
            await asyncio.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Map a repair issue back to the config entry that raised it.

    Matching on the issue id rather than trusting the issue's stored data
    keeps one source of truth: the id is derived from the entry's address,
    so a re-added fan's issue still resolves to its new entry.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if unreachable_issue_id(entry.data[CONF_ADDRESS]) == issue_id:
            return BleRecoveryFixFlow(entry.entry_id)
    return BleRecoveryFixFlow(None)
