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
from bleak_retry_connector import BleakClientWithServiceCache

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

# Display register: [brightness gear] or [brightness gear, backlight 0/1].
# The AC Infinity app reads it with its settings (opcodes 32-36) and writes
# it in the 2-byte form only to fans whose read answered with two bytes, so
# the switch below works the same way: it writes nothing until the fan has
# reported the register, and never in a form the fan did not report.
OPCODE_DISPLAY = 33

# The rest of the vendor app's "settings" bundle (decompiled app 2.0.9,
# ProtocolResolution.getSettingData reads 32, 33, 34, 36, 17, 18 for device
# type 6). Each is read with every poll and written only in the exact shape
# the fan reported, exactly like the display register.
#   32 [unit]           display unit: 1 = Celsius, 0 = Fahrenheit
#   34 [F, C, hum]      AUTO ramp: degrees per speed step (0 = jump to max)
#   36 [F, C, hum]      temperature/humidity calibration, signed offsets
# Register 35 (buffer) is deliberately not read: the app reads it only for
# other models, and nobody has seen how an AIRTAP answers for it.
OPCODE_UNIT = 32
OPCODE_RAMP = 34
OPCODE_CALIBRATION = 36

# Every poll reads the mode registers (get_model_data's 16-23) and the
# settings registers in one round trip. The fan answers each group it was
# asked for (an empty one for a register it lacks, as with 23), so the
# extra groups cannot disturb the others.
POLL_OPCODES = (
    16, 17, 18, 19, 20, 21, 22, 23,
    OPCODE_UNIT, OPCODE_DISPLAY, OPCODE_RAMP, OPCODE_CALIBRATION,
)

# Display brightness gears, as the fan's own panel and the vendor app name
# them: three fixed levels, and two that dim to level 1 after 15 s idle.
DISPLAY_BRIGHTNESS_GEARS = {0x01: "Low", 0x02: "Medium", 0x03: "High",
                            0xA2: "Auto-dim, medium", 0xA3: "Auto-dim, high"}

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


def _f_to_c(fahrenheit: float) -> int:
    """A reading in whole degrees Celsius."""
    return round((fahrenheit - 32) * 5 / 9)


def _f_delta_to_c(degrees_f: int) -> int:
    """A difference (ramp step, calibration offset) in whole degrees Celsius.

    Truncated, as the fan itself converts: a 1 F ramp set on the Master
    Bedroom vent's own panel reads back as [1 F, 0 C] (2026-09-26).
    """
    return int(degrees_f * 5 / 9)


def _signed_byte(value: int) -> int:
    return value - 256 if value > 127 else value


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
    # Display register (OPCODE_DISPLAY). display_on stays None for a fan that
    # reports only a brightness byte: it has no backlight switch to drive.
    display_brightness: Optional[int] = None
    display_on: Optional[bool] = None
    # Settings registers, kept as the raw groups the fan reported so a write
    # can send back every byte it does not mean to change.
    display_celsius: Optional[bool] = None
    ramp: Optional[tuple[int, ...]] = None
    calibration: Optional[tuple[int, ...]] = None


@dataclass
class AutoModeConfig:
    """AUTO-mode trigger thresholds as read from/written to the device.

    Temperatures are whole degrees in both scales, as the device stores
    them: ``*_temp`` Celsius, ``*_temp_f`` Fahrenheit (None only for a config
    built before the Fahrenheit bytes were read). Humidity fields exist for
    all device types, but the AIRTAP type 6 has no humidity sensor — entity
    layers must not expose humidity thresholds for it.
    """

    high_temp_enabled: bool
    high_temp: int
    low_temp_enabled: bool
    low_temp: int
    high_humidity_enabled: bool
    high_humidity: int
    low_humidity_enabled: bool
    low_humidity: int
    high_temp_f: Optional[int] = None
    low_temp_f: Optional[int] = None


class ACInfinityDevice(ACInfinityController):
    """Controller with AUTO-mode support and optimistic local state."""

    _config_changed_since_last_update = False

    def __init__(
        self,
        ble_device: BLEDevice,
        state: DeviceInfoEx | None = None,
        advertisement_data: AdvertisementData | None = None,
        client_class: type = BleakClientWithServiceCache,
    ):
        super().__init__(
            ble_device=ble_device,
            state=state,
            advertisement_data=advertisement_data,
            client_class=client_class,
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

        Fetches work_type, the speed bounds, the AUTO threshold block, the
        timer/cycle durations and the display register, by walking the
        response's ``[opcode, length, value]`` groups (see
        ``parse_model_data``) rather than trusting fixed offsets — which is
        what makes the groups past the AUTO block readable at all.
        """
        await self._ensure_connected()
        try:
            _LOGGER.debug("%s: Updating model data", self.name)
            command = list(POLL_OPCODES)
            if self.state.type in FAMILY_E_MODELS:
                command += [255, 0]
            command = self._protocol._add_head(command, 1, self.sequence)
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
                        high_temp_f=thresholds[1],
                        low_temp_f=thresholds[3],
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
                    display = groups.get(OPCODE_DISPLAY)
                    if display:
                        self.state.display_brightness = display[0]
                        self.state.display_on = (
                            bool(display[1]) if len(display) >= 2 else None
                        )
                    unit = groups.get(OPCODE_UNIT)
                    if unit:
                        self.state.display_celsius = bool(unit[0] & 1)
                    for opcode, field in (
                        (OPCODE_RAMP, "ramp"),
                        (OPCODE_CALIBRATION, "calibration"),
                    ):
                        value = groups.get(opcode)
                        if value is not None and len(value) >= 3:
                            setattr(self.state, field, tuple(value))

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

    async def async_set_display(self, on: bool) -> None:
        """Turn the fan's display on or off, keeping its brightness gear.

        Sends the register in the only form the fan itself reported (see
        OPCODE_DISPLAY), so it refuses until a poll has read it back with a
        backlight byte.
        """
        if self.state.display_on is None or self.state.display_brightness is None:
            raise ValueError(
                "This fan has not reported a display switch; cannot change it"
            )
        _LOGGER.debug("%s: Setting display %s", self.name, "on" if on else "off")

        command = [OPCODE_DISPLAY, 2, self.state.display_brightness, 1 if on else 0]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)
        await self._ensure_connected()
        try:
            await self._send_command(command)

            self.state.display_on = on
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

    async def async_set_display_brightness(self, gear: int) -> None:
        """Change the display brightness gear, keeping the on/off byte."""
        if self.state.display_brightness is None:
            raise ValueError("This fan has not reported its display; cannot change it")
        if gear not in DISPLAY_BRIGHTNESS_GEARS:
            raise ValueError(f"Unknown brightness gear {gear:#x}")
        value = [gear] if self.state.display_on is None else [gear, int(self.state.display_on)]
        await self._async_write_register(OPCODE_DISPLAY, value)
        self.state.display_brightness = gear

    async def async_set_display_celsius(self, celsius: bool) -> None:
        """Show temperatures on the fan's display in Celsius or Fahrenheit."""
        if self.state.display_celsius is None:
            raise ValueError("This fan has not reported its display unit; cannot change it")
        await self._async_write_register(OPCODE_UNIT, [1 if celsius else 0])
        self.state.display_celsius = celsius

    async def async_set_ramp_f(self, degrees_f: int) -> None:
        """Set the AUTO ramp: how many degrees F past a trigger per speed step.

        0 jumps straight from the resting speed to the full speed. Only the
        temperature bytes change; the humidity byte goes back as read.
        """
        if self.state.ramp is None:
            raise ValueError("This fan has not reported its ramp setting; cannot change it")
        if not 0 <= degrees_f <= 20:
            raise ValueError("ramp must be between 0 and 20 degrees F")
        value = [degrees_f, _f_delta_to_c(degrees_f), *self.state.ramp[2:]]
        await self._async_write_register(OPCODE_RAMP, value)
        self.state.ramp = tuple(value)

    async def async_set_calibration_f(self, offset_f: int) -> None:
        """Offset the fan's temperature reading by whole degrees F."""
        if self.state.calibration is None:
            raise ValueError("This fan has not reported its calibration; cannot change it")
        if not -20 <= offset_f <= 20:
            raise ValueError("calibration must be between -20 and 20 degrees F")
        value = [
            offset_f & 0xFF,
            _f_delta_to_c(offset_f) & 0xFF,
            *self.state.calibration[2:],
        ]
        await self._async_write_register(OPCODE_CALIBRATION, value)
        self.state.calibration = tuple(value)

    @property
    def ramp_f(self) -> Optional[int]:
        return None if self.state.ramp is None else self.state.ramp[0]

    @property
    def calibration_f(self) -> Optional[int]:
        cal = self.state.calibration
        return None if cal is None else _signed_byte(cal[0])

    async def async_set_hot_trigger_f(self, value: float) -> None:
        """Air above this many degrees F speeds the fan up (AUTO high trigger)."""
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )
        f = round(value)
        new_config = dataclasses.replace(
            self.auto_mode, high_temp_f=f, high_temp=_f_to_c(f)
        )
        await self.async_set_auto_mode_config(new_config)

    async def async_set_cold_trigger_f(self, value: float) -> None:
        """Air below this many degrees F speeds the fan up (AUTO low trigger)."""
        if self.auto_mode is None:
            raise ValueError(
                "Auto mode configuration is not loaded; cannot change configuration values"
            )
        f = round(value)
        new_config = dataclasses.replace(
            self.auto_mode, low_temp_f=f, low_temp=_f_to_c(f)
        )
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

        def c_to_f(celsius: float) -> int:
            return round((celsius * 9.0 / 5.0) + 32.0)

        temp_hum_enabled_switches = byte_for_temp_hum_enabled_switches(config)
        # The device stores each trigger in both scales. Fahrenheit is kept
        # exactly as read or set (whole-Celsius steps would round a 65 F
        # trigger to 64 F); Celsius is derived only when no F value exists.
        high_temp_f = (
            config.high_temp_f if config.high_temp_f is not None else c_to_f(config.high_temp)
        )
        high_temp_c = config.high_temp
        low_temp_f = (
            config.low_temp_f if config.low_temp_f is not None else c_to_f(config.low_temp)
        )
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
