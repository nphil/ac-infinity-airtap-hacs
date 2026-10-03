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
* that arrival never makes the integration send anything;
* no single connect / subscribe / disconnect step can hang the controller
  longer than its bound, and a link that stalled on its subscribe steers the
  next attempt away from the same proxy.
"""

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError
from homeassistant.components import bluetooth

import custom_components.ac_infinity as integration
import custom_components.ac_infinity.coordinator as coordinator_module
from custom_components.ac_infinity.ac_infinity_ble import ACInfinityController
from custom_components.ac_infinity.circulation import CirculationController
from custom_components.ac_infinity.const import CONF_PREFERRED_PROXY, DOMAIN
from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import WORK_TYPE_AUTO
from custom_components.ac_infinity.fan import ACInfinityFan
from tests.test_coordinator_events import CHANGE, aci_frame, make_coordinator
from tests.test_link_poll import build
from tests.test_live_level import CAPTURES
from tests.test_shutdown_release import (
    ADDRESS,
    FakeCoordinator,
    FakeEntry,
    FakeHass,
    setup_env,  # noqa: F401 - fixture
)

vendored = importlib.import_module("custom_components.ac_infinity.ac_infinity_ble.device")

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


# ---------------------------------------------------------------------------
# S4: no single step hangs
# ---------------------------------------------------------------------------


class StepClient:
    """A GATT client whose steps hang on demand."""

    address = ADDRESS
    is_connected = True

    def __init__(self, *, hang_subscribe=False, hang_disconnect=False, hang_stop=False):
        self.services = SimpleNamespace(get_characteristic=lambda uuid: object())
        self.hang_subscribe = hang_subscribe
        self.hang_disconnect = hang_disconnect
        self.hang_stop = hang_stop
        self.disconnects = 0

    async def start_notify(self, char, handler) -> None:
        if self.hang_subscribe:
            await asyncio.Event().wait()

    async def stop_notify(self, char) -> None:
        if self.hang_stop:
            await asyncio.Event().wait()

    async def disconnect(self) -> None:
        self.disconnects += 1
        if self.hang_disconnect:
            await asyncio.Event().wait()
        self.is_connected = False

    async def clear_cache(self) -> None:
        pass


@pytest.fixture
def fast_steps(monkeypatch):
    monkeypatch.setattr(vendored, "LINK_STEP_TIMEOUT", 0.05)
    monkeypatch.setattr(vendored, "STOP_NOTIFY_TIMEOUT", 0.02)


def controller_for(client):
    """A real controller whose connect hands back ``client`` (or hangs)."""
    from tests.test_vendored_controller import airtap_state

    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    controller = ACInfinityController(ble, state=airtap_state())

    async def fake_establish(*args, **kwargs):
        if client is None:
            await asyncio.Event().wait()
        return client

    return controller, fake_establish


def test_a_connect_that_never_answers_fails_fast_and_frees_the_lock(
    fast_steps, monkeypatch
):
    async def scenario():
        controller, hang = controller_for(None)
        monkeypatch.setattr(vendored, "establish_connection", hang)
        started = time.monotonic()
        with pytest.raises(BleakError):  # retryable, unlike a bare TimeoutError
            await controller._ensure_connected()
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert not controller._connect_lock.locked()
    assert controller._client is None


def test_a_subscribe_that_never_answers_fails_fast_and_drops_the_link(
    fast_steps, monkeypatch
):
    client = StepClient(hang_subscribe=True)
    stalled = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_stalled_link_handler(stalled.append)
        started = time.monotonic()
        with pytest.raises(BleakError):
            await controller._ensure_connected()
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert client.disconnects == 1, "the half-open link is let go"
    assert stalled == [client]
    assert controller._client is None and not controller._connect_lock.locked()


def test_a_failed_attempts_hanging_disconnect_cannot_block_the_next_attempt(
    fast_steps, monkeypatch
):
    client = StepClient(hang_subscribe=True, hang_disconnect=True)

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        started = time.monotonic()
        with pytest.raises(BleakError):
            await controller._ensure_connected()
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert not controller._connect_lock.locked()


def test_an_ordinary_subscribe_failure_is_not_reported_as_a_stall(fast_steps, monkeypatch):
    class Refusing(StepClient):
        async def start_notify(self, char, handler) -> None:
            raise BleakError("GATT error")

    client = Refusing()
    stalled = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_stalled_link_handler(stalled.append)
        with pytest.raises(BleakError):
            await controller._ensure_connected()

    asyncio.run(scenario())
    assert stalled == []
    assert client.disconnects == 1


def test_a_hung_stop_notify_does_not_keep_the_disconnect_from_running(fast_steps):
    client = StepClient(hang_stop=True)

    async def scenario():
        controller, _ = controller_for(client)
        controller._client = client
        controller._read_char = object()
        started = time.monotonic()
        await controller._execute_disconnect(force=True)
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert client.disconnects == 1
    assert elapsed < 0.5
    assert not controller._connect_lock.locked()


def test_a_hung_disconnect_is_bounded_and_frees_the_lock(fast_steps):
    client = StepClient(hang_disconnect=True)

    async def scenario():
        controller, _ = controller_for(client)
        controller._client = client
        controller._read_char = object()
        started = time.monotonic()
        await controller._execute_disconnect(force=True)
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert not controller._connect_lock.locked()


def test_the_step_bounds_are_within_the_contract():
    assert vendored.LINK_STEP_TIMEOUT <= 10
    assert vendored.STOP_NOTIFY_TIMEOUT <= vendored.LINK_STEP_TIMEOUT


# ---------------------------------------------------------------------------
# S4: steering the next attempt after a stalled subscribe
# ---------------------------------------------------------------------------


def stalled_setup(monkeypatch, setup_env, preferred):  # noqa: F811
    """Run setup with the affinity factory captured; return (getter, device)."""
    captured = {}

    def capture(base, getter, **kwargs):
        captured["getter"] = getter
        return base

    monkeypatch.setattr(integration, "make_affinity_client_class", capture)

    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=False)
        entry.options[CONF_PREFERRED_PROXY] = preferred
        await integration.async_setup_entry(hass, entry)
        return hass.data[DOMAIN]["one"].device

    return captured, asyncio.run(scenario())


def test_a_stalled_subscribe_skips_the_preferred_proxy_once(setup_env, monkeypatch):
    captured, device = stalled_setup(monkeypatch, setup_env, "plant-room-proxy")
    getter = captured["getter"]
    assert getter() == "plant-room-proxy"
    device._on_stalled_link(SimpleNamespace())
    assert getter() is None, "the next connect takes habluetooth's own routing"
    assert getter() == "plant-room-proxy", "and only that one"


def test_a_stalled_subscribe_counts_against_the_proxy_that_carried_it():
    failures = []
    scanner = SimpleNamespace(_add_connect_failure=failures.append)
    integration._record_stalled_link(
        SimpleNamespace(_connected_scanner=scanner, address=ADDRESS)
    )
    assert failures == [ADDRESS]


def test_a_habluetooth_without_the_hooks_degrades_quietly():
    integration._record_stalled_link(SimpleNamespace(address=ADDRESS))
    integration._record_stalled_link(
        SimpleNamespace(_connected_scanner=SimpleNamespace(), address=ADDRESS)
    )
