"""Regression tests for the config flow's manufacturer-data guard.

The user step builds its pick-list from every discovered BLE advertisement.
Any advertisement lacking AC Infinity's manufacturer ID (2306) MUST be
skipped — indexing its manufacturer_data blindly raised KeyError and killed
the whole flow when an unrelated BLE device was in range.

These tests drive the real ConfigFlow.async_step_user with Home Assistant
stubbed (tests/ha_stubs.py provides a behavioral ConfigFlow base returning
FlowResult-shaped dicts) and fake discovered-service records.
"""

import asyncio
from types import SimpleNamespace

from custom_components.ac_infinity import config_flow as config_flow_module
from custom_components.ac_infinity.config_flow import ConfigFlow
from tests.conftest import build_manufacturer_data

AC_ADDRESS = "AA:BB:CC:DD:EE:01"
FOREIGN_ADDRESS = "AA:BB:CC:DD:EE:02"


def service_info(address: str, manufacturer_data: dict[int, bytes]):
    return SimpleNamespace(
        address=address,
        device=SimpleNamespace(address=address, name=None),
        advertisement=SimpleNamespace(manufacturer_data=manufacturer_data),
    )


def ac_service_info(address: str = AC_ADDRESS):
    return service_info(
        address, {2306: build_manufacturer_data(name="A6B2C", device_type=6)}
    )


def foreign_service_info(address: str = FOREIGN_ADDRESS):
    # e.g. an Apple continuity advertisement: manufacturer ID 76
    return service_info(address, {76: b"\x10\x05\x01\x02\x03"})


def run_user_step(discovered):
    """Run async_step_user against a fixed set of discovered advertisements."""
    flow = ConfigFlow()
    flow.hass = object()
    original = config_flow_module.async_discovered_service_info
    config_flow_module.async_discovered_service_info = lambda hass: list(discovered)
    try:
        return asyncio.run(flow.async_step_user(None))
    finally:
        config_flow_module.async_discovered_service_info = original


def picklist_addresses(result) -> list[str]:
    """Extract the addresses offered by the form's vol.In selector."""
    assert result["type"] == "form", result
    schema = result["data_schema"].schema
    (validator,) = schema.values()
    return list(validator.container)


class TestManufacturerDataGuard:
    def test_foreign_advertisements_are_skipped(self):
        result = run_user_step([foreign_service_info(), ac_service_info()])
        assert picklist_addresses(result) == [AC_ADDRESS]

    def test_malformed_aci_payload_is_skipped_not_fatal(self):
        """An advertisement claiming manufacturer ID 2306 but carrying a
        truncated payload must be skipped like a foreign device — one broken
        neighbor must not crash discovery for the whole flow."""
        malformed = service_info("AA:BB:CC:DD:EE:03", {2306: b"\x00\x01"})
        result = run_user_step([malformed, ac_service_info()])
        assert picklist_addresses(result) == [AC_ADDRESS]

    def test_only_malformed_aci_payload_aborts(self):
        malformed = service_info("AA:BB:CC:DD:EE:03", {2306: b"\x00\x01"})
        result = run_user_step([malformed])
        assert result == {"type": "abort", "reason": "no_devices_found"}

    def test_only_foreign_advertisements_aborts(self):
        """No AC Infinity device in range -> clean abort, not KeyError."""
        result = run_user_step([foreign_service_info()])
        assert result == {"type": "abort", "reason": "no_devices_found"}

    def test_no_advertisements_aborts(self):
        result = run_user_step([])
        assert result == {"type": "abort", "reason": "no_devices_found"}

    def test_picklist_labels_use_parsed_name(self):
        """The label must come from the parsed advertisement (family letter +
        ASCII id), proving the guard admitted a parseable payload."""
        result = run_user_step([ac_service_info()])
        schema = result["data_schema"].schema
        (validator,) = schema.values()
        assert validator.container[AC_ADDRESS] == f"D-A6B2C ({AC_ADDRESS})"
