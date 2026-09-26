"""The fan's display switch: register 33, read with every poll.

The AC Infinity app reads register 33 with its settings and gets back either
[brightness gear] or [brightness gear, backlight 0/1]; it writes the 2-byte
form only to a fan that answered with it (decompiled app 2.0.9,
ProtocolResolution.parseSetting / setSettingData). The integration follows
the same rule: it reads the register in the poll it already makes and
refuses to write before the fan has reported a backlight byte.

Responses below are the verbatim AUTO capture from test_model_data with the
display group appended, since the rest of the poll must parse exactly as it
did before the extra group was requested.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.ac_infinity.device import (OPCODE_DISPLAY, POLL_OPCODES,
                                                  ACInfinityDevice,
                                                  DeviceInfoEx)
from tests.test_model_data import CAPTURE_AUTO

ADDRESS = "AA:BB:CC:DD:EE:FF"


def with_display(frame: bytes, value: bytes) -> bytes:
    """``frame`` with a display group appended to its payload."""
    payload = frame[10:-2] + bytes([OPCODE_DISPLAY, len(value)]) + value
    return frame[:2] + len(payload).to_bytes(2, "big") + frame[4:10] + payload + frame[-2:]


def run(action, response=None, **state):
    """Run ``action(device)`` on a fan whose BLE transport is a recorder."""

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


def payload(frame: bytes) -> list[int]:
    return list(frame[10:-2])


class TestDisplayRead:
    def test_the_poll_reads_brightness_and_backlight(self):
        device = run(
            lambda d: d.update(), response=with_display(CAPTURE_AUTO, bytes([0xA3, 0]))
        )
        assert payload(device.sent[0]) == list(POLL_OPCODES)
        assert (device.state.display_brightness, device.state.display_on) == (0xA3, False)
        # The extra group must not disturb the registers read before it.
        assert (device.state.work_type, device.state.level_on) == (3, 10)

    def test_a_brightness_only_fan_has_no_switch_to_offer(self):
        device = run(lambda d: d.update(), response=with_display(CAPTURE_AUTO, bytes([2])))
        assert (device.state.display_brightness, device.state.display_on) == (2, None)

    def test_a_fan_that_does_not_answer_stays_unknown(self):
        device = run(lambda d: d.update(), response=CAPTURE_AUTO)
        assert device.state.display_on is None


class TestDisplayWrite:
    def test_turning_it_off_keeps_the_brightness_gear(self):
        device = run(
            lambda d: d.async_set_display(False), display_brightness=0xA3, display_on=True
        )
        (frame,) = device.sent
        assert frame[9] == 3, "a write, not a read"
        assert payload(frame) == [OPCODE_DISPLAY, 2, 0xA3, 0]
        assert device.state.display_on is False

    def test_nothing_is_written_before_the_fan_reports_a_switch(self):
        async def attempt(device):
            with pytest.raises(ValueError):
                await device.async_set_display(False)

        device = run(attempt, display_brightness=0xA3, display_on=None)
        assert device.sent == []
