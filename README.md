# AC Infinity AIRTAP (BLE) for Home Assistant

Home Assistant custom integration for Bluetooth Low Energy control of
[AC Infinity AIRTAP](https://acinfinity.com/register-booster-fans/) T-series
register booster fans, with best-effort support for other AC Infinity BLE
controllers that speak the same protocol (CONTROLLER 67 / 69 / 69 Pro device
types are recognized; only AIRTAP hardware is actively tested).

The BLE protocol library is vendored (no external Python requirements) and
descends from [hunterjm/ac-infinity-ble](https://github.com/hunterjm/ac-infinity-ble/).

## What it supports

Everything below is limited to what the reverse-engineered protocol code
provably speaks today. No guessed commands are ever sent to the hardware.

- **Fan entity** — OFF / ON with speed 1-10 (mapped to percentage), plus a
  preset for each mode the fan runs by itself: **Auto**, **Timer to On**,
  **Timer to Off** and **Cycle**.
- **Auto-mode configuration** — number/switch entities for high/low
  temperature triggers and their enable flags, and min/max fan speed bounds
  used by AUTO. Written as the same threshold block the vendor app writes.
- **Timer and cycle configuration** — number entities (in minutes) for the
  two countdown timers and for the cycle's on/off phases. These are the
  registers the fan's own control panel edits; the device stores them in
  seconds and keeps them independently of which mode is selected, so a
  duration can be prepared before the preset is switched.
- **Sensors** — temperature, fan speed; humidity and VPD only on device
  types that actually carry those sensors. The AIRTAP (type 6) has **no
  humidity sensor** (the device reports a constant 0), so no humidity
  entities are created for it.
- **Live push updates** — state follows BLE advertisements, and a
  lightweight GATT poll (at most every 30 s per fan, max 2 fans at a time)
  fills in state that advertisements cannot carry (work mode, speed bounds,
  auto thresholds, timer/cycle durations). Holding the connection silences
  a fan's advertisements almost entirely, so while a link is held the poll
  is driven by the link itself — on connect, then once a minute.
- **Held Bluetooth connection** (option, on by default) — the GATT link to
  each fan stays open, so a command lands in ~0.2 s instead of paying a 2-6 s
  proxy connect. It costs one of a proxy's three connection slots per fan and
  blocks the AC Infinity phone app while held; after a drop it reconnects with
  backoff, roaming to whichever proxy Home Assistant scores best at that
  moment. Turn it off per fan in the integration's *Configure* dialog.
- **Genuine availability** — entities go unavailable when no Bluetooth
  scanner/proxy has seen the fan for the tracked interval, and recover on
  the first frame seen again; a fan on a live held connection always counts
  as available.
- **Diagnostics** — a **Connection** sensor naming the proxy that currently
  carries the link (`disconnected` when there is none), with drop counts and
  the reconnect attempt as attributes; plus a download from the device page:
  advertisement age, RSSI, which proxy last saw the fan, connection stats,
  full (address-redacted) device state.
- **Recovery repair** — when a fan's own Bluetooth link has been down for 15
  minutes straight, a repair appears under *Settings → System → Repairs*
  whose Fix button walks up an escalation ladder: check again, reload the
  entry, restart the ESPHome proxy that last carried the fan (offered only
  when that proxy exposes a `restart_proxy` action), and finally power-cycle
  the fan through a switch entity you pick. Each step waits and re-reads the
  link before reporting back, and the repair clears itself the moment the
  link is up again — including after a reload, which is when a naive
  implementation leaves the issue orphaned on screen.

## Known protocol gaps (not implemented — on purpose)

The protocol enumerates twelve work modes (`OFF`, `ON`, `AUTO`, `TIMER ON`,
`TIMER OFF`, `CYCLE`, `SCHEDULE`, `VPD`, `TEMPERATURE PARAM`,
`HUMIDITY PARAM`, `ADVANCE`, `AI`). The first six are the ones the AIRTAP
control panel itself offers, and all six are commandable here, each with the
configuration register the device reports for it.

Modes 7-12 are **not** commandable: the AIRTAP answers with an empty group
for the `SCHEDULE` register and has no register at all for the rest, so
there is nothing to configure and no verified byte sequence to select them.
They can still be *observed* — if other hardware is running in one, the fan
reports the mode and the preset chip is left blank. Contributions with
verified captures from hardware that does support them are welcome.

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → *Custom repositories*.
2. Add `https://github.com/nphil/ac-infinity-airtap-hacs` as type
   *Integration*.
3. Install **AC Infinity Airtap** and restart Home Assistant.

### Manual

Copy `custom_components/ac_infinity/` into your `config/custom_components/`
directory and restart.

Fans advertising within range of a Bluetooth adapter or ESPHome Bluetooth
proxy are discovered automatically; otherwise add via *Settings →
Devices & Services → Add integration → AC Infinity Airtap*.

## Troubleshooting

### Debug logging

```yaml
logger:
  default: info
  logs:
    custom_components.ac_infinity: debug
```

The vendored BLE library logs under the same namespace, so this single line
captures advertisements, GATT connections and raw command/response hex.

### ESPHome proxy connection-slot economics

Each ESPHome Bluetooth proxy typically offers **3 concurrent connection
slots**, and how this integration spends them depends on the *Hold Bluetooth
connection* option:

- **held (default)** — one slot per fan, permanently, on whichever proxy Home
  Assistant picked for it. Budget accordingly: seven proxies give 21 slots.
  The **Connection** sensor shows which proxy holds each fan, so a heal
  automation can avoid restarting a proxy that other devices are using;
- **not held** — connections are opened only for polls and commands and
  released immediately afterwards (back-to-back commands reuse the live
  connection);
- either way, at most **2 fans poll concurrently across the whole
  integration**, so a fleet can never exhaust every slot at once and user
  commands always find headroom.

If commands still time out, you likely have more BLE devices than slots in
range of one proxy — add a proxy near the congested area.

### Signal strength (RSSI)

Check RSSI in the diagnostics download or the device page. Guidance from a
live six-fan fleet:

- **≥ -80 dBm** — solid.
- **-80 to -90 dBm** — advertisements are fine; GATT connects may need
  retries (handled automatically).
- **≤ -90 dBm** — workable but expect slower connects and occasional failed
  polls; place the nearest proxy closer if a fan is persistently stale.

`unavailable` entities mean *no scanner has seen the fan at all* recently —
that is a coverage/power problem, not a connection-slot problem.

## Credits

This project stands on a chain of prior work:

- [Jason Hunter (hunterjm)](https://github.com/hunterjm/ac-infinity-hacs) —
  original integration and the
  [ac-infinity-ble](https://github.com/hunterjm/ac-infinity-ble/) protocol
  library.
- [mtsphere](https://github.com/mtsphere/ac-infinity-airtap-hacs) — AIRTAP
  adaptation, AUTO mode and threshold support.
- rohorner — fixes and hardening this fork builds on.
