"""Average temperature of the conditioned air a fan delivers.

The fan's thermistor sits in the airflow, so while the HVAC is cooling (or
heating) it reads the supply air that reaches this particular register. How
cold that air is, compared vent to vent, is the evidence for duct losses.

The first minutes of a cycle are left out: the ducts start at room
temperature and take minutes to cool down. Measured over 15 days of cooling
(2026-09-10..25), the first 8 minutes of each cycle averaged 61-68 F at the
six vents against 54-63 F for the rest of the cycle. Only the steady part
counts, and a cycle with less than MIN_MEASURED of it is dropped rather than
published as a number mostly made of transition.

The thermostat's Wi-Fi drops out a few times a week. A gap pauses the
measurement instead of ending the cycle: if the same action is still
running when it comes back, the cycle simply continues; if not, the cycle
is closed with what was measured before the gap.
"""
from __future__ import annotations

SETTLE_SECONDS = 8 * 60
MIN_MEASURED = 2 * 60
# A reading older than this does not stand in for the air between samples
# (held links notify once a second; this only matters across a stall).
MAX_SAMPLE_GAP = 60


class ConditionedAirAverage:
    """Time-weighted average of the steady part of each cycle of one action."""

    def __init__(self, action: str) -> None:
        self.action = action
        self._cycle_start: float | None = None
        self._paused = False
        self._last: tuple[float, float] | None = None  # (temperature, time)
        self._sum = 0.0
        self._weight = 0.0

    def on_action(self, action: str | None, now: float) -> float | None:
        """Feed the thermostat's hvac_action (None: thermostat unavailable).

        Returns the cycle's average when this ends one worth publishing.
        """
        if action is None:
            self._take(now)
            self._paused = True
            self._last = None
            return None
        if action == self.action:
            if self._cycle_start is None:
                self._cycle_start = now
                self._sum = self._weight = 0.0
                self._last = None
            return None
        if self._cycle_start is None:
            return None
        self._take(now)
        result = self._sum / self._weight if self._weight >= MIN_MEASURED else None
        self._cycle_start = None
        self._last = None
        return result

    def on_temperature(self, temperature: float | None, now: float) -> None:
        """Feed a reading from the fan's thermistor."""
        if self._cycle_start is None or self._paused or temperature is None:
            self._last = None
            return
        self._take(now)
        self._last = (temperature, now)

    @property
    def measured_seconds(self) -> float:
        return self._weight

    def _take(self, now: float) -> None:
        """Credit the previous reading with the steady time it stood for."""
        if self._last is None or self._cycle_start is None:
            return
        value, since = self._last
        start = max(since, self._cycle_start + SETTLE_SECONDS)
        end = min(now, since + MAX_SAMPLE_GAP)
        if end > start:
            self._sum += value * (end - start)
            self._weight += end - start
