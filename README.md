# EVCC Alfen UID Bridge

Home Assistant app that reads RFID card events from an Alfen Single Pro-line
charger and assigns the matching vehicle to an EVCC loadpoint.

This fork packages the original project as a Home Assistant app. The original
project was created by **Sebastiaan Van Baelen**: [sevba/evcc-alfen-uid-bridge](https://github.com/sevba/evcc-alfen-uid-bridge).

## Requirements

- Home Assistant OS or a Supervised installation with the Apps feature.
- EVCC running as a Home Assistant app, with MQTT publishing enabled.
- An Alfen Single Pro-line charger reachable from Home Assistant on HTTPS port
  443, plus its local API username and password.
- An EVCC vehicle for each card, with the exact vehicle names available for the
  UID map below.

EVCC MQTT publishing must use the configured topic prefix (default: `evcc`).
Set the app's loadpoint ID to the EVCC loadpoint that controls the Alfen. To
prevent EVCC's own vehicle detection from replacing the bridge's selection,
configure that loadpoint with an API-accessible fallback vehicle such as
`unknown`.

## Install

In Home Assistant, open **Settings → Apps → App store**, open the menu, choose
**Repositories**, and add:

```text
https://github.com/tarbax/evcc-alfen-uid-bridge
```

Install **EVCC Alfen UID Bridge** from the repository. Home Assistant builds
the app image locally.

## Configure

Enter the Alfen host, username, and password, the EVCC loadpoint ID, and the
RFID UID map. The settings page includes field descriptions. EVCC and the
Home Assistant MQTT service are discovered automatically by default.

The UID map is a JSON object whose keys are card UIDs and whose values are
exact EVCC vehicle names:

```json
{"04AABBCCDDEEFF":"car_one","12345678":"car_two"}
```

Use your own UIDs and vehicle names. Keep UIDs private and leave plaintext UID
logging disabled outside temporary card discovery.

### Automatic discovery and manual settings

The app discovers one installed EVCC app and the MQTT service published through
Home Assistant's Supervisor. Leave `EVCC_BASE_URL` and `MQTT_HOST` empty to use
discovery. If EVCC cannot be selected automatically, enter its API base URL,
for example `http://192.168.1.10:7070`. If no MQTT service is available, enter
the broker host and, if needed, port, username, and password. Manual values
take precedence over discovery.

The app uses Home Assistant's internal app network for EVCC and MQTT, and can
reach the Alfen charger over the local network. Do not run another client that
logs in to the Alfen management API at the same time; the charger allows one
management session at a time.

## First start

`DRY_RUN` is enabled by default. Start the app and check its logs to confirm
that EVCC and MQTT connect, the charger is reachable, and the UID map selects
the intended vehicle. The Alfen API login is confirmed on the first RFID log
scan; the startup reachability check does not verify the password. Once the
mapping is correct, disable `DRY_RUN` to allow the app to change vehicle
selection in EVCC.

## Optional back-office notification

When enabled, the app checks the Alfen's OCPP back-office status at the start
of a charging session. To receive an alert when it is offline, configure
`NOTIFY_URL` with a local-only Home Assistant webhook that accepts a JSON body
containing `title` and `msg`. The notification does not affect RFID detection
or vehicle assignment.

## Troubleshooting

- **EVCC not discovered:** set `EVCC_BASE_URL` to the EVCC API URL. Automatic
  discovery requires exactly one installed EVCC app whose name or slug contains
  `EVCC`.
- **MQTT not discovered:** enter `MQTT_HOST` and any required connection
  details manually. Confirm that EVCC publishes to the configured topic prefix.
- **Alfen login fails:** check the host and credentials, and ensure MyEve,
  ACE Service Installer, and other Alfen clients are not holding the charger's
  management session.
- **Card not matched:** check the exact UID and EVCC vehicle name in the JSON
  map. Keep `LOG_UID_PLAINTEXT` off except while temporarily discovering a
  card UID.
