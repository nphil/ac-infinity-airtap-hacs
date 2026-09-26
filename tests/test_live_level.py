"""The live fan level carried by the 1E FF notification.

Measured live on 2026-09-25: while a GATT link is held, an AIRTAP advertises
by name alone, with no manufacturer data, so the advertised level byte never
arrives and the fan's reported speed froze at whatever the last
manufacturer-data advertisement (or the pairing snapshot) said. The Master
Bedroom vent read 60 % all day while it idled or ramped by itself.

The notification the fan sends once a second while the link is up carries
the level in the high nibble of byte 17, next to work_type in the low nibble.
Upstream distrusted it; on type 6 it was exact on every fan. The frames below
are VERBATIM captures from the vendored controller's "Notification received"
debug line, each with the context that makes its level checkable (the idle
one was taken with the AC switched off: as the duct air warmed back into the
fans' AUTO ranges every AUTO fan stepped down to 0, three of them within
15 s of crossing the trigger and the Master Bedroom vent a level at a time
along its transition ramp).
"""

import asyncio

import pytest

from custom_components.ac_infinity.ac_infinity_ble import CallbackType
from tests.test_vendored_controller import airtap_state, make_controller

# (frame, live level, work_type, why that level is the right answer)
CAPTURES = [
    (
        "1eff0209030c0000065700000000271000a2",
        10,
        2,
        "Nitin's Office: ON mode, stored ON level 10",
    ),
    (
        "1eff0209030c0000052a0000000027100082",
        8,
        2,
        "Plant Room: ON mode, stored ON level 8",
    ),
    (
        "1eff0209030c0000046b0000000027100093",
        9,
        3,
        "Living Room: AUTO, duct air 11.3 C past its 13 C low trigger, "
        "so it runs at its AUTO maximum of 9",
    ),
    (
        "1eff0209030c0000050a0000000027100073",
        7,
        3,
        "Master Bedroom: AUTO with a transition ramp, at 7 on its way up",
    ),
    (
        "1eff0209030c0000055c0000000027100003",
        0,
        3,
        "Living Room: AUTO back inside its range (duct air 13.7 C, above its "
        "13 C low trigger), idling at its minimum of 0",
    ),
]


def receive(frame_hex: str, **state):
    async def scenario():
        controller = make_controller(airtap_state(**state))
        controller._notification_handler(None, bytearray.fromhex(frame_hex))
        return controller

    return asyncio.run(scenario())


class TestLiveLevel:
    @pytest.mark.parametrize(
        "frame, level, work_type",
        [capture[:3] for capture in CAPTURES],
        ids=[capture[3] for capture in CAPTURES],
    )
    def test_each_capture_reports_the_level_the_fan_was_running_at(
        self, frame, level, work_type
    ):
        controller = receive(frame)
        assert (controller.state.fan, controller.state.work_type) == (level, work_type)
        assert controller.events == [CallbackType.NOTIFICATION]

    def test_a_notification_replaces_a_stale_level(self):
        """THE regression: the old level (here the pairing-day 6) must not
        survive the fan's own report of what it is doing now."""
        controller = receive(CAPTURES[0][0], fan=6)
        assert controller.state.fan == 10

    def test_unmeasured_device_types_keep_the_upstream_reading(self):
        """Only type 6 was measured; other models must not start trusting a
        nibble nobody has checked on them."""
        controller = receive(CAPTURES[0][0], type=7, fan=4)
        assert (controller.state.fan, controller.state.work_type) == (4, 2)
