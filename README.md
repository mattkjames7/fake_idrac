# Fake iDRAC fan server

A small in-memory server for testing software that controls Dell iDRAC 7/8
fans. It exposes a JSON HTTP API and enough IPMI over LAN behavior for common
fan-control commands. It is a test double, not a complete iDRAC emulator.

## Requirements

- Python 3
- [`cryptography`](https://cryptography.io/) for IPMI v2.0 encryption
- `ipmitool` to run the end-to-end tests and examples
- `pytest` to run the test suite

Install the Python dependency:

```sh
python3 -m pip install -r requirements.txt
```

## Run

```sh
python3 fake_idrac.py
```

The defaults are HTTP on `127.0.0.1:8000`, IPMI on UDP port `623`, six fans,
and 30% PWM. All options can be changed:

```sh
python3 fake_idrac.py --host 127.0.0.1 --port 8000 \
  --ipmi-port 6623 --fans 6 --pwm 30
```

The fake IPMI credentials are `user` / `pass`.

## Supported requests

| Interface | Request | Behavior |
|---|---|---|
| HTTP | `GET /state` | Complete fan and controller state |
| HTTP | `GET /fans` | List fans |
| HTTP | `GET /fans/<id>` | Read one fan |
| HTTP | `POST /fans/<id>` | Set PWM with `{"pwm": 40}` |
| IPMI | `raw 0x30 0x30 0x01 0x00` | Enable manual fan control |
| IPMI | `raw 0x30 0x30 0x01 0x01` | Restore automatic fan control |
| IPMI | `raw 0x30 0x30 0x02 0xff <PWM>` | Set every fan's PWM percentage |
| IPMI | `raw 0x30 0xce ...` | Query/set PCI cooling response and legacy fan offset |
| IPMI | `sdr type Fan` | Enumerate synthetic fan SDR records |
| IPMI | `sensor list/get/reading` | Read fan status and RPM |
| IPMI | `sensor thresh` | Update synthetic fan thresholds |
| IPMI | `chassis status`, `sel elist` | Return basic synthetic responses |

Both `ipmitool -I lan` (IPMI v1.5) and `ipmitool -I lanplus` (IPMI v2.0
RMCP+, cipher suite 3) are supported. See
[IDRAC-FAN-COMMANDS.md](IDRAC-FAN-COMMANDS.md) for the full command reference
and safety notes.

## Examples

Start the server on an unprivileged IPMI port:

```sh
python3 fake_idrac.py --ipmi-port 6623
```

Enable manual control and set all fans to 50% over IPMI v2.0:

```sh
ipmitool -I lanplus -H 127.0.0.1 -p 6623 -U user -P pass \
  raw 0x30 0x30 0x01 0x00
ipmitool -I lanplus -H 127.0.0.1 -p 6623 -U user -P pass \
  raw 0x30 0x30 0x02 0xff 0x32
```

Inspect state or set one fan through HTTP:

```sh
curl http://127.0.0.1:8000/state
curl -X POST -H 'Content-Type: application/json' \
  -d '{"pwm": 40}' http://127.0.0.1:8000/fans/1
```

## Tests

```sh
pytest -q
```

Tests invoke a real `ipmitool` client against ephemeral local ports. Tests
requiring `ipmitool` are skipped when it is not installed.
