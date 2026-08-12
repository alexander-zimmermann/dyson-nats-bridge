# dyson-nats-bridge

Bridge Dyson purifier fans to NATS JetStream. Dyson devices run an MQTT
broker **on the device itself**; this service connects to each one as a client
(via [libdyson-neon](https://github.com/libdyson-wg/libdyson-neon)),
normalizes the Dyson dialect into flat scalar JSON, and publishes to NATS.
Commands flow the other way on core NATS subjects.

```
Dyson fans (MQTT broker on device, :1883)
  ↕ dyson-nats-bridge                      # one process, N devices
    → dyson.<device>.state        {"power": true, "speed": 4, "oscillation_mode": 3, ...}
    → dyson.<device>.environment  {"pm25": 3, "pm10": 5, ...}
    ← dyson.<device>.command.{power,speed,oscillation,night,lock}  {"value": ...}
```

Design notes:

- One process serves any number of fans: a device list in YAML, one connection
  and poll loop per device, and a single wildcard command subscription
  (`dyson.*.command.>`) routed by the device token in the subject. Every
  device-scoped metric carries a `device` label.

- All Dyson quirks are resolved here so downstream consumers (e.g.
  knx-nats-bridge writer rules) only see named scalars: `ON`/`OFF` become
  booleans, fan speed `AUTO` becomes `speed: 0` (0 = auto, 1-10 = manual),
  Kelvin becomes °C, and sleeping/failed sensors drop their field.
- Oscillation is a mode enum mirroring the app's angle presets:
  `oscillation_mode` 0 = off, 1/2/3/4 = 45/90/180/350 degrees (the device's
  numeric `ancp` width on newer models). The `.command.oscillation` value
  uses the same enum; booleans still work (true = on with the last angle).
- Environment carries whatever the device's sensors report — entry-level
  models (e.g. PC1) only have the particulate sensor (`pm25`/`pm10`).
- `command.lock` is not a device capability; the fan has no such concept. It is
  a bridge-side flag (`true` = locked) that makes the other four commands
  no-ops, so a KNX/Basalte lock can hold a device at its current setting.
  Unlocking always gets through, a swallowed command produces no status write
  (`state.locked` reflects the lock itself, not each command it swallows), and
  the flag is restored from the last archived state message on startup so a
  restart cannot silently unlock a device.
- State is event-driven (the device pushes `STATE-CHANGE`) plus a periodic
  poll (`POLL_INTERVAL`, default 60 s) that also refreshes sensor data.
- `/healthz` covers NATS and the logging pipeline only; an unreachable fan
  sets `dyson_connected{device=…} 0` (Prometheus) instead of restart-looping
  the pod, since a device may legitimately be unplugged. A missing credential
  file, on the other hand, fails startup — that is misconfiguration, not an
  operational condition.

## Devices

Non-secret device details live in a YAML file (`DYSON_DEVICES_FILE`), one local
MQTT credential per device in `DYSON_CREDENTIALS_DIR/<name>`. `name` is the
subject slug (`dyson.<name>.state`) and is deliberately decoupled from the
serial, so a device can be swapped without breaking consumers.

```yaml
devices:
  - name: ventilator-1
    host: ventilator-1.example.com
    serial: XX1-EU-ABC1234A
    product_type: "438M" # optional, defaults to 438M
```

Duplicate names, an empty list, and unknown keys are rejected at startup.

## One-time credential bootstrap

Newer Dyson devices only hand out their local MQTT credential via the
MyDyson cloud API. Fetch it once (locally, never in the cluster), then
everything runs LAN-only:

```sh
uv run dyson-cloud-fetch --email you@example.com --region DE --host <device-ip>
```

This prints serial, product type, and the decrypted local credential, and
(with `--host`) smoke-tests a local MQTT connection against the device.

## Configuration (env)

| Variable                | Default                               | Description                                    |
| ----------------------- | ------------------------------------- | ---------------------------------------------- |
| `DYSON_DEVICES_FILE`    | `/etc/dyson-nats-bridge/devices.yaml` | Device list (see above)                        |
| `DYSON_CREDENTIALS_DIR` | `/etc/dyson-nats-bridge/credentials`  | One credential file per device name            |
| `POLL_INTERVAL`         | `60`                                  | Seconds between state/sensor polls, per device |
| `ENSURE_MONITORING`     | `true`                                | Keep sensors reporting while the fan is off    |
| `NATS_SERVERS`          | `nats://localhost:4222`               | Comma-separated server list                    |
| `NATS_NKEY_SEED_FILE`   | —                                     | NKey seed (or creds file / user+password file) |
| `NATS_STREAM_NAME`      | `DYSON`                               | JetStream stream expected to cover `dyson.>`   |
| `METRICS_PORT`          | `9090`                                | `/metrics` + `/healthz`                        |

## Development

```sh
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest
```
