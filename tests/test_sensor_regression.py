"""Regression tests for the fan speed sensor.

Verified live on 2026-09-05: with the fan stopped (state.fan == 0) the sensor
kept reporting the last non-zero speed (80%) indefinitely, because it retained
``_last_speed``.  Contract agreed with the entity layer: the sensor reports the
ACTUAL speed — 0 when the fan is stopped, never a retained value.

These tests run against the real sensor module with Home Assistant stubbed
(see tests/ha_stubs.py); the coordinator/device are lightweight fakes because
only the value computation is under test, not BLE plumbing.
"""

from types import SimpleNamespace

import pytest

from custom_components.ac_infinity.device import DeviceInfoEx
from custom_components.ac_infinity.sensor import FanSpeedSensor

ADDRESS = "AA:BB:CC:DD:EE:FF"


def make_sensor(fan: int | None = 0) -> tuple[FanSpeedSensor, DeviceInfoEx]:
    state = DeviceInfoEx(type=6, name="D-A6B2C", version=1, fan=fan)
    device = SimpleNamespace(address=ADDRESS, name="D-A6B2C", state=state)
    coordinator = SimpleNamespace(available=True)
    return FanSpeedSensor(coordinator, device, "Fan Speed"), state


def push_update(sensor: FanSpeedSensor) -> None:
    """Deliver a coordinator update the way the coordinator would."""
    sensor._handle_coordinator_update()


class TestFanSpeedSensor:
    def test_stopped_fan_reports_zero(self):
        sensor, _ = make_sensor(fan=0)
        push_update(sensor)
        assert sensor.native_value == 0

    def test_running_fan_reports_percentage(self):
        sensor, _ = make_sensor(fan=5)
        push_update(sensor)
        assert sensor.native_value == 50

    @pytest.mark.parametrize(("fan", "pct"), [(1, 10), (8, 80), (10, 100)])
    def test_speed_to_percentage_mapping(self, fan, pct):
        sensor, _ = make_sensor(fan=fan)
        push_update(sensor)
        assert sensor.native_value == pct

    def test_stop_after_run_never_retains_old_speed(self):
        """THE regression: 8 -> 0 must read 0, not a remembered 80."""
        sensor, state = make_sensor(fan=8)
        push_update(sensor)
        assert sensor.native_value == 80
        state.fan = 0
        push_update(sensor)
        assert sensor.native_value == 0

    def test_unknown_speed_reports_none_not_stale(self):
        """state.fan is None only before any advertisement has been merged;
        report unknown rather than inventing a speed."""
        sensor, _ = make_sensor(fan=None)
        push_update(sensor)
        assert sensor.native_value is None
