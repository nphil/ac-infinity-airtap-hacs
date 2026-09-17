"""Tests for preferred-proxy affinity wiring (see ble_affinity.py).

``make_affinity_client_class`` and its selection algorithm are vendored,
shared code; what is this repo's own is the wiring around it -
CONF_PREFERRED_PROXY and the ``entry.options.get(CONF_PREFERRED_PROXY) or
None`` getter ``async_setup_entry`` builds for it (see __init__.py). These
tests drive that exact getter shape against small fakes for the habluetooth
objects ble_affinity talks to, proving what this repo's own option produces,
not re-deriving ble_affinity's algorithm from scratch.
"""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ac_infinity.ble_affinity import make_affinity_client_class
from custom_components.ac_infinity.const import CONF_PREFERRED_PROXY

ADDRESS = "AA:BB:CC:DD:EE:FF"
PREFERRED = "plant-room-bluetooth-proxy"
OTHER = "downstairs-bluetooth-proxy"

# What default_select() returns: a plain object with a `.scanner`, matching
# the (scanner, ble_device) shape bleak_retry_connector's own selection
# returns. ble_affinity's on_choice fallback path reads `.scanner.name` off
# whatever this is, so it must not be a bare object().
DEFAULT_BACKEND = SimpleNamespace(scanner=SimpleNamespace(name="default-scanner"))


class FakeConnector:
    def __init__(self, can_connect: bool = True) -> None:
        self._can_connect = can_connect

    def can_connect(self) -> bool:
        return self._can_connect


class FakeScanner:
    def __init__(
        self, adapter: str, *, failures: int = 0, can_connect: bool = True
    ) -> None:
        self.adapter = adapter
        self.name = adapter
        self.connector = FakeConnector(can_connect)
        self._failures = failures

    def connection_failures(self, address: str) -> int:
        return self._failures


class FakeScannerDevice:
    def __init__(self, scanner: FakeScanner) -> None:
        self.scanner = scanner
        self.ble_device = f"ble-device-{scanner.adapter}"
        self.advertisement = SimpleNamespace(rssi=-60)


class FakeManager:
    """Reports whichever scanner_devices a test configures for the address."""

    def __init__(self, scanner_devices: list[FakeScannerDevice]) -> None:
        self._scanner_devices = scanner_devices

    def async_scanner_devices_by_address(self, address: str, connectable: bool):
        assert connectable is True
        return list(self._scanner_devices)


class FakeBase:
    """Stands in for the runtime bleak_retry_connector client class.

    Exposes exactly the two habluetooth hooks affinity_supported() checks
    for, plus the name-mangled address attribute HaBleakClientWrapper keeps
    before connect() (it skips BleakClient.__init__ entirely).
    """

    def __init__(self, address: str) -> None:
        self._HaBleakClientWrapper__address = address

    def _async_get_best_available_backend_and_device(self, manager):
        return DEFAULT_BACKEND

    def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
        return SimpleNamespace(scanner=scanner, ble_device=ble_device)


def make_entry(preferred: str) -> SimpleNamespace:
    """A fake ConfigEntry carrying exactly the option this repo reads."""
    return SimpleNamespace(options={CONF_PREFERRED_PROXY: preferred})


def select(entry: SimpleNamespace, scanner_devices: list[FakeScannerDevice]):
    """Build the affinity client the same way async_setup_entry does, and
    run one selection against it."""
    choices: list[tuple[str, bool]] = []
    client_class = make_affinity_client_class(
        FakeBase,
        lambda: entry.options.get(CONF_PREFERRED_PROXY) or None,
        on_choice=lambda name, used: choices.append((name, used)),
    )
    client = client_class(ADDRESS)
    manager = FakeManager(scanner_devices)
    backend = client._async_get_best_available_backend_and_device(manager)
    return backend, choices


class TestPreferredProxySelection:
    def test_preferred_present_and_connectable_is_chosen(self):
        preferred_scanner = FakeScanner(PREFERRED)
        backend, choices = select(
            make_entry(PREFERRED),
            [
                FakeScannerDevice(FakeScanner(OTHER)),
                FakeScannerDevice(preferred_scanner),
            ],
        )
        assert backend.scanner is preferred_scanner
        assert choices == [(PREFERRED, True)]

    def test_preferred_absent_uses_default(self):
        backend, choices = select(
            make_entry(PREFERRED), [FakeScannerDevice(FakeScanner(OTHER))]
        )
        assert backend is DEFAULT_BACKEND
        assert choices == [("default-scanner", False)]

    def test_repeated_failures_fall_back_to_default(self):
        preferred_scanner = FakeScanner(PREFERRED, failures=3)
        backend, choices = select(
            make_entry(PREFERRED), [FakeScannerDevice(preferred_scanner)]
        )
        assert backend is DEFAULT_BACKEND
        assert choices == [("default-scanner", False)]

    def test_automatic_option_value_never_inspects_scanners(self):
        """CONF_PREFERRED_PROXY's default ("") must mean "no preference",
        same as the option never having been set at all - this repo's
        getter does ``entry.options.get(CONF_PREFERRED_PROXY) or None``
        specifically so the empty-string default takes this path even
        though a matching, healthy scanner is right there. Unlike a real
        but currently-unusable preference, automatic never invokes
        on_choice at all: preferred-proxy affinity was never in play."""
        preferred_scanner = FakeScanner(PREFERRED)
        backend, choices = select(make_entry(""), [FakeScannerDevice(preferred_scanner)])
        assert backend is DEFAULT_BACKEND
        assert choices == []
