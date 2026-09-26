"""The fan's minimum follows the HVAC blower (circulation.py).

Contract: while the thermostat reports the blower running, an AUTO fan's
minimum is the circulation speed; once the blower has been off for the hold,
it is the resting speed. Every change is a write the fan keeps in its own
memory, so the tests pin what must NOT write: an unavailable thermostat, a
fan outside AUTO, a restart that does not know when the blower stopped, and
a fan that did not take a value the first time.
"""

import asyncio

from homeassistant.core import State

from custom_components.ac_infinity.circulation import CirculationController
from custom_components.ac_infinity.device import WORK_TYPE_AUTO, WORK_TYPE_ON
from custom_components.ac_infinity.hold import HoldStatus

THERMOSTAT = "climate.living_room_thermostat"


class FakeHass:
    def __init__(self, action: str | None) -> None:
        self.states = {}
        self.tasks: list[asyncio.Task] = []
        self.set_action(action)

    def set_action(self, action: str | None) -> State:
        state = (
            State(THERMOSTAT, "unavailable")
            if action is None
            else State(THERMOSTAT, "cool", {"hvac_action": action})
        )
        self.states[THERMOSTAT] = state
        return state

    def async_create_background_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeDevice:
    name = "D-A6B2C"
    address = "AA:BB:CC:DD:EE:FF"

    def __init__(self, floor: int, work_type: int = WORK_TYPE_AUTO) -> None:
        self.state = type("S", (), {"work_type": work_type})()
        self.min_speed = floor
        self.is_connected = True
        self.hold_status = HoldStatus()
        self.writes: list[int] = []
        self.accepts = True

    def register_callback(self, _callback):
        return lambda: None

    async def async_set_min_speed(self, value: int) -> None:
        self.writes.append(value)
        if self.accepts:
            self.min_speed = value


class Settings:
    thermostat = THERMOSTAT
    rest_speed = 0
    circulation_speed = 4
    circulation_hold = 20


def start(action: str | None, floor: int, **device_kwargs):
    hass = FakeHass(action)
    device = FakeDevice(floor, **device_kwargs)
    controller = CirculationController(hass, Settings(), device, lambda: None)
    controller.async_start()
    return hass, device, controller


def blower(hass: FakeHass, action: str | None) -> None:
    state = hass.set_action(action)
    for _ids, callback in list(hass.state_subscriptions):
        callback(type("E", (), {"data": {"new_state": state}})())


def hold_timers(hass: FakeHass):
    return [action for delay, action in getattr(hass, "pending_timers", []) if delay == 20 * 60]


async def settle(hass: FakeHass) -> None:
    await asyncio.gather(*hass.tasks)
    hass.tasks.clear()


class TestFollowsTheBlower:
    def test_running_blower_raises_the_minimum_and_a_long_stop_lowers_it(self):
        async def scenario():
            hass, device, _ = start("cooling", floor=0)
            await settle(hass)
            after_start = list(device.writes)
            blower(hass, "idle")
            await settle(hass)
            during_hold = list(device.writes)
            (expire,) = hold_timers(hass)
            expire(None)
            await settle(hass)
            return after_start, during_hold, device.writes

        after_start, during_hold, final = asyncio.run(scenario())
        assert after_start == [4]
        assert during_hold == [4], "the hold absorbs the blower's short gaps"
        assert final == [4, 0]

    def test_a_blower_back_within_the_hold_writes_nothing(self):
        async def scenario():
            hass, device, _ = start("fan", floor=4)
            blower(hass, "idle")
            blower(hass, "fan")
            await settle(hass)
            return device.writes, hold_timers(hass)

        assert asyncio.run(scenario()) == ([], [])


class TestWritesNothing:
    def test_when_the_thermostat_is_unavailable(self):
        async def scenario():
            hass, device, controller = start("fan", floor=4)
            blower(hass, None)
            await settle(hass)
            return device.writes, hold_timers(hass), controller.circulating

        assert asyncio.run(scenario()) == ([], [], True)

    def test_to_a_fan_that_is_not_in_auto(self):
        """Register 17 is also OFF mode's speed: writing it could start a fan."""

        async def scenario():
            hass, device, _ = start("cooling", floor=0, work_type=WORK_TYPE_ON)
            await settle(hass)
            return device.writes

        assert asyncio.run(scenario()) == []

    def test_after_a_restart_until_the_hold_has_passed(self):
        """Nothing knows when the blower stopped, so the fan keeps its minimum."""

        async def scenario():
            hass, device, _ = start("idle", floor=4)
            await settle(hass)
            return device.writes, len(hold_timers(hass))

        assert asyncio.run(scenario()) == ([], 1)

    def test_again_soon_after_the_fan_refused_a_value(self):
        async def scenario():
            hass, device, controller = start("cooling", floor=0)
            device.accepts = False
            await settle(hass)
            for _ in range(5):
                controller.async_reconcile()
            await settle(hass)
            return device.writes

        assert asyncio.run(scenario()) == [4]
