# Tests & CI — what changed

New files only; no integration source was touched.

## Added

- `tests/` — plain-pytest suite, 129 tests, <1 s, no Home Assistant install
  required.
- `tests/ha_stubs.py` — minimal `homeassistant.*` stand-ins registered in
  `sys.modules` when real HA is not importable (skipped entirely if it is).
  Pure functions the values flow through (`ranged_value_to_percentage`,
  `percentage_to_ranged_value`, `slugify`) are faithful copies of HA's
  implementations; everything else is the thinnest importable shape.
- `tests/conftest.py` — path setup, stub installation, and
  `build_manufacturer_data()`: synthesizes advertisement payloads from the
  parser's own field layout (no captured hex blobs, no invented protocol).
- `.github/workflows/validate.yml` — hassfest, HACS validation
  (`ignore: brands` — personal fork is not in home-assistant/brands),
  and pytest on Python 3.13.
- `requirements_test.txt` — pytest + the real bleak stack the vendored
  library imports at module level (bleak, bleak-retry-connector,
  async-timeout) + voluptuous for the config-flow schema. Nothing else.

## Coverage

### Vendored library (pure python, `tests/test_util.py`, `test_protocol_parse.py`, `test_protocol_commands.py`)
- `crc16` verified against the published CRC-16/CCITT-FALSE check vector
  (`'123456789' -> 0x29B1`) — an implementation-independent proof it matches
  the wire CRC; windowed form equals slice form (what `_add_head` relies on).
- `get_bit` INVERTED vendor convention (True = bit clear) pinned explicitly
  so a well-meaning "fix" cannot silently corrupt every decoded flag.
- `get_short` signed big-endian (negative temperatures), `get_bits` MSB-side
  field extraction.
- `get_type` family-letter table (incl. AIRTAP type 6 -> "D") and full
  `get_mode` 1–12 table.
- `parse_manufacturer_data`: field-by-field against synthetic payloads;
  signed sub-zero temperature; `fan=0` parses as 0 (not None); vpd/choose_port
  section gated to E/F/G types at version >= 3 (type 6 never gets it);
  advertisements never carry `work_type`/`level_on`/`level_off`; short
  payloads raise instead of fabricating state.
- Frame construction (`_add_head`, `get_model_data`, `set_level`): header
  `A5 00`, big-endian length/sequence, both embedded CRCs recomputed and
  verified, command-type byte, exact payloads the builders define, E-family
  `[255, port]` suffix, `ValueError` on non-manual work types (AUTO has its
  own command path) and out-of-range levels. No new command bytes invented.

### Device wrapper (`tests/test_device_state_merge.py`)
- Advertisement merge updates environmental fields but MUST NOT wipe
  poll-only state (`work_type`/AUTO, `level_on/off`, `auto_mode`) — the
  invariant behind "fleet runs AUTO while advertisements stream in".
- Advertised `fan=0` overwrites a previous non-zero speed (root of the stale
  sensor bug).
- Advertisement merge fires the callback the coordinator listens to.
- `update_needed`: first poll always, <=30 s suppressed (connection-slot
  economy across six fans / ~4 proxies), >30 s polls, config-change flag
  forces an immediate poll.

### Hardened vendored controller (`tests/test_vendored_controller.py`)
Transportless: `_send_command`/`_execute_disconnect` replaced with recording
fakes on the instance; real command construction and state-commit logic runs.
- Honest-state rule: `set_speed`/`turn_on`/`turn_off` commit state and fire
  `UPDATE_RESPONSE` only after the send succeeds; a raised `BleakError`
  leaves state untouched, fires nothing, and still releases the connection.
- Exact `set_level` frame payloads sent for each verb (e.g. `set_speed(7)`
  on type 6 -> `[16, 1, 2, 18, 1, 7]`); `turn_on` defaults to the stored
  `level_on`; `turn_off` preserves the stored `level_off` (OFF is a real
  mode with its own level).
- `update()` response validation: good frame merges work_type/levels and
  syncs `fan` in OFF/ON modes but leaves the floating AUTO level alone;
  `None` and short (<19 byte) responses are ignored, never parsed.
- Advertisement clamp precedence: bounds clamp toward an observed live level
  (below `level_off` lowers it, above `level_on` raises it, in-range is a
  no-op, unknown bounds are not invented) — pins the fix for the
  `level_off or 0 > fan` operator-precedence clobber.
- Advertisement freshness: `last_advertisement_monotonic`/`advertisement_age`
  are None until the first advertisement and recorded by the merge.


### Regression tests for 2026-09-05 live bugs
- `tests/test_sensor_regression.py` — FanSpeedSensor reports the ACTUAL
  speed: 0 when stopped (the 8 -> 0 sequence must never retain 80), mapping
  1..10 -> 10..100, None only when the device has never reported a speed.
  Runs the real sensor module via `_handle_coordinator_update()`.
- `tests/test_fan_regression.py` — fan entity optimistic updates: preset
  "Auto" reflects immediately (state written + coordinator listeners poked,
  the 8-minute-stale-preset bug), speed changes clear the preset chip (the
  silent-AUTO-exit bug), percentage 0 turns off. Outcomes asserted, not call
  order.
- `tests/test_config_flow_guard.py` — user-step device list skips
  advertisements lacking manufacturer ID 2306 (foreign BLE devices in range
  must not crash or pollute the flow); an advert claiming ID 2306 with a
  truncated/unparseable payload is likewise skipped, not fatal; all-foreign,
  all-malformed, or empty discovery aborts with `no_devices_found`;
  pick-list labels come from the parsed payload. Drives the REAL
  `ConfigFlow.async_step_user` (HA base stubbed) — no extracted-logic
  shortcut was needed.
- `tests/test_module_imports.py` — every integration module (incl.
  diagnostics and the vendored package) imports under the stub layer;
  catches typo'd imports, syntax invalid on the target Python, and missing
  stub coverage with a clear error.

## Not covered (and why)

- GATT connect/retry plumbing (`_ensure_connected`, notify wiring,
  disconnect timers): requires a BLE transport double far heavier than unit
  value; the command/commit layer above it and the frame bytes it sends are
  covered instead.
- `pytest-homeassistant-custom-component` harness: not used — it drags a full
  pinned HA install; the stub layer keeps the suite <1 s and CI-cheap. If the
  repo later wants full-integration tests, that is the tool to add.

## Running

```
pip install -r requirements_test.txt
python -m pytest tests/ -q
```
