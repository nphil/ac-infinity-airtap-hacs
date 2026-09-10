"""Behaviour tests for the unreachable watchdog and the recovery wizard.

Two failures are pinned here, in the order they bite.

THE ORPHAN BUG (observed in the sibling fluvalble integration on 2026-09-09:
an issue raised at 12:22 was still open 13 hours after its condition cleared
at 13:00, because the delete was gated on an in-memory flag that the 12:38
entry reload reset).  Reconciliation must therefore compare the issue against
the link's actual state, unconditionally, and must run at setup — not only on
a transition it may have missed while unloaded.

THE THRESHOLD.  Six fans share this code, so the issue, the 15-minute timer
and the remembered proxy all belong to one config entry; an unreachable fan
must not raise five extra repairs.

The real coordinator, device, watchdog and fix flow run.  Home Assistant is
the stub layer (tests/ha_stubs.py), whose issue registry is a real in-memory
registry and whose ``async_call_later`` records the timer so a 15-minute
threshold can be reached without waiting for it.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import custom_components.ac_infinity as integration
import custom_components.ac_infinity.coordinator as coordinator_module
import custom_components.ac_infinity.repairs as repairs_module
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.ac_infinity.config_flow import OptionsFlowHandler
from custom_components.ac_infinity.const import (
    CONF_HOLD_CONNECTION,
    CONF_LAST_HOLDING_PROXY,
    CONF_RECOVERY_OUTLET,
    DOMAIN,
)
from custom_components.ac_infinity.coordinator import (
    UNREACHABLE_AFTER,
    ACInfinityDataUpdateCoordinator,
    ACInfinityLinkWatchdog,
    unreachable_issue_id,
)
from custom_components.ac_infinity.device import ACInfinityDevice, DeviceInfoEx
from custom_components.ac_infinity.models import ACInfinityData
from custom_components.ac_infinity.repairs import async_create_fix_flow

ADDRESS = "AA:BB:CC:DD:EE:FF"
OTHER_ADDRESS = "11:22:33:44:55:66"
ISSUE_ID = unreachable_issue_id(ADDRESS)
PROXY = "plant-room-bluetooth-proxy"
PROXY_ACTION = "plant_room_bluetooth_proxy_restart_proxy"
PROXY_SOURCE = "54:32:04:3E:F3:72"
# habluetooth names a remote scanner "<node> (<MAC>)" — verified live via
# bluetooth/subscribe_scanner_details on 2026-09-09.  Slugified whole, it
# matches no ESPHome action.
PROXY_SCANNER_NAME = f"{PROXY} ({PROXY_SOURCE})"


class FakeClient:
    """A live GATT link, as far as ``controller.is_connected`` can tell."""

    is_connected = True


class FakeEntry:
    def __init__(self, entry_id: str, title: str, address: str, **options) -> None:
        self.entry_id = entry_id
        self.title = title
        self.data = {"address": address}
        self.options = dict(options)
        self.state = ConfigEntryState.LOADED
        self.disabled_by = None


class FakeConfigEntries:
    def __init__(self, *entries: FakeEntry) -> None:
        self._entries = list(entries)
        self.reloads: list[str] = []
        self.option_writes: list[tuple[str, dict]] = []

    def async_entries(self, domain=None):
        return list(self._entries)

    def async_get_entry(self, entry_id):
        return next((e for e in self._entries if e.entry_id == entry_id), None)

    def async_update_entry(self, entry, *, options=None, **kwargs):
        if options is not None:
            entry.options = dict(options)
            self.option_writes.append((entry.entry_id, entry.options))

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)


class FakeServices:
    """Faithful to the two ServiceRegistry members the wizard uses."""

    def __init__(self, services: dict | None = None) -> None:
        self._services = services or {}
        self.calls: list[tuple[str, str, dict | None]] = []
        # (domain, service) pairs that raise once, to exercise error paths.
        self.fail: set[tuple[str, str]] = set()

    def has_service(self, domain, service):
        return service in self._services.get(domain, {})

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append((domain, service, data))
        if (domain, service) in self.fail:
            self.fail.discard((domain, service))
            raise HomeAssistantError(f"{domain}.{service} unavailable")


class FakeHass:
    def __init__(self, *entries: FakeEntry, services: dict | None = None) -> None:
        self.data = {}
        self.config_entries = FakeConfigEntries(*entries)
        self.services = FakeServices(services)


class Timeline:
    """The monotonic clock the outage maths reads, plus the timers armed on it.

    A timer fires only once the clock has actually reached its deadline, so
    a test advances time the way the night did.  Firing whatever was recorded
    (the stub's approach) cannot tell "raised 15 minutes after the first
    drop" from "raised 15 minutes after the last reload" — the exact
    distinction the autoheal measurement turned on.
    """

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        self.timers: list[tuple[float, Callable]] = []
        monkeypatch.setattr(coordinator_module, "monotonic", lambda: self.now)
        monkeypatch.setattr(coordinator_module, "async_call_later", self._call_later)

    def _call_later(self, hass, delay, action):
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else delay
        timer = (self.now + seconds, action)
        self.timers.append(timer)

        def cancel() -> None:
            if timer in self.timers:
                self.timers.remove(timer)

        return cancel

    @property
    def deadlines(self) -> list[float]:
        """Absolute clock readings the pending timers fire at."""
        return sorted(when for when, _action in self.timers)

    @property
    def remaining(self) -> list[float]:
        """Seconds from now until each pending timer fires."""
        return [when - self.now for when in self.deadlines]

    def advance(self, seconds: float) -> None:
        self.now += seconds
        while due := sorted(t for t in self.timers if t[0] <= self.now):
            for timer in due:
                self.timers.remove(timer)
                timer[1](datetime.now(timezone.utc))


THRESHOLD = UNREACHABLE_AFTER.total_seconds()


@pytest.fixture(autouse=True)
def timeline(monkeypatch) -> Timeline:
    return Timeline(monkeypatch)


def reload(hass: FakeHass, entry: FakeEntry, watchdog: ACInfinityLinkWatchdog, *, connected: bool):
    """What async_unload_entry then async_setup_entry do to one fan's runtime
    objects: the watchdog is stopped, the runtime data dropped, and a fresh
    device/coordinator/watchdog trio is built and started."""
    watchdog.async_stop()
    hass.data[DOMAIN].pop(entry.entry_id)
    device, coordinator, watchdog = build(hass, entry, connected=connected)
    watchdog.async_start()
    return device, coordinator, watchdog


def build(hass: FakeHass, entry: FakeEntry, *, connected: bool, hold: bool = True):
    """Wire up a real device/coordinator/watchdog trio for ``entry``.

    ``hold`` mirrors the CONF_HOLD_CONNECTION option: on (as it is on all six
    live fans) ``link_healthy`` is the GATT link itself, off it is
    advertisement availability.
    """

    async def _build():
        ble = SimpleNamespace(address=entry.data["address"], name="D-A6B2C")
        device = ACInfinityDevice(
            ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=1)
        )
        device.hold_status.set_hold(hold)
        if connected:
            device._client = FakeClient()
        coordinator = ACInfinityDataUpdateCoordinator(
            hass, logging.getLogger("test"), ble, device
        )
        watchdog = ACInfinityLinkWatchdog(hass, entry, coordinator)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = ACInfinityData(
            entry.title, device, coordinator, watchdog
        )
        return device, coordinator, watchdog

    return asyncio.run(_build())


def reconnect(device: ACInfinityDevice) -> None:
    """Reproduce what the hold supervisor does on a successful reconnect.

    Both writes matter: the supervisor announces the attempt before calling
    _ensure_connected and clears it after, and HoldStatus only notifies on a
    change — so 1 then 0 is the notification pair a real reconnect produces.
    """
    device.hold_status.set_reconnect_attempt(1)
    device._client = FakeClient()
    device.hold_status.set_reconnect_attempt(0)


def drop(device: ACInfinityDevice) -> None:
    """Reproduce a lost link: no client, and the supervisor notified."""
    device._client = None
    device.hold_status.record_drop()


def issues(hass: FakeHass) -> dict:
    return ir.async_get(hass).issues


class TestSetupReconciliation:
    """async_setup_entry calls async_start; it must settle the issue then."""

    def test_orphaned_issue_is_deleted_at_setup(self):
        """THE orphan bug: an issue whose condition has cleared must go, and
        nothing about "did this entry see the transition" may gate that —
        the entry that raised it is gone after a reload."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_ID,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
        )
        _, _, watchdog = build(hass, entry, connected=True)

        watchdog.async_start()

        assert (DOMAIN, ISSUE_ID) not in issues(hass)

    def test_issue_survives_a_reload_while_the_link_is_still_down(self, timeline):
        """The other half of the rule: reconciliation is unconditional, not
        blind. A still-broken fan keeps its repair across the reload, and the
        15-minute countdown does not start over (which would let a reload
        loop hide a dead fan forever)."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_ID,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
        )
        _, _, watchdog = build(hass, entry, connected=False)

        watchdog.async_start()

        assert (DOMAIN, ISSUE_ID) in issues(hass)
        assert timeline.remaining == []


class TestUnreachableThreshold:
    def test_nothing_is_raised_before_the_threshold(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=False)

        watchdog.async_start()

        assert issues(hass) == {}
        assert timeline.remaining == [THRESHOLD]

    def test_issue_is_raised_once_the_threshold_passes(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()

        timeline.advance(THRESHOLD - 1)
        assert issues(hass) == {}
        timeline.advance(1)

        issue = issues(hass)[(DOMAIN, ISSUE_ID)]
        assert issue["is_fixable"] is True
        assert issue["severity"] is ir.IssueSeverity.WARNING
        assert issue["translation_key"] == "device_unreachable"
        assert issue["translation_placeholders"]["name"] == "Tent Vent Fan"

    def test_reconnect_deletes_the_issue(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        device, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()
        timeline.advance(THRESHOLD)
        assert (DOMAIN, ISSUE_ID) in issues(hass)

        reconnect(device)

        assert issues(hass) == {}

    def test_reconnect_before_the_threshold_cancels_the_timer(self, timeline):
        """Otherwise the fan is marked unreachable 15 minutes after a blip it
        already recovered from."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        device, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()
        assert timeline.remaining == [THRESHOLD]

        reconnect(device)

        assert timeline.remaining == []
        timeline.advance(THRESHOLD)
        assert issues(hass) == {}

    def test_a_second_drop_arms_the_timer_again(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        device, _, watchdog = build(hass, entry, connected=True)
        watchdog.async_start()

        drop(device)

        assert timeline.remaining == [THRESHOLD]
        assert issues(hass) == {}
        timeline.advance(THRESHOLD)
        assert (DOMAIN, ISSUE_ID) in issues(hass)

    def test_unload_cancels_the_timer(self, timeline):
        """An orphaned timer would fire against a torn-down entry."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()

        watchdog.async_stop()

        assert timeline.remaining == []
        # And the link going up/down afterwards no longer touches issues.
        timeline.advance(THRESHOLD)
        assert issues(hass) == {}


class TestHoldDisabledEntry:
    """With the hold off there is no supervisor and no proxy allocation, so
    the ONLY thing that moves health is advertisement visibility. Without the
    coordinator's health listener the watchdog would reconcile once at setup
    and then never again for these entries."""

    @staticmethod
    def frame():
        return SimpleNamespace(
            name="D-A6B2C",
            address=ADDRESS,
            device=SimpleNamespace(address=ADDRESS, name="D-A6B2C"),
            advertisement=SimpleNamespace(manufacturer_data={}),
        )

    def test_going_unavailable_then_the_threshold_raises_the_issue(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, coordinator, watchdog = build(hass, entry, connected=False, hold=False)
        watchdog.async_start()
        assert issues(hass) == {}

        coordinator._async_handle_unavailable(self.frame())

        assert timeline.remaining == [THRESHOLD]
        timeline.advance(THRESHOLD)
        assert (DOMAIN, ISSUE_ID) in issues(hass)

    def test_the_fan_being_seen_again_clears_the_issue(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, coordinator, watchdog = build(hass, entry, connected=False, hold=False)
        watchdog.async_start()
        coordinator._async_handle_unavailable(self.frame())
        timeline.advance(THRESHOLD)
        assert (DOMAIN, ISSUE_ID) in issues(hass)

        coordinator._async_handle_bluetooth_event(self.frame(), object())

        assert issues(hass) == {}

    def test_the_listener_is_dropped_on_unload(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, coordinator, watchdog = build(hass, entry, connected=False, hold=False)
        watchdog.async_start()
        watchdog.async_stop()

        coordinator._async_handle_unavailable(self.frame())

        assert timeline.remaining == []


class TestPerEntryIsolation:
    def test_only_the_unreachable_fans_issue_is_raised(self, timeline):
        """Six entries share this code; one dead fan must raise exactly one
        repair, keyed to its own address."""
        down = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        up = FakeEntry("e2", "Closet Vent Fan", OTHER_ADDRESS)
        hass = FakeHass(down, up)
        _, _, down_watchdog = build(hass, down, connected=False)
        _, _, up_watchdog = build(hass, up, connected=True)

        down_watchdog.async_start()
        up_watchdog.async_start()
        timeline.advance(THRESHOLD)

        assert set(issues(hass)) == {(DOMAIN, ISSUE_ID)}
        assert unreachable_issue_id(OTHER_ADDRESS) != ISSUE_ID

    def test_another_fans_lifecycle_leaves_the_outage_alone(self, timeline):
        """The outage clock is process state keyed by address; a second fan
        loading healthy, unloading and being removed must touch only its
        own address."""
        down = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        other = FakeEntry("e2", "Closet Vent Fan", OTHER_ADDRESS)
        hass = FakeHass(down, other)
        _, _, down_watchdog = build(hass, down, connected=False)
        down_watchdog.async_start()
        deadline = timeline.now + UNREACHABLE_AFTER.total_seconds()
        assert timeline.deadlines == [deadline]

        timeline.advance(5 * 60)
        _, _, other_watchdog = build(hass, other, connected=True)
        other_watchdog.async_start()
        timeline.advance(3 * 60)
        other_watchdog.async_stop()
        hass.data[DOMAIN].pop(other.entry_id)
        asyncio.run(integration.async_remove_entry(hass, other))

        assert timeline.deadlines == [deadline]
        timeline.advance(7 * 60)
        assert set(issues(hass)) == {(DOMAIN, ISSUE_ID)}


class TestOutageClockSurvivesReloads:
    """THE AUTOHEAL MEASUREMENT (live, 2026-09-09, during a deliberate
    21-minute power cut of the sibling Fluval light — the fans sit behind the
    same automation):

        22:24:37  link drop       -> countdown armed
        22:35:00  autoheal reload -> fresh watcher, countdown restarts at zero
        22:40:00  autoheal reload -> fresh watcher, countdown restarts at zero

    automation.ble_proxy_autoheal reloads the entry of any device whose link
    is down every 5 minutes, for exactly as long as it is down, so a
    countdown that lives in anything the entry owns can never reach 15
    minutes.  The repair must appear 15 minutes after the FIRST drop.
    """

    def test_repair_is_raised_fifteen_minutes_after_the_first_drop(self, timeline):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        device, _, watchdog = build(hass, entry, connected=True)
        watchdog.async_start()
        assert timeline.deadlines == []

        drop(device)
        deadline = timeline.now + UNREACHABLE_AFTER.total_seconds()
        assert timeline.deadlines == [deadline]

        for _reload in range(2):
            timeline.advance(5 * 60)
            _, _, watchdog = reload(hass, entry, watchdog, connected=False)
            # The remaining window, not a fresh one.
            assert timeline.deadlines == [deadline]
            assert issues(hass) == {}

        timeline.advance(5 * 60 - 1)
        assert issues(hass) == {}
        timeline.advance(1)
        assert (DOMAIN, ISSUE_ID) in issues(hass)

    def test_a_reload_after_the_threshold_raises_at_once(self, timeline):
        """Reloaded with the outage already older than the window: no timer,
        the repair is raised on the spot."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()
        watchdog.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)
        assert timeline.deadlines == []

        timeline.advance(UNREACHABLE_AFTER.total_seconds() + 60)
        _, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()

        assert (DOMAIN, ISSUE_ID) in issues(hass)
        assert timeline.deadlines == []

    def test_a_healthy_reload_forgets_the_outage(self, timeline):
        """A fan that came back is judged afresh on its next drop."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()
        timeline.advance(10 * 60)
        device, _, watchdog = reload(hass, entry, watchdog, connected=True)
        assert timeline.deadlines == []

        timeline.advance(60)
        drop(device)

        assert timeline.deadlines == [timeline.now + UNREACHABLE_AFTER.total_seconds()]


class TestSetupNotReady:
    """Live on 2026-09-09: the living-room vent fan sat in setup_retry from
    22:23:52 with "Could not find AC Infinity device with address
    A4:C1:38:44:3D:49", its Connection sensor unavailable and NO repair —
    async_setup_entry raises before the watchdog is even constructed, and
    Home Assistant retries setup on a backoff forever.  The most total
    outage there is must raise the repair like any other.
    """

    @staticmethod
    def setup(hass: FakeHass, entry: FakeEntry) -> None:
        with pytest.raises(ConfigEntryNotReady):
            asyncio.run(integration.async_setup_entry(hass, entry))

    def test_a_fan_that_never_advertises_gets_the_repair(self, timeline):
        entry = FakeEntry("e1", "Living Room Vent Fan", ADDRESS)
        hass = FakeHass(entry)

        self.setup(hass, entry)
        deadline = timeline.now + UNREACHABLE_AFTER.total_seconds()
        assert timeline.deadlines == [deadline]

        # Home Assistant's retry backoff: 10 s, 20 s, 40 s ... capped at
        # 5 min. None of them may push the deadline out or stack timers.
        for wait in (10, 20, 40, 80, 160, 300):
            timeline.advance(wait)
            self.setup(hass, entry)
            assert timeline.deadlines == [deadline]
            assert issues(hass) == {}

        timeline.advance(deadline - timeline.now)

        issue = issues(hass)[(DOMAIN, ISSUE_ID)]
        assert issue["is_fixable"] is True
        assert issue["translation_placeholders"]["name"] == "Living Room Vent Fan"

    def test_a_retry_after_the_threshold_raises_at_once(self, timeline):
        entry = FakeEntry("e1", "Living Room Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        self.setup(hass, entry)
        # The deadline fires while the entry is not loaded, which is what
        # setup_retry looks like from the outside.
        timeline.advance(UNREACHABLE_AFTER.total_seconds())
        assert (DOMAIN, ISSUE_ID) in issues(hass)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)

        timeline.advance(300)
        self.setup(hass, entry)

        assert (DOMAIN, ISSUE_ID) in issues(hass)
        assert timeline.deadlines == []

    def test_a_fan_found_on_a_later_retry_is_judged_by_its_link(self, timeline):
        """Setup eventually succeeds: the watchdog takes over the SAME clock,
        so a fan that is found but still will not connect is flagged 15
        minutes after it first went missing, and one that connects is not."""
        entry = FakeEntry("e1", "Living Room Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        self.setup(hass, entry)
        deadline = timeline.now + UNREACHABLE_AFTER.total_seconds()

        timeline.advance(10 * 60)
        device, _, watchdog = build(hass, entry, connected=False)
        watchdog.async_start()
        assert timeline.deadlines == [deadline]

        reconnect(device)
        assert timeline.deadlines == []
        timeline.advance(10 * 60)
        assert issues(hass) == {}

    def test_an_entry_disabled_while_retrying_is_left_alone(self, timeline):
        """Disabling a setup_retry entry runs no unload hook of ours, so the
        pending deadline is the only thing left that could raise a repair
        for a fan the operator has deliberately switched off."""
        entry = FakeEntry("e1", "Living Room Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        self.setup(hass, entry)

        entry.disabled_by = "user"
        entry.state = ConfigEntryState.NOT_LOADED
        timeline.advance(UNREACHABLE_AFTER.total_seconds())

        assert issues(hass) == {}


def hold_through(monkeypatch, scanner) -> list:
    """Make habluetooth report ``scanner`` as holding ADDRESS's slot.

    Returns the list allocation callbacks get registered into, so a test
    can deliver a slot change the way habluetooth does.
    """
    callbacks: list = []
    allocation = SimpleNamespace(
        source=PROXY_SOURCE, slots=3, free=2, allocated=[ADDRESS]
    )
    manager = SimpleNamespace(
        async_current_allocations=lambda source=None: [allocation],
        async_register_allocation_callback=lambda cb, source=None: (
            callbacks.append(cb) or (lambda: None)
        ),
    )
    monkeypatch.setattr(coordinator_module, "get_manager", lambda: manager)
    monkeypatch.setattr(
        coordinator_module.bluetooth,
        "async_scanner_by_source",
        lambda hass, source: scanner,
    )
    return callbacks


class TestProxyMemory:
    """A dead link has no holding scanner, so the wizard can only offer a
    proxy restart if one was written down while the link was up."""

    @pytest.fixture
    def holding_proxy(self, monkeypatch):
        self._callbacks = hold_through(
            monkeypatch, SimpleNamespace(adapter=PROXY, name=PROXY_SCANNER_NAME)
        )

    @pytest.fixture
    def holding_proxy_without_adapter(self, monkeypatch):
        """A scanner that exposes only its display name."""
        self._callbacks = hold_through(
            monkeypatch, SimpleNamespace(name=PROXY_SCANNER_NAME)
        )

    def test_holding_proxy_is_persisted_while_the_link_is_up(self, holding_proxy):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=True)
        watchdog.async_start()

        # Delivered the way habluetooth delivers it, which also proves the
        # subscription is live.
        for callback in self._callbacks:
            callback(None)

        assert entry.options[CONF_LAST_HOLDING_PROXY] == PROXY
        # Written once, not on every allocation report: each write is a
        # config-entry update.
        assert len(hass.config_entries.option_writes) == 1

    def test_remembered_proxy_is_the_node_name_not_the_display_name(
        self, holding_proxy_without_adapter
    ):
        """The record exists to name an ESPHome action, so it must be the
        bare node even when all the scanner offers is "<node> (<MAC>)"."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=True)
        watchdog.async_start()

        for callback in self._callbacks:
            callback(None)

        assert entry.options[CONF_LAST_HOLDING_PROXY] == PROXY
        assert "(" not in entry.options[CONF_LAST_HOLDING_PROXY]


class TestRecoveryMenu:
    def make_flow(self, hass: FakeHass, issue_id: str = ISSUE_ID):
        flow = asyncio.run(async_create_fix_flow(hass, issue_id, None))
        flow.hass = hass
        return flow

    def test_restart_proxy_is_not_offered_without_a_known_proxy(self):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry, services={"esphome": {PROXY_ACTION: object()}})
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert result["type"] == "menu"
        assert "restart_proxy" not in result["menu_options"]

    def test_restart_proxy_is_not_offered_without_the_matching_action(self):
        """A proxy that does not expose the restart action must not be
        advertised as a fix."""
        entry = FakeEntry(
            "e1", "Tent Vent Fan", ADDRESS, **{CONF_LAST_HOLDING_PROXY: PROXY}
        )
        hass = FakeHass(entry, services={"esphome": {"some_other_node_restart": {}}})
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert "restart_proxy" not in result["menu_options"]

    def test_restart_proxy_is_offered_when_proxy_and_action_exist(self):
        entry = FakeEntry(
            "e1", "Tent Vent Fan", ADDRESS, **{CONF_LAST_HOLDING_PROXY: PROXY}
        )
        hass = FakeHass(entry, services={"esphome": {PROXY_ACTION: object()}})
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert result["menu_options"] == [
            "recheck",
            "reload",
            "restart_proxy",
            "power_cycle",
        ]

    def test_restart_proxy_is_offered_for_the_live_holder(self, monkeypatch):
        """No remembered proxy: the rung comes from the scanner holding the
        slot right now, whose name carries the MAC suffix."""
        hold_through(
            monkeypatch, SimpleNamespace(adapter=PROXY, name=PROXY_SCANNER_NAME)
        )
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry, services={"esphome": {PROXY_ACTION: object()}})
        build(hass, entry, connected=True)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert "restart_proxy" in result["menu_options"]

    def test_restart_proxy_is_offered_for_a_record_written_as_a_display_name(self):
        """Options written before the node-name cutover hold "<node> (<MAC>)"
        and an unreachable fan cannot rewrite them."""
        entry = FakeEntry(
            "e1",
            "Tent Vent Fan",
            ADDRESS,
            **{CONF_LAST_HOLDING_PROXY: PROXY_SCANNER_NAME},
        )
        hass = FakeHass(entry, services={"esphome": {PROXY_ACTION: object()}})
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_restart_proxy())

        assert hass.services.calls == [("esphome", PROXY_ACTION, None)]

    def test_first_entry_reports_no_previous_attempt(self):
        """Never the string "None": that placeholder is rendered to the
        operator."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert result["description_placeholders"]["last_result"] == ""
        assert result["description_placeholders"]["link"] == "disconnected"

    def test_unloaded_entry_aborts(self):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        entry.state = ConfigEntryState.SETUP_RETRY
        hass = FakeHass(entry)
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_init())

        assert result["type"] == "abort"
        assert result["reason"] == "entry_not_loaded"

    def test_issue_for_an_unknown_fan_aborts(self):
        hass = FakeHass()

        result = asyncio.run(self.make_flow(hass, "DEADBEEF_unreachable").async_step_init())

        assert result["type"] == "abort"
        assert result["reason"] == "entry_not_loaded"


class TestRecoveryActions:
    @pytest.fixture(autouse=True)
    def no_waiting(self, monkeypatch):
        """Collapse every wait: the ladder's timing is not what is tested."""
        monkeypatch.setattr(repairs_module, "RECOVERY_TIMEOUT", 0)
        monkeypatch.setattr(repairs_module, "POWER_CYCLE_TIMEOUT", 0)
        monkeypatch.setattr(repairs_module, "POWER_CYCLE_OFF_SECONDS", 0)

    def make_flow(self, hass: FakeHass):
        flow = asyncio.run(async_create_fix_flow(hass, ISSUE_ID, None))
        flow.hass = hass
        return flow

    def test_a_healthy_link_finishes_the_flow_and_clears_the_issue(self):
        """Finishing makes HA drop the issue; the integration's own reconcile
        must delete it too, so the repair cannot outlive the fault."""
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        _, _, watchdog = build(hass, entry, connected=True)
        watchdog.async_start()
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_ID,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
        )

        result = asyncio.run(self.make_flow(hass).async_step_reload())

        assert result["type"] == "create_entry"
        assert hass.config_entries.reloads == ["e1"]
        assert issues(hass) == {}

    def test_a_still_dead_link_returns_to_the_menu(self):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_reload())

        assert result["type"] == "menu"
        assert result["description_placeholders"]["last_result"]

    def test_power_cycle_remembers_the_outlet_and_switches_it(self):
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        build(hass, entry, connected=False)

        result = asyncio.run(
            self.make_flow(hass).async_step_power_cycle(
                {CONF_RECOVERY_OUTLET: "switch.tent_outlet"}
            )
        )

        assert entry.options[CONF_RECOVERY_OUTLET] == "switch.tent_outlet"
        assert hass.services.calls == [
            ("switch", "turn_off", {"entity_id": "switch.tent_outlet"}),
            ("switch", "turn_on", {"entity_id": "switch.tent_outlet"}),
        ]
        assert result["type"] == "menu"

    def test_power_cycle_restores_power_when_the_flow_is_cancelled(
        self, monkeypatch
    ):
        """Closing the repair dialog cancels the flow task. Mains must come
        back anyway — this is the one rung that can leave a fan dark."""
        monkeypatch.setattr(repairs_module, "POWER_CYCLE_OFF_SECONDS", 0.2)
        entry = FakeEntry("e1", "Tent Vent Fan", ADDRESS)
        hass = FakeHass(entry)
        build(hass, entry, connected=False)
        flow = self.make_flow(hass)

        async def scenario():
            task = asyncio.get_running_loop().create_task(
                flow.async_step_power_cycle(
                    {CONF_RECOVERY_OUTLET: "switch.tent_outlet"}
                )
            )
            await asyncio.sleep(0.05)  # inside the mains-off window
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Let the shielded turn-on finish.
            for _ in range(5):
                await asyncio.sleep(0)
            return [service for _domain, service, _data in hass.services.calls]

        assert asyncio.run(scenario()) == ["turn_off", "turn_on"]

    def test_power_cycle_form_prefills_a_remembered_outlet(self):
        entry = FakeEntry(
            "e1",
            "Tent Vent Fan",
            ADDRESS,
            **{CONF_RECOVERY_OUTLET: "switch.tent_outlet"},
        )
        hass = FakeHass(entry)
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_power_cycle())

        assert result["type"] == "form"
        defaults = {
            str(key): key.default() for key in result["data_schema"].schema
        }
        assert defaults == {CONF_RECOVERY_OUTLET: "switch.tent_outlet"}

    def test_restart_proxy_calls_the_discovered_action(self):
        entry = FakeEntry(
            "e1", "Tent Vent Fan", ADDRESS, **{CONF_LAST_HOLDING_PROXY: PROXY}
        )
        hass = FakeHass(entry, services={"esphome": {PROXY_ACTION: object()}})
        build(hass, entry, connected=False)

        result = asyncio.run(self.make_flow(hass).async_step_restart_proxy())

        assert hass.services.calls == [("esphome", PROXY_ACTION, None)]
        assert result["type"] == "menu"


class TestOptionsBookkeeping:
    """The proxy memory lives in entry.options next to a user-facing option."""

    def test_toggling_the_hold_keeps_the_remembered_proxy_and_outlet(self):
        """The options form does not offer these two keys, so submitting it
        must merge, not replace — otherwise one hold toggle silently forgets
        which proxy to restart and which outlet to cut."""
        handler = OptionsFlowHandler()
        handler.config_entry = SimpleNamespace(
            options={
                CONF_HOLD_CONNECTION: True,
                CONF_LAST_HOLDING_PROXY: PROXY,
                CONF_RECOVERY_OUTLET: "switch.tent_outlet",
            }
        )

        result = asyncio.run(
            handler.async_step_init({CONF_HOLD_CONNECTION: False})
        )

        assert result["data"] == {
            CONF_HOLD_CONNECTION: False,
            CONF_LAST_HOLDING_PROXY: PROXY,
            CONF_RECOVERY_OUTLET: "switch.tent_outlet",
        }
