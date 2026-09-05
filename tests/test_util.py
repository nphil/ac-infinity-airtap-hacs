"""Unit tests for the vendored bit/CRC helpers.

These functions are transliterated from the vendor's Java app, so their
conventions are unusual (get_bit is INVERTED: True means the bit is CLEAR).
The tests pin those conventions because the parser and the auto-mode config
decoding both depend on them; a well-meaning "fix" of the inversion would
silently corrupt every decoded flag.
"""

from custom_components.ac_infinity.ac_infinity_ble.util import (
    crc16,
    get_bit,
    get_bits,
    get_short,
)


class TestCrc16:
    def test_known_ccitt_false_vector(self):
        """crc16 is CRC-16/CCITT-FALSE: the standard check value for
        '123456789' is 0x29B1.  This is an independent, published vector —
        if it holds, the implementation matches the wire protocol's CRC."""
        assert crc16([ord(c) for c in "123456789"]) == [0x29, 0xB1]

    def test_empty_input_is_initial_value(self):
        assert crc16([]) == [0xFF, 0xFF]

    def test_windowed_range_matches_slice(self):
        """The (i, i2) window form must equal running the CRC over the slice —
        _add_head relies on this to checksum the header and body regions
        of a frame in place."""
        data = [0xA5, 0x00, 0x12, 0x34, 0x56, 0x78, 0x9A]
        assert crc16(data, 2, 4) == crc16(data[2:6])

    def test_output_bytes_are_big_endian_pair(self):
        hi, lo = crc16([0x00])
        assert 0 <= hi <= 255 and 0 <= lo <= 255

    def test_partial_window_args_fall_back_to_full_range(self):
        """Passing only one of (i, i2) is treated as neither: full range."""
        data = [1, 2, 3]
        assert crc16(data, 1, None) == crc16(data)


class TestGetShort:
    def test_big_endian_positive(self):
        assert get_short(bytes([0x09, 0xE6]), 0) == 2534  # 25.34 C * 100

    def test_big_endian_negative(self):
        """Temperatures below zero arrive as signed 16-bit values."""
        assert get_short(bytes([0xFE, 0x00]), 0) == -512

    def test_offset_indexes_into_buffer(self):
        assert get_short(bytes([0, 0, 0x01, 0x00]), 2) == 256


class TestGetBit:
    def test_inverted_convention(self):
        """get_bit returns True when the bit is ZERO (vendor convention)."""
        assert get_bit(0b0100_0000, 1) is False  # bit set -> False
        assert get_bit(0b0000_0000, 1) is True  # bit clear -> True

    def test_index_counts_from_msb(self):
        assert get_bit(0b1000_0000, 0) is False
        assert get_bit(0b0000_0001, 7) is False


class TestGetBits:
    def test_extracts_field_from_msb_side(self):
        # bits 2-3 (from MSB) of 0b0011_0000 are 0b11
        assert get_bits(0b0011_0000, 2, 2) == 3

    def test_low_field(self):
        assert get_bits(0b0000_0010, 6, 2) == 2

    def test_full_byte(self):
        assert get_bits(0xA5, 0, 8) == 0xA5
