"""Regression tests for polling driven by the held GATT link.

Measured live on 2026-09-10: six fans, all held, all "receiving
advertisements" — and zero GATT polls in the 25 minutes after a restart.
Holding the link is what silences the advertisements, and polling in the
``ActiveBluetoothDataUpdateCoordinator`` family is advertisement-driven, so
the poll cycle stopped exactly on the fans it was needed for.  Four of the
six showed ``unknown`` for every auto-mode number and switch (work_type,
speed bounds and the threshold block are GATT-only), and every attempt to
write a threshold raised ``ValueError: Auto mode configuration is not
loaded`` because the read that populates it had never run.

Contract pinned here: while a link is up, a poll happens on connect and then
on the interval — sharing the base coordinator's ``_last_poll`` clock with
the advertisement path so the two never double-poll — and a down link is
skipped rather than forced (connecting is the hold supervisor's job).
"""

import asyncio
import logging
from types import SimpleNamespace

from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import ACInfinityDevice, DeviceInfoEx

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeHass:
    """Just enough hass for the link-poll path: timers plus task creation."""

    def __init__(self) -> None:
        self.is_stopping = False
        self.interval_timers: list = []
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name):
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task


class RecordingDevice(ACInfinityDevice):
    """Controller whose connectivity is set by the test and whose polls count."""

    def __init__(self, ble) -> None:
        super().__init__(ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=1))
        self.connected = False
        self.polls = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def update(self) -> None:
        self.polls += 1


def build() -> tuple[ACInfinityDataUpdateCoordinator, RecordingDevice, FakeHass]:
    hass = FakeHass()
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    device = RecordingDevice(ble)
    coordinator = ACInfinityDataUpdateCoordinator(
        hass, logging.getLogger("test"), ble, device
    )
    return coordinator, device, hass


async def drain(hass: FakeHass) -> None:
    if hass.tasks:
        await asyncio.gather(*hass.tasks)
        hass.tasks.clear()


class TestLinkDrivenPolling:
    def test_a_live_link_is_polled_without_any_advertisement(self):
        """THE regression: a held fan advertises nothing, and must still poll."""

        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = True
            # No bluetooth frame is ever dispatched — the hold status change
            # a reconnect produces is the only trigger.
            device.hold_status.set_reconnect_attempt(0)
            device.hold_status.set_hold(True)
            await drain(hass)
            return device.polls

        assert asyncio.run(scenario()) == 1

    def test_the_interval_timer_polls_a_quiet_held_link(self):
        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = True
            # Only the recorded interval timer fires; nothing else happens.
            (_interval, tick), = hass.interval_timers
            tick(None)
            await drain(hass)
            return device.polls

        assert asyncio.run(scenario()) == 1

    def test_a_down_link_is_not_polled(self):
        """Connecting is the hold supervisor's job; a poll must not force one."""

        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = False
            (_interval, tick), = hass.interval_timers
            tick(None)
            await drain(hass)
            return device.polls

        assert asyncio.run(scenario()) == 0

    def test_a_recent_poll_suppresses_the_next_tick(self):
        """Shared clock with the advertisement path: no double-polling."""

        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = True
            (_interval, tick), = hass.interval_timers
            tick(None)
            await drain(hass)
            first = device.polls
            tick(None)
            await drain(hass)
            return first, device.polls

        assert asyncio.run(scenario()) == (1, 1)

    def test_our_own_config_write_re_syncs_immediately(self):
        """A threshold write sets the fast-track flag; the next tick polls."""

        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = True
            (_interval, tick), = hass.interval_timers
            tick(None)
            await drain(hass)
            device._config_changed_since_last_update = True
            tick(None)
            await drain(hass)
            return device.polls

        assert asyncio.run(scenario()) == 2

    def test_stop_cancels_the_timer_and_the_listener(self):
        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            coordinator._async_stop()
            device.connected = True
            device.hold_status.set_reconnect_attempt(0)
            await drain(hass)
            return hass.interval_timers, device.polls

        assert asyncio.run(scenario()) == ([], 0)

    def test_a_failing_poll_is_swallowed_and_retried(self):
        async def scenario():
            coordinator, device, hass = build()
            coordinator._async_start()
            device.connected = True

            failures = []

            async def failing_update():
                failures.append(1)
                raise RuntimeError("proxy went away mid-read")

            device.update = failing_update
            (_interval, tick), = hass.interval_timers
            tick(None)
            await drain(hass)
            # The clock still advanced, so the very next tick is suppressed;
            # what matters is that nothing propagated out of the task.
            device._config_changed_since_last_update = True
            tick(None)
            await drain(hass)
            return len(failures)

        assert asyncio.run(scenario()) == 2
