"""The pairing snapshot seeds identity, never readings.

CONF_SERVICE_DATA is the advertisement seen when a fan was paired. Setting
up from it verbatim made every held fan report its pairing-day speed after
each restart or reload, with nothing to correct it: a held fan sends no
manufacturer data. The dict below is the Master Bedroom vent's stored
snapshot exactly as its diagnostics showed it on 2026-09-25; that vent read
60 % (the stored fan level 6) from each restart until its next link drop.
"""

import custom_components.ac_infinity as integration

MASTER_BEDROOM_SNAPSHOT = {
    "auto_mode": None,
    "choose_port": None,
    "fan": 6,
    "fan_state": 0,
    "fan_type": None,
    "hum": 0.0,
    "hum_state": 0,
    "is_degree": False,
    "level_off": None,
    "level_on": None,
    "name": "D-AE942",
    "tmp": 13.65,
    "tmp_state": 0,
    "type": 6,
    "version": 3,
    "vpd": None,
    "vpd_state": None,
    "work_type": None,
}


def test_pairing_day_readings_do_not_become_live_state():
    state = integration._runtime_state_from_entry_data(MASTER_BEDROOM_SNAPSHOT)
    assert (state.fan, state.tmp, state.hum, state.fan_state) == (None, None, None, None)


def test_the_identity_setup_needs_survives():
    state = integration._runtime_state_from_entry_data(MASTER_BEDROOM_SNAPSHOT)
    assert (state.type, state.name, state.version) == (6, "D-AE942", 3)
