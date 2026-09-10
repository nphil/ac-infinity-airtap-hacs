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
from datetime import datetime, timedelta

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

    One instance per config entry: six fans share this code, so the issue id,
    the 15-minute timer and the remembered proxy all belong to the entry, not
    to the module.

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
        self._cancel_deadline: CALLBACK_TYPE | None = None
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
        # the deadline already passed once, so the countdown must not restart
        # from zero (which would let a genuinely dead fan's issue be re-armed
        # forever by a reload loop). A Home Assistant restart drops the issue
        # (is_persistent=False), and then a fresh countdown is the honest
        # answer, because nothing knows how long the link was down.
        self._deadline_passed = (
            ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id) is not None
        )
        self.async_link_changed()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and cancel the pending deadline (unload/reload)."""
        while self._unsubscribes:
            self._unsubscribes.pop()()
        self._async_cancel_deadline()

    @callback
    def async_link_changed(self) -> None:
        """Reconcile the repair issue against the link's actual state.

        Unconditional in both directions on purpose — see the class docstring.
        """
        if self.coordinator.link_healthy:
            self._async_cancel_deadline()
            self._deadline_passed = False
            self._async_remember_proxy()
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return
        if self._deadline_passed:
            self._async_create_issue()
            return
        if self._cancel_deadline is None:
            self._cancel_deadline = async_call_later(
                self.hass, UNREACHABLE_AFTER, self._async_deadline_reached
            )

    @callback
    def _async_deadline_reached(self, _now: datetime) -> None:
        """Handle the link having been down for the whole threshold."""
        self._cancel_deadline = None
        self._deadline_passed = True
        self.async_link_changed()

    @callback
    def _async_cancel_deadline(self) -> None:
        if self._cancel_deadline is not None:
            self._cancel_deadline()
            self._cancel_deadline = None

    @callback
    def _async_create_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self.issue_id,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders={
                "name": self.entry.title,
                "minutes": str(int(UNREACHABLE_AFTER.total_seconds() // 60)),
            },
        )

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
