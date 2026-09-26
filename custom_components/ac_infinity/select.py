"""Select entities for the fan's display: brightness gear and unit.

Both registers are read with every poll (device.POLL_OPCODES) and written
only once the fan has reported them, so each select reads unknown, and
refuses changes, on a fan that does not answer for its register.
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DEVICE_MODEL, DOMAIN, MANUFACTURER
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity)
from .device import DISPLAY_BRIGHTNESS_GEARS, ACInfinityDevice
from .models import ACInfinityData

UNIT_OPTIONS = {False: "°F", True: "°C"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DisplayBrightnessSelect(data.coordinator, data.device, "Display Brightness"),
            DisplayUnitSelect(data.coordinator, data.device, "Display Units"),
        ]
    )


class ACInfinitySelect(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], SelectEntity
):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._attr_name = name[0] + name[1:].lower()
        self._attr_unique_id = f"{device.address}_select_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=DEVICE_MODEL.get(device.state.type, "Controller"),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )

    @callback
    def _update_attrs(self) -> None:
        raise NotImplementedError

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_attrs()
        super()._handle_coordinator_update()


class DisplayBrightnessSelect(ACInfinitySelect):
    _attr_icon = "mdi:brightness-6"
    _attr_options = list(DISPLAY_BRIGHTNESS_GEARS.values())

    @callback
    def _update_attrs(self) -> None:
        self._attr_current_option = DISPLAY_BRIGHTNESS_GEARS.get(
            self._device.state.display_brightness
        )

    async def async_select_option(self, option: str) -> None:
        gear = next(g for g, label in DISPLAY_BRIGHTNESS_GEARS.items() if label == option)
        await self._device.async_set_display_brightness(gear)
        self.coordinator.async_update_listeners()


class DisplayUnitSelect(ACInfinitySelect):
    _attr_icon = "mdi:temperature-fahrenheit"
    _attr_options = list(UNIT_OPTIONS.values())

    @callback
    def _update_attrs(self) -> None:
        celsius = self._device.state.display_celsius
        self._attr_current_option = None if celsius is None else UNIT_OPTIONS[celsius]

    async def async_select_option(self, option: str) -> None:
        await self._device.async_set_display_celsius(option == UNIT_OPTIONS[True])
        self.coordinator.async_update_listeners()
