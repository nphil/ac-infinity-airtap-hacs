# Entity-layer changes (fan.py, sensor.py, number.py, switch.py, strings.json, translations/en.json)

Each change below maps to a bug verified live on real hardware (six AIRTAP T-series
fans, HA 2026.9, ESPHome Bluetooth proxies), or to dead/missing translation coverage.
No BLE commands were added or altered; every write path uses the pre-existing
command builders.

## fan.py

### Bug 1 — stale preset after switching to Auto (8+ min live)
`async_set_preset_mode` performed the BLE write (`set_mode_auto()`) but never wrote
optimistic entity state, so HA displayed the previous preset until the next
advertisement/poll refresh — 8+ minutes on a congested proxy.
**Fix:** after the awaited `set_mode_auto()` succeeds (placed after the await so a
failed BLE write raises without falsely claiming Auto), set
`_attr_preset_mode = PRESET_AUTO_MODE`, `_attr_is_on = True`, call
`async_write_ha_state()` and `coordinator.async_update_listeners()` — the same
optimistic pattern `async_set_percentage` already used. `set_mode_auto()` also sets
`state.work_type = 3`, so the listener nudge re-derives the same state for sibling
entities.

### Bug 4 — silent exit from Auto on manual speed / turn-on
`async_set_percentage` and `async_turn_on` drive the device to manual
(work_type 2, or 1 for speed 0) — correct HA fan semantics — but left
`_attr_preset_mode` claiming "Auto" until a later refresh.
**Fix:** both paths now optimistically clear `_attr_preset_mode = None` before
`async_write_ha_state()`, so the UI never claims Auto while the device runs
manually. (`async_turn_off` was intentionally left untouched: only the two
manual-transition paths named in the verified bug were changed, and
`_update_attrs` already clears the preset on the next state refresh.)

## sensor.py

### Bug 3 — FanSpeedSensor reported retained speed while stopped (showed 80%)
`_update_attrs` cached `_last_speed` and reported it whenever current speed was 0.
**Fix:** the `_last_speed` cache is deleted. The sensor reports the actual
`state.fan`: explicit `0` when stopped/off, converted percentage when running,
`None` only when the device has never reported a speed at all.
Deliberately **not** gated on `work_type == OFF`: OFF mode on these devices is
itself a level (`level_off`, the "off speed"; the vendored `update()`/`turn_off()`
model work_type 1 as `fan = level_off`), so forcing 0 whenever the mode is OFF
would misreport blades genuinely spinning at a nonzero off speed. The advertised
`fan` byte is the device's own current-level report, refreshed every few seconds,
and reads 0 when truly stopped — which is exactly the verified bug scenario.

Additionally `_attr_entity_registry_enabled_default = False`: the sensor duplicates
`fan.percentage` and exists for history/statistics users. The entity registry
remembers prior enable/disable choices, so the six live (user-disabled) sensors are
untouched; only fresh registrations start disabled.

## number.py / switch.py

### Bug 5 — humidity thresholds on a humidity-less device
`AutoModeConfig` carries high/low humidity fields, but AIRTAP type 6 has no
humidity sensor (`hum` is always 0.0). **No gating code was needed:** the platforms
never created humidity threshold entities, so there is nothing to gate. Comments in
both `async_setup_entry` functions now document that the absence is deliberate
(a humidity knob would configure a trigger the device can never evaluate), so a
future contributor does not "complete" the AutoModeConfig surface blindly.
No behavioral change; all six auto-mode config knobs remain, all with
`entity_category = EntityCategory.CONFIG` (set on the `ACInfinityNumber` /
`ACInfinitySwitch` base classes):

| Knob | Platform | entity_category |
|---|---|---|
| Min Speed | number | config |
| Max Speed | number | config |
| Auto Mode High Temperature | number | config |
| Auto Mode Low Temperature | number | config |
| Auto Mode High Temperature Trigger | switch | config |
| Auto Mode Low Temperature Trigger | switch | config |

## strings.json / translations/en.json — dead and missing flow strings
The files described a `confirm` step and a `single_instance_allowed` abort that the
config flow never uses, while the strings the flow *does* surface were uncovered.
Rewritten to match `config_flow.py` exactly: `flow_title` (`{name}` from bluetooth
discovery title placeholders), `user` step with the `address` selector, errors
`cannot_connect`/`unknown`, aborts `no_devices_found`/`already_configured`/
`already_in_progress`. Custom integrations cannot resolve `[%key:...]` references
at runtime, so both files carry identical literals.

## unique_id proof — nothing orphaned, nothing duplicated

Every constructor call is unchanged in name and pattern; no entity was added or
removed. Patterns (with `slugify(name)`):

- fan: `f"{address}_{slugify(name)}"` → `{address}_fan`
- sensor: `f"{address}_{slugify(name)}"` → `{address}_temperature`,
  `{address}_fan_speed`, `{address}_humidity` (non-type-6 only, unchanged gate),
  `{address}_vpd` (FAMILY_E v3+ only, unchanged gate)
- number: `f"{address}_number_{slugify(name)}"` → `{address}_number_min_speed`,
  `{address}_number_max_speed`, `{address}_number_auto_mode_high_temperature`,
  `{address}_number_auto_mode_low_temperature`
- switch: `f"{address}_switch_{slugify(name)}"` →
  `{address}_switch_auto_mode_high_temperature_trigger`,
  `{address}_switch_auto_mode_low_temperature_trigger`

The six live config entries (schema `address` + `service_data`, domain
`ac_infinity`, untouched) therefore re-attach to exactly the same registry entries
after upgrade.
