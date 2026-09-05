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


class CallbackType(Enum):
    """Why a state callback fired.

    UPDATE_RESPONSE covers both a parsed get_model_data poll response and a
    state commit after a successful write command; either way the state now
    reflects a device-confirmed round-trip.
    """

    ADVERTISEMENT = 1
    NOTIFICATION = 2
    UPDATE_RESPONSE = 3
