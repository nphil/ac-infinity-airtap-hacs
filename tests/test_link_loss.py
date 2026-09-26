"""Entities must learn that the held link is gone.

Live on 2026-09-25 the Master Bedroom vent lost power for 90 minutes. Its
Connection sensor went unavailable at the drop, but the fan entity kept
reading "on, 60 %" and the temperature kept its last value the whole time,
while the integration's own diagnostics said the coordinator was
unavailable. Availability is "link up, or seen advertising recently"; a held
fan advertises so rarely that the tracker had already given up, the link was
the only thing keeping the entities available, and losing it re-rendered
nothing but the Connection sensor, which has a hold listener of its own.

Contract pinned here: a drop is given LINK_LOSS_GRACE to be explained by an
advertisement (a powered fan sends one within about a second of any
disconnect); when it runs out the entities are re-rendered against the
truth. Reconnecting inside the window cancels it.
"""

import asyncio

from custom_components.ac_infinity.coordinator import LINK_LOSS_GRACE
from tests.test_coordinator_events import CHANGE, frame
from tests.test_link_poll import build


def held_then_dropped():
    """A coordinator whose held link came up, the tracker gave up, then it dropped."""
    coordinator, device, hass = build()
    coordinator._async_start()
    device.connected = True
    device.hold_status.set_reconnect_attempt(1)
    device.hold_status.set_reconnect_attempt(0)
    # The advertisement tracker already declared the quiet held fan gone;
    # only the live link was keeping the entities available.
    coordinator._available = False
    assert coordinator.available
    renders = coordinator.listener_update_count
    device.connected = False
    device.hold_status.record_drop()
    return coordinator, device, hass, renders


def grace_timers(hass):
    return [
        action
        for delay, action in getattr(hass, "pending_timers", [])
        if delay == LINK_LOSS_GRACE
    ]


class TestLinkLoss:
    def test_a_silent_fan_goes_unavailable_when_the_grace_runs_out(self):
        async def scenario():
            coordinator, _device, hass, renders = held_then_dropped()
            during = coordinator.available
            (expire,) = grace_timers(hass)
            expire(None)
            return during, coordinator.available, coordinator.listener_update_count - renders

        during, after, renders = asyncio.run(scenario())
        assert during is True, "a drop is not a verdict until the grace runs out"
        assert after is False
        assert renders >= 1, "the entities must be told, or they keep their last state"

    def test_an_advertisement_inside_the_grace_keeps_the_fan_available(self):
        async def scenario():
            coordinator, _device, hass, _renders = held_then_dropped()
            coordinator._async_handle_bluetooth_event(frame({}), CHANGE)
            (expire,) = grace_timers(hass)
            expire(None)
            return coordinator.available

        assert asyncio.run(scenario()) is True

    def test_reconnecting_inside_the_grace_cancels_it(self):
        async def scenario():
            coordinator, device, hass, _renders = held_then_dropped()
            device.hold_status.set_reconnect_attempt(1)
            device.connected = True
            device.hold_status.set_reconnect_attempt(0)
            return grace_timers(hass), coordinator.available

        timers, available = asyncio.run(scenario())
        assert timers == []
        assert available is True

    def test_stop_cancels_a_pending_grace(self):
        async def scenario():
            coordinator, _device, hass, _renders = held_then_dropped()
            coordinator._async_stop()
            return grace_timers(hass)

        assert asyncio.run(scenario()) == []
