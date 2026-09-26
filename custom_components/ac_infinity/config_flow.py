"""Config and options flow for the AC Infinity AIRTAP BLE integration.

Entry schema is FROZEN at VERSION 1 (CONF_ADDRESS + CONF_SERVICE_DATA):
live entries must load unchanged across upgrades.  Tunables therefore live
in ``entry.options``, never in ``entry.data``.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .ac_infinity_ble.const import MANUFACTURER_ID
from .ac_infinity_ble.protocol import parse_manufacturer_data as _parse_vendored
from .const import (
    BLEAK_EXCEPTIONS,
    CONF_HOLD_CONNECTION,
    CONF_PREFERRED_PROXY,
    CONF_THERMOSTAT,
    DEFAULT_HOLD_CONNECTION,
    DEFAULT_PREFERRED_PROXY,
    DOMAIN,
)
from .device import ACInfinityDevice, DeviceInfoEx

_LOGGER = logging.getLogger(__name__)


def parse_manufacturer_data(data: bytes) -> DeviceInfoEx:
    """Parse an AC Infinity manufacturer-data record into DeviceInfoEx."""
    return DeviceInfoEx.create(_parse_vendored(data))


def _try_parse_service_info(
    service_info: BluetoothServiceInfoBleak,
) -> DeviceInfoEx | None:
    """Return parsed device info, or None for foreign/malformed advertisers.

    The manufacturer-ID guard is the device-identity check for this
    integration; anything without record 2306, or whose record does not parse
    (truncated frames occur at the fringe of proxy range), must never be
    offered for setup.
    """
    if MANUFACTURER_ID not in service_info.advertisement.manufacturer_data:
        return None
    try:
        return parse_manufacturer_data(
            service_info.advertisement.manufacturer_data[MANUFACTURER_ID]
        )
    except (IndexError, ValueError, UnicodeDecodeError):
        _LOGGER.debug(
            "Ignoring unparseable AC Infinity manufacturer data from %s",
            service_info.address,
        )
        return None


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AC Infinity."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        """Return the options flow for an existing entry."""
        return OptionsFlowHandler()

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle the bluetooth discovery step."""
        # raise_on_progress defaults True here: a second discovery for the
        # same address aborts itself with "already_in_progress" instead of
        # stacking duplicate flows.
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if MANUFACTURER_ID not in discovery_info.advertisement.manufacturer_data:
            return self.async_abort(reason="no_devices_found")
        if (device := _try_parse_service_info(discovery_info)) is None:
            return self.async_abort(reason="no_devices_found")
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": device.name}
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            controller = ACInfinityDevice(
                discovery_info.device, advertisement_data=discovery_info.advertisement
            )
            try:
                await controller.update()
            except BLEAK_EXCEPTIONS:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected error")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=controller.name,
                    data={
                        CONF_ADDRESS: discovery_info.address,
                        CONF_SERVICE_DATA: parse_manufacturer_data(
                            discovery_info.advertisement.manufacturer_data[
                                MANUFACTURER_ID
                            ]
                        ),
                    },
                )
            finally:
                # Release the GATT connection on every path (including the
                # error branches, which previously leaked it): connection
                # slots on the ESPHome proxies are scarce.
                await controller.stop()

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            in_progress_addresses = {
                flow["context"].get("unique_id")
                for flow in self._async_in_progress()
            }
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.address in in_progress_addresses
                    or discovery.address in self._discovered_devices
                    # Collect ONLY AC Infinity advertisers so the picker can
                    # never offer (or later connect to) a foreign device.
                    or _try_parse_service_info(discovery) is None
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        _LOGGER.debug("Discovered devices: %s", self._discovered_devices)

        devices = {}
        for service_info in self._discovered_devices.values():
            if (device := _try_parse_service_info(service_info)) is None:
                continue
            devices[service_info.address] = f"{device.name} ({service_info.address})"

        if not devices:
            return self.async_abort(reason="no_devices_found")

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(devices),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )


def _preferred_proxy_choices(
    hass: HomeAssistant, current: str
) -> list[selector.SelectOptionDict]:
    """Automatic, then the node names of connectable scanners, plus ``current``.

    ``current`` (the entry's already-configured value) is included even
    when no live scanner reports it, so a proxy that is temporarily offline
    is never silently dropped from a choice the operator already made.

    Automatic's value is the empty string, which Home Assistant cannot
    translate (translation keys must be non-empty), so its label is carried
    here rather than in strings.json.
    """
    names = {
        scanner.adapter
        for scanner in bluetooth.async_current_scanners(hass)
        if scanner.connectable and getattr(scanner, "adapter", None)
    }
    if current:
        names.add(current)
    return [
        selector.SelectOptionDict(
            value=DEFAULT_PREFERRED_PROXY, label="Automatic (strongest signal)"
        ),
        *(selector.SelectOptionDict(value=name, label=name) for name in sorted(names)),
    ]


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Per-fan tunables.

    ``self.config_entry`` is supplied by Home Assistant; assigning it here
    is removed API.  Changing the hold, the preferred proxy or the
    thermostat triggers the entry update listener in __init__.py, which
    reloads the entry: that is what starts or stops the hold supervisor and
    re-subscribes the circulation controller and air sensors.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            # MERGE, never replace: options also hold bookkeeping the
            # integration writes itself (the last holding proxy, the outlet
            # the repair wizard learned) and the speeds the number entities
            # store, and this form does not offer those. Replacing would wipe
            # them on every hold toggle.
            options = {**self.config_entry.options, **user_input}
            if CONF_THERMOSTAT not in user_input:
                # An emptied optional field is simply absent from the input.
                options.pop(CONF_THERMOSTAT, None)
            return self.async_create_entry(data=options)

        preferred_proxy = self.config_entry.options.get(
            CONF_PREFERRED_PROXY, DEFAULT_PREFERRED_PROXY
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOLD_CONNECTION,
                        default=self.config_entry.options.get(
                            CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION
                        ),
                    ): bool,
                    vol.Required(
                        CONF_PREFERRED_PROXY, default=preferred_proxy
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_preferred_proxy_choices(
                                self.hass, preferred_proxy
                            ),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            custom_value=True,
                        )
                    ),
                    vol.Optional(
                        CONF_THERMOSTAT,
                        description={
                            "suggested_value": self.config_entry.options.get(
                                CONF_THERMOSTAT
                            )
                        },
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="climate")
                    ),
                }
            ),
        )
