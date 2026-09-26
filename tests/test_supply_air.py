"""Average temperature of the air a vent delivers while cooling or heating.

Only the steady part of a cycle counts: the first SETTLE_SECONDS are the
ducts cooling down (measured: 61-68 F in the first 8 minutes against
54-63 F after). A thermostat that drops off Wi-Fi pauses a cycle rather
than ending it.
"""

import pytest

from custom_components.ac_infinity.supply_air import (MIN_MEASURED,
                                                      SETTLE_SECONDS,
                                                      ConditionedAirAverage)


def feed(avg: ConditionedAirAverage, readings, step=30):
    """(start, end, temperature) spans, sampled every ``step`` seconds."""
    for start, end, temperature in readings:
        for t in range(start, end, step):
            avg.on_temperature(temperature, t)


class TestCycle:
    def test_only_the_steady_part_is_averaged(self):
        avg = ConditionedAirAverage("cooling")
        avg.on_action("cooling", 0)
        feed(avg, [(0, SETTLE_SECONDS, 22.0), (SETTLE_SECONDS, 900, 13.0)])
        assert avg.on_action("fan", 900) == pytest.approx(13.0)

    def test_a_cycle_too_short_to_settle_publishes_nothing(self):
        avg = ConditionedAirAverage("cooling")
        avg.on_action("cooling", 0)
        end = SETTLE_SECONDS + MIN_MEASURED - 30
        feed(avg, [(0, end, 14.0)])
        assert avg.on_action("idle", end) is None

    def test_a_thermostat_gap_pauses_and_the_cycle_continues(self):
        avg = ConditionedAirAverage("cooling")
        avg.on_action("cooling", 0)
        feed(avg, [(0, 600, 13.0)])
        avg.on_action(None, 600)  # ecobee offline
        feed(avg, [(600, 1200, 30.0)])  # not counted: nobody knows the action
        avg.on_action("cooling", 1200)
        feed(avg, [(1200, 1500, 13.0)])
        assert avg.on_action("idle", 1500) == pytest.approx(13.0)

    def test_heating_ignores_cooling(self):
        avg = ConditionedAirAverage("heating")
        avg.on_action("cooling", 0)
        feed(avg, [(0, 900, 13.0)])
        assert avg.on_action("idle", 900) is None
