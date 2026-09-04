# embeddedci (BenchPod pytest library)

A pytest-friendly Python client for an [EmbeddedCI](https://embeddedci.com)
**BenchPod** — the same device operations you do today with `benchpod-cli`,
available straight from your tests:

* connect to a BenchPod over **network, USB, or the cloud** (`embeddedci.com`)
* power the target on/off
* **flash** firmware to the target and **assert ok / not ok**
* emulate an I2C sensor (BMP280) and decode the bus traffic

The same test runs against a pod on your desk *or* a remote pod in CI: point
`--benchpod-connection` at an IP, a USB port, or `embeddedci:<device-name>` to
drive a named device through embeddedci.com from a GitHub Action (no secrets — it
authenticates with the workflow's GitHub OIDC token). See
[Running in GitHub Actions](#running-in-github-actions-cloud).

```python
from embeddedci import benchpod

with benchpod.BenchPod("192.168.1.213") as bp:   # or "/dev/ttyACM0", or "usb"
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
# optional: numpy-backed FFT helpers on captures (Capture.fft / dominant_frequency)
pip install "embeddedci[analysis]"
```

The `[cloud]` extra pulls in a WebSocket client used only by the `embeddedci:` destination;
local network/USB use needs nothing extra. The `[analysis]` extra adds numpy for the FFT helpers
— the mean/rms/peak-to-peak reductions are pure-Python and always available.

### OpenOCD (required for flashing)

Flashing shells out to **OpenOCD**, which must be on your `PATH`. The pod runs a **CMSIS-DAP**
processor locally; the library drives it through OpenOCD's `cmsis-dap` adapter using its **TCP
backend** (`cmsis_dap_tcp`), shipping whole DAP transfers rather than per-bit toggles. That backend
**only exists in OpenOCD *master* (post‑0.12.0)**. The stock packages (`apt install openocd`,
`brew install open-ocd`) are **0.12.0** and lack it; flashing with them fails up front with a clear
message.

Install a master snapshot instead — the easiest is **xPack OpenOCD**:

```bash
# macOS / Linux via npm (xpm), or grab a release tarball directly:
npm install -g @xpack-dev-tools/openocd
# or: https://github.com/xpack-dev-tools/openocd-xpack/releases  (extract, add bin/ to PATH)
```

Verify your OpenOCD has the CMSIS-DAP TCP backend:

```bash
openocd -c "adapter driver cmsis-dap" -c "cmsis-dap backend tcp" -c "exit"
# must NOT error on the 'backend tcp' line (if it errors, the build is too old)
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
# or: export BENCHPOD_CONNECTION=usb
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
| `/dev/ttyACM0`, `COM3` | USB console (CDC-ACM), explicit device path |
| `usb` | USB console, auto-detected by probing the ports for a pod |
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
| Connect (network + USB) | ✅ |
| Connect (cloud via GitHub OIDC) | ✅ server + SDK; device firmware tunnel pending on-hardware bring-up |
| Power on/off (+ scheduled delay) | ✅ |
| Flash + assert ok/not ok | ✅ (pure-Python OpenOCD `cmsis-dap` TCP bridge) |
| Emulated I2C sensor (BMP280) | ✅ network + USB (USB via `json` console mode) |
| I2C bus decode (`benchpod.i2c`) | ✅ START/STOP, R/W, ACK, register reads |
| Protocol decode I2C / UART / SPI (`bp.decode`) | ✅ off-device, parity-checked vs the server |
| UART capture (`capture_uart`) | ✅ network + USB |
| ADC scope capture → calibrated volts (`scope_capture`) | ✅ LAN + cloud tunnel |
| Raw 12-ch LA capture (`capture_la`) | ✅ LAN + cloud tunnel |
| Correlated ADC + LA capture (`capture_analog`) | ✅ one hardware trigger, aligned |
| DAC generate / DC / replay (`generate`, `dac_set`, `replay`) | ✅ looping, concurrent-with-capture (gw v18) |
| Cloud waveform library + replay (`bp.waveforms`, `replay_waveform`) | ✅ cloud (OIDC) or an API key on LAN (see below) |
| DAC↔capture co-trigger + auto-stop (`replay(..., on_capture=True)`, `capture_*(stop_dac_after_us=…)`) | ✅ phase-locked to capture t0 (gw v27 / v21) |
| Closed-loop DAC control — panel/MPPT emulator (`control_loop`, `loop_probe`) | ✅ needs the loop gateware image (`fpga_image`) |
| Runtime gateware image swap (`fpga_image`) | ✅ loop ↔ deep-replay, no reflash |
| Signal helpers (`la_step`) | ✅ minimal, TCP transport only |

## Scope, logic analyzer, and DAC replay

Every capture returns data with the scaling already applied — an ADC `scope_capture` gives you
**calibrated volts** (the same affine front-end model the web UI uses), so you assert on real
voltages, not raw counts. It all works over any transport: a LAN/serial pod **or** a cloud device
over the byte tunnel.

```python
from embeddedci import benchpod

with benchpod.BenchPod("embeddedci:my-bench") as bp:
    # ADC scope — calibrated volts + summary helpers
    cap = bp.scope_capture(4096, sample_rate_mhz=1)
    assert 3.2 < cap.mean() < 3.4          # a 3.3 V rail
    print(cap.rms(), cap.peak_to_peak(), cap.dominant_frequency())   # fft needs the [analysis] extra

    # Raw logic analyzer + off-device decode (i2c / uart / spi)
    la = bp.capture_la(8192, sample_rate_mhz=1)
    txns = la.decode("i2c", sda=2, scl=1)
    frames = bp.decode(la, "uart", rx=5, baud=115200)

    # Correlated ADC + LA from one trigger (aligned timebases)
    both = bp.capture_analog(adc_samples=2048, adc_rate_mhz=0.4, la_samples=2048, la_rate_mhz=1)

    # DAC replay — loops until stopped, and (gateware v18) can run *while* you capture
    with bp.replay(cap, dac_path="5v"):            # replay the captured waveform back out
        alongside = bp.capture_la(8192, sample_rate_mhz=1)   # DAC + LA at the same time
```

`bp.replay(...)` accepts a `Capture` (its volts are replayed), a list of volts, or raw DAC codes
(`are_codes=True`); `mapping="faithful"` reproduces the voltage (clipping outside the path range),
`"fit"` auto-scales. Inject a fault with `fault=benchpod.Fault("flatline", start=…, width=…)`.

### DAC ↔ capture co-trigger and auto-stop

For a *deterministic* stimulus→response run, phase-lock the DAC to the capture instead of racing
them from the host. `on_capture=True` arms the DAC to fire on the next capture's hardware t0, and
`stop_dac_after_us=` cuts it at a sample-precise offset from that same t0:

```python
    # armed, not yet driving — the following capture starts it at t0 and cuts it at 2 ms
    with bp.replay(cap, dac_path="5v", on_capture=True) as r:
        assert r.cotrig                                    # device armed the co-trigger (gw v27)
        a = bp.capture_analog(adc_samples=4096, adc_rate_mhz=0.4,
                              la_samples=0, stop_dac_after_us=2000)   # DAC fires at t0, off at 2 ms
```

### In-fabric DAC control loop (any tabulated transfer function)

On the **loop gateware image**, the iCE40 runs a control loop in fabric: each tick it takes an
input, looks it up in a reloadable curve, damps and clamps, then drives the DAC — no host in the
loop. Any transfer function you can tabulate works; a solar-panel I-V table is one preset. Switch
to that image with `fpga_image(0)` (deep-replay is `fpga_image(1)`).

**Start open-loop, with the ADC out of the path.** With `source="fixed"` the loop holds one point
of the curve, so the DAC and output stage can be metered on their own before any of the loop
behaviour is trusted — if 0% doesn't read what the curve says, no amount of loop debugging was
going to help. Needs `capabilities.dac_loop_sources` (gateware v29+):

```python
    if not bp.capabilities.dac_control_loop:
        bp.fpga_image(0)                                   # swap to the loop image (~10 ms, no reflash)

    curve = benchpod.build_panel_curve(voc_code=52000, sharpness=6)   # or build_constant_curve(...)
    with bp.control_loop(curve=curve, vmax=65535, source="fixed", input_code=0) as loop:
        for pct in (0, 50, 100):                           # walk a few points of the curve
            loop.set_input(benchpod.input_percent_to_code(pct))   # no re-arm, no curve re-upload
            time.sleep(0.2)
            want = benchpod.curve_output_at(curve, benchpod.input_percent_to_code(pct))
            print(pct, loop.probe().v, "expected", want)   # ... and read the SMA with a meter
```

`source="sweep"` (with `step=`) walks the whole curve as a function of time, still with no ADC.
Then close the loop — the input becomes the live ADC and the output reacts to the DUT:

```python
    with bp.control_loop(curve=curve, vmax=52000) as loop:   # source defaults to the ADC
        pt = loop.probe()                                    # IVPoint(i=<ADC>, v=<DAC>)
        print(pt.loop_input, pt.v)                           # loop_input == what indexed the curve
```

## Cloud waveform library (load + replay stored recordings)

Save a captured waveform to the cloud library and replay it later — on this pod or another:

```python
with benchpod.BenchPod("embeddedci:my-bench", api_key="eci_…") as bp:
    cap = bp.scope_capture(8192, sample_rate_mhz=0.4)
    wf = bp.save_capture_as_recording(cap, "golden-startup")   # stored raw in S3

    for w in bp.waveforms.list():
        print(w.id, w.name, w.kind)

    handle = bp.replay_waveform(wf.id, dac_path="5v", mapping="faithful")
    ...
    handle.stop()
```

> **Auth note.** The cloud waveform library and server-side replay carry the `benchpod:control`
> capability. Over the cloud destination (`embeddedci:<device>`) the SDK reuses the connection's
> session token — including the **GitHub Actions OIDC** token minted in CI — so no API key is
> needed: a workflow that already drives the device can also load/save library waveforms and run
> server-side replay, scoped to the devices/org the OIDC token is allowed to drive. On a
> **LAN/serial** connection there is no session token, so pass an API key (`api_key=` /
> `--benchpod-api-key` / `BENCHPOD_API_KEY`) to reach the shared library. Capture and direct
> `replay(...)` never need one — they run over the device transport.

In pytest, the `benchpod_waveforms` fixture gives you the library with automatic cleanup of
anything created during the test, and `@pytest.mark.benchpod_capability("dac_deep_replay")` skips a
test when the device doesn't advertise a capability.

## Emulated I2C sensor + UART capture (HIL)

The pod can **pretend to be an I2C sensor** (a BMP280) on two LA channels while
you capture the DUT's UART — so you can flash an app, power-cycle it, and assert
on its boot output with and without the sensor present:

```python
from embeddedci import benchpod

with benchpod.BenchPod("192.168.1.213") as bp:
    bp.flash(file="app.elf", target="target/stm32f4x.cfg",
             swclk=benchpod.PIN11, swdio=benchpod.PIN12, target_power=benchpod.INTERNAL)

    # I2C is open-drain — enable the pod's pull-ups on SDA/SCL. Pull-ups exist
    # only on LA1-8 (LA1/2=4.7k, LA3/4=2.2k, LA5-8=10k), so put SDA/SCL on LA1/2.
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
The `pins` fixture exposes the pod's 12 generic LA channels (`pins.pin_1` …
`pins.pin_12`) plus `pins.efuse` — there are no role-named pins, since any DUT
signal can be on any channel. The example maps its own wiring (which signal is on
which channel) in a small `wiring` fixture at the top. See
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
