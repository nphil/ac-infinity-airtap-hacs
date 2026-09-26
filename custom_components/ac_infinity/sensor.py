from __future__ import annotations

from habluetooth import HaBluetoothSlotAllocations, get_manager
from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorStateClass)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (PERCENTAGE, EntityCategory, UnitOfPressure,
                                 UnitOfTemperature)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify
from homeassistant.util.percentage import ranged_value_to_percentage

from .const import DEVICE_MODEL, DOMAIN, FAMILY_E_MODELS, MANUFACTURER
from .coordinator import (ACInfinityDataUpdateCoordinator,
                          ActiveBluetoothCoordinatorEntity,
                          async_holding_scanner_name)
from .device import ACInfinityDevice
from .fan import SPEED_RANGE
from .hold import connection_state
from .models import ACInfinityData

# How far a raw reading must move from the value already published before a new
# one is published. 0.8 comfortably exceeds the observed sample-to-sample
# wander (up to 0.9 degC, but almost always far less) without hiding a real
# change of a degree or more. See `quantize`.
QUANTIZE_DEADBAND = 0.8


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data: ACInfinityData = hass.data[DOMAIN][entry.entry_id]
    entities: list[ACInfinitySensor] = [
        TemperatureSensor(data.coordinator, data.device, "Temperature"),
        FanSpeedSensor(data.coordinator, data.device, "Fan Speed", "Speed"),
        ConnectionSensor(data.coordinator, data.device),
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
        translation_key: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._name = name
        # `name` is the unique_id seed and MUST stay stable - changing it orphans
        # every existing entity. `display_name` is what the UI shows, so a sensor
        # can read "<device> Speed" instead of "<device> Fan Speed" on a device
        # already called "... Vent Fan".
        if translation_key is not None:
            # Name comes from strings.json via the key. _attr_name must stay
            # UNSET: HA checks hasattr(self, "_attr_name") first, so setting
            # it to anything (including None) would defeat the translation.
            self._attr_translation_key = translation_key
        else:
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


def quantize(raw: float | None, published: float | None) -> float | None:
    """Publish whole units, holding the last value inside a deadband.

    These controllers re-advertise every ~2 s and their readings genuinely
    wander: measured on the live install 2026-09-18,
    sensor.isabel_s_office_vent_fan_temperature swung 1.7 degC inside ten
    minutes with consecutive samples up to 0.9 degC apart. It is a thermistor
    in the airflow of a running fan, so most of that is real - not decode
    noise - which is why the first attempt at this (rounding to 0.1 degC)
    changed nothing: measured before 27 rows/min, after 26-36 rows/min.

    Rounding ALONE cannot fix it at any grid size. A value wandering either
    side of a boundary crosses it repeatedly, so a quantized reading flaps
    between two neighbours and each flap is still a distinct state with its
    own recorder row. The deadband is the part that actually works: a new
    value is published only once the raw reading has moved far enough from
    what is already published that it cannot be boundary jitter.

    Cost: the published value can lag the true reading by up to DEADBAND.
    For room air off a vent that is well inside the sensor's own accuracy,
    and the raw value stays in `device.temperature`, so diagnostics keep it.
    """
    if raw is None:
        # Unknown: forget the published value so the next real reading is
        # treated as a first reading rather than deadbanded against a stale one.
        return None
    if published is None:
        return float(round(raw))
    if abs(raw - published) < QUANTIZE_DEADBAND:
        return published
    return float(round(raw))


class TemperatureSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Whole degrees: the deadband already holds the value steady to about a
    # degree, so decimals would only advertise precision that is not there.
    _attr_suggested_display_precision = 0

    @callback
    def _update_attrs(self) -> None:
        """Publish a deadbanded whole-degree temperature (see `quantize`)."""
        self._attr_native_value = quantize(
            self._device.temperature, self._attr_native_value
        )


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
        None is reserved for a speed the device has not reported since setup.

        Deliberately NOT gated on work_type: OFF mode on these devices is
        itself a level (level_off, the "off speed" — see the vendored
        update()/turn_off(), which model work_type 1 as fan = level_off), so
        forcing 0 whenever the mode is OFF would misreport blades genuinely
        spinning at a nonzero off speed. The level is the device's own
        report: its once-a-second notification while the link is held, its
        advertised byte otherwise. It reads 0 when the fan is truly stopped.
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
    _attr_suggested_display_precision = 0

    @callback
    def _update_attrs(self) -> None:
        """Publish a deadbanded whole-percent humidity (see `quantize`).

        The Airtap models on this install report no humidity at all (the
        entity sits at `unknown`), so this costs nothing here; it is
        deadbanded so a controller that *does* report it cannot reproduce the
        recorder flood the temperature sensors caused. A deadband of 0.8 %RH
        is far inside these sensors' accuracy.
        """
        self._attr_native_value = quantize(
            self._device.humidity, self._attr_native_value
        )


class VpdSensor(ACInfinitySensor):
    _attr_native_unit_of_measurement = UnitOfPressure.KPA
    _attr_device_class = SensorDeviceClass.ATMOSPHERIC_PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        self._attr_native_value = self._device.vpd


class ConnectionSensor(ACInfinitySensor):
    """Which Bluetooth proxy currently carries this fan's GATT link.

    Enabled by default despite being diagnostic: heal automations read it to
    decide which ESPHome proxy is safe to restart (restarting one that is
    holding other devices' links costs every one of them a reconnect).

    State is the scanner's friendly name (e.g. ``plant-room-bluetooth-proxy``)
    while a link is held through it, otherwise ``disconnected``.  The
    allocation table is habluetooth's, the same one core's
    ``bluetooth/subscribe_connection_allocations`` websocket serves.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(
        self,
        coordinator: ACInfinityDataUpdateCoordinator,
        device: ACInfinityDevice,
    ) -> None:
        super().__init__(
            coordinator, device, "Connection", translation_key="connection"
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to the two things that move this sensor."""
        await super().async_added_to_hass()
        # Allocation changes cover connect/disconnect/roam across every
        # proxy; the hold status covers drop counts and the reconnect
        # ladder, which no bluetooth event reports.
        self.async_on_remove(
            get_manager().async_register_allocation_callback(
                self._async_allocations_changed, None
            )
        )
        self.async_on_remove(
            self._device.hold_status.add_listener(self._async_hold_status_changed)
        )
        self._update_attrs()

    @callback
    def _async_allocations_changed(
        self, allocations: HaBluetoothSlotAllocations
    ) -> None:
        """Handle a proxy reporting a change to its connection slots."""
        self._update_attrs()
        self.async_write_ha_state()

    @callback
    def _async_hold_status_changed(self) -> None:
        """Handle a drop, a reconnect attempt, or the hold being toggled."""
        self._update_attrs()
        self.async_write_ha_state()

    @callback
    def _update_attrs(self) -> None:
        """Handle updating _attr values."""
        connected = self._device.is_connected
        self._attr_native_value = connection_state(
            connected=connected,
            # Only pay for the allocation lookup when there is a link to
            # name: this also runs on every advertisement, for every fan.
            scanner_name=(
                async_holding_scanner_name(self.hass, self._device.address)
                if connected
                else None
            ),
        )
        self._attr_extra_state_attributes = self._device.hold_status.as_attributes()
