"""Tests for the get_model_data response parser and the registers behind it.

The frames below are VERBATIM captures from live AIRTAP T-series (type 6)
fans on 2026-09-10, taken from the vendored controller's own notification
log line.  They are the evidence for every offset this integration reads
past the AUTO threshold block, so they are pinned here rather than
hand-built: a synthetic frame could only ever confirm the parser agrees
with itself.

The register map the captures prove:

    16:1 work_type   19:7 AUTO thresholds   22:8 CYCLE on + off
    17:1 level_off   20:4 TIMER TO ON       23:0 absent on this model
    18:1 level_on    21:4 TIMER TO OFF
"""

import pytest

from custom_components.ac_infinity.ac_infinity_ble.protocol import parse_model_data
from custom_components.ac_infinity.device import (MAX_DURATION_SECONDS,
                                                  _duration, _duration_bytes)

# Living-room fan, running in AUTO (work_type 3) with min 0 / max 10 and
# thresholds 34C high / 16C low, both enabled, no timers configured.
CAPTURE_AUTO = bytes.fromhex(
    "a513002a00055713000110010311010012010a13070c5d223c10"
    "000014040000000015040000000016080000000000000000170051dc"
)
# A second fan in ON (work_type 2) with max level 8 — different values in the
# same layout, which is what proves the walk is not reading fixed offsets
# that happen to line up once.
CAPTURE_ON = bytes.fromhex(
    "a513002a00055713000110010211010012010813070c571f3f11"
    "00001404000000001504000000001608000000000000000017005175"
)


class TestParseModelData:
    def test_a_live_capture_splits_into_its_eight_groups(self):
        groups = parse_model_data(CAPTURE_AUTO)
        assert {op: value.hex() for op, value in groups.items()} == {
            16: "03",
            17: "00",
            18: "0a",
            19: "0c5d223c100000",
            20: "00000000",
            21: "00000000",
            22: "0000000000000000",
            23: "",
        }

    def test_the_same_layout_carries_different_values(self):
        groups = parse_model_data(CAPTURE_ON)
        assert (groups[16][0], groups[17][0], groups[18][0]) == (2, 0, 8)

    @pytest.mark.parametrize(
        "frame",
        [
            b"",
            b"\xa5\x13\x00\x2a",  # header only: the payload never arrived
            CAPTURE_AUTO[:20],  # truncated mid-payload
            bytes.fromhex("a5000000000000000000"),  # an ack: zero-length payload
        ],
        ids=["empty", "header-only", "truncated", "ack"],
    )
    def test_anything_that_is_not_a_whole_response_parses_to_nothing(self, frame):
        """Responses are not sequence-correlated, so a poll can be handed an
        ack or a stale frame; parsing one would poison work_type."""
        assert parse_model_data(frame) == {}

    def test_a_group_running_past_the_payload_is_rejected_whole(self):
        """Partial credit is the dangerous answer: the groups already read
        would be applied while the rest silently vanished."""
        frame = bytearray(CAPTURE_AUTO)
        frame[41] = 0xFF  # the CYCLE group now claims 255 bytes
        assert parse_model_data(bytes(frame)) == {}


class TestDurationCodec:
    def test_a_register_round_trips(self):
        assert _duration(bytes(_duration_bytes(5400))) == 5400

    def test_an_unanswered_register_stays_unknown(self):
        """None, not zero: a fan that never reported cannot be shown as 0."""
        assert _duration(None) is None
        assert _duration(b"") is None

    @pytest.mark.parametrize("seconds", [-1, MAX_DURATION_SECONDS + 60])
    def test_a_duration_the_panel_cannot_express_is_refused(self, seconds):
        with pytest.raises(ValueError):
            _duration_bytes(seconds)

    def test_the_bound_is_the_panels_own_maximum(self):
        assert MAX_DURATION_SECONDS == 23 * 3600 + 59 * 60 == 86340
        # Big-endian, four bytes wide, as the registers themselves are.
        assert _duration_bytes(MAX_DURATION_SECONDS) == [0x00, 0x01, 0x51, 0x44]
