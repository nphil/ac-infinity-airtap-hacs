"""Constants for the vendored AC Infinity BLE library."""
from enum import Enum

# Bluetooth SIG company identifier AC Infinity uses in manufacturer data
# (0x0902); advertisements without this key are not AC Infinity devices.
MANUFACTURER_ID = 2306

# Vendor GATT characteristics, in preference order: newer firmware exposes
# the 70D5... custom service, older firmware the ff01/ff02 shorthand pair.
POSSIBLE_WRITE_CHARACTERISTIC_UUIDS = [
    "70D51001-2C7F-4E75-AE8A-D758951CE4E0",
    "0000ff01-0000-1000-8000-00805f9b34fb",
]
POSSIBLE_READ_CHARACTERISTIC_UUIDS = [
    "70D51002-2C7F-4E75-AE8A-D758951CE4E0",
    "0000ff02-0000-1000-8000-00805f9b34fb",
]

# Device types whose unsolicited 1E FF notification carries the LIVE fan
# level (high nibble of byte 17). Upstream disabled that read as "Not
# accurate"; on the AIRTAP T-series it is the only live source there is while
# a GATT link is held, because a held fan advertises by name alone, with no
# manufacturer data. Measured 2026-09-25 on six type-6 fans (firmware v3), one
# frame a second each: fans in ON mode reported exactly their stored ON level
# (10 and 8), fans in AUTO with a trigger active reported their AUTO maximum
# (10, 10 and 9), and a fan with an AUTO transition ramp tracked it level by
# level (6 -> 9 as the duct air cooled). Other types stay unproven, so their
# frames keep the upstream behaviour.
LIVE_LEVEL_NOTIFICATION_TYPES = frozenset({6})


class CallbackType(Enum):
    """Why a state callback fired.

    UPDATE_RESPONSE covers both a parsed get_model_data poll response and a
    state commit after a successful write command; either way the state now
    reflects a device-confirmed round-trip.
    """

    ADVERTISEMENT = 1
    NOTIFICATION = 2
    UPDATE_RESPONSE = 3
