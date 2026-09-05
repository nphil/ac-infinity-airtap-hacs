"""Tests for advertisement parsing and the type/mode lookup tables.

Fixtures are synthesized from the parser's own field layout (see
tests/conftest.py) — no captured payloads are required, and no byte outside
that layout is given meaning.
"""

import pytest

from custom_components.ac_infinity.ac_infinity_ble.protocol import (
    get_mode,
    get_type,
    parse_manufacturer_data,
)


class TestGetType:
    @pytest.mark.parametrize(
        ("type_id", "letter"),
        [
            (2, "B"),
            (3, "C"),
            (4, "C"),
            (5, "C"),
            (14, "C"),
            (15, "C"),
            (6, "D"),  # AIRTAP T-series
            (7, "E"),
            (8, "E"),
            (9, "F"),
            (12, "F"),
            (11, "G"),
            (1, "A"),  # anything unlisted falls back to A
            (99, "A"),
        ],
    )
    def test_family_letter(self, type_id, letter):
        assert get_type(type_id) == letter


class TestGetMode:
    @pytest.mark.parametrize(
        ("mode", "label"),
        [
            (1, "OFF"),
            (2, "ON"),
            (3, "AUTO"),
            (4, "TIMER ON"),
            (5, "TIMER OFF"),
            (6, "CYCLE"),
            (7, "SCHEDULE"),
            (8, "VPD"),
            (9, "TEMPERATURE PARAM"),
            (10, "HUMIDITY PARAM"),
            (11, "ADVANCE"),
            (12, "AI"),
        ],
    )
    def test_known_modes(self, mode, label):
        assert get_mode(mode) == label

    def test_unknown_mode_is_empty_string(self):
        assert get_mode(0) == ""
        assert get_mode(13) == ""


class TestParseManufacturerData:
    def test_airtap_basic_fields(self, adv_builder):
        data = adv_builder(
            name="A6B2C",
            version=1,
            device_type=6,
            is_degree=True,
            fan_state=2,
            tmp_state=1,
            hum_state=3,
            tmp_centi=2534,
            hum_centi=4875,
            fan=7,
        )
        info = parse_manufacturer_data(data)
        assert info.type == 6
        assert info.version == 1
        assert info.name == "D-A6B2C"  # family letter D + 5 ASCII chars
        assert info.is_degree is True
        assert info.fan_state == 2
        assert info.tmp_state == 1
        assert info.hum_state == 3
        assert info.tmp == pytest.approx(25.34)
        assert info.hum == pytest.approx(48.75)
        assert info.fan == 7

    def test_negative_temperature_is_signed(self, adv_builder):
        info = parse_manufacturer_data(adv_builder(tmp_centi=-512))
        assert info.tmp == pytest.approx(-5.12)

    def test_fan_zero_parses_as_zero_not_none(self, adv_builder):
        """A stopped fan is 0, not missing.  The state-merge in the device
        layer only overwrites non-None fields, so 0-vs-None here decides
        whether 'fan stopped' propagates to Home Assistant at all."""
        info = parse_manufacturer_data(adv_builder(fan=0))
        assert info.fan == 0

    def test_is_degree_false_when_flag_clear(self, adv_builder):
        info = parse_manufacturer_data(adv_builder(is_degree=False))
        assert info.is_degree is False

    def test_airtap_type6_has_no_vpd_section(self, adv_builder):
        """Type 6 (AIRTAP) never gets the extended vpd/choose_port section,
        even at version >= 3 — that block is gated to E/F/G family types."""
        info = parse_manufacturer_data(
            adv_builder(device_type=6, version=3, vpd_state=2, vpd_centi=123)
        )
        assert info.vpd is None
        assert info.vpd_state is None
        assert info.choose_port is None

    def test_efamily_v3_parses_vpd_section(self, adv_builder):
        info = parse_manufacturer_data(
            adv_builder(
                device_type=7,
                version=3,
                choose_port=2,
                vpd_state=1,
                vpd_centi=142,
            )
        )
        assert info.choose_port == 2
        assert info.vpd_state == 1
        assert info.vpd == pytest.approx(1.42)

    def test_efamily_v2_skips_vpd_section(self, adv_builder):
        info = parse_manufacturer_data(adv_builder(device_type=7, version=2))
        assert info.vpd is None

    def test_advertisement_never_carries_work_type(self, adv_builder):
        """work_type / level_on / level_off only come from GATT reads.  The
        coordinator merge depends on the parser leaving them None so a fresh
        advertisement cannot wipe mode state learned from a poll."""
        info = parse_manufacturer_data(adv_builder())
        assert info.work_type is None
        assert info.level_on is None
        assert info.level_off is None

    def test_short_payload_raises(self, adv_builder):
        """18 bytes cannot contain the fan level byte at index 18; the parser
        must fail loudly rather than fabricate state."""
        with pytest.raises((IndexError, ValueError)):
            parse_manufacturer_data(adv_builder()[:18])
