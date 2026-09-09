"""Decision logic for the persistent-connection ("hold") supervisor.

Deliberately free of Home Assistant *and* Bluetooth imports: everything here
is arithmetic and bookkeeping, so it is unit-testable on a bare interpreter
(see tests/test_hold.py).  The moving parts that need a transport live in
``device.ACInfinityDevice._hold_supervisor`` and in the vendored controller's
connection management.

WHY a hold at all: every command previously paid a fresh ESPHome-proxy
connect (measured 1.8-6.4 s) because the code hung up after each round-trip.
Holding the GATT link open costs one proxy connection slot per fan and drops
command latency to ~150-300 ms.  The trade is documented in the README and
switchable per config entry.
"""
from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

# Reconnect ladder, in seconds, for consecutive failed reconnect attempts.
# Attempt N (1-based) waits SCHEDULE[N-1]; attempts past the end repeat the
# last rung, so the worst case settles at one attempt per minute forever
# rather than giving up on a device that is merely out of range for a while.
HOLD_BACKOFF_SCHEDULE: tuple[int, ...] = (1, 2, 5, 10, 30, 60)

# +-20% jitter. Six fans that all lost their proxy at the same moment (proxy
# reboot, AP flap) would otherwise retry in lockstep forever and keep
# colliding for the same handful of connection slots.
HOLD_BACKOFF_JITTER = 0.2

# Consecutive-failure interval for the WARNING log. Every failure is logged
# at DEBUG; one in ten is escalated so a persistently unreachable fan is
# visible in a default-level log without flooding it.
HOLD_FAILURE_LOG_EVERY = 10

# Trailing window for the drops_1h attribute.
DROP_WINDOW_SECONDS = 3600.0

# Sensor states. "connected" is only used when the link is genuinely up but
# no scanner has claimed the address in habluetooth's allocation table (a
# local adapter, or the instant between connecting and the allocation
# callback landing); reporting "disconnected" there would be a lie.
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backoff_base(attempt: int) -> float:
    """Un-jittered reconnect delay in seconds for a 1-based attempt number."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return float(HOLD_BACKOFF_SCHEDULE[min(attempt, len(HOLD_BACKOFF_SCHEDULE)) - 1])


def backoff_delay(
    attempt: int, *, rand: Callable[[], float] = random.random
) -> float:
    """Jittered reconnect delay for a 1-based attempt number.

    ``rand`` returns a float in [0, 1) (``random.random``'s contract); the
    result therefore lands in [0.8, 1.2] x the scheduled delay.
    """
    return backoff_base(attempt) * (1.0 + HOLD_BACKOFF_JITTER * (2.0 * rand() - 1.0))


def allocation_source_for_address(allocations: Any, address: str) -> str | None:
    """Return the scanner source currently holding ``address``.

    ``allocations`` is whatever ``habluetooth`` reports from
    ``async_current_allocations()``: a list of objects with ``source`` and
    ``allocated`` (addresses), or None when nothing has reported yet.
    Comparison is case-insensitive because scanners are inconsistent about
    MAC casing.
    """
    if not allocations:
        return None
    wanted = address.upper()
    for allocation in allocations:
        for allocated in allocation.allocated or ():
            if allocated.upper() == wanted:
                return allocation.source
    return None


def connection_state(*, connected: bool, scanner_name: str | None) -> str:
    """State string for the Connection diagnostic sensor."""
    if not connected:
        return STATE_DISCONNECTED
    return scanner_name or STATE_CONNECTED


class HoldStatus:
    """Live hold bookkeeping shared by the device and the Connection sensor.

    Counts *unexpected* disconnects only: the supervisor records a drop when
    the device (or a dying proxy link) takes the connection away, never when
    the integration lets go on purpose.  habluetooth does not count
    post-connect drops against a proxy's path score, so this counter is the
    only place the fleet's real link stability is visible.

    The clocks are injectable so the trailing-window arithmetic is testable
    without sleeping; ``monotonic`` drives the window (immune to system clock
    changes) while ``utcnow`` produces the human-readable ``last_drop``.
    """

    def __init__(
        self,
        *,
        window: float = DROP_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._window = window
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._drops: deque[float] = deque()
        self._last_drop: datetime | None = None
        self._hold = False
        self._reconnect_attempt = 0
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to hold-status changes; returns the unsubscribe callable."""

        def remove_listener() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        self._listeners.append(listener)
        return remove_listener

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    @property
    def hold(self) -> bool:
        """Whether a persistent hold is active for this device."""
        return self._hold

    def set_hold(self, hold: bool) -> None:
        if self._hold != hold:
            self._hold = hold
            self._notify()

    @property
    def reconnect_attempt(self) -> int:
        """1-based number of the reconnect attempt in flight; 0 when connected."""
        return self._reconnect_attempt

    def set_reconnect_attempt(self, attempt: int) -> None:
        if self._reconnect_attempt != attempt:
            self._reconnect_attempt = attempt
            self._notify()

    def record_drop(self) -> None:
        """Record one unexpected disconnect."""
        self._drops.append(self._monotonic())
        self._last_drop = self._utcnow()
        self._notify()

    def _prune(self) -> None:
        cutoff = self._monotonic() - self._window
        drops = self._drops
        while drops and drops[0] < cutoff:
            drops.popleft()

    @property
    def drops_1h(self) -> int:
        """Unexpected disconnects in the trailing window."""
        self._prune()
        return len(self._drops)

    @property
    def last_drop(self) -> str | None:
        """ISO-8601 UTC timestamp of the most recent drop, or None.

        Never pruned: the window only governs the *count*, and "when did this
        fan last lose its link" stays useful long after the hour is up.
        """
        if self._last_drop is None:
            return None
        return self._last_drop.isoformat()

    def as_attributes(self) -> dict[str, Any]:
        """Attribute payload for the Connection diagnostic sensor."""
        return {
            "hold": self._hold,
            "drops_1h": self.drops_1h,
            "last_drop": self.last_drop,
            "reconnect_attempt": self._reconnect_attempt,
        }
