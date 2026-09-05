"""Tests for BLE command frame construction.

HARDWARE SAFETY: these tests assert the frame structure the code itself
defines (header, length, sequence, CRC placement, command byte) so that any
accidental change to framing is caught before it reaches a live fan.  They do
NOT invent new command bytes — payload expectations are copied from the
builders under test.
"""

import pytest

from custom_components.ac_infinity.ac_infinity_ble.protocol import Protocol
from custom_components.ac_infinity.ac_infinity_ble.util import crc16


def frame_fields(frame: bytes) -> dict:
    """Decode a frame using the layout _add_head defines.

    [0:2] head 0xA5 0x00; [2:4] payload length (BE); [4:6] sequence (BE);
    [6:8] CRC16 of bytes 0-5; [8] zero; [9] command type; [10:10+n] payload;
    [-2:] CRC16 of bytes 8..8+n+1.
    """
    length = (frame[2] << 8) | frame[3]
    return {
        "head": list(frame[0:2]),
        "length": length,
        "sequence": (frame[4] << 8) | frame[5],
        "header_crc": list(frame[6:8]),
        "reserved": frame[8],
        "command_type": frame[9],
        "payload": list(frame[10 : 10 + length]),
        "trailer_crc": list(frame[10 + length : 12 + length]),
    }


def assert_well_formed(frame: bytes) -> dict:
    """A frame is self-consistent iff both embedded CRCs verify."""
    f = frame_fields(frame)
    assert f["head"] == [0xA5, 0x00]
    assert len(frame) == f["length"] + 12
    assert f["reserved"] == 0
    assert f["header_crc"] == crc16(list(frame), 0, 6)
    assert f["trailer_crc"] == crc16(list(frame), 8, f["length"] + 2)
    return f


class TestAddHead:
    def test_frame_layout_and_crcs(self):
        frame = Protocol()._add_head([1, 2, 3], 3, 0x1234)
        f = assert_well_formed(frame)
        assert f["length"] == 3
        assert f["sequence"] == 0x1234
        assert f["command_type"] == 3
        assert f["payload"] == [1, 2, 3]

    def test_sequence_is_big_endian(self):
        frame = Protocol()._add_head([0], 1, 0xABCD)
        assert frame[4] == 0xAB
        assert frame[5] == 0xCD


class TestGetModelData:
    def test_basic_read_command(self):
        frame = Protocol().get_model_data(6, 0, 1)
        f = assert_well_formed(frame)
        assert f["command_type"] == 1  # read
        assert f["payload"] == [16, 17, 18, 19, 20, 21, 22, 23]

    def test_efamily_appends_port_selector(self):
        frame = Protocol().get_model_data(7, 4, 1)
        f = assert_well_formed(frame)
        assert f["payload"] == [16, 17, 18, 19, 20, 21, 22, 23, 255, 4]

    @pytest.mark.parametrize("type_id", [7, 9, 11, 12])
    def test_all_efamily_types_get_selector(self, type_id):
        f = frame_fields(Protocol().get_model_data(type_id, 0, 1))
        assert f["payload"][-2:] == [255, 0]


class TestSetLevel:
    def test_on_command_payload(self):
        frame = Protocol().set_level(6, 2, 5, 0, 7)
        f = assert_well_formed(frame)
        assert f["command_type"] == 3  # write
        assert f["sequence"] == 7
        # [16, 1, work_type, work_type+16, 1, level] per the builder
        assert f["payload"] == [16, 1, 2, 18, 1, 5]

    def test_off_command_payload(self):
        f = assert_well_formed(Protocol().set_level(6, 1, 0, 0, 1))
        assert f["payload"] == [16, 1, 1, 17, 1, 0]

    def test_efamily_appends_port_selector(self):
        f = assert_well_formed(Protocol().set_level(7, 2, 5, 3, 1))
        assert f["payload"] == [16, 1, 2, 18, 1, 5, 255, 3]

    @pytest.mark.parametrize("work_type", [0, 3, 4])
    def test_rejects_non_manual_work_types(self, work_type):
        """set_level only speaks OFF(1)/ON(2).  AUTO(3) has its own dedicated
        command path; passing it here must fail instead of emitting a frame
        the fan would misinterpret."""
        with pytest.raises(ValueError):
            Protocol().set_level(6, work_type, 5, 0, 1)

    @pytest.mark.parametrize("level", [-1, 11, 100])
    def test_rejects_out_of_range_levels(self, level):
        with pytest.raises(ValueError):
            Protocol().set_level(6, 2, level, 0, 1)

    @pytest.mark.parametrize("level", [0, 10])
    def test_accepts_level_bounds(self, level):
        assert_well_formed(Protocol().set_level(6, 2, level, 0, 1))
