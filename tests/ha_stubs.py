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
from collections.abc import Callable
from dataclasses import dataclass
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

    Real entities need a hass/platform to write state; the integration's
    entities are exercised directly in tests, so the recorder stands in for
    the state machine and lets tests assert that a state write happened.
    """

    _write_ha_state_calls = 0
    _attr_unique_id = None
    _attr_entity_category = None
    _attr_translation_key = None
    _attr_icon = None
    hass = None

    # Faithful to homeassistant.helpers.entity.Entity: each of these is a
    # property over the matching _attr_. Note that `name` is deliberately
    # absent — HA resolves it through hasattr(self, "_attr_name"), which is
    # the mechanism the Connection sensor relies on to be named by its
    # translation key.
    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def entity_category(self):
        return self._attr_entity_category

    @property
    def translation_key(self):
        return self._attr_translation_key

    @property
    def icon(self):
        return self._attr_icon
    hass = None

    @property
    def write_ha_state_calls(self) -> int:
        return self._write_ha_state_calls

    def async_write_ha_state(self) -> None:
        self._write_ha_state_calls = self.write_ha_state_calls + 1

    def async_on_remove(self, func) -> None:
        self._on_remove = getattr(self, "_on_remove", [])
        self._on_remove.append(func)

    async def async_added_to_hass(self) -> None:
        """Real HA calls this when the entity is registered."""


def install() -> bool:
    """Install stubs unless real Home Assistant is available. Returns True if stubbed."""
    if "homeassistant" in sys.modules:
        return False
    if importlib.util.find_spec("homeassistant") is not None:
        return False

    # habluetooth: the connection-slot allocation table the Connection
    # diagnostic sensor reads. Shipped with Home Assistant at runtime, so it
    # is stubbed under the same "no real HA installed" condition.
    habluetooth = _module("habluetooth")

    @dataclass
    class HaBluetoothSlotAllocations:
        source: str
        slots: int
        free: int
        allocated: list[str]

    class _StubManager:
        """Reports no allocations and no subscribers; tests inject their own."""

        def async_current_allocations(self, source=None):
            return []

        def async_register_allocation_callback(self, callback, source=None):
            return lambda: None

    _stub_manager = _StubManager()

    habluetooth.HaBluetoothSlotAllocations = HaBluetoothSlotAllocations
    habluetooth.get_manager = lambda: _stub_manager

    ha = _module("homeassistant")

    # homeassistant.core
    core = _module("homeassistant.core")

    def callback(func):  # HA's @callback is only a scheduling marker
        return func

    class HomeAssistant:
        pass

    class ServiceCall:
        """Only what the release_link handler reads: `data`."""

        def __init__(self, data=None):
            self.data = data or {}

    class CoreState(Enum):
        running = "RUNNING"
        not_running = "NOT_RUNNING"

    core.callback = callback
    core.HomeAssistant = HomeAssistant
    core.ServiceCall = ServiceCall
    core.CoreState = CoreState
    core.CALLBACK_TYPE = Callable[[], None]

    # homeassistant.const
    const = _module("homeassistant.const")
    const.PERCENTAGE = "%"
    const.ATTR_ENTITY_ID = "entity_id"
    const.CONF_ADDRESS = "address"
    const.CONF_SERVICE_DATA = "service_data"
    const.SERVICE_TURN_OFF = "turn_off"
    const.SERVICE_TURN_ON = "turn_on"

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

    class ConfigEntryState(Enum):
        """Subset of HA's entry states the repair wizard branches on."""

        NOT_LOADED = "not_loaded"
        LOADED = "loaded"
        SETUP_ERROR = "setup_error"
        SETUP_RETRY = "setup_retry"

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

    class OptionsFlow:
        """Behavioral subset of HA's OptionsFlow.

        ``config_entry`` is injected by HA on the real class; tests assign it
        directly, which is exactly how the handler consumes it.
        """

        config_entry = None

        def async_show_form(self, *, step_id: str, data_schema=None, errors=None):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors,
            }

        def async_create_entry(self, *, title: str | None = None, data=None):
            return {"type": "create_entry", "title": title, "data": data}

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigFlowResult = dict  # HA 2024+ alias for FlowResult
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow

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

    def async_scanner_by_source(hass, source):
        return None

    bluetooth.BluetoothScanningMode = BluetoothScanningMode
    bluetooth.BluetoothChange = BluetoothChange
    bluetooth.BluetoothServiceInfoBleak = BluetoothServiceInfoBleak
    bluetooth.async_discovered_service_info = async_discovered_service_info
    bluetooth.async_ble_device_from_address = async_ble_device_from_address
    bluetooth.async_last_service_info = async_last_service_info
    bluetooth.async_scanner_by_source = async_scanner_by_source

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
            # Faithful to HA: `available` is a read-only property over
            # `_available`, which is what lets a subclass widen it.
            self._available = True
            self.listener_update_count = 0
            # Test bookkeeping: the real base's event handler is the ONLY
            # place that notifies listeners / re-marks availability /
            # schedules polls, so tests pin that overrides always reach it.
            self.bluetooth_event_super_calls = 0
            self._async_start_calls = 0

        def __class_getitem__(cls, item):
            return cls

        @property
        def available(self) -> bool:
            return self._available

        def async_update_listeners(self) -> None:
            self.listener_update_count += 1

        def _async_handle_bluetooth_event(self, service_info, change) -> None:
            # Real base (bluetooth/passive_update_coordinator.py): marks the
            # device available again, notifies listeners and evaluates
            # needs_poll per dispatched event. Mirror the first two so tests
            # observe the same externally visible effects.
            self.bluetooth_event_super_calls += 1
            self._available = True
            self.async_update_listeners()

        def _async_handle_unavailable(self, service_info) -> None:
            # Real base flips availability and notifies listeners when no
            # scanner has seen the address for the tracked interval.
            self._available = False
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
        _attr_extra_state_attributes = None

        @property
        def native_value(self):
            return self._attr_native_value

        @property
        def extra_state_attributes(self):
            return self._attr_extra_state_attributes

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

    # homeassistant.components.repairs
    repairs = _module("homeassistant.components.repairs")
    components.repairs = repairs

    class RepairsFlow:
        """Behavioral subset of HA's RepairsFlow.

        Same approach as the ConfigFlow stub above: every helper returns a
        plain dict shaped like the real FlowResult, so the wizard's own
        branching runs unchanged and tests assert on type/step_id/
        menu_options rather than on HA internals.  ``hass`` is injected by
        HA's flow manager on the real class; tests assign it directly.
        """

        hass = None
        issue_id = None
        data = None

        def async_show_form(
            self,
            *,
            step_id: str | None = None,
            data_schema=None,
            errors=None,
            description_placeholders=None,
            last_step=None,
        ):
            return {
                "type": "form",
                "step_id": step_id,
                "data_schema": data_schema,
                "errors": errors,
                "description_placeholders": description_placeholders,
            }

        def async_show_menu(
            self,
            *,
            step_id: str | None = None,
            menu_options=None,
            sort: bool = False,
            description_placeholders=None,
        ):
            return {
                "type": "menu",
                "step_id": step_id,
                "menu_options": menu_options,
                "description_placeholders": description_placeholders,
            }

        def async_create_entry(
            self,
            *,
            title: str | None = None,
            data=None,
            description=None,
            description_placeholders=None,
        ):
            return {"type": "create_entry", "title": title, "data": data}

        def async_abort(
            self,
            *,
            reason: str,
            description_placeholders=None,
            translation_domain=None,
        ):
            return {"type": "abort", "reason": reason}

    repairs.RepairsFlow = RepairsFlow

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

    # homeassistant.helpers.issue_registry — behavioral, not a no-op: the
    # repair issue's presence/absence IS what the watchdog tests assert on,
    # so the stub keeps a real in-memory registry with HA's own semantics
    # (create replaces, delete of a missing issue is not an error) under
    # HA's own hass.data key.
    issue_registry = _module("homeassistant.helpers.issue_registry")
    helpers.issue_registry = issue_registry

    class IssueSeverity(StrEnum):
        CRITICAL = "critical"
        ERROR = "error"
        WARNING = "warning"

    class IssueRegistry:
        def __init__(self) -> None:
            self.issues: dict[tuple[str, str], dict] = {}

        def async_get_issue(self, domain: str, issue_id: str):
            return self.issues.get((domain, issue_id))

    def issue_registry_async_get(hass) -> IssueRegistry:
        return hass.data.setdefault("issue_registry", IssueRegistry())

    def async_create_issue(hass, domain, issue_id, **kwargs) -> None:
        issue_registry_async_get(hass).issues[(domain, issue_id)] = {
            "domain": domain,
            "issue_id": issue_id,
            **kwargs,
        }

    def async_delete_issue(hass, domain, issue_id) -> None:
        issue_registry_async_get(hass).issues.pop((domain, issue_id), None)

    issue_registry.IssueSeverity = IssueSeverity
    issue_registry.IssueRegistry = IssueRegistry
    issue_registry.async_get = issue_registry_async_get
    issue_registry.async_create_issue = async_create_issue
    issue_registry.async_delete_issue = async_delete_issue

    # homeassistant.helpers.event
    event = _module("homeassistant.helpers.event")
    helpers.event = event

    def async_call_later(hass, delay, action):
        """Record the timer instead of arming a real one.

        Nothing in the integration may arm a real timer under pytest, and
        cancelling must actually remove the record, because "the timer was
        cancelled on reconnect" is part of the contract.  tests/test_repairs.py
        replaces this with its Timeline, which fires a timer only once a
        patched monotonic clock reaches its deadline — the 15-minute threshold
        has to be measured from the first drop, not from whenever a test
        chooses to fire whatever is recorded.
        """
        timers = getattr(hass, "pending_timers", None)
        if timers is None:
            timers = []
            hass.pending_timers = timers
        timer = (delay, action)
        timers.append(timer)

        def cancel() -> None:
            if timer in timers:
                timers.remove(timer)

        return cancel

    event.async_call_later = async_call_later

    # homeassistant.helpers.selector
    selector = _module("homeassistant.helpers.selector")
    helpers.selector = selector

    class EntitySelectorConfig(dict):
        """HA models this as a TypedDict; a dict subclass is faithful enough."""

    class EntitySelector:
        def __init__(self, config=None) -> None:
            self.config = config or {}

        def __call__(self, data):
            # Real selectors are voluptuous-callable validators.
            return data

    selector.EntitySelector = EntitySelector
    selector.EntitySelectorConfig = EntitySelectorConfig

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
