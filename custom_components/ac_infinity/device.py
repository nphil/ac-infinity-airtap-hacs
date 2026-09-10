"""Integration-side wrapper around the vendored AC Infinity BLE controller.

Adds what the vendored library does not model: the work modes beyond OFF/ON
(AUTO, the two countdown timers and CYCLE), their configuration registers,
and min/max speed bounds.

HARDWARE SAFETY: every byte sequence sent from this module follows the
payload grammar the vendored library already encodes (``[opcode, length,
value...]`` groups wrapped by ``Protocol._add_head``), writing only the
registers the device itself reports in its ``get_model_data`` response.
Do not invent register numbers here; add one only after a live capture
shows the device answering for it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .ac_infinity_ble import ACInfinityController, DeviceInfo
from .ac_infinity_ble.const import CallbackType
from .ac_infinity_ble.protocol import get_mode, parse_model_data
from .ac_infinity_ble.util import get_bit
from .const import FAMILY_E_MODELS
from .hold import HOLD_FAILURE_LOG_EVERY, HoldStatus, backoff_delay

# Work types (mode register values). The protocol enumerates modes 1-12 (see
# ac_infinity_ble/protocol.py get_mode); the AIRTAP T-series exposes exactly
# the six below on its own control panel ("OFF, ON, AUTO (2 triggers), TIMER
# TO ON, TIMER TO OFF, and CYCLE" — AIRTAP series manual), and its
# get_model_data response carries a configuration register for each of them
# (opcodes 19-22) and an empty group for the SCHEDULE register (23) other
# hardware has. Modes 7-12 have no register on this hardware and are
# read-only labels; see the README.
WORK_TYPE_OFF = 1
WORK_TYPE_ON = 2
WORK_TYPE_AUTO = 3
WORK_TYPE_TIMER_TO_ON = 4
WORK_TYPE_TIMER_TO_OFF = 5
WORK_TYPE_CYCLE = 6

# Configuration registers, by mode. Named here because they appear both in
# the poll parser and in the writers.
OPCODE_MIN_SPEED = 17
OPCODE_MAX_SPEED = 18
OPCODE_AUTO_THRESHOLDS = 19
OPCODE_TIMER_TO_ON = 20
OPCODE_TIMER_TO_OFF = 21
OPCODE_CYCLE = 22

# Longest duration the AIRTAP control panel can express (23:59:00). The
# registers are four bytes wide, so this bound is the hardware's, not the
# wire format's; it keeps a mistyped automation from parking a fan on a
# timer measured in weeks.
MAX_DURATION_SECONDS = 23 * 3600 + 59 * 60

# Modes ``async_set_work_type`` will send. OFF and ON are excluded on
# purpose: they go through the vendored ``set_level`` builder, which writes
# the mode AND its stored level in one frame.
SELECTABLE_WORK_TYPES = frozenset(
    {WORK_TYPE_AUTO, WORK_TYPE_TIMER_TO_ON, WORK_TYPE_TIMER_TO_OFF, WORK_TYPE_CYCLE}
)

# Log under the vendored controller's logger namespace so one logger line in
# configuration.yaml captures the whole BLE conversation.
_LOGGER = logging.getLogger(ACInfinityController.__module__)

# Floor between routine GATT polls. Polls are advertisement-driven (see
# coordinator.py); this floor keeps six fans from monopolizing the ESPHome
# proxy connection slots while still catching mode/threshold changes made
# from the vendor app within ~30s. Config writes bypass the floor via
# _config_changed_since_last_update so the next advertisement re-syncs
# immediately after we change something ourselves.
_MIN_SECONDS_BETWEEN_POLLS = 30


def _duration(value: bytes | None) -> Optional[int]:
    """Decode a duration register: a 32-bit big-endian count of seconds.

    None for a register the device did not answer for, so an entity can stay
    unknown rather than claim a zero the hardware never reported.
    """
    if value is None or len(value) < 4:
        return None
    return int.from_bytes(value[:4], "big")


def _duration_bytes(seconds: int) -> list[int]:
    """Encode a duration for a register, bounded by what the panel can set."""
    if not 0 <= seconds <= MAX_DURATION_SECONDS:
        raise ValueError(
            f"duration must be between 0 and {MAX_DURATION_SECONDS} seconds"
        )
    return list(int(seconds).to_bytes(4, "big"))


@dataclass
class DeviceInfoEx(DeviceInfo):
    """DeviceInfo extended with AUTO-mode configuration.

    Kept as a subclass (not a wrapper) so ``dataclasses.replace`` performed by
    the vendored advertisement merge preserves both the subclass and the
    ``auto_mode`` field — config entries store this dataclass in
    CONF_SERVICE_DATA, so its field set must stay backward compatible.
    """

    @staticmethod
    def create(device_info: DeviceInfo) -> DeviceInfoEx:
        return DeviceInfoEx(**device_info.__dict__)

    auto_mode: Optional[AutoModeConfig] = None


@dataclass
class AutoModeConfig:
    """AUTO-mode trigger thresholds as read from/written to the device.

    Temperatures are Celsius (the device stores both scales; is_degree is a
    display flag only). Humidity fields exist for all device types, but the
    AIRTAP type 6 has no humidity sensor — entity layers must not expose
    humidity thresholds for it.
    """

    high_temp_enabled: bool
    high_temp: int
    low_temp_enabled: bool
    low_temp: int
    high_humidity_enabled: bool
    high_humidity: int
    low_humidity_enabled: bool
    low_humidity: int


class ACInfinityDevice(ACInfinityController):
    """Controller with AUTO-mode support and optimistic local state."""

    _config_changed_since_last_update = False

    def __init__(
        self,
        ble_device: BLEDevice,
        state: DeviceInfoEx | None = None,
        advertisement_data: AdvertisementData | None = None,
    ):
        super().__init__(
            ble_device=ble_device,
            state=state,
            advertisement_data=advertisement_data,
        )

        # When constructed from advertisement_data alone (config flow path),
        # the vendored constructor parses a plain DeviceInfo. Upgrade it so
        # every later dataclasses.replace() (advertisement merges) keeps the
        # auto_mode field instead of silently dropping it. This was previously
        # ``if self._state is DeviceInfo:`` which compared an instance against
        # the class object and therefore never ran.
        if not isinstance(self._state, DeviceInfoEx):
            self._state = DeviceInfoEx.create(self._state)

        self._hold_status = HoldStatus()
        self._hold_task: asyncio.Task[None] | None = None
        self._hold_wake = asyncio.Event()
        self._cancel_drop_callback: Callable[[], None] | None = None
        self._hold_scanner_name: Callable[[], str | None] | None = None

    @property
    def hold_status(self) -> HoldStatus:
        """Live hold bookkeeping (read by the Connection diagnostic sensor)."""
        return self._hold_status

    def async_start_hold(
        self, scanner_name: Callable[[], str | None] | None = None
    ) -> None:
        """Start holding the GATT link open until ``async_stop_hold``.

        Idempotent, and non-blocking: the first connect happens in the
        supervisor task so config-entry setup never waits on a proxy.
        ``scanner_name`` resolves the proxy currently carrying the link for
        the log line; it is injected because naming a scanner needs Home
        Assistant and this module stays HA-free.
        """
        if self._hold_task is not None:
            return
        self._hold_scanner_name = scanner_name
        self.set_hold_connection(True)
        self._hold_status.set_hold(True)
        self._cancel_drop_callback = self.register_disconnect_callback(
            self._handle_unexpected_disconnect
        )
        self._hold_task = self.loop.create_task(
            self._hold_supervisor(), name=f"ac_infinity hold {self.address}"
        )

    async def async_stop_hold(self) -> None:
        """Stop holding and let the connection be released again.

        Unsubscribes the drop callback FIRST so the teardown that follows
        (``stop()`` on unload) cannot be mistaken for a lost link, then
        cancels the supervisor so nothing reconnects behind our back.
        """
        if self._cancel_drop_callback is not None:
            self._cancel_drop_callback()
            self._cancel_drop_callback = None
        task = self._hold_task
        self._hold_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - unload must never fail here
                _LOGGER.exception(
                    "%s: Hold supervisor exited with an unexpected error",
                    self.name,
                )
        self.set_hold_connection(False)
        self._hold_status.set_hold(False)
        self._hold_status.set_reconnect_attempt(0)

    def _handle_unexpected_disconnect(self) -> None:
        """Record a lost link and wake the supervisor (event-loop callback)."""
        self._hold_status.record_drop()
        self._hold_wake.set()

    async def _hold_supervisor(self) -> None:
        """Keep the link up for as long as the hold is active.

        Sleeps on an event between drops — no polling, no stacked reconnect
        tasks.  Reconnects go through the vendored ``_ensure_connected``,
        hence ``bleak_retry_connector.establish_connection``, so Home
        Assistant re-scores every proxy that can see the fan on every
        attempt and the link roams to the best path for free.

        INVARIANT — every path through the loop must hit a real suspension.
        ``asyncio.Event.wait()`` on an already-set event returns without
        yielding, so the wake event is cleared IMMEDIATELY before each wait
        (nothing can run between the two statements: neither is an await
        point, so no drop can be lost).  Waiting on a stale set — e.g. after
        a command's own retry rebuilt the link before this task was
        scheduled — would otherwise spin the event loop at 100%.
        """
        attempt = 0
        first_connect = True
        while True:
            if self.is_connected:
                self._hold_wake.clear()
                await self._hold_wake.wait()
                continue
            attempt += 1
            self._hold_status.set_reconnect_attempt(attempt)
            if not first_connect:
                await asyncio.sleep(backoff_delay(attempt))
            first_connect = False
            self._hold_wake.clear()
            try:
                await self._ensure_connected()
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - the hold never dies
                if attempt % HOLD_FAILURE_LOG_EVERY == 0:
                    _LOGGER.warning(
                        "%s: Still unable to hold a BLE connection after "
                        "%s attempts; RSSI: %s; last error: %s",
                        self.name,
                        attempt,
                        self.rssi,
                        ex,
                    )
                else:
                    _LOGGER.debug(
                        "%s: Hold reconnect attempt %s failed: %s",
                        self.name,
                        attempt,
                        ex,
                    )
                continue
            attempt = 0
            self._hold_status.set_reconnect_attempt(0)
            _LOGGER.info(
                "%s: Holding BLE connection via %s; RSSI: %s",
                self.name,
                self._resolve_scanner_name(),
                self.rssi,
            )

    def _resolve_scanner_name(self) -> str:
        """Name of the proxy carrying the link, for logging only."""
        if self._hold_scanner_name is None:
            return "unknown scanner"
        return self._hold_scanner_name() or "unknown scanner"

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Refresh only the BLEDevice used for connections.

        Called by the coordinator for dispatched frames that carry no
        manufacturer-data record: they cannot update parsed state (and must
        not count as data freshness), but they do prove which proxy currently
        sees the device, and reconnects should use that freshest path.
        """
        self._ble_device = ble_device

    @property
    def speed(self) -> Optional[int]:
        """Get the speed of the device (None until first advertisement)."""
        return self._state.fan

    @property
    def temperature(self) -> Optional[float]:
        """Get the temperature of the device."""
        return self._state.tmp

    @property
    def humidity(self) -> Optional[float]:
        """Get the humidity of the device (always 0.0 on AIRTAP type 6)."""
        return self._state.hum

    @property
    def vpd(self) -> Optional[float]:
        """Get the vpd of the device."""
        return self._state.vpd

    @property
    def auto_mode(self) -> Optional[AutoModeConfig]:
        """AUTO-mode thresholds; None until the first successful GATT poll."""
        return self._state.auto_mode

    @property
    def min_speed(self) -> Optional[int]:
        return self._state.level_off

    @property
    def max_speed(self) -> Optional[int]:
        return self._state.level_on

    @property
    def timer_to_on(self) -> Optional[int]:
        """TIMER TO ON countdown in seconds; None until the first poll."""
        return self._state.timer_to_on

    @property
    def timer_to_off(self) -> Optional[int]:
        """TIMER TO OFF countdown in seconds; None until the first poll."""
        return self._state.timer_to_off

    @property
    def cycle_on(self) -> Optional[int]:
        """CYCLE running phase in seconds; None until the first poll."""
        return self._state.cycle_on

    @property
    def cycle_off(self) -> Optional[int]:
        """CYCLE idle phase in seconds; None until the first poll."""
        return self._state.cycle_off

    @property
    def state(self) -> DeviceInfoEx:
        return self._state

    def update_needed(self, seconds_since_last_update: Optional[float | int]) -> bool:
        """Poll gate used by the coordinator's needs_poll evaluation."""
        return (
            self._config_changed_since_last_update
            or seconds_since_last_update is None
            or seconds_since_last_update > _MIN_SECONDS_BETWEEN_POLLS
        )

    async def update(self) -> None:
        """Poll the device for state not present in BLE advertisements.

        Fetches work_type, the speed bounds, the AUTO threshold block and the
        timer/cycle durations, by walking the response's ``[opcode, length,
        value]`` groups (see ``parse_model_data``) rather than trusting fixed
        offsets — which is what makes the groups past the AUTO block readable
        at all.
        """
        await self._ensure_connected()
        try:
            _LOGGER.debug("%s: Updating model data", self.name)
            command = self._protocol.get_model_data(self.state.type, 0, self.sequence)
            if data := await self._send_command(command):
                groups = parse_model_data(data)
                if 16 not in groups or len(groups.get(OPCODE_AUTO_THRESHOLDS, b"")) < 7:
                    # An ack, or a stale response to an earlier command:
                    # responses are not sequence-correlated. Keep the previous
                    # state and let the next poll retry.
                    _LOGGER.debug(
                        "%s: Skipping update; not a model-data response (%s): %s",
                        self.name,
                        len(data),
                        data.hex(),
                    )
                else:
                    self.state.work_type = groups[16][0]
                    self.state.level_off = groups[OPCODE_MIN_SPEED][0]
                    self.state.level_on = groups[OPCODE_MAX_SPEED][0]

                    thresholds = groups[OPCODE_AUTO_THRESHOLDS]
                    self.state.auto_mode = AutoModeConfig(
                        high_temp_enabled=not get_bit(thresholds[0], 4),
                        low_temp_enabled=not get_bit(thresholds[0], 5),
                        high_humidity_enabled=not get_bit(thresholds[0], 6),
                        low_humidity_enabled=not get_bit(thresholds[0], 7),
                        high_temp=thresholds[2],
                        low_temp=thresholds[4],
                        high_humidity=thresholds[5],
                        low_humidity=thresholds[6],
                    )

                    # Absent on a model that does not answer for the register
                    # (the group is missing, or empty as opcode 23 is here);
                    # leaving those None is what keeps their entities honest.
                    self.state.timer_to_on = _duration(groups.get(OPCODE_TIMER_TO_ON))
                    self.state.timer_to_off = _duration(groups.get(OPCODE_TIMER_TO_OFF))
                    cycle = groups.get(OPCODE_CYCLE)
                    if cycle is not None and len(cycle) >= 8:
                        self.state.cycle_on = _duration(cycle[:4])
                        self.state.cycle_off = _duration(cycle[4:8])

                    self._config_changed_since_last_update = False
                    self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            # Free the proxy connection slot immediately instead of holding
            # it for the vendored DISCONNECT_DELAY; the disconnect is polite
            # (skipped while another operation holds the lock, and skipped
            # entirely while a persistent hold is active — see
            # ACInfinityController._execute_disconnect).
            await self._execute_disconnect()

    async def async_set_work_type(self, work_type: int) -> None:
        """Select a work mode.

        Payload ``[16, 1, work_type]`` is the mode half of the vendored
        ``set_level`` builder, sent on its own: unlike ``set_level`` it does
        NOT also rewrite a level, which is exactly right for the modes that
        run themselves off a configuration register (AUTO, the two countdown
        timers, CYCLE). OFF and ON keep going through ``set_level`` so they
        continue to carry their stored level with them.
        """
        if work_type not in SELECTABLE_WORK_TYPES:
            raise ValueError(f"Work type {work_type} cannot be selected")
        _LOGGER.debug("%s: Setting mode to %s", self.name, get_mode(work_type))

        command = [16, 1, work_type]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)
        await self._ensure_connected()
        try:
            await self._send_command(command)

            self.state.work_type = work_type
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def set_mode_auto(self) -> None:
        """Set the device's mode to automatic."""
        await self.async_set_work_type(WORK_TYPE_AUTO)

    async def async_set_auto_high_temp(self, value: float) -> None:
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )

        new_config = dataclasses.replace(self.auto_mode, high_temp=round(value))
        await self.async_set_auto_mode_config(new_config)

    async def async_set_auto_low_temp(self, value: float) -> None:
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )

        new_config = dataclasses.replace(self.auto_mode, low_temp=round(value))
        await self.async_set_auto_mode_config(new_config)

    async def async_set_auto_mode_high_temp_enabled(self, enabled: bool) -> None:
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )

        new_config = dataclasses.replace(self.auto_mode, high_temp_enabled=enabled)
        await self.async_set_auto_mode_config(new_config)

    async def async_set_auto_mode_low_temp_enabled(self, enabled: bool) -> None:
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )

        new_config = dataclasses.replace(self.auto_mode, low_temp_enabled=enabled)
        await self.async_set_auto_mode_config(new_config)

    async def async_set_auto_mode_config(self, config: AutoModeConfig) -> None:
        """Write the full AUTO-mode threshold block.

        The device only accepts the block as a whole (command 19), so single
        threshold changes are performed by the helpers above as a
        read-modify-write against the last polled configuration.
        """
        if config is None:
            raise ValueError("config cannot be None")
        _LOGGER.debug("%s: Setting auto mode config to %s", self.name, config)

        def byte_for_temp_hum_enabled_switches(config: AutoModeConfig) -> int:
            b = 8 if config.high_temp_enabled else 0
            if config.low_temp_enabled:
                b |= 4
            if config.high_humidity_enabled:
                b |= 2
            if config.low_humidity_enabled:
                b |= 1
            return b

        def c_to_f(celsius: float) -> float:
            return round((celsius * 9.0 / 5.0) + 32.0, 2)

        temp_hum_enabled_switches = byte_for_temp_hum_enabled_switches(config)
        # Note: Logic does not differ based on value of is_degree, as that is
        # a display flag only. The protocol carries both Celsius and
        # Fahrenheit values; our data model uses Celsius only.
        high_temp_f = round(c_to_f(config.high_temp))
        high_temp_c = config.high_temp
        low_temp_f = round(c_to_f(config.low_temp))
        low_temp_c = config.low_temp

        command = [
            19,
            7,
            temp_hum_enabled_switches,
            high_temp_f,
            high_temp_c,
            low_temp_f,
            low_temp_c,
            config.high_humidity,
            config.low_humidity,
        ]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)

        await self._ensure_connected()
        try:
            await self._send_command(command)

            self.state.auto_mode = config
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def async_set_min_speed(self, value: int) -> None:
        """Set the minimum fan speed for auto and other dynamic modes."""
        if value not in range(0, 11):
            raise ValueError("value must be between 0 and 10")

        _LOGGER.debug("%s: Setting min speed to %s", self.name, value)

        command = [17, 1, value]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)

        await self._ensure_connected()
        try:
            await self._send_command(command)

            self.state.level_off = value
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def async_set_max_speed(self, value: int) -> None:
        """Set the maximum fan speed for auto and other dynamic modes."""
        if value not in range(0, 11):
            raise ValueError("value must be between 0 and 10")

        _LOGGER.debug("%s: Setting max speed to %s", self.name, value)

        command = [18, 1, value]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)

        await self._ensure_connected()
        try:
            await self._send_command(command)

            # Register 18 is the ON/max level; mirroring it into level_off
            # here (as this method previously did) made the min-speed number
            # entity jump to the max value after every max-speed change.
            self.state.level_on = value
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def _async_write_register(self, opcode: int, value: list[int]) -> None:
        """Write one ``[opcode, length, value...]`` configuration group."""
        command = [opcode, len(value), *value]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)

        await self._ensure_connected()
        try:
            await self._send_command(command)
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def async_set_timer_to_on(self, seconds: int) -> None:
        """Set the TIMER TO ON countdown (mode 4)."""
        _LOGGER.debug("%s: Setting timer to on to %ss", self.name, seconds)
        payload = _duration_bytes(seconds)
        await self._async_write_register(OPCODE_TIMER_TO_ON, payload)
        self.state.timer_to_on = seconds

    async def async_set_timer_to_off(self, seconds: int) -> None:
        """Set the TIMER TO OFF countdown (mode 5)."""
        _LOGGER.debug("%s: Setting timer to off to %ss", self.name, seconds)
        payload = _duration_bytes(seconds)
        await self._async_write_register(OPCODE_TIMER_TO_OFF, payload)
        self.state.timer_to_off = seconds

    async def async_set_cycle(self, on_seconds: int, off_seconds: int) -> None:
        """Write both halves of the CYCLE register (mode 6).

        One register holds both phases, so — exactly like the AUTO threshold
        block — a single-phase change is a read-modify-write against the last
        polled pair rather than a partial write.
        """
        _LOGGER.debug(
            "%s: Setting cycle to %ss on / %ss off", self.name, on_seconds, off_seconds
        )
        payload = _duration_bytes(on_seconds) + _duration_bytes(off_seconds)
        await self._async_write_register(OPCODE_CYCLE, payload)
        self.state.cycle_on = on_seconds
        self.state.cycle_off = off_seconds

    async def async_set_cycle_on(self, seconds: int) -> None:
        if self.cycle_off is None:
            raise ValueError(
                "Cycle configuration is not loaded; cannot change configuration values"
            )
        await self.async_set_cycle(seconds, self.cycle_off)

    async def async_set_cycle_off(self, seconds: int) -> None:
        if self.cycle_on is None:
            raise ValueError(
                "Cycle configuration is not loaded; cannot change configuration values"
            )
        await self.async_set_cycle(self.cycle_on, seconds)
