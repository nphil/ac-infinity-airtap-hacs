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

- **Fan entity** — OFF / ON with speed 1-10 (mapped to percentage), plus an
  **Auto** preset that puts the device into its onboard AUTO mode.
- **Auto-mode configuration** — number/switch entities for high/low
  temperature triggers and their enable flags, and min/max fan speed bounds
  used by AUTO. Written as the same threshold block the vendor app writes.
- **Sensors** — temperature, fan speed; humidity and VPD only on device
  types that actually carry those sensors. The AIRTAP (type 6) has **no
  humidity sensor** (the device reports a constant 0), so no humidity
  entities are created for it.
- **Live push updates** — state follows BLE advertisements; a lightweight
  GATT poll (at most every 30 s per fan, max 2 fans at a time) fills in
  state that advertisements cannot carry (work mode, speed bounds, auto
  thresholds).
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

## Known protocol gaps (not implemented — on purpose)

The protocol enumerates twelve work modes (`OFF`, `ON`, `AUTO`, `TIMER ON`,
`TIMER OFF`, `CYCLE`, `SCHEDULE`, `VPD`, `TEMPERATURE PARAM`,
`HUMIDITY PARAM`, `ADVANCE`, `AI`), but the reverse-engineered command
builders only exist for **OFF, ON and AUTO**. The other modes:

- can be *observed* (if you set them from the official app, the integration
  reports the fan as running in an unsupported preset),
- cannot be *commanded* from Home Assistant, because no verified byte
  sequences exist for them and this integration does not invent BLE writes.

If you need timers/schedules, set them in the AC Infinity app; Home
Assistant automations plus OFF/ON/AUTO cover the rest. Contributions with
verified captures are welcome.

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
