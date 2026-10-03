"""A proxy that stalled an attempt must not be handed the next one.

habluetooth scores a connection path as RSSI minus ``0.51 x rssi-gap`` per
recorded failure and forgets the failures on the next successful connect.  Two
idle proxies at -50 and -70 dBm: the one that just hung scores -60.2 and still
beats -70, so both the preferred-proxy affinity and habluetooth's default pick
send the retry straight back to it (and a connect that succeeds then stalls in
the subscribe clears its own penalty each time, so it can loop forever).

Pinned here:

* ``StalledProxies`` expires, and a working link ends the skip at once;
* the excluded scanner is left out of the default pick AND the preferred pick,
  by filtering BEFORE habluetooth's selector runs (so it never reserves a slot
  for a scanner it then discards), only while another connectable route exists;
* the Connection sensor's ``via_preferred_proxy`` follows every pick;
* setup wires a stall to the scanner the failing attempt went through, keeps
  the record across entry reloads, and forgets it when the entry is removed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bleak.exc import BleakError

import custom_components.ac_infinity as integration
from custom_components.ac_infinity.ble_affinity import make_affinity_client_class
from custom_components.ac_infinity.const import CONF_PREFERRED_PROXY, DOMAIN
from custom_components.ac_infinity.proxy_health import (
    STALL_AVOID_SECONDS,
    StalledProxies,
)
from tests.test_shutdown_release import (
    FakeEntry,
    FakeHass,
    setup_env,  # noqa: F401 - fixture
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# StalledProxies
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_stalled_proxy_is_excluded_until_the_window_ends():
    clock = Clock()
    stalled = StalledProxies(clock=clock)
    assert not stalled.is_excluded("proxy-a")
    stalled.record("proxy-a")
    assert stalled.is_excluded("proxy-a")
    assert not stalled.is_excluded("proxy-b")
    clock.now += STALL_AVOID_SECONDS - 1
    assert stalled.is_excluded("proxy-a")
    clock.now += 2
    assert not stalled.is_excluded("proxy-a"), "the skip is temporary"


def test_a_link_through_the_proxy_ends_its_exclusion_at_once():
    stalled = StalledProxies(clock=Clock())
    stalled.record("proxy-a")
    stalled.record("proxy-b")
    stalled.clear("proxy-a")
    assert not stalled.is_excluded("proxy-a")
    assert stalled.is_excluded("proxy-b"), "another proxy's record is its own"


def test_the_window_is_short_enough_to_retry_the_proxy_beside_the_fan():
    assert STALL_AVOID_SECONDS <= 120


def test_an_unknown_source_is_never_excluded():
    stalled = StalledProxies(clock=Clock())
    stalled.record("proxy-a")
    assert not stalled.is_excluded(None)


# ---------------------------------------------------------------------------
# The affinity wrapper, against habluetooth's real scoring shape
# ---------------------------------------------------------------------------


class Connector:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def can_connect(self) -> bool:
        return self.ok


class Scanner:
    def __init__(self, name: str, rssi: int, *, failures=0, free_slot=True) -> None:
        self.name = name
        self.adapter = name
        self.source = f"mac-{name}"
        self.rssi = rssi
        self.failures = failures
        self.free_slot = free_slot
        self.connector = Connector(free_slot)

    def connection_failures(self, address: str) -> int:
        return self.failures


class ScannerDevice:
    def __init__(self, scanner: Scanner) -> None:
        self.scanner = scanner
        self.ble_device = f"ble-device-{scanner.name}"
        self.advertisement = SimpleNamespace(rssi=scanner.rssi)


class Manager:
    """habluetooth's manager as far as selection touches it, with slot accounting."""

    def __init__(self, scanners: list[Scanner]) -> None:
        self.scanners = scanners
        self.allocated: list[str] = []
        self.other = "reachable through the proxy"

    def async_scanner_devices_by_address(self, address: str, connectable: bool):
        return [ScannerDevice(s) for s in self.scanners]

    def async_allocate_connection_slot(self, ble_device) -> bool:
        scanner = next(s for s in self.scanners if f"ble-device-{s.name}" == ble_device)
        if not scanner.free_slot:
            return False
        self.allocated.append(scanner.name)
        return True


class RealisticBase:
    """The shape of ``HaBleakClientWrapper``'s selection.

    The default pick sorts by score = rssi - 0.51 x (rssi gap) x failures,
    then builds a backend for the first scanner that can take a connection;
    building one reserves a slot (a local adapter does) so a pick discarded
    afterwards would leak it.  ``self.__address`` lives under the wrapper's
    mangled name.
    """

    def __init__(self, address: str) -> None:
        self._HaBleakClientWrapper__address = address

    def _async_get_best_available_backend_and_device(self, manager):
        devices = manager.async_scanner_devices_by_address(
            self._HaBleakClientWrapper__address, True
        )
        by_rssi = sorted(devices, key=lambda d: d.advertisement.rssi, reverse=True)
        gap = (
            by_rssi[0].advertisement.rssi - by_rssi[1].advertisement.rssi
            if len(by_rssi) > 1
            else 0
        )
        scored = sorted(
            devices,
            key=lambda d: d.advertisement.rssi
            - max(gap, 1) * d.scanner.failures * 0.51,
            reverse=True,
        )
        for device in scored:
            backend = self._async_get_backend_for_ble_device(
                manager, device.scanner, device.ble_device
            )
            if backend is not None:
                return backend
        raise BleakError("No backend with an available connection slot")

    def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
        if not manager.async_allocate_connection_slot(ble_device):
            return None
        return SimpleNamespace(scanner=scanner, ble_device=ble_device)


class Routing:
    """One affinity client class wired the way async_setup_entry wires it."""

    def __init__(self, scanners, *, preferred=None, stalled_sources=()):
        self.manager = Manager(scanners)
        self.stalled = StalledProxies(clock=Clock())
        for source in stalled_sources:
            self.stalled.record(source)
        self.preferred = preferred
        self.choices: list[tuple[str, bool]] = []
        self.selected: list[str] = []
        client_class = make_affinity_client_class(
            RealisticBase,
            lambda: self.preferred,
            on_choice=lambda name, used: self.choices.append((name, used)),
            is_excluded=lambda scanner: self.stalled.is_excluded(scanner.source),
            on_selected=lambda scanner: self.selected.append(scanner.name),
        )
        self.client = client_class(ADDRESS)

    def pick(self):
        return self.client._async_get_best_available_backend_and_device(self.manager)


NEAR = "near-proxy"
FAR = "far-proxy"


def two_proxies(**near_kwargs):
    return [Scanner(NEAR, -50, **near_kwargs), Scanner(FAR, -70)]


def test_control_without_exclusion_habluetooth_re_picks_the_proxy_that_just_failed():
    """The bug: one recorded failure (-50 - 20 x 0.51 = -60.2) still beats -70."""
    routing = Routing(two_proxies(failures=1))
    assert routing.pick().scanner.name == NEAR


def test_default_routing_leaves_out_a_stalled_proxy():
    routing = Routing(two_proxies(failures=1), stalled_sources=[f"mac-{NEAR}"])
    assert routing.pick().scanner.name == FAR
    assert routing.selected == [FAR]


def test_a_discarded_pick_never_reserves_a_slot():
    """Filter BEFORE habluetooth allocates: picking first and discarding after
    leaks the slot the discarded backend reserved."""
    routing = Routing(two_proxies(), stalled_sources=[f"mac-{NEAR}"])
    routing.pick()
    assert routing.manager.allocated == [FAR]


def test_a_fan_with_one_route_still_uses_it():
    routing = Routing([Scanner(NEAR, -50)], stalled_sources=[f"mac-{NEAR}"])
    assert routing.pick().scanner.name == NEAR
    assert routing.manager.allocated == [NEAR]


def test_when_every_route_is_excluded_the_best_one_is_used():
    routing = Routing(
        two_proxies(), stalled_sources=[f"mac-{NEAR}", f"mac-{FAR}"]
    )
    assert routing.pick().scanner.name == NEAR


def test_an_excluded_route_beats_no_route_when_the_others_have_no_slot():
    scanners = [Scanner(NEAR, -50), Scanner(FAR, -70, free_slot=False)]
    routing = Routing(scanners, stalled_sources=[f"mac-{NEAR}"])
    assert routing.pick().scanner.name == NEAR
    assert routing.manager.allocated == [NEAR]


def test_the_exclusion_ends_by_itself():
    routing = Routing(two_proxies(), stalled_sources=[f"mac-{NEAR}"])
    assert routing.pick().scanner.name == FAR
    routing.stalled._clock.now += STALL_AVOID_SECONDS + 1
    assert routing.pick().scanner.name == NEAR


def test_a_stalled_preferred_proxy_is_skipped_and_the_sensor_is_told():
    """P2: the pick that routed around the preferred proxy is reported as not
    preferred, so via_preferred_proxy cannot keep the previous attempt's True."""
    routing = Routing(two_proxies(), preferred=NEAR)
    assert routing.pick().scanner.name == NEAR
    assert routing.choices == [(NEAR, True)]

    routing.stalled.record(f"mac-{NEAR}")
    assert routing.pick().scanner.name == FAR
    assert routing.choices[-1] == (FAR, False)

    routing.stalled.clear(f"mac-{NEAR}")  # a link came up through it
    assert routing.pick().scanner.name == NEAR
    assert routing.choices[-1] == (NEAR, True)
    assert routing.manager.allocated == [NEAR, FAR, NEAR], "one slot per pick"


def test_an_excluded_preferred_proxy_that_is_the_only_route_is_still_used():
    routing = Routing([Scanner(NEAR, -50)], preferred=NEAR)
    routing.stalled.record(f"mac-{NEAR}")
    assert routing.pick().scanner.name == NEAR
    assert routing.choices[-1] == (NEAR, False), "default routing chose it"


def test_without_a_preference_the_exclusion_still_applies_and_nothing_is_reported():
    """Automatic never reports a choice (affinity is not in play), but it must
    still be steered away from a proxy that stalled."""
    routing = Routing(two_proxies(failures=1), stalled_sources=[f"mac-{NEAR}"])
    assert routing.pick().scanner.name == FAR
    assert routing.choices == []


# ---------------------------------------------------------------------------
# Wiring in async_setup_entry
# ---------------------------------------------------------------------------


class Capture:
    """make_affinity_client_class stand-in that keeps the callbacks setup passes."""

    def __init__(self, monkeypatch) -> None:
        self.kwargs: dict = {}
        self.getter = None

        def capture(base, getter, **kwargs):
            self.getter = getter
            self.kwargs = kwargs
            return base

        monkeypatch.setattr(integration, "make_affinity_client_class", capture)


def set_up(hass, entry_id="one", preferred="plant-room-proxy"):
    entry = FakeEntry(entry_id, hold=False)
    entry.options[CONF_PREFERRED_PROXY] = preferred
    asyncio.get_running_loop()  # fail loudly outside a loop
    return entry


def test_a_stall_is_recorded_against_the_scanner_the_attempt_went_through(
    setup_env, monkeypatch  # noqa: F811
):
    capture = Capture(monkeypatch)

    async def scenario():
        hass = FakeHass()
        entry = set_up(hass)
        await integration.async_setup_entry(hass, entry)
        device = hass.data[DOMAIN]["one"].device
        near = SimpleNamespace(source="mac-near", name="near")
        far = SimpleNamespace(source="mac-far", name="far")
        excluded = capture.kwargs["is_excluded"]

        capture.kwargs["on_selected"](near)
        device._on_link_stalled()
        before = (excluded(near), excluded(far))

        capture.kwargs["on_selected"](far)
        device._on_link_ready()  # a link came up through the other proxy
        after_other = (excluded(near), excluded(far))

        capture.kwargs["on_selected"](near)
        device._on_link_ready()  # ... and now through the one that stalled
        return before, after_other, excluded(near)

    before, after_other, near_after_link = asyncio.run(scenario())
    assert before == (True, False)
    assert after_other == (True, False), "another proxy's success is not this one's"
    assert near_after_link is False


def test_a_stall_before_any_pick_blames_nobody(setup_env, monkeypatch):  # noqa: F811
    capture = Capture(monkeypatch)

    async def scenario():
        hass = FakeHass()
        await integration.async_setup_entry(hass, set_up(hass))
        device = hass.data[DOMAIN]["one"].device
        device._on_link_stalled()
        device._on_link_ready()
        return capture.kwargs["is_excluded"](SimpleNamespace(source=None))

    assert asyncio.run(scenario()) is False


def test_the_record_survives_a_reload_and_goes_with_the_entry(
    setup_env, monkeypatch  # noqa: F811
):
    capture = Capture(monkeypatch)

    async def scenario():
        hass = FakeHass()
        entry = set_up(hass)
        await integration.async_setup_entry(hass, entry)
        capture.kwargs["on_selected"](SimpleNamespace(source="mac-near"))
        hass.data[DOMAIN]["one"].device._on_link_stalled()

        entry.unload()  # the reload's unload half ...
        hass.data[DOMAIN].pop("one", None)
        reloaded = set_up(hass)
        await integration.async_setup_entry(hass, reloaded)  # ... and its setup
        survived = capture.kwargs["is_excluded"](SimpleNamespace(source="mac-near"))

        await integration.async_remove_entry(hass, reloaded)
        return survived, hass.data[DOMAIN].get(integration.STALLED_PROXIES_KEY)

    survived, store = asyncio.run(scenario())
    assert survived is True, "autoheal reloads must not forget a stalled proxy"
    assert store == {}


def test_setup_keeps_the_preference_plain_and_reports_picks(setup_env, monkeypatch):  # noqa: F811
    """The preferred-proxy getter is the live option again, with no one-shot
    bypass that could leave the sensor reporting a stale choice."""
    capture = Capture(monkeypatch)

    async def scenario():
        hass = FakeHass()
        entry = set_up(hass)
        await integration.async_setup_entry(hass, entry)
        device = hass.data[DOMAIN]["one"].device
        device.hold_status.set_via_preferred_proxy(True)
        capture.kwargs["on_choice"]("far", False)
        entry.options[CONF_PREFERRED_PROXY] = ""
        return capture.getter(), device.hold_status.via_preferred_proxy

    getter_value, via = asyncio.run(scenario())
    assert getter_value is None
    assert via is False
