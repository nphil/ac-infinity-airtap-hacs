from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify
from homeassistant.util.percentage import (int_states_in_range,
                                           percentage_to_ranged_value,
                                           ranged_value_to_percentage)

from .const import DEVICE_MODEL, DOMAIN, MANUFACTURER
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity)
from .device import (WORK_TYPE_AUTO, WORK_TYPE_CYCLE, WORK_TYPE_TIMER_TO_OFF,
                     WORK_TYPE_TIMER_TO_ON, ACInfinityDevice)
from .models import ACInfinityData

SPEED_RANGE = (1, 10)

PRESET_AUTO_MODE = "Auto"
PRESET_TIMER_TO_ON = "Timer to On"
PRESET_TIMER_TO_OFF = "Timer to Off"
PRESET_CYCLE = "Cycle"

# The device's own mode names, in the order its control panel cycles them.
# Each is a mode the fan runs by itself off a configuration register; the
# durations live on the matching number entities.
PRESET_WORK_TYPES = {
    PRESET_AUTO_MODE: WORK_TYPE_AUTO,
    PRESET_TIMER_TO_ON: WORK_TYPE_TIMER_TO_ON,
    PRESET_TIMER_TO_OFF: WORK_TYPE_TIMER_TO_OFF,
    PRESET_CYCLE: WORK_TYPE_CYCLE,
}
WORK_TYPE_PRESETS = {mode: preset for preset, mode in PRESET_WORK_TYPES.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ACInfinityFan(data.coordinator, data.device, "Fan")])


class ACInfinityFan(
    ActiveBluetoothCoordinatorEntity[ACInfinityDataUpdateCoordinator], FanEntity
):
    _attr_has_entity_name = True
    # The fan IS the device, so it carries no name of its own: with
    # has_entity_name this makes it inherit the device name verbatim
    # ("Living Room Vent Fan") instead of appending a second "Fan" to it.
    _attr_name = None
    _attr_speed_count = int_states_in_range(SPEED_RANGE)
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.PRESET_MODE
    )
    _attr_preset_modes = list(PRESET_WORK_TYPES)

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._last_speed = 1
        # `name` survives only as the unique_id seed (renaming it would orphan
        # every existing entity); the displayed name comes from the device.
        self._attr_unique_id = f"{self._device.address}_{slugify(name)}"
        self._attr_device_info = DeviceInfo(
            name=device.name,
            model=DEVICE_MODEL.get(device.state.type, "Controller"),
            manufacturer=MANUFACTURER,
            sw_version=str(device.state.version),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan, as a percentage."""
        speed = 0
        if percentage > 0:
            speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        if speed > 0:
            self._last_speed = speed
        self._attr_is_on = speed > 0
        self._attr_percentage = percentage
        # Setting a manual speed drops the device out of Auto (work_type 3 -> 2/1).
        # That is correct HA fan semantics, but it must be visible immediately:
        # without clearing the preset here the UI kept claiming "Auto" while the
        # fan was already running manually (verified live on real hardware).
        self._attr_preset_mode = None
        self.async_write_ha_state()
        await self._device.set_speed(speed)
        self.coordinator.async_update_listeners()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
            return
        speed = None
        if percentage is not None:
            speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        self._attr_is_on = True
        if speed is not None and speed > 0:
            self._last_speed = speed
            self._attr_percentage = ranged_value_to_percentage(SPEED_RANGE, speed)
        # turn_on drives work_type 2 (manual); clear any stale Auto preset so
        # the UI never claims Auto while the device runs manually.
        self._attr_preset_mode = None
        self.async_write_ha_state()
        await self._device.turn_on(speed)
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._attr_percentage = 0
        self.async_write_ha_state()
        await self._device.turn_off()
        self.coordinator.async_update_listeners()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch the device into one of its self-running modes."""
        work_type = PRESET_WORK_TYPES.get(preset_mode)
        if work_type is None:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        await self._device.async_set_work_type(work_type)
        # Optimistic state, mirroring async_set_percentage. The write has
        # already flipped state.work_type on success, but the next
        # advertisement/poll can be minutes away over congested ESPHome
        # proxies (verified live: HA showed the stale preset 8+ minutes after
        # a successful BLE mode change). Written after the await so a failed
        # BLE write raises without falsely claiming the mode changed.
        self._attr_preset_mode = preset_mode
        # A preset mode is the device driving itself — including TIMER TO ON,
        # where it is waiting at zero. Reporting the fan as off there would
        # invite an automation to "turn it on" and cancel the countdown.
        self._attr_is_on = True
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        preset = WORK_TYPE_PRESETS.get(self._device.state.work_type)
        if preset is not None:
            self._attr_is_on = True
            self._attr_preset_mode = preset
        else:
            self._attr_is_on = self._device.is_on
            self._attr_preset_mode = None
        self._attr_percentage = ranged_value_to_percentage(
            SPEED_RANGE, self._device.state.fan
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()
