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
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA
from homeassistant.core import callback

from .ac_infinity_ble.const import MANUFACTURER_ID
from .ac_infinity_ble.protocol import parse_manufacturer_data as _parse_vendored
from .const import (
    BLEAK_EXCEPTIONS,
    CONF_HOLD_CONNECTION,
    DEFAULT_HOLD_CONNECTION,
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


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Per-fan tunables.

    ``self.config_entry`` is supplied by Home Assistant; assigning it here
    is removed API.  Changing an option triggers the entry update listener
    in __init__.py, which reloads the entry — that is what starts or stops
    the hold supervisor.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

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
                }
            ),
        )
