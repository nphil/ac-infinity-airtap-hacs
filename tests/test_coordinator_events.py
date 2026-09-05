"""Regression tests for the coordinator's bluetooth event flow.

Verified live on 2026-09-05 (bug #2, the headline): all six fan entities froze
for 8+ minutes while advertisements kept arriving.  ROOT CAUSE: the event
handler returned early for dispatched frames lacking the AC Infinity
manufacturer record (BLE splits data across ADV_IND/SCAN_RSP; proxies coalesce
unpredictably), skipping the base-class call that notifies listeners, re-marks
availability, and — critically — evaluates ``needs_poll`` (polling in this
coordinator family is advertisement-driven, so dropped frames silently
disabled the 30 s GATT poll cycle too).

Contract pinned here: EVERY dispatched frame reaches
``super()._async_handle_bluetooth_event``; only the state merge is
conditional on a parseable manufacturer record.  Plus: unavailability flips
``available`` (entities must not serve stale state forever) and re-arms the
online-transition path.

The real ACInfinityDataUpdateCoordinator and real ACInfinityDevice run; only
HA's base coordinator is the stub (tests/ha_stubs.py), which records
``bluetooth_event_super_calls`` and mirrors the base's listener notification.
"""

import asyncio
import logging
from types import SimpleNamespace

from custom_components.ac_infinity.coordinator import ACInfinityDataUpdateCoordinator
from custom_components.ac_infinity.device import ACInfinityDevice, DeviceInfoEx
from tests.conftest import build_manufacturer_data

ADDRESS = "AA:BB:CC:DD:EE:FF"
CHANGE = object()  # opaque to the coordinator; passed through to the base


def make_coordinator() -> tuple[ACInfinityDataUpdateCoordinator, ACInfinityDevice]:
    async def _build():
        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        device = ACInfinityDevice(
            ble, state=DeviceInfoEx(type=6, name="D-A6B2C", version=1, fan=4)
        )
        coordinator = ACInfinityDataUpdateCoordinator(
            SimpleNamespace(), logging.getLogger("test"), ble, device
        )
        return coordinator, device

    return asyncio.run(_build())


def frame(manufacturer_data: dict[int, bytes]):
    return SimpleNamespace(
        name="D-A6B2C",
        address=ADDRESS,
        device=SimpleNamespace(address=ADDRESS, name="D-A6B2C"),
        advertisement=SimpleNamespace(manufacturer_data=manufacturer_data),
    )


def aci_frame(fan: int = 7):
    return frame({2306: build_manufacturer_data(name="A6B2C", fan=fan)})


class TestEveryFrameReachesBase:
    def test_frame_with_record_merges_and_calls_super(self):
        coordinator, device = make_coordinator()
        coordinator._async_handle_bluetooth_event(aci_frame(fan=7), CHANGE)
        assert device.state.fan == 7  # merged
        assert coordinator.bluetooth_event_super_calls == 1

    def test_recordless_frame_still_calls_super(self):
        """THE fleet-freeze regression: a frame without the AC Infinity
        record must still notify listeners and drive poll scheduling."""
        coordinator, device = make_coordinator()
        coordinator._async_handle_bluetooth_event(
            frame({76: b"\x10\x05\x01\x02\x03"}), CHANGE
        )
        assert device.state.fan == 4  # no merge from a foreign record
        assert coordinator.bluetooth_event_super_calls == 1

    def test_malformed_record_is_skipped_but_frame_still_counts(self):
        coordinator, device = make_coordinator()
        coordinator._async_handle_bluetooth_event(frame({2306: b"\x00\x01"}), CHANGE)
        assert device.state.fan == 4  # merge skipped, no crash
        assert coordinator.bluetooth_event_super_calls == 1

    def test_parseable_frame_marks_device_ready(self):
        coordinator, _ = make_coordinator()
        assert not coordinator._device_ready.is_set()
        coordinator._async_handle_bluetooth_event(aci_frame(), CHANGE)
        assert coordinator._device_ready.is_set()


class TestUnavailability:
    def test_unavailable_flips_availability(self):
        """Entities must go unavailable when no scanner sees the device —
        never serve stale state forever (the base flips it; the override
        must preserve that via super())."""
        coordinator, _ = make_coordinator()
        assert coordinator.available is True
        coordinator._async_handle_unavailable(frame({}))
        assert coordinator.available is False

    def test_frame_after_unavailable_marks_online_again(self):
        coordinator, _ = make_coordinator()
        coordinator._async_handle_unavailable(frame({}))
        assert coordinator._was_unavailable is True
        coordinator._async_handle_bluetooth_event(aci_frame(), CHANGE)
        # online-transition consumed; next unavailability re-arms it
        assert coordinator._was_unavailable is False
