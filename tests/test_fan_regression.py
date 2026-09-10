"""Regression tests for the fan entity's optimistic state updates.

Verified live on 2026-09-05:
- async_set_preset_mode changed the device to AUTO over BLE but wrote no
  optimistic state and notified no listeners — HA showed a stale preset for
  8+ minutes (until the next poll merged work_type back in).
- async_set_percentage silently exited AUTO (work_type 3 -> 2) with no UI
  indication: the preset chip stayed on "Auto" while the fan ran manually.

Contract (agreed with the entity layer): after each command coroutine
resolves, the entity reflects the commanded state locally and pokes the
coordinator's listeners; changing speed clears the preset chip.

Outcomes are asserted, not call order, so the entity layer keeps freedom in
how it sequences writes around the BLE await.
"""

import asyncio
from types import SimpleNamespace

from custom_components.ac_infinity.device import (WORK_TYPE_AUTO,
                                                  WORK_TYPE_CYCLE,
                                                  WORK_TYPE_TIMER_TO_OFF,
                                                  WORK_TYPE_TIMER_TO_ON,
                                                  DeviceInfoEx)
from custom_components.ac_infinity.fan import (PRESET_AUTO_MODE, PRESET_CYCLE,
                                               PRESET_TIMER_TO_OFF,
                                               PRESET_TIMER_TO_ON,
                                               ACInfinityFan)

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeDevice:
    """Records BLE-level commands; no transport involved."""

    def __init__(self, fan: int = 5) -> None:
        self.state = DeviceInfoEx(type=6, name="D-A6B2C", version=1, fan=fan)
        self.address = ADDRESS
        self.name = "D-A6B2C"
        self.calls: list[tuple] = []

    @property
    def is_on(self) -> bool:
        return bool(self.state.work_type == 2 and self.state.fan)

    async def async_set_work_type(self, work_type: int) -> None:
        self.calls.append(("async_set_work_type", work_type))
        self.state.work_type = work_type

    async def set_speed(self, speed: int) -> None:
        self.calls.append(("set_speed", speed))

    async def turn_on(self, speed=None) -> None:
        self.calls.append(("turn_on", speed))

    async def turn_off(self) -> None:
        self.calls.append(("turn_off",))


def make_fan(fan_speed: int = 5) -> tuple[ACInfinityFan, FakeDevice, SimpleNamespace]:
    device = FakeDevice(fan=fan_speed)
    listeners = []
    coordinator = SimpleNamespace(
        available=True,
        async_update_listeners=lambda: listeners.append(True),
        listener_notifications=listeners,
    )
    return ACInfinityFan(coordinator, device, "Fan"), device, coordinator


class TestPresetModeOptimism:
    def test_preset_auto_updates_state_immediately(self):
        fan, device, coordinator = make_fan()
        asyncio.run(fan.async_set_preset_mode(PRESET_AUTO_MODE))
        assert ("async_set_work_type", WORK_TYPE_AUTO) in device.calls
        assert fan.preset_mode == PRESET_AUTO_MODE
        assert fan.is_on is True
        assert fan.write_ha_state_calls >= 1
        assert coordinator.listener_notifications, (
            "coordinator listeners must be poked so sibling entities refresh"
        )

    def test_turn_on_with_preset_routes_through_preset_path(self):
        fan, device, _ = make_fan()
        asyncio.run(fan.async_turn_on(preset_mode=PRESET_AUTO_MODE))
        assert ("async_set_work_type", WORK_TYPE_AUTO) in device.calls
        assert fan.preset_mode == PRESET_AUTO_MODE

    def test_every_timer_preset_selects_its_own_work_type(self):
        """The four presets are distinct modes, not four names for AUTO."""
        selected = []
        for preset in (
            PRESET_AUTO_MODE,
            PRESET_TIMER_TO_ON,
            PRESET_TIMER_TO_OFF,
            PRESET_CYCLE,
        ):
            fan, device, _ = make_fan()
            asyncio.run(fan.async_set_preset_mode(preset))
            selected.append((preset, fan.preset_mode, device.calls[-1][1]))
        assert selected == [
            (PRESET_AUTO_MODE, PRESET_AUTO_MODE, WORK_TYPE_AUTO),
            (PRESET_TIMER_TO_ON, PRESET_TIMER_TO_ON, WORK_TYPE_TIMER_TO_ON),
            (PRESET_TIMER_TO_OFF, PRESET_TIMER_TO_OFF, WORK_TYPE_TIMER_TO_OFF),
            (PRESET_CYCLE, PRESET_CYCLE, WORK_TYPE_CYCLE),
        ]

    def test_a_timer_to_on_fan_waiting_at_zero_still_reads_on(self):
        """Otherwise an automation "turns it on" and cancels the countdown."""
        fan, device, _ = make_fan(fan_speed=0)
        device.state.work_type = WORK_TYPE_TIMER_TO_ON
        fan._update_attrs()
        assert (fan.is_on, fan.preset_mode) == (True, PRESET_TIMER_TO_ON)

    def test_an_unknown_preset_is_refused(self):
        fan, device, _ = make_fan()
        try:
            asyncio.run(fan.async_set_preset_mode("Schedule"))
        except ValueError:
            assert device.calls == []
        else:
            raise AssertionError("an unsupported mode must not reach the hardware")


class TestSpeedClearsPreset:
    def test_set_percentage_clears_preset_chip(self):
        """Changing speed exits AUTO on the hardware; the UI must not keep
        claiming 'Auto'."""
        fan, device, _ = make_fan()
        fan._attr_preset_mode = PRESET_AUTO_MODE  # entity currently in AUTO
        asyncio.run(fan.async_set_percentage(50))
        assert ("set_speed", 5) in device.calls
        assert fan.preset_mode is None
        assert fan.percentage == 50
        assert fan.is_on is True

    def test_turn_on_with_percentage_clears_preset_chip(self):
        fan, device, _ = make_fan()
        fan._attr_preset_mode = PRESET_AUTO_MODE
        asyncio.run(fan.async_turn_on(percentage=30))
        assert fan.preset_mode is None
        assert fan.is_on is True

    def test_set_percentage_zero_turns_off(self):
        fan, device, _ = make_fan()
        asyncio.run(fan.async_set_percentage(0))
        assert ("set_speed", 0) in device.calls
        assert fan.is_on is False
