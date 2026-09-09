"""Tests for the persistent-connection ("hold") decision logic.

The supervisor loop itself needs a BLE transport, but every decision it makes
is arithmetic in ``custom_components.ac_infinity.hold`` and is pinned here:
the reconnect ladder (a wrong cap or a missing rung means a fan that dropped
either hammers the proxies or waits hours), the jitter bounds (six fans that
all lost the same proxy must not retry in lockstep), and the trailing-hour
drop window that heal automations read off the Connection sensor.

Clocks are injected, so nothing here sleeps.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.ac_infinity.hold import (
    HOLD_BACKOFF_JITTER,
    HOLD_BACKOFF_SCHEDULE,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    HoldStatus,
    allocation_source_for_address,
    backoff_base,
    backoff_delay,
    connection_state,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeClock:
    """Manually advanced monotonic + wall clock pair."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = datetime(2026, 9, 8, 3, 0, 0, tzinfo=timezone.utc)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self.mono

    def utcnow(self) -> datetime:
        return self.wall


def make_status(clock: FakeClock) -> HoldStatus:
    return HoldStatus(monotonic=clock.monotonic, utcnow=clock.utcnow)


class TestBackoffSchedule:
    def test_ladder_is_1_2_5_10_30_60(self):
        assert [backoff_base(n) for n in range(1, 7)] == [1, 2, 5, 10, 30, 60]

    @pytest.mark.parametrize("attempt", [7, 8, 20, 500])
    def test_beyond_the_ladder_repeats_the_cap(self, attempt):
        """A fan out of range for a day retries once a minute forever, and
        never gives up: the hold is meant to outlive an outage."""
        assert backoff_base(attempt) == 60.0

    @pytest.mark.parametrize("attempt", [0, -1])
    def test_attempt_is_one_based(self, attempt):
        with pytest.raises(ValueError):
            backoff_base(attempt)

    def test_ladder_is_monotonically_non_decreasing(self):
        rungs = list(HOLD_BACKOFF_SCHEDULE)
        assert rungs == sorted(rungs)


class TestBackoffJitter:
    @pytest.mark.parametrize("attempt", range(1, 8))
    def test_jitter_stays_within_20_percent(self, attempt):
        base = backoff_base(attempt)
        low = backoff_delay(attempt, rand=lambda: 0.0)
        high = backoff_delay(attempt, rand=lambda: 1.0)
        assert low == pytest.approx(base * (1 - HOLD_BACKOFF_JITTER))
        assert high == pytest.approx(base * (1 + HOLD_BACKOFF_JITTER))

    def test_midpoint_is_the_scheduled_delay(self):
        assert backoff_delay(3, rand=lambda: 0.5) == pytest.approx(backoff_base(3))

    def test_real_randomness_is_bounded_and_spread(self):
        """Guards the default rand wiring: values must land inside the band
        and must not collapse to a single point (lockstep retries)."""
        samples = [backoff_delay(4) for _ in range(200)]
        assert all(8.0 <= value <= 12.0 for value in samples)
        assert len(set(samples)) > 1

    def test_delay_is_never_negative_or_zero(self):
        assert backoff_delay(1, rand=lambda: 0.0) > 0


class TestDropWindow:
    def test_no_drops_reports_zero_and_no_timestamp(self):
        status = make_status(FakeClock())
        assert status.drops_1h == 0
        assert status.last_drop is None

    def test_drops_inside_the_hour_are_counted(self):
        clock = FakeClock()
        status = make_status(clock)
        for _ in range(3):
            clock.advance(600)  # 10 min apart
            status.record_drop()
        assert status.drops_1h == 3

    def test_drops_older_than_an_hour_fall_out(self):
        clock = FakeClock()
        status = make_status(clock)
        status.record_drop()
        clock.advance(3599)
        assert status.drops_1h == 1
        clock.advance(2)  # now 3601s old
        assert status.drops_1h == 0

    def test_window_slides_rather_than_resets(self):
        """Two drops 50 minutes apart: 20 minutes later the first has aged
        out and the second has not."""
        clock = FakeClock()
        status = make_status(clock)
        status.record_drop()
        clock.advance(3000)
        status.record_drop()
        assert status.drops_1h == 2
        clock.advance(1200)
        assert status.drops_1h == 1

    def test_last_drop_survives_the_window(self):
        """The count is windowed; "when did this fan last drop" is not."""
        clock = FakeClock()
        status = make_status(clock)
        status.record_drop()
        expected = clock.utcnow().isoformat()
        clock.advance(7200)
        assert status.drops_1h == 0
        assert status.last_drop == expected

    def test_last_drop_is_iso_utc(self):
        clock = FakeClock()
        status = make_status(clock)
        status.record_drop()
        parsed = datetime.fromisoformat(status.last_drop)
        assert parsed.utcoffset() == timedelta(0)


class TestHoldStatusAttributes:
    def test_attribute_payload_shape(self):
        clock = FakeClock()
        status = make_status(clock)
        status.set_hold(True)
        status.set_reconnect_attempt(3)
        status.record_drop()
        assert status.as_attributes() == {
            "hold": True,
            "drops_1h": 1,
            "last_drop": clock.utcnow().isoformat(),
            "reconnect_attempt": 3,
        }

    def test_listeners_fire_on_change_only(self):
        status = make_status(FakeClock())
        calls = []
        unsub = status.add_listener(lambda: calls.append(1))
        status.set_hold(True)
        status.set_hold(True)  # no change, no notification
        status.set_reconnect_attempt(1)
        status.record_drop()
        assert len(calls) == 3
        unsub()
        status.set_reconnect_attempt(2)
        assert len(calls) == 3


class TestAllocationLookup:
    @staticmethod
    def allocation(source, allocated):
        return SimpleNamespace(source=source, slots=3, free=1, allocated=allocated)

    def test_finds_the_holding_proxy(self):
        allocations = [
            self.allocation("11:11:11:11:11:11", ["99:99:99:99:99:99"]),
            self.allocation("22:22:22:22:22:22", [ADDRESS]),
        ]
        assert (
            allocation_source_for_address(allocations, ADDRESS)
            == "22:22:22:22:22:22"
        )

    def test_case_insensitive(self):
        allocations = [self.allocation("22:22:22:22:22:22", [ADDRESS.lower()])]
        assert allocation_source_for_address(allocations, ADDRESS) is not None

    def test_absent_address_is_none(self):
        allocations = [self.allocation("22:22:22:22:22:22", [])]
        assert allocation_source_for_address(allocations, ADDRESS) is None

    @pytest.mark.parametrize("allocations", [None, []])
    def test_no_allocation_data_is_none(self, allocations):
        assert allocation_source_for_address(allocations, ADDRESS) is None

    def test_tolerates_none_allocated_list(self):
        allocations = [SimpleNamespace(source="x", slots=0, free=0, allocated=None)]
        assert allocation_source_for_address(allocations, ADDRESS) is None


class TestConnectionState:
    def test_disconnected_when_no_link(self):
        assert (
            connection_state(connected=False, scanner_name="proxy")
            == STATE_DISCONNECTED
        )

    def test_reports_the_scanner_name_when_held(self):
        assert (
            connection_state(connected=True, scanner_name="plant-room-bluetooth-proxy")
            == "plant-room-bluetooth-proxy"
        )

    def test_connected_but_unattributed_is_not_reported_as_disconnected(self):
        """A live link with no allocation entry (local adapter, or the beat
        before the allocation callback lands) must not read `disconnected` —
        heal automations treat that as "go fix this fan"."""
        assert (
            connection_state(connected=True, scanner_name=None) == STATE_CONNECTED
        )
