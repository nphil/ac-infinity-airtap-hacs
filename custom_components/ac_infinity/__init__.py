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
from homeassistant.helpers import issue_registry as ir

from .ac_infinity_ble import DeviceInfo
from .const import CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION, DOMAIN
from .coordinator import (
    DEVICE_STARTUP_TIMEOUT,
    ACInfinityDataUpdateCoordinator,
    ACInfinityLinkWatchdog,
    async_holding_scanner_name,
    unreachable_issue_id,
)
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

    # Entries created before the option existed carry no options dict, so
    # the default decides for them; holding is the point of this integration
    # now (a fresh proxy connect per command costs 1.8-6.4 s).
    hold = entry.options.get(CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION)

    if hold:
        # Before the coordinator starts, on purpose. async_start replays the
        # cached advertisement immediately, which triggers the first poll;
        # with the hold not yet switched on that poll ran poll-and-release
        # semantics - connect, subscribe, read, DISCONNECT - and the supervisor
        # reconnected 20 s later. Every teardown is a chance for the proxy's
        # Bluedroid stack to leave a ghost link (measured 2026-09-09), so the
        # first poll must ride on the link the hold keeps.
        device.async_start_hold(
            lambda: async_holding_scanner_name(hass, address.upper())
        )
        # Covers the paths async_unload_entry never sees: a platform forward
        # below raising leaves the supervisor running otherwise. Idempotent,
        # so the ordered call in async_unload_entry stays the normal route.
        entry.async_on_unload(device.async_stop_hold)

    # Start listening BEFORE waiting: async_start registers the bluetooth
    # callback (which replays the current advertisement immediately when the
    # device is already known) and the unavailability tracker. Registered via
    # async_on_unload first so a ConfigEntryNotReady below still tears the
    # callbacks down before the retry.
    entry.async_on_unload(coordinator.async_start())

    if not await coordinator.async_wait_ready():
        if not hold:
            raise ConfigEntryNotReady(
                f"{entry.title} ({address}) is not advertising state; "
                "check ESPHome proxy coverage"
            )
        # A held AIRTAP advertises rarely, and right after a restart a proxy
        # may still own the previous link, so a 30 s window regularly misses
        # the manufacturer-data frame (all six fans failed setup this way on
        # 2026-09-09). The connectable path above is proof enough: the hold
        # supervisor connects and the first GATT poll/notification fills the
        # state in; entities stay unavailable until then, which is honest.
        _LOGGER.info(
            "%s (%s): no parseable advertisement within %ss; holding the link "
            "and taking state from GATT instead",
            entry.title,
            address,
            DEVICE_STARTUP_TIMEOUT,
        )

    watchdog = ACInfinityLinkWatchdog(hass, entry, coordinator)
    # Registered before async_start so the 15-minute timer is cancelled even
    # if a platform forward below raises: an orphaned timer would fire
    # against a torn-down entry.
    entry.async_on_unload(watchdog.async_stop)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ACInfinityData(
        entry.title, device, coordinator, watchdog
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Started last: the first reconcile may write CONF_LAST_HOLDING_PROXY,
    # and the update listener below has to be able to find this entry's
    # runtime data to recognise that write as bookkeeping, not a hold toggle.
    watchdog.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when — and only when — the hold setting changed.

    ``entry.options`` also carries bookkeeping the integration writes itself
    (CONF_LAST_HOLDING_PROXY on every proxy change, CONF_RECOVERY_OUTLET when
    the repair wizard learns an outlet).  Reloading for those would tear the
    held link down for no reason, and a fan roaming between two proxies would
    reload-loop: each reload reconnects, each reconnect writes the new proxy
    name, which reloads again.  The live hold supervisor is the truth about
    which setting this entry was actually set up with.
    """
    data: ACInfinityData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    hold = entry.options.get(CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION)
    if data is not None and bool(hold) == data.device.hold_status.hold:
        return
    await hass.config_entries.async_reload(entry.entry_id)


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
        # Unsubscribe the watchdog FIRST. The teardown below turns the hold
        # off, which notifies hold-status listeners; the watchdog would then
        # reconcile with the hold already reported as off, fall back to
        # advertisement availability, judge a fan that is advertising but
        # whose GATT link is dead as healthy, and delete a perfectly valid
        # issue on every reload — the exact orphan/reload class of bug this
        # watchdog exists to prevent. It is also the only listener left that
        # could write to an entry whose runtime data is already gone.
        # (async_on_unload holds this same call for the failed-setup path;
        # async_stop is idempotent.)
        data.watchdog.async_stop()
        # Stop the supervisor BEFORE stop(): otherwise the forced teardown
        # below looks like a lost link and the hold immediately rebuilds the
        # connection we are trying to release.
        await data.device.async_stop_hold()
        await data.device.stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Retire this fan's repair issue when its entry is deleted.

    Unload deliberately leaves the issue alone (a reload must not clear a
    genuine fault), but a removed entry means the fan is gone: nothing would
    ever reconcile the issue again, and its Fix button could only abort.
    """
    ir.async_delete_issue(
        hass, DOMAIN, unreachable_issue_id(entry.data[CONF_ADDRESS])
    )
