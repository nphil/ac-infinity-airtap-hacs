"""Data update coordination for AC Infinity AIRTAP BLE devices.

Design notes (read before touching the event flow):

This integration is ``local_push``: state freshness comes from three sources,
in order of frequency:

1. BLE advertisements (temperature/humidity/fan speed), dispatched to
   ``_async_handle_bluetooth_event`` by Home Assistant's bluetooth manager.
2. GATT notifications and command commits pushed by the controller while a
   connection is open (forwarded via ``register_callback``).
3. Periodic GATT polls for state that advertisements cannot carry
   (work_type, level_on/level_off, auto-mode thresholds). Polls are
   *advertisement-driven*: ``ActiveBluetoothDataUpdateCoordinator`` only
   evaluates ``needs_poll`` while frames are flowing.

Availability is advertisement-based, not poll-based: the base coordinator
registers ``bluetooth.async_track_unavailable``, which flips ``available`` to
False (and notifies entity listeners) once no scanner has seen the address
for the tracked interval, and any subsequently dispatched frame flips it back.
Poll failures are logged by core but deliberately do not mark entities
unavailable — with six fans sharing a handful of ESPHome proxy connection
slots at RSSI as low as -91, transient GATT failures are routine and coupling
them to availability would cause constant flapping while advertisement data
is still perfectly good.  A live GATT link overrides all of that: a held
device advertises far less often (observed live), so the advertisement
tracker can call it unavailable while we are actively talking to it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING

from bleak.backends.device import BLEDevice
from habluetooth import HaBluetoothSlotAllocations, get_manager
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .ac_infinity_ble.const import MANUFACTURER_ID, CallbackType
from .ac_infinity_ble.exceptions import CharacteristicMissingError
from .ac_infinity_ble.models import DeviceInfo
from .const import CONF_LAST_HOLDING_PROXY, DOMAIN
from .device import ACInfinityDevice
from .hold import allocation_source_for_address

if TYPE_CHECKING:
    from .models import ACInfinityData

DEVICE_STARTUP_TIMEOUT = 30

# Upper bound for one GATT poll (connect + subscribe + command + response).
# bleak-retry-connector has its own per-attempt timeouts, but the worst-case
# retry ladder against an RSSI -91 device can exceed a minute; converting that
# into a clean poll failure keeps the shared poll slot below from being held
# hostage. A failed poll is retried on a later advertisement.
POLL_TIMEOUT = 45

# Integration-wide cap on concurrent GATT polls. The six fans reach HA only
# through ESPHome Bluetooth proxies with ~3 connection slots each; letting all
# coordinators poll simultaneously (e.g. right after startup, when every
# device becomes due at once) can exhaust every slot and starve user-initiated
# commands, which do NOT pass through this gate and therefore always find
# headroom. Module-level on purpose: the cap must span all config entries.
_POLL_SLOTS = 2
_POLL_SEMAPHORE = asyncio.Semaphore(_POLL_SLOTS)

# How long this entry's own BLE link must be continuously down before the
# device_unreachable repair is raised. Deliberately long: the household heal
# machinery (script.ble_heal_device, plus the hourly re-home) gets several
# passes inside this window, and the hold's own reconnect ladder settles at
# one attempt per minute — so 15 minutes of nothing means the automatic
# recovery has genuinely given up, not that a proxy is mid-roam.
UNREACHABLE_AFTER = timedelta(minutes=15)


def _async_holding_scanner(
    hass: HomeAssistant, address: str
) -> tuple[str, bluetooth.BaseHaScanner | None] | None:
    """(source, scanner) for the slot currently holding ``address``.

    habluetooth's slot-allocation table is the same source Home Assistant's
    own ``bluetooth/subscribe_connection_allocations`` websocket serves, so
    this answers "which proxy is carrying this fan right now" without poking
    at private scanner state.  None when no scanner reports the address —
    either nothing is connected, or the connection is via a path that does
    not report slot allocations.  The scanner is None when Home Assistant
    has none registered under that source.
    """
    source = allocation_source_for_address(
        get_manager().async_current_allocations(), address
    )
    if source is None:
        return None
    return source, bluetooth.async_scanner_by_source(hass, source)


@callback
def async_holding_scanner_name(hass: HomeAssistant, address: str) -> str | None:
    """Display name of the scanner/proxy currently holding a link to ``address``.

    This is what the Connection sensor shows.  For a remote scanner it is
    habluetooth's ``"<node> (<MAC>)"`` — see ``async_holding_proxy_node`` for
    the bare node name the ESPHome action lookup needs.
    """
    holder = _async_holding_scanner(hass, address)
    if holder is None:
        return None
    source, scanner = holder
    return scanner.name if scanner is not None else source


@callback
def async_holding_proxy_node(hass: HomeAssistant, address: str) -> str | None:
    """ESPHome node name of the proxy currently holding a link to ``address``.

    Not ``async_holding_scanner_name``: habluetooth builds a remote scanner's
    ``name`` as ``"<adapter> (<source>)"``, verified live on 2026-09-09 via
    ``bluetooth/subscribe_scanner_details`` — every proxy reported
    ``name="plant-room-bluetooth-proxy (54:32:04:3E:F3:72)"`` next to
    ``adapter="plant-room-bluetooth-proxy"``, while the registered action was
    ``esphome.plant_room_bluetooth_proxy_restart_proxy``.  Slugifying the
    display name looks up ``plant_room_bluetooth_proxy_54_32_04_3e_f3_72_…``,
    finds nothing, and silently drops the wizard's proxy rung.

    ``adapter`` is the node name ESPHome registered with, so it also survives
    the proxy's HA device being renamed or moved between areas
    (``downstairs-bluetooth-proxy`` kept its node name after its HA device
    moved to the Tool Room).  Falls back to the source MAC when no scanner is
    registered: that matches no ESPHome action, which is the honest answer.
    """
    holder = _async_holding_scanner(hass, address)
    if holder is None:
        return None
    source, scanner = holder
    return proxy_node_name(scanner) if scanner is not None else source


def proxy_node_name(scanner: bluetooth.BaseHaScanner) -> str:
    """Bare node name of ``scanner``: ``adapter``, else its name before " (".

    The split covers a scanner without ``adapter`` (a stub, or one whose
    ``name`` is all it exposes); neither branch ever yields the MAC suffix.
    """
    adapter = getattr(scanner, "adapter", None)
    if adapter:
        return adapter
    return scanner.name.split(" (")[0]


def unreachable_issue_id(address: str) -> str:
    """Repair-issue id for the fan at ``address`` being unreachable.

    One issue per config entry, keyed off the address rather than the entry
    id so the six fans never share one and the id stays stable across an
    entry being removed and re-added.
    """
    return f"{address.upper().replace(':', '')}_unreachable"


# hass.data[DOMAIN] key of the per-address outage clocks.  Deliberately NOT
# on the watchdog, the coordinator or the entry's runtime data: measured live
# on 2026-09-09 during a deliberate 21-minute power cut of the sibling Fluval
# light (the fans sit behind the same automation), automation.ble_proxy_autoheal
# reloads the config entry of any device whose link is down every 5 minutes,
# for exactly as long as it is down:
#
#     22:24:37  link drop       -> countdown armed
#     22:35:00  autoheal reload -> fresh watcher, countdown restarts at zero
#     22:40:00  autoheal reload -> fresh watcher, countdown restarts at zero
#
# Anything the entry owns dies with it on every reload, so a countdown kept
# there could never reach 15 minutes.  This mapping outlives every reload
# (async_unload_entry pops only its own entry id from hass.data[DOMAIN]) and
# an entry that cannot even be set up; it is forgotten only by a healthy
# observation or by the entry being removed.  A Home Assistant restart drops
# it, and then a fresh countdown is the honest answer.
LINK_OUTAGES_KEY = "_link_outages"


@dataclass(slots=True)
class LinkOutage:
    """One address's current outage: when it began, and the pending deadline."""

    down_since: float  # monotonic()
    cancel_deadline: CALLBACK_TYPE | None = None


def _link_outages(hass: HomeAssistant) -> dict[str, LinkOutage]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(LINK_OUTAGES_KEY, {})


@callback
def async_link_down(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The link to ``entry``'s fan is down right now; keep the repair honest.

    Starts the outage clock on the first unhealthy observation — and ONLY
    then, so a reload cannot restart it — then either raises the repair on
    the spot when the outage already spans the threshold, or makes sure one
    deadline is pending for the REMAINING window.  Idempotent by design:
    Home Assistant retries a not-ready setup on a backoff and the watchdog
    reconciles on every link event, and neither may push the deadline out
    or stack a second timer.

    Two callers, one clock: the watchdog of a loaded entry, and
    async_setup_entry at the point where it gives up because the fan cannot
    be found (the living-room vent fan sat in setup_retry from 22:23 on
    2026-09-09 with no watchdog and therefore no repair).
    """
    address = entry.data[CONF_ADDRESS].upper()
    outages = _link_outages(hass)
    now = monotonic()
    if (outage := outages.get(address)) is None:
        outage = outages[address] = LinkOutage(down_since=now)
    remaining = UNREACHABLE_AFTER - timedelta(seconds=now - outage.down_since)
    if remaining <= timedelta(0):
        async_create_unreachable_issue(hass, entry)
        return
    if outage.cancel_deadline is None:
        outage.cancel_deadline = async_call_later(
            hass, remaining, partial(_async_outage_deadline, hass, address)
        )


@callback
def async_clear_outage(hass: HomeAssistant, address: str) -> None:
    """Forget ``address``'s outage: the link is healthy, or the entry is gone."""
    outage = _link_outages(hass).pop(address.upper(), None)
    if outage is not None and outage.cancel_deadline is not None:
        outage.cancel_deadline()


@callback
def async_cancel_outage_deadline(hass: HomeAssistant, address: str) -> None:
    """Cancel the pending deadline but keep the clock running (entry unload).

    Whatever sets the entry up next — the watchdog, or the not-ready path —
    re-arms it for what is left of the window.
    """
    outage = _link_outages(hass).get(address.upper())
    if outage is not None and outage.cancel_deadline is not None:
        outage.cancel_deadline()
        outage.cancel_deadline = None


@callback
def _async_outage_deadline(hass: HomeAssistant, address: str, _now: datetime) -> None:
    """The outage has spanned the threshold; judge it against LIVE state.

    Scheduled on hass, not through the entry, because the not-ready path
    arms it from a setup that never gets to register an unload hook — so
    nothing about the entry can be assumed to still be true when it fires.
    """
    outage = _link_outages(hass).get(address)
    if outage is None:
        return
    outage.cancel_deadline = None
    entry = next(
        (
            candidate
            for candidate in hass.config_entries.async_entries(DOMAIN)
            if candidate.data[CONF_ADDRESS].upper() == address
        ),
        None,
    )
    # Disabling a setup_retry entry runs no unload hook of ours; this is the
    # only place that can notice the operator switched the fan off.
    if entry is None or entry.disabled_by is not None:
        return
    data: ACInfinityData | None = hass.data[DOMAIN].get(entry.entry_id)
    if data is not None:
        # Loaded: the watchdog judges the link, so a fan that came back
        # without anyone noticing is cleared rather than flagged.
        data.watchdog.async_link_changed()
        return
    # Not loaded: setup has failed to find the fan for the whole window.
    async_link_down(hass, entry)


@callback
def async_create_unreachable_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        unreachable_issue_id(entry.data[CONF_ADDRESS]),
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="device_unreachable",
        translation_placeholders={
            "name": entry.title,
            "minutes": str(int(UNREACHABLE_AFTER.total_seconds() // 60)),
        },
    )


class ACInfinityDataUpdateCoordinator(ActiveBluetoothDataUpdateCoordinator[None]):
    """Coordinator bridging HA bluetooth events and the AC Infinity controller."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        ble_device: BLEDevice,
        controller: ACInfinityDevice,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=logger,
            address=ble_device.address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_update,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self.ble_device = ble_device
        self.controller = controller
        self._device_ready = asyncio.Event()
        # Start True so the very first frame after (re)start logs the online
        # transition; also armed again by _async_handle_unavailable.
        self._was_unavailable = True
        self._cancel_controller_callback: Callable[[], None] | None = None
        self._health_listener: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Advertisement-based availability, widened by a live GATT link.

        A held device advertises much less often than an idle one (observed
        live), so ``async_track_unavailable`` can declare it gone while the
        integration is holding an open connection to it and commands are
        landing in ~200 ms.  A live link is the strongest proof of
        reachability there is, so it wins; everything else falls through to
        the base class's advertisement logic unchanged.
        """
        return self.controller.is_connected or super().available

    @property
    def link_healthy(self) -> bool:
        """Whether this entry's own path to the fan is working right now.

        The single source of truth for "is this fan reachable" — used by the
        unreachable watchdog and by the Repairs recovery wizard, so both
        judge the link exactly as the integration itself does.

        With the hold on (the default) the honest measure is the GATT link:
        keeping it up is the supervisor's entire job, so a link that is down
        is a real fault no matter how well the fan is still advertising.
        With the hold off there is no persistent link to measure and
        ``available`` — some scanner has seen the fan recently — is the only
        notion of reachability the integration has.
        """
        if self.controller.hold_status.hold:
            return self.controller.is_connected
        return self.available

    @callback
    def async_set_health_listener(self, listener: CALLBACK_TYPE | None) -> None:
        """Register (or clear) a callback for advertisement-driven health flips.

        The unreachable watchdog otherwise learns about the link from the
        hold supervisor and from habluetooth's allocation table, and both are
        silent when the hold is turned off for an entry: no supervisor runs,
        and a fan that simply stops advertising changes nobody's connection
        slots.  In exactly the configuration where ``link_healthy`` IS
        advertisement availability, the watchdog would then reconcile once at
        setup and never again — a fan that died afterwards would never raise
        the repair, and one that recovered would keep it forever.  So the two
        handlers that own those transitions report them here.
        """
        self._health_listener = listener

    @callback
    def _async_notify_health(self) -> None:
        if self._health_listener is not None:
            self._health_listener()

    @callback
    def _async_start(self) -> None:
        """Start bluetooth callbacks plus the controller push channel."""
        super()._async_start()
        # The controller fires callbacks for GATT notifications (0x1EFF frames
        # carrying tmp/hum/vpd/work_type while connected) and for command/poll
        # commits. Forwarding those to entity listeners means the UI reflects
        # a successful BLE write the moment it lands instead of waiting for
        # the next advertisement or poll.
        self._cancel_controller_callback = self.controller.register_callback(
            self._async_handle_controller_push
        )

    @callback
    def _async_stop(self) -> None:
        """Stop the controller push channel plus bluetooth callbacks."""
        if self._cancel_controller_callback is not None:
            self._cancel_controller_callback()
            self._cancel_controller_callback = None
        super()._async_stop()

    @callback
    def _async_handle_controller_push(
        self, state: DeviceInfo, change: CallbackType
    ) -> None:
        """Fan controller-originated state changes out to entities.

        ADVERTISEMENT callbacks are deliberately ignored here: they are always
        the direct result of _async_handle_bluetooth_event below, whose
        super() call already notifies listeners — forwarding them again would
        double-render every advertisement.
        """
        if change is CallbackType.ADVERTISEMENT:
            return
        self.async_update_listeners()

    @callback
    def _needs_poll(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        # Only poll once HA is fully running (startup floods every coordinator
        # with replayed advertisements; polling then would stampede the proxy
        # slots), when the controller says its GATT-only state is due, and
        # when there is actually a connectable path to the device right now.
        return (
            self.hass.state is CoreState.running
            and self.controller.update_needed(seconds_since_last_poll)
            and bool(
                bluetooth.async_ble_device_from_address(
                    self.hass, service_info.device.address, connectable=True
                )
            )
        )

    async def _async_update(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Poll the device for state advertisements cannot carry.

        Serialized through the module-level semaphore (see _POLL_SLOTS) and
        bounded by POLL_TIMEOUT so a pathological connection attempt cannot
        hold a poll slot indefinitely.
        """
        try:
            async with _POLL_SEMAPHORE:
                async with asyncio.timeout(POLL_TIMEOUT):
                    await self.controller.update()
        except CharacteristicMissingError:
            # Transient: a proxy handed us a cached-but-stale service table.
            # bleak-retry-connector re-resolves on the next attempt, and the
            # next due advertisement re-triggers the poll, so swallowing this
            # is safe and avoids flapping last_poll_successful.
            self.logger.debug(
                "%s (%s) transient BLE connection error during poll, will retry",
                self.ble_device.name,
                self.ble_device.address,
            )
            return
        self.logger.debug(
            "%s (%s) state after poll: %s",
            self.ble_device.name,
            self.ble_device.address,
            self.controller.state,
        )

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle every frame HA's bluetooth manager dispatches for this address.

        ROOT CAUSE of the historic fleet-wide freeze — do not reintroduce it:
        this method used to ``return`` early whenever a dispatched frame did
        not contain the AC Infinity manufacturer-data record (BLE splits data
        across ADV_IND and SCAN_RSP frames, and what each delivery contains
        depends on the proxy's scan mode and coalescing). The early return
        skipped ``super()._async_handle_bluetooth_event``, which is the ONLY
        place that (a) notifies entity listeners, (b) marks the device
        available again, and (c) evaluates ``needs_poll`` — polling in this
        coordinator family is advertisement-driven, so dropping frames also
        silently disabled the 30s GATT poll cycle. During stretches where the
        proxies delivered only record-less frames, all six fans kept
        "receiving advertisements" (and stayed available, since the address
        was genuinely being seen) while entity state froze indefinitely.

        The fix: every dispatched frame flows through super(); only the state
        merge is conditional on the manufacturer record being present.
        """
        self.logger.debug(
            "%s (%s) received: %s",
            self.ble_device.name,
            self.ble_device.address,
            service_info.advertisement,
        )
        self.ble_device = service_info.device
        # Keep the controller connecting via the freshest BLEDevice/proxy path
        # even when this particular frame carries no parseable payload.
        self.controller.update_ble_device(service_info.device)
        was_unavailable = self._was_unavailable
        if was_unavailable:
            self._was_unavailable = False
            self.logger.info(
                "%s (%s) is online", service_info.name, service_info.address
            )
        if MANUFACTURER_ID in service_info.advertisement.manufacturer_data:
            try:
                self.controller.set_ble_device_and_advertisement_data(
                    service_info.device, service_info.advertisement
                )
            except (IndexError, ValueError):
                # Truncated/malformed manufacturer record (seen at the fringe
                # of proxy range). Skip the merge; the frame still counts for
                # availability and poll scheduling below.
                self.logger.debug(
                    "%s (%s) ignoring malformed manufacturer data: %s",
                    self.ble_device.name,
                    self.ble_device.address,
                    service_info.advertisement.manufacturer_data[MANUFACTURER_ID].hex(),
                )
            else:
                if self.controller.name:
                    self._device_ready.set()
                self.logger.debug(
                    "%s (%s) state after advertisement: %s",
                    self.ble_device.name,
                    self.ble_device.address,
                    self.controller.state,
                )
        # ALWAYS runs: fires entity listeners (base passive coordinator does
        # this unconditionally per dispatched event) and schedules GATT polls.
        super()._async_handle_bluetooth_event(service_info, change)
        if was_unavailable:
            # A health transition, reported only on the flip (this runs for
            # every frame of every fan) and only after super(), which is what
            # marks the device available again.
            self._async_notify_health()

    @callback
    def _async_handle_unavailable(
        self, service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        """Handle no scanner having seen the device for the tracked interval.

        The base class (via bluetooth.async_track_unavailable) flips
        ``available`` to False and notifies listeners, so entities genuinely
        go unavailable instead of serving stale state forever. We add the
        operational log line and re-arm the online-transition log.
        """
        super()._async_handle_unavailable(service_info)
        self._was_unavailable = True
        self.logger.info(
            "%s (%s) is no longer seen by any Bluetooth scanner; marking unavailable",
            service_info.name,
            service_info.address,
        )
        self._async_notify_health()

    async def async_wait_ready(self) -> bool:
        """Wait for the first parseable advertisement after start."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(DEVICE_STARTUP_TIMEOUT):
                await self._device_ready.wait()
                return True
        return False


class ACInfinityLinkWatchdog:
    """Keeps the ``device_unreachable`` repair in sync with one fan's link.

    One instance per config entry: six fans share this code, so the issue id
    and the remembered proxy belong to the entry, not to the module.  The
    outage clock and its deadline do NOT belong to the entry — see
    LINK_OUTAGES_KEY for the measurement that decided this; the watchdog only
    drives them through async_link_down / async_clear_outage.

    THE RULE THIS CLASS EXISTS TO ENFORCE (learned the hard way in the
    sibling fluvalble integration on 2026-09-09: an issue raised at 12:22 was
    still open 13 hours after its condition cleared at 13:00, because the
    delete was gated on an in-memory "was previously bad" flag that the 12:38
    entry reload reset to None): deletion is NEVER gated on remembered
    state.  Reconciliation compares the issue against the link's ACTUAL
    state, runs on every link transition and once at setup, and both
    ``async_create_issue`` and ``async_delete_issue`` are idempotent — which
    is precisely what makes an unconditional reconcile the only form that
    survives a reload.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: ACInfinityDataUpdateCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.issue_id = unreachable_issue_id(self.address)
        self._unsubscribes: list[CALLBACK_TYPE] = []
        self._deadline_passed = False

    @callback
    def async_start(self) -> None:
        """Subscribe to every source of link changes and reconcile once, now.

        Two of the three are the pair the Connection sensor uses:
        habluetooth's allocation table reports connect/disconnect/roam across
        every proxy, and the hold status reports the drops and the reconnect
        ladder no bluetooth event carries.  Both are silent for an entry with
        the hold turned off, hence the coordinator's health listener as well —
        see async_set_health_listener.
        """
        self._unsubscribes.append(
            get_manager().async_register_allocation_callback(
                self._async_allocations_changed, None
            )
        )
        self._unsubscribes.append(
            self.coordinator.controller.hold_status.add_listener(
                self.async_link_changed
            )
        )
        self.coordinator.async_set_health_listener(self.async_link_changed)
        self._unsubscribes.append(
            lambda: self.coordinator.async_set_health_listener(None)
        )
        # Reality, not remembered state: an issue that outlived a reload means
        # the deadline already passed once, so it must not be re-armed (which
        # would let a genuinely dead fan's issue be re-armed forever by a
        # reload loop).  The outage clock is the primary source of "how long"
        # and already survives reloads; this covers the issue itself.  A Home
        # Assistant restart drops both (is_persistent=False), and then a fresh
        # countdown is the honest answer, because nothing knows how long the
        # link was down.
        self._deadline_passed = (
            ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id) is not None
        )
        self.async_link_changed()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and cancel the pending deadline (unload/reload).

        The deadline only: the outage clock keeps running, and whatever sets
        this entry up next arms a deadline for what is left of the window.
        """
        while self._unsubscribes:
            self._unsubscribes.pop()()
        async_cancel_outage_deadline(self.hass, self.address)

    @callback
    def async_link_changed(self) -> None:
        """Reconcile the repair issue against the link's actual state.

        Unconditional in both directions on purpose — see the class docstring.
        """
        if self.coordinator.link_healthy:
            async_clear_outage(self.hass, self.address)
            self._deadline_passed = False
            self._async_remember_proxy()
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return
        if self._deadline_passed:
            async_create_unreachable_issue(self.hass, self.entry)
            return
        async_link_down(self.hass, self.entry)

    @callback
    def _async_allocations_changed(
        self, allocations: HaBluetoothSlotAllocations
    ) -> None:
        """Handle a proxy reporting a change to its connection slots."""
        self.async_link_changed()

    @callback
    def _async_remember_proxy(self) -> None:
        """Write down the proxy currently carrying the link.

        Only while the link is up can this be answered at all: an unreachable
        fan is held by nobody, so the recovery wizard has no way to discover
        a proxy at Fix time.  Written only when it changed — every write is a
        config-entry update, and a fan that roams between two proxies would
        otherwise churn storage (and fire the entry update listener) on every
        reconnect.  The node name, not the display name: this record exists
        only to derive the ESPHome restart action later.
        """
        node = async_holding_proxy_node(self.hass, self.address)
        if node is None:
            return
        if self.entry.options.get(CONF_LAST_HOLDING_PROXY) == node:
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_LAST_HOLDING_PROXY: node},
        )


class ActiveBluetoothCoordinatorEntity[
    _ACInfinityCoordinatorT: ActiveBluetoothDataUpdateCoordinator = ActiveBluetoothDataUpdateCoordinator
](PassiveBluetoothCoordinatorEntity[_ACInfinityCoordinatorT]):
    """Entity base whose availability tracks live advertisement visibility.

    Subclasses core's PassiveBluetoothCoordinatorEntity (which provides
    listener registration and ``available = coordinator.available``) instead
    of re-implementing it: availability therefore means "some scanner has
    seen this device recently", which is the honest signal for a passive
    BLE fleet — GATT poll failures alone must not knock entities offline
    while advertisement data keeps flowing.
    """
