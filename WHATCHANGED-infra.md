# WHATCHANGED — InfraLayer (coordinator / __init__ / config_flow / device wrapper / diagnostics / metadata)

Maps every change to the live-verified bug or robustness gap it addresses.
Files owned: `custom_components/ac_infinity/{__init__,coordinator,config_flow,device,diagnostics}.py`,
`manifest.json`, `hacs.json`, `README.md`.

## coordinator.py

### Bug 2 (verified live): fleet-wide staleness — six entities froze `last_updated` for 8+ min while advertisements kept arriving, and stayed `available` on stale data

**Root cause** (also written into the `_async_handle_bluetooth_event`
docstring so it cannot be reintroduced silently): the handler did
`if MANUFACTURER_ID not in advertisement.manufacturer_data: return` *before*
calling `super()._async_handle_bluetooth_event()`. BLE splits payload across
ADV_IND and SCAN_RSP frames, and whether a dispatched frame carries the
manufacturer record depends on proxy scan mode/coalescing. In HA's
`ActiveBluetoothDataUpdateCoordinator` family, the `super()` call is the ONLY
place that (a) notifies entity listeners, (b) re-marks the device available,
and (c) evaluates `needs_poll` — **polling is advertisement-driven**, so the
early return didn't just skip one UI refresh, it silently disabled the 30 s
GATT poll cycle too. Result: during stretches of record-less frames, data
froze indefinitely while the address was genuinely still "seen" (hence
`available` — correctly — stayed true, making the staleness invisible).

**Fix**: every dispatched frame now flows through `super()` (listeners fire
and polls get scheduled unconditionally, per the parent contract "every
processed advertisement triggers listener updates"); only the state *merge*
remains conditional on the manufacturer record being present and parseable.
Malformed/truncated records (fringe-of-range reality at RSSI -91) are logged
and skipped without losing the frame's availability/poll value.

### Genuine availability

`BasePassiveBluetoothCoordinator` already registers
`bluetooth.async_track_unavailable`; entities inherit
`available = coordinator.available`. Previously this *mostly* worked by
accident but recovery was broken: after an unavailability episode, frames
without the manufacturer record could not flip `_available` back to true
(same early return). Now any dispatched frame restores availability, and
`_async_handle_unavailable` logs the transition + arms an "is online" log
for the first frame seen afterwards (operational visibility for a fleet on
marginal RSSI). Availability is deliberately **not** coupled to GATT poll
success (matches HA 2026.9 core semantics — verified against the shipped
`active_update_coordinator.py`): with scarce proxy slots, transient poll
failures are routine and must not flap entities while advertisement data
still flows.

### Bug 1 (verified live): successful BLE mode change not reflected for 8+ min

Defense-in-depth at the coordinator level (EntityLayer fixes the optimistic
write in `fan.py`): the coordinator now registers a controller callback and
forwards `NOTIFICATION` / `UPDATE_RESPONSE` events (GATT notify frames and
command/poll commits — LibHardening commits state after successful writes)
to `async_update_listeners()`. A command that lands is rendered immediately,
without waiting for the next advertisement or poll. `ADVERTISEMENT`
callbacks are explicitly not forwarded (the bluetooth event path already
notifies listeners; forwarding would double-render every frame).

### Connection economy (six fans, ~4 proxies × 3 slots, RSSI to -91)

- Module-level `_POLL_SEMAPHORE(2)`: at most two concurrent GATT polls
  across ALL config entries, so post-startup poll stampedes cannot exhaust
  every slot; user commands bypass the gate and always find headroom.
- `POLL_TIMEOUT = 45 s` around each poll: a pathological retry ladder
  against a weak-signal device becomes a clean failed poll instead of
  holding a poll slot hostage.

### Housekeeping

- `async_timeout` → stdlib `asyncio.timeout` (`requirements` is `[]`; the
  external package cannot be assumed present on HA 2026.9).
- Custom `ActiveBluetoothCoordinatorEntity` no longer re-implements core:
  it now subclasses `PassiveBluetoothCoordinatorEntity` (same exported name
  and generic signature — entity platform imports unchanged).

## device.py (integration wrapper)

- **Deleted** the `set_ble_device_and_advertisement_data` override.
  It existed to dodge a precedence bug in the vendored clamp block;
  LibHardening fixed that upstream (vendored merge also now records
  advertisement freshness via `mark_advertisement_received`, which the
  override would have bypassed). `dataclasses.replace` preserves
  `DeviceInfoEx` + `auto_mode` through the vendored merge.
- **Fixed** `__init__` state upgrade: `if self._state is DeviceInfo:`
  compared an instance to the class object (always false). Now a proper
  `isinstance` upgrade, so advertisement-only construction (config flow
  path) yields `DeviceInfoEx` and later merges can't drop `auto_mode`.
- **Fixed** `async_set_max_speed` writing its optimistic value to
  `level_off` (copy-paste from min-speed): min-speed entity jumped to the
  max value after every max-speed change. Now writes `level_on`.
- **Added** `update_ble_device()` so the coordinator can refresh the
  connect path from record-less frames without faking data freshness.
- Docstrings explain the poll floor (30 s), the hardware-safety rule (only
  existing command builders; modes 4-12 have none), and the short-response
  guard in `update()`.

## __init__.py

- **Added missing `async_unload_entry`**: entries previously could not be
  unloaded/reloaded cleanly and leaked `hass.data` on every attempt; also
  releases any held GATT connection (slot economy).
- **Setup ordering** now matches core BLE integrations (switchbot pattern):
  start coordinator → `async_wait_ready()` (raise `ConfigEntryNotReady`
  with a proxy-coverage hint if nothing parseable arrives in 30 s) →
  register data → forward platforms. Previously platforms were set up
  before the coordinator even started, and readiness was never awaited.
- **Entry-data normalization hardened** (`_device_info_from_entry_data`):
  filters stored dicts to known fields and coerces a serialized
  `auto_mode` block, so all six live entries — and any historical shape
  from the hunterjm/mtsphere/rohorner lineage — keep loading. Schema and
  unique_id patterns are untouched (FROZEN per contract).

## config_flow.py

- Manufacturer-ID guards preserved **exactly**; additionally the user-path
  picker now *collects* only AC Infinity advertisers (previously foreign
  devices were stored and merely display-filtered) and skips records that
  fail to parse (truncated fringe-of-range frames).
- Duplicate-flow handling: bluetooth step documents the implicit
  `already_in_progress` abort (`async_set_unique_id` default); the user
  picker now also excludes addresses with in-progress discovery flows.
- Error paths in the user submit now `await controller.stop()` (previously
  a failed validation connect leaked the GATT connection/slot).
- `FlowResult` → `ConfigFlowResult` (current core typing).

## diagnostics.py (new)

Standard config-entry diagnostics: entry data with the BLE address redacted
to its last 4 hex digits, full device state, advertisement age + RSSI +
which proxy last saw the device, coordinator availability/poll health, and
connectable-path/scanner counts. Consumes LibHardening's
`advertisement_age` / `connection_stats` via `getattr` (defensive; no merge
ordering constraint). No BLE traffic is generated by a diagnostics dump.

## manifest.json / hacs.json / README.md

- `version` 1.2.0; `documentation`/`issue_tracker` →
  `github.com/nphil/ac-infinity-airtap-hacs`; `codeowners` `@nphil`
  (credit lineage lives in README); `integration_type: device` added;
  `requirements` stays `[]` (vendored lib).
- `hacs.json` verified sane (name, `homeassistant: 2025.7.0` floor —
  py3.13 / `ConfigFlowResult` / PEP 696 syntax all satisfied well below it).
- README rewritten honestly: exact supported feature set (OFF/ON/AUTO,
  speeds, temperature, auto thresholds), AIRTAP type 6 has no humidity
  sensor, modes 4-12 documented as visible-but-not-commandable protocol
  gaps, install instructions, debug-logger snippet (single namespace covers
  the vendored lib), proxy slot economics, RSSI guidance, and the
  hunterjm → mtsphere → rohorner credit chain.
