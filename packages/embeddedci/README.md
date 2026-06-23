# embeddedci (BenchPod pytest library)

A pytest-friendly Python client for an [EmbeddedCI](https://embeddedci.com)
**BenchPod** — the same device operations you do today with `benchpod-cli`,
available straight from your tests:

* connect to a BenchPod over **wifi/network, serial, or the cloud** (`embeddedci.com`)
* power the target on/off
* **flash** firmware to the target and **assert ok / not ok**
* emulate an I2C sensor (BMP280) and decode the bus traffic

The same test runs against a pod on your desk *or* a remote pod in CI: point
`--benchpod-connection` at an IP, a serial port, or `embeddedci:<device-name>` to
drive a named device through embeddedci.com from a GitHub Action (no secrets — it
authenticates with the workflow's GitHub OIDC token). See
[Running in GitHub Actions](#running-in-github-actions-cloud).

```python
from embeddedci import benchpod

with benchpod.BenchPod("192.168.1.213") as bp:   # or "/dev/ttyACM0", or "serial"
    bp.ping()
    bp.power_on(benchpod.INTERNAL)
    result = bp.flash(
        file="firmware.elf", target="target/stm32f1x.cfg",
        swclk=benchpod.PIN1, swdio=benchpod.PIN2, nreset=benchpod.PIN3,
        target_power=benchpod.INTERNAL,
    )
    assert result.ok
    bp.power_off(benchpod.INTERNAL)
```

## Install

```bash
pip install embeddedci
# for the cloud (embeddedci:<device>) destination, add the cloud extra:
pip install "embeddedci[cloud]"
```

The `[cloud]` extra pulls in a WebSocket client used only by the `embeddedci:` destination;
local wifi/serial use needs nothing extra.

### OpenOCD (required for flashing)

Flashing shells out to **OpenOCD**, which must be on your `PATH`. It drives the pod's SWD probe
through OpenOCD's `remote_bitbang` adapter in **SWD** mode — and **SWD support for `remote_bitbang`
only exists in OpenOCD *master* (post‑0.12.0)**. The stock packages (`apt install openocd`,
`brew install open-ocd`) are **0.12.0**, whose `remote_bitbang` is `jtag_only`; flashing with them
fails immediately:

```
Info : only one transport option; autoselect 'jtag'
Error: Can't change session's transport after the initial selection was made
```

Install a master snapshot instead — the easiest is **xPack OpenOCD**:

```bash
# macOS / Linux via npm (xpm), or grab a release tarball directly:
npm install -g @xpack-dev-tools/openocd
# or: https://github.com/xpack-dev-tools/openocd-xpack/releases  (extract, add bin/ to PATH)
```

Verify your OpenOCD can do SWD over `remote_bitbang`:

```bash
openocd -c "adapter driver remote_bitbang" -c "transport list" -c "exit"
# must list:  jtag  swd     (if only 'jtag', it's too old)
```

## Named constants (no magic numbers)

| Concept | Constants | Wire value |
|---|---|---|
| Target-power eFuse | `benchpod.INTERNAL`, `benchpod.EXTERNAL` | 1, 2 |
| LA pins (SWCLK/SWDIO/NRESET) | `benchpod.PIN1` … `benchpod.PIN12` | 1 … 12 |

Plain ints still work (`efuse=1`, `swclk=1`); they're validated and coerced.

## Using it in pytest

Installing the package registers a pytest plugin. Point it at a pod and use the
fixtures:

```bash
pytest --benchpod-connection=192.168.1.213
# or: export BENCHPOD_CONNECTION=serial
```

```python
def test_firmware_flashes(benchpod):
    benchpod.power_on(benchpod.INTERNAL)
    assert benchpod.flash(
        file="firmware.elf", target="target/stm32f1x.cfg",
        swclk=benchpod.PIN1, swdio=benchpod.PIN2,
    ).ok

# benchpod_target powers the target on for the test and off at teardown:
def test_with_powered_target(benchpod_target):
    ...
```

Without a connection configured the fixtures **skip** rather than fail, so the
suite stays green in CI runners without hardware.

## Connection strings

| Form | Transport |
|---|---|
| `192.168.1.213` or `host:8080` | wifi/network (JSON over TCP, port 8080 default) |
| `/dev/ttyACM0`, `COM3` | serial (USB CDC-ACM console) |
| `serial` / `usb` | serial, auto-detected by USB VID `0x2E8A` |
| `embeddedci:<device-name>` | cloud — drive a named device through embeddedci.com (CI only; see below) |

Resolution order: explicit argument / `--benchpod-connection` →
`benchpod_connection` ini option → `BENCHPOD_CONNECTION` env var.

## Running in GitHub Actions (cloud)

The `embeddedci:<device-name>` destination drives a pod that lives somewhere else
— behind NAT, in a lab — through `embeddedci.com`. Your workflow proves *which
repo it is* with a GitHub OIDC token; the server exchanges that for a short-lived
session token scoped to the devices that repo is allowed to drive, then bridges a
raw byte tunnel to the device. **The full API works** — including flashing (SWD)
and UART/scope captures — so the same test you run locally runs unchanged in CI.

One-time setup (in the EmbeddedCI web app):

1. Give the device a stable name on the **BenchPod** page (the editable name;
   letters/digits/`-_.`, unique per org).
2. On **BenchPod → GitHub Actions**, add your repo (click *Look up* to fill the
   numeric ids) and choose **Any device** or the specific device(s) this repo may drive.

Then in `.github/workflows/hil.yml`:

```yaml
permissions:
  id-token: write          # REQUIRED — lets the job mint a GitHub OIDC token
  contents: read

jobs:
  hil:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install "embeddedci[cloud]"
      - run: pytest --benchpod-connection=embeddedci:my-bench-01
```

No API key or secret is stored — auth is the GitHub OIDC token, exactly like PyPI
Trusted Publishing.

Config knobs (only the `embeddedci:` destination uses these):

| Option / env | Default | Purpose |
|---|---|---|
| `--benchpod-api-base` / `BENCHPOD_API_BASE` | `https://embeddedci.com` | embeddedci server base URL |

If the token can't be minted, the error says exactly why — one of: **not running
inside a GitHub Action**, the job is **missing `id-token: write` permission**, or
the **token request itself failed**.

## Status of features

| Feature | State |
|---|---|
| Connect (wifi + serial) | ✅ |
| Connect (cloud via GitHub OIDC) | ✅ server + SDK; device firmware tunnel pending on-hardware bring-up |
| Power on/off (+ scheduled delay) | ✅ |
| Flash + assert ok/not ok | ✅ (pure-Python OpenOCD `remote_bitbang` bridge) |
| Emulated I2C sensor (BMP280) | ✅ wifi + serial (serial via `json` console mode) |
| I2C bus decode (`benchpod.i2c`) | ✅ START/STOP, R/W, ACK, register reads |
| UART capture (`capture_uart`) | ✅ wifi + serial |
| Signal helpers (`capture`, `gpio_set`) | ✅ minimal, TCP transport only |

## Emulated I2C sensor + UART capture (HIL)

The pod can **pretend to be an I2C sensor** (a BMP280) on two LA channels while
you capture the DUT's UART — so you can flash an app, power-cycle it, and assert
on its boot output with and without the sensor present:

```python
from embeddedci import benchpod

with benchpod.BenchPod("192.168.1.213") as bp:
    bp.flash(file="app.elf", target="target/stm32f4x.cfg",
             swclk=benchpod.PIN13, swdio=benchpod.PIN14, target_power=benchpod.INTERNAL)

    # I2C is open-drain — enable the pod's pull-ups on SDA/SCL (LA1-8 only),
    # then have the pod become a BMP280 on those lines.
    bp.enable_pullup(benchpod.PIN1, benchpod.PIN2)
    bp.enable_i2c_sensor(benchpod.Sensor.BMP280, sda=benchpod.PIN1, scl=benchpod.PIN2,
                         temperature_c=22.5, pressure_pa=101000)

    # Power-cycle while capturing UART so the boot banner lands in the window.
    cap = bp.power_cycle_and_capture(rx=benchpod.PIN5, tx=benchpod.PIN6,
                                     delay=1.5, duration=6.0, until=r"APP_OK")
    assert cap.match("APP_OK")
    assert cap.match(r"chip id match=0x58|bmp280_detected=yes")
```

How the power-cycle works: `power_cycle_and_capture` powers the eFuse off, then
schedules a power-on `delay` seconds out (a **pod-side timer** via
`target_power`'s `delay_ms`), then enters UART capture — so the scheduled
power-on, and the DUT's boot output, land *inside* the capture window. This
sidesteps the firmware's one-connection-at-a-time limit (you can't send a
power-on while the UART proxy owns the link).

`rx` is the LA channel the pod **samples** (wire the DUT's TX here); `tx` is the
channel the pod **drives** (DUT's RX).

> **Serial gets the full JSON API too.** The pod's TCP API is JSON; the serial
> console is normally text-only. The firmware exposes a `json` console command
> that switches the console into the same JSON dispatcher (exit with
> `{"cmd":"json_exit"}`). The serial transport enters/leaves this mode
> automatically, so `enable_i2c_sensor(...)`, `i2c_sensor_la_decoded(...)`, etc.
> work over serial as well as wifi — no code change in your test.

### Decode the I2C bus

While the pod serves the emulated sensor it also samples the SDA/SCL lines. The
`benchpod.i2c` module turns that raw capture into decoded transactions, so you
can assert what the DUT *actually did* on the wire — not just that it booted:

```python
from embeddedci import benchpod
from embeddedci.benchpod import i2c

with benchpod.BenchPod("192.168.1.213") as bp:
    bp.enable_i2c_sensor(benchpod.Sensor.BMP280, sda=benchpod.PIN7, scl=benchpod.PIN8)
    bp.power_off(benchpod.INTERNAL); bp.power_on(benchpod.INTERNAL)  # DUT boots & probes

    txns = bp.i2c_sensor_la_decoded(samples=4096, sample_rate_mhz=0.5)
    print(i2c.format_transactions(txns))
    # -> S 0x76W+ 0xD0+ Sr 0x76R+ 0x58- P   (write reg 0xD0, read chip id 0x58)
    assert i2c.read_register(txns, 0x76, 0xD0) == [0x58]
```

The decoder handles START/repeated-START/STOP, the R/W bit, per-byte ACK/NACK,
and the common "write register pointer, then read" pattern (`read_register`). It
works on a `(scl, sda)` stream too (`i2c.decode_samples`) and ships a waveform
synthesizer (`i2c.synthesize`) so you can build and decode traces with no
hardware — see [`tests/test_i2c_decode.py`](tests/test_i2c_decode.py).

### Getting started: one clean HIL test

The "hello world" of EmbeddedCI HIL — flash → emulate a BMP280 → power-cycle →
assert on UART — is in [`examples/test_bmp280.py`](examples/test_bmp280.py), with
a wiring table and run command at the top:

```bash
pytest examples/test_bmp280.py \
    --benchpod-connection=/dev/tty.usbserial-0001 \   # or an IP for wifi
    --benchpod-firmware=path/to/your_app.elf
```

It uses the plugin's `benchpod`, `pins` and `firmware` fixtures and the
`@pytest.mark.hardware` marker, and **skips automatically** without a connection.
The pin map comes from `--benchpod-swclk/-swdio/-nreset/-uart-rx/-uart-tx/-i2c-sda/-i2c-scl/-efuse`
options (sensible defaults) so tests aren't hardcoded to your wiring. See
[`examples/README.md`](examples/README.md) for the full wiring table.

A more thorough multi-case version (present/absent + I2C-bus decode) lives in
[`tests/examples/test_bmp280_hil.py`](tests/examples/test_bmp280_hil.py).

## Releasing (maintainers)

Releases publish to [PyPI](https://pypi.org/project/embeddedci/) automatically
via [`.github/workflows/publish.yml`](.github/workflows/publish.yml) using PyPI
**Trusted Publishing** (OIDC) — no API token or secret is stored in GitHub.

**One-time setup** on PyPI (or first via [TestPyPI](https://test.pypi.org)):
project → *Settings → Publishing → Add a pending publisher* with
owner `embeddedci-com`, repo `embeddedci-python`, workflow `publish.yml`,
environment `pypi`.

This is a monorepo, so each package releases on its **own** tag — `publish.yml`
triggers on `embeddedci-v*` for this package (and `embeddedci-mcp-v*` for the MCP
server). To cut a release:

```bash
# 1. bump the version in pyproject.toml (e.g. 0.1.0 -> 0.2.0), commit it
# 2. tag and push — the tag is embeddedci-v<version> and must match the version
git tag embeddedci-v0.2.0
git push origin embeddedci-v0.2.0
```

The tag push builds the sdist + wheel, runs `twine check`, and publishes. The
git tag is the source of truth for what shipped; keep `embeddedci-v<version>`
equal to the `version` in `pyproject.toml`.

Build and verify locally before tagging:

```bash
python -m pip install build twine
python -m build           # -> dist/embeddedci-*.tar.gz and *.whl
twine check dist/*
```
