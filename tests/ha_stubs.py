"""Minimal Home Assistant stand-ins so the integration modules import under plain pytest.

WHY: this repo vendors its BLE library and targets HA 2026.9 (Python 3.13).
Installing a full Home Assistant just to unit-test parsing/guard logic is slow,
version-coupled, and unnecessary — every HA symbol the integration imports at
module level is either a constant, a tiny pure function, or a base class whose
behavior the tests do not rely on.  We register light replacements in
``sys.modules`` *before* ``custom_components`` is imported.

Rules for this file:
- If real Home Assistant is importable, we install NOTHING (see ``install()``),
  so the suite also runs unmodified inside a dev container that has HA.
- Pure functions whose math the tests depend on (``ranged_value_to_percentage``,
  ``percentage_to_ranged_value``, ``slugify``) are faithful copies of the HA
  implementations, because sensor/fan values flow through them.
- Everything else is the thinnest object that lets the module import and the
  code under test run.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from enum import Enum, IntFlag, StrEnum
from types import ModuleType


def _module(name: str) -> ModuleType:
    mod = ModuleType(name)
    sys.modules[name] = mod
    return mod


# --- faithful copies of homeassistant.util.percentage / slugify -------------
# These are load-bearing: FanSpeedSensor and ACInfinityFan values are computed
# through them, so the stubs must match HA's math exactly.

def _states_in_range(low_high_range: tuple[float, float]) -> float:
    return low_high_range[1] - low_high_range[0] + 1


def int_states_in_range(low_high_range: tuple[float, float]) -> int:
    return int(_states_in_range(low_high_range))


def ranged_value_to_percentage(
    low_high_range: tuple[float, float], value: float
) -> int:
    offset = low_high_range[0] - 1
    return int((value - offset) * 100 // _states_in_range(low_high_range))


def percentage_to_ranged_value(
    low_high_range: tuple[float, float], percentage: int
) -> float:
    offset = low_high_range[0] - 1
    return _states_in_range(low_high_range) * percentage / 100 + offset


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class _WriteStateRecorder:
    """Mixin for entity stubs: records async_write_ha_state calls.

    Real HA schedules a state-machine update; tests only need to know the
    entity *asked* for one (that is the optimistic-update contract).
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    @property
    def write_ha_state_calls(self) -> int:
        return getattr(self, "_write_ha_state_calls", 0)

    def async_write_ha_state(self) -> None:
        self._write_ha_state_calls = self.write_ha_state_calls + 1


def install() -> bool:
    """Install stubs unless real Home Assistant is available. Returns True if stubbed."""
    if "homeassistant" in sys.modules:
        return False
    if importlib.util.find_spec("homeassistant") is not None:
        return False

    ha = _module("homeassistant")

    # homeassistant.core
    core = _module("homeassistant.core")

    def callback(func):  # HA's @callback is only a scheduling marker
        return func

    class HomeAssistant:
        pass

    class CoreState(Enum):
        running = "RUNNING"
        not_running = "NOT_RUNNING"

    core.callback = callback
    core.HomeAssistant = HomeAssistant
    core.CoreState = CoreState

    # homeassistant.const
    const = _module("homeassistant.const")
    const.PERCENTAGE = "%"
    const.CONF_ADDRESS = "address"
    const.CONF_SERVICE_DATA = "service_data"

    class UnitOfTemperature(StrEnum):
        CELSIUS = "°C"
        FAHRENHEIT = "°F"

    class UnitOfPressure(StrEnum):
        KPA = "kPa"

    class Platform(StrEnum):
        FAN = "fan"
        NUMBER = "number"
        SENSOR = "sensor"
        SWITCH = "switch"

    class EntityCategory(StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    const.UnitOfTemperature = UnitOfTemperature
    const.UnitOfPressure = UnitOfPressure
    const.Platform = Platform
    const.EntityCategory = EntityCategory

    # homeassistant.exceptions
    exceptions = _module("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

    # homeassistant.data_entry_flow
    data_entry_flow = _module("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    # homeassistant.config_entries
    config_entries = _module("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigFlow:
        """Behavioral subset of HA's ConfigFlow used by the guard tests.

        Results are plain dicts shaped like HA FlowResults so tests can assert
        on type/reason without importing HA.
        """

        def __init_subclass__(cls, *, domain: str | None = None, **kwargs):
            super().__init_subclass__(**kwargs)
            cls._domain = domain

        # The integration's ConfigFlow defines its own __init__ without
        # calling super(); provide class-level defaults so instances work.
        hass = None
        context: dict = {}

        async def async_set_unique_id(self, unique_id, *, raise_on_progress=True):
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            pass

        def _async_current_ids(self):
            return set()

        def _async_in_progress(self):
            return []

        def async_abort(self, *, reason: str):
            return {"type": "abort", "reason": reason}

        def async_show_form(self, *, step_id: str, data_schema=None, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors,
            }

        def async_create_entry(self, *, title: str, data):
            return {"type": "create_entry", "title": title, "data": data}

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlowResult = dict  # HA 2024+ alias for FlowResult
    config_entries.ConfigFlow = ConfigFlow

    # homeassistant.components (+ bluetooth)
    components = _module("homeassistant.components")
    bluetooth = _module("homeassistant.components.bluetooth")
    components.bluetooth = bluetooth

    class BluetoothScanningMode(Enum):
        PASSIVE = "passive"
        ACTIVE = "active"

    class BluetoothChange(Enum):
        ADVERTISEMENT = 1

    class BluetoothServiceInfoBleak:
        pass

    def async_discovered_service_info(hass, connectable=True):
        return []

    def async_ble_device_from_address(hass, address, connectable=True):
        return None

    def async_last_service_info(hass, address, connectable=True):
        return None

    bluetooth.BluetoothScanningMode = BluetoothScanningMode
    bluetooth.BluetoothChange = BluetoothChange
    bluetooth.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak
    bluetooth.async_discovered_service_info = async_discovered_service_info
    bluetooth.async_ble_device_from_address = async_ble_device_from_address
    bluetooth.async_last_service_info = async_last_service_info

    active_update_coordinator = _module(
        "homeassistant.components.bluetooth.active_update_coordinator"
    )
    bluetooth.active_update_coordinator = active_update_coordinator

    class ActiveBluetoothDataUpdateCoordinator:
        """Constructor-compatible stub; tests drive entities directly."""

        def __init__(
            self,
            *,
            hass=None,
            logger=None,
            address=None,
            needs_poll_method=None,
            poll_method=None,
            mode=None,
            connectable=True,
            **kwargs,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.address = address
            self.available = True
            self.listener_update_count = 0
            # Test bookkeeping: the real base's event handler is the ONLY
            # place that notifies listeners / re-marks availability /
            # schedules polls, so tests pin that overrides always reach it.
            self.bluetooth_event_super_calls = 0
            self._async_start_calls = 0

        def __class_getitem__(cls, item):
            return cls

        def async_update_listeners(self) -> None:
            self.listener_update_count += 1

        def _async_handle_bluetooth_event(self, service_info, change) -> None:
            # Real base: notifies listeners and evaluates needs_poll per
            # dispatched event. Mirror the listener notification so tests
            # observe the same externally visible effect.
            self.bluetooth_event_super_calls += 1
            self.async_update_listeners()

        def _async_handle_unavailable(self, service_info) -> None:
            # Real base flips availability and notifies listeners when no
            # scanner has seen the address for the tracked interval.
            self.available = False
            self.async_update_listeners()

        def _async_start(self) -> None:
            self._async_start_calls += 1

        def _async_stop(self) -> None:
            pass

        def async_start(self):
            return lambda: None

    active_update_coordinator.ActiveBluetoothDataUpdateCoordinator = (
        ActiveBluetoothDataUpdateCoordinator
    )

    passive_update_coordinator = _module(
        "homeassistant.components.bluetooth.passive_update_coordinator"
    )
    bluetooth.passive_update_coordinator = passive_update_coordinator

    class PassiveBluetoothCoordinatorEntity(_WriteStateRecorder):
        """Entity base tied to a bluetooth coordinator (constructor shape only)."""

        def __init__(self, coordinator, context=None) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            # Faithful to HA: entity availability is the coordinator's.
            return self.coordinator.available

        def __class_getitem__(cls, item):
            return cls

        async def async_update(self) -> None:
            pass

        def _handle_coordinator_update(self) -> None:
            self.async_write_ha_state()

    passive_update_coordinator.PassiveBluetoothCoordinatorEntity = (
        PassiveBluetoothCoordinatorEntity
    )

    # homeassistant.components.sensor
    sensor = _module("homeassistant.components.sensor")
    components.sensor = sensor

    class SensorDeviceClass(StrEnum):
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"
        ATMOSPHERIC_PRESSURE = "atmospheric_pressure"

    class SensorStateClass(StrEnum):
        MEASUREMENT = "measurement"

    class SensorEntity(_WriteStateRecorder):
        _attr_native_value = None

        @property
        def native_value(self):
            return self._attr_native_value

    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    sensor.SensorEntity = SensorEntity

    # homeassistant.components.fan
    fan = _module("homeassistant.components.fan")
    components.fan = fan

    class FanEntityFeature(IntFlag):
        SET_SPEED = 1
        OSCILLATE = 2
        DIRECTION = 4
        PRESET_MODE = 8
        TURN_OFF = 16
        TURN_ON = 32

    class FanEntity(_WriteStateRecorder):
        _attr_is_on = None
        _attr_percentage = None
        _attr_preset_mode = None

        @property
        def is_on(self):
            return self._attr_is_on

        @property
        def percentage(self):
            return self._attr_percentage

        @property
        def preset_mode(self):
            return self._attr_preset_mode

    fan.FanEntityFeature = FanEntityFeature
    fan.FanEntity = FanEntity

    # homeassistant.components.switch / number — only needed if those platform
    # modules get imported; keep symmetry cheap.
    switch = _module("homeassistant.components.switch")
    components.switch = switch

    class SwitchEntity(_WriteStateRecorder):
        _attr_is_on = None


    class SwitchDeviceClass(StrEnum):
        OUTLET = "outlet"
        SWITCH = "switch"

    switch.SwitchDeviceClass = SwitchDeviceClass
    switch.SwitchEntity = SwitchEntity

    number = _module("homeassistant.components.number")
    components.number = number

    class NumberEntity(_WriteStateRecorder):
        _attr_native_value = None

    class NumberDeviceClass(StrEnum):
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"

    class NumberMode(StrEnum):
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    number.NumberEntity = NumberEntity
    number.NumberDeviceClass = NumberDeviceClass
    number.NumberMode = NumberMode

    # homeassistant.helpers
    helpers = _module("homeassistant.helpers")

    device_registry = _module("homeassistant.helpers.device_registry")
    helpers.device_registry = device_registry
    device_registry.CONNECTION_BLUETOOTH = "bluetooth"

    entity = _module("homeassistant.helpers.entity")
    helpers.entity = entity

    class DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    entity.DeviceInfo = DeviceInfo

    entity_platform = _module("homeassistant.helpers.entity_platform")
    helpers.entity_platform = entity_platform

    class AddEntitiesCallback:
        pass

    entity_platform.AddEntitiesCallback = AddEntitiesCallback

    update_coordinator = _module("homeassistant.helpers.update_coordinator")
    helpers.update_coordinator = update_coordinator

    class BaseCoordinatorEntity:
        def __init__(self, coordinator, context=None) -> None:
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls

        async def async_update(self) -> None:
            pass

        def _handle_coordinator_update(self) -> None:
            # Real HA writes entity state here; the recorder mixin captures it.
            self.async_write_ha_state()

    update_coordinator.BaseCoordinatorEntity = BaseCoordinatorEntity

    # homeassistant.util (+ percentage)
    util = _module("homeassistant.util")
    ha.util = util
    util.slugify = slugify

    percentage = _module("homeassistant.util.percentage")
    util.percentage = percentage
    percentage.int_states_in_range = int_states_in_range
    percentage.ranged_value_to_percentage = ranged_value_to_percentage
    percentage.percentage_to_ranged_value = percentage_to_ranged_value

    ha.core = core
    ha.const = const
    ha.exceptions = exceptions
    ha.config_entries = config_entries
    ha.components = components
    ha.helpers = helpers
    ha.data_entry_flow = data_entry_flow

    return True
