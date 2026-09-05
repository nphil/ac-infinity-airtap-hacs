"""Shared test setup.

Installs the Home Assistant stubs (when real HA is absent) *before* any test
module imports ``custom_components``, and exposes fixture helpers for building
synthetic AC Infinity BLE advertisements.

The advertisement builder mirrors the byte layout that
``ac_infinity_ble.protocol.parse_manufacturer_data`` reads, so tests construct
inputs from the same field map the parser documents rather than hand-copied
hex blobs.  NEVER extend it with fields the parser does not read — fixtures
must not invent protocol.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests import ha_stubs  # noqa: E402

HA_STUBBED = ha_stubs.install()


def build_manufacturer_data(
    *,
    name: str = "12345",
    version: int = 1,
    device_type: int = 6,
    is_degree: bool = True,
    fan_state: int = 0,
    tmp_state: int = 0,
    hum_state: int = 0,
    tmp_centi: int = 0,
    hum_centi: int = 0,
    fan: int = 0,
    choose_port: int = 0,
    vpd_state: int = 0,
    vpd_centi: int = 0,
    length: int = 27,
) -> bytes:
    """Build a synthetic manufacturer-data payload for MANUFACTURER_ID 2306.

    Field map (index -> meaning), as read by parse_manufacturer_data:
      [6:11]  5 ASCII chars appended to the family letter for the name
      [11]    version
      [12]    device type
      [13]    flags: bit6 = degree display, bits 5-4 = fan_state,
              bits 3-2 = tmp_state, bits 1-0 = hum_state
      [14:16] temperature, signed big-endian, hundredths of a degree C
      [16:18] humidity, signed big-endian, hundredths of a percent
      [18]    current fan level (0-10)
      [19]    choose_port           (version >= 3, E-family types only)
      [20]    bits 7-6 = vpd_state  (same gate)
      [21:23] vpd, hundredths of kPa (same gate)
    """
    if len(name) != 5:
        raise ValueError("device name payload is exactly 5 ASCII bytes")
    data = bytearray(length)
    data[6:11] = name.encode("ascii")
    data[11] = version
    data[12] = device_type
    data[13] = (
        (0x40 if is_degree else 0)
        | ((fan_state & 0x3) << 4)
        | ((tmp_state & 0x3) << 2)
        | (hum_state & 0x3)
    )
    data[14:16] = tmp_centi.to_bytes(2, "big", signed=True)
    data[16:18] = hum_centi.to_bytes(2, "big", signed=True)
    data[18] = fan
    if length > 19:
        data[19] = choose_port
        data[20] = (vpd_state & 0x3) << 6
        data[21:23] = vpd_centi.to_bytes(2, "big", signed=True)
    return bytes(data)


@pytest.fixture
def adv_builder():
    return build_manufacturer_data
