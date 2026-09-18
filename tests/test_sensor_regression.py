"""Regression tests for the fan speed and temperature sensors.

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
from custom_components.ac_infinity.sensor import FanSpeedSensor, TemperatureSensor

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


class TestTemperatureSensor:
    """These controllers re-advertise every ~2 s and the reading genuinely
    wanders - measured live 2026-09-18, one vent swung 1.7 degC in ten minutes
    with consecutive samples up to 0.9 degC apart - so every update was a
    distinct state with its own recorder row: the six vents' temperature
    sensors held 48% of a 15-day, 7.2 M-row history.

    Rounding alone does not fix that at any grid size (a value sitting on a
    boundary flaps between neighbours), and measurably did not: 27 rows/min
    before, 26-36 after. The contract is whole degrees held inside a deadband.
    """

    @staticmethod
    def make(temperature):
        state = DeviceInfoEx(type=6, name="D-A6B2C", version=1, fan=0)
        device = SimpleNamespace(
            address=ADDRESS, name="D-A6B2C", state=state, temperature=temperature
        )
        coordinator = SimpleNamespace(available=True)
        return TemperatureSensor(coordinator, device, "Temperature")

    def feed(self, sensor, *readings):
        """Deliver successive advertisements, collecting what was published."""
        published = []
        for reading in readings:
            sensor._device.temperature = reading
            push_update(sensor)
            published.append(sensor.native_value)
        return published

    @pytest.mark.parametrize(
        ("raw", "published"),
        [(21.01, 21.0), (21.6, 22.0), (22.0, 22.0), (-3.4, -3.0)],
    )
    def test_first_reading_publishes_whole_degrees(self, raw, published):
        sensor = self.make(raw)
        push_update(sensor)
        assert sensor.native_value == pytest.approx(published)

    def test_jitter_around_a_boundary_publishes_one_unchanged_value(self):
        """THE regression: a reading wandering either side of x.5 must not
        flap between two whole degrees - that flapping is the recorder flood.
        """
        sensor = self.make(22.4)
        push_update(sensor)
        first = sensor.native_value
        assert self.feed(sensor, 22.5, 22.6, 22.49, 22.51, 22.45) == [first] * 5

    def test_slow_drift_inside_the_deadband_holds_the_value(self):
        sensor = self.make(22.0)
        push_update(sensor)
        assert self.feed(sensor, 22.3, 22.7, 21.4, 22.1) == [22.0] * 4

    def test_real_movement_is_reported(self):
        sensor = self.make(22.0)
        push_update(sensor)
        assert self.feed(sensor, 22.9, 24.2) == [23.0, 24.0]

    def test_missing_reading_is_unknown_not_zero(self):
        sensor = self.make(None)
        push_update(sensor)
        assert sensor.native_value is None

    def test_reading_after_unknown_is_treated_as_first(self):
        """A value held across an outage would be deadbanded against a stale
        reading, so the published value is forgotten when it goes unknown."""
        sensor = self.make(22.0)
        push_update(sensor)
        assert self.feed(sensor, None, 22.4) == [None, 22.0]
