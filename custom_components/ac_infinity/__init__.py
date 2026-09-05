"""The AC Infinity AIRTAP BLE integration.

Config entry compatibility is FROZEN: entries store CONF_ADDRESS plus a
CONF_SERVICE_DATA snapshot of the device state, and live installs depend on
both surviving upgrades untouched. Anything that changes the stored shape
must keep _device_info_from_entry_data able to read every historical shape.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .ac_infinity_ble import DeviceInfo
from .const import DOMAIN
from .coordinator import ACInfinityDataUpdateCoordinator
from .device import ACInfinityDevice, AutoModeConfig, DeviceInfoEx
from .models import ACInfinityData

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


def _device_info_from_entry_data(service_data: Any) -> DeviceInfoEx:
    """Normalize every historical CONF_SERVICE_DATA shape to DeviceInfoEx.

    Three shapes exist in the wild:
    - dict: any entry loaded from storage (dataclasses are JSON-serialized on
      save), including entries created by the upstream forks this repository
      descends from.
    - DeviceInfoEx: an entry created by this fork earlier in the same HA
      session (never persisted in object form).
    - DeviceInfo: an entry created by an upstream fork earlier in the same HA
      session.

    Dicts are filtered to the currently known field set so an entry written
    by a future/older version with extra keys can never brick setup, and a
    serialized auto_mode block is coerced back to its dataclass (it is stored
    as null at flow-creation time, but be tolerant of rewritten entries).
    """
    if isinstance(service_data, DeviceInfoEx):
        return service_data
    if isinstance(service_data, DeviceInfo):
        return DeviceInfoEx.create(service_data)
    if isinstance(service_data, Mapping):
        known_fields = {field.name for field in dataclasses.fields(DeviceInfoEx)}
        data = {k: v for k, v in service_data.items() if k in known_fields}
        auto_mode = data.get("auto_mode")
        if isinstance(auto_mode, Mapping):
            # TypeError means an unknown threshold shape: drop it, the first
            # successful GATT poll repopulates it.
            with contextlib.suppress(TypeError):
                data["auto_mode"] = AutoModeConfig(**auto_mode)
        if not isinstance(data.get("auto_mode"), (AutoModeConfig, type(None))):
            data["auto_mode"] = None
        return DeviceInfoEx(**data)
    raise ValueError(
        f"Unexpected config entry service data type: {type(service_data)}"
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AC Infinity from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find AC Infinity device with address {address}"
        )

    device_info = _device_info_from_entry_data(entry.data[CONF_SERVICE_DATA])
    device = ACInfinityDevice(ble_device, device_info)
    coordinator = ACInfinityDataUpdateCoordinator(hass, _LOGGER, ble_device, device)

    # Start listening BEFORE waiting: async_start registers the bluetooth
    # callback (which replays the current advertisement immediately when the
    # device is already known) and the unavailability tracker. Registered via
    # async_on_unload first so a ConfigEntryNotReady below still tears the
    # callbacks down before the retry.
    entry.async_on_unload(coordinator.async_start())

    if not await coordinator.async_wait_ready():
        raise ConfigEntryNotReady(
            f"{entry.title} ({address}) is not advertising state; "
            "check ESPHome proxy coverage"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ACInfinityData(
        entry.title, device, coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Previously missing entirely: entries could not be reloaded/removed
    cleanly, and hass.data leaked the coordinator on every attempt. Also
    releases any GATT connection the controller may still hold so the proxy
    slot returns to the pool immediately.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data: ACInfinityData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.device.stop()
    return unload_ok
