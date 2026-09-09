"""Behaviour tests for the persistent GATT hold.

Verified live on 2026-09-08: every command paid a fresh ESPHome-proxy connect
(1.8-6.4 s) because the integration hung up after each round-trip, while the
same command on an already-open link lands in 150-300 ms.  The hold keeps the
link (and one proxy connection slot per fan) open.

Pinned here, in the order things go wrong:

* the polite trailing disconnects and the idle timer must be suppressed while
  holding, and must behave EXACTLY as before when it is off;
* a lost link must be reported once — never twice, never not at all — for
  both flavours of loss (the device dropping us, and a command's error path
  force-resetting a live link);
* the supervisor must rebuild the link and must never spin the event loop
  (``asyncio.Event.wait()`` on an already-set event returns without yielding,
  and a starved loop is the fleet-freeze failure mode this repo already has
  scar tissue from);
* a held device must stay ``available`` even when it stops advertising.

The real controller/device/coordinator/sensor run; only the BLE transport and
Home Assistant are fakes.
"""

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.ac_infinity.device as device_module
import custom_components.ac_infinity.sensor as sensor_module
from custom_components.ac_infinity.ac_infinity_ble import ACInfinityController
from custom_components.ac_infinity.ac_infinity_ble.models import DeviceInfo
from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import ACInfinityDevice, DeviceInfoEx
from custom_components.ac_infinity.hold import STATE_DISCONNECTED
from custom_components.ac_infinity.sensor import ConnectionSensor

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeClient:
    """Stands in for BleakClientWithServiceCache."""

    def __init__(self) -> None:
        self.is_connected = True
        self.disconnect_calls = 0

    async def stop_notify(self, char) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


def make_controller() -> ACInfinityController:
    """A connected controller with a fake client. Needs a running loop."""
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    controller = ACInfinityController(
        ble, state=DeviceInfo(type=6, name="D-A6B2C", version=1)
    )
    controller._client = FakeClient()
    controller._reset_disconnect_timer()
    return controller


class TestPoliteDisconnectGate:
    def test_polite_disconnect_releases_the_link_when_not_holding(self):
        """Unchanged behaviour with the hold off — this is the pre-1.3.0
        contract every existing test and live install depends on."""

        async def scenario():
            controller = make_controller()
            client = controller._client
            await controller._execute_disconnect()
            return controller, client

        controller, client = asyncio.run(scenario())
        assert client.disconnect_calls == 1
        assert controller.is_connected is False

    def test_polite_disconnect_is_skipped_while_holding(self):
        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            client = controller._client
            await controller._execute_disconnect()
            return controller, client

        controller, client = asyncio.run(scenario())
        assert client.disconnect_calls == 0
        assert controller.is_connected is True

    def test_forced_disconnect_still_tears_down_while_holding(self):
        """stop() and the command error paths must always win: a held link
        that cannot be reset would strand the proxy slot."""

        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            client = controller._client
            await controller._execute_disconnect(force=True)
            return controller, client

        controller, client = asyncio.run(scenario())
        assert client.disconnect_calls == 1
        assert controller.is_connected is False


class TestIdleTimer:
    def test_idle_timer_is_armed_when_not_holding(self):
        async def scenario():
            controller = make_controller()
            return controller._disconnect_timer

        assert asyncio.run(scenario()) is not None

    def test_idle_timer_is_not_armed_while_holding(self):
        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            controller._reset_disconnect_timer()
            return controller

        controller = asyncio.run(scenario())
        assert controller._disconnect_timer is None
        # Still classified as a live link, so the next drop counts.
        assert controller._expected_disconnect is False

    def test_enabling_the_hold_cancels_a_pending_idle_timer(self):
        async def scenario():
            controller = make_controller()
            assert controller._disconnect_timer is not None
            controller.set_hold_connection(True)
            return controller._disconnect_timer

        assert asyncio.run(scenario()) is None


class TestDropReporting:
    def test_unexpected_disconnect_notifies_once(self):
        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            drops = []
            controller.register_disconnect_callback(lambda: drops.append(1))
            client = controller._client
            client.is_connected = False
            controller._disconnected(client)
            return drops

        assert asyncio.run(scenario()) == [1]

    def test_expected_disconnect_is_not_a_drop(self):
        """Our own teardown must never inflate drops_1h."""

        async def scenario():
            controller = make_controller()
            drops = []
            controller.register_disconnect_callback(lambda: drops.append(1))
            await controller._execute_disconnect(force=True)
            return drops

        assert asyncio.run(scenario()) == []

    def test_forced_teardown_of_a_live_held_link_notifies(self):
        """A command hitting a BleakError force-resets a link that is still
        up; _disconnected() stays quiet for it, so this is the only report
        the supervisor gets — without it the hold silently dies."""

        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            drops = []
            controller.register_disconnect_callback(lambda: drops.append(1))
            await controller._execute_disconnect(force=True)
            return drops

        assert asyncio.run(scenario()) == [1]

    def test_forced_teardown_of_an_already_dead_link_does_not_double_count(self):
        """The device dropped us (already reported by _disconnected), then
        the in-flight command's error handler resets state."""

        async def scenario():
            controller = make_controller()
            controller.set_hold_connection(True)
            drops = []
            controller.register_disconnect_callback(lambda: drops.append(1))
            client = controller._client
            client.is_connected = False
            controller._disconnected(client)
            await controller._execute_disconnect(force=True)
            return drops

        assert asyncio.run(scenario()) == [1]


class CountingDevice(ACInfinityDevice):
    """Device whose ``is_connected`` reads are counted.

    A supervisor that spins would read it thousands of times per second and
    starve the loop; raising instead turns that into a clean test failure
    rather than a hung suite.
    """

    SPIN_LIMIT = 2000

    def __init__(self, *args, **kwargs) -> None:
        self.is_connected_reads = 0
        super().__init__(*args, **kwargs)
        self.connects = 0
        self.fail_next = 0

    @property
    def is_connected(self) -> bool:
        self.is_connected_reads += 1
        if self.is_connected_reads > self.SPIN_LIMIT:
            raise RuntimeError("hold supervisor is spinning the event loop")
        return ACInfinityController.is_connected.fget(self)

    async def _ensure_connected(self) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("no proxy slot")
        self.connects += 1
        self._client = FakeClient()
        self._reset_disconnect_timer()

    def drop(self) -> None:
        """Simulate the device taking the link away."""
        client = self._client
        client.is_connected = False
        self._disconnected(client)


def make_device() -> CountingDevice:
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    return CountingDevice(ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=1))


@pytest.fixture
def no_backoff(monkeypatch):
    """Collapse the reconnect ladder so tests never sleep, but record it."""
    waited = []

    def fake_delay(attempt):
        waited.append(attempt)
        return 0

    monkeypatch.setattr(device_module, "backoff_delay", fake_delay)
    return waited


async def settle(times: int = 12) -> None:
    """Give the supervisor task a bounded number of loop turns."""
    for _ in range(times):
        await asyncio.sleep(0)


class TestHoldSupervisor:
    def test_connects_at_start_and_reports_the_hold(self, no_backoff):
        async def scenario():
            device = make_device()
            device.async_start_hold()
            await settle()
            result = (
                device.connects,
                device.hold_status.hold,
                device.hold_status.reconnect_attempt,
                no_backoff,
            )
            await device.async_stop_hold()
            return result

        connects, hold, attempt, waited = asyncio.run(scenario())
        assert connects == 1
        assert hold is True
        assert attempt == 0
        assert waited == []  # the very first connect is immediate

    def test_reconnects_after_a_drop_and_counts_it(self, no_backoff):
        async def scenario():
            device = make_device()
            device.async_start_hold()
            await settle()
            device.drop()
            await settle()
            result = (
                device.connects,
                device.hold_status.drops_1h,
                device.hold_status.last_drop,
                device.hold_status.reconnect_attempt,
                list(no_backoff),
            )
            await device.async_stop_hold()
            return result

        connects, drops, last_drop, attempt, waited = asyncio.run(scenario())
        assert connects == 2
        assert drops == 1
        assert last_drop is not None
        assert attempt == 0
        assert waited == [1]  # first rung of the ladder before the retry

    def test_failed_reconnects_walk_up_the_ladder(self, no_backoff):
        async def scenario():
            device = make_device()
            device.async_start_hold()
            await settle()
            device.fail_next = 3
            device.drop()
            await settle(40)
            result = (device.connects, list(no_backoff))
            await device.async_stop_hold()
            return result

        connects, waited = asyncio.run(scenario())
        assert connects == 2  # initial + the one that finally succeeded
        assert waited == [1, 2, 3, 4]

    def test_stale_wake_while_connected_does_not_spin(self, no_backoff):
        """THE loop-starvation regression: a drop wakes the supervisor, but a
        command's own retry rebuilt the link first, so the supervisor finds
        itself connected with the event still set."""

        async def scenario():
            device = make_device()
            device.async_start_hold()
            await settle()
            reads_before = device.is_connected_reads
            device._hold_wake.set()  # stale signal, link is up
            await settle(30)
            result = (
                device.connects,
                device.is_connected_reads - reads_before,
                device._hold_task.done(),
            )
            await device.async_stop_hold()
            return result

        connects, reads, task_done = asyncio.run(scenario())
        assert connects == 1  # nothing reconnected; nothing needed to
        assert reads < 10  # a spin would blow past SPIN_LIMIT
        assert task_done is False

    def test_stop_hold_cancels_the_supervisor_and_stops_reconnecting(
        self, no_backoff
    ):
        async def scenario():
            device = make_device()
            device.async_start_hold()
            await settle()
            task = device._hold_task
            await device.async_stop_hold()
            # A drop after unload must not resurrect the connection.
            device.drop()
            await settle()
            return device.connects, task.cancelled(), device.hold_connection

        connects, cancelled, holding = asyncio.run(scenario())
        assert connects == 1
        assert cancelled is True
        assert holding is False

    def test_start_hold_is_idempotent(self, no_backoff):
        async def scenario():
            device = make_device()
            device.async_start_hold()
            device.async_start_hold()
            await settle()
            result = device.connects
            await device.async_stop_hold()
            return result

        assert asyncio.run(scenario()) == 1


class TestAvailability:
    @staticmethod
    def make_coordinator(controller):
        import logging

        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        return ACInfinityDataUpdateCoordinator(
            None, logging.getLogger(__name__), ble, controller
        )

    def test_held_device_stays_available_without_advertisements(self):
        """A held fan advertises far less often; the advertisement tracker
        must not be allowed to call it unavailable while we are talking to
        it over a live link."""

        async def scenario():
            controller = make_controller()
            coordinator = self.make_coordinator(controller)
            coordinator._async_handle_unavailable(
                SimpleNamespace(name="D-A6B2C", address=ADDRESS)
            )
            return coordinator

        coordinator = asyncio.run(scenario())
        assert coordinator.available is True

    def test_unavailability_still_applies_when_not_connected(self):
        async def scenario():
            controller = make_controller()
            await controller._execute_disconnect(force=True)
            coordinator = self.make_coordinator(controller)
            coordinator._async_handle_unavailable(
                SimpleNamespace(name="D-A6B2C", address=ADDRESS)
            )
            return coordinator

        coordinator = asyncio.run(scenario())
        assert coordinator.available is False


class TestConnectionSensor:
    @staticmethod
    def make_sensor(controller):
        coordinator = SimpleNamespace(available=True)
        sensor = ConnectionSensor(coordinator, controller)
        sensor.hass = object()
        return sensor

    def test_unique_id_and_category(self):
        async def scenario():
            return self.make_sensor(make_controller())

        sensor = asyncio.run(scenario())
        assert sensor.unique_id == f"{ADDRESS}_connection"
        assert sensor.entity_category == "diagnostic"
        # Name must come from the translation key, not a hardcoded _attr_name.
        assert sensor.translation_key == "connection"
        assert not hasattr(sensor, "_attr_name")

    def test_reports_the_holding_proxy_name(self, monkeypatch):
        monkeypatch.setattr(
            sensor_module,
            "async_holding_scanner_name",
            lambda hass, address: "plant-room-bluetooth-proxy",
        )

        async def scenario():
            device = make_device()
            device._client = FakeClient()
            device.hold_status.set_hold(True)
            sensor = self.make_sensor(device)
            sensor._update_attrs()
            return sensor

        sensor = asyncio.run(scenario())
        assert sensor.native_value == "plant-room-bluetooth-proxy"
        assert sensor.extra_state_attributes == {
            "hold": True,
            "drops_1h": 0,
            "last_drop": None,
            "reconnect_attempt": 0,
        }

    def test_reports_disconnected_with_the_drop_history(self, monkeypatch):
        monkeypatch.setattr(
            sensor_module, "async_holding_scanner_name", lambda hass, address: None
        )

        async def scenario():
            device = make_device()
            device.hold_status.set_hold(True)
            device.hold_status.record_drop()
            device.hold_status.set_reconnect_attempt(2)
            sensor = self.make_sensor(device)
            sensor._update_attrs()
            return sensor

        sensor = asyncio.run(scenario())
        assert sensor.native_value == STATE_DISCONNECTED
        attrs = sensor.extra_state_attributes
        assert attrs["hold"] is True
        assert attrs["drops_1h"] == 1
        assert attrs["last_drop"] is not None
        assert attrs["reconnect_attempt"] == 2
