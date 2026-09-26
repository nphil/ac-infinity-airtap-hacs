"""Number entities: the settings that shape how a fan responds to air.

Read together they describe one curve. In AUTO the fan sits at its minimum
while the air is room temperature; once the air falls below "Cold air
below" (or rises above "Hot air above") it steps up one speed per "Ramp"
degrees until it reaches "Full speed". The minimum is "Rest speed", or
"Circulation speed" while the HVAC blower runs (circulation.py).

Speeds are the fan's own 0-10 levels, as its panel shows them.
Temperatures are whole degrees Fahrenheit because that is the precision the
fan stores them at: a Celsius setting can only land on 1.8 F steps.

unique_ids keep their original seeds (``Min Speed``, ``Auto Mode Low
Temperature``, ...): changing a seed would orphan the entity. Only what is
displayed changed.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Optional

from homeassistant.components.number import (NumberDeviceClass, NumberEntity,
                                             NumberMode)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .circulation import VentSettings
from .const import (CONF_CIRCULATION_HOLD, CONF_CIRCULATION_SPEED,
                    CONF_REST_SPEED, DEVICE_MODEL, DOMAIN, MANUFACTURER)
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity)
from .device import MAX_DURATION_SECONDS, ACInfinityDevice
from .models import ACInfinityData

# The temperature unit of a difference: HA's temperature conversion would
# shift a 2-degree step by 32, so steps and offsets carry a bare unit.
DEGREES_F = "°F"


def _trigger_f(celsius: Optional[int], fahrenheit: Optional[int]) -> Optional[int]:
    if fahrenheit is not None:
        return fahrenheit
    return None if celsius is None else round(celsius * 9 / 5 + 32)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    c, d, s = data.coordinator, data.device, data.settings
    # Deliberately NO auto-mode humidity threshold numbers: AutoModeConfig
    # carries high/low humidity fields, but the AIRTAP T-series (type 6) this
    # fork targets has no humidity sensor (hum is always 0.0), so humidity
    # knobs would configure a trigger the device can never evaluate.
    entities: list[ACInfinityNumber] = [
        SettingNumber(c, d, s, "Min Speed", "Rest speed", CONF_REST_SPEED,
                      fallback=lambda dev: dev.min_speed),
        SettingNumber(c, d, s, "Circulation Speed", "Circulation speed",
                      CONF_CIRCULATION_SPEED),
        CirculationHoldNumber(c, d, s),
        LevelNumber(c, d, "Max Speed", "Full speed",
                    lambda dev: dev.max_speed,
                    ACInfinityDevice.async_set_max_speed),
        TriggerNumber(c, d, "Auto Mode Low Temperature", "Cold air below",
                      lambda dev: None if dev.auto_mode is None
                      else _trigger_f(dev.auto_mode.low_temp, dev.auto_mode.low_temp_f),
                      ACInfinityDevice.async_set_cold_trigger_f),
        TriggerNumber(c, d, "Auto Mode High Temperature", "Hot air above",
                      lambda dev: None if dev.auto_mode is None
                      else _trigger_f(dev.auto_mode.high_temp, dev.auto_mode.high_temp_f),
                      ACInfinityDevice.async_set_hot_trigger_f),
        DegreesNumber(c, d, "Ramp", "Ramp", 0, 10,
                      lambda dev: dev.ramp_f, ACInfinityDevice.async_set_ramp_f),
        DegreesNumber(c, d, "Temperature Calibration", "Temperature calibration",
                      -10, 10, lambda dev: dev.calibration_f,
                      ACInfinityDevice.async_set_calibration_f),
        DurationNumber(c, d, "Timer to On", lambda dev: dev.timer_to_on,
                       ACInfinityDevice.async_set_timer_to_on),
        DurationNumber(c, d, "Timer to Off", lambda dev: dev.timer_to_off,
                       ACInfinityDevice.async_set_timer_to_off),
        DurationNumber(c, d, "Cycle On", lambda dev: dev.cycle_on,
                       ACInfinityDevice.async_set_cycle_on),
        DurationNumber(c, d, "Cycle Off", lambda dev: dev.cycle_off,
                       ACInfinityDevice.async_set_cycle_off),
    ]
    async_add_entities(entities)


class ACInfinityNumber(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], NumberEntity
):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
        display_name: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        # `name` seeds the unique_id and MUST stay stable; see module docstring.
        self._attr_name = display_name or name
        self._attr_unique_id = f"{self._device.address}_number_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=DEVICE_MODEL.get(device.state.type, "Controller"),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )

    @callback
    def _update_attrs(self) -> None:
        raise NotImplementedError("Not yet implemented.")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()


class DeviceNumber(ACInfinityNumber):
    """A value read from and written to one of the fan's registers."""

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
        display_name: str | None,
        get_value: Callable[[ACInfinityDevice], Optional[float]],
        async_set_value: Callable[[ACInfinityDevice, int], Awaitable[None]],
    ) -> None:
        self._get_value = get_value
        self._async_set_value = async_set_value
        super().__init__(coordinator, device, name, display_name)

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._get_value(self._device)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_value(self._device, round(value))
        self.coordinator.async_update_listeners()


class LevelNumber(DeviceNumber):
    """A fan speed, 0-10 as on the fan's panel."""

    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER


class TriggerNumber(DeviceNumber):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT
    _attr_native_min_value = 32
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX


class DegreesNumber(DeviceNumber):
    """A temperature difference in whole degrees F (a step or an offset)."""

    _attr_native_unit_of_measurement = DEGREES_F
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
        display_name: str,
        minimum: int,
        maximum: int,
        get_value: Callable[[ACInfinityDevice], Optional[int]],
        async_set_value: Callable[[ACInfinityDevice, int], Awaitable[None]],
    ) -> None:
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        super().__init__(coordinator, device, name, display_name, get_value, async_set_value)


class SettingNumber(ACInfinityNumber):
    """A speed kept by Home Assistant and applied by circulation.py."""

    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        settings: VentSettings,
        name: str,
        display_name: str,
        key: str,
        fallback: Callable[[ACInfinityDevice], Optional[int]] | None = None,
    ) -> None:
        self._settings = settings
        self._key = key
        self._fallback = fallback
        super().__init__(coordinator, device, name, display_name)

    @callback
    def _update_attrs(self) -> None:
        value = getattr(self._settings, self._key)
        if value is None and self._fallback is not None:
            # Not set from Home Assistant yet: show what the fan has.
            value = self._fallback(self._device)
        self._attr_native_value = value

    async def async_set_native_value(self, value: float) -> None:
        self._settings.async_set(self._key, round(value))


class CirculationHoldNumber(ACInfinityNumber):
    """Minutes the circulation speed outlasts the blower (circulation.py)."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = 5
    _attr_native_max_value = 60
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        settings: VentSettings,
    ) -> None:
        self._settings = settings
        super().__init__(coordinator, device, "Circulation Hold", "Circulation hold")

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._settings.circulation_hold

    async def async_set_native_value(self, value: float) -> None:
        self._settings.async_set(CONF_CIRCULATION_HOLD, round(value))


class DurationNumber(ACInfinityNumber):
    """A timer/cycle duration, shown in minutes and written in seconds.

    Minutes, not seconds: the fan's own panel and the vendor app both set
    these as hours:minutes, and a 1439-step slider is usable where an
    86340-step one is not. The register itself is seconds, so the conversion
    lives here and the device layer keeps the hardware's own unit.
    """

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_min_value = 0.0
    _attr_native_max_value = MAX_DURATION_SECONDS / 60
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
        get_value: Callable[[ACInfinityDevice], Optional[int]],
        async_set_value: Callable[[ACInfinityDevice, int], Awaitable[None]],
    ) -> None:
        self._get_value = get_value
        self._async_set_value = async_set_value
        super().__init__(coordinator, device, name)

    @callback
    def _update_attrs(self) -> None:
        seconds = self._get_value(self._device)
        self._attr_native_value = None if seconds is None else seconds / 60

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_value(self._device, round(value * 60))
