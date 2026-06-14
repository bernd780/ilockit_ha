# I LOCK IT — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Custom [Home Assistant](https://www.home-assistant.io/) integration for the
**I LOCK IT** smart bike lock by haveltec GmbH (NoBond / Plus / Pro / GPS, the
`ILOCKIT-…` models that talk encrypted BLE).

It controls the lock over Bluetooth (open/close + settings) and shows the lock's
location from the haveltec cloud.

> **Unofficial & not affiliated with haveltec GmbH.** The BLE protocol was
> reconstructed by reverse-engineering the official app for interoperability with
> your *own* locks. It only ever uses credentials from your own I LOCK IT account.
> Use at your own risk — see [Caveats](#caveats).

## Features

| Entity | Type | Source | Notes |
|---|---|---|---|
| Lock | `lock` | BLE | Open / close |
| Battery | `sensor` | BLE | Read on each open/close or via *Refresh* |
| Location | `device_tracker` | Cloud | GPS position from the haveltec backend |
| Location source | `sensor` | Cloud | `phone` (app) vs `lock_gps` (SIM) — see below |
| Firmware | `sensor` | Cloud | Diagnostic |
| Color code | `sensor` | Cloud | Decoded to colour names |
| Theft protection | `binary_sensor` | Cloud | 24h theft check (DEA-series only) |
| Refresh status | `button` | BLE | One BLE session: read battery + state |
| Acoustic signal | `button` | BLE | "Find my lock" beep |
| Stop alarm | `button` | BLE | |
| Alarm | `switch` | BLE | On/off (+ sensitivity select) |
| Do not disturb | `switch` | BLE | |
| Automatic open/close | `switch` | BLE | |
| Closing / Opening / Warning sound | `switch` | BLE | |
| Alarm sensitivity | `select` | BLE | Levels 1–4 |

Plus three **services** on the lock entity for recovery/advanced use:
`ilockit.enter_pairing_mode`, `ilockit.reset`, `ilockit.unpair`.

## How it works

The integration has two independent sides:

* **Cloud side** (location, source, firmware, colour code, theft protection) —
  read-only HTTPS polling of the haveltec backend. Never touches Bluetooth.
* **BLE side** (lock/unlock, battery, settings) — connects on demand through any
  connectable Home Assistant Bluetooth proxy/adapter, runs the authorize
  handshake and issues the command, then disconnects.

## Requirements / preconditions

* **Home Assistant 2024.1 or newer.**
* An **I LOCK IT (haveltec) account** — see
  [Why your I LOCK IT account is required](#why-your-i-lock-it-account-is-required).
* **Cloud entities** (location, source, firmware, colour code, theft protection)
  need only an internet connection — **no Bluetooth at all**.
* **BLE entities** (lock/unlock, battery, buttons, switches) need a
  **_connectable_ Bluetooth proxy or adapter** within radio range of the lock:
  * an **ESP32 running ESPHome** with `bluetooth_proxy:` and **`active: true`**, or
  * a **built-in or USB Bluetooth adapter on the Home Assistant host** in range.
* During BLE actions the lock should be **awake / on USB power**, and the phone
  app **not connected** at the same moment.

> ⚠️ **A Shelly (or any scanner-only BLE proxy) cannot control the lock.**
> Shelly Bluetooth proxies only forward *advertisements* — they can **see** the
> lock but cannot open the GATT connection needed to control it. Lock control
> therefore requires a **connectable** proxy/adapter (ESP32 ESPHome
> `bluetooth_proxy: active: true`, or a local adapter). Without one, the BLE
> entities report *"not in range of a connectable Bluetooth proxy/adapter"*. A
> Shelly may still help as an extra advertisement scanner, but it does **not**
> replace a connectable proxy.

### Setting up a connectable ESP32 proxy

The easiest route is the ready-made **ESPHome Bluetooth Proxy** firmware: flash it
from your browser at <https://esphome.io/projects/> (choose *Bluetooth Proxy*) and
adopt it in Home Assistant.

To build it yourself, the key lines are `esp32_ble_tracker` plus `bluetooth_proxy:`
with **`active: true`** — that flag is what makes the proxy *connectable*:

```yaml
esphome:
  name: ilockit-proxy

esp32:
  board: esp32dev
  framework:
    type: esp-idf        # recommended for Bluetooth proxies

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

api:
  encryption:
    key: !secret api_encryption_key

ota:
  - platform: esphome

logger:

# --- the important part: a *connectable* Bluetooth proxy ---
esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

Place the ESP32 within radio range of the lock. A **dedicated** ESP32 (ideally one
with PSRAM) is recommended — adding a Bluetooth proxy to an ESP32 that already runs
a busy configuration can exhaust its memory.

## Why your I LOCK IT account is required

The lock has **no open/local API**, and the key needed to talk to it is **not
fixed on the device** — it is created when the lock is paired in the official
app. To control the lock over Bluetooth you must prove you are an authorized
owner using per-lock secrets:

* an **auth id** and **client/phone id** (the authorization slot), and
* a **long-term key (LTK)** that encrypts the BLE messages.

These secrets are **synchronised to your haveltec account** (the backend is a
Traccar server). During setup the integration signs in to your account **once**
to fetch each lock's **auth id, client id and LTK**, plus the **device id** used
to read its location. From then on they are stored in the Home Assistant config
entry and used locally — nothing is sent anywhere except your own haveltec
backend.

The same login also powers the **location** (`device_tracker`) and the cloud
diagnostic sensors, which read from the haveltec cloud directly.

In short: the e-mail/password are simply your **I LOCK IT app login**, used only
to retrieve *your own* locks' keys and location — never shared with any third
party.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/bernd780/ilockit_ha` with category **Integration**.
3. Install **I LOCK IT Smart Bike Lock**, then restart Home Assistant.

### Manual

Copy `custom_components/ilockit/` into your Home Assistant `config/custom_components/`
directory and restart Home Assistant.

## Configuration

1. **Settings → Devices & Services → Add Integration → I LOCK IT.**
   (Locks in Bluetooth range are also auto-discovered.)
2. Sign in with your I LOCK IT (haveltec) account email and password.
3. Pick the lock to add. The integration resolves the BLE address from the cloud
   (or from a live advertisement if the cloud has no MAC for that lock), so the
   lock should be in Bluetooth range when adding it.

Repeat to add more locks.

## Caveats

* **BLE actions briefly take control away from the phone app.** Every Bluetooth
  session authorizes with the owner slot, which evicts the app's active control.
  After using a BLE entity you may need to re-authorize the lock once in the
  I LOCK IT app. Because of this the integration does **no background BLE
  polling** — battery/state are read only on open/close or when you press
  *Refresh status*.
* **Battery & lock state are unknown until the first BLE session** (after the
  first open/close or *Refresh*).
* **Location source.** The lock's own GPS (over its SIM) only reports when the
  bike is reported stolen or a theft alarm is triggered (lock closed + alarm
  armed + movement while you are out of Bluetooth range). In normal use the
  tracker shows the last phone position (`source = phone`); `source = lock_gps`
  realistically only appears during a theft/alarm event. The 24h theft check and
  GPS tracking require a DEA-series I LOCK IT PRO; the PRO Lite (DAA) has no GPS.
* **Verification status.** Open/close, battery & state reads and pairing mode are
  verified against real hardware. The other settings writes (alarm, sound,
  automatic, do-not-disturb) were derived from the app: the message framing is
  verified but the exact payload bytes were not tested on a device — try them on
  a lock you are not actively using in the app first.
* `reset` and `unpair` are **destructive** (they clear the lock's settings /
  pairing) and are exposed as services, not buttons, so they cannot be tapped by
  accident.

## License

[MIT](LICENSE). Not affiliated with or endorsed by haveltec GmbH. "I LOCK IT" is
a trademark of its respective owner.
