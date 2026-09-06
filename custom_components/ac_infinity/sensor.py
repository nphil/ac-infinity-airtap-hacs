from __future__ import annotations

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPressure, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify
from homeassistant.util.percentage import ranged_value_to_percentage

from .const import DEVICE_MODEL, DOMAIN, FAMILY_E_MODELS, MANUFACTURER
from .coordinator import ACInfinityDataUpdateCoordinator, ActiveBluetoothCoordinatorEntity
from .device import ACInfinityDevice
from .fan import SPEED_RANGE
from .models import ACInfinityData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TemperatureSensor(data.coordinator, data.device, "Temperature"),
        FanSpeedSensor(data.coordinator, data.device, "Fan Speed", "Speed"),
    ]

    if data.device.state.type not in [6]:  # Airtap does not have humidity
        entities.append(HumiditySensor(data.coordinator, data.device, "Humidity"))

    if data.device.state.version >= 3 and data.device.state.type in FAMILY_E_MODELS:
        entities.append(VpdSensor(data.coordinator, data.device, "VPD"))
    async_add_entities(entities)


class ACInfinitySensor(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], SensorEntity
):
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
        self._name = name
        # `name` is the unique_id seed and MUST stay stable - changing it orphans
        # every existing entity. `display_name` is what the UI shows, so a sensor
        # can read "<device> Speed" instead of "<device> Fan Speed" on a device
        # already called "... Vent Fan".
        self._attr_name = display_name or name
        self._attr_unique_id = f"{self._device.address}_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=DEVICE_MODEL.get(device.state.type, "Controller"),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        raise NotImplementedError("Not yet implemented.")

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()


class TemperatureSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.temperature


class FanSpeedSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:fan"
    # Disabled by default: this duplicates fan.percentage and exists only for
    # history/statistics users (the live install has all six disabled by user
    # choice). Registry remembers prior enable/disable choices, so existing
    # installs are untouched; only fresh registrations start disabled.
    _attr_entity_registry_enabled_default = False

    @callback
    def _update_attrs(self) -> None:
        """Report the fan's actual current speed.

        A stopped fan is 0% — never a retained last-known running speed (the
        old _last_speed cache made a stopped fan read 80%, verified live).
        None is reserved for a speed the device has never reported at all.

        Deliberately NOT gated on work_type: OFF mode on these devices is
        itself a level (level_off, the "off speed" — see the vendored
        update()/turn_off(), which model work_type 1 as fan = level_off), so
        forcing 0 whenever the mode is OFF would misreport blades genuinely
        spinning at a nonzero off speed. The advertised fan byte is the
        device's own report of the current level, refreshed every few
        seconds, and reads 0 when the fan is truly stopped.
        """
        fan_speed = self._device.state.fan
        if fan_speed is None:
            self._attr_native_value = None
        elif fan_speed == 0:
            # Explicit zero path: off/stopped must read 0, not unknown and
            # not any converted/retained value.
            self._attr_native_value = 0
        else:
            self._attr_native_value = ranged_value_to_percentage(
                SPEED_RANGE, fan_speed
            )


class HumiditySensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.humidity


class VpdSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = UnitOfPressure.KPA
    _attr_device_class = SensorDeviceClass.ATMOSPHERIC_PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.vpd
