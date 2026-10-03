"""Home Assistant's startup must never wait on a fan.

HA reports "initialized" only after every integration's setup returns, so a
setup that waits on a radio is on the critical path of every restart.
Measured 2026-10-02: this integration held it for 10 s (held fans) and would
have held it 30 s and then failed setup (unheld fans).

Contract pinned here:

* ``async_setup_entry`` returns within ``STARTUP_BUDGET`` (<= 5 s) however the
  fan behaves, held or not, and concurrently across entries;
* a miss never fails setup (the hold supervisor / the next advertisement
  recovers by itself); only "no connectable path at all", which is instant,
  still raises ``ConfigEntryNotReady``;
* until the fan's first state the entities are unavailable, and they fill in
  when it arrives after setup returned - advertisement or GATT;
* that arrival never makes the integration send anything.

Step bounds (S4/S8) are in test_link_steps.py; proxy routing in
test_proxy_routing.py.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from homeassistant.components import bluetooth

import custom_components.ac_infinity as integration
import custom_components.ac_infinity.coordinator as coordinator_module
from custom_components.ac_infinity.ac_infinity_ble import ACInfinityController
from custom_components.ac_infinity.circulation import CirculationController
from custom_components.ac_infinity.const import DOMAIN
from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import WORK_TYPE_AUTO
from custom_components.ac_infinity.fan import ACInfinityFan
from tests.test_coordinator_events import CHANGE, aci_frame, make_coordinator
from tests.test_link_poll import build
from tests.test_live_level import CAPTURES
from tests.test_shutdown_release import (
    FakeCoordinator,
    FakeEntry,
    FakeHass,
    setup_env,  # noqa: F401 - fixture
)

BUDGET = 0.1  # stands in for STARTUP_BUDGET so the tests do not sleep 5 s


class NeverReadyCoordinator(FakeCoordinator):
    """The real wait over an event the fan never sets."""

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._device_ready = asyncio.Event()

    async_wait_ready = ACInfinityDataUpdateCoordinator.async_wait_ready


@pytest.fixture
def never_ready(setup_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(integration, "ACInfinityDataUpdateCoordinator", NeverReadyCoordinator)
    monkeypatch.setattr(integration, "STARTUP_BUDGET", BUDGET)

    async def hang_forever(self) -> None:
        await asyncio.Event().wait()

    # The radio: a connect that never answers (what a hanging proxy looks like).
    monkeypatch.setattr(ACInfinityController, "_ensure_connected", hang_forever)
    return setup_env


def test_the_budget_is_within_the_contract():
    assert coordinator_module.STARTUP_BUDGET <= 5


@pytest.mark.parametrize("hold", [True, False], ids=["held", "unheld"])
def test_setup_returns_within_the_budget_when_the_fan_never_answers(never_ready, hold):
    waited = []
    original = NeverReadyCoordinator.async_wait_ready

    async def spy(self, timeout) -> bool:
        waited.append(timeout)
        return await original(self, timeout)

    NeverReadyCoordinator.async_wait_ready = spy
    try:

        async def scenario():
            hass = FakeHass()
            entry = FakeEntry("one", hold=hold)
            started = time.monotonic()
            result = await integration.async_setup_entry(hass, entry)
            elapsed = time.monotonic() - started
            await asyncio.sleep(0)
            data = hass.data[DOMAIN].get("one")
            if data is not None:
                await data.device.async_stop_hold()
            return result, elapsed, data

        result, elapsed, data = asyncio.run(scenario())
    finally:
        NeverReadyCoordinator.async_wait_ready = original

    assert result is True, "a miss never fails setup, held or not"
    assert waited == [BUDGET]
    assert BUDGET <= elapsed < BUDGET + 0.5
    assert data is not None, "the entry is fully set up, entities unavailable"


def test_six_entries_wait_together_not_one_after_another(never_ready):
    async def scenario():
        hass = FakeHass()
        entries = [FakeEntry(f"fan{n}", hold=n % 2 == 0) for n in range(6)]
        started = time.monotonic()
        results = await asyncio.gather(
            *(integration.async_setup_entry(hass, entry) for entry in entries)
        )
        elapsed = time.monotonic() - started
        for data in list(hass.data[DOMAIN].values()):
            if hasattr(data, "device"):
                await data.device.async_stop_hold()
        return results, elapsed

    results, elapsed = asyncio.run(scenario())
    assert results == [True] * 6
    assert elapsed < BUDGET * 3, f"serial setups would take {BUDGET * 6:.1f} s+"


def test_no_connectable_path_still_fails_instantly(setup_env, monkeypatch):
    outages = []
    monkeypatch.setattr(integration, "async_link_down", lambda *a: outages.append(a))
    monkeypatch.setattr(bluetooth, "async_ble_device_from_address", lambda *a, **k: None)

    async def scenario():
        started = time.monotonic()
        with pytest.raises(integration.ConfigEntryNotReady):
            await integration.async_setup_entry(FakeHass(), FakeEntry("one", hold=False))
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 0.5
    assert len(outages) == 1, "the outage clock still starts"


# ---------------------------------------------------------------------------
# Entities without data, then data after setup returned
# ---------------------------------------------------------------------------


def test_nothing_is_available_before_the_first_state():
    """The passive base seeds availability from the scanner cache, so a fan
    seen once looks available with no reading at all."""
    coordinator, _device = make_coordinator()
    assert coordinator._available is True
    assert coordinator.available is False


def test_a_notification_after_setup_fills_the_entities_in():
    async def scenario():
        coordinator, device, _hass = build()
        health = []
        coordinator.async_set_health_listener(lambda: health.append(True))
        coordinator._async_start()
        fan = ACInfinityFan(coordinator, device, "Fan")
        before = (fan.available, fan.percentage)
        renders = coordinator.listener_update_count
        # The fan answers over the held link long after setup gave up waiting.
        device._notification_handler(None, bytearray.fromhex(CAPTURES[0][0]))
        fan._handle_coordinator_update()
        after = (fan.available, fan.percentage)
        return before, after, coordinator.listener_update_count - renders, health

    before, after, renders, health = asyncio.run(scenario())
    assert before == (False, None)
    assert after == (True, 100)
    assert renders >= 1
    assert health == [True], "the watchdog is told the fan is reachable now"


def test_an_advertisement_after_setup_fills_the_entities_in():
    coordinator, device = make_coordinator()
    health = []
    coordinator.async_set_health_listener(lambda: health.append(True))
    assert coordinator.available is False
    coordinator._async_handle_bluetooth_event(aci_frame(fan=7), CHANGE)
    assert coordinator.available is True
    assert device.state.fan == 7
    # Once more: a frame that is not the first state is not a health flip.
    coordinator._async_handle_bluetooth_event(aci_frame(fan=7), CHANGE)
    assert health == [True]


def test_the_first_state_arriving_actuates_nothing():
    """Coming back from nothing must not send a single command; the
    circulation controller, already started, only compares registers."""

    async def scenario():
        coordinator, device, hass = build()
        sent = []

        async def record(*args, **kwargs):
            sent.append(args)

        device._send_command = record
        device._ensure_connected = record
        hass.config_entries = None
        settings = SimpleNamespace(
            thermostat=None, rest_speed=0, circulation_speed=None, circulation_hold=20
        )
        circulation = CirculationController(
            hass, settings, device, coordinator.async_update_listeners
        )
        coordinator._async_start()
        circulation.async_start()
        device.connected = True
        # First state: the live notification (type 6, AUTO), then a poll whose
        # minimum already equals the configured resting speed.
        device._notification_handler(None, bytearray.fromhex(CAPTURES[3][0]))
        assert device.state.work_type == WORK_TYPE_AUTO
        device.state.level_off = 0
        device._fire_callbacks(coordinator_module.CallbackType.UPDATE_RESPONSE)
        await asyncio.sleep(0)
        await asyncio.gather(*hass.tasks)
        return sent

    assert asyncio.run(scenario()) == []
