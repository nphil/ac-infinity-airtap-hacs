"""The AC Infinity AIRTAP BLE integration.

Config entry compatibility is FROZEN: entries store CONF_ADDRESS plus a
CONF_SERVICE_DATA snapshot of the device state, and live installs depend on
both surviving upgrades untouched. Anything that changes the stored shape
must keep _device_info_from_entry_data able to read every historical shape.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import bleak_retry_connector
import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_SERVICE_DATA, Platform
from homeassistant.core import HassJob, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .ac_infinity_ble import DeviceInfo
from .ble_affinity import make_affinity_client_class
from .circulation import CirculationController, VentSettings
from .const import (
    CONF_HOLD_CONNECTION,
    CONF_PREFERRED_PROXY,
    DEFAULT_HOLD_CONNECTION,
    DOMAIN,
)
from .coordinator import (
    STARTUP_BUDGET,
    ACInfinityDataUpdateCoordinator,
    ACInfinityLinkWatchdog,
    async_clear_outage,
    async_holding_scanner_name,
    async_link_down,
    unreachable_issue_id,
)
from .device import ACInfinityDevice, AutoModeConfig, DeviceInfoEx
from .models import ACInfinityData
from .proxy_health import STALL_AVOID_SECONDS, StalledProxies

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

PLATFORMS: list[Platform] = [
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)

# No YAML configuration; required by hassfest because this module defines
# async_setup (the domain-lifetime shutdown latch).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


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


# Readings, as opposed to identity. CONF_SERVICE_DATA is the advertisement
# seen when the fan was paired, so these fields hold that moment's values.
_PAIRING_READINGS = (
    "tmp",
    "hum",
    "vpd",
    "fan",
    "fan_state",
    "tmp_state",
    "hum_state",
    "vpd_state",
)


def _runtime_state_from_entry_data(service_data: Any) -> DeviceInfoEx:
    """The state a fan starts with: its stored identity, none of its readings.

    Seeding the pairing snapshot's readings made a held fan report its
    pairing-day speed after every restart or reload, and nothing corrected
    it: a held fan sends no manufacturer data. Live on 2026-09-25 the Master
    Bedroom vent read 60 % from each restart until its next link drop, while
    its own broadcasts showed it idling at 0 overnight and ramping 6-9
    that evening. Unknown until the fan itself reports is the honest start;
    its first notification arrives about a second after the link comes up.
    """
    return dataclasses.replace(
        _device_info_from_entry_data(service_data),
        **dict.fromkeys(_PAIRING_READINGS),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AC Infinity from a config entry."""
    _async_register_services(hass)
    # Rule B: refuse while Home Assistant is shutting down. Stage-1 jobs were
    # listed before this call, so a device built now would never be released;
    # nothing may be created, started or recorded as an outage (hence before
    # async_link_down too).
    if shutting_down(hass):
        raise ConfigEntryNotReady("Home Assistant is shutting down")
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

    device_info = _runtime_state_from_entry_data(entry.data[CONF_SERVICE_DATA])

    # Proxies that stalled an attempt for this fan are left out of the routing
    # for a while (proxy_health.py), whichever the preferred proxy or
    # habluetooth's own scoring would have picked. The record lives in
    # hass.data so a reload (autoheal sweeps reload a down fan every five
    # minutes) does not forget it. ``selected_source`` is the scanner the most
    # recent connect was routed through; connects for one fan are serialised
    # by the controller's connect lock, so it is the one a stall belongs to.
    stalled_proxies = _stalled_proxies(hass, address)
    selected_source: str | None = None

    def _on_scanner_selected(scanner: Any) -> None:
        nonlocal selected_source
        selected_source = getattr(scanner, "source", None)

    def _on_link_stalled() -> None:
        if selected_source is None:
            return
        stalled_proxies.record(selected_source)
        _LOGGER.info(
            "%s (%s): a connection step through proxy %s stalled; routing "
            "around it for %d s while another route exists",
            entry.title,
            address,
            selected_source,
            STALL_AVOID_SECONDS,
        )

    def _on_link_ready() -> None:
        if selected_source is not None:
            stalled_proxies.clear(selected_source)

    def _on_proxy_choice(_scanner_name: str, preferred_used: bool) -> None:
        device.hold_status.set_via_preferred_proxy(preferred_used)

    # bleak_retry_connector.BleakClientWithServiceCache is monkeypatched by
    # HA's bluetooth integration into a HaBleakClientWrapper subclass; read
    # it off the module here (call time) instead of importing the name,
    # which could bind the pre-patch class. Built once per device object -
    # preferred_getter re-reads the live option on every connect, so an
    # options change takes effect on the next reconnect without rebuilding
    # this class.
    client_class = make_affinity_client_class(
        bleak_retry_connector.BleakClientWithServiceCache,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        on_choice=_on_proxy_choice,
        is_excluded=lambda scanner: stalled_proxies.is_excluded(
            getattr(scanner, "source", None)
        ),
        on_selected=_on_scanner_selected,
    )
    device = ACInfinityDevice(ble_device, device_info, client_class=client_class)
    device.set_link_handlers(on_stalled=_on_link_stalled, on_ready=_on_link_ready)
    device.hold_status.set_preferred_proxy(
        entry.options.get(CONF_PREFERRED_PROXY) or None
    )
    coordinator = ACInfinityDataUpdateCoordinator(hass, _LOGGER, ble_device, device)

    # Per-entry shutdown job. Home Assistant runs these in stage 1 of its stop
    # (concurrently, before EVENT_HOMEASSISTANT_STOP), while the bluetooth
    # stack and the proxy connections are still alive - the one moment a
    # disconnect can still complete (see "Releasing the GATT link" below).
    # Registered the moment the link-holding object exists, with no await
    # (and no started hold) before it: the job list is read once, so a job
    # added after stage 1 began would never run. The watchdog and circulation
    # controller it quiets are read when it runs, so it also works if
    # shutdown comes before they are built.
    watchdog: ACInfinityLinkWatchdog | None = None
    circulation: CirculationController | None = None

    async def _async_shutdown_release() -> None:
        await _async_release_at_shutdown(
            hass, entry.title, device, watchdog, circulation
        )

    entry.async_on_unload(
        hass.async_add_shutdown_job(
            HassJob(
                _async_shutdown_release,
                f"ac_infinity release BLE link {entry.title}",
            )
        )
    )

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

    ready = await coordinator.async_wait_ready(STARTUP_BUDGET)
    # Rule B, after the await: shutdown can land while waiting, and what
    # follows creates the watchdog (which could delete a real unreachable
    # repair) and circulation. The job registered above has already latched
    # the device and released the link, or will; this makes sure of both and
    # leaves before any watcher exists and before any outage is recorded.
    if shutting_down(hass) or device.closing:
        await _async_stop_setup_for_shutdown(hass, entry, device)
        raise ConfigEntryNotReady("Home Assistant is shutting down")
    if not ready:
        # Setup never fails on a miss, held or not: Home Assistant is not
        # "initialized" until every setup returns, and its own retry backoff
        # would only delay a fan the integration can recover by itself. The
        # connectable path above is proof enough that something can reach it.
        # A held fan keeps connecting in its supervisor and takes state from
        # GATT when it arrives; right after a restart a proxy may still own
        # the previous link, and a held AIRTAP advertises rarely, so the
        # window is regularly missed. An unheld fan is polled on its next
        # advertisement once Home Assistant is running. Either way the
        # entities stay unavailable until state arrives, and the watchdog
        # below owns the outage clock if it never does.
        _LOGGER.info(
            "%s (%s): no state within %ss of setup; the entities fill in "
            "when it arrives (%s)",
            entry.title,
            address,
            STARTUP_BUDGET,
            "holding the link, state from GATT" if hold else "state from the next advertisement",
        )

    watchdog = ACInfinityLinkWatchdog(hass, entry, coordinator)
    # Registered before async_start so the 15-minute timer is cancelled even
    # if a platform forward below raises: an orphaned timer would fire
    # against a torn-down entry.
    entry.async_on_unload(watchdog.async_stop)

    settings = VentSettings(hass, entry)
    circulation = CirculationController(
        hass, settings, device, coordinator.async_update_listeners
    )
    entry.async_on_unload(circulation.async_stop)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ACInfinityData(
        entry.title, device, coordinator, watchdog, settings, circulation
    )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Started last: the first reconcile may write CONF_LAST_HOLDING_PROXY,
    # and the update listener below has to be able to find this entry's
    # runtime data to recognise that write as bookkeeping, not a hold toggle.
    watchdog.async_start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Shutdown may have landed during the forward. The entry stays loaded
    # (nothing to unwind, and its entities are restore-state); what must not
    # happen is circulation starting on a fan that has just been released.
    if shutting_down(hass) or device.closing:
        await _async_stop_setup_for_shutdown(hass, entry, device)
        watchdog.async_stop()
        circulation.async_stop()
        return True
    circulation.async_start()

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry only when a setting it was set up with changed.

    ``entry.options`` also carries bookkeeping the integration writes itself
    (CONF_LAST_HOLDING_PROXY on every proxy change, CONF_RECOVERY_OUTLET when
    the repair wizard learns an outlet) and the speeds the number entities
    store (circulation.py).  Reloading for those would tear the held link
    down for no reason, and a fan roaming between two proxies would
    reload-loop: each reload reconnects, each reconnect writes the new proxy
    name, which reloads again.  The live device and circulation controller
    are the truth about what this entry was actually set up with; a speed
    change just asks the controller to re-check.
    """
    data: ACInfinityData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    hold = entry.options.get(CONF_HOLD_CONNECTION, DEFAULT_HOLD_CONNECTION)
    preferred_proxy = entry.options.get(CONF_PREFERRED_PROXY) or None
    if (
        data is not None
        and bool(hold) == data.device.hold_status.hold
        and preferred_proxy == data.device.hold_status.preferred_proxy
        and data.settings.thermostat == data.circulation.thermostat
    ):
        data.circulation.async_reconcile()
        data.coordinator.async_update_listeners()
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
        # Before the hold stops too: the teardown notifies hold listeners,
        # and a minimum write racing the disconnect would only fail.
        data.circulation.async_stop()
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
    hass.data.get(DOMAIN, {}).get(STALLED_PROXIES_KEY, {}).pop(address.upper(), None)
    ir.async_delete_issue(hass, DOMAIN, unreachable_issue_id(address))


# ---------------------------------------------------------------------------
# Releasing the GATT link: at shutdown, and on demand (release_link)
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
# The fix is the shutdown job each entry registers in async_setup_entry. Home
# Assistant runs those in "stage 1" of its stop, BEFORE it fires
# EVENT_HOMEASSISTANT_STOP, while the bluetooth stack and the proxies are still
# alive; every job runs concurrently under one shared 20 s budget, so each fan
# gets its own job and its own 8 s bound.
#
# `release_link` below predates that and does the same teardown on request,
# for every loaded entry, and re-arms the hold afterwards so an operator who
# calls it and then does not restart - or a restart that fails - is not left
# with disconnected fans. It stays as the manual way to free a stuck link.
SERVICE_RELEASE_LINK = "release_link"
ATTR_RESUME_AFTER = "resume_after"
#: Seconds before the hold is rebuilt if no restart took the process away.
#: Long enough for `homeassistant.restart` to actually stop the process,
#: short enough that a mistaken call heals itself well inside the
#: 15-minute unreachable window.
DEFAULT_RESUME_AFTER = 180
#: Seconds one fan gets to let go of its link, at shutdown or in
#: release_link. Well inside Home Assistant's shared 20 s stage-1 budget.
RELEASE_TIMEOUT = 8

RELEASE_LINK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_RESUME_AFTER, default=DEFAULT_RESUME_AFTER): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=900)
        )
    }
)


# hass.data[DOMAIN] keys that are not entry ids. Both outlive every entry
# unload and reload; only a Home Assistant restart clears them.
SHUTDOWN_LATCH_KEY = "shutting_down"
RESUME_TIMERS_KEY = "release_link_resume_timers"
#: address -> StalledProxies; see proxy_health.py. Outlives entry reloads.
STALLED_PROXIES_KEY = "_stalled_proxies"


def _stalled_proxies(hass: HomeAssistant, address: str) -> StalledProxies:
    store: dict[str, StalledProxies] = hass.data.setdefault(DOMAIN, {}).setdefault(
        STALLED_PROXIES_KEY, {}
    )
    return store.setdefault(address.upper(), StalledProxies())


def shutting_down(hass: HomeAssistant) -> bool:
    """Whether Home Assistant has started its shutdown jobs.

    ``hass.state`` is still ``running`` while they execute and ``is_stopping``
    is still false, so neither can say; this process-lifetime latch can.
    """
    return bool(hass.data.get(DOMAIN, {}).get(SHUTDOWN_LATCH_KEY))


@callback
def _async_latch_shutdown(hass: HomeAssistant) -> None:
    """Latch the whole domain shut and cancel every pending resume timer.

    Home Assistant lists its shutdown jobs once, when stage 1 starts. An entry
    set up (or reloaded) after that has a fresh device whose job is never in
    the list, so a per-entry latch alone cannot keep it from opening a link:
    this flag, set by the domain-lifetime job below and by every entry job,
    is what makes ``async_setup_entry`` refuse. Idempotent.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[SHUTDOWN_LATCH_KEY] = True
    timers: list[Callable[[], None]] = domain_data.pop(RESUME_TIMERS_KEY, [])
    for cancel in timers:
        with contextlib.suppress(Exception):
            cancel()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain-lifetime shutdown latch, once per Home Assistant run.

    Never removed when an entry unloads: it has to exist for the whole run so
    that a reload or setup retry during stage 1 still finds the latch set.
    A callback job, so it runs the moment stage 1 starts rather than on a
    task the loop may schedule after an entry's setup.
    """

    @callback
    def _latch() -> None:
        _async_latch_shutdown(hass)

    hass.async_add_shutdown_job(HassJob(_latch, "ac_infinity shutdown latch"))
    return True


async def _async_drop_link(name: str, device: ACInfinityDevice) -> bool:
    """Stop the hold, then force the disconnect; True when both went cleanly.

    The supervisor stops FIRST, exactly as async_unload_entry does: a teardown
    underneath a live supervisor looks like a lost link and it immediately
    rebuilds the connection we are releasing. A failure of the first step must
    not skip the second, so each is caught on its own. Never raises (except
    for cancellation).
    """
    clean = True
    for step in (device.async_stop_hold, device.stop):
        try:
            await step()
        except Exception as err:  # noqa: BLE001 - release must always go on
            clean = False
            _LOGGER.warning(
                "%s: error while releasing the BLE link (%s): %s",
                name,
                step.__name__,
                err,
            )
    return clean


async def _async_release_link(name: str, device: ACInfinityDevice) -> float | None:
    """Release one fan's link within RELEASE_TIMEOUT.

    Returns the seconds it took, or None when it timed out or hit an error
    (already logged as a warning). Never raises, so a caller can gather any
    number of these and one slow fan cannot hold up or fail the others.
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(RELEASE_TIMEOUT):
            clean = await _async_drop_link(name, device)
    except TimeoutError:
        _LOGGER.warning(
            "Releasing the BLE link to %s did not finish within %s s; giving up",
            name,
            RELEASE_TIMEOUT,
        )
        return None
    return time.monotonic() - started if clean else None


async def _async_release_links(hass: HomeAssistant, resume_after: int) -> None:
    """Drop every held GATT link cleanly, then re-arm the holds.

    Entries are released concurrently, each under its own RELEASE_TIMEOUT:
    done one after another, a single slow fan once held four others for ~20 s
    (live, 2026-10-02).
    """
    targets: list[tuple[ConfigEntry, ACInfinityData]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        data: ACInfinityData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if data is not None:
            targets.append((entry, data))

    async def _release(entry: ConfigEntry, data: ACInfinityData) -> None:
        elapsed = await _async_release_link(entry.title, data.device)
        if elapsed is not None:
            _LOGGER.info(
                "Released the BLE link held for %s in %.2f s", entry.title, elapsed
            )

    await asyncio.gather(*(_release(entry, data) for entry, data in targets))

    if not targets or resume_after <= 0 or shutting_down(hass):
        return

    cancel_resume: Callable[[], None] | None = None

    async def _resume(_now: Any) -> None:
        """Rebuild the holds, for the restart that never came."""
        timers = hass.data.get(DOMAIN, {}).get(RESUME_TIMERS_KEY)
        if timers is not None and cancel_resume in timers:
            timers.remove(cancel_resume)
        if shutting_down(hass):
            return  # Home Assistant is stopping; a link opened now is a ghost
        for entry, data in targets:
            # Identity, not membership. A RELOAD inside the window puts the
            # entry id straight back into hass.data with a NEW data/device, so
            # "is it still there" passes - and re-arming the captured OLD
            # device then starts a supervisor that no unload will ever cancel
            # (its entry now points at the new device). Two supervisors then
            # fight for a fan that accepts one connection: the loser retries
            # forever ("never seen by any scanner", ~900 attempts observed
            # 2026-09-22..24 on two vents that were connected throughout), and
            # the winner can flip whenever the fan re-advertises.
            if hass.data.get(DOMAIN, {}).get(entry.entry_id) is not data:
                continue  # unloaded or reloaded meanwhile; it owns itself now
            if data.device.closing:
                continue  # Home Assistant is shutting down; never reconnect
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

    cancel_resume = async_call_later(hass, resume_after, _resume)
    # Kept where the domain shutdown job can reach it: a timer that fires
    # while Home Assistant is stopping must find the latch, and one still
    # pending is cancelled outright.
    hass.data.setdefault(DOMAIN, {}).setdefault(RESUME_TIMERS_KEY, []).append(
        cancel_resume
    )


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


async def _async_stop_setup_for_shutdown(
    hass: HomeAssistant, entry: ConfigEntry, device: ACInfinityDevice
) -> None:
    """Make sure a setup that shutdown overtook leaves no link behind.

    The entry's own shutdown job normally did this already (it was registered
    before setup's first await); doing it again is idempotent and covers a job
    that has not run yet. Never raises.
    """
    _async_latch_shutdown(hass)
    device.begin_closing()
    await _async_release_link(entry.title, device)



async def _async_release_at_shutdown(
    hass: HomeAssistant,
    name: str,
    device: ACInfinityDevice,
    watchdog: ACInfinityLinkWatchdog | None,
    circulation: CirculationController | None,
) -> None:
    """Let go of one fan's link while Home Assistant is shutting down.

    Run as a Home Assistant shutdown job (stage 1, before the bluetooth stack
    and the proxy connections close). The entry is NOT unloaded and no entity
    is removed: restore-state stays intact and no wave of ``unavailable``
    states is written. Never raises.
    """
    try:
        # Latch FIRST: from here nothing in this process may open a link to
        # this fan again (hold supervisor, polls, commands, resume timer), and
        # no entry may be set up again (see _async_latch_shutdown).
        _async_latch_shutdown(hass)
        device.begin_closing()
        # Then quiet the watchers in async_unload_entry's order, so the
        # deliberate disconnect is not recorded as an outage and no repair
        # issue is created or deleted. A watcher that raises must not skip
        # the release below.
        if watchdog is not None:
            with contextlib.suppress(Exception):
                watchdog.async_stop()
        if circulation is not None:
            with contextlib.suppress(Exception):
                circulation.async_stop()
        elapsed = await _async_release_link(name, device)
        if elapsed is not None:
            _LOGGER.info(
                "Released BLE link to %s at shutdown in %.2f s", name, elapsed
            )
    except Exception as err:  # noqa: BLE001 - a shutdown job must never raise
        _LOGGER.warning("Could not release the BLE link to %s at shutdown: %s", name, err)
