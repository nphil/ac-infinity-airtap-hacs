"""Tests for the hardened vendored controller (transportless).

``_send_command`` / ``_execute_disconnect`` are replaced with recording fakes
on the instance, so these tests exercise the real command construction and
state-commit logic without any BLE transport.  Frame payload expectations are
the builders' own (see test_protocol_commands.py) — nothing is invented.

Contracts pinned here (agreed with the library hardening pass):
- Honest state: turn_on/turn_off/set_speed commit state ONLY after the send
  coroutine returns; a raised BLE error leaves state untouched and fires no
  callback (a failed command must never leave HA believing the fan changed).
- update() ignores None and short (<19 byte) responses instead of writing
  garbage into work_type/levels.
- Advertisement clamp precedence: bounds are clamped toward an observed live
  level, never clobbered by it.
- Advertisement freshness bookkeeping for availability logic.
"""

import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ac_infinity.ac_infinity_ble import (
    ACInfinityController,
    CallbackType,
    DeviceInfo,
)
from tests.conftest import build_manufacturer_data
from tests.test_protocol_commands import frame_fields

ADDRESS = "AA:BB:CC:DD:EE:FF"


def make_controller(
    state: DeviceInfo, fail: bool = False, response: bytes | None = None
) -> ACInfinityController:
    """Build a controller whose transport is a recording fake.

    Must be called from within a running loop (controller __init__ requires
    one).  ``controller.sent`` collects raw frames; ``controller.events``
    collects fired callback types.
    """
    ble = SimpleNamespace(address=ADDRESS, name=state.name)
    controller = ACInfinityController(ble, state=state)
    controller.sent = []
    controller.events = []
    controller.disconnects = 0

    async def fake_send(command: bytes, retry=None):
        controller.sent.append(bytes(command))
        if fail:
            raise BleakError("proxy slot unavailable")
        return response

    async def fake_disconnect(force: bool = False):
        controller.disconnects += 1

    controller._send_command = fake_send
    controller._execute_disconnect = fake_disconnect
    controller.register_callback(lambda state, kind: controller.events.append(kind))
    return controller


def airtap_state(**overrides) -> DeviceInfo:
    base = dict(type=6, name="D-A6B2C", version=1)
    base.update(overrides)
    return DeviceInfo(**base)


class TestSetSpeedCommit:
    def test_set_speed_sends_manual_on_frame_and_commits(self):
        async def scenario():
            c = make_controller(airtap_state(work_type=1, fan=0))
            await c.set_speed(7)
            return c

        c = asyncio.run(scenario())
        f = frame_fields(c.sent[0])
        assert f["command_type"] == 3
        assert f["payload"] == [16, 1, 2, 18, 1, 7]  # ON(2), level 7
        assert c.state.work_type == 2
        assert c.state.fan == 7
        assert c.state.level_on == 7
        assert c.events == [CallbackType.UPDATE_RESPONSE]
        assert c.disconnects == 1  # connection always released

    def test_set_speed_zero_enters_off_mode(self):
        async def scenario():
            c = make_controller(airtap_state(work_type=2, fan=5))
            await c.set_speed(0)
            return c

        c = asyncio.run(scenario())
        assert frame_fields(c.sent[0])["payload"] == [16, 1, 1, 17, 1, 0]
        assert c.state.work_type == 1
        assert c.state.fan == 0
        assert c.state.level_off == 0

    def test_failed_send_leaves_state_untouched(self):
        """Honest-state rule: a BLE failure must not flip HA's picture of the
        fan (the fleet runs at RSSI -91; failures are routine, not edge)."""

        async def scenario():
            c = make_controller(
                airtap_state(work_type=3, fan=4, level_on=8, level_off=2),
                fail=True,
            )
            with pytest.raises(BleakError):
                await c.set_speed(9)
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 3  # still AUTO
        assert c.state.fan == 4
        assert c.state.level_on == 8
        assert c.events == []  # no callback for a change that didn't happen
        assert c.disconnects == 1  # connection still released


class TestTurnOnOff:
    def test_turn_on_defaults_to_stored_on_level(self):
        async def scenario():
            c = make_controller(airtap_state(work_type=1, fan=2, level_on=8))
            await c.turn_on()
            return c

        c = asyncio.run(scenario())
        assert frame_fields(c.sent[0])["payload"] == [16, 1, 2, 18, 1, 8]
        assert c.state.work_type == 2
        assert c.state.fan == 8

    def test_turn_off_preserves_stored_off_level(self):
        """OFF is a real mode with its own level; turning off must keep the
        user's stored level_off, not zero it."""

        async def scenario():
            c = make_controller(airtap_state(work_type=2, fan=8, level_off=2))
            await c.turn_off()
            return c

        c = asyncio.run(scenario())
        assert frame_fields(c.sent[0])["payload"] == [16, 1, 1, 17, 1, 2]
        assert c.state.work_type == 1
        assert c.state.fan == 2
        assert c.state.level_off == 2

    def test_failed_turn_off_keeps_running_state(self):
        async def scenario():
            c = make_controller(airtap_state(work_type=2, fan=8), fail=True)
            with pytest.raises(BleakError):
                await c.turn_off()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 2
        assert c.state.fan == 8


class TestUpdateResponseValidation:
    @staticmethod
    def model_data(work_type: int, level_off: int, level_on: int) -> bytes:
        """Model-data response shaped as update() reads it:
        work_type @12, level_off @15, level_on @18."""
        data = bytearray(19)
        data[12] = work_type
        data[15] = level_off
        data[18] = level_on
        return bytes(data)

    def test_good_response_merges_mode_and_levels(self):
        async def scenario():
            c = make_controller(
                airtap_state(), response=self.model_data(2, 3, 9)
            )
            await c.update()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 2
        assert c.state.level_off == 3
        assert c.state.level_on == 9
        assert c.state.fan == 9  # in ON mode the live level IS level_on
        assert c.events == [CallbackType.UPDATE_RESPONSE]

    def test_off_mode_response_sets_fan_to_off_level(self):
        async def scenario():
            c = make_controller(
                airtap_state(fan=9), response=self.model_data(1, 4, 9)
            )
            await c.update()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 1
        assert c.state.fan == 4

    def test_auto_mode_response_leaves_live_level_alone(self):
        """In AUTO the level floats between the bounds; advertisements own
        its freshness, not the poll."""

        async def scenario():
            c = make_controller(
                airtap_state(fan=6), response=self.model_data(3, 2, 8)
            )
            await c.update()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 3
        assert c.state.fan == 6

    def test_none_response_keeps_previous_state(self):
        async def scenario():
            c = make_controller(airtap_state(work_type=3, fan=6), response=None)
            await c.update()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 3
        assert c.state.fan == 6
        assert c.events == []

    def test_short_response_is_ignored(self):
        """Short frames are acks/stale responses; parsing one used to
        IndexError or write garbage into work_type/levels."""

        async def scenario():
            c = make_controller(
                airtap_state(work_type=3, fan=6), response=b"\x00" * 10
            )
            await c.update()
            return c

        c = asyncio.run(scenario())
        assert c.state.work_type == 3
        assert c.state.fan == 6
        assert c.events == []


class TestAdvertisementClamp:
    @staticmethod
    def advertise(controller: ACInfinityController, fan: int) -> None:
        adv = SimpleNamespace(
            manufacturer_data={
                2306: build_manufacturer_data(name="A6B2C", fan=fan)
            }
        )
        controller.set_ble_device_and_advertisement_data(
            SimpleNamespace(address=ADDRESS, name="D-A6B2C"), adv
        )

    def make(self, **state_overrides) -> ACInfinityController:
        async def _build():
            return make_controller(airtap_state(**state_overrides))

        return asyncio.run(_build())

    def test_level_within_bounds_leaves_bounds_alone(self):
        c = self.make(level_off=2, level_on=8)
        self.advertise(c, 6)
        assert c.state.level_off == 2
        assert c.state.level_on == 8

    def test_live_level_below_off_bound_lowers_it(self):
        c = self.make(level_off=4, level_on=8)
        self.advertise(c, 1)
        assert c.state.level_off == 1
        assert c.state.level_on == 8

    def test_live_level_above_on_bound_raises_it(self):
        c = self.make(level_off=2, level_on=6)
        self.advertise(c, 9)
        assert c.state.level_off == 2
        assert c.state.level_on == 9

    def test_unknown_bounds_are_not_invented(self):
        """Bounds stay None until a poll provides them — a live level of 5
        satisfies the implicit (0, 10) envelope."""
        c = self.make()
        self.advertise(c, 5)
        assert c.state.level_off is None
        assert c.state.level_on is None


class TestAdvertisementFreshness:
    def test_state_only_construction_has_no_freshness(self):
        async def _build():
            return make_controller(airtap_state())

        c = asyncio.run(_build())
        assert c.last_advertisement_monotonic is None
        assert c.advertisement_age is None

    def test_merge_records_freshness(self):
        c = TestAdvertisementClamp().make()
        TestAdvertisementClamp.advertise(c, 3)
        assert c.last_advertisement_monotonic is not None
        assert c.advertisement_age is not None
        assert c.advertisement_age >= 0.0
