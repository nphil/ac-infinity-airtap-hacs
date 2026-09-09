"""Import smoke test for every integration module.

Under the stub layer this catches two whole classes of CI-cheap breakage:
missing/typo'd imports (a module that cannot import kills its whole platform
at HA setup time) and syntax not valid on the target Python.  It also keeps
tests/ha_stubs.py honest — a new homeassistant import in any module fails
here with a clear ModuleNotFoundError naming what to stub.
"""

import importlib

import pytest

MODULES = [
    "custom_components.ac_infinity",
    "custom_components.ac_infinity.config_flow",
    "custom_components.ac_infinity.const",
    "custom_components.ac_infinity.coordinator",
    "custom_components.ac_infinity.device",
    "custom_components.ac_infinity.diagnostics",
    "custom_components.ac_infinity.fan",
    "custom_components.ac_infinity.hold",
    "custom_components.ac_infinity.models",
    "custom_components.ac_infinity.number",
    "custom_components.ac_infinity.sensor",
    "custom_components.ac_infinity.switch",
    "custom_components.ac_infinity.ac_infinity_ble",
    "custom_components.ac_infinity.ac_infinity_ble.const",
    "custom_components.ac_infinity.ac_infinity_ble.device",
    "custom_components.ac_infinity.ac_infinity_ble.exceptions",
    "custom_components.ac_infinity.ac_infinity_ble.models",
    "custom_components.ac_infinity.ac_infinity_ble.protocol",
    "custom_components.ac_infinity.ac_infinity_ble.util",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module):
    importlib.import_module(module)
