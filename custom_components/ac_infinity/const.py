from bleak.exc import BleakError

DOMAIN = "ac_infinity"

MANUFACTURER = "AC Infinity"

DEVICE_TIMEOUT = 30
UPDATE_SECONDS = 15

BLEAK_EXCEPTIONS = (AttributeError, BleakError, TimeoutError)

# Per-entry option: keep the GATT link (and one ESPHome proxy connection
# slot) open permanently so commands skip the 1.8-6.4 s connect. Entries
# created before the option existed have no options dict at all, so the
# default is what they get.
CONF_HOLD_CONNECTION = "hold_connection"
DEFAULT_HOLD_CONNECTION = True

DEVICE_MODEL = {1: "Controller 67",
                6: "Airtap Series",
                7: "Controller 69",
                11: "Controller 69 Pro"}

FAMILY_E_MODELS = {7, 9, 11, 12}
