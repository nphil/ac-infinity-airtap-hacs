"""No single step of bringing a link up or letting it go may hang the fan.

Startup contract S4 (every step bounded <= 10 s) and S8 (the BACKEND ends a
stalled step before our cancellation does).

Why S8: on an ESPHome proxy, cancelling ``start_notify`` mid-flight leaves its
notification handler registered (aioesphomeapi removes it only on an
exception, not on cancellation), so later links deliver notifications to stale
objects.  So every subscribe passes a backend ``timeout`` shorter than the outer
guard, the connect's backend timeout is capped below it too, and the guard is
only the safety net.  A stall is reported to the owner either way, so it can
steer the next attempt to another proxy.
"""

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

from custom_components.ac_infinity.ac_infinity_ble import ACInfinityController
from tests.test_vendored_controller import airtap_state

vendored = importlib.import_module("custom_components.ac_infinity.ac_infinity_ble.device")

ADDRESS = "AA:BB:CC:DD:EE:FF"


class StepClient:
    """A GATT client whose steps hang on demand.

    ``honours_timeout`` makes a hanging subscribe behave like bleak-esphome:
    it gives up after its own ``timeout`` kwarg with a TimeoutError and runs
    its error path (``cleaned_up``), instead of waiting to be cancelled.
    """

    address = ADDRESS
    is_connected = True

    def __init__(
        self,
        *,
        hang_subscribe=False,
        hang_disconnect=False,
        hang_stop=False,
        honours_timeout=False,
    ):
        self.services = SimpleNamespace(get_characteristic=lambda uuid: object())
        self.hang_subscribe = hang_subscribe
        self.hang_disconnect = hang_disconnect
        self.hang_stop = hang_stop
        self.honours_timeout = honours_timeout
        self.subscribe_kwargs: dict | None = None
        self.subscribe_cancelled = False
        self.cleaned_up = False
        self.disconnects = 0

    async def start_notify(self, char, handler, **kwargs) -> None:
        self.subscribe_kwargs = kwargs
        if not self.hang_subscribe:
            return
        try:
            if self.honours_timeout:
                await asyncio.sleep(kwargs["timeout"])
                self.cleaned_up = True  # the backend's own error path
                raise TimeoutError("proxy did not acknowledge the subscribe")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.subscribe_cancelled = True
            raise

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
    monkeypatch.setattr(vendored, "LINK_STEP_TIMEOUT", 0.2)
    monkeypatch.setattr(vendored, "NOTIFY_SUBSCRIBE_TIMEOUT", 0.03)
    monkeypatch.setattr(vendored, "STOP_NOTIFY_TIMEOUT", 0.02)


def controller_for(client):
    """A real controller whose connect hands back ``client`` (or hangs)."""
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
    stalled = []

    async def scenario():
        controller, hang = controller_for(None)
        monkeypatch.setattr(vendored, "establish_connection", hang)
        controller.set_link_handlers(on_stalled=lambda: stalled.append(1))
        started = time.monotonic()
        with pytest.raises(BleakError):  # retryable, unlike a bare TimeoutError
            await controller._ensure_connected()
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert stalled == [1], "the guard is the safety net: it reports the stall"
    assert not controller._connect_lock.locked()
    assert controller._client is None


def test_a_subscribe_that_never_answers_fails_fast_and_drops_the_link(
    fast_steps, monkeypatch
):
    """The guard still ends a backend that ignores its own timeout."""
    client = StepClient(hang_subscribe=True)
    stalled = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_link_handlers(on_stalled=lambda: stalled.append(1))
        started = time.monotonic()
        with pytest.raises(BleakError):
            await controller._ensure_connected()
        return time.monotonic() - started, controller

    elapsed, controller = asyncio.run(scenario())
    assert elapsed < 0.5
    assert client.disconnects == 1, "the half-open link is let go"
    assert stalled == [1]
    assert controller._client is None and not controller._connect_lock.locked()


def test_every_subscribe_passes_a_backend_timeout_below_the_outer_guard(monkeypatch):
    """S8: the backend must be able to end the step (and unregister its
    notification handler) before our cancellation would."""
    client = StepClient()  # a subscribe that succeeds
    ready = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_link_handlers(on_ready=lambda: ready.append(1))
        await controller._ensure_connected()
        return controller

    controller = asyncio.run(scenario())
    timeout = client.subscribe_kwargs["timeout"]
    assert timeout == vendored.NOTIFY_SUBSCRIBE_TIMEOUT == 4.0
    assert 2 * timeout <= 8 < vendored.LINK_STEP_TIMEOUT, (
        "two proxy round trips (subscribe + CCCD write) must fit inside the guard"
    )
    assert ready == [1], "a usable link is announced exactly once"
    assert controller.is_connected


def test_the_backend_ends_a_stalled_subscribe_before_the_guard_cancels_it(
    fast_steps, monkeypatch
):
    """S8: with a backend that honours ``timeout`` the subscribe is never
    cancelled mid-flight (aioesphomeapi leaves the notification handler
    registered on cancellation); it fails through its own error path and is
    still reported as a stall."""
    client = StepClient(hang_subscribe=True, honours_timeout=True)
    stalled = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_link_handlers(on_stalled=lambda: stalled.append(1))
        started = time.monotonic()
        with pytest.raises(BleakError):
            await controller._ensure_connected()
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert client.cleaned_up and not client.subscribe_cancelled
    assert elapsed < vendored.LINK_STEP_TIMEOUT
    assert stalled == [1]
    assert client.disconnects == 1


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
    assert elapsed < 0.8
    assert not controller._connect_lock.locked()


def test_an_ordinary_subscribe_failure_is_not_reported_as_a_stall(
    fast_steps, monkeypatch
):
    class Refusing(StepClient):
        async def start_notify(self, char, handler, **kwargs) -> None:
            raise BleakError("GATT error")

    client = Refusing()
    stalled = []

    async def scenario():
        controller, establish = controller_for(client)
        monkeypatch.setattr(vendored, "establish_connection", establish)
        controller.set_link_handlers(on_stalled=lambda: stalled.append(1))
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


def test_cancelling_a_teardown_does_not_abandon_the_link_half_open():
    """S8: an outside cancellation (unload cancelling the hold task) while a
    failed attempt's link is being dropped must still finish the disconnect."""

    class SlowDisconnect(StepClient):
        finished = False

        async def disconnect(self) -> None:
            self.disconnects += 1
            await asyncio.sleep(0.05)
            self.finished = True
            self.is_connected = False

    client = SlowDisconnect()

    async def scenario():
        controller, _ = controller_for(client)
        task = asyncio.get_running_loop().create_task(
            controller._disconnect_unpublished(client)
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)

    asyncio.run(scenario())
    assert client.finished and not client.is_connected


def test_the_step_bounds_are_within_the_contract():
    assert vendored.LINK_STEP_TIMEOUT <= 10
    assert vendored.STOP_NOTIFY_TIMEOUT <= vendored.LINK_STEP_TIMEOUT
    assert vendored.CONNECT_TIMEOUT < vendored.LINK_STEP_TIMEOUT


class FakeConnectClient:
    """Stands in for the HA wrapper client: records the connect timeout it is given."""

    timeouts: list = []
    outcome = "ok"

    def __init__(self, device, **kwargs) -> None:
        pass

    async def connect(self, **kwargs) -> None:
        type(self).timeouts.append(kwargs["timeout"])
        if type(self).outcome == "timeout":
            raise TimeoutError("proxy connect timed out")


def test_the_backend_connect_timeout_is_capped_below_the_outer_guard():
    """establish_connection passes 20 s; the backend must give up first (S8)."""
    FakeConnectClient.timeouts = []
    FakeConnectClient.outcome = "ok"

    async def scenario():
        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        controller = ACInfinityController(
            ble, state=airtap_state(), client_class=FakeConnectClient
        )
        client = controller._client_class(ble)
        await client.connect(timeout=20.0, dangerous_use_bleak_cache=False)
        await client.connect(timeout=3.0)  # a smaller request is kept

    asyncio.run(scenario())
    assert FakeConnectClient.timeouts == [vendored.CONNECT_TIMEOUT, 3.0]


def test_a_backend_connect_timeout_is_reported_once_even_if_the_retry_is_cut_off(
    fast_steps, monkeypatch
):
    """The attempt that timed out is the one to blame; the retry the guard then
    cuts off must not blame a second, innocent scanner."""
    FakeConnectClient.timeouts = []
    FakeConnectClient.outcome = "timeout"
    stalled = []

    async def scenario():
        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        controller = ACInfinityController(
            ble, state=airtap_state(), client_class=FakeConnectClient
        )
        controller.set_link_handlers(on_stalled=lambda: stalled.append(1))

        async def two_attempts(client_class, device, *args, **kwargs):
            client = client_class(device)
            with pytest.raises(TimeoutError):
                await client.connect(timeout=20.0)  # attempt 1 times out ...
            await asyncio.Event().wait()  # ... attempt 2 is cut off by the guard

        monkeypatch.setattr(vendored, "establish_connection", two_attempts)
        with pytest.raises(BleakError):
            await controller._ensure_connected()

    asyncio.run(scenario())
    assert stalled == [1]
