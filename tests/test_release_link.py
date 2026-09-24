"""release_link must never re-arm a device object its entry no longer owns.

Observed 2026-09-22..24: two vents that were continuously connected logged
~900 "Still unable to hold a BLE connection" attempts. release_link had
captured each entry's data, the entry was reloaded inside the resume window,
and the timer then re-armed the captured OLD device - starting a supervisor
that no unload could ever cancel, fighting the live one for a fan that accepts
a single connection.
"""

import asyncio
from types import SimpleNamespace

import custom_components.ac_infinity as integration
from custom_components.ac_infinity.const import DOMAIN


class FakeDevice:
    def __init__(self) -> None:
        self.hold_starts = 0
        self.stopped = False

    async def async_stop_hold(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True

    def async_start_hold(self, scanner_name=None) -> None:
        self.hold_starts += 1


def make_entry(entry_id: str):
    return SimpleNamespace(
        entry_id=entry_id,
        title=entry_id,
        options={"hold_connection": True},
        data={"address": "AA:BB:CC:DD:EE:FF"},
    )


def make_hass(entries):
    hass = SimpleNamespace(data={DOMAIN: {}})
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: list(entries))
    for entry in entries:
        hass.data[DOMAIN][entry.entry_id] = SimpleNamespace(device=FakeDevice())
    return hass


def fire_resume(hass) -> None:
    (_delay, action), = hass.pending_timers
    asyncio.run(action(None))


def test_unchanged_entry_is_re_armed_when_no_restart_follows() -> None:
    entry = make_entry("vent")
    hass = make_hass([entry])
    device = hass.data[DOMAIN]["vent"].device

    asyncio.run(integration._async_release_links(hass, 180))
    fire_resume(hass)

    assert device.stopped
    assert device.hold_starts == 1


def test_reload_inside_the_window_never_re_arms_the_stale_device() -> None:
    entry = make_entry("vent")
    hass = make_hass([entry])
    stale = hass.data[DOMAIN]["vent"].device

    asyncio.run(integration._async_release_links(hass, 180))
    # The entry reloads (options flow, manual reload): same id, new objects,
    # and the new device's own setup already started its supervisor.
    live = FakeDevice()
    hass.data[DOMAIN]["vent"] = SimpleNamespace(device=live)
    fire_resume(hass)

    assert stale.hold_starts == 0, "a supervisor was started on an orphaned device"
    assert live.hold_starts == 0, "the live device owns its hold; resume must not touch it"


def test_unload_inside_the_window_re_arms_nothing() -> None:
    entry = make_entry("vent")
    hass = make_hass([entry])
    device = hass.data[DOMAIN]["vent"].device

    asyncio.run(integration._async_release_links(hass, 180))
    del hass.data[DOMAIN]["vent"]
    fire_resume(hass)

    assert device.hold_starts == 0
