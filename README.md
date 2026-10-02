# evcc-alfen-uid-bridge

> Bridges EVCC and Alfen Single Pro-line chargers: reads RFID card UIDs from the Alfen local HTTPS API on vehicle connect events and automatically assigns the correct vehicle in EVCC via REST API, enabling deterministic vehicle identification without OCPP or ISO 15118.

---

## Home Assistant app

This repository can be added as a custom app repository on Home Assistant OS or a
Supervised installation. In **Settings → Apps → App store**, open the menu,
choose **Repositories**, and add this repository's Git URL. Install **EVCC Alfen
UID Bridge**, set the Alfen host and password, EVCC URL and loadpoint, MQTT
broker details, and the JSON `UID_VEHICLE_MAP`, then start it.

The app uses host networking so it can reach the charger and services on your
local network. Set `DRY_RUN` to `true` for the first run; after confirming the
logs identify the intended vehicle, turn it off to allow vehicle assignments.
The app configuration uses Home Assistant's secret field for passwords. Full
card UIDs are sensitive: keep them out of logs except during temporary card
discovery, and protect access to the app configuration.

The app image is built locally by Home Assistant. No prebuilt registry image is
configured in this repository.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Background & Rationale](#background--rationale)
3. [Architecture](#architecture)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Notifications](#notifications)
7. [Troubleshooting](#troubleshooting)

---

## Introduction

**evcc-alfen-uid-bridge** is a lightweight containerised Python service that solves a specific integration gap: when [EVCC](https://evcc.io) controls an Alfen Single Pro-line charger in EMS (Modbus) mode, it has no way to read the RFID card that started a session. Without knowing which card was tapped, EVCC cannot reliably determine which vehicle is connected, making features like per-vehicle charge limits, SoC tracking, and smart charging plans unreliable.

This bridge fills that gap by reading the card UID directly from the Alfen's local HTTPS management API and using it to set the correct vehicle in EVCC — automatically, on every plug-in.

---

## Background & Rationale

### How EVCC controls the Alfen

EVCC talks to the Alfen charger over **Modbus TCP** in EMS (Energy Management System) mode. In this mode EVCC acts as the energy manager: it reads measured currents and sets charge current limits. The charger handles all lower-level safety and OCPP communication independently.

This is a clean and reliable control path, but it comes with a limitation: **Modbus EMS mode exposes no RFID data**. EVCC cannot see which card was tapped, because that information lives in the OCPP/application layer of the charger, not in the Modbus register map.

### Why EVCC's built-in vehicle detection falls short

EVCC has a built-in vehicle detection mechanism that polls each configured vehicle's cloud API (e.g. BMW CarData) to determine which car is at the charger, based on GPS location or charging status. In practice this is unreliable:

- Cloud APIs have polling delays (30–60 s is common).
- A vehicle reported as "charging" via its app may be at a different location (e.g. at a public charger).
- Multiple vehicles from the same household will all show as "at home", making disambiguation impossible.
- The BMW CarData API in particular is known to be flaky and slow to update.

Worse, native detection doesn't just fail passively — it can **actively undo the bridge**. With the Alfen in EMS mode (no RFID exposed to EVCC), detection falls back to *status polling*: on plug-in EVCC picks whichever vehicle's cloud `Status()` reports "plugged in". If the car the bridge just selected reports "unplugged" because its cloud data is stale, EVCC flags the loadpoint as unidentified and **reverts the selection to a guest/unknown vehicle**, racing over the bridge's assignment. The bridge setup therefore **disables native detection** by pinning a default vehicle on the loadpoint (see [Installation step 5](#5-add-an-unknown-vehicle-and-disable-evccs-native-detection)).

### The Alfen local API as a side-channel

The Alfen Single Pro-line exposes a local HTTPS management API (the same one used by the MyEve app and ACE Service Installer). This API provides access to a structured device log that records every RFID tap event — **including the full card UID** — within seconds of it happening.

By combining:
- **EVCC's MQTT output** as a trigger (vehicle connected event)
- **The Alfen local API** as a source of truth for the RFID UID
- **EVCC's REST API** to assign the vehicle

...it is possible to deterministically identify the vehicle within seconds of plug-in, without relying on cloud polling.

### Why not OCPP?

OCPP is the standard protocol for charger-to-backend communication and does carry RFID/transaction data. However:

- EVCC's Alfen integration uses Modbus, not OCPP.
- Routing OCPP through EVCC would require replacing or proxying the charger's existing OCPP backend, which is complex and potentially disrupts other integrations (e.g. smart charging tariffs, load balancing from the grid operator).

The local API side-channel approach is non-intrusive and requires no changes to the charger's OCPP configuration.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      Host (Docker)                      │
│                                                         │
│  ┌──────────┐   Modbus TCP    ┌──────────────────────┐  │
│  │   EVCC   │◀───────────────▶│  Alfen Single        │  │
│  │  :7070   │                 │  Pro-line            │  │
│  └────┬─────┘                 │  10.0.40.66          │  │
│       │ MQTT publish          └──────────┬───────────┘  │
│       │ evcc/loadpoints/1/connected      │ HTTPS :443   │
│       │ evcc/status                      │              │
│       ▼                                  │              │
│  ┌───────────┐                           │              │
│  │ Mosquitto │  :1883                    │              │
│  └─────┬─────┘                           │              │
│        │ MQTT subscribe                  │              │
│        ▼                                 ▼              │
│  ┌───────────────────────────────────────────────┐      │
│  │         evcc-alfen-uid-bridge                 │      │
│  │                                               │      │
│  │  1. MQTT: receive connected=true              │      │
│  │  2. Alfen API: login, read log, find UID      │      │
│  │  3. Map UID → EVCC vehicle name               │      │
│  │  4. EVCC REST: POST /api/loadpoints/1/vehicle │      │
│  │                                               │      │
│  │  Also: on evcc/status=online (EVCC restart)   │      │
│  │  → set flag; on next connected=true,          │      │
│  │    re-identify via Alfen (72h lookback)       │      │
│  └───────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────┘
```

### Event flow

**Normal plug-in:**
1. Car is plugged in; driver taps RFID card on charger.
2. Alfen logs the card UID in its device log.
3. EVCC detects the car via Modbus and publishes `evcc/loadpoints/1/connected = true` to MQTT.
4. Bridge receives the MQTT event and logs in to the Alfen local API.
5. Bridge scans the device log, looking back up to 300 seconds (the tap always precedes the EVCC event).
6. Bridge extracts the UID, normalises it, and looks it up in `UID_VEHICLE_MAP`.
7. Bridge calls `POST /api/loadpoints/1/vehicle/{name}` on EVCC.

**Unplug:**
1. EVCC publishes `connected = false`.
2. Bridge calls `DELETE /api/loadpoints/1/vehicle` — EVCC returns to auto-detection.

**EVCC restart while car is connected:**
1. EVCC publishes `evcc/status = online`.
2. Bridge sets an internal flag and clears its cached vehicle state.
3. EVCC republishes `connected = true`.
4. Bridge logs in to Alfen and scans the log with a 72-hour lookback to re-identify the vehicle.
5. Bridge calls `POST /api/loadpoints/1/vehicle/{name}` on EVCC.

### Single-session constraint

The Alfen allows only **one** active management session at a time. The bridge holds the session only for the duration of each log read, then logs out. **Do not run the bridge alongside MyEve, ACE Service Installer, or any Home Assistant Alfen integration** — they compete for the same session slot. Between charging sessions the slot is free.

---

## Installation

### Prerequisites

- Docker and Docker Compose on the host running EVCC.
- EVCC configured with `mqtt` publishing enabled (see [EVCC MQTT docs](https://docs.evcc.io/docs/reference/configuration/mqtt)).
- The Alfen charger reachable on the network from the host (HTTPS port 443).
- Alfen admin password.

### 1. Clone the repository

```bash
git clone <your-repo-url> evcc-alfen-uid-bridge
cd evcc-alfen-uid-bridge
```

### 2. Create `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Fill in at minimum: `ALFEN_HOST`, `ALFEN_PASSWORD`, `EVCC_BASE_URL`, `MQTT_HOST`, and `UID_VEHICLE_MAP` (see [Configuration](#configuration) for how to discover UIDs).

### 3. Discover your RFID card UIDs (first run)

Run in discovery mode — this logs full UIDs and takes no action in EVCC:

```bash
LOG_LEVEL=DEBUG LOG_UID_PLAINTEXT=true DRY_RUN=true docker compose up
```

Tap your RFID card at the charger and watch the output. You will see lines like:

```
orchestrator: tag acquired uid_hash=... (plaintext: 04AABBCCDDEEFF)
```

Full UIDs come from two log patterns:
- `Reader 0 Got NFC tag: {UID}` — fires on every tap (preferred source).
- `Tag {UID} is authorised by server` — fires when the charger updates its whitelist.

> If only a short/truncated UID appears (e.g. `1234` instead of `12345678`), look for a `Tag X is authorised` line from a previous session, or check the OCPP transaction log in the charger's web UI at `https://<alfen-ip>`.

### 4. Update `.env` with the UID map

```
UID_VEHICLE_MAP={"04AABBCCDDEEFF": "bmwx130e", "12345678": "bmw320e"}
```

Vehicle names must match the `name:` field of the vehicle entries in your `evcc.yaml`.

### 5. Add an "Unknown" vehicle and disable EVCC's native detection

**5a. Define an `unknown` fallback vehicle.** It serves two purposes: a named target for unrecognised cards, and — more importantly — the loadpoint default that switches EVCC's own detection off (step 5b). Add to your `evcc.yaml`:

```yaml
vehicles:
  - name: unknown
    type: template
    template: offline
    title: Unknown
    capacity: 50     # set a sensible default capacity in kWh
```

**5b. Pin it as the loadpoint's default vehicle (recommended).** This is what stops EVCC's native detection from racing the bridge. Add `vehicle: unknown` to the loadpoint that drives the Alfen:

```yaml
loadpoints:
  - title: EV Charger
    charger: alfen        # your Alfen charger
    vehicle: unknown      # default vehicle → disables native status-based detection
    # ...rest of your loadpoint config
```

With a default vehicle configured, EVCC assigns it on plug-in and **never runs status-based auto-detection** (`vehicleUnidentified()` short-circuits while a vehicle is assigned). The bridge then overrides the selection via the REST API on each connect, and that choice **sticks for the whole session** — reset back to `unknown` only on unplug. Without this, EVCC can revert the bridge's assignment to a guest vehicle whenever a car's cloud status is stale (see [Why EVCC's built-in vehicle detection falls short](#why-evccs-built-in-vehicle-detection-falls-short)).

**5c. Enable the unknown-card fallback (optional).** If your charger is accessible to people other than the registered owners (family members, guests, visitors), make unrecognised cards charge under the `unknown` vehicle by setting in `.env`:

```
ON_UNKNOWN_TAG=default
DEFAULT_VEHICLE=unknown
```

> **Note:** EVCC's built-in "Guest vehicle" (visible in the UI dropdown) cannot be set via the REST API, so it is usable neither as a bridge target nor as a loadpoint default. The `unknown` vehicle defined above is a separate, API-accessible entry that works for both.

### 6. Run in dry-run mode to verify

```bash
DRY_RUN=true docker compose up
```

Plug in a car, tap the card, and confirm the logs show the correct vehicle being *selected* (without actually calling EVCC). Then stop the container and remove `DRY_RUN=true` (or set it to `false` in `.env`).

### 7. Start in production

```bash
docker compose up -d
```

---

## Configuration

All settings are via environment variables. The recommended approach is to keep them in `.env` (gitignored) and reference them from `docker-compose.yml`.

| Variable | Default | Required | Description |
|---|---|---|---|
| `ALFEN_HOST` | — | ✓ | Charger IP address (e.g. `10.0.40.66`) |
| `ALFEN_USERNAME` | `admin` | | Alfen management API username |
| `ALFEN_PASSWORD` | — | ✓ | Alfen admin password |
| `ALFEN_SOCKET` | `1` | | Socket number to match in state log lines |
| `ALFEN_TLS_VERIFY` | `true` | | Set `false` to skip TLS cert verification (Alfen uses self-signed cert) |
| `TAG_WAIT_TIMEOUT_S` | `15` | | Max seconds to poll for a tag after connect event |
| `TAG_POLL_INTERVAL_S` | `3` | | Seconds between Alfen log polls during wait |
| `LOGIN_RATE_MAX` | `5` | | Max Alfen logins allowed per rate window |
| `LOGIN_RATE_WINDOW_S` | `60` | | Rate limit window in seconds |
| `EVCC_BASE_URL` | — | ✓ | EVCC REST base URL (e.g. `http://127.0.0.1:7070`) |
| `EVCC_LOADPOINT_ID` | `1` | | Numeric EVCC loadpoint ID |
| `RELEASE_ON_DISCONNECT` | `true` | | Clear vehicle from EVCC on unplug |
| `ON_UNKNOWN_TAG` | `auto` | | `auto` = leave on auto-detection; `default` = set `DEFAULT_VEHICLE` |
| `DEFAULT_VEHICLE` | — | | Vehicle name to assign for unknown cards (requires `ON_UNKNOWN_TAG=default`) |
| `MQTT_HOST` | — | ✓ | MQTT broker hostname or IP |
| `MQTT_PORT` | `1883` | | MQTT broker port |
| `MQTT_USERNAME` | — | | MQTT username |
| `MQTT_PASSWORD` | — | | MQTT password |
| `MQTT_TOPIC_PREFIX` | `evcc` | | EVCC MQTT topic prefix (must match `evcc.yaml`) |
| `UID_VEHICLE_MAP` | — | ✓ | JSON object mapping normalised UID → EVCC vehicle name |
| `BACKOFFICE_CHECK_ENABLED` | `true` | | Check the charger's OCPP back-office connection at session start and notify if offline. Set `false` to disable the check and its notification entirely (see [Notifications](#notifications)) |
| `NOTIFY_URL` | *(HA webhook)* | | HTTP endpoint the offline alert is POSTed to as JSON `{"title","msg"}` |
| `LOG_LEVEL` | `INFO` | | Python log level (`DEBUG`, `INFO`, `WARNING`) |
| `LOG_UID_PLAINTEXT` | `false` | | Log full UIDs instead of hashes (personal data — discovery only) |
| `DRY_RUN` | `false` | | Log intended actions without executing them |

### UID normalisation

UIDs are normalised before matching: non-alphanumeric characters are stripped and the string is uppercased. This means `04:A1:B2:C3` and `04a1b2c3` both match `04A1B2C3` in the map.

### Truncated UIDs (state-line fallback)

The Alfen firmware logs RFID UIDs through three different log patterns. The two preferred patterns (`Reader 0 Got NFC tag` and `Tag X is authorised by server`) always carry the full UID. The fallback pattern — a socket state line such as `Socket #1: main state: …, tag: 5B9F` — can render a truncated UID (typically the first 2 bytes).

If the preferred patterns are not present in the 300-second lookback window, the bridge falls back to the state-line UID. To handle this gracefully, the bridge performs a **prefix match** when no exact map entry is found: if exactly one key in `UID_VEHICLE_MAP` starts with the found UID, that entry is used and a log line notes the match. If the prefix is ambiguous (matches more than one key), the bridge falls through to unknown-tag behaviour.

This means `UID_VEHICLE_MAP` only ever needs full UIDs — there is no need to add shortened variants as extra entries.

### BMW CarData vehicles

When configuring BMW vehicles in EVCC, the `clientid` is a **registered OAuth application ID**, not a per-account or per-vehicle identifier. It is the same for all vehicles in the same BMW ConnectedDrive account. If two vehicles belong to different BMW accounts, use the same community `clientid` for both — the VIN determines which car's data is returned. Each account must be separately authorised through the EVCC UI.

---

## Notifications

The bridge can send a single, advisory push notification: an **Alfen back-office offline alert**. At the start of each charging session it reads the charger's `BOConnection` status from the Alfen local API; if that is not `"online"` (the charger has lost contact with its OCPP back office), it fires one notification. The check is fire-and-forget on a daemon thread and never blocks or affects RFID/vehicle detection.

### Enable / disable

Controlled by a single flag, `BACKOFFICE_CHECK_ENABLED` (default `true`). Setting it to `false` disables the check *and* its notification — there are no other notifications in the bridge.

```dotenv
BACKOFFICE_CHECK_ENABLED=false
```

`docker compose restart` does **not** re-read `.env`; recreate the container to apply:

```bash
docker compose up -d --force-recreate
```

Verify with `docker logs <container> 2>&1 | grep backoffice_check`.

### Notification target (`NOTIFY_URL`)

The alert is a fire-and-forget HTTP `POST` to `NOTIFY_URL` with a JSON body:

```json
{ "title": "EV charger", "msg": "Alfen back office OFFLINE - ... (seen at session start HH:MM:SS DD/MM/YYYY)." }
```

Point `NOTIFY_URL` at anything that accepts this shape. The default is a **Home Assistant webhook** that relays the message to a mobile-app push notification (setup below). Any service that can parse a JSON `{title, msg}` POST works too.

### Home Assistant setup (webhook → mobile-app push)

This delivers the alert to the HA companion app without any extra notification service.

1. **Add a webhook automation.** In `automations.yaml` (or via the UI: *Settings → Automations → Create → from YAML*). Replace `<your_device>` with your companion-app device — list options in *Developer Tools → Actions* by searching `notify.mobile_app_`:

   ```yaml
   - id: evcc_notify_push
     alias: 'EVCC: push notification via mobile app'
     mode: queued
     max: 10
     trigger:
       - platform: webhook
         webhook_id: evcc-push-CHANGE-ME    # pick your own unguessable string
         local_only: true                   # only reachable from the local network
         allowed_methods: [POST]
     action:
       - service: notify.mobile_app_<your_device>
         data:
           title: '{{ trigger.json.title }}'
           message: '{{ trigger.json.msg }}'
   ```

   To notify several devices, point the action at a [notify group](https://www.home-assistant.io/integrations/group/#notify-groups) instead.

2. **Reload automations** (or restart HA) so the webhook registers.

3. **Set `NOTIFY_URL`** in `.env` to match your `webhook_id`. From a container on the HA host (`network_mode: host`), `127.0.0.1:8123` reaches HA:

   ```dotenv
   NOTIFY_URL=http://127.0.0.1:8123/api/webhook/evcc-push-CHANGE-ME
   ```

   Recreate the container (`docker compose up -d --force-recreate`).

4. **Test the path** independently of the bridge:

   ```bash
   curl -X POST http://127.0.0.1:8123/api/webhook/evcc-push-CHANGE-ME \
     -H 'Content-Type: application/json' \
     -d '{"title":"EV charger","msg":"test"}'
   ```

   A `200` plus a push on your phone confirms the HA side. Webhooks need no auth token — keep the `webhook_id` secret and `local_only: true` so it is only reachable on your LAN.

> The same webhook/automation can be shared by EVCC's own messaging (`messaging.services: [{type: custom, ...}]` POSTing the same `{title, msg}` JSON), so charge-session and back-office alerts land through one HA automation.

---

## Troubleshooting

**`401` errors from Alfen / bridge logs "login failed"**
Another client is holding the management session. MyEve, ACE Service Installer, and some Home Assistant integrations all compete for the same single session slot. Stop the other client and restart the bridge. The Alfen also rate-limits logins to ~5 per 60 seconds — repeated restarts can trigger this temporarily.

**"no RFID tag found within Xs window"**
The bridge polled the Alfen log but found no matching UID. Run with `LOG_LEVEL=DEBUG LOG_UID_PLAINTEXT=true` to see raw candidates. Possible causes:
- The card tap did not produce an NFC reader line in the log (uncommon).
- The connect event fired more than 300 seconds after the tap (e.g. the car was plugged in without a card, then a card was tapped later).

**Vehicle set to "unknown" / UID not matched despite card being in the map**
The bridge may have found only a truncated UID from the fallback state-line pattern (e.g. `5B9F` instead of `5B9F4379`). The bridge handles this with a prefix match, so it should resolve correctly as long as the map contains the full UID and no other entry shares the same prefix. Enable `LOG_LEVEL=DEBUG LOG_UID_PLAINTEXT=true` and look for `matched by prefix` in the log to confirm. If two cards share the same first 2 bytes the prefix is ambiguous and both must be present as full UIDs for the match to succeed.

**Vehicle set to wrong car**
Check `UID_VEHICLE_MAP` in `.env` — UIDs are normalised (uppercase, no separators). Run with `LOG_UID_PLAINTEXT=true` to confirm which UID is being detected. The two patterns (NFC reader vs. OCPP auth line) can produce different representations of the same card; the bridge prefers the NFC reader line.

**EVCC restarts and vehicle selection is lost**
The bridge subscribes to `evcc/status`. When EVCC comes back online and the car is still connected, the bridge performs a fresh 72-hour Alfen log scan to re-identify the vehicle and re-assigns it in EVCC. This also covers hard crashes where EVCC didn't publish `connected=false` before going down. No manual intervention needed.

**EVCC still shows "requesting authorization" for a BMW vehicle**
The OAuth callback URL (`http://<evcc-host>:<port>/...`) must be reachable from the browser you use to authorise. If you authorise from a remote device via VPN or Tailscale, the BMW redirect may land on the wrong host. Use a browser on the local network that can reach EVCC directly.

**`docker compose restart` does not pick up `.env` changes**
Use `docker compose up -d --force-recreate` instead. `restart` reuses the existing container environment.

## Running tests

```bash
pip install -r requirements.txt
pytest tests/
```

## Network requirements

The container uses `network_mode: host` — it shares the host network stack, so `127.0.0.1` reaches both EVCC and the MQTT broker directly.

Outbound access required:
- `<ALFEN_HOST>:443` — Alfen local HTTPS API
- `<EVCC_HOST>:<PORT>` — EVCC REST API
- `<MQTT_HOST>:1883` — MQTT broker
