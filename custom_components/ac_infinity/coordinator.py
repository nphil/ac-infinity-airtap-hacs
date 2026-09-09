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

from bleak.backends.device import BLEDevice
from habluetooth import get_manager
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.core import CoreState, HomeAssistant, callback

from .ac_infinity_ble.const import MANUFACTURER_ID, CallbackType
from .ac_infinity_ble.exceptions import CharacteristicMissingError
from .ac_infinity_ble.models import DeviceInfo
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


@callback
def async_holding_scanner_name(hass: HomeAssistant, address: str) -> str | None:
    """Name of the scanner/proxy currently holding a link to ``address``.

    habluetooth's slot-allocation table is the same source Home Assistant's
    own ``bluetooth/subscribe_connection_allocations`` websocket serves, so
    this answers "which proxy is carrying this fan right now" without poking
    at private scanner state.  None when no scanner reports the address —
    either nothing is connected, or the connection is via a path that does
    not report slot allocations.
    """
    source = allocation_source_for_address(
        get_manager().async_current_allocations(), address
    )
    if source is None:
        return None
    scanner = bluetooth.async_scanner_by_source(hass, source)
    return scanner.name if scanner is not None else source


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
        if self._was_unavailable:
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

    async def async_wait_ready(self) -> bool:
        """Wait for the first parseable advertisement after start."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(DEVICE_STARTUP_TIMEOUT):
                await self._device_ready.wait()
                return True
        return False


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
