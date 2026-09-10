from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Optional

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (PERCENTAGE, EntityCategory, UnitOfTemperature,
                                 UnitOfTime)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify
from homeassistant.util.percentage import (percentage_to_ranged_value,
                                           ranged_value_to_percentage)

from .const import DEVICE_MODEL, DOMAIN, MANUFACTURER
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity)
from .device import MAX_DURATION_SECONDS, ACInfinityDevice
from .fan import SPEED_RANGE
from .models import ACInfinityData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    # Deliberately NO auto-mode humidity threshold numbers: AutoModeConfig
    # carries high/low humidity fields, but the AIRTAP T-series (type 6) this
    # fork targets has no humidity sensor (hum is always 0.0), so humidity
    # knobs would configure a trigger the device can never evaluate. Only
    # temperature thresholds and speed limits are exposed.
    entities: list[ACInfinityNumber] = [
        PercentageNumber(data.coordinator,
                         data.device,
                         "Min Speed",
                         lambda d: d.min_speed,
                         ACInfinityDevice.async_set_min_speed),
        PercentageNumber(data.coordinator,
                         data.device,
                         "Max Speed",
                         lambda d: d.max_speed,
                         ACInfinityDevice.async_set_max_speed),
        TemperatureNumber(data.coordinator,
                          data.device,
                          "Auto Mode High Temperature",
                          lambda d: None if d.auto_mode is None else d.auto_mode.high_temp,
                          ACInfinityDevice.async_set_auto_high_temp),
        TemperatureNumber(data.coordinator,
                          data.device,
                          "Auto Mode Low Temperature",
                          lambda d: None if d.auto_mode is None else d.auto_mode.low_temp,
                          ACInfinityDevice.async_set_auto_low_temp),
        DurationNumber(data.coordinator,
                       data.device,
                       "Timer to On",
                       lambda d: d.timer_to_on,
                       ACInfinityDevice.async_set_timer_to_on),
        DurationNumber(data.coordinator,
                       data.device,
                       "Timer to Off",
                       lambda d: d.timer_to_off,
                       ACInfinityDevice.async_set_timer_to_off),
        DurationNumber(data.coordinator,
                       data.device,
                       "Cycle On",
                       lambda d: d.cycle_on,
                       ACInfinityDevice.async_set_cycle_on),
        DurationNumber(data.coordinator,
                       data.device,
                       "Cycle Off",
                       lambda d: d.cycle_off,
                       ACInfinityDevice.async_set_cycle_off),
    ]

    async_add_entities(entities)


class ACInfinityNumber(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], NumberEntity
):
    _attr_entity_category = (
        EntityCategory.CONFIG
    )
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_name = name
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


class PercentageNumber(ACInfinityNumber):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 10.0

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
        value = self._get_value(self._device)
        self._attr_native_value = None if value is None else ranged_value_to_percentage(SPEED_RANGE, value)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_value(self._device, math.ceil(percentage_to_ranged_value(SPEED_RANGE, value)))


class TemperatureNumber(ACInfinityNumber):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_min_value = 0.0
    _attr_native_max_value = 90.0
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
        get_value: Callable[[ACInfinityDevice], Optional[float]],
        async_set_value: Callable[[ACInfinityDevice, float], Awaitable[None]],
    ) -> None:
        self._get_value = get_value
        self._async_set_value = async_set_value
        super().__init__(coordinator, device, name)

    @callback
    def _update_attrs(self) -> None:
        self._attr_native_value = self._get_value(self._device)

    async def async_set_native_value(self, value: float) -> None:
        await self._async_set_value(self._device, value)


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
