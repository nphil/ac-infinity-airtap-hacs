"""Integration-side wrapper around the vendored AC Infinity BLE controller.

Adds what the vendored library does not model: the AUTO work mode (mode 3),
its temperature/humidity threshold configuration, and min/max speed bounds.

HARDWARE SAFETY: every byte sequence sent from this module reuses command
builders/framing already present in the vendored library (``Protocol._add_head``
with command IDs 16-19 observed from the official app). Do not invent new
register writes here.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import Optional

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .ac_infinity_ble import ACInfinityController, DeviceInfo
from .ac_infinity_ble.const import CallbackType
from .ac_infinity_ble.util import get_bit
from .const import FAMILY_E_MODELS

# Work types (mode register values) with working command builders. The
# protocol enumerates modes 1-12 (see ac_infinity_ble/protocol.py get_mode),
# but only these three can currently be COMMANDED; the rest are read-only
# labels — documented as known gaps in the README.
WORK_TYPE_OFF = 1
WORK_TYPE_ON = 2
WORK_TYPE_AUTO = 3

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

        Fetches work_type, speed bounds and the AUTO-mode threshold block.
        Response layout mirrors the vendored ``update()`` for bytes 12-18 and
        extends it with the threshold block (bytes 21-27) observed from the
        official app's model-data response.
        """
        await self._ensure_connected()
        try:
            _LOGGER.debug("%s: Updating model data", self.name)
            command = self._protocol.get_model_data(self.state.type, 0, self.sequence)
            if data := await self._send_command(command):
                if len(data) < 28:
                    # A short frame is a truncated/foreign response; parsing
                    # it would poison work_type and the thresholds, so keep
                    # the previous state and let the next poll retry.
                    _LOGGER.debug(
                        "%s: Skipping update; data too short (%s): %s",
                        self.name,
                        len(data),
                        data.hex(),
                    )
                else:
                    self.state.work_type = data[12]
                    self.state.level_off = data[15]
                    self.state.level_on = data[18]

                    self.state.auto_mode = AutoModeConfig(
                        high_temp_enabled=not get_bit(data[21], 4),
                        low_temp_enabled=not get_bit(data[21], 5),
                        high_humidity_enabled=not get_bit(data[21], 6),
                        low_humidity_enabled=not get_bit(data[21], 7),
                        high_temp=data[23],
                        low_temp=data[25],
                        high_humidity=data[26],
                        low_humidity=data[27],
                    )

                    self._config_changed_since_last_update = False
                    self._fire_callbacks(CallbackType.UPDATE_RESPONSE)
        finally:
            # Free the proxy connection slot immediately instead of holding
            # it for the vendored DISCONNECT_DELAY; the disconnect is polite
            # (skipped while another operation holds the lock).
            await self._execute_disconnect()

    async def set_mode_auto(self) -> None:
        """Set the device's mode to automatic.

        Command [16, 1, work_type] is the same mode register used by the
        vendored ``set_level`` builder; 3 selects AUTO.
        """
        _LOGGER.debug("%s: Setting mode to auto", self.name)

        command = [16, 1, WORK_TYPE_AUTO]
        if self.state.type in FAMILY_E_MODELS:
            command += [255, 0]
        command = self._protocol._add_head(command, 3, self.sequence)
        await self._ensure_connected()
        try:
            await self._send_command(command)

            self.state.work_type = WORK_TYPE_AUTO
            self._config_changed_since_last_update = True
        finally:
            await self._execute_disconnect()

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
