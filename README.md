# dyson-nats-bridge

Bridge a Dyson purifier fan to NATS JetStream. Dyson devices run an MQTT
broker **on the device itself**; this service connects to it as a client
(via [libdyson-neon](https://github.com/libdyson-wg/libdyson-neon)),
normalizes the Dyson dialect into flat scalar JSON, and publishes to NATS.
Commands flow the other way on core NATS subjects.

```
Dyson fan (MQTT broker on device, :1883)
  ↕ dyson-nats-bridge
    → dyson.<device>.state        {"power": true, "speed": 4, "auto": false, ...}
    → dyson.<device>.environment  {"temperature_c": 21.5, "humidity": 52, "pm25": 3, ...}
    ← dyson.<device>.command.{power,speed,oscillation,night}  {"value": ...}
```

Design notes:

- All Dyson quirks are resolved here so downstream consumers (e.g.
  knx-nats-bridge writer rules) only see named scalars: `ON`/`OFF` become
  booleans, fan speed `AUTO` becomes `speed: 0` (0 = auto, 1-10 = manual),
  Kelvin becomes °C, and sleeping/failed sensors drop their field.
- State is event-driven (the device pushes `STATE-CHANGE`) plus a periodic
  poll (`POLL_INTERVAL`, default 60 s) that also refreshes sensor data.
- `/healthz` covers NATS and the logging pipeline only; an unreachable fan
  sets `dyson_connected 0` (Prometheus) instead of restart-looping the pod,
  since the device may legitimately be unplugged.

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

| Variable | Default | Description |
| --- | --- | --- |
| `DYSON_HOST` | — | Device IP/hostname on the LAN |
| `DYSON_SERIAL` | — | Device serial (MQTT username) |
| `DYSON_CREDENTIAL_FILE` | `/etc/dyson-nats-bridge/credential` | Local credential (from bootstrap) |
| `DYSON_PRODUCT_TYPE` | `438M` | MQTT topic prefix, e.g. `438M` = Purifier Cool PC1 |
| `DYSON_DEVICE_NAME` | — | Subject slug: `dyson.<name>.state` |
| `POLL_INTERVAL` | `60` | Seconds between state/sensor polls |
| `ENSURE_MONITORING` | `true` | Keep sensors reporting while the fan is off |
| `NATS_SERVERS` | `nats://localhost:4222` | Comma-separated server list |
| `NATS_NKEY_SEED_FILE` | — | NKey seed (or creds file / user+password file) |
| `NATS_STREAM_NAME` | `DYSON` | JetStream stream expected to cover `dyson.>` |
| `METRICS_PORT` | `9090` | `/metrics` + `/healthz` |

## Development

```sh
uv sync --extra dev
uv run ruff check .
uv run mypy src
uv run pytest
```
