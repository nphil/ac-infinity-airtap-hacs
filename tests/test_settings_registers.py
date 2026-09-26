"""The fan's settings registers: unit (32), ramp (34), calibration (36),
and the triggers kept in whole degrees F.

Encodings are the vendor app's (decompiled 2.0.9, ProtocolResolution
parseSetting/setSettingData): 32 is one byte, 1 = Celsius; 34/36 are
[F, C, humidity], calibration signed. Every write sends back the bytes it
does not mean to change exactly as the fan reported them, and nothing is
written to a register the fan has not answered for.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.ac_infinity.device import (OPCODE_CALIBRATION,
                                                  OPCODE_DISPLAY, OPCODE_RAMP,
                                                  OPCODE_UNIT, ACInfinityDevice,
                                                  AutoModeConfig, DeviceInfoEx)
from tests.test_model_data import CAPTURE_AUTO

ADDRESS = "AA:BB:CC:DD:EE:FF"


def with_groups(frame: bytes, groups: list[tuple[int, bytes]]) -> bytes:
    """``frame`` with extra ``[opcode, length, value]`` groups appended."""
    extra = b"".join(bytes([op, len(value)]) + value for op, value in groups)
    payload = frame[10:-2] + extra
    return frame[:2] + len(payload).to_bytes(2, "big") + frame[4:10] + payload + frame[-2:]


def run(action, response=None, **state):
    async def scenario():
        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        device = ACInfinityDevice(
            ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=3, **state)
        )
        device.sent = []

        async def fake_send(command, retry=None):
            device.sent.append(bytes(command))
            return response

        async def nothing(*_args, **_kwargs):
            return None

        device._send_command = fake_send
        device._ensure_connected = nothing
        device._execute_disconnect = nothing
        await action(device)
        return device

    return asyncio.run(scenario())


def written(device) -> list[int]:
    (frame,) = device.sent
    assert frame[9] == 3, "a write, not a read"
    return list(frame[10:-2])


class TestPoll:
    def test_the_settings_registers_are_read_alongside_the_rest(self):
        response = with_groups(
            CAPTURE_AUTO,
            [
                (OPCODE_UNIT, bytes([0])),
                (OPCODE_DISPLAY, bytes([0xA3, 1])),
                (OPCODE_RAMP, bytes([1, 1, 0])),
                (OPCODE_CALIBRATION, bytes([0xFE, 0xFF, 0])),
            ],
        )
        device = run(lambda d: d.update(), response=response)
        assert device.state.display_celsius is False
        assert (device.ramp_f, device.calibration_f) == (1, -2)
        # The groups before them still parse as they always did.
        assert (device.state.work_type, device.state.level_on) == (3, 10)
        assert (device.auto_mode.low_temp_f, device.auto_mode.high_temp_f) == (60, 93)


class TestWrites:
    def test_ramp_changes_the_degrees_and_keeps_the_humidity_byte(self):
        device = run(lambda d: d.async_set_ramp_f(2), ramp=(1, 1, 7))
        assert written(device) == [OPCODE_RAMP, 3, 2, 1, 7]
        assert device.ramp_f == 2

    def test_a_negative_calibration_is_sent_as_signed_bytes(self):
        device = run(lambda d: d.async_set_calibration_f(-3), calibration=(0, 0, 0))
        assert written(device) == [OPCODE_CALIBRATION, 3, 0xFD, 0xFF, 0]
        assert device.calibration_f == -3

    def test_a_one_degree_ramp_matches_what_the_fan_stores(self):
        """The Master Bedroom vent's own panel stored a 1 F ramp as [1, 0]."""
        device = run(lambda d: d.async_set_ramp_f(1), ramp=(0, 0, 0))
        assert written(device) == [OPCODE_RAMP, 3, 1, 0, 0]

    def test_brightness_keeps_the_backlight_state(self):
        device = run(
            lambda d: d.async_set_display_brightness(0x02),
            display_brightness=0xA3,
            display_on=False,
        )
        assert written(device) == [OPCODE_DISPLAY, 2, 0x02, 0]

    def test_display_unit(self):
        device = run(lambda d: d.async_set_display_celsius(True), display_celsius=False)
        assert written(device) == [OPCODE_UNIT, 1, 1]

    @pytest.mark.parametrize(
        "action",
        [
            lambda d: d.async_set_ramp_f(2),
            lambda d: d.async_set_calibration_f(1),
            lambda d: d.async_set_display_brightness(0x02),
            lambda d: d.async_set_display_celsius(True),
        ],
        ids=["ramp", "calibration", "brightness", "unit"],
    )
    def test_nothing_is_written_before_the_fan_reports_the_register(self, action):
        async def attempt(device):
            with pytest.raises(ValueError):
                await action(device)

        assert run(attempt).sent == []

    def test_a_trigger_is_set_in_whole_fahrenheit_and_the_other_kept_as_read(self):
        """Whole-Celsius steps would land a 65 F trigger on 64 F, and
        recomputing the untouched trigger from its Celsius byte would move it."""
        config = AutoModeConfig(
            high_temp_enabled=True, high_temp=34, low_temp_enabled=True, low_temp=16,
            high_humidity_enabled=False, high_humidity=0,
            low_humidity_enabled=False, low_humidity=0,
            high_temp_f=94, low_temp_f=60,
        )
        device = run(lambda d: d.async_set_cold_trigger_f(65), auto_mode=config)
        assert written(device) == [19, 7, 0x0C, 94, 34, 65, 18, 0, 0]
