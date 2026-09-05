"""Tests for ACInfinityDevice advertisement/state merging and poll gating.

Tonight's fleet ran AUTO mode learned via GATT polls while advertisements —
which never carry work_type — kept streaming in.  The merge MUST NOT let an
advertisement wipe poll-derived fields (work_type, level_on/off, auto_mode);
that invariant is what these tests pin.
"""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.ac_infinity.device import (
    WORK_TYPE_AUTO,
    ACInfinityDevice,
    AutoModeConfig,
    DeviceInfoEx,
)
from tests.conftest import build_manufacturer_data

ADDRESS = "AA:BB:CC:DD:EE:FF"


def make_device(state: DeviceInfoEx) -> ACInfinityDevice:
    """Construct a device off a fake BLEDevice.

    The controller only reads .address/.name from the BLEDevice, and its
    __init__ requires a running event loop, so we build inside asyncio.run.
    """

    async def _build() -> ACInfinityDevice:
        ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
        return ACInfinityDevice(ble, state=state)

    return asyncio.run(_build())


def airtap_state(**overrides) -> DeviceInfoEx:
    base = dict(type=6, name="D-A6B2C", version=1)
    base.update(overrides)
    return DeviceInfoEx(**base)


def advertise(device: ACInfinityDevice, payload: bytes) -> None:
    adv = SimpleNamespace(manufacturer_data={2306: payload})
    ble = SimpleNamespace(address=ADDRESS, name="D-A6B2C")
    device.set_ble_device_and_advertisement_data(ble, adv)


class TestAdvertisementMerge:
    def test_advertisement_updates_environmental_fields(self):
        device = make_device(airtap_state())
        advertise(
            device,
            build_manufacturer_data(name="A6B2C", tmp_centi=2101, fan=4),
        )
        assert device.temperature == pytest.approx(21.01)
        assert device.speed == 4

    def test_advertisement_preserves_poll_only_fields(self):
        """work_type/levels/auto_mode come only from polls; an advertisement
        (whose parse leaves them None) must not reset them."""
        auto = AutoModeConfig(
            high_temp_enabled=True,
            high_temp=30,
            low_temp_enabled=True,
            low_temp=20,
            high_humidity_enabled=False,
            high_humidity=70,
            low_humidity_enabled=False,
            low_humidity=40,
        )
        device = make_device(
            airtap_state(
                work_type=WORK_TYPE_AUTO,
                level_on=8,
                level_off=2,
                auto_mode=auto,
            )
        )
        advertise(device, build_manufacturer_data(name="A6B2C", fan=6))
        assert device.state.work_type == WORK_TYPE_AUTO
        assert device.max_speed == 8
        assert device.min_speed == 2
        assert device.auto_mode == auto
        assert device.speed == 6  # while advertised fields did update

    def test_advertised_fan_zero_overwrites_previous_speed(self):
        """fan=0 is a real value, not 'unknown' — a stopped fan must reach
        the merged state (root of tonight's stale-speed sensor bug)."""
        device = make_device(airtap_state(fan=9))
        advertise(device, build_manufacturer_data(name="A6B2C", fan=0))
        assert device.state.fan == 0

    def test_advertisement_fires_callbacks(self):
        """The coordinator relies on the ADVERTISEMENT callback to fan state
        out to entities; a silent merge would freeze the UI."""
        device = make_device(airtap_state())
        seen = []
        unregister = device.register_callback(lambda *args: seen.append(args))
        advertise(device, build_manufacturer_data(name="A6B2C", fan=3))
        assert len(seen) == 1
        unregister()


class TestUpdateNeeded:
    def test_first_poll_always_needed(self):
        device = make_device(airtap_state())
        assert device.update_needed(None) is True

    def test_recent_poll_suppresses_repolling(self):
        """Six fans share ~4 proxy connection slots; polling more often than
        every 30s would starve the fleet of connections."""
        device = make_device(airtap_state())
        assert device.update_needed(10) is False

    def test_stale_poll_triggers_repolling(self):
        device = make_device(airtap_state())
        assert device.update_needed(31) is True

    def test_config_change_forces_immediate_poll(self):
        """After a config write the device state must be re-read regardless
        of the poll interval (the write path sets this flag)."""
        device = make_device(airtap_state())
        device._config_changed_since_last_update = True
        assert device.update_needed(1) is True
