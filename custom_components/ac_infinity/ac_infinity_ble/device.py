"""BLE controller for AC Infinity devices.

Vendored (formerly the standalone ``ac-infinity-ble`` package) so the
integration ships fixes without a PyPI round-trip; the manifest deliberately
declares no requirements.  This module MUST stay a pure BLE library:
no ``homeassistant`` imports.

Concurrency model (WHY it is shaped this way):

- ``_operation_lock`` serializes a whole command round-trip
  (connect -> write -> wait for notification).  It is held for the entire
  span so nothing can tear the connection down between the connectivity
  check and the GATT write.
- ``_connect_lock`` guards connection setup/teardown only.
- Lock order is ALWAYS operation lock -> connect lock, never the reverse.
  ``_execute_disconnect`` only *peeks* at the operation lock with a
  non-blocking ``locked()``, so it cannot deadlock with a command in flight.
- The idle-disconnect timer and the polite (``force=False``) disconnects
  refuse to tear down while an operation holds the operation lock; only
  error paths and ``stop()`` force a teardown.  This matters because the
  fans share a handful of ESPHome proxy connection slots: needlessly tearing
  down a connection another command is using costs a full reconnect cycle at
  poor RSSI.

Connect retries: ``establish_connection`` (bleak-retry-connector) already
implements bounded, backed-off connect retries, including ESP32-proxy
out-of-slot handling.  ``_send_command_locked`` additionally re-ensures the
connection per attempt under ``retry_bluetooth_connection_error``, so a
connection dropped mid-command is re-established and the command resent up
to ``DEFAULT_ATTEMPTS`` times.  Do not stack another retry loop on top:
retry layers multiply worst-case blocking time and hog proxy slots.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)

from .const import (
    MANUFACTURER_ID,
    POSSIBLE_READ_CHARACTERISTIC_UUIDS,
    POSSIBLE_WRITE_CHARACTERISTIC_UUIDS,
    CallbackType,
)
from .exceptions import CharacteristicMissingError
from .models import DeviceInfo
from .protocol import Protocol, parse_manufacturer_data
from .util import get_bit, get_bits, get_short

BLEAK_BACKOFF_TIME = 0.25
DISCONNECT_DELAY = 120
DEFAULT_ATTEMPTS = 3
# Seconds to wait for the notification that answers a written command.
# Devices normally answer well under a second; five seconds tolerates a
# congested proxy without stalling the coordinator for long.
NOTIFY_TIMEOUT = 5

_LOGGER = logging.getLogger(__name__)


class ACInfinityController:
    def __init__(
        self,
        ble_device: BLEDevice,
        state: DeviceInfo | None = None,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Init the ACInfinityController."""
        if not state and not advertisement_data:
            raise ValueError("Must provide either state or advertisement_data")

        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._operation_lock = asyncio.Lock()
        self._state = state or parse_manufacturer_data(
            advertisement_data.manufacturer_data[MANUFACTURER_ID]  # type: ignore
        )
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._read_char: BleakGATTCharacteristic | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._client: BleakClientWithServiceCache | None = None
        self._protocol: Protocol = Protocol()
        self._expected_disconnect = False
        self.loop = asyncio.get_running_loop()
        self._callbacks: list[Callable[[DeviceInfo, CallbackType], None]] = []
        self._notify_future: asyncio.Future[bytearray] | None = None
        self._sequence = 1
        self._last_advertisement_monotonic: float | None = None
        if advertisement_data is not None:
            # Being handed an advertisement at construction means one was
            # just seen (or HA's scanner cache holds a recent one); without
            # this, availability logic would treat a freshly set-up device
            # as never-seen until its next broadcast.
            self.mark_advertisement_received()

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device and merge freshly advertised state."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        info = parse_manufacturer_data(
            advertisement_data.manufacturer_data[MANUFACTURER_ID]
        )
        # Advertisements never carry work_type/level_on/level_off; the
        # None-filter below preserves those previously learned fields.
        self._state = replace(
            self._state, **{k: v for k, v in asdict(info).items() if v is not None}
        )
        if self._state.fan:
            # Advertisements carry the live fan level but not the stored
            # bounds; clamp the bounds so they never contradict an observed
            # level.  In work_type 1/2 the level equals level_off/level_on so
            # this is a no-op there; it only matters in AUTO where the level
            # floats between the two.  NOTE: this previously read
            # ``level_off or 0 > fan``, which parses as
            # ``level_off or (0 > fan)`` and clobbered both bounds with the
            # live level on every advertisement.
            if (self._state.level_off or 0) > self._state.fan:
                self._state.level_off = self._state.fan
            if (self._state.level_on or 10) < self._state.fan:
                self._state.level_on = self._state.fan
        self.mark_advertisement_received()
        self._fire_callbacks(CallbackType.ADVERTISEMENT)

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        return self._state.name

    @property
    def is_on(self) -> bool:
        """Get whether the device is in manual-ON mode with a nonzero level.

        AUTO (work_type 3) deliberately reports False here; how AUTO maps to
        an on/off UI concept is the entity layer's decision.
        """
        return bool(self._state.work_type == 2 and self._state.fan)

    @property
    def speed(self) -> int:
        """Get the speed of the device."""
        return self._state.fan or 0

    @property
    def temperature(self) -> float:
        """Get the temperature of the device."""
        return self._state.tmp or 0

    @property
    def humidity(self) -> float:
        """Get the humidity of the device."""
        return self._state.hum or 0

    @property
    def vpd(self) -> float:
        """Get the vpd of the device."""
        return self._state.vpd or 0

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def state(self) -> DeviceInfo:
        """Return the state."""
        return self._state

    @property
    def last_advertisement_monotonic(self) -> float | None:
        """``time.monotonic()`` timestamp of the last recorded advertisement.

        None until the first advertisement is recorded.  Monotonic (not wall
        clock) so availability math survives system clock changes.
        """
        return self._last_advertisement_monotonic

    @property
    def advertisement_age(self) -> float | None:
        """Seconds since the last recorded advertisement, or None if never.

        Intended for the integration's availability logic: a device whose
        advertisements have stopped is unreachable regardless of what the
        (stale) state dataclass still says.
        """
        if self._last_advertisement_monotonic is None:
            return None
        return time.monotonic() - self._last_advertisement_monotonic

    def mark_advertisement_received(self, when: float | None = None) -> None:
        """Record that an advertisement was parsed for this device.

        Called automatically by ``set_ble_device_and_advertisement_data``;
        exposed publicly because integration subclasses override that method
        without calling super() and must record freshness themselves.
        ``when`` must be a ``time.monotonic()`` value when supplied.
        """
        self._last_advertisement_monotonic = (
            time.monotonic() if when is None else when
        )

    @property
    def sequence(self) -> int:
        """Increment and return the sequence number."""
        if self._sequence == 65535:
            self._sequence = 0
        self._sequence += 1
        return self._sequence

    async def update(self) -> None:
        """Poll model data and merge the response into state."""
        _LOGGER.debug("%s: Updating", self.name)
        command = self._protocol.get_model_data(self._state.type, 0, self.sequence)
        try:
            data = await self._send_command(command)
            if data is None:
                # Timed out waiting for the response; keep previous state
                # rather than inventing values.
                return
            if len(data) < 19:
                # A short frame is an ack or a stale response from an earlier
                # command (responses are not sequence-correlated).  Parsing
                # it would previously IndexError or write garbage into
                # work_type/levels.
                _LOGGER.debug(
                    "%s: Ignoring short model-data response (%d bytes): %s",
                    self.name,
                    len(data),
                    data.hex(),
                )
                return
            self._state.work_type = data[12]
            self._state.level_off = data[15]
            self._state.level_on = data[18]
            # Mirror the device's own model: in OFF/ON mode the live level IS
            # the stored level_off/level_on.  In AUTO (3) the level floats and
            # advertisements keep it fresh, so leave it untouched here.
            if self._state.work_type == 1:
                self._state.fan = self._state.level_off
            elif self._state.work_type == 2:
                self._state.fan = self._state.level_on
            self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            await self._execute_disconnect()

    async def turn_on(self, speed: int | None = None) -> None:
        """Switch to manual-ON mode.

        State is committed only after the BLE write succeeds so a failed
        command can never leave HA believing the fan is on (honest-state
        rule; the old code mutated first and never rolled back).
        """
        _LOGGER.debug("%s: Turn on", self.name)
        level_on = speed if speed is not None else self._state.level_on or 10
        command = self._protocol.set_level(
            self._state.type, 2, level_on, 0, self.sequence
        )
        try:
            await self._send_command(command)
            self._state.work_type = 2
            self._state.level_on = level_on
            self._state.fan = level_on
            self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            await self._execute_disconnect()

    async def turn_off(self) -> None:
        """Switch to OFF mode, preserving the stored OFF-mode level.

        OFF (work_type 1) is a real mode with its own level; this keeps the
        user's stored ``level_off`` instead of forcing it to zero.  State is
        committed only after the BLE write succeeds.
        """
        _LOGGER.debug("%s: Turn off", self.name)
        level_off = self._state.level_off or 0
        command = self._protocol.set_level(
            self._state.type, 1, level_off, 0, self.sequence
        )
        try:
            await self._send_command(command)
            self._state.work_type = 1
            self._state.level_off = level_off
            self._state.fan = level_off
            self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            await self._execute_disconnect()

    async def set_speed(self, speed: int) -> None:
        """Set the fan level, entering ON mode (or OFF mode for level 0).

        Mutations mirror the update() parser exactly: work_type 1 keeps
        fan == level_off, work_type 2 keeps fan == level_on.  State is
        committed only after the BLE write succeeds.
        """
        _LOGGER.debug("%s: Set speed to %s", self.name, speed)
        work_type = 2 if speed > 0 else 1
        command = self._protocol.set_level(
            self._state.type, work_type, speed, 0, self.sequence
        )
        try:
            await self._send_command(command)
            self._state.work_type = work_type
            self._state.fan = speed
            if work_type == 1:
                self._state.level_off = speed
            else:
                self._state.level_on = speed
            self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            await self._execute_disconnect()

    async def stop(self) -> None:
        """Stop the controller and release the connection.

        Waits for any in-flight command (operation lock) so unload cannot
        yank the connection out from under a GATT write, then forces the
        teardown.
        """
        _LOGGER.debug("%s: Stop", self.name)
        async with self._operation_lock:
            await self._execute_disconnect(force=True)

    def _fire_callbacks(self, type: CallbackType) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(self._state, type)

    def register_callback(
        self, callback: Callable[[DeviceInfo, CallbackType], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            _LOGGER.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self.name,
                self._disconnected,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,
            )
            _LOGGER.debug("%s: Connected; RSSI: %s", self.name, self.rssi)
            if not self._resolve_characteristics(client.services):
                # A stale service cache (firmware update, proxy quirk) can
                # leave the vendor characteristics unresolved.  The old code
                # fell back to the long-removed BleakClient.get_services()
                # and, failing that, called start_notify(None) and leaked the
                # connection.  Clear the cache and fail this attempt closed
                # so the next attempt refetches services.
                _LOGGER.warning(
                    "%s: Characteristics missing, clearing service cache; RSSI: %s",
                    self.name,
                    self.rssi,
                )
                self._read_char = None
                self._write_char = None
                await client.clear_cache()
                self._expected_disconnect = True
                await client.disconnect()
                raise CharacteristicMissingError(
                    "Failed to resolve read/write characteristics"
                )
            _LOGGER.debug(
                "%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi
            )
            try:
                await client.start_notify(self._read_char, self._notification_handler)
            except BLEAK_EXCEPTIONS:
                # Subscribing can fail transiently on a fresh link; drop the
                # connection so the retry layer reconnects cleanly instead of
                # leaving a half-initialized client that would fail every
                # later command.
                self._read_char = None
                self._write_char = None
                self._expected_disconnect = True
                await client.disconnect()
                raise
            # Publish the client only once it is fully usable, so no other
            # coroutine can observe a connection without notifications.
            self._client = client
            self._reset_disconnect_timer()

    def _notification_handler(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle notification responses."""
        _LOGGER.debug("%s: Notification received: %s", self.name, data.hex())
        if self._notify_future and not self._notify_future.done():
            self._notify_future.set_result(data)
            return

        # Unsolicited broadcast frame; indexes up to data[17], so guard the
        # length: a truncated frame would IndexError inside a bleak callback.
        if len(data) >= 18 and data[0] == 0x1E and data[1] == 0xFF:
            self._state.is_degree = get_bit(data[6], 0)
            self._state.tmp_state = get_bits(data[6], 1, 2)
            self._state.hum_state = get_bits(data[6], 3, 2)
            self._state.vpd_state = get_bits(data[6], 5, 2)
            self._state.choose_port = get_bits(data[7], 4, 4)
            self._state.tmp = get_short(data, 8) / 100
            self._state.hum = get_short(data, 10) / 100
            self._state.vpd = get_short(data, 12) / 100
            self._state.fan_type = get_short(data, 14)
            self._state.fan_state = get_bits(data[16], 0, 2)
            # self._state.fan = get_bits(data[17], 0, 4) # Not accurate
            self._state.work_type = get_bits(data[17], 4, 4)
            self._fire_callbacks(CallbackType.NOTIFICATION)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        self._disconnect_timer = self.loop.call_later(
            DISCONNECT_DELAY, self._disconnect
        )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._client is not None and client is not self._client:
            # A previous connection's late callback must not affect the
            # current connection's command in flight.
            _LOGGER.debug(
                "%s: Ignoring disconnect callback from a stale client", self.name
            )
            return
        if self._notify_future and not self._notify_future.done():
            # Fail a waiting command immediately instead of letting it burn
            # the full notification timeout; BleakError is retryable, so the
            # retry layer reconnects and resends right away.
            self._notify_future.set_exception(
                BleakError("Disconnected while waiting for response")
            )
        if self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s", self.name, self.rssi
            )
            return
        _LOGGER.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.name,
            self.rssi,
        )

    def _disconnect(self) -> None:
        """Idle-disconnect timer fired."""
        self._disconnect_timer = None
        if self._operation_lock.locked():
            # A command round-trip is in flight; disconnecting now would
            # yank the connection out from under its GATT write.  Give it a
            # fresh idle window instead.
            self._reset_disconnect_timer()
            return
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            DISCONNECT_DELAY,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self, force: bool = False) -> None:
        """Execute disconnection.

        ``force=False`` (the default, used by the idle timer and the polite
        trailing disconnects of the high-level operations) refuses to tear
        the connection down while another operation holds the operation
        lock: the in-flight command owns the connection, and its own
        trailing disconnect (or the idle timer) will release it.  Error
        paths and ``stop()`` pass ``force=True`` because they must reset
        connection state unconditionally.
        """
        async with self._connect_lock:
            if not force and self._operation_lock.locked():
                _LOGGER.debug(
                    "%s: Skipping disconnect; another operation is in progress",
                    self.name,
                )
                return
            if self._disconnect_timer:
                # A pending idle timer would otherwise fire later against
                # whatever connection exists at that point.
                self._disconnect_timer.cancel()
                self._disconnect_timer = None
            read_char = self._read_char
            client = self._client
            self._expected_disconnect = True
            self._client = None
            self._read_char = None
            self._write_char = None
            if client and client.is_connected:
                if read_char:
                    try:
                        await client.stop_notify(read_char)
                    except BLEAK_EXCEPTIONS:
                        # A dying link often fails stop_notify; the
                        # disconnect below must still run or the proxy
                        # connection slot leaks until the supervisor times
                        # it out.
                        _LOGGER.debug(
                            "%s: Failed to stop notifications on disconnect",
                            self.name,
                            exc_info=True,
                        )
                try:
                    await client.disconnect()
                except BLEAK_EXCEPTIONS:
                    _LOGGER.debug(
                        "%s: Error during disconnect", self.name, exc_info=True
                    )

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _send_command_locked(self, command: bytes) -> bytes | None:
        """Connect if needed, send the command, and read the response.

        The connection attempt lives INSIDE this retried unit on purpose:
        the error handlers below tear the connection down, so without a
        reconnect here every retry would previously hit
        CharacteristicMissingError ("Client is not connected") and the
        retry decorator was effectively dead code.
        """
        await self._ensure_connected()
        try:
            return await self._execute_command_locked(command)
        except BleakDBusError as ex:
            # Disconnect so we can reset state and try again
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnecting due to error: %s",
                self.name,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            await self._execute_disconnect(force=True)
            raise
        except BleakError as ex:
            # Disconnect so we can reset state and try again
            _LOGGER.debug(
                "%s: RSSI: %s; Disconnecting due to error: %s", self.name, self.rssi, ex
            )
            await self._execute_disconnect(force=True)
            raise

    async def _send_command(
        self, command: bytes, retry: int | None = None
    ) -> bytes | None:
        """Send command to device and read response.

        ``retry`` is kept for API compatibility but unused; retry count is
        fixed by the ``retry_bluetooth_connection_error`` decorator.
        """
        return await self._send_command_while_connected(command, retry)

    async def _send_command_while_connected(
        self, command: bytes, retry: int | None = None
    ) -> bytes | None:
        """Send command to device and read response.

        Connection is (re-)established inside the operation lock, so a
        disconnect executed between two queued commands cannot yank the
        connection out from under the next one -- it simply reconnects.
        """
        _LOGGER.debug(
            "%s: Sending command %s",
            self.name,
            command.hex(),
        )
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                return await self._send_command_locked(command)
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                _LOGGER.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.debug("%s: communication failed", self.name, exc_info=True)
                raise

    async def _execute_command_locked(self, command: bytes) -> bytes | None:
        """Execute command and read response."""
        if self._client is None:
            raise CharacteristicMissingError("Client is not connected")
        if not self._read_char:
            raise CharacteristicMissingError("Read characteristic missing")
        if not self._write_char:
            raise CharacteristicMissingError("Write characteristic missing")

        # Hold locals so a forced teardown nulling the attributes cannot
        # AttributeError mid-command; a torn connection surfaces as a
        # BleakError from the write/wait instead, which the retry layer
        # handles.
        client = self._client
        write_char = self._write_char
        self._notify_future = self.loop.create_future()
        try:
            await client.write_gatt_char(write_char, command, False)
            try:
                async with asyncio.timeout(NOTIFY_TIMEOUT):
                    return await self._notify_future
            except asyncio.TimeoutError:
                _LOGGER.debug(
                    "%s: No response within %ss for command %s",
                    self.name,
                    NOTIFY_TIMEOUT,
                    command.hex(),
                )
                return None
        finally:
            # Always clear the pending future, including on write failure or
            # cancellation; a stale future would otherwise swallow the next
            # unsolicited broadcast notification instead of parsing it.
            self._notify_future = None

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        for characteristic in POSSIBLE_READ_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._read_char = char
                break
        for characteristic in POSSIBLE_WRITE_CHARACTERISTIC_UUIDS:
            if char := services.get_characteristic(characteristic):
                self._write_char = char
                break
        return bool(self._read_char and self._write_char)
