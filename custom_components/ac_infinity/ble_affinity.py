"""Preferred-proxy affinity for Home Assistant Bluetooth connections.

Vendored verbatim into each nateshome BLE integration (ac_infinity, bedjet,
fluvalble, ecoflow_iot). Change it here first
(`/data/home/tmp/ble_affinity.py` is the source of truth) and copy to every
integration in the same pass.

Why this exists
---------------
`HaBleakClientWrapper.connect()` (habluetooth `wrappers.py`) ignores the
`BLEDevice` an integration hands it and re-picks the connection path on
every connect: sorted by advertisement RSSI, then
`BaseHaScanner._score_connection_paths` (penalties for connections in
progress, prior failures and a last free slot). An integration therefore
has NO supported way to say "connect through the proxy in this room";
habluetooth #602 asks for one and is still open.

The distance the RSSI sort ignores is what matters here. Every ghost link
seen on this network (2026-09-09 .. 2026-09-17) formed on a marginal link
to a *distant* proxy: the disconnect handshake failed to complete over the
weak path and left the peripheral holding a connection the proxy had
forgotten. A device that is always carried by the proxy sitting next to it
does not get into that state.

How
---
`make_affinity_client_class(base, ...)` returns a subclass of whatever
client class `bleak_retry_connector.establish_connection` would otherwise
use and overrides ONE method, `_async_get_best_available_backend_and_device`.
When the preferred scanner currently advertises the address, can accept a
connection, and has not failed this address `max_failures` times in a row,
it is chosen; in every other case the default selection runs unchanged.
All of the wrapper's bookkeeping (`_add_connecting`, `_track`, slot
release, abort-on-unregister) still runs because `connect()` is untouched.

Fallback is therefore bounded by habluetooth's own failure counter, which
resets on the next successful connect through that scanner:
`establish_connection` retries, the first `max_failures` attempts go to the
preferred proxy, then the default path takes over for the rest.

The preferred scanner is named by its ESPHome node name (`scanner.adapter`,
what `bleak_esphome` registers from `device_info.name`), the same identity
the integrations already store in `last_holding_proxy`. Source MAC and the
full `scanner.name` are accepted too.

Temporary exclusion (ac_infinity only; not in the shared copy)
--------------------------------------------------------------
habluetooth clears a scanner's failure count on every *successful* connect, so
an attempt that connected and then hung in the notification subscribe - or one
our own step timeout cut off - barely dents that scanner's score, and both the
preferred proxy and habluetooth's default routing send the next attempt
straight back to it. `is_excluded(scanner)` is an injected predicate; the
caller backs it with its own record of scanners that stalled an attempt (see
`proxy_health.py`) and the scanner is left out of BOTH the preferred pick and
the default pick for as long as it says so - but only while another
connectable route exists: a device reachable through one proxy still uses it.
The default pick is filtered BEFORE habluetooth runs, by handing its selector
a manager whose `async_scanner_devices_by_address` omits the excluded
scanners, so the connection slot habluetooth reserves (local adapters
allocate one when a backend is built) is only ever reserved for the scanner
that is actually used; picking first and discarding afterwards would leak it.
`on_selected(scanner)` reports every pick, preferred or default, so the caller
knows which scanner a stalled attempt went through.


Private-API note: the overridden method and `_async_get_backend_for_ble_device`
are habluetooth internals (present in 6.26.x). `affinity_supported()` checks
for them; when absent the factory returns `base` unchanged and logs once, so
an upgrade degrades to default routing rather than breaking connections.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

# Preferred scanner gives up after this many consecutive failures for one
# address (habluetooth resets the counter on success). Three is the point at
# which the default scorer would itself have ranked the scanner below a
# healthy alternative.
DEFAULT_MAX_FAILURES = 3

_SELECT = "_async_get_best_available_backend_and_device"
_BACKEND_FOR = "_async_get_backend_for_ble_device"
# HaBleakClientWrapper skips BleakClient.__init__ and keeps the address in a
# name-mangled attribute; there is no public accessor before connect().
_WRAPPER_ADDRESS = "_HaBleakClientWrapper__address"
_warned_unsupported = False


def scanner_matches(scanner: Any, preferred: str) -> bool:
    """Return True if ``scanner`` is the one the operator named."""
    if not preferred:
        return False
    wanted = preferred.strip().lower()
    for attr in ("adapter", "source", "name"):
        value = getattr(scanner, attr, None)
        if isinstance(value, str) and value.strip().lower() == wanted:
            return True
    return False


def affinity_supported(base: type) -> bool:
    """Return True if ``base`` exposes the habluetooth hooks this relies on."""
    return callable(getattr(base, _SELECT, None)) and callable(
        getattr(base, _BACKEND_FOR, None)
    )


def _client_address(client: Any) -> str:
    """Return the target address of a not-yet-connected wrapper client."""
    address = getattr(client, _WRAPPER_ADDRESS, None)
    if isinstance(address, str):
        return address
    return str(getattr(client, "address", ""))


class _FilteredManager:
    """The habluetooth manager, minus some scanners for one address.

    Handed to habluetooth's own selector so it scores and allocates a slot
    over the allowed scanners only; every other attribute is the real
    manager's.
    """

    def __init__(self, manager: Any, address: str, allowed: list[Any]) -> None:
        self._manager = manager
        self._address = address
        self._allowed = allowed

    def async_scanner_devices_by_address(self, address: str, connectable: bool) -> Any:
        if address == self._address:
            return list(self._allowed)
        return self._manager.async_scanner_devices_by_address(address, connectable)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


def make_affinity_client_class(
    base: type,
    preferred_getter: Callable[[], str | None],
    *,
    max_failures: int = DEFAULT_MAX_FAILURES,
    on_choice: Callable[[str, bool], None] | None = None,
    is_excluded: Callable[[Any], bool] | None = None,
    on_selected: Callable[[Any], None] | None = None,
) -> type:
    """Return ``base`` specialised to prefer one scanner.

    ``preferred_getter`` is called at each connect so an options change
    takes effect on the next reconnect without rebuilding the client.
    ``on_choice(scanner_name, preferred_used)`` is invoked after every
    selection that preferred-proxy affinity took part in, so the caller can
    surface which path was taken (not when no preference is configured).
    ``is_excluded(scanner)`` and ``on_selected(scanner)``: see "Temporary
    exclusion" in the module docstring; ``on_selected`` fires for every pick.
    """
    global _warned_unsupported
    if not affinity_supported(base):
        if not _warned_unsupported:
            _LOGGER.warning(
                "Bluetooth client %s has no backend-selection hook; "
                "preferred-proxy affinity is disabled and habluetooth's "
                "default routing applies",
                getattr(base, "__name__", base),
            )
            _warned_unsupported = True
        return base

    default_select = getattr(base, _SELECT)

    def _default_pick(self: Any, manager: Any, address: str) -> Any:
        """habluetooth's own pick, over the scanners not currently excluded."""
        if is_excluded is not None:
            devices = manager.async_scanner_devices_by_address(address, True)
            allowed = [d for d in devices if not is_excluded(d.scanner)]
            if allowed and len(allowed) != len(devices):
                try:
                    backend = default_select(
                        self, _FilteredManager(manager, address, allowed)
                    )
                except BleakError:
                    # Nothing allowed can take a connection right now (no
                    # free slot): an excluded route beats no route. The
                    # selector reserves nothing before it raises.
                    pass
                else:
                    _LOGGER.info(
                        "%s: routing around %d proxy(ies) that recently "
                        "stalled a connection attempt; using %s",
                        address,
                        len(devices) - len(allowed),
                        getattr(backend.scanner, "name", "?"),
                    )
                    return backend
        return default_select(self, manager)

    def _select(self: Any, manager: Any) -> Any:
        preferred = preferred_getter()
        address = _client_address(self)
        if not preferred:
            backend = _default_pick(self, manager, address)
            if on_selected is not None:
                on_selected(backend.scanner)
            return backend

        for scanner_device in manager.async_scanner_devices_by_address(address, True):
            scanner = scanner_device.scanner
            if not scanner_matches(scanner, preferred):
                continue
            if is_excluded is not None and is_excluded(scanner):
                _LOGGER.info(
                    "%s: preferred proxy %s stalled a recent connection "
                    "attempt; using default routing for now",
                    address,
                    scanner.name,
                )
                break
            connector = getattr(scanner, "connector", None)
            if connector is None or not connector.can_connect():
                _LOGGER.debug(
                    "%s: preferred proxy %s has no free connection slot; "
                    "falling back to default routing",
                    address,
                    scanner.name,
                )
                break
            failures = scanner.connection_failures(address)
            if failures >= max_failures:
                _LOGGER.info(
                    "%s: preferred proxy %s failed %d times in a row; "
                    "falling back to default routing until it succeeds again",
                    address,
                    scanner.name,
                    failures,
                )
                break
            backend = getattr(self, _BACKEND_FOR)(
                manager, scanner, scanner_device.ble_device
            )
            if backend is None:
                break
            _LOGGER.info(
                "%s: connecting via preferred proxy %s (RSSI %s)",
                address,
                scanner.name,
                scanner_device.advertisement.rssi,
            )
            if on_choice is not None:
                on_choice(scanner.name, True)
            if on_selected is not None:
                on_selected(scanner)
            return backend
        else:
            _LOGGER.debug(
                "%s: preferred proxy %s does not currently see this device; "
                "falling back to default routing",
                address,
                preferred,
            )

        backend = _default_pick(self, manager, address)
        if on_choice is not None:
            on_choice(getattr(backend.scanner, "name", "?"), False)
        if on_selected is not None:
            on_selected(backend.scanner)
        return backend

    return type(
        f"Affinity{base.__name__}",
        (base,),
        {_SELECT: _select, "__module__": __name__},
    )
