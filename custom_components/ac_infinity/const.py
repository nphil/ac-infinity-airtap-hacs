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

# Per-entry option: prefer routing this fan's GATT connections through one
# named ESPHome proxy (its node name, i.e. habluetooth's `scanner.adapter` -
# the same identity CONF_LAST_HOLDING_PROXY records) instead of whichever
# scanner Home Assistant's own RSSI-based scorer picks on every reconnect.
# Falls back to that default routing after a bounded run of failures through
# the named proxy; see ble_affinity.py for the mechanism and why it exists.
# Empty string (the default) means automatic: no preference, today's
# behavior unchanged.
CONF_PREFERRED_PROXY = "preferred_proxy"
DEFAULT_PREFERRED_PROXY = ""

# Per-entry bookkeeping options. Written by the integration itself, never
# offered in the options flow: the options flow must therefore merge rather
# than replace, or one hold toggle would wipe both.
#
# CONF_LAST_HOLDING_PROXY: the ESPHome proxy that last carried this fan's
# link. While the fan is unreachable NOTHING holds it, so the recovery
# wizard cannot discover a proxy at Fix time — which is exactly when it
# needs to offer restarting one.
# CONF_RECOVERY_OUTLET: the switch the operator last used to power-cycle
# this fan, remembered so the last resort is one click next time.
CONF_LAST_HOLDING_PROXY = "last_holding_proxy"
CONF_RECOVERY_OUTLET = "recovery_outlet"

# Per-entry options behind the HA-side settings (see circulation.py). Written
# by the integration's own number entities and the options flow; none of them
# reloads the entry except a thermostat change.
#
# CONF_THERMOSTAT: climate entity whose hvac_action says when the blower
# runs. Also what the cold/warm air sensors key their measurements on.
# CONF_REST_SPEED / CONF_CIRCULATION_SPEED: the AUTO minimum (0-10) with the
# blower off / running. CONF_CIRCULATION_HOLD: minutes after the blower
# stops before the resting speed returns.
CONF_THERMOSTAT = "thermostat"
CONF_REST_SPEED = "rest_speed"
CONF_CIRCULATION_SPEED = "circulation_speed"
CONF_CIRCULATION_HOLD = "circulation_hold"
DEFAULT_CIRCULATION_HOLD = 20

DEVICE_MODEL = {1: "Controller 67",
                6: "Airtap Series",
                7: "Controller 69",
                11: "Controller 69 Pro"}

FAMILY_E_MODELS = {7, 9, 11, 12}
