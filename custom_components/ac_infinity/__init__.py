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

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .ac_infinity_ble import DeviceInfo
from .const import CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION, DOMAIN
from .coordinator import (
    DEVICE_STARTUP_TIMEOUT,
    ACInfinityDataUpdateCoordinator,
    ACInfinityLinkWatchdog,
    async_clear_outage,
    async_holding_scanner_name,
    async_link_down,
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
    _async_register_services(hass)
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address.upper(), True)
    if not ble_device:
        # The most total outage there is, and the one the watchdog below can
        # never see: it is constructed after this raise, and Home Assistant
        # retries setup on a backoff for as long as the fan stays missing.
        # Live on 2026-09-09 the living-room vent fan sat in setup_retry from
        # 22:23:52 with exactly this reason, its Connection sensor
        # unavailable and no repair.  So the outage clock is recorded and
        # the deadline armed here, before the raise; async_link_down is
        # idempotent per address, so the retries cannot push the deadline
        # out, and its timer is scheduled on hass rather than through
        # entry.async_on_unload, which a failed setup never gets to keep.
        async_link_down(hass, entry)
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
            # Same outage, one step later: found once, silent since.
            async_link_down(hass, entry)
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
    """Retire this fan's repair issue and outage clock when its entry is deleted.

    Unload deliberately leaves both alone (a reload must not clear a genuine
    fault, and the clock is what makes the 15-minute threshold reachable
    across reloads), but a removed entry means the fan is gone: nothing
    would ever reconcile the issue again, its Fix button could only abort,
    and a deadline left pending would raise it once more.
    """
    address: str = entry.data[CONF_ADDRESS]
    async_clear_outage(hass, address)
    ir.async_delete_issue(hass, DOMAIN, unreachable_issue_id(address))


# ---------------------------------------------------------------------------
# release_link - a clean teardown before a Home Assistant restart
# ---------------------------------------------------------------------------
#
# Home Assistant does NOT unload config entries on shutdown: it fires
# EVENT_HOMEASSISTANT_STOP and the `bluetooth` integration tears its stack down
# concurrently with everything else. Measured on 2026-09-09 at 23:21 local, the
# shutdown log reads
#
#     D-YHN4F / D-4F668 / D-6LB2N / D-NN8P7: "Device unexpectedly disconnected"
#     bleak.exc.BleakError: Bluetooth is already shutdown
#
# so the GATT disconnects never complete. The ESP keeps the ACL, the fan keeps
# believing it is connected, and it stops advertising - a ghost link nothing on
# the Home Assistant side can see or clear (the proxy reports its slots free).
# The living-room fan wedged exactly four minutes after that restart and only a
# proxy reboot freed it.
#
# The ordering inside HA's shutdown cannot be fixed from outside, so the
# teardown has to happen BEFORE the restart is requested. This action does that
# for every loaded entry, and re-arms the hold afterwards so an operator who
# calls it and then does not restart - or a restart that fails - is not left
# with disconnected fans.
SERVICE_RELEASE_LINK = "release_link"
ATTR_RESUME_AFTER = "resume_after"
#: Seconds before the hold is rebuilt if no restart took the process away.
#: Long enough for `homeassistant.restart` to actually stop the process,
#: short enough that a mistaken call heals itself well inside the
#: 15-minute unreachable window.
DEFAULT_RESUME_AFTER = 180
RELEASE_LINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RESUME_AFTER, default=DEFAULT_RESUME_AFTER): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=900)
        )
    }
)


async def _async_release_links(hass: HomeAssistant, resume_after: int) -> None:
    """Drop every held GATT link cleanly, then re-arm the holds."""
    released: list[tuple[ConfigEntry, ACInfinityData]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data: ACInfinityData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if data is None:
            continue
        # Stop the supervisor first, exactly as async_unload_entry does: a
        # teardown underneath a live supervisor looks like a lost link and it
        # immediately rebuilds the connection we are releasing.
        with contextlib.suppress(Exception):
            await data.device.async_stop_hold()
        with contextlib.suppress(Exception):
            await data.device.stop()
        released.append((entry, data))
        _LOGGER.info("Released the BLE link held for %s", entry.title)

    if not released or resume_after <= 0:
        return

    async def _resume(_now: Any) -> None:
        """Rebuild the holds, for the restart that never came."""
        for entry, data in released:
            if entry.entry_id not in hass.data.get(DOMAIN, {}):
                continue  # unloaded or reloaded meanwhile; it owns itself now
            if not entry.options.get(CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION):
                continue
            address: str = entry.data[CONF_ADDRESS]
            with contextlib.suppress(Exception):
                data.device.async_start_hold(
                    lambda addr=address: async_holding_scanner_name(hass, addr.upper())
                )
        _LOGGER.info(
            "No restart followed release_link within %s s; holds re-armed", resume_after
        )

    async_call_later(hass, resume_after, _resume)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain action once, however many fans are configured."""
    if hass.services.has_service(DOMAIN, SERVICE_RELEASE_LINK):
        return

    async def _handle(call: ServiceCall) -> None:
        await _async_release_links(hass, call.data[ATTR_RESUME_AFTER])

    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_LINK, _handle, schema=RELEASE_LINK_SCHEMA
    )
