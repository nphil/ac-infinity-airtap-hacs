# WHATCHANGED — vendored `ac_infinity_ble/` hardening

Scope: `custom_components/ac_infinity/ac_infinity_ble/` only (pure BLE
library; no homeassistant imports; zero new external dependencies — the
`async_timeout` dependency was actually *removed*). Public API unchanged
except documented additions. All 828 protocol frame permutations verified
byte-identical to pre-change git HEAD; no BLE byte sequences were added or
altered.

## protocol.py

- **Capability inventory module docstring** (documentation only, builders
  untouched): frame layout, the `[opcode, len, values...]` payload grammar
  proven by the five in-tree builders, byte-by-byte map of the
  `get_model_data` response as far as parsing proves it, advertisement
  layout, and the honest mode matrix — 12 modes named, only OFF/ON/AUTO
  drivable; modes 4–12 (TIMER/CYCLE/SCHEDULE/VPD/PARAM/ADVANCE/AI) have no
  command builder anywhere in the repo and cannot be selected over BLE.
  Inferences (Fahrenheit twin bytes, response-echo structure) are labelled
  `[INFERENCE]`.

## device.py — connection robustness

1. **Dead retry decorator revived.** `_send_command_locked`'s error paths
   disconnect and re-raise, but the old code never reconnected inside the
   `retry_bluetooth_connection_error` unit — every retry attempt hit a
   `None` client and died with `CharacteristicMissingError`, so one BLE
   hiccup failed the whole operation. `_ensure_connected()` now runs inside
   the retried unit (the upstream pyswitchbot pattern), so each attempt
   re-establishes the link. This is also why no extra 1/2/4s connect-retry
   loop was added: `establish_connection` (bleak-retry-connector) already
   does bounded, backed-off connect retries with ESP32-proxy out-of-slot
   handling, and the decorator now covers connection setup too — a third
   retry layer would multiply worst-case blocking and hog proxy slots.

2. **Disconnect-vs-in-flight-command race (AttributeError / lost
   response).** Teardown could null `self._client` between a command's
   connectivity check and its GATT write (`'NoneType' has no attribute
   'write_gatt_char'`), or kill the connection while the command awaited
   its notification. Fixes, layered:
   - The operation lock is now held across the *entire* round-trip
     (connect → write → wait), not just the write.
   - `_execute_disconnect(force=False)` is now **polite by default**: it
     refuses to tear down while another operation holds the operation lock
     (checked under the connect lock, so the check cannot race the
     command's own connect). Error paths and `stop()` pass `force=True`.
     Net effect: back-to-back commands reuse one connection instead of
     churning proxy slots.
   - The idle timer (`_disconnect`) reschedules itself instead of firing
     mid-command.
   - `_execute_command_locked` holds the client in a local so a *forced*
     teardown surfaces as a retryable `BleakError`, never an
     `AttributeError`.
   - Lock order is documented and uniform (operation → connect); teardown
     only peeks at the operation lock non-blockingly, so no deadlock.

3. **Unexpected disconnect mid-wait stalled commands 5 s.** The
   `_disconnected` callback now fails a pending notify future with a
   retryable `BleakError`, so the retry layer reconnects and resends
   immediately (measured 71 ms vs 5000 ms in the smoke harness). A
   stale-client guard ignores late callbacks from previous connections so
   they can't fail the *current* command's future.

4. **Leaked notify future.** If `write_gatt_char` raised, the old code left
   `self._notify_future` set; the next unsolicited 0x1E/FF broadcast would
   resolve that stale future and be swallowed instead of parsed. The future
   is now cleared in a `finally`.

5. **Proxy-slot leak on dying links.** `stop_notify` failing during
   teardown used to skip `disconnect()` entirely, holding an ESPHome proxy
   connection slot until supervisor timeout. Both teardown steps are now
   individually guarded. A pending idle timer is cancelled during teardown
   so it can't fire against a later connection.

6. **Removed-API crash + connection leak in `_ensure_connected`.** The
   characteristics-unresolved fallback called `BleakClient.get_services()`
   (removed in modern bleak) and then `start_notify(None)`, leaking the
   connection. Now: clear the service cache, disconnect, and raise
   `CharacteristicMissingError` (fail closed; next attempt refetches
   services). `start_notify` failure likewise tears down before re-raising,
   and `self._client` is only published once notifications are live, so no
   coroutine can observe a half-initialized connection.

7. **`stop()` interlocked.** Takes the operation lock before its forced
   disconnect, so unload can't yank a connection out from under an
   in-flight GATT write.

8. **`async_timeout` → stdlib `asyncio.timeout`.** The vendored library
   can't declare dependencies (manifest `requirements` is frozen at `[]`);
   depending on HA continuing to ship the deprecated `async_timeout`
   package was a time bomb. Python 3.13 stdlib only now.

## device.py — honest state

9. **Set-ops commit only after a confirmed write.** `turn_on` /
   `turn_off` / `set_speed` used to mutate `work_type`/`fan`/levels
   *before* sending and never rolled back — a failed BLE write left HA
   showing a state the fan never entered. They now build the frame,
   send, and only then commit; on success they fire
   `CallbackType.UPDATE_RESPONSE` so the coordinator can push the
   confirmed state to entities immediately (part of the fleet-staleness /
   stale-preset fix; the coordinator forwards these to
   `async_update_listeners`). Mutations mirror the `update()` parser
   invariants exactly (wt 1 ⇒ fan == level_off, wt 2 ⇒ fan == level_on).

10. **`update()` no longer parses garbage.** The response parser indexed
    `data[12]`/`data[15]`/`data[18]` with no length check — a short ack or
    stale response (responses are not sequence-correlated) meant
    `IndexError` or corrupted state. Responses shorter than 19 bytes are
    now logged and ignored; state and callbacks untouched. In AUTO
    (wt 3) the live fan level is deliberately left alone — advertisements
    keep it fresh.

11. **Advertisement clamp operator-precedence bug.** The merge step read
    `if self._state.level_off or 0 > self._state.fan:` which parses as
    `level_off or (0 > fan)` — every advertisement with a truthy stored
    bound clobbered `level_off`/`level_on` to the live fan level,
    fighting the real values learned from polls. Fixed to the evident
    intent: `(level_off or 0) > fan` lowers the minimum,
    `(level_on or 10) < fan` raises the maximum (no-ops in OFF/ON where
    fan equals the respective bound; only tightens bounds toward an
    observed AUTO level).

12. **Broadcast frame length guard.** The 0x1E/FF notification parser
    indexes up to `data[17]`; a truncated frame would IndexError inside a
    bleak callback. Guarded with `len(data) >= 18`.

## device.py — additions (new API, used by the coordinator)

13. **Advertisement freshness timestamp** for availability logic (the
    fleet froze for 8+ minutes on stale data while staying "available"):
    - `mark_advertisement_received(when: float | None = None)` — records
      `time.monotonic()`; called automatically by
      `set_ble_device_and_advertisement_data` and at construction when an
      advertisement is supplied.
    - `last_advertisement_monotonic: float | None` (property).
    - `advertisement_age: float | None` (property) — seconds since last
      parsed advertisement; `None` if never. Monotonic clock, so immune to
      wall-clock changes. Pure library: no homeassistant import.

## models.py / util.py / const.py

14. Documentation only: `DeviceInfo` field semantics (sources, units,
    merge-must-preserve-None rule, level_on/level_off meaning),
    `util.get_bit`'s deliberately inverted polarity (True == bit CLEAR —
    "fixing" it would flip every parsed flag), `crc16` identified as
    CRC-16/CCITT-FALSE (check value verified: `b"123456789"` → 0x29B1),
    MSB-indexed bit addressing, and `CallbackType.UPDATE_RESPONSE`'s
    widened meaning (poll response *or* confirmed command commit).

## Verification

- `python3 -m py_compile` passes on all library files.
- Smoke harness (stubbed bleak, real controller code) exercised: retry
  reconnection, polite-disconnect deferral, idle-timer deferral,
  commit-after-success + untouched state on total failure, short-response
  rejection, mid-wait disconnect fast-fail (71 ms), clamp precedence,
  freshness timestamps, and 828-frame byte-equality of `get_model_data` /
  `set_level` against git HEAD including `ValueError` validators.

## Known gaps (deliberate, per hardware-safety constraint)

- Modes 4–12 remain un-drivable: no proven byte sequences exist in the
  repo. Documented in protocol.py rather than guessed.
- Command responses are not sequence-correlated by the protocol code; the
  length guard rejects mismatched acks, which is the strongest check the
  proven protocol allows.
