"""Every fan must let go of its GATT link when Home Assistant shuts down.

Home Assistant does not unload config entries on shutdown; ``bluetooth`` tears
its stack down on EVENT_HOMEASSISTANT_STOP, so a disconnect attempted then
never completes and the proxy keeps a ghost link (measured 2026-09-09).  Home
Assistant's stage-1 shutdown jobs run BEFORE that event, concurrently, under
one shared budget — so each entry registers its own job.

Pinned here:

* one job per entry, removed again when the entry unloads;
* running the job quiets the watchers in unload order, drops the link, and
  latches: afterwards nothing (hold supervisor, advertisement poll, link poll,
  command, release_link's resume timer) can open a link in this process;
* a hanging or failing disconnect is bounded and never raises;
* release_link releases entries concurrently, each under the same bound.

``hass.state`` is still ``running`` while shutdown jobs execute, so none of
the gating may lean on it.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from homeassistant.components import bluetooth
from homeassistant.core import CoreState

import custom_components.ac_infinity as integration
import custom_components.ac_infinity.coordinator as coordinator_module
from custom_components.ac_infinity.ac_infinity_ble import ACInfinityController
from custom_components.ac_infinity.const import DOMAIN
from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import (
    ACInfinityDevice,
    DeviceInfoEx,
    LinkClosingError,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
SNAPSHOT = {"name": "D-A6B2C", "type": 6, "version": 1}


# ---------------------------------------------------------------------------
# Fakes for async_setup_entry
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, log: list[str]) -> None:
        self.is_connected = True
        self.log = log

    async def stop_notify(self, char) -> None:
        pass

    async def disconnect(self) -> None:
        self.log.append("disconnect")
        self.is_connected = False


class FakeHass:
    """hass as async_setup_entry touches it, with a live shutdown-job list."""

    def __init__(self) -> None:
        self.data: dict = {}
        self.shutdown_jobs: list = []
        self.is_stopping = False
        self.state = CoreState.running  # still "running" during stage 1
        self.services = SimpleNamespace(
            has_service=lambda domain, service: True,
            async_register=lambda *args, **kwargs: None,
        )
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=self._forward, async_entries=self._entries
        )
        self.loaded: list = []

    async def _forward(self, entry, platforms) -> None:
        pass

    def _entries(self, domain):
        return list(self.loaded)

    def async_add_shutdown_job(self, job, *args):
        self.shutdown_jobs.append(job)

        def remove() -> None:
            if job in self.shutdown_jobs:
                self.shutdown_jobs.remove(job)

        return remove


class FakeEntry:
    def __init__(self, entry_id: str, hold: bool) -> None:
        self.entry_id = entry_id
        self.title = f"Vent {entry_id}"
        self.data = {"address": ADDRESS, "service_data": dict(SNAPSHOT)}
        self.options = {"hold_connection": hold}
        self.unload_callbacks: list = []

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)

    def add_update_listener(self, listener):
        return lambda: None

    def unload(self) -> None:
        """What Home Assistant does after async_unload_entry: run the callbacks."""
        while self.unload_callbacks:
            callback = self.unload_callbacks.pop()
            result = callback()
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)


class FakeCoordinator:
    def __init__(self, hass, logger, ble_device, device) -> None:
        self.controller = device

    def async_start(self):
        return lambda: None

    async def async_wait_ready(self, timeout) -> bool:
        return True

    def async_update_listeners(self) -> None:
        pass


class Watchers:
    """Shared event log: watchdog, circulation and the link all write to it."""

    def __init__(self) -> None:
        self.log: list[str] = []


@pytest.fixture
def setup_env(monkeypatch):
    """Patch everything async_setup_entry builds except the device itself."""
    watchers = Watchers()
    connects: list[int] = []

    class FakeWatchdog:
        def __init__(self, hass, entry, coordinator) -> None:
            pass

        def async_start(self) -> None:
            pass

        def async_stop(self) -> None:
            watchers.log.append("watchdog.async_stop")

    class FakeCirculation:
        def __init__(self, hass, settings, device, notify) -> None:
            pass

        def async_start(self) -> None:
            pass

        def async_stop(self) -> None:
            watchers.log.append("circulation.async_stop")

    async def fake_connect(self) -> None:
        # Stands in for the vendored connect: opens a fake link.
        connects.append(1)
        self._client = FakeClient(watchers.log)

    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    monkeypatch.setattr(
        bluetooth, "async_ble_device_from_address", lambda *a, **k: ble
    )
    monkeypatch.setattr(integration, "ACInfinityDataUpdateCoordinator", FakeCoordinator)
    monkeypatch.setattr(integration, "ACInfinityLinkWatchdog", FakeWatchdog)
    monkeypatch.setattr(integration, "CirculationController", FakeCirculation)
    monkeypatch.setattr(integration, "VentSettings", lambda hass, entry: object())
    monkeypatch.setattr(
        integration, "async_holding_scanner_name", lambda hass, address: "proxy"
    )
    monkeypatch.setattr(ACInfinityController, "_ensure_connected", fake_connect)
    return SimpleNamespace(watchers=watchers, connects=connects)


async def settle(times: int = 12) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_each_entry_registers_its_own_job_and_unload_removes_it(setup_env):
    async def scenario():
        hass = FakeHass()
        first, second = FakeEntry("one", hold=False), FakeEntry("two", hold=False)
        assert await integration.async_setup_entry(hass, first)
        assert await integration.async_setup_entry(hass, second)
        registered = len(hass.shutdown_jobs)
        first.unload()
        after_first = len(hass.shutdown_jobs)
        second.unload()
        return registered, after_first, len(hass.shutdown_jobs)

    assert asyncio.run(scenario()) == (2, 1, 0)


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------


def test_the_job_quiets_watchers_releases_the_link_and_latches(setup_env, caplog):
    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=True)
        await integration.async_setup_entry(hass, entry)
        device = hass.data[DOMAIN]["one"].device
        await settle()
        assert device.is_connected and len(setup_env.connects) == 1

        (job,) = hass.shutdown_jobs
        await job.target()

        opened_before = len(setup_env.connects)
        device.async_start_hold()  # a late re-arm must be refused
        await settle()
        with pytest.raises(LinkClosingError):
            await device._ensure_connected()
        with pytest.raises(LinkClosingError):
            await device.async_set_max_speed(5)  # an entity command
        await settle()
        return device, opened_before

    with caplog.at_level(logging.INFO):
        device, opened_before = asyncio.run(scenario())

    assert setup_env.watchers.log == [
        "watchdog.async_stop",
        "circulation.async_stop",
        "disconnect",
    ], "watchers first (as async_unload_entry orders it), then the link"
    assert device.closing
    assert not device.is_connected
    assert device._hold_task is None
    assert len(setup_env.connects) == opened_before == 1, "nothing may reconnect"
    assert any(
        "Released BLE link to Vent one at shutdown in" in record.getMessage()
        for record in caplog.records
    )


def test_the_job_keeps_the_entry_loaded(setup_env):
    """No unload, no entity removal: restore-state must stay intact."""

    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=True)
        await integration.async_setup_entry(hass, entry)
        await settle()
        (job,) = hass.shutdown_jobs
        await job.target()
        return hass, entry

    hass, entry = asyncio.run(scenario())
    assert "one" in hass.data[DOMAIN]
    assert entry.unload_callbacks, "the job must not run the entry's unload callbacks"


class HangingDevice:
    """A device whose disconnect never completes."""

    closing = False

    def begin_closing(self) -> None:
        self.closing = True

    async def async_stop_hold(self) -> None:
        pass

    async def stop(self) -> None:
        await asyncio.Event().wait()


class ExplodingDevice(HangingDevice):
    async def stop(self) -> None:
        raise RuntimeError("proxy went away")


def test_a_hanging_disconnect_is_bounded_and_never_raises(monkeypatch, caplog):
    monkeypatch.setattr(integration, "RELEASE_TIMEOUT", 0.05)
    device = HangingDevice()

    async def scenario():
        started = asyncio.get_running_loop().time()
        await integration._async_release_at_shutdown(FakeHass(), "Vent", device, None, None)
        return asyncio.get_running_loop().time() - started

    with caplog.at_level(logging.WARNING):
        elapsed = asyncio.run(scenario())

    assert elapsed < 2
    assert device.closing, "the latch is set even when the release gives up"
    assert any("did not finish within" in r.getMessage() for r in caplog.records)


def test_a_failing_disconnect_is_reported_and_never_raises(caplog):
    device = ExplodingDevice()
    with caplog.at_level(logging.WARNING):
        asyncio.run(integration._async_release_at_shutdown(FakeHass(), "Vent", device, None, None))
    assert any("proxy went away" in r.getMessage() for r in caplog.records)


def test_a_watcher_that_raises_does_not_stop_the_release(setup_env):
    class BrokenWatchdog:
        def async_stop(self) -> None:
            raise RuntimeError("boom")

    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=True)
        await integration.async_setup_entry(hass, entry)
        await settle()
        device = hass.data[DOMAIN]["one"].device
        await integration._async_release_at_shutdown(
            hass, "Vent", device, BrokenWatchdog(), None
        )
        return device

    device = asyncio.run(scenario())
    assert device.closing
    assert not device.is_connected, "the release still ran"


# ---------------------------------------------------------------------------
# The latch, on the paths that open links
# ---------------------------------------------------------------------------


class RecordingDevice(ACInfinityDevice):
    def __init__(self, ble) -> None:
        super().__init__(ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=1))
        self.connected = False
        self.polls = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def update(self) -> None:
        self.polls += 1


class PollHass:
    def __init__(self) -> None:
        self.is_stopping = False
        self.state = CoreState.running
        self.interval_timers: list = []
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


def build_coordinator():
    hass = PollHass()
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    device = RecordingDevice(ble)
    coordinator = ACInfinityDataUpdateCoordinator(
        hass, logging.getLogger("test"), ble, device
    )
    return coordinator, device, hass


def test_an_advertisement_no_longer_triggers_a_poll_once_latched(monkeypatch):
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    monkeypatch.setattr(
        coordinator_module.bluetooth,
        "async_ble_device_from_address",
        lambda *a, **k: ble,
    )

    async def scenario():
        coordinator, device, hass = build_coordinator()
        info = SimpleNamespace(device=ble)
        before = coordinator._needs_poll(info, None)
        device.begin_closing()
        return before, coordinator._needs_poll(info, None), hass.state

    before, after, state = asyncio.run(scenario())
    assert before is True, "control: an unlatched fan is polled"
    assert after is False
    assert state is CoreState.running, "gating must not depend on hass.state"


def test_a_live_link_is_not_polled_once_latched():
    async def scenario():
        coordinator, device, hass = build_coordinator()
        coordinator._async_start()
        device.connected = True
        device.begin_closing()
        (_interval, tick), = hass.interval_timers
        tick(None)
        device.hold_status.set_hold(True)  # a hold change also tries to poll
        if hass.tasks:
            await asyncio.gather(*hass.tasks)
        return device.polls, hass.tasks

    polls, tasks = asyncio.run(scenario())
    assert polls == 0
    assert tasks == []


def test_a_poll_already_in_flight_is_dropped_quietly_when_the_latch_lands(caplog):
    async def scenario():
        coordinator, device, hass = build_coordinator()

        async def refuse() -> None:
            raise LinkClosingError("shutting down")

        device.update = refuse
        with caplog.at_level(logging.WARNING):
            await coordinator._async_link_poll()

    asyncio.run(scenario())
    assert not caplog.records, "a refused poll at shutdown must not log a traceback"


def test_the_hold_supervisor_exits_instead_of_retrying_once_latched(monkeypatch):
    async def scenario():
        device = RecordingDevice(SimpleNamespace(address=ADDRESS, name="D-A6B2C"))
        attempts = []

        async def refuse() -> None:
            attempts.append(1)
            raise OSError("no slot")

        device._ensure_connected = refuse
        monkeypatch.setattr(
            "custom_components.ac_infinity.device.backoff_delay", lambda attempt: 0
        )
        device.async_start_hold()
        await settle()
        device.begin_closing()
        seen = len(attempts)
        await settle(50)
        task = device._hold_task
        return seen, len(attempts), task.done()

    seen, after, done = asyncio.run(scenario())
    assert done, "the supervisor must end, not keep backing off forever"
    assert after <= seen + 1


def test_a_connect_still_in_flight_when_the_latch_lands_is_torn_down(monkeypatch):
    log: list[str] = []

    async def slow_connect(self) -> None:
        await asyncio.sleep(0)  # the latch lands here
        self._client = FakeClient(log)

    monkeypatch.setattr(ACInfinityController, "_ensure_connected", slow_connect)

    async def scenario():
        device = ACInfinityDevice(
            SimpleNamespace(address=ADDRESS, name="D-A6B2C"),
            state=DeviceInfoEx(type=6, name="D-A6B2C", version=1),
        )
        attempt = asyncio.ensure_future(device._ensure_connected())
        await asyncio.sleep(0)
        device.begin_closing()
        with pytest.raises(LinkClosingError):
            await attempt
        return device

    device = asyncio.run(scenario())
    assert log == ["disconnect"], "the link opened mid-latch must be closed again"
    assert not device.is_connected


# ---------------------------------------------------------------------------
# release_link
# ---------------------------------------------------------------------------


class SlowDevice:
    """Records how many releases are in flight at once."""

    def __init__(self, group: "Group", hang: bool = False) -> None:
        self.group = group
        self.hang = hang
        self.closing = False
        self.stopped = False

    async def async_stop_hold(self) -> None:
        pass

    async def stop(self) -> None:
        self.group.in_flight += 1
        self.group.peak = max(self.group.peak, self.group.in_flight)
        if self.group.in_flight == self.group.size:
            self.group.all_in.set()
        try:
            if self.hang:
                await asyncio.Event().wait()
            await self.group.all_in.wait()
            self.stopped = True
        finally:
            self.group.in_flight -= 1

    def async_start_hold(self, scanner_name=None) -> None:
        pass


class Group:
    def __init__(self, size: int) -> None:
        self.size = size
        self.in_flight = 0
        self.peak = 0
        self.all_in = asyncio.Event()


def release_hass(devices):
    entries = [
        SimpleNamespace(
            entry_id=f"e{i}",
            title=f"Vent {i}",
            options={"hold_connection": True},
            data={"address": ADDRESS},
        )
        for i in range(len(devices))
    ]
    hass = SimpleNamespace(data={DOMAIN: {}})
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: list(entries))
    for entry, device in zip(entries, devices):
        hass.data[DOMAIN][entry.entry_id] = SimpleNamespace(device=device)
    return hass


def test_release_link_releases_every_entry_at_the_same_time(monkeypatch):
    # Serial release would park the first device on `all_in` forever; the
    # bound turns that into a clean failure instead of a hung suite.
    monkeypatch.setattr(integration, "RELEASE_TIMEOUT", 1)

    async def scenario():
        group = Group(4)
        devices = [SlowDevice(group) for _ in range(4)]
        await integration._async_release_links(release_hass(devices), 0)
        return group.peak, [d.stopped for d in devices]

    peak, stopped = asyncio.run(scenario())
    assert peak == 4
    assert all(stopped)


def test_one_hanging_fan_is_bounded_and_does_not_hold_up_the_others(monkeypatch):
    monkeypatch.setattr(integration, "RELEASE_TIMEOUT", 0.2)

    async def scenario():
        group = Group(3)
        devices = [SlowDevice(group, hang=True), SlowDevice(group), SlowDevice(group)]
        hass = release_hass(devices)
        started = asyncio.get_running_loop().time()
        await integration._async_release_links(hass, 180)
        elapsed = asyncio.get_running_loop().time() - started
        return elapsed, [d.stopped for d in devices], hass.pending_timers

    elapsed, stopped, timers = asyncio.run(scenario())
    assert elapsed < 2, "bounded by one timeout, not one per fan"
    assert stopped == [False, True, True]
    assert len(timers) == 1, "holds are still re-armed after a slow release"


def test_the_resume_timer_never_reopens_a_latched_fan():
    async def scenario():
        group = Group(1)
        device = SlowDevice(group)
        started = []
        device.async_start_hold = lambda scanner_name=None: started.append(1)
        hass = release_hass([device])
        await integration._async_release_links(hass, 180)
        device.closing = True  # the shutdown job ran inside the window
        (_delay, action), = hass.pending_timers
        await action(None)
        return started

    assert asyncio.run(scenario()) == []


# ---------------------------------------------------------------------------
# Addendum rules A-D: the latch outlives entries; setup refuses while latched
# ---------------------------------------------------------------------------


def run_domain_setup(hass):
    asyncio.run(integration.async_setup(hass, {}))


def test_a_the_domain_job_latches_and_cancels_pending_resume_timers():
    """Rule A: one job per HA run; it is not tied to any entry's lifetime."""
    hass = FakeHass()
    run_domain_setup(hass)
    cancelled = []
    hass.data.setdefault(DOMAIN, {})[integration.RESUME_TIMERS_KEY] = [
        lambda: cancelled.append("a"),
        lambda: cancelled.append("b"),
    ]
    assert not integration.shutting_down(hass)
    (job,) = hass.shutdown_jobs
    job.target()
    assert integration.shutting_down(hass)
    assert cancelled == ["a", "b"]
    assert integration.RESUME_TIMERS_KEY not in hass.data[DOMAIN]


def test_a_the_domain_job_survives_entry_unload(setup_env):
    async def scenario():
        hass = FakeHass()
        await integration.async_setup(hass, {})
        entry = FakeEntry("one", hold=False)
        await integration.async_setup_entry(hass, entry)
        entry.unload()
        return hass

    hass = asyncio.run(scenario())
    assert len(hass.shutdown_jobs) == 1, "only the domain latch job is left"


def test_a_a_resume_timer_armed_by_release_link_is_cancelled_by_the_latch(monkeypatch):
    monkeypatch.setattr(integration, "RELEASE_TIMEOUT", 1)

    async def scenario():
        hass = release_hass([SlowDevice(Group(1))])
        hass.shutdown_jobs = []
        hass.async_add_shutdown_job = lambda job: hass.shutdown_jobs.append(job)
        await integration.async_setup(hass, {})
        await integration._async_release_links(hass, 180)
        armed = list(hass.pending_timers)
        hass.shutdown_jobs[0].target()  # Stage 1 starts
        return armed, list(hass.pending_timers)

    armed, left = asyncio.run(scenario())
    assert len(armed) == 1 and left == []


def test_a_release_link_called_during_shutdown_arms_no_resume_timer(monkeypatch):
    async def scenario():
        hass = release_hass([SlowDevice(Group(1))])
        hass.data[DOMAIN][integration.SHUTDOWN_LATCH_KEY] = True
        await integration._async_release_links(hass, 180)
        return getattr(hass, "pending_timers", [])

    assert asyncio.run(scenario()) == []


def test_a_resume_timer_that_fires_during_shutdown_re_arms_nothing():
    """The timer may fire after the latch even if cancelling it raced."""

    async def scenario():
        group = Group(1)
        device = SlowDevice(group)
        started = []
        device.async_start_hold = lambda scanner_name=None: started.append(1)
        hass = release_hass([device])
        await integration._async_release_links(hass, 180)
        hass.data[DOMAIN][integration.SHUTDOWN_LATCH_KEY] = True
        (_delay, action), = hass.pending_timers
        await action(None)
        return started

    assert asyncio.run(scenario()) == []


def test_b_setup_refuses_while_latched_and_builds_nothing(setup_env, monkeypatch):
    """A retry or reload during Stage 1 must not make a fresh, unlatched device."""
    outages = []
    monkeypatch.setattr(integration, "async_link_down", lambda *a: outages.append(a))

    async def scenario():
        hass = FakeHass()
        await integration.async_setup(hass, {})
        hass.shutdown_jobs[0].target()
        entry = FakeEntry("one", hold=True)
        with pytest.raises(integration.ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)
        await settle()
        return hass, entry

    hass, entry = asyncio.run(scenario())
    assert setup_env.connects == [], "no hold, no connect"
    assert "one" not in hass.data[DOMAIN]
    assert len(hass.shutdown_jobs) == 1, "no per-entry job for a refused setup"
    assert entry.unload_callbacks == []
    assert outages == [], "a refused setup is not an outage"


def test_b_a_reload_after_the_job_ran_is_refused_too(setup_env):
    async def scenario():
        hass = FakeHass()
        await integration.async_setup(hass, {})
        entry = FakeEntry("one", hold=True)
        await integration.async_setup_entry(hass, entry)
        await settle()
        for job in list(hass.shutdown_jobs):
            result = job.target()
            if asyncio.iscoroutine(result):
                await result
        entry.unload()  # the reload's unload half
        with pytest.raises(integration.ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)  # ... and its setup
        await settle()

    asyncio.run(scenario())
    assert len(setup_env.connects) == 1, "only the original link was ever opened"


def test_b_shutdown_during_wait_ready_creates_no_watchers_and_releases(
    setup_env, monkeypatch
):
    """Rule B after the await, plus rule C: the job exists before any await."""
    built = []
    outages = []
    monkeypatch.setattr(integration, "async_link_down", lambda *a: outages.append(a))
    jobs_at_first_await = []
    hass_holder = []

    class Watchdog:
        def __init__(self, *args) -> None:
            built.append("watchdog")

    class Circulation:
        def __init__(self, *args) -> None:
            built.append("circulation")

    monkeypatch.setattr(integration, "ACInfinityLinkWatchdog", Watchdog)
    monkeypatch.setattr(integration, "CirculationController", Circulation)

    async def wait_ready(self, timeout) -> bool:
        hass = hass_holder[0]
        jobs_at_first_await.append(len(hass.shutdown_jobs))
        await settle()  # the hold supervisor connects in the meantime
        for job in list(hass.shutdown_jobs):  # Stage 1 lands mid-wait
            await job.target()
        return False  # would be the 'not advertising' branch for an unheld fan

    monkeypatch.setattr(FakeCoordinator, "async_wait_ready", wait_ready)

    async def scenario():
        hass = FakeHass()
        hass_holder.append(hass)
        entry = FakeEntry("one", hold=False)
        with pytest.raises(integration.ConfigEntryNotReady):
            await integration.async_setup_entry(hass, entry)
        return hass

    hass = asyncio.run(scenario())
    assert jobs_at_first_await == [1], "rule C: registered before the first await"
    assert built == [], "no watchdog or circulation after the release"
    assert outages == [], "shutdown is not an outage (and not a repair)"
    assert "one" not in hass.data.get(DOMAIN, {})


def test_b_shutdown_during_the_platform_forward_never_starts_circulation(
    setup_env, monkeypatch
):
    started = []

    class Circulation:
        def __init__(self, *args) -> None:
            pass

        def async_start(self) -> None:
            started.append("circulation")

        def async_stop(self) -> None:
            setup_env.watchers.log.append("circulation.async_stop")

    monkeypatch.setattr(integration, "CirculationController", Circulation)

    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=True)

        async def forward(entry_, platforms) -> None:
            await settle()
            for job in list(hass.shutdown_jobs):
                await job.target()

        hass.config_entries.async_forward_entry_setups = forward
        assert await integration.async_setup_entry(hass, entry)
        await settle()
        return hass.data[DOMAIN]["one"].device

    device = asyncio.run(scenario())
    assert started == []
    assert device.closing and not device.is_connected
    assert len(setup_env.connects) == 1


def test_a_every_entry_job_also_sets_the_domain_latch(setup_env):
    """release_link-style paths may have no domain job; the entry job covers it."""

    async def scenario():
        hass = FakeHass()
        entry = FakeEntry("one", hold=False)
        await integration.async_setup_entry(hass, entry)
        assert not integration.shutting_down(hass)
        await hass.shutdown_jobs[0].target()
        return integration.shutting_down(hass)

    assert asyncio.run(scenario()) is True
