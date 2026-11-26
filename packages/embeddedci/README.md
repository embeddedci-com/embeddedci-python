# embeddedci — BenchPod SDK and pytest plugin

A Python SDK and pytest plugin for the [EmbeddedCI](https://embeddedci.com) **BenchPod**, a
hardware-in-the-loop instrument that sits next to your device under test (DUT). From a test you
can:

* connect to a pod over the **network**, **USB** or the **cloud** (`embeddedci:<device-name>`)
* switch and monitor **target power**, and pulse the target's **reset**
* **flash** firmware over SWD and assert it worked
* capture the DUT's **UART**, or hold an interactive UART session
* **emulate sensors** (I2C, SPI, UART and analog) from YAML definitions, including the public
  sensor library (BME280, SHT4x, MPU-6050, NMEA GPS, ...), with stimuli and faults
* capture **ADC** (calibrated volts), a 14-channel **logic analyzer**, or both from one trigger
* drive the **DAC**: DC levels, generated waveforms, arbitrary replay, fault injection, and an
  in-fabric **control loop**
* talk **CAN**, including an autonomous ECU simulator
* check what the DUT's **screen** shows, and press its buttons, with the AI Vision board
  (`embeddedci.aivision`, see [AI Vision board](#ai-vision-board))

The same test runs against a pod on your desk or a remote pod in CI.

```python
from embeddedci.benchpod import BenchPod

with BenchPod("192.168.1.213", la_voltage=3.3) as bp:        # or "usb", or "embeddedci:my-bench"
    bp.flash(file="build/app.elf", target="target/stm32f4x.cfg")
    boot = bp.power_cycle_and_capture(duration=5.0, until="APP_OK")
    assert boot.matched, boot.text
```

No pin numbers: the SWD channels, the UART pair, the baud and the power rail come from the bench's
[wiring profile](#wiring-profile). Pass them explicitly when you want to override it.

**Pod firmware 4.0 or later.** embeddedci 4.x drives pods with firmware 4.0.0 and later (client
API 2). Connecting to a pod with firmware 3.x raises `FirmwareTooOldError`, which says to update
the pod with the BenchPod CLI (`benchpod flash-self`) or the firmware update in the web app; to
keep firmware 3.x, stay on embeddedci 3.x (`pip install 'embeddedci<4'`).

**Contents:** [Install](#install) · [Quick start](#quick-start) ·
[API conventions](#api-conventions-and-stability) · [pytest plugin](#pytest-plugin) · [Test runs](#test-run-recording) ·
[Power and reset](#power-reset-and-power-monitoring) · [Flashing](#flashing) · [UART](#uart) ·
[Pull resistors](#la-pull-resistors) · [Emulated sensors](#emulated-sensors) ·
[Analog paths](#analog-paths-dc-output-and-single-readings) · [Captures](#captures) ·
[DAC](#dac-generator-replay-and-faults) · [Control loop](#in-fabric-dac-control-loop) ·
[CAN](#can) · [AI Vision](#ai-vision-board) · [Cloud](#cloud-embeddedcidevice-name) · [Build reporting](#build-reporting) ·
[Errors](#errors) · [Escape hatches](#escape-hatches) · [API reference](#api-reference) ·
[Releasing](#releasing-maintainers)

## Install

Requires **Python 3.10+**.

```bash
pip install embeddedci
pip install "embeddedci[cloud,analysis]"     # with extras
```

On Python 3.10 or newer this gives you the current 2.x release. On **Python 3.9 it cannot** — pip
skips 2.x and installs the 0.2.4 placeholder instead, which fails on import with a message telling
you to upgrade. (Before that placeholder existed, 3.9 quietly resolved to 0.2.3: a completely
different, pre-2.0 API, installed without any warning.)

Pinning the major version is still the most explicit form, and turns a wrong Python into a resolver
error rather than an import error:

```bash
pip install "embeddedci>=2,<3"
```

`python -c "import embeddedci; print(embeddedci.__version__)"` tells you what you actually got.

| Extra | Adds | Needed for |
|---|---|---|
| `cloud` | `websocket-client` | the `embeddedci:<device-name>` destination |
| `discovery` | `zeroconf` | `discover` connection string / `--benchpod-discover` (mDNS) |
| `analysis` | `numpy` | `Capture.fft()` and `Capture.dominant_frequency()` |
| `pytest` | `pytest` | nothing extra — the plugin registers itself; this just installs pytest |
| `dev` | `pytest`, `websocket-client`, `numpy` | working on this package |

Network and USB connections need nothing beyond the base install (`pyserial`). Capture reductions
such as mean, RMS and peak-to-peak are pure Python; only the FFT uses numpy.

### OpenOCD (required for flashing)

Flashing shells out to **OpenOCD**, which must be on your `PATH` (or passed as `openocd_bin=`).
The pod runs a **CMSIS-DAP** probe locally; the library drives it through OpenOCD's `cmsis-dap`
adapter using its **TCP backend** (`cmsis_dap_tcp`), shipping whole DAP transfers rather than
per-bit toggles. That backend is **newer than OpenOCD 0.12.0**. The stock packages
(`apt install openocd`, `brew install open-ocd`) are 0.12.0 and lack it; flashing with them fails
up front with a clear message.

Install a recent build instead — the easiest is **xPack OpenOCD**:

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

## Quick start

Most people use this through **pytest** — that is what the plugin is for. Two files and you have a
hardware test that runs on your desk and in CI:

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
def benchpod_la_voltage():
    return 3.3                               # your DUT's I/O voltage, once for the whole suite
```

```python
# test_bench.py
import pytest


def test_pod_is_alive(benchpod):
    assert benchpod.ping()                   # no wiring, no firmware: the honest first green tick


@pytest.mark.hardware
def test_target_flashes_and_boots(benchpod_target, firmware):
    assert benchpod_target.flash(file=firmware, target="target/stm32f4x.cfg").ok
    boot = benchpod_target.power_cycle_and_capture(duration=5.0, until="APP_OK")
    assert boot.matched, boot.text


@pytest.mark.hardware
def test_boot_stays_inside_its_power_budget(benchpod_target):
    profile = benchpod_target.measure_power(3.0)
    assert profile.peak_current < 0.25       # amps, measured on the rail
    assert not profile.fault                 # the eFuse never tripped
```

```bash
pytest --benchpod-connection=192.168.1.213 --benchpod-firmware=build/app.elf
```

Without a connection the hardware fixtures **skip** rather than fail, so the suite stays green on a
runner with no pod. See [pytest plugin](#pytest-plugin) for every fixture, option and marker.

Driving a pod directly, without pytest:

```python
from embeddedci.benchpod import INTERNAL, BenchPod

with BenchPod("192.168.1.213", la_voltage=3.3) as bp:
    bp.ping()
    print(bp.status())                       # firmware version, board, network, features
    print(bp.capabilities.board, bp.capabilities.dac_deep_replay)
    bp.power_on(INTERNAL)
```

`BenchPod` is a context manager; `close()` is idempotent.

**The I/O voltage comes from the wiring profile.** The pod refuses every LA-bank operation
(flashing, UART, LA capture, pull resistors, sensor emulation) until its LA I/O-bank voltage is
selected, and it forgets it on every reboot. The profile's `la_mv` (`3300` or `1800`, the board's
I/O voltage) fills it in: on connect the SDK sets it from the pod's profile when the pod has none,
and firmware 3.10.10+ takes it from the active profile itself on the first LA operation. So a
profile that sets its I/O voltage needs nothing else. `la_voltage` (1.8 or 3.3) still sets it right
after connecting for this session, and `bp.set_la_voltage(3.3)` at any time; `bp.get_la_voltage()`
returns a `LaVoltage` whose `voltage` is `None` until one is chosen. 1.8 V needs a rev3 pod. With
no voltage anywhere, a refused command raises `FirmwareError` with code `BP0001`: `The I/O voltage
is not set; set the board's I/O voltage first: BenchPod(conn, la_voltage=3.3) ...` (prefixed with
the command, as every `FirmwareError` is).

**5 V I/O (digital board v4).** On a pod with the digital board v4 (`bp.supports("la_5v")`,
`bp.capabilities.vref_out`), `bp.set_la_voltage(5.0)` (or `la_mv: 5000` in the profile) keeps the
bank at 3.3 V and switches J1 pin 21, the pull-up rail, to 5 V; outputs reach 5 V through their
pull-up (`bp.set_pull(la, "up")`). What that means for the wiring is on the
[5 V page](https://embeddedci.com/docs/benchpod-targets/5v-rs485#5v-io). `FirmwareError` `BP0028`: 5 V did not come up. Other pods raise
`ValueError` before anything is sent. On v4, `LaVoltage.pin21` reports pin 21 (`None` elsewhere):

```python
st = bp.get_la_voltage()
if st.pin21 and st.pin21.state == "high":
    # a DUT signal above the I/O voltage lifts pin 21 through the pulls: select 5 V or remove it
    print(f"pin 21 at {st.pin21.measured} V, set to {st.pin21.setting} V")
```

`Pin21` has `source` (`"bank"` or `"5v"`), `setting` and `measured` (volts), `state` (`"ok"`,
`"high"`, `"low"`, `"short"` or `"unknown"`), `up` (high enough for the pulls) and `ok`.

Other constructor arguments: `timeout` (seconds, default 30), `api_key`, `api_base`,
`cloud_token`, `cloud_audience`, `cloud_user_token`, `lease`, `lease_wait`, `lease_ttl` (see [Cloud](#cloud-embeddedcidevice-name)),
`wiring` and `wiring_source` (see [Wiring profile](#wiring-profile)) and `transport` (inject a
custom backend).

### Which pod `BenchPod()` connects to

The CLI, the SDK and the MCP server share one saved connection. `BenchPod()` picks, first match
wins:

1. the `connection` argument;
2. the `BENCHPOD_CONNECTION` environment variable;
3. the default connection `benchpod-cli` saved with `benchpod set-connection <address|device|usb>`
   (or `benchpod discover --save`), read from `~/.config/benchpod-cli/config.json`
   (`$XDG_CONFIG_HOME/benchpod-cli/config.json` when that is set).

With none of them it raises `ConnectionConfigError`. The SDK only reads that file, and a missing or
malformed file counts as "nothing saved". The connection string `saved` names the saved connection
explicitly. The pytest plugin does **not** fall back to it (see [pytest plugin](#pytest-plugin)).

### Connection strings

| Form | Transport |
|---|---|
| `192.168.1.213`, `benchpod-baea06.local`, `[fe80::1]` | network: JSON over TLS on port 8443 (see [TLS on the LAN](#tls-on-the-lan)); an address on the pod's USB network adapter (`10.237.x.y`) is plaintext 8080 |
| `host:8080`, `host:8443` | network on that port: `:8080` is plaintext (refused on Ethernet and Wi-Fi unless the pod's HTTPS-only is off), `:8443` TLS |
| `/dev/ttyACM0`, `COM3`, `\\.\COM10` | USB console, explicit device path |
| `benchpod-baea06`, `baea06` | the pod with that name (as on its label and in `discover`): its address found on the LAN over mDNS, else `usb:benchpod-baea06`. Without `[discovery]` it is the host name `benchpod-baea06.local` |
| `usb` | USB pod, auto-detected by probing the serial ports: its USB network adapter (full API), else its USB console (status only) |
| `usb:<name>` | the USB pod with that name when several are plugged into one computer: `usb:baea06`, `usb:benchpod-baea06` (any case, optional `.local`); otherwise as `usb` |
| `discover` (or `mdns`, `auto`) | find exactly one pod on the LAN via mDNS (needs `[discovery]`); errors on zero or several |
| `embeddedci:<device-name>` | a named device through embeddedci.com (needs `[cloud]`; the `benchpod login` session or an API key anywhere, or GitHub Actions OIDC) |
| `saved` | the default connection `benchpod-cli` saved (`benchpod set-connection`); errors when none is saved |

**USB network (firmware 3.9.0 and later).** The STM32 pod's USB port is a composite device
(its own USB id `1209:EC10` from the firmware after 4.0.4, ST's `0483:5740` before; pre-release 3.9.0 builds used `1209:0001`; the SDK finds the pod under any of them): the console plus a USB network adapter
(CDC-NCM) that carries the same JSON API as the LAN. The link is a point-to-point `/30` in
`10.237.0.0/16` (the pod is the first address, your computer the second, handed out by the pod's
DHCP server). With connection `usb` the SDK first looks at this computer's own interfaces (Linux
and macOS): one address in `10.237.0.0/16` with a `/30` mask names the pod's address without
opening the console, so it works for a Linux user outside the `dialout` group (with several such
links, `usb:<name>` picks the pod by its identity). Otherwise it finds the pod's console and reads
the pod's address on that link from its `status`. When the link is up and port 8080 answers within
1.5 s it talks to `<pod address>:8080` over TCP: everything works as on the LAN. The transport is
then a `TcpTransport` with `usb_net` set, and `usb_console` naming the console's device path when
the console was used (`bp.transport.usb_console`). The SDK falls back to the console, as on older firmware, when the
pod's firmware is older than 3.9.0, when Wi-Fi is configured on the pod (that turns USB networking
off), when the link is down, or when the computer has not configured the adapter (for example
macOS under a locked screen); it logs which one it picked. An explicit device path such as
`/dev/ttyACM0` is always the console.

The USB network adapter works with the in-box drivers of macOS, Linux and Windows 11 (tested).
Windows 10 (1903 or newer) is supported but not tested: if the adapter shows up without a driver,
update its driver in Device Manager and pick "UsbNcm Host Device".

**Turning the adapter off.** The pod keeps a USB network setting, on by default and separate from
its external LAN policy. `bp.usb_network()` reads it (`True` = on) over any connection;
`bp.usb_network(False)` turns the adapter off, which only works over the USB link itself: the pod
replies, then re-enumerates as its USB console only, which ends the connection. Over Ethernet,
Wi-Fi or the cloud the pod refuses the change (`PodLockedError`). Turning it back on needs the USB
console: `benchpod usb-network on`. Status reports it as `usb.enabled` (a boolean, as `usb.net` is) and, when the adapter is
off, `usb.off_reason` (`"setting"` or `"wifi"`).

**When the USB network is off.** `usb` is the full API over the USB network adapter when it is
up. When USB networking is off (Wi-Fi configured) or down, it is the full API over the pod's
Ethernet or Wi-Fi address from its console status, when this computer reaches it (logged as a
warning; the pod's external LAN policy applies there; `transport.usb_lan_fallback` says why);
else the console, which answers `status` and `ping` only.

**Several pods on USB.** Auto-detect reads each pod's name (its identity short id, `baea06` for
`benchpod-baea06`) from the console's `identity` command. Connection `usb:<name>` opens the pod
with that name and raises `ConnectionConfigError` listing the pods it found when none matches.
Plain `usb` with two pods plugged in raises `ConnectionConfigError` naming both, for example
`2 pods on USB (benchpod-aaaaaa on /dev/cu.usbmodemA, benchpod-bbbbbb on /dev/cu.usbmodemB)`;
with one pod it works as before. Two pods on one computer have not been tested on hardware yet. macOS lists each pod twice (`/dev/cu.*` and `/dev/tty.*`); the
same name on two ports counts as one pod. `SerialTransport.identity()` reads the name of the pod
on an open console.

**Otherwise use the network (or the cloud) for testing.** The STM32 pod's USB console is a text
shell for setup and diagnostics with no JSON mode, so over the console the SDK can only read
`status()` and `ping()`. Everything else (the LA voltage, target power, flashing, UART, analog,
captures, sensor emulation, CAN) raises a `TransportError` right away that points at the network
connection or `usb` (the USB network adapter). Over the console the firmware version decides the
client API check: firmware 3.x raises `FirmwareTooOldError` there too.

| | TCP | USB network (`usb`) | USB console (STM32 pod) | Cloud |
|---|---|---|---|---|
| `status`, `ping` | yes | yes | yes | yes |
| LA voltage, target power, flashing, UART, analog, captures, sensor emulation, CAN, control loop | yes | yes | no | yes |
| `replay()` / client-side `replay_waveform()` (streams the waveform to the pod) | yes | yes | no | yes |
| Device lease | the pod's lease (feature `lease`) | the pod's lease (feature `lease`) | no | the server's lease |

### TLS on the LAN

Firmware 4.0 serves the JSON API on Ethernet and Wi-Fi over TLS only, on port 8443 (HTTPS-only is
on by default; plaintext 8080 stays on the USB network adapter). A network address without a port
(`192.168.1.5`, a pod name, an mDNS discovery) goes to 8443 over TLS; there is no fallback to
plaintext. Plaintext 8080 is used only when you name the port (`host:8080`, for a pod whose
HTTPS-only you turned off) and on the USB network adapter (`usb`, or a `10.237.x.y` address: a
physical link). Any other explicit port is plaintext too. `BENCHPOD_TLS=on|off` overrides (`auto`
is the default). `bp.transport.last_tls` says which one the last connection used. mDNS discovery
reads the pod's TLS port and client API level from its TXT record (`tls`, `api`) and refuses a pod
with firmware 3.x (`FirmwareTooOldError`).

The pod's certificate is its own, so the SDK pins its SHA-256 the first time (trust on first use)
in `known_pods.json` next to `benchpod-cli`'s config; the CLI reads and writes the same file. A
different certificate later raises `TlsPinMismatchError` (`addr`, `pinned`, `got`). After a
deliberate change elsewhere, `trust_lan_tls(host, sha256=None)` pins the certificate the pod
presents now (`sha256` only accepts that one) and `forget_lan_tls(host)` drops the pin. With
`BENCHPOD_TLS_CA=ca.pem` the certificate and host name are verified against that CA instead (for a
certificate you installed, signed by your own CA).

```python
bp = BenchPod("192.168.1.220")
info = bp.lan_tls()            # LanTls: port, https_only, sessions, subject, san, sha256, ...
bp.set_lan_tls_https_only(True)   # plaintext 8080 off on Ethernet and Wi-Fi (USB keeps it)
bp.set_lan_tls_cert(open("pod.pem").read(), open("pod.key").read())
bp.regenerate_lan_tls()
```

`set_lan_tls_https_only(False)`, `regenerate_lan_tls()` and `set_lan_tls_cert()` need a TLS, USB or
cloud connection; over plaintext Ethernet or Wi-Fi the pod refuses (`PodLockedError`).
`set_lan_tls_cert` needs a network connection (it uploads the bundle); after it and after
`regenerate_lan_tls()` the SDK pins the new certificate for the address it used.

### LAN pairing

From firmware 4.0.1 the pod answers only status to a computer on Ethernet or Wi-Fi that is not
paired with it: anything else raises `PodNotPairedError` (`BP0081`, a `PodLockedError`). Pair this
computer once over the pod's USB cable, or through the cloud as an organization owner or admin:

```python
BenchPod("usb").pair()                 # the USB network adapter, or the console when it is off
BenchPod("embeddedci:bench-01").pair()
```

`benchpod setup` does the same. The SDK presents this computer's TLS client key on every
connection to port 8443: `client.key` and `client.crt` next to `known_pods.json`, made on first use
and shared with `benchpod` and the MCP server. A CI runner passes its own with
`BENCHPOD_CLIENT_KEY_PEM` (or `BENCHPOD_CLIENT_CERT` and `BENCHPOD_CLIENT_KEY`). Over USB,
`pair()` also pins the pod's LAN certificate for its LAN address.

`bp.lan_clients()` lists the paired computers (`LanClient.this` marks this one);
`lan_clients(add=..., spki_sha256=...)`, `remove=...` (a name, or 8+ hex digits of a key) and
`clear=True` change the list over USB or the cloud only. `set_lan_pairing(False)` turns pairing off.

### Console log level

How much the pod prints on its USB console (firmware newer than 3.10.12, feature `log_level`).
`info` is the default; `debug` adds the per-command trace (`[cmd] <- ...`, `[cmd] -> ok ...`) and
the retry detail of the cloud, Wi-Fi and DHCP loops, `warn` and `error` print less. Errors always
print. The pod stores the level, so it survives a reboot and a firmware update; any connection may
read or set it.

```python
bp.log_level()              # "info"
bp.set_log_level("debug")   # one of LOG_LEVELS: "error", "warn", "info", "debug"
```

Another name raises `ValueError` without asking the pod; older firmware raises
`UnsupportedFeatureError` (it has no levels and prints everything).

### MAC addresses (managed networks)

A lab network that hands out addresses only to registered devices needs the pod's MACs first.
`mac_addresses()` returns them as `MacAddresses`: `eth_mac`, `wifi_mac` (the MAC the pod uses on
Wi-Fi), `has_wifi` and `source`. With firmware after 3.10.7 they come from the pod's `status`
(`eth_mac`, `wifi_mac`; LAN and USB), else from embeddedci.com's record of the pod (`source`
`"server"`; recorded when the pod shipped, refreshed on every connect), which is how a cloud
connection gets them. `wifi_mac` is None until the pod scanned for Wi-Fi or joined a network.

```python
macs = bp.mac_addresses()      # MacAddresses(eth_mac='02:35:c7:76:b7:9b', wifi_mac=..., source='pod')
```

### Environment variables

| Variable | Used when | Purpose |
|---|---|---|
| `BENCHPOD_CONNECTION` | no `connection` argument / `--benchpod-connection` | connection string (`BenchPod()` then falls back to the CLI's saved connection; pytest does not) |
| `BENCHPOD_LA_VOLTAGE` | no `la_voltage` argument / `--benchpod-la-voltage` | LA bank voltage to select on connect (`1.8` or `3.3`) |
| `BENCHPOD_API_KEY` | no `api_key` argument / `--benchpod-api-key` | EmbeddedCI API key (`eci_…`) for the cloud destination and server features |
| `BENCHPOD_API_BASE` | no `api_base` argument / `--benchpod-api-base` | server base URL (default `https://www.embeddedci.com`) |
| `BENCHPOD_BUILD_TARGET` | no `--benchpod-build-target` | platform id recorded by the `build_report` fixture |
| `BENCHPOD_WIRING` | no `wiring` argument / `--benchpod-wiring` / `benchpod_wiring` fixture | wiring profile file (`.json`/`.toml`), see [Wiring profile](#wiring-profile) |
| `BENCHPOD_WIRING_PROFILE` | no `wiring_profile` argument / `--benchpod-wiring-profile` | the name of a wiring profile stored on the pod to use (default: the pod's default profile), see [Wiring profile](#wiring-profile) |
| `BENCHPOD_WIRING_SOURCE` | no `wiring_source` argument | `local` never asks embeddedci.com for the pod's stored wiring profile; `auto` (default) does, see [Wiring profile](#wiring-profile) |
| `BENCHPOD_LIFT_DAC_LIMITS` | pytest sessions | `1` clears the pod's DAC output limits for the session and restores the same limits at the end, even when tests fail (only with the output stage switched off) |
| `BENCHPOD_TLS` | network connections | `auto` (default), `on` or `off`: TLS to the pod's LAN API on 8443, see [TLS on the LAN](#tls-on-the-lan) |
| `BENCHPOD_TLS_CA` | network connections over TLS | PEM file of CA certificates: verify the pod's certificate and host name instead of pinning it |
| `BENCHPOD_CLIENT_KEY_PEM` | network connections over TLS | This computer's TLS client certificate and key, PEM, for [LAN pairing](#lan-pairing) (a CI secret; `benchpod lan-clients add --new-key` prints one) instead of `client.key`/`client.crt` |
| `BENCHPOD_CLIENT_CERT`, `BENCHPOD_CLIENT_KEY` | network connections over TLS | The same as two files |
| `BENCHPOD_STALL_TIMEOUT` | flashing | seconds with no SWD traffic before a flash attempt is aborted and retried (default 60) |
| `BENCHPOD_SENSOR_LIBRARY` | no `version` argument / `--benchpod-sensor-library` | sensor library version: `latest` or a release such as `0.2.0` (default: the version the SDK pins), see [Emulated sensors](#emulated-sensors) |
| `BENCHPOD_LEASE` | connecting | a lease id to join instead of taking a new lease (set by `benchpod ci` for its child processes), see [Device leases](#device-leases) |
| `BENCHPOD_SENSOR_CACHE` | sensor library downloads | cache folder for downloaded library versions (default `<user cache>/embeddedci/benchpod-sensors`) |

## Error codes

When the pod refuses a request it can explain, it sends a stable code and its values, and no text
(firmware 4.0). The SDK puts them on the exception: `FirmwareError.code` (`"BP0009"`), `.values`
(`{"name": "k66", "rev": 2}`) and `.docs_url`, and renders the message from the error catalog it
ships (benchpod-firmware's `errors/errors.json`): what went wrong and how to fix it from Python:

```text
wiring_put: BP0009: The wiring profile k66 changed since it was loaded (it is now at revision 2);
load it again (bp.load_wiring("k66")), redo your change and save, or save under another name
(https://embeddedci.com/docs/errors#BP0009)
```

A code never changes meaning and is never reused, so match on `exc.code` (or the exception
class) in tests and scripts, not on the text: the text may change between releases. The SDK itself
never reads a refusal's text. `embeddedci.benchpod.errors.explain("BP0009")` prints a code's entry
offline. [embeddedci.com/docs/errors](https://embeddedci.com/docs/errors) lists every code.

The code also picks the exception class, and the code's values fill its fields:

| Codes | Class | Fields |
|---|---|---|
| `BP0002` unknown command, `BP0017` gateware too old, `BP0046` no analog front end, `BP0056` no module link, `BP0058`, `BP0059` not in the running image, `BP0060` unknown update target, `BP0064` sweep with an input map, `BP0076` to `BP0078` USB host | `UnsupportedFeatureError` | `feature` (the command, the feature, `"analog"`, `"module"`, ...) |
| `BP0080` the board and module flat-cable pinouts differ (the DAC is off) | `DacPinoutMismatchError` (an `UnsupportedFeatureError`) | `digital`, `module` |
| `BP0003`, `BP0004`, `BP0049`, `BP0050` an I2C, SPI, UART or SWD session is open, `BP0005` the bus engine is busy, `BP0016`, `BP0048` storage busy, `BP0047` another long command, `BP0051` an update, `BP0052` the previous cloud command, `BP0066` a step train | `PodBusyError` | |
| `BP0007` to `BP0015` wiring profiles | `WiringError` | `name` |
| `BP0023`, `BP0024` pull resistors | `PullDirectionError` | `la`, `want`, `have` |
| `BP0025` lease held by someone else | `PodLeasedError` | `holder`, `purpose`, `left_s` |
| `BP0026` lease stopped | `PodLeaseLostError` | `by` |
| `BP0030` client too old | `ClientTooOldError` | `client_api`, `min_client_api`, `api` |
| `BP0031` pin in use | `PinConflictError` | `la`, `function`, `owner`, `instance` |
| `BP0032`, `BP0033` pull-down conflicts | `PullConflictError` | `la` |
| `BP0034`, `BP0035` DUT output conflicts | `DirectionConflictError` | `la` |
| `BP0038`, `BP0039` trigger timeouts | `TriggerTimeout` | `la`, `edge` (`"start"` for `BP0039`) |
| `BP0040` to `BP0044` LAN lock and settings the LAN may not change, `BP0075` plaintext 8080 off | `PodLockedError` | |
| `BP0045` owner or admin only | `PermissionDeniedError` | |
| `BP0053`, `BP0054` reply too large | `ReplyTooLargeError` | |
| every other code (`BP0001` I/O voltage not set, `BP0006` missing argument, UART options, capture limits, ...) | `FirmwareError` | |

A refusal without a code (an argument the pod checks itself, such as `"missing steps"`) keeps the
pod's text as its message and is a plain `FirmwareError` (`.code == ""`). A code newer than this
SDK is kept on `.code`, with a message that says to update the SDK.

## API conventions and stability

2.0.0 froze the API. The conventions hold everywhere:

* **Units are volts, seconds and hertz** — `sample_rate_hz`, `delay`, `duration`, `stop_dac_after`,
  `pulse`; readings come back in volts and amps. (Raw DAC/ADC *codes* are named as such.)
* **Invalid arguments raise `ValueError`**. A `BenchPodError` subclass always means a device,
  transport or server failure — see [Errors](#errors).
* **Device state is typed.** Methods return frozen dataclasses (`LaVoltage`, `TargetStatus`,
  `PowerStatus`, `PullState`, `DacOutput`, `CurrentOutput`, `AdcReading`, …) from `embeddedci.benchpod.state`; each
  keeps the untouched firmware reply in `.raw`.
* **String options are `Literal` types** and are validated: `DacPath`, `DacOutputPath`,
  `AnalogPath`, `AdcSource`, `LoopSource`, `Waveshape`, `ReplayMapping`, `DecodeProtocol`,
  `CanMode`, `FaultType`, `GpioMode`, `Edge`, `TriggerEdge`.
* **Named constants instead of magic numbers**:

  | Concept | Constants | Wire value |
  |---|---|---|
  | Target-power eFuse | `INTERNAL`, `EXTERNAL` (`Efuse`) | 1, 2 |
  | LA channels | `PIN1` … `PIN14` (`Pin`) | 1 … 14 |
  | BMP280 addresses | `BMP280_ADDR_PRIMARY`, `BMP280_ADDR_SECONDARY` | 0x76, 0x77 |
  | Gateware image | `FpgaImage.LOOP`, `FpgaImage.DEEP_REPLAY`, `FpgaImage.EMULATION` | 0, 1, 2 |

  Plain ints work too (`efuse=1`, `swclk=11`); they are validated. The target's reset line is not an
  LA channel — it has a dedicated pin (see [Flashing](#flashing)).

**Feature checks.** Pods differ by board, firmware and gateware. `bp.supports("spi_master")` says
whether the pod has any `Capabilities` flag (`False` for a flag this SDK does not know), and the
properties `can_gpio`, `can_trigger`, `can_spi`, `can_profile_power`, `can_calibrate`,
`can_current_out`, `can_reset_target` and `can_analog` name the common ones (`can_config` …
`can_disable` stay the CAN bus commands). A call the pod cannot serve raises
`UnsupportedFeatureError` (see [Errors](#errors)), before anything is sent when the capabilities
already say so.

Where those come from: the pod's `status` (and its cloud capabilities frame) carries
`capabilities.features_rev`, the revision of the features table its firmware is built from, plus
the running image (`capabilities.fpga_image`), the gateware and a few hardware facts. The SDK
ships that table (`embeddedci.benchpod.feature_map`, a copy of benchpod-firmware's
`features/features.json`), so this needs no network: `capabilities.features` is every feature the
table gives the pod, and `bp.supports()` also takes table names without a flag of their own
(`"la_dir"`, `"i2c_master"`, `"diag"`, `"wiring_store"`, ...). A pod with a higher `features_rev`
than the SDK knows logs a warning once: update the SDK (`pip install -U embeddedci`) to use those
features.

**Client API level.** A new feature is additive, so an older SDK just misses it. The other kind of
change, one an older SDK would misread (a reply that changes shape, a field that changes meaning,
a refusal text replaced), raises the firmware's client API level. The SDK is built for
`embeddedci.benchpod.feature_map.CLIENT_API` (2) and sends it with every JSON command as
`"client_api"`, with `"client": "sdk <version>"` (the MCP server sends `"mcp <version>"`). The pod
announces its level (`capabilities.api`, with `capabilities.min_client_api`;
`capabilities.client_api_level` counts a pod that does not say as 1).

- A pod below `feature_map.MIN_POD_API` (2, firmware 3.x) raises `FirmwareTooOldError` on connect:
  update the pod (`benchpod flash-self`, or the firmware update in the web app), or use
  embeddedci 3.x with it.
- A pod above the SDK's level, or one whose `min_client_api` is above it, raises
  `ClientTooOldError`, which says the pod's firmware version and both levels; the pod raises the
  same error (`BP0030`) for a client below its `min_client_api`. The fix is
  `pip install -U embeddedci`.

`status`, `diag`, `ping` and `lease_release` are never refused, so a mismatched client can still
see what the pod runs.


```python
import pytest


def test_flash_spi_nor(benchpod):
    if not benchpod.can_spi:
        pytest.skip("this pod has no SPI master")
    benchpod.spi_flash("fw.bin")
```

**Stability promise.** Everything in `embeddedci.benchpod.__all__` follows semantic versioning:
within 2.x names and signatures only grow. `BenchPod.command()`, `BenchPod.transport` and
`BenchPod.lowlevel` are escape hatches **outside** the promise. Migrating from 0.x/1.x? The
[CHANGELOG](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/CHANGELOG.md)
has a complete old → new table.

## pytest plugin

Installing the package registers a pytest plugin (options, fixtures and markers). The board's I/O
voltage normally comes from the pod's wiring profile (`la_mv`); when the profile sets none, or a
suite must not depend on it, set it **once** for the whole suite in `conftest.py`. The pod refuses
flashing, UART, LA capture, pull resistors and sensor emulation until a voltage is selected:

```python
# conftest.py
import pytest


@pytest.fixture(scope="session")
def benchpod_la_voltage():
    return 3.3  # the board's I/O voltage — change to 1.8 for a 1V8 board
```

Write the bench's wiring down once too, so tests use names instead of channel numbers (see
[Wiring profile](#wiring-profile)). A cloud device already has one — edited in the web UI — and
`benchpod.wiring` loads it; for a LAN pod override the fixture (or pass `--benchpod-wiring=wiring.json`):

```python
# conftest.py
from embeddedci.benchpod import Signal, Wiring


@pytest.fixture(scope="session")
def benchpod_wiring():
    return Wiring(uart_rx=3, uart_tx=4, swd_target="target/stm32f4x.cfg",
                  signals=[Signal("TRIGGER", 9, "output"), Signal("READY", 10)])
```

Then point pytest at a pod and use the fixtures:

```bash
pytest --benchpod-connection=192.168.1.213
# or: export BENCHPOD_CONNECTION=usb
```

```python
import pytest
from embeddedci.benchpod import INTERNAL


@pytest.mark.hardware
def test_firmware_boots(benchpod, firmware):
    benchpod.flash(file=firmware, target_power=INTERNAL)     # SWD pins + target from the wiring profile
    boot = benchpod.power_cycle_and_capture(delay=1.0, duration=5.0, until="APP_OK")  # UART from it too
    assert boot.matched, boot.text


@pytest.mark.hardware
def test_result_pin_follows_the_trigger(benchpod):
    trigger, ready = benchpod.signal("TRIGGER"), benchpod.signal("READY")
    trigger.configure()                                      # a GPIO output, starting inactive
    trigger.pulse(0.005)
    assert ready.wait_for(1, timeout=2.0)


def test_rail_is_healthy(benchpod_target, pins):   # target powered for this test only
    assert not benchpod_target.target_status().efuse(pins.efuse).fault
```

The `benchpod` fixture is a `BenchPod` instance, not the module — import constants such as
`INTERNAL` and `PIN1` from `embeddedci.benchpod`. Without a connection configured the fixtures
**skip** rather than fail, so the suite stays green on runners without hardware.

### Options

| Option | Env fallback | Default | Purpose |
|---|---|---|---|
| `--benchpod-connection` | `BENCHPOD_CONNECTION` | — | connection string (also the `benchpod_connection` ini option) |
| `--benchpod-la-voltage` | `BENCHPOD_LA_VOLTAGE` | — | override the `benchpod_la_voltage` fixture for one run (1.8 or 3.3); the flag wins over the fixture, the env var only applies when neither is set |
| `--benchpod-wiring` | `BENCHPOD_WIRING` | — | wiring profile file (`.json`/`.toml`); the flag wins over the `benchpod_wiring` fixture, the env var only applies when neither is set |
| `--benchpod-wiring-profile` | `BENCHPOD_WIRING_PROFILE` | the pod's default | name of a wiring profile stored on the pod to run with; not with `--benchpod-wiring` |
| `--benchpod-efuse` | — | the profile's rail | target-power rail for `benchpod_target` and `pins.efuse` (1 internal, 2 external); `benchpod_target` otherwise follows the wiring profile, `pins.efuse` is 1 |
| `--benchpod-firmware` | — | — | firmware image for the `firmware` fixture |
| `--benchpod-discover` | — | off | when no connection is configured, find one pod via mDNS (needs `[discovery]`) |
| `--benchpod-api-key` | `BENCHPOD_API_KEY` | — | API key for the cloud destination and the waveform library; it, `--benchpod-api-base` and the lease options also reach the short connection that lifts the DAC limits (`BENCHPOD_LIFT_DAC_LIMITS=1`) |
| `--benchpod-api-base` | `BENCHPOD_API_BASE` | `https://www.embeddedci.com` | embeddedci server |
| `--benchpod-lease-wait` | — | `600` | seconds to wait for a busy pod (its lease, or the cloud device's) |
| `--benchpod-no-lease` | — | off | don't lock the pod (only when nothing else can use it) |
| `--benchpod-build-target` | `BENCHPOD_BUILD_TARGET` | — | platform id recorded by `build_report` (e.g. `stm32f4`) |
| `--benchpod-sensor-library` | `BENCHPOD_SENSOR_LIBRARY` | the pinned version | sensor library version for `benchpod_sensors` (`latest` or a release) |
| `--embeddedci-run-dir` | `EMBEDDEDCI_RUN_DIR` | off | record the run into this folder (`auto`: `./embeddedci-runs/<started UTC>-<suite>/`), see [Test run recording](#test-run-recording) |
| `--embeddedci-suite` | — | `pytest` | suite name recorded in `run.json` |
| `--embeddedci-dut` | — | `""` | name of the board under test recorded in `run.json` |
| `--embeddedci-keep-traces` | `EMBEDDEDCI_KEEP_TRACES` | `all` | which pod-call traces to keep: `all`, `failed` or `none` |
| `--embeddedci-upload` | `EMBEDDEDCI_UPLOAD` | off | upload the recorded run to embeddedci.com at the end (also the `embeddedci_upload` ini option), see [Uploading a run](#uploading-a-run) |

Connection resolution order: `--benchpod-connection` → `benchpod_connection` ini option →
`BENCHPOD_CONNECTION` → `--benchpod-discover`.

The plugin never falls back to the connection `benchpod-cli` saved on this machine, unlike
`BenchPod()`: a suite must not start driving hardware on a developer's machine just because they
once ran `benchpod set-connection`. Opt in with `--benchpod-connection=saved` (or `saved` in the ini
option or `BENCHPOD_CONNECTION`); with nothing saved the hardware tests skip and say why.

```ini
# pytest.ini
[pytest]
benchpod_connection = 192.168.1.213
```

### Fixtures

| Fixture | Scope | Provides |
|---|---|---|
| `benchpod_la_voltage` | session | the board's I/O voltage selected on connect — **override it in `conftest.py`** (default `None` → `BENCHPOD_LA_VOLTAGE`) |
| `benchpod_wiring` | session | the bench's wiring profile — **override it in `conftest.py`** (a `Wiring`, dict or file path; default `None` → `BENCHPOD_WIRING`, a profile stored on the pod, nothing wired; see [Wiring profile](#wiring-profile)). An explicit profile also supplies the LA voltage when nothing else sets one |
| `benchpod` | session | a connected `BenchPod` with the options above applied; LA channels left in GPIO mode are released when the session starts and ends; closed at session end |
| `benchpod_connection` | session | the resolved connection string (skips when none) |
| `benchpod_target` | function | `benchpod` with the target rail (`--benchpod-efuse`, else the wiring profile's) powered on for the test, off at teardown |
| `benchpod_sensors` | function | `benchpod.sensors` (`SensorEmulation`); stops the stimulus and every emulated sensor at teardown; skips on firmware without the `sensors` capability |
| `benchpod_dac` | function | `benchpod`; stops any DAC output (generate, replay, control loop) at teardown |
| `benchpod_capabilities` | session | `benchpod.capabilities` |
| `benchpod_waveforms` | function | the cloud `WaveformLibrary`; deletes waveforms saved through it during the test; skips without server access |
| `benchpod_pins` / `pins` | session | `pin_1` … `pin_14`, `efuse`, and `has_pullup()`, `has_pulldown()`, `pull_ohms()`, `pullup_ohms()`, `pull_direction()` |
| `firmware` | function | the `--benchpod-firmware` path (skips when unset) |
| `build_report` | function | a build reporter — see [Build reporting](#build-reporting) |
| `measure` | function | `measure(name, value, unit="", min=None, max=None, drift=None)`: records a number for the test; a value outside `min`/`max` fails it, see [Test run recording](#test-run-recording) |

`benchpod` is shared by the whole session, so state a test leaves behind (a running DAC, engaged
pull-ups, a running sensor) carries into the next one — use the teardown fixtures.

**DAC output limits.** A pod can carry DAC output limits that protect an external output stage (a
solar simulator module on the 5 V path, say); it then refuses what they forbid. With the stage
switched off, run the suite with `BENCHPOD_LIFT_DAC_LIMITS=1`: the plugin clears the limits once
when the session starts and puts the same limits back at the end, even when tests fail. It opens
its own short connection, so it applies whichever fixture the suite uses. Without the variable
nothing changes.

### Markers

* `@pytest.mark.hardware` — labels tests that need a real pod, for selection
  (`pytest -m "not hardware"`). Skipping comes from the fixtures, not the marker.
* `@pytest.mark.benchpod_capability("dac_deep_replay", ...)` — skips the test unless the connected
  device advertises every named `Capabilities` flag (`scope`, `analyzer`, `dac_replay`,
  `dac_deep_replay`, `dac_control_loop`, `dac_loop_sources`, `dac_cotrig`, …). For the
  image-bound `dac_control_loop` and `dac_deep_replay` it switches the pod's gateware image instead
  of skipping, when the pod carries both images (see [Gateware images](#gateware-images)).
  `spi_master` and `i2c_master` leave the emulation image (gateware v65+, no bus masters) for the
  loop image, and skip while emulated sensors run.
  `benchpod_capability("analog")` skips only on the digital-only board (no DAC, ADC or 4-20 mA);
  firmware that does not announce the flag ran on analog boards only and counts as analog, like
  `BenchPod.can_analog`.

### Test run recording

With `--embeddedci-run-dir DIR` the plugin records the whole run into one folder: the outcome of
every test, the numbers tests measured, and every call each test made to a pod, with the
provenance needed to compare two runs (the firmware file's hash, the tests' git commit, the pod's
firmware, gateware and calibration, the wiring profile, tool versions). It needs no account and
writes nothing anywhere else. Recording is off unless the option or `EMBEDDEDCI_RUN_DIR` is set, so
an existing suite writes nothing new.

```bash
pytest --embeddedci-run-dir=auto --embeddedci-suite=process-node-hil --embeddedci-dut=process-node \
       --embeddedci-keep-traces=failed
```

```
<run folder>/
  run.json                  the record (schema_version 1), written last
  junit.xml                 the same tests as JUnit XML, for CI test reporters
  trace/<test>.ndjson       the pod calls of one test, one JSON object per line
  att/<n>-<command>.json    large requests and replies a trace line points to (.json.zst with zstandard)
```

`run.json` holds `key` (suite, dut), `inputs` (`dut_firmware` from `--benchpod-firmware`, `tests`
from git, `pod`, `wiring`, `tools`, `github`), `health`, and `results`: the run's `outcome`
(`passed`, `failed`, `error`, `invalid`), `counts` and one entry per test in `cases`. A case has its
`id` (the pytest node id), `outcome` (`passed`, `failed`, `error` for a failure in setup or
teardown, `skipped`, `xfailed`, `xpassed`), `duration_ms`, `message` (the failure: its crash line, such as `ValueError: bad CRC`, then pytest's full report; up to 16 KB), `skip_reason`,
`log` (captured output of failed tests, up to 64 KB), `trace` (the trace file, `""` when not kept),
`measurements`, and `calls`: per pod command the number of calls, the 50th and 95th percentile and
maximum time in ms, and the errors. `calls` is filled even when the trace file is not kept.

A trace line looks like
`{"t": 12.304, "op": "command", "cmd": "can_write", "req": {...}, "ms": 3.1, "ok": true, "resp": {...}}`:
`t` is seconds since the run started, `op` one of `command`, `sequence`, `samples`, `stream`,
`raw` (a CMSIS-DAP or UART proxy session, logged when it closes, with its byte counts), `status`,
`power`. Requests and replies over 4 KB move to `att/`. Passwords, tokens, keys and other secrets
are replaced by `"<redacted>"` before anything is written. A run keeps at most 50 MB of traces and
attachments; past that, lines keep only `t`, `op`, `cmd`, `ms` and `ok`, and `health` says so.
Every `BenchPod` opened while the run records is traced, also one a test opens itself.

`--embeddedci-keep-traces` picks which traces stay: `all` (default), `failed` (failed and error
tests only) or `none`.

**Measurements.** The `measure` fixture records named numbers with optional limits:

```python
def test_heater_response(benchpod, measure):
    ...
    measure("settle_ms", 840, unit="ms", max=1000)          # fails the test above 1000
    measure("overshoot_c", 0.6, unit="C", drift="15%")     # recorded, compared later
```

`value`, `min` and `max` must be finite numbers (`bool`, NaN and infinity raise an error), and a
name is used once per test. A value outside `min`/`max` fails the test after its body ran, listing
every failed limit; the limits are checked also when no run is being recorded. `drift` (`"20%"` or
`0.2`) is a tolerance for comparisons with a baseline and never fails the test. Numeric values a
test passes to pytest's `record_property` are recorded too (with source `record_property`; other
values are left out). A test keeps at most 200 measurements; the rest are dropped and counted in
`health.measurements_dropped`.

**Uploading a run.** `--embeddedci-upload` (or `embeddedci_upload = true` in the ini file, or
`EMBEDDEDCI_UPLOAD=1`) uploads the run to embeddedci.com when the session ends, where it shows
next to the build it tested and can be compared with earlier runs. Without `--embeddedci-run-dir` it
records into `./embeddedci-runs/<started UTC>-<suite>/` first. What goes up: `run.json`,
`junit.xml`, and the traces (with their attachments) of the failed and error tests only; the
folder on disk keeps what `--embeddedci-keep-traces` says. Credentials: `BENCHPOD_API_KEY`, else
the `benchpod login` session, else the workflow's OIDC token in GitHub Actions (`id-token: write`);
`--benchpod-api-base` / `BENCHPOD_API_BASE` picks the server. The run's URL is printed at the end.
A failed upload is a warning: it never changes the test result. Under `benchpod ci` the plugin does
not upload; the runner uploads the whole run.

The upload can be done later, or from another machine, with the same code:

```python
from embeddedci.benchpod.runs.upload import upload_run

print(upload_run("embeddedci-runs/20261118T140211Z-hil")["url"])
```

**Reading runs back.** `embeddedci.benchpod.runs.api` lists, reads and compares the runs on
embeddedci.com, with the same credentials as the upload:

```python
from embeddedci.benchpod.runs import api

runs = api.list_runs(suite="process-node-hil", ref="main", limit=5)["runs"]   # newest first
run = api.get_run(runs[0]["id"])
print(api.summarize_run(run)["failed_cases"])          # id, outcome, the message's first line
diff = api.compare_runs(run["id"], "previous")         # or a run id, or "baseline:main"
print(diff["cases"]["newly_failing"], diff["inputs_changed"])
trace = api.case_trace(run, "test_ack", errors_only=True)   # the failed case's pod calls
junit = api.get_run_file(run["id"], "junit.xml")
```

`list_runs` takes `suite`, `dut`, `build`, `pod` (name or id), `ref`, `limit` (up to 200) and
`before` (the previous page's `next_before`). `RunsApi(server)` does the same with a `ServerApi`
of your own. The MCP server's `cloud_runs_list`, `cloud_run_get`, `cloud_run_compare` and
`cloud_run_trace` tools are built on it.

**Inside `benchpod ci`.** The `benchpod ci` runner hands its run folder to the `pytest` steps it
starts through the environment; the plugin then writes `parts/<step>.json` (the cases, health
counters, tool versions and pod) and its traces into that folder, and the runner merges them into
one `run.json`:

| Variable | Value |
|---|---|
| `EMBEDDEDCI_RUN_DIR` | the run folder (alone, without `EMBEDDEDCI_RUN_STEP`: same as `--embeddedci-run-dir`) |
| `EMBEDDEDCI_RUN_STEP` | the step id: write `parts/<step>.json` instead of `run.json` and `junit.xml` |
| `EMBEDDEDCI_RUN_ID` | the run id recorded in `run.json` |
| `EMBEDDEDCI_RUN_T0` | the run's start in unix seconds, so every step's trace shares one timeline |
| `EMBEDDEDCI_KEEP_TRACES` | `all`, `failed` or `none` (`--embeddedci-keep-traces` wins) |

## Wiring profile

The pod has no role-named pins, so the bench's wiring — which DUT signal is on which LA channel —
is written down once in a `Wiring` profile. Methods that take a channel fall back to it, and named
signals replace channel numbers:

```python
from embeddedci.benchpod import BenchPod, Signal, Trigger, Wiring

wiring = Wiring(uart_rx=3, uart_tx=4, swd_target="target/stm32f4x.cfg",
                signals=[Signal("TRIGGER", 9, "output"), Signal("READY", 10)])
bp = BenchPod("192.168.1.50", la_voltage=wiring.la_voltage, wiring=wiring)

with bp.open_uart() as uart:                   # rx=3, tx=4, 115200 baud from the profile
    bp.power_on()                              # the profile's eFuse
    uart.expect("APP_OK", timeout=6)
bp.flash(file="app.elf")                        # swclk/swdio/nreset/target from the profile
bp.sensors.start("bmp280")                      # sda/scl from the profile
la = bp.capture_la(100_000, trigger=Trigger("READY"))
print(wiring.describe())                        # a pin table: bias resistor and DUT direction per channel
```

| Key | When not set | Meaning |
|---|---|---|
| `la_mv` | not set | LA I/O-bank voltage in mV (1800 or 3300); `wiring.la_voltage` in volts |
| `efuse` | not set (pass `rail=`) | the target-power rail: 1 internal 5 V, 2 external, or on a digital board v4 `"j1_3v3"` / `"j1_1v8"` (J1 pins 17 and 18); every power call that names no rail uses it |
| `uart_rx`, `uart_tx`, `uart_baud` | not wired, not wired, `115200` | `rx` samples the DUT's TX; `tx` drives the DUT's RX |
| `uart_parity`, `uart_stop_bits` | `"none"`, `1` | The UART frame after the 8 data bits (`"even"`, `"odd"`; 2 stop bits). `open_uart`, `capture_uart` and `power_cycle_and_capture` use it when `parity` / `stop_bits` are omitted; anything but 8N1 needs firmware 3.10.8+ |
| `i2c_sda`, `i2c_scl`, `i2c_addr` | not wired, not wired, not set | the I2C bus (emulated sensors default to it; put I2C on LA1-LA6, which have pull-ups) |
| `swd_swclk`, `swd_swdio`, `swd_nreset`, `swd_target` | not wired, not wired, `False`, `""` (auto-detect) | flashing |
| `jtag_tck`, `jtag_tms`, `jtag_tdi`, `jtag_tdo`, `jtag_trst` | not wired | JTAG flashing (firmware feature `jtag`); `jtag_trst` is optional. `jtag_tck` may share a channel with `swd_swclk` and `jtag_tms` with `swd_swdio` (the same pins on an ARM part); every other overlap is an error |
| `debug_port` | `None` (automatic) | `"swd"` or `"jtag"`: which port `flash` uses when the call passes no pins of either kind. Automatic: JTAG only when SWD is not wired or the target is JTAG only. An incomplete pin set for the chosen port is an error naming the unset fields |
| `spi_sclk`, `spi_mosi`, `spi_miso`, `spi_cs` | not wired | decoder channels |
| `signals` | `[]` | named signals: `Signal(name, la, direction="input"/"output"/"open_drain"/"bidir", active_low=False, description="")` |
| `dut_dirs` | `{}` | the DUT direction of a role or signal where its default is wrong: `{"uart_rx": "bidir", "READY": "dut_input"}` (role key or signal name to `dut_input`, `dut_output` or `bidir`); see [pin directions](#gpio-on-the-la-pins) |

**Nothing is wired unless the profile says so** (schema version 2, SDK 3.0). A pin the profile does
not set is never driven, and a call that needs it says which one to pass: `bp.flash()` without
`swclk`/`swdio` in the profile raises `ValueError: swd_swclk was not given and the wiring profile has
no swd_swclk`. The default pins, what profiles used to default to (UART RX on LA5 and TX on LA4, I2C
SDA on LA1 and SCL on LA2, SWD on LA11/LA12, 3.3 V, internal 5 V), are a preset:
`Wiring.preset("embeddedci-default")`. They are
not the wiring of any particular bench: load your board's own profile by name
(`BenchPod(conn, wiring_profile="process-node-stm32")`, `bp.load_wiring("process-node-stm32")`), as
the [process-node example](https://github.com/embeddedci-com/examples/tree/main/process-node-stm32)
does with its `wiring/process-node-stm32.json`. A version 1 file or dict (or one without a `version`
key) still means what it did: what it leaves out takes the default pins' values, and the profile you
get back is version 2 with them written out. Re-save such a file as version 2 with
`benchpod wiring upgrade wiring.json`.

A role set to `None` is not wired. Construction validates the whole profile and raises `ValueError`
listing every problem — including two roles or signals on one LA channel; `wiring.warnings()` lists
wiring that works but is risky, such as an I2C bus on a channel without a pull-up.

**Profiles live on the pod.** A pod with current firmware stores up to 16 named profiles (4096 bytes
of JSON each, 16 KB in all), and one of them is its default. Every connection sees the same ones:
LAN, USB and the cloud, from any computer, signed in or not. The web app shows them and keeps their
history. `bp.wiring` is resolved once, first match wins:

| # | Source | `bp.wiring.source` |
|---|---|---|
| 1 | the `wiring=` argument (a `Wiring`, a dict, or a `.json`/`.toml` file); in pytest `--benchpod-wiring`, then the `benchpod_wiring` fixture | `dict`, `file` (a `Wiring` keeps its own) |
| 2 | the `BENCHPOD_WIRING` file | `file` |
| 3 | a profile stored on the pod: `wiring_profile="k66"` (or `BENCHPOD_WIRING_PROFILE`, pytest `--benchpod-wiring-profile`), else the pod's default profile. A named profile the pod does not have raises | `pod` |
| 4 | firmware that stores no profiles: the profile stored on embeddedci.com for this pod (for `embeddedci:<device>` that device; over the LAN, the device the pod reports it is registered as, with an API key or the `benchpod login` session) | `server` |
| 5 | nothing: `Wiring.undefined()` | `undefined` |

A file (`wiring=` / `BENCHPOD_WIRING`) and a profile name together is an error, not a silent choice.
Step 4 never fails the connection: without credentials, with the pod not registered or the server
unreachable, the SDK logs why (logger `embeddedci.benchpod`) and goes on to step 5;
`wiring_source="local"` (or `BENCHPOD_WIRING_SOURCE=local`) skips it.

```python
bp = BenchPod(conn)                          # the pod's default profile
bp = BenchPod(conn, wiring_profile="k66")    # a named one

bp.wiring = bp.wiring.with_changes(uart_baud=9600)   # this connection only, nothing saved
bp.save_wiring()                             # store it, to the profile it was loaded from
bp.save_wiring(name="nrf52840")              # store it under that name (created if new)
bp.save_wiring(Wiring.preset(), name="nucleo")  # store a full profile (a Wiring, dict or file)
bp.load_wiring("k66")                        # this connection switches to another stored profile
bp.stored_wiring("k66")                      # read one without switching
bp.wiring_profiles()                         # {"default": "k66", "profiles": [{"name", "rev", "bytes"}], ...}
bp.set_default_wiring("k66")                 # what a connection loads when it names none
bp.delete_wiring("old")                      # not the default
```

`bp.wiring.profile_name` and `bp.wiring.profile_rev` say which stored profile (and revision) this
connection uses; both are empty when the profile came from elsewhere.

Scripts and tests never save on their own: a test that changes the baud for one run must not change
the bench for everyone. Saving never changes the default; the pod's first profile becomes its
default. Saving the profile this connection loaded checks nobody saved it meanwhile: when someone
did, `save_wiring` raises `FirmwareError` with `code == "BP0009"` instead of overwriting their change
(`load_wiring` to see it). Saving refuses with `BP0016` while the DAC runs or a capture holds the
pod's bus; reading never touches it. Firmware that stores no profiles: `save_wiring()` replaces a
cloud device's single profile on embeddedci.com, and raises over the LAN or USB.

**Power-on applies the profile.** `bp.power_on()` (or `bp.power_on(wiring_profile="k66")`) sets the
profile's I/O voltage when the pod has none or another one, sets its pull resistors, then switches
the profile's rail on and records on the pod which profile powered the board (`target_status`).
`Wiring.pulls` says which pull resistors are on, as a map of channel to direction
(`{"pulls": {"1": "up", "2": "up", "8": "down"}}` in the JSON) or as a list of channels that pull
their own direction (`{"pulls": [1, 2]}`); `Wiring.pull_directions()` gives either as the map.
`power_on` switches exactly those on and the others off. They work at 3.3 V only, so at 1.8 V nothing
is sent, and a profile without `pulls` (`None`) leaves them as they are. It never changes the I/O
voltage under a board that is already powered: that raises, and `bp.set_la_voltage()` does it if you
mean it.

A file holds the same JSON object the pod stores:

```json
{"version": 2, "la_mv": 3300, "efuse": 1, "uart_rx": 3, "uart_tx": 4, "swd_target": "target/stm32f4x.cfg",
 "signals": [{"name": "TRIGGER", "la": 9, "direction": "output"}, {"name": "READY", "la": 10}]}
```

`bp.signal(name)` returns a GPIO handle for a signal (see [GPIO on the LA pins](#gpio-on-the-la-pins));
channel arguments accept a role or signal name too (`bp.open_uart(rx="uart_rx", tx=4)`).

**The profile on the pod's header.** `bp.board_header()` says which I/O header the pod has:
`"j1"` (the 2x11 pin headers of the one-piece boards up to v3-r2) or `"front"` (the 2x13 connector
on the front of digital v3r3 and later), None when the pod cannot tell. `header_map(wiring, header)`
writes the profile on that header as text: every pin in its physical place, then what to connect
to each pin on your board (header None writes both headers). The web and desktop apps draw the same
thing from the Pins pane's Visualize button.

```python
from embeddedci.benchpod import header_map

print(header_map(bp.wiring, bp.board_header()))
```

## Power, reset and power monitoring

```python
from embeddedci.benchpod import EXTERNAL, INTERNAL

bp.power_on(INTERNAL)                         # internal 5 V eFuse
bp.power_off(INTERNAL, delay=2.0)             # scheduled pod-side; returns immediately
bp.target_power(EXTERNAL, on=True)

rail = bp.target_status().efuse(INTERNAL)     # EfuseState(enabled, fault, valid)
assert rail.enabled and not rail.fault        # fault = tripped (over-current / short)

power = bp.power_status().rail(INTERNAL)      # RailPower(ok, bus_voltage, current)
print(f"{power.bus_voltage:.2f} V  {power.current * 1000:.1f} mA")

bp.reset_target(pulse=0.1)                    # pulse reset without power-cycling
bp.set_reset(True)                            # hold the DUT in reset …
assert bp.reset_state().asserted
bp.set_reset(False)                           # … and release it
print(bp.usb_cc().advertised)                 # USB-C orientation + advertised current
```

`delay` lets a power change land *during* something else (see `power_cycle_and_capture`). The reset
controls drive the pod's dedicated reset pin (DUT header J1 pin 22); they and `usb_cc()` need a rev3
pod (`ResetState.supported`). `TargetStatus.supported` is false on boards that cannot read the eFuse
state back. An omitted rail is the wiring profile's (internal by default).

**`rail=` names every power output** (`power_on`, `power_off`, `target_power`, `measure_power`,
`power_profile`, `power_cycle_and_capture`): `"internal"` (eFuse 1), `"external"` (eFuse 2) and, on the
digital board v4 (hardware fact `dut_rails`, feature `dut_rails`), `"j1_3v3"` and `"j1_1v8"`, the
switched J1 pins 17 (+3.3 V, about 1.45 A limit) and 18 (+1.8 V). `efuse=` is the old name for 1 and 2;
passing both is a `ValueError`. The J1 pins are on from boot, independent of the eFuses and of
resets, and have no current monitor (`measure_power` and `power_profile` on one raise `ValueError`).
`TargetStatus.j1_rails` is a `J1Rails(v3v3, v1v8)`, `None` on boards where both pins are always on.

```python
bp.power_off(rail="j1_3v3")
assert bp.target_status().j1_rails.v3v3 is False
```

### Power profiles

`power_status()` is one snapshot. A power profile samples the rail's monitor about a thousand times a
second **without gaps** — each sample averages its whole period — so energy and charge are integrated,
and short bursts (an inference, a radio transmit) show up:

```python
prof = bp.measure_power(2.0, keep_samples=1000)          # blocks for 2 s
print(prof.avg_current, prof.peak_current, prof.avg_voltage, prof.energy, prof.charge)

with bp.power_profile(efuse=INTERNAL) as session:        # around a block of code
    bp.power_on(delay=0.2)                               # inrush lands inside the profile
    time.sleep(3)
boot = session.result
assert boot.peak_current < 0.5 and not boot.fault
t, amps, volts = boot.samples[0]                         # bin-averaged trace (keep_samples, ≤ 4096)
```

`PowerProfile`: `efuse`, `rate_hz`, `adc_rate_hz`, `n`, `duration` (s), `avg_current`, `min_current`,
`peak_current` (A), `avg_voltage`, `min_voltage`, `max_voltage` (V), `energy` (J), `charge` (C),
`avg_power` (W), `fault` (the eFuse tripped), `truncated` (stopped at `max_duration`), `samples`.

**Sample rate.** Ask for 100-500 Hz (default 500). The pod takes one reading per firmware pass, so
what you get back is not always what you asked for: it tracks the request to ~200 Hz, then beats
against the pass interval and flattens near 365 Hz. `rate_hz` is what was **actually delivered**
(measured) and `adc_rate_hz` what the current sensor was configured for. Every sample carries its own
timestamp, so the trace and the energy/charge integrals are exact either way — the rate only tells you
how finely a transient was resolved. Measured on a v2 pod:

| Asked | 100 | 200 | 300 | 400 | 450 | 500 |
|---|---|---|---|---|---|---|
| Delivered | 105 | 189 | 288 | 305 | 361 | 364 |

| Rail | Supply | Current limit | Cut-off | Monitor resolution |
|---|---|---|---|---|
| eFuse 1 (internal) | 5 V from the pod's USB-C | ≈ 2.0 A | 5.7 V | 100 µA |
| eFuse 2 (external) | 2.7-20 V on the external terminal | ≈ 3.0 A | ≈ 21.8 V | 167 µA |

An overload is held at the limit for about 2.8 ms, then the eFuse trips (`TargetStatus` `fault`) and
retries after ≈ 110 ms. A trip shorter than one sample does not show in a profile. The ADC's `current_in`
input is a 4-20 mA loop terminal (249 Ω, 100 Ω from analog module v4), not a way to measure the target's supply.

## Flashing

```python
from embeddedci.benchpod import INTERNAL, PIN11, PIN12

bp.flash(
    file="build/app.elf",
    target="target/stm32f4x.cfg",   # any OpenOCD target config
    swclk=PIN11, swdio=PIN12,       # any two LA channels
    nreset=True,                    # the target's NRST is wired to the pod's reset pin (J1 pin 22)
    target_power=INTERNAL,          # optional: power the target first
)
```

SWD and JTAG flashing need the pod's bus masters, which the emulation image lacks from gateware v65
(see [Gateware images](#gateware-images)): flash first, then start the sensors. With sensors
already running, `flash()` raises `UnsupportedFeatureError` (`BP0059`: stop them with
`bp.sensors.stop()` first); with none running the SDK switches the pod to the loop image itself
(~3 s).

By default a failed flash raises `FlashError` (or `TargetUnreachableError` when nothing answers on
SWD) carrying OpenOCD's output. Pass `check=False` to get the `FlashResult` and assert yourself:

```python
result = bp.flash(file="build/app.bin", load_address="0x08000000",
                  target="target/stm32f4x.cfg", swclk=PIN11, swdio=PIN12, check=False)
assert result.ok, result.stderr     # also: returncode, stdout, target_unreachable, stalled, artifact
```

* **SWD or JTAG is chosen for you** (there is no transport option). `tck`, `tms`, `tdi`, `tdo` and
  `trst` are the JTAG pins; omitted ones come from the profile's `jtag_*` fields. The flash uses
  JTAG when all four JTAG pins are known and either no SWD pins are, or the target is JTAG only:
  `esp32`, `esp32s2`, `gd32vf103`, `fe310`, `k210` and any name starting with `riscv` (the OpenOCD
  config's file name, so `target/esp32.cfg` counts). Otherwise it uses SWD. With both wired, an ARM
  target keeps SWD (faster) unless the profile's `debug_port` says `"jtag"`. The order: pins passed
  in the call win (JTAG pins only: JTAG, the rest from the profile; SWD pins only: SWD; both kinds
  or neither: go on), then the profile's `debug_port` (`"swd"` or `"jtag"`; an incomplete pin set
  raises, e.g. "debug_port is jtag but jtag_tdo is not set"), then the automatic rule above. JTAG needs the pod's `jtag` feature (firmware with features table
  revision 16, gateware v61 or later, not the emulation FPGA image) and raises
  `UnsupportedFeatureError` before anything is armed without it. A bare `"esp32"` target (no
  `target/` path) still flashes over the chip's UART bootloader. An uploaded build flashed by
  embeddedci.com cannot use JTAG yet: pass `server_side=False`.
* `nreset` is a flag, not a pin. With it, `connect_under_reset` defaults to `True`; without it,
  connect-under-reset is forced off (the pod cannot hold a reset it isn't wired to).
* `verify=True` and `reset=True` map to OpenOCD's `program … verify reset`. `verify=False` is
  noticeably faster.
* `connect_attempts=5` retries the whole run when the target was unreachable or the SWD link stalled
  (see `BENCHPOD_STALL_TIMEOUT`); a flash that ran and failed is not retried. `timeout=300` bounds
  one OpenOCD run.
* `extra_configs` adds `-c` commands and `extra_args` raw OpenOCD arguments; `clear_reset_events=True`
  empties the target config's clock-boost reset events, which otherwise race the SWD link.
* OpenOCD runs on the machine running the test, on every transport including the cloud.

### Flashing a build uploaded to embeddedci.com

A build uploaded with the `upload-artifact` GitHub action or the `build_report` fixture can be
flashed by naming it instead of passing `file`:

```python
bp.flash(build="latest", repo="acme/firmware", ref="main")   # newest build of that branch
bp.flash(build="3f9c2a1e-...")                               # the build_id the upload printed
res = bp.flash(build="3f9c2a1e-...", artifact="app.hex", check=False)
print(res.artifact["name"], res.artifact.get("sha"))         # which file was flashed
```

* `build` is the build id (`"latest"` or empty: the newest build that matches the rest), `repo` is
  `owner/name` (or `name`), `ref` a branch or tag, and `artifact` a file name in the build. Without
  `artifact` the build's .elf is used, else its .hex, else its .bin; a build with two of the same
  type needs `artifact`.
* On a cloud connection (`embeddedci:<device>`) embeddedci.com runs OpenOCD through the pod: nothing
  to install on the runner, and a GitHub Actions OIDC session is enough. Such a flash always
  verifies and resets and takes no `openocd_bin`, `extra_configs` or `extra_args`.
* On a LAN or USB connection the file is downloaded to a temp file and flashed by the local OpenOCD
  as above. Downloading needs an API key (`BENCHPOD_API_KEY`) or `benchpod login`.
* `server_side=False` flashes a cloud pod with the local OpenOCD instead; `server_side=True` asks for
  the server on a pod connected some other way (it must be registered on embeddedci.com).
* `FlashResult.artifact` holds the build's `job_id`, `name`, `repo`, `ref` and `sha` (`None` for a
  local `file`). An omitted `target` falls back to the target the build recorded.

### Flashing an ESP32 target (UART ROM bootloader)

ESP32-family targets (ESP32, S2, S3, C2, C3, C6, H2) have no SWD. Give `flash()` an ESP32 `target`
and the pod writes the image through the chip's own ROM bootloader instead, the way esptool does:
no OpenOCD, no esptool and no USB cable to the target. Wire the target's TXD0 to `rx` and RXD0 to
`tx` (default: the profile's `uart_rx`/`uart_tx`), IO0 (IO9 on the C3/C6) to `boot`, EN to the
pod's reset pin (`nreset=True`) and GND to GND, at 3.3 V.

```python
bp.flash(target="esp32s3", file="build/flasher_args.json", boot=6, nreset=True)  # ESP-IDF build
bp.flash(target="esp32", file="merged.bin", rx=3, tx=4, boot=6, nreset=True)    # merge_bin image at 0
bp.flash(target="esp32c3", file="build/app.bin", load_address="0x10000", boot=6, nreset=True)
```

* `file` is a `.bin` written at `load_address` (default 0) or an ESP-IDF `flasher_args.json`
  (bootloader, partition table and app at their offsets, and the flash size).
* `target` is `esp32`, `esp32s2`, `esp32s3`, `esp32c2`, `esp32c3`, `esp32c6` or `esp32h2`; the pod
  reads the actual chip and `FlashResult.stdout` names it, with every region and its MD5.
* `boot` defaults to a profile signal named `BOOT`, `IO0` or `IO9`; EN may also be a profile
  signal named `EN` instead of the reset pin. Both are driven open-drain and released at the end.
* Each region is uploaded into the pod, written at 460800 baud and checked against the chip's
  MD5. `reset=True` (the default) starts the new image; `reset=False` leaves the target in its
  loader.
* A failure raises `FlashError` naming the region and the step (no answer from the ROM loader:
  check power, the TX/RX crossing, the boot pin and EN); `check=False` returns the result.
* Works over the LAN, USB and the cloud. Needs pod firmware with the `esp_flash` command.

### Flashing a chip over its own programming pins (UPDI, PIC, C2, TPI, PDI)

Some chips have neither SWD, JTAG nor a bootloader: they are programmed over a one- or two-wire
protocol of their own. The pod runs that protocol itself, so there is no programmer, no OpenOCD and
no USB cable to the chip. It is the same `flash()` call, with one of these `target` names:

| `target` | Also accepted | Chips | Wiring |
|---|---|---|---|
| `avr-updi` | `updi` | AVR tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD | UPDI on `data` |
| `pic` | | PIC16F1xxx, PIC16F18xxx, PIC18F Q-series (low-voltage entry) | ICSPCLK on `clk`, ICSPDAT on `data`, MCLR on the pod's reset pin or `reset_pin` |
| `efm8` | `c2` | Silicon Labs EFM8 and C8051F | C2CK on `clk`, C2D on `data` |
| `attiny-tpi` | `tpi` | ATtiny4/5/9/10/20/40/102/104 | TPICLK on `clk`, TPIDATA on `data`, RESET on the pod's reset pin or `reset_pin` |
| `xmega` | `pdi` | AVR XMEGA | PDI_CLK (the chip's RESET pin) on `clk`, PDI_DATA on `data` |
| `msp430` | `sbw` | MSP430 FRAM parts (FR2433 so far) over Spy-Bi-Wire | SBWTCK (the chip's TEST pin) on `clk`, SBWTDIO (its RST pin) on `data`; gateware v52+; about 70 s for 16 KB |

```python
bp.flash(target="avr-updi", file="build/app.hex", dry_run=True)   # wiring test: reads the chip's ID, writes nothing
bp.flash(target="avr-updi", file="build/app.hex")                 # data from the profile's swd_swdio
bp.flash(target="pic", file="build/app.hex", clk=3, data=4)       # MCLR on the pod's reset pin
bp.flash(target="efm8", file="build/app.hex", device="EFM8BB10F8G", erase="page", reset=False)
```

* **The chip is found by its ID.** The SDK enters programming mode first and reads the chip's ID,
  looks it up in its device table (`embeddedci/benchpod/prog_devices.json`, described in
  `prog_devices.README.md` next to it: the chips, their memories and the hex-file address of each,
  the protocol options and the voltage limits, every family with its datasheets) and cuts the image into the chip's memories. `device`
  names the chip (`"ATtiny1616"`, `"PIC16F18446"`) when you want it checked: a different chip on
  the board is an error before anything is erased, and so is a chip that is not in the table.
* **`file`** is a `.hex` (Intel HEX), an `.elf` (32-bit) or a `.bin` (written at `load_address`
  into the flash, default 0). The regions use the toolchains' usual addresses: avr-gcc puts the
  flash at 0, the EEPROM at 0x810000, the fuses at 0x820000 and the user row at 0x850000; XC8 puts the
  PIC's configuration words, user ID and EEPROM where the table says. Flash comes first, EEPROM and user row next, the
  configuration (fuses) last. A partial flash page is padded with 0xFF. Lock bits and signature
  rows in the image are left out (the result says so); data that fits none of the chip's memories is
  an error.
* **Pins.** `clk`, `data` and `reset_pin` are LA pins (`benchpod.PIN1`..`PIN14`, 1-14, or a profile
  name); `avr-updi` takes `data` only. Omitted, a wiring-profile signal named for the protocol is
  used (`UPDI`; `ICSPCLK`/`ICSPDAT` or `PGC`/`PGD`; `C2CK`/`C2D`; `TPICLK`/`TPIDATA`;
  `PDI_CLK`/`PDI_DATA`; `MCLR` or `RESET` for the reset), else the profile's `swd_swclk` and `swd_swdio`. The reset of `pic` and `attiny-tpi` is
  the pod's reset pin by default (`nreset=True`) or `reset_pin`.
* **`erase`** is `"chip"` (the default: erase the chip before the first write when the image has flash; this
  also unlocks a locked chip and erases its EEPROM on some families), `"page"` (only the pages written; the TPI chips
  can only erase their whole code section, once) or `"none"`. **`reset`** (default `True`) lets the
  chip start its program afterwards; `False` keeps it in programming mode or reset.
* **`dry_run=True`** only enters programming mode and reads the ID: `FlashResult.stdout` names the chip.
  Run it first on a new wiring.
* Every page written is read back and compared. `FlashResult.stdout` lists the chip and every
  region written.
* **Voltage.** The pod's I/O is 1.8 or 3.3 V, the chip's supply. The ATtiny4/5/9/10/20/40 can be
  programmed at 5 V only (4.75-5.5 V), which the pod cannot give: `flash()` raises
  `ProgDeviceError` before anything is written. PIC bulk erase needs the supply above the chip's
  brown-out level (about 2.7 V on most): `erase="chip"` at 1.8 V is refused for those chips.
* A failure raises `ProgFlashError` (a `FlashError`) with `result` (the pod's outcome), `phase`,
  `mem`, `addr` and `at_addr`, and a message that says what to check: `ProgNoAnswerError` (also a
  `TargetUnreachableError`: wiring, power, I/O voltage, a programming pin disabled by fuses),
  `ProgLockedError` (`erase="chip"` unlocks), `ProgVerifyError`, a pin the wiring profile marks as a chip output
  (the pod refuses to drive it, `BP0034`: mark it `bidir` with `la_dir` or fix the wiring; the SDK never overrides directions), `ProgDeviceError` (chip not in the
  table, not the `device` named, image does not fit, voltage). `check=False` returns the
  `FlashResult` instead.
* Works over the LAN, USB and the cloud. Needs pod firmware with the `prog_flash` feature
  (`bp.supports("prog_flash")`; `UnsupportedFeatureError` otherwise) and the `xip_prog` blob,
  installed with the other blobs (`benchpod install-blobs`): the pod refuses with a
  `XipUnavailableError` without it. While a job runs the pod answers busy to captures, uploads
  and DAP, SPI or I2C sessions, and a USB flash cannot start.

## Detecting the chip on the board

`bp.detect()` finds out which chip is wired to the pod and reports every probe's answer. Nothing
needs to be named: it uses the wiring profile.

```python
res = bp.detect()                      # all probes the profile wires
res = bp.detect(invasive=False)        # SWD, JTAG and USB only: nothing resets the chip
res = bp.detect(methods=["swd", "esp32"])
for f in res.findings:
    print(f.summary)                   # STM32F446 (STMicroelectronics, Cortex-M4) by swd, high
print(res.skipped, res.notes)
res.to_dict()                          # {"powered", "findings", "skipped", "notes"}
```

`detect(*, invasive=True, methods=None)` returns a `DetectResult`: `powered`, `findings` (a list of
`DetectFinding`), `skipped` (a list of `(method, reason)`) and `notes`. A `DetectFinding` has
`method`, `chip` (`"STM32F446"`, or `None` when only the vendor or the debug port is known), `vendor`,
`core` (`"Cortex-M4"`), `id` (the number the match rests on, for example the STM32's DBGMCU_IDCODE
`"0x10006421"`), `confidence`, `pins` (the LA channels used), `raw` (the pod's reply) and `invasive`,
and a `summary` line. `confidence` is `"high"` for an exact table match (an STM32 DEV_ID at its
family's address, an ID of the programming device table, an ESP chip name, a known VID:PID),
`"medium"` when the vendor and the core are known but not the part, and `"low"` when only a debug
port answered (`chip` then names the port).

| `method` | What it reads | Needs in the wiring profile | Resets the chip |
|---|---|---|---|
| `swd` | the debug port (DPIDR, TARGETID, the AP's CPUID), then the vendor's ID register for that core | `swd_swclk`, `swd_swdio` | no |
| `jtag` | the IDCODEs of the JTAG chain | `jtag_tck`, `jtag_tms`, `jtag_tdi`, `jtag_tdo` | no |
| `usb` | the device already mounted on the pod's USB host port | host mode on (detect never turns it on) | no |
| `updi`, `pic`, `c2`, `tpi`, `pdi` | the ID the chip answers in programming mode (the `prog_flash` `info` op, as `flash(..., dry_run=True)`) | signals named for the protocol: `UPDI`; `ICSPCLK` and `ICSPDAT`; `C2CK` and `C2D`; `TPICLK` and `TPIDATA`; `PDI_CLK` and `PDI_DATA` | yes |
| `esp32` | the chip name the ROM loader reports (the `esp_flash` `sync` op) | `uart_rx`, `uart_tx` and a `BOOT`, `IO0` or `IO9` signal | yes |

* **Power first.** With the target's rail (the profile's `efuse`, else either rail) off, nothing is
  probed: `powered` is `False` and a note says so. The LA voltage must be set (the profile's `la_mv`).
* **Pins owned by something else** (a UART, GPIO, sensor or an open SPI/I2C session) are not
  probed: the method is in `skipped` with the owner. A probe that errors or gets no answer is a
  `skipped` entry too; only a lost connection raises.
* **A probe runs only when the profile wires its pins.** The programming protocols never fall back
  to the SWD pins (unlike `flash()`): detect does not know the chip, so it only drives a pin
  the profile names for that protocol. `pic` and `tpi` also need the chip's reset: a profile signal
  named `MCLR` or `RESET`, else the pod's reset pin only when the profile sets `swd_nreset`; the
  ESP32's EN likewise (a signal named `EN`, else the reset pin under `swd_nreset`). Methods the
  profile does not wire are one line in `notes`; name them in `methods=` to see them in `skipped`.
* **SWD and JTAG** need pod firmware with the `dap_id` feature (`bp.supports("dap_id")`); on older
  firmware they are `skipped` with "needs pod firmware with dap_id (update the pod)". The pod reads
  the IDs itself without halting, resetting or writing the chip (the first read waits up to 3 s for the target to answer, `SWD_ANSWER_WAIT_MS`). SWD is two calls: the debug port
  and CPUID first, then the ID registers that fit that core.
* **Invasive probes** (`invasive=True`, the default) reset the chip: the programming protocols
  enter programming mode, and the ESP32 is reset into its ROM loader (reset or power-cycle it to
  run its program again). `invasive=False` runs only `swd`, `jtag` and `usb`.
* **The ID table** is `embeddedci/benchpod/detect_ids.json` (STM32 DEV_IDs per family, Nordic parts,
  Arm cores, JTAG IDCODEs, USB VID:PIDs); `embeddedci.benchpod.detect` has the pure mapping
  functions (`finding_from_swd`, `findings_from_jtag`, `finding_from_usb`).

## USB host mode and USB flashing

Firmware 4.0.3 and later. The pod's USB port becomes a full-speed USB host for the board under test,
reports what enumerates, and flashes the board the way a computer would. Older firmware raises
`UnsupportedFeatureError` ("needs firmware 4.0.3"). Works over the LAN and the cloud, not over the
pod's own USB link: while the pod hosts it is off the computer's USB (no console, no USB network).

| Pod | Port | How the board connects |
|---|---|---|
| digital v4 and later | `usb_a` | its normal USB cable in the pod's USB-A host port |
| older pods | `usb_c` | the pod's USB-C connector through a breakout (D+, D-, GND to the board, 5 V into the pod's VBUS) |

```python
st = bp.usb_host(timeout_s=120)             # the port comes from board_rev; waits for mode "host"
dev = bp.usb_wait_device()                  # or usb_wait_device(vid="16c0", pid="0478")
print(dev.vid_pid, dev.product, [i.class_name for i in dev.interfaces])
res = bp.usb_flash("build/app.hex", progress=lambda done, total: print(done, total))
print(res.mode, res.reason)                 # which loader the SDK chose, and why
bp.usb_host(False)                          # back to being a USB device
```

* `usb_host(enable=True, *, port=None, timeout_s=60)` starts or stops host mode and returns a
  `UsbHostState`. `port` is `"usb_a"` or `"usb_c"`; left out, the SDK uses `usb_a` on a board whose
  `board_rev` is v4 or later, `usb_c` on an older one, and tries `usb_a` then `usb_c` when the pod
  does not say. When `timeout_s` runs out the pod is a USB device again.
* `usb_host_status()` returns the `UsbHostState`: `mode`, `port`, `attached`, `enumeration`
  (`none`, `running`, `done`, `stuck`), `connected` and `line_speed` (a board that draws power but is
  not `connected` has no D+/D- link to the pod; `low` means D+ and D- are swapped), `enumerations`,
  `remaining_s`, `host_ms`, `hosting`, `mounted` and the `device` (a `UsbDevice`: `vid`, `pid`,
  `vid_pid`, `manufacturer`, `product`, `serial`, `speed`, `config` with `max_power_ma` and its
  `interfaces`, `has_interface(class, subclass=None)`).
* `usb_wait_device(vid=None, pid=None, timeout=10)` waits for a (matching) device and returns it.
  `UsbHostError` when host mode is off, `UsbTimeout` when nothing mounts; the message says how far
  the board got.
* `usb_flash(file, *, address=None, name=None, leave=True, mode=None, port=None, timeout=None,
  device_timeout=10, progress=None, verify=False)` flashes the board and returns a `UsbFlashResult`
  (`mode`, `reason`, `length`, `address`, `name`, `protocol`, `ms`, `device`, `note`). A failure raises
  `UsbFlashError` with the pod's `result`, `phase` and `mode`. `usb_flash_status()` returns the
  `UsbFlashState` (`running`, `have_result`, `phase`, `done`, `total`, `result`, `ok`, `ms`, `note`) and
  `usb_flash_abort()` stops a flash.

There is nothing to choose. The SDK picks the loader from the file and the device that mounted:

| The board shows | File | Loader |
|---|---|---|
| Teensy loader (16C0:0478) | `.bin`, `.hex` | `teensy` |
| a DFU interface (STM32 ROM loader, Black Pill) | `.bin`, `.hex`, `.dfu` | `dfu`, at the address in the file, else 0x08000000 on an STM32 |
| a drive (Pico, Adafruit, tinyuf2, DAPLink, micro:bit) | `.uf2` | `uf2` |
| a drive | anything else | `file`, copied as `name` (default: the file's name in 8.3) |
| Arduino, Adafruit or SparkFun AVR board, or a USB-serial chip (CP210x, CH34x, FTDI) | `.bin`, `.hex` | `avr` |
| Arduino, Adafruit or Seeed SAMD board | `.bin`, `.hex` | `samd`, at the address in the file, else 0x2000 (0x4000 on a SAMD51) |
| Espressif device, or a USB-serial chip with an ESP image (starts with 0xE9) | `.bin` | `esp`, at `address` (default 0, a merged image) |
| Raspberry Pi Pico (RP2040) or Pico 2 (RP2350) in BOOTSEL | `.bin`, `.hex` | `picoboot` (feature `usb_flash_picoboot`), at `address` (default 0x10000000): erase, write, read every page back and compare, then boot. A `.uf2` file still goes to the drive |

`.hex` and `.dfu` files are converted to a binary with their start address (gaps up to 64 KB are
filled with 0xFF). `leave=False` keeps a `dfu`, `teensy`, `avr`, `samd`, `esp` or `picoboot` board
in its loader. A drive can be FAT12, FAT16, FAT32 or exFAT, and a `file` flash sends the computer's
clock, so the file on the drive has today's date. `verify=True` (feature `usb_flash_readback`) reads
the drive back: after a `file` flash it waits for the loader's drive to come back and reports its
`FAIL.TXT` as the failure (a DAPLink board says why it refused the file), after a `uf2` flash it
compares the loader's `CURRENT.UF2` with the file while the board stays in its loader; `result.note`
says what was found, or `verify skipped: ...` when the board had left its loader. `mode=` overrides the choice (a name from `embeddedci.benchpod.usb_host.MODES`). `usb_flash`
starts host mode itself when it is off, switches the pod to the deep-replay gateware image (about
3 s, like an ESP32 flash), stages the file in the pod's memory and polls the flash; `progress` gets
sectors, blocks or bytes (by loader). The flash is aborted after `timeout` seconds (default: a
minute plus the file size at 2.5 KB/s) or on Ctrl-C. While it runs the pod refuses captures, DAC
playback and uploads. Host mode stays on afterwards until its own timeout or `usb_host(False)`.

### The board's USB serial port

The serial port of the hosted board is just another argument where you would give pins: pass
`port="usb"` to `open_uart` or `capture_uart` (firmware 4.0.3+, feature `uart_usb`). It is a CDC ACM
port (ESP32-S3 and C3, Pico, nRF52840, ...) or the FTDI, CP210x or CH34x chip of a dev kit, so a test
reads the boot log over the cable the board was flashed through. It returns the usual `UartSession`
or `UartCapture`; no LA channel, wiring profile, pull-up or I/O voltage is involved.

```python
bp.usb_host(timeout_s=300)
bp.usb_flash("build/app.bin", address=0x10000)
with bp.open_uart(port="usb", baud=115200, dtr=True, rts=True) as uart:
    uart.expect("ready", timeout=10)
log = bp.capture_uart(port="usb", duration=5, until="login:")
```

`baud` (default 115200; real on a USB-serial chip, ignored by a board's own USB), `parity`,
`stop_bits` and the control lines `dtr` and `rts` (default `True`; many boards reset or enter their
loader on them) apply. `rx`, `tx`, `half_duplex`, `entry` and `de` with `port="usb"`, and `dtr` or
`rts` without it, raise `ValueError`. A pod without the feature raises `UnsupportedFeatureError`
before anything is sent; host mode off, no board or no serial port is `UsbSerialUnavailableError`
(`BP0085`), and a running `usb_flash` or `usb_check` is `UsbSerialBusyError` (`BP0086`). While the
port is open the pod refuses `usb_flash` and `usb_check`. The session ends when host mode ends.

### USB checks

`usb_check(test, ...)` checks the board's own USB firmware (feature `usb_check`): with the board's
VBUS wired to target-power eFuse 1 and eFuse 1 on (`power_on`), the pod measures what a computer
cannot. It starts host mode when it is off, runs the check in the background and polls it to the end,
returning a `UsbCheckState`: `passed` (the verdict), `error` (why it stopped early, empty when it ran
through), `phase`, `done`, `total`, `ms`, `test`, `running`, `have_result`, `ended` and `result` (the
numbers of the test, in uA and ms). A board that fails is a result, not an exception.

```python
bp.power_on(benchpod.INTERNAL)                       # the board's VBUS comes from eFuse 1
st = bp.usb_check("enumerate", count=10)             # power-on to pull-up, descriptor, mounted
print(st.passed, st.result["mount_ms"])              # {"min": ..., "avg": ..., "max": ...}
st = bp.usb_check("power", duration_ms=3000)         # current against bMaxPower
st = bp.usb_check("suspend")                         # suspended current, resume, still answers
st = bp.usb_check("reset", count=20)                 # port resets, time to enumerate again
st = bp.usb_check("replug", count=10, off_ms=[5, 20, 100, 500])   # VBUS dropouts
```

* `usb_check(test, *, count=None, off_ms=None, timeout_ms=None, duration_ms=None, settle_ms=None,
  port=None, device_timeout=10, timeout=None, progress=None)`: `test` is one of
  `embeddedci.benchpod.usb_host.CHECKS` (`enumerate`, `power`, `suspend`, `reset`, `replug`). Arguments
  left out take the pod's defaults and the pod checks their ranges. The check is aborted after
  `timeout` seconds (default: its worst case plus a margin) or on Ctrl-C.
* `usb_check_status()` and `usb_check_abort()` return the running check's `UsbCheckState`, or stop it
  (eFuse 1 and the bus are put back as they were).
* A refusal is `UsbCheckRefusedError` (`BP0087`: bad argument, host mode off, no board, a flash,
  another check or a power profile running) or `UsbCheckNoPowerError` (`BP0088`: eFuse 1 is off or
  does not power the board).

### HID interfaces and input reports

With a keyboard, mouse, gamepad or custom HID device on the host port (feature `usb_hid`):

```python
for itf in bp.usb_hid_interfaces().interfaces:       # the first two HID interfaces
    print(itf.instance, itf.protocol, itf.descriptor.hex())
bp.usb_hid_capture()                                 # catch every input report into a ring of 64
rep = bp.usb_hid_read(count=8, timeout=5.0)          # up to 8 reports, waiting up to 5 s for one
for r in rep.reports:
    print(r.ms, r.instance, r.data.hex())
bp.usb_hid_capture(False)                            # stop and free the ring
```

* `usb_hid_interfaces()` returns a `UsbHidState`: `attached` and `interfaces`, each a `UsbHidInterface`
  (`instance`, `interface`, `protocol` of `keyboard`, `mouse` or `none`, `descriptor_len`, the whole
  report `descriptor` as bytes, `truncated`). The SDK reads a long descriptor in pages.
* `usb_hid_capture(enable=True)` starts or stops the capture and returns a `UsbHidCapture`
  (`capturing`, `buffered`, `dropped`). It ends by itself when the board detaches or host mode ends.
* `usb_hid_read(count=1, timeout=2.0, instance=None)` returns a `UsbHidReports` (`reports`,
  `dropped`, `capturing`; possibly no report): each `UsbHidReport` has `ms` since the capture
  started, `instance` and `data` (with several report IDs the first byte is the ID). A read while no
  capture runs starts one and leaves it running. `capturing` false means the capture had ended.
* While a capture runs, `usb_flash` and `usb_check` are refused; host mode is kept on by its own
  timeout, so renew it (`usb_host(timeout_s=...)`) for a long capture.

## SPI flash and SPI devices

Pods with gateware v45+ (`bp.capabilities.spi_master`) run an SPI master on any four LA pins, up
to 6 MHz, modes 0 and 3. Gateware v55+ also runs modes 1 and 2 (`open_spi(mode=1)`); on older
gateware they raise `UnsupportedFeatureError` before anything is sent. From gateware v65 the
emulation image has no SPI master: with emulated sensors running `open_spi()` and `spi_flash()`
raise `UnsupportedFeatureError` (`BP0059`; stop the sensors first), with none running the SDK
switches the pod to the loop image itself. Wire a 25-series SPI NOR flash (W25Q, MX25, GD25, ...) with its SCK, DI,
DO and CS on LA channels (power it at the LA voltage, tie /WP and /HOLD high) and program it:

```python
res = bp.spi_flash("fw.bin", 0x0, sck=13, mosi=14, miso=6, cs=5, hold_reset=True)
print(res.jedec_id, res.length, res.seconds)       # erased, written and verified
```

Or work with a session (pins from the wiring profile's `spi_sclk`, `spi_mosi`, `spi_miso`,
`spi_cs` when omitted):

```python
with bp.open_spi(hz=6_000_000) as spi:
    print(spi.flash_id())                          # SpiFlashInfo(jedec_id='ef4017', present=True, ...)
    data = spi.flash_read(0x0, 4096)
    spi.flash_erase(0x10000, 65536)
    spi.flash_write(0x10000, payload)              # 768 B per command, each verified
    rx = spi.transfer(b"\x9f\x00\x00\x00")        # raw full-duplex, for any SPI device
```

`hold_reset=True` holds the DUT in reset for the job (rev3 pods), so its own controller does not
drive the same bus. The SPI master shares the SWD engine: an SWD flash cannot run while a session
is open. Expect about 30 KB/s written (verified) over the LAN, less over the cloud.

`spi.stream(data, head=b"...")` sends data of any size in one CS frame, write-only: it is uploaded
into the pod's PSRAM first, then clocked out 512 bytes at a time (an FPGA bitstream into a
slave-SPI configuration port). It needs a LAN or cloud connection and firmware with the
`spi_stream` command (`bp.capabilities.spi_stream`).

## I2C master

Pods with gateware v55+ (`bp.capabilities.gateware`, `bp.capabilities.bus_master`) are an I2C master
on any two LA pins, 10 kHz to 1 MHz: read and write registers of a chip on the DUT's board, or scan
the bus. The pins are open drain, so the bus needs pull-ups: the pod's own on LA1-LA6 (switch them
on first; they pull to the LA voltage, at 1.8 V on firmware newer than 3.10.12), or the board's with
`pullups="external"`.

From gateware v65 the emulation image has no I2C master: with emulated sensors running `open_i2c()`
raises `UnsupportedFeatureError` (`BP0059`; stop the sensors first), with none running the SDK
switches the pod to the loop image itself.

```python
from embeddedci.benchpod import I2CNackError

bp.enable_pullup(1, 2)                             # SDA, SCL on LA1, LA2
with bp.open_i2c(hz=400_000) as i2c:               # pins from the wiring profile's i2c_sda, i2c_scl
    print([hex(a) for a in i2c.scan()])            # [0x76]
    chip_id = i2c.read_reg(0x76, 0xD0)[0]          # write 0xD0, repeated START, read 1 byte
    i2c.write_reg(0x76, 0xF4, b"\x27")
    raw = i2c.write_read(0x76, b"\xf7", 6)
    assert i2c.probe(0x76)
    try:
        i2c.read(0x50, 2)
    except I2CNackError as exc:                    # exc.addr, exc.nack_at (0 = the address)
        print(exc)
    r = i2c.xfer(0x50, b"\x00", read=2)            # the I2CResult, without raising
    print(r.ack, r.nack_at, r.data)
```

`open_i2c(sda=None, scl=None, *, hz=100_000, pullups="pod")` takes channels or wiring names. A
transfer writes up to 128 bytes and reads up to 248; `read_reg` and `write_reg` take a register
as an int (one byte) or as bytes (a wider address). `scan(start=0x08, end=0x77)` lists the
addresses that answer. The session's `hz` is the rate the pod runs; a real bus runs somewhat
slower, because the master waits for SCL to read high (which lets a target stretch the clock), so
the SCL rise time adds to each period: with the pod's pull-ups on a short cable, 400 kHz measured
about 333 kHz and 1 MHz about 774 kHz. Stronger pull-ups bring it closer. `i2c.status()` is the
pod's view of the session.

SWD, SPI and I2C sessions share one engine in the pod, so they exclude each other. Every command
fits one cloud frame: this works over the LAN, USB and the cloud alike.

The pod has one I2C master for all its connections. While another connection has it open (the
desktop app's I2C tab, the web app, an AI agent or another script), `open_i2c()` raises a
`FirmwareError` that says so and names that session's pins and rate; close the session there
first. If this `BenchPod` still has its own session open, the error says to close that one.

## Motor & battery emulator

The BenchPod motor emulator boards (ECP5, up to four on one SPI link) are driven with
`bp.open_motor_emulator()`. The SPI pins come from the wiring profile, as do `PROGRAMN` (open
drain) and `DONE` (input) when it names them:

```python
from embeddedci.benchpod import BatteryModel

with bp.open_motor_emulator() as emu:
    emu.configure("emu.bit")                       # every board at once; waits for DONE
    print(emu.probe(0))                            # BoardInfo(version=7, ...)
    emu.set_protection(0, overcurrent_a=8, overvoltage_on_v=30, overvoltage_off_v=29)
    emu.set_battery(BatteryModel(capacity_ah=2.0, ocv=[25.2, 23.4, 22.2, 18.0], r0_ohm=0.02))
    emu.set_pwm(200_000)
    emu.arm(0)
    emu.set_control(0, pwm=True)
    print(emu.sample(0).phase_currents, emu.battery_state(0).soc)
    emu.start_log(["a", "bus"])
    log = emu.read_log()                           # whole sample sets, sequence-checked
```

Every register of the link protocol is reachable by name with `emu.read("SPEED")` and
`emu.write("KE", 1200, board=1)` (`motor_emulator.REGISTERS`); `broadcast=True` writes every
board. Units use `EmulatorCalibration` (the design values unless you pass a bench calibration).
Needs emulator gateware 0x0007 or later.

## UART

`rx` is the LA channel the pod **samples** (wire the DUT's TX here); `tx` is the channel the pod
**drives** (the DUT's RX). `until` is a substring, a compiled regex, or a `text -> bool` predicate.

```python
import re

from embeddedci.benchpod import INTERNAL, PIN4, PIN5

# A fixed window that stops early on a match.
log = bp.capture_uart(rx=PIN5, tx=PIN4, baud=115200, duration=3.0, until="login:")
print(log.matched, log.lines)
assert "login:" in log

# Catch the boot banner: power off, schedule the power-on pod-side, capture across it.
boot = bp.power_cycle_and_capture(rx=PIN5, tx=PIN4, efuse=INTERNAL,
                                  delay=1.0, duration=6.0, until=re.compile(r"APP_OK"))
assert boot.match(r"chip id match=0x58")
```

`power_cycle_and_capture` powers the eFuse off, waits `off_settle` seconds, schedules the power-on
`delay` seconds out with a **pod-side timer**, then captures for `duration` (which must exceed
`delay`) — so the power-on and the boot output land inside the window. It works on every transport,
including USB, where the UART proxy owns the link for the whole capture.

For an interactive session, `open_uart` starts a background reader that buffers from the moment it
opens, so you can listen *before* acting and write to the DUT's console:

```python
with bp.open_uart(rx=PIN5, tx=PIN4, baud=115200) as uart:
    bp.power_on(INTERNAL)                    # immediate: the session is already buffering
    uart.expect("APP_OK", timeout=6)         # raises UartTimeout (with .text) on timeout
    uart.write("status\r\n")
    uptime = uart.expect(re.compile(r"uptime=(\d+)"), timeout=2).group(1)   # expect returns the match
    reply = uart.read_until("> ", timeout=2)    # the output up to the next prompt; None on timeout
    rest = uart.read()                          # whatever arrived since
```

Reading works like a console. `read(timeout=)` returns the output since the last read (waiting up to
`timeout` when there is none), and `read_until` / `expect` mark the output read up to the end of
their match, so the next call only sees newer output. `text` and `lines` keep everything received;
`closed` says the session ended, and `overflowed` is set when more than `max_buffer` characters
arrived and the oldest were dropped. Running commands such as `power_on` while a session is open needs a TCP or
cloud connection.

**Frame format.** The UART is 8N1 unless you pass `parity` (`"none"`, `"even"`, `"odd"`) or
`stop_bits` (`1`, `2`) to `open_uart`, `capture_uart` or `power_cycle_and_capture`:

```python
with bp.open_uart(rx=PIN5, tx=PIN4, baud=19200, parity="even") as uart:   # 8E1
    uart.write(request)
```

Anything but 8N1 needs firmware 3.10.8+ (gateware v56, `bp.supports("uart_frame")`); on older
firmware the call raises `UnsupportedFeatureError` before anything is sent. The USB console
(`usb` falling back to the serial console) is 8N1 only. The pod checks the parity bit and the first
stop bit of what it receives; a byte with a wrong parity bit is still delivered, and
`bp.status()["uart_rx_parity_error"]` turns true for the session.

No pod has an RS-485 port, and boards before the digital v4 have no 5 V I/O: a 5 V UART needs a
level shifter there, and Modbus RTU on RS-485 an external RS-485 transceiver powered at 3.3 V (or a
5 V transceiver behind a level shifter). The digital v4 has [5 V I/O](https://embeddedci.com/docs/benchpod-targets/5v-rs485#5v-io).

**One wire (half duplex).** Some parts talk UART on a single pin, for example a boot ROM's
programming pin. `half_duplex=True` runs the session on `rx` alone (`tx` defaults to the same
channel): the pod only pulls the line low and lets the pull-up hold it high, so the device answers
on the same wire, and the pod does not read back its own bytes. Use LA1 to LA6, whose pull-up the
pod turns on for the session, or a pull-up on the board.

**Boot-mode entry.** A boot ROM that picks its mode from the UART pin at reset needs the pin held
low while the chip leaves reset, then a byte soon after. `entry=UartEntry(...)` has the pod run that
sequence before the session starts, timed on the pod so no network delay sits in between: TX
low (`tx_low`), reset for `reset_ms`, release, wait `hold_ms`, release TX, wait `idle_ms`, then send
`data` (at most 32 bytes). Times are whole milliseconds, 0 to 1000:

```python
from embeddedci.benchpod import UartEntry

entry = UartEntry(reset_ms=5, hold_ms=3, idle_ms=2, data=b"\x00")
with bp.open_uart(rx=PIN3, half_duplex=True, entry=entry, encoding="latin-1") as uart:
    reply = uart.read(timeout=0.5)
```

Both need gateware v57 (`bp.supports("uart_hd")`), and `entry` also the pod's reset pin
(`bp.supports("nrst_pin")`); older pods raise `UnsupportedFeatureError` before anything is sent.
The USB console has neither.

**Binary protocols.** A session decodes what it receives as UTF-8, which suits a console. For bytes
that are not text (a binary protocol's frames), open it with `encoding="latin-1"`: every byte is one
character, so `uart.read().encode("latin-1")` gives the bytes back unchanged, and a `bytes` write is
sent as is. For the adapter wiring and a Modbus RTU test through a transceiver, see
[5 V logic and RS-485](https://www.embeddedci.com/docs/benchpod-targets/5v-rs485).

**RS-485 direction (DE).** A half-duplex RS-485 transceiver sends while its DE (tied to /RE) is
high and listens while it is low. `de=` names the LA channel wired to DE, and the pod switches it
itself: high from before the first start bit of what it sends to the end of the last stop bit, low
otherwise. Nothing on the host times it, so a device that answers within a millisecond is still
heard (switching DE with `gpio` calls from the host takes tens of milliseconds over the network,
and the reply of a fast device is lost meanwhile). `de_active_low=True` inverts it:

```python
with bp.open_uart(rx=PIN5, tx=PIN4, de=PIN9, baud=19200, parity="even",
                  encoding="latin-1") as uart:
    uart.write(request)                  # DE goes high and low with the bytes
    reply = uart.read(timeout=0.2)
```

`de` needs gateware v58 (`bp.supports("uart_de")`); older pods raise `UnsupportedFeatureError`
before anything is sent. The USB console has no DE.

## LA pull resistors

You name the direction, `"up"`, `"down"` or `"off"`; the pod picks the resistor and reports its
value. Which channels have one and which way each can pull depends on the board, and
`PullState.supported` says it per channel. On today's boards each resistor pulls one way only:

| Channels | Pulls | Value |
|---|---|---|
| LA1, LA2 | up | 4.7k |
| LA3, LA4 | up | 2.2k |
| LA5, LA6 | up | 10k |
| LA7, LA8 | **down** | 10k |
| LA9–LA14 | none | — |

The resistors are referenced to 3V3, so the pod won't engage one while the LA bank is at 1.8 V
(`PullState.available` is false).

**Digital board v4** (`bp.supports("la_pull_strength")`): every channel, LA1–LA14, pulls up or
down with a 10k, a 2.2k or both (about 1.8k). You still name only the direction: the pod picks the
strength from the pin's function (2.2k for I2C, one-wire UART, open drain, and pod outputs in 5 V
mode; 10k otherwise), and `PullState.strength` says which (`"10k"`, `"2.2k"`, `"both"`, `"none"`
when off). The pull-ups go to J1 pin 21, so they work at 1.8, 3.3 and 5 V. While pin 21 is down
(shorted or held down) the pod turns every pull off and refuses new ones with `BP0029`; when the
pod switches the DUT's power off, the pull-ups stay off until a rail is on again or you set a pull.
While the pod holds a pull off like this, `PullState.enabled` and `state` say what is on the wire
and `PullState.held` says why (`"pin21_down"`, `"dut_off"` or `"fault"`; `None` otherwise). The pod
puts the pull back by itself (firmware after 3.10.12).

```python
from embeddedci.benchpod import PIN1, PIN2, PIN7

bp.set_pull(PIN1, "up")            # PullState(la=1, enabled=True, direction='up', ohms='4.7k', supported=('up',), ...)
bp.set_pull(PIN7, "down")
bp.set_pull(PIN7, "off")           # None is off too; "off" works on every channel
print(bp.pull_state(PIN1).state)   # 'up' ('up', 'down' or 'off')
print(bp.enabled_pulls())          # [1]
bp.enable_pullup(PIN1, PIN2)       # shortcuts: set_pull(la, "up") ...
bp.enable_pulldown(PIN7)           # ... and set_pull(la, "down")
bp.disable_pullup(PIN1, PIN2)      # off, on a channel that pulls up
```

A direction the channel can't pull raises `PullDirectionError` (a `FirmwareError` and a
`ValueError`) with code `BP0024`, "LA7 supports pull-down only on this board", and `"up"` or `"down"`
on a channel without a resistor raises it with `BP0023`. `.la`, `.want` and `.have` name the channel
and the directions. The boolean form (`set_pull(la, True)`, `enabled=`) is gone in 4.0: pass the
direction. The shortcuts `enable_pullup`, `disable_pullup`, `enable_pulldown` and
`disable_pulldown` stay.

An open-drain bus such as I2C must sit on LA1–LA6.

## GPIO on the LA pins

Any LA channel can be a GPIO: an input, a push-pull output or an open-drain output, driven at the LA
I/O voltage through the pod's 330 Ω series resistor. These are the pod's LA pins, not microcontroller
GPIOs.

```python
from embeddedci.benchpod import PinConflictError

start = bp.gpio(9, "output")                  # push-pull, starts low
start.pulse(0.002, count=3)                   # FPGA-timed pulses; returns at once
irq = bp.gpio(10, "input")
assert irq.wait_for(1, timeout=1.0)           # polled from the host (ms resolution)
bus = bp.gpio(6, "open_drain")                # 0 pulls low, 1 releases (LA6's pull-up holds it high)
print(bp.pin_levels())                        # {1: 0, 2: 1, …} — live levels of every channel

reset = bp.signal("RESET_N")                  # a wiring-profile signal, honouring active_low
reset.configure()
reset.activate()

a, b = bp.gpio_pins([11, 12], "open_drain")   # claimed as a group: a conflict on either claims neither

bp.release_gpio(9, 10)                        # back to high-Z; no arguments releases every GPIO pin
```

**One function per channel.** Each channel has exactly one owner at a time: `none` (the default — high-Z
and watched by captures), `gpio`, or a peripheral that claimed it — `uart_rx`/`uart_tx` (a UART session),
`swd_clk`/`swd_dio` (flashing), `sensor:<instance>` (an emulated sensor), `step`/`step_dir` (a pulse
train) and `spi_*` (an SPI session).
The pod refuses a second function with `PinConflictError` (`.la`, `.function`) instead of letting one
silently override the other, and the message names the SDK call that frees the channel:

```python
bp.gpio(4)
try:
    bp.open_uart(rx=3, tx=4)
except PinConflictError as exc:  # open_uart: BP0031: LA4 is in use by gpio; free LA4 first: bp.release_gpio(4) ...
    bp.release_gpio(exc.la)
```

`exc.function` and `exc.owner` say who holds it (`exc.instance` names an emulated sensor); the
message lists the ways to free a channel: `bp.release_gpio(la)`, `bp.sensors.stop("<instance>")`,
closing the session from `bp.open_uart()` or `bp.open_spi()`, or waiting for a flash or step train
to finish.

`bp.la_pins()` lists every channel's `LaPinState` (`function`, `gpio` mode, `level`, `pull`, `pull_on`, `pull_dirs`);
`bp.la_pins(la=[3, 4])` only those channels. A listing too large for one cloud reply (a long
sensor instance name on every channel) is read in parts by itself: in pages on firmware after
4.0.0 (`la_pins_paged`), else in two halves (firmware after 3.10.12),
and `bp.configure_gpio(channels, mode)` is the same group claim as `gpio_pins()` returning those states.
Captures observe every channel whatever its function. GPIO channels stay GPIO — also across
disconnects — until released (the pytest `benchpod` fixture releases them at session start and end),
and the LA voltage can't change while any channel is in use.

**Pin directions.** `bp.la_dir(la, dir)` tells the pod which way a pin's DUT signal goes:
`dut_input`, `dut_output`, `bidir` or `unknown` (the default after a reboot). On a `dut_output` pin
the pod refuses every push-pull driver (a GPIO `output`, UART TX, SPI SCK / MOSI / CS, SWD, ...)
with `DirectionConflictError` (`.la`), because two push-pull outputs would drive against each
other; open drain and inputs still work, and there is no override. A pin that is driven can't be
marked `dut_output` until it is released.

```python
from embeddedci.benchpod import DirectionConflictError

bp.la_dir([9, 10], "dut_output")
try:
    bp.gpio(9, "output")
except DirectionConflictError:                # use "open_drain", or fix the wiring
    bp.gpio(9, "open_drain")

print(bp.pin_directions_status)               # "sent": the wiring's directions went out on connect
print(bp.pin_directions())                    # {5: 'dut_output', 4: 'dut_input', 1: 'bidir', ...}
bp.apply_pin_directions()                     # send them again (raises on a conflict)
print(bp.la_dir())                            # every pin's LaPinState, dir included
```

**The wiring profile's directions are on by default.** The SDK sends them with `la_dir` when it
connects and whenever `bp.wiring` changes, and marks the pins the profile doesn't name `unknown`.
`bp.pin_directions_status` says how it went: `"sent"`, or `"not sent: <why>"` (for example a pin
the profile calls a DUT output that is driven right now; the other directions still go out).
The connection never fails over it. The pod forgets the directions on a reboot; the next connect
sends them again.

Every wired role and signal has a direction. The defaults: `uart_rx` and `spi_miso` are DUT
outputs (the DUT drives them); `uart_tx`, `spi_sclk`, `spi_mosi`, `spi_cs`, `swd_swclk`, `jtag_tck`, `jtag_tdi` and `jtag_trst` DUT
inputs; `jtag_tdo` a DUT output; the I2C pins, `swd_swdio` and `jtag_tms` `bidir`; a signal with direction `input` (the pod watches it)
is a DUT output, `output` a DUT input, `open_drain` and `bidir` are `bidir`. So a pin's direction
follows from what it is used for: set a signal's `direction` to change it. `wiring.dut_dir(name)` is
one role's or signal's direction, `wiring.pin_dut_dirs()` the per-channel map. Profiles from older
versions may carry `dut_dirs` (role key or signal name -> `dut_input`, `dut_output`, `bidir`), which
still overrides the defaults; the web and desktop editors no longer write it. Note the point of
view: `la_dir` and `dut_dirs` describe the DUT's signal, while a signal's `direction` is seen from the
pod.
`LaPinState.dir` is a pin's direction (`None` when unknown), and `LaPinState.warn` holds warning
bits that `LaPinState.warnings` spells out: a pull-up under a push-pull output, and a pin driven high
or pulled up while the target power is off (it powers an unpowered DUT through its I/O pins).

**Bias resistors must suit the function** — the pod refuses the combination with `PullConflictError`,
whichever you set first:

| Function | Pull-up (LA1-LA6) | Pull-down (LA7, LA8) |
|---|---|---|
| none, gpio input, gpio output, swd_clk, step | ok | ok |
| gpio open_drain | ok (it needs one, or an external pull-up) | refused — a released line would read low |
| uart_rx, uart_tx | ok | refused — the line idles high |
| sensor:<instance> on an I2C bus | ok (needed) | refused — the bus needs pull-ups |
| swd_dio | ok | refused |

A `pulse()` or `la_step()` on a GPIO output keeps the channel GPIO (its level returns afterwards); on a
free channel the train owns it only while it runs. `pin_levels()` reads the pins directly on gateware
v35+ and falls back to a short capture on older gateware. GPIO needs pod firmware that advertises
`Capabilities.la_pins`.

## GPIO expanders

Need more GPIOs than the 14 LA channels, for LEDs, buttons, relays or a keypad? Wire an
off-the-shelf I2C or SPI expander breakout to a few LA channels and attach it with
`bp.expansion(...)`. Its pins become `EXT1`..`EXTn`, and the GPIO calls above take them next to the
LA channels:

```python
bp.expansion("tca9555", addr=0x20, sda=1, scl=2)   # probes the chip: EXT1..EXT16, all inputs
led = bp.gpio("EXT1", "output")                    # an ExtPin: set(), high(), low(), read(), ...
led.high()
bp.gpio_pins(["EXT2", "EXT3", "EXT4"], "output")
bp.set_gpio(["EXT2", "EXT3", "EXT4"], [1, 0, 1])   # one level per pin: one bus transaction
assert bp.wait_for_level("EXT9", 1, timeout=2.0)   # polled over the bus
print(bp.expansion())                              # chip, bus pins, every pin's mode and level
bp.expansion(None)                                 # every EXT pin back to input; bus released
```

`expansion(chip="", *, addr=None, sda=None, scl=None, spi=None, load=None, count=None, hz=None,
pullups=None, names=None)` is the only call for expanders: with a chip name it attaches (checks the
LA voltage is one the chip runs at, probes it, puts every pin in input, names the pins), with no
arguments it returns the `ExpansionStatus`, and `expansion(None)` detaches.

- `addr` is the chip's 7-bit I2C address (default: its first, 0x20), or a list of addresses for
  several chips on one bus: `addr=[0x20, 0x21]` gives `EXT1`..`EXT32`, numbered in that order.
- `sda` and `scl` default to the wiring profile's `i2c_sda` and `i2c_scl`, so the expander can sit
  on the DUT's I2C bus.
- `pullups`: `"pod"` switches on the pod's pull-ups (LA1-LA6 only; they pull to the LA voltage)
  and `"external"` relies on the breakout's. By default `"pod"` when both pins are on LA1-LA6 and
  the pod has pull-ups at this LA voltage (1.8 V and 3.3 V), else `"external"`.
- `hz` defaults to 400 kHz for I2C chips (100 kHz for the PCF8574) and 1 MHz for SPI chips.
- SPI chips take `spi=(sck, mosi, miso, cs)` (or a dict with those keys) instead of `sda`/`scl`;
  omitted, the wiring profile's `spi_sclk`, `spi_mosi`, `spi_miso` and `spi_cs`. The pod's SPI
  master always takes four LA channels, so a 74HC595 chain's MISO and a 74HC165 chain's MOSI are
  free pins with nothing connected. Several MCP23S17s share one CS by hardware address:
  `addr=[0, 1]`.
- Shift-register chains take `count`, the chips in the chain (1..32): `EXT1` is the first chip's
  QA (74HC595: the chip MOSI feeds) or A (74HC165: the chip whose QH drives MISO). A 74HC595's CS
  goes to RCLK, which latches the outputs as CS rises. A 74HC165 needs `load`, the LA channel on
  SH/LD, which the SDK claims as a GPIO output and pulses low before each read; its CS goes to
  CLK INH.
- `names` names the pins: `{"EXT1": "LED1", "EXT9": "BUTTON"}` or a list in EXT order. A name works
  wherever `"EXTn"` does, including `bp.signal("BUTTON")`.

Power the breakout from the LA voltage: an expander on 5 V does not see 3.3 V as a reliable high.

Omitted arguments also come from an `expansion` object in the wiring profile, which the SDK keeps as
it is:

```json
{"i2c_sda": 1, "i2c_scl": 2,
 "expansion": {"chip": "tca9555", "addr": "0x20", "names": {"EXT1": "LED1", "EXT2": "LED2"}}}
```

What EXT pins do:

- `gpio`, `gpio_pins`, `configure_gpio`, `set_gpio`, `read_gpio`, `wait_for_level`, `release_gpio`
  and `signal` take EXT pins and their names. `release_gpio()` with no arguments also releases
  every EXT pin. A mixed list (`[5, "EXT1"]`) sets the LA channels first, then the EXT pins.
- `set_gpio(pins, level)` takes one level, or a list with one level per pin (LA channels too). EXT
  pins on one chip are written in one bus transaction, and only the registers that changed.
- `open_drain` on an EXT pin switches the pin between output low (0) and input (1).
- An EXT pin is one bus transaction per call, a few milliseconds over the LAN, not FPGA-timed:
  `pulse()`, `la_step()`, capture triggers, a capture's `send` and `la_dir()` refuse EXT pins
  with an `ExpansionError` that says why. `pin_levels()` stays LA-only; `expansion()` has the EXT
  levels.

The bus: SWD, SPI and the I2C master share one engine in the pod. The SDK opens the expander's bus
on the first EXT access and keeps it open; it closes it on its own before `flash()`,
`spi_flash()`, `open_i2c()`, `open_spi()` and `close()`, and opens it again when an EXT pin is next
used. An `open_i2c()` session on the same two pins is used as it is, so the DUT and the expander
share one bus; on other pins, or while an `open_spi()` session is open, EXT calls raise
`ExpansionError` naming the session to close. The expander runs on that engine, so from gateware v65
it needs the loop or deep-replay image: `expansion()` and EXT access raise
`UnsupportedFeatureError` (`BP0059`) while emulated sensors run, and switch the pod to the loop
image when none do.

`EXPANSION_CHIPS` lists every chip the SDK drives as `ExpansionChip` entries (`name`, `bus`,
`pins` per chip, `la_pins` it uses, `addrs`, `voltages`, `directions`, `max_hz`, `since`,
`aliases`, `note`, `chained`); `expansion_chip(name)` looks one up by name or alias.

| Chip | Bus | LA pins | Pins gained | LA voltage | Directions |
|---|---|---|---|---|---|
| `tca9555` (also `tca9535`) | I2C 0x20-0x27 | 2 | 16 | 1.8 V, 3.3 V | in/out |
| `pca9555` (also `pca9535`) | I2C 0x20-0x27 | 2 | 16 | 3.3 V | in/out |
| `mcp23017` | I2C 0x20-0x27 | 2 | 16 | 1.8 V, 3.3 V | in/out |
| `pcf8574` | I2C 0x20-0x27 | 2 | 8 | 3.3 V | quasi |
| `pcf8574a` | I2C 0x38-0x3f | 2 | 8 | 3.3 V | quasi |
| `pcf8575` | I2C 0x20-0x27 | 2 | 16 | 3.3 V | quasi |
| `tca6416a` | I2C 0x20-0x21 | 2 | 16 | 1.8 V, 3.3 V | in/out |
| `tca6424a` | I2C 0x22-0x23 | 2 | 24 | 1.8 V, 3.3 V | in/out |
| `mcp23s17` | SPI, hardware address 0-7 | 4 | 16 | 1.8 V, 3.3 V | in/out |
| `74hc595` | SPI chain (CS = latch) | 4 | 8 per chip | 3.3 V | out |
| `74hc165` | SPI chain + `load` | 5 | 8 per chip | 3.3 V | in |

Directions: `in/out` pins are inputs, push-pull outputs or open drain. `quasi` (PCF857x) pins
have no push-pull high: a high output is a weak pull-up (about 100 µA) that can't drive a load, and
an input is a high output that something else pulls low. A 74HC595 has outputs only (released is
low, and `read_gpio` returns the last level written); a 74HC165 has inputs only. Shift registers
don't answer, so attaching one can't probe it.

`ExpansionStatus`: `attached`, `chip`, `bus`, `la` (the LA channels it uses, `{"sda": 1, "scl": 2}`),
`addrs`, `count`, `hz`, `pullups`, `pins` (an `ExtPinState` each), `notes`, and `names` (the pin
list, `["EXT1", ...]`). `ExtPinState`: `ext` (the n of EXTn), `pin` (`"EXT3"`), `name`, `chip_pin`
(the pin on the chip: `"P02"`, `"GPB0"`), `addr`, `mode` (`none`, `input`, `output`,
`open_drain`), `level` (the commanded level) and `live` (the level read from the chip). An
`ExtPin` is a `GpioPin` with `ext` and `pin`; its `la` is `None`.

## Emulated sensors

The pod can **play sensors** for the DUT: I2C, SPI and UART sensors on LA channels and analog
sensors on its analog outputs. What a sensor does is not built into the firmware: the SDK sends the
pod a **sensor definition** (YAML or JSON, the format of the public
[sensor library](https://github.com/embeddedci-com/benchpod-sensors), `docs/format.md`) and the pod
compiles and plays it. So firmware that probes a sensor can be tested with it present, absent,
reporting chosen values, following a curve over time, or failing in a chosen way. Up to 4 I2C
sensors (on one bus or several), 1 SPI sensor, 1 UART sensor and 2 analog sensors (one per analog
output) run at once; each is an **instance** with a name you pick (1 to 16 characters of `a-z`,
`0-9` and `_`; the definition's `name` by default, so a longer definition name needs `instance=`). Every
pod with firmware 4.0 has it (`Capabilities.sensors`).

```python
from embeddedci.benchpod import PIN1, PIN2, i2c

bp.enable_pullup(PIN1, PIN2)                       # the open-drain bus must idle high
env = bp.sensors.start("bme280", pins={"sda": PIN2, "scl": PIN1}, address=0x76,
                       values={"temperature_c": 22.5, "humidity_pct": 40})
env.set(temperature_c=40.0)                        # the DUT's next read sees it
print(env.params)                                  # key, unit, min, max, default of each value
print(env.regs(0xD0, 1))                           # [0x60]: the register image the DUT reads
print(env.status())                                # slot, values, faults, bus counters (writes, last_write)
txns = env.i2c_transactions(4096)                  # wait for the DUT's next transaction, capture, decode
print(i2c.format_transactions(txns))
env.stop()                                         # pins released, definition forgotten
```

`i2c.timed_transactions(la.words, sda_ch, scl_ch, la.sample_rate_hz)` decodes an LA capture into
transactions with the time of each START, address, data byte, ACK and STOP in microseconds (dicts
in the shape the EmbeddedCI capture view draws), for placing decoded bytes on a waveform.

`bp.sensors` is a `SensorEmulation`; `start()` returns a `Sensor` handle (also a context manager
that stops the instance). I2C, SPI and UART sensors need the pod's emulation FPGA image: `start()`
loads it when another image runs (about 2 s; `sensor.started["image_loaded"]`), and while it
runs the DAC control loop and deep DAC replay are unavailable (`sensor.started["unavailable"]`).
The pod does not switch back after `stop()`: `control_loop()` and the deep replays do it once no
I2C, SPI or UART sensor runs, and raise a `BenchPodError` naming `bp.sensors.stop()` while one does.
Set the LA voltage first, and the pull-ups for I2C.

What the pod's emulation supports: UART 8N1 only; the SPI target has no data-ready pin; one
sensor with a `fifo` at a time; conversion and command times of at most 65.535 ms (a longer one
is cut to that), at most 4 distinct ones per I2C sensor; one analog instance per output, and no
`excitation: adc` yet; `bus_stuck` lasts at most 1 s. The pod refuses the rest with a clear
message.

| `SensorEmulation` | Does |
|---|---|
| `start(definition, *, instance=None, pins=None, address=None, baud=None, values=None)` | define and start; returns a `Sensor` |
| `define(definition, *, instance=None, dry_run=False)` | upload and compile only (chunked `sensor_define`); returns the pod's compile report |
| `validate(definition)` | compile on the pod and keep nothing: the report, or a `FirmwareError` naming the place, e.g. `registers[4].s16be: unknown name 'ax' at 17` |
| `sensor(instance)` | a `Sensor` handle for an instance that already runs |
| `set(instance, values=None, **values)` | change readings; a value outside its param's range is refused with the range |
| `fault(instance, type, *, duration_s=None, **fields)`, `clear_fault(instance)` | inject a fault now (`stuck`, `offset`, `gain`, `no_response`, `corrupt`, `bus_stuck`, `garbage`, `drop`, `baud_error`, `open`, `short`; fields `param`, `value`, `at`, `mask`, `rate_hz`); without `duration_s` it lasts until cleared |
| `load_stimulus(stimulus, *, base_dir=None)`, `start_stimulus(at_s=None)`, `stop_stimulus()` | values over time (below) |
| `stop(instance=None)` | stop one instance, or all of them; returns the names the pod stopped |
| `status(instance=None)` | `image`, `stimulus` (`loaded`, `running`, `t_s`) and `instances` with their `slot` (`i2c0`..`i2c3`, `spi0`, `uart0`, `analog0` the DAC, `analog1` the 4-20 mA output), `pins`, `values`, `faults` (objects with `type` and `left_s`, `None` until cleared) and `bus` (`writes`, `last_write`, `log_lost`...; `transfers` is always `None`: the gateware counts written bytes); every page the pod sends, as one |
| `running_digital()` | the I2C, SPI and UART instances that run (they hold the emulation image) |
| `regs(instance, *, start=0, length=None)` | the bytes an I2C/SPI instance answers now (the DUT's own writes included): `start` 0..255 in its 256-byte image, to its end by default |
| `capture(instance, samples=4096, sample_rate_hz=500000, *, timeout_s=2.0, wait_for_traffic=True)`, `i2c_transactions(...)` (same arguments) | a raw capture of an I2C instance's bus pins (`sensor_la`, 1..4096 packed bytes of four samples), and that capture decoded. It waits up to `timeout_s` for the DUT's next transaction and starts on its START, with 1/8 of the samples from before it (`TriggerTimeout` when the bus stays quiet); `wait_for_traffic=False` captures at once. Firmware without the `sensor_la_trigger` capability (3.10.3 and earlier) always captures at once. SPI and UART sensors: `la_capture()` their pins |
| `library(version=None, *, offline=False)`, `library_version` | the sensor library used for names such as `"bme280"` |

The `Sensor` handle has `instance`, `kind`, `name`, `params`, `info` (the compile report: `label`,
`program_bytes`, the bus settings), `started` (the start reply: `slot`, `image`) and the per-instance
methods `set()`, `fault()`, `clear_fault()`, `status()`, `regs()`, `capture()`,
`i2c_transactions()` and `stop()`.

**Pins** map roles to LA channels or wiring names: I2C `sda`, `scl` (default: the profile's
`i2c_sda`/`i2c_scl`) and an optional `drdy`; SPI `sclk`, `mosi`, `miso`, `cs` (default: the
profile's `spi_*`; the emulated SPI target has no data-ready pin); UART `tx` (pod to DUT) and an optional `rx` (DUT to pod); analog
`output`, `"dac"` (default) or `"current"` (the 4-20 mA output). The SDK sends LA channels: it
resolves wiring names itself. Several I2C instances may share
`sda`/`scl` when their addresses differ. `la_pins()` shows an instance's channels as owner
`sensor:<instance>`; a pin conflict names `bp.sensors.stop("<instance>")`.

**Definitions** come from a library name (`"bme280"`, or its library path
`"sensors/i2c/bme280.yaml"`), a `.yaml`/`.json` file, a dict, or YAML/JSON text. Your own part is a
file next to your tests; copy a library definition (`bp.sensors.library().text("bmp280")`) and edit it:

```python
probe = bp.sensors.start("tests/sensors/my_adc.yaml", instance="adc", pins={"sda": 2, "scl": 1})
bp.sensors.validate("tests/sensors/my_adc.yaml")   # compile check without starting it
```

YAML is read with the YAML 1.2 rules (PyYAML when installed, the `yaml` extra, else a built-in
reader): only `true`/`false` are booleans, so a key such as `on:` stays the word "on". The SDK
checks the header before sending (`format: 1`, `kind`, `name`, `params`, at most 32 KB of JSON) and
raises `DefinitionError` (a `ValueError`) listing every problem; the pod checks the rest.
`load_definition(source)` and `load_stimulus(source)` return the dict the pod receives.

**Stimuli** play values over time, and **faults** at chosen moments. One stimulus is loaded at a
time and may drive several instances (`env.temperature_c`, or `temperature_c` when one runs):

```python
bp.sensors.load_stimulus("tests/stimuli/hot_afternoon.yaml")   # data `file:` CSVs are inlined
bp.sensors.start_stimulus()                                    # from t = 0 (or at_s=12.5)
env.fault("no_response", duration_s=2)                         # or a fault right now
bp.sensors.stop_stimulus()                                     # values hold where they are
```

A **GPS module** is the library's `gps_nmea` UART sensor: NMEA sentences (RMC VTG GGA GSA GSV GLL,
u-blox style, 9600 baud, 1 Hz) on the LA channel wired to the DUT's RX:

```python
gps = bp.sensors.start("gps_nmea", pins={"tx": 9}, values={
    "lat_deg": 50.8466, "lon_deg": 4.3528, "sats": 9, "utc_start": 1791462896})
gps.set(speed_kmh=36, course_deg=90)    # drive east: the position moves between fixes
gps.set(fix=0)                          # lose the lock
```

The `benchpod.i2c` decoder handles START / repeated START / STOP, R/W, per-byte ACK/NACK and the
"write register pointer, then read" pattern (`read_register`, `addressed`). It also decodes
`(scl, sda)` streams (`i2c.decode_samples`) and synthesizes traces (`i2c.synthesize`), so decode
logic can be tested without hardware. For a bus on arbitrary channels, use `capture_la` + `decode`
(below).

### The sensor library

The SDK pins a library version, `SENSOR_LIBRARY_VERSION` (`"0.1.0"`), and ships its files inside
the package, so the default works offline and never changes under a test suite. Pick another
version, or `"latest"`, with (first match wins) the `version` argument, `bp.sensors.library_version`,
the `--benchpod-sensor-library` pytest option (for `benchpod_sensors`), `BENCHPOD_SENSOR_LIBRARY`,
or `sensor_library` in benchpod-cli's `config.json`. Other versions are downloaded once from the
library's GitHub release archive and cached per version (`BENCHPOD_SENSOR_CACHE`, default
`<user cache>/embeddedci/benchpod-sensors`, shared with the CLI); `latest` is resolved from GitHub
once per process. Without a network the cache serves, then the bundled copy, with a logged warning
in `SensorLibrary.warning`.

```python
from embeddedci.benchpod import sensor_library

lib = sensor_library()                 # or sensor_library("latest"), sensor_library("0.2.0")
print(lib.version, lib.source)         # 0.1.0 bundled (or cache / download)
for e in lib.entries:                  # LibraryEntry: path, name, kind, label
    print(e.name, e.kind, e.label)
print(lib.text("sht4x"))               # the YAML, to copy and edit
doc = lib.load("sht4x")                # the dict
print(lib.format_doc()[:200])          # docs/format.md of that version
```

`SensorLibrary` also has `find(name)` (None when missing), `get(name)` (a `LibraryEntry`, or
`LibraryError`), `path(name)` and `root`; `LibraryEntry.to_dict()` gives its `library.json` row.
The `embeddedci.benchpod.sensors` module has the lower-level pieces (`upload_requests`,
`requested_version`, `latest_version`, `cached_versions`, `bundled_library`).

## Analog paths, DC output and single readings

A named analog path is one fully specified mux and relay state, defined in the firmware, applied in
one step:

| `analog_path(...)` | Effect |
|---|---|
| `"dac_3v3"`, `"dac_5v"`, `"dac_12v"` | route the DAC to that output (`dac_12v` is the bipolar ±12 V output) |
| `"adc_ext"` | connect the ADC to the front SMA |
| `"cal1"`, `"cal2"` | loop the 5 V / 12 V DAC output back into the ADC |
| `"current_in"` | read the 4-20 mA measurement terminal |
| `"current_out"` | the 4-20 mA output: switch the DAC voltage outputs off |
| `"off"` | park everything |

```python
out = bp.dac_output("5v", volts=2.5)       # DacOutput(path, voltage, code): voltage actually produced
loop = bp.adc_read("cal1")                 # AdcReading(source, voltage, count, span)
print(out.voltage, loop.voltage, loop.span)
bp.dac_output("off")

state = bp.analog_path("adc_ext")          # AnalogPathState(path, dac_mux_register, cal_relay_register)
print(bp.adc_read("ext").voltage)          # the true front-SMA voltage (the ÷12 divider is applied)
```

`dac_output` takes calibrated volts (`"3v3"`, `"5v"`, `"12v"`, `"cal1"`, or `"off"`); without `volts` it only
routes. `"cal1"` (firmware with feature `dac_out_cal1`, features table revision 20) sets the level on
the 5 V DAC stage routed into the ADC loopback, so `bp.adc_read("cal1")` reads it back without relays
switching between steps; the 5 V output is disconnected while it is on. `generate(dac_path="cal1")`
works the same way, and older firmware is refused with `UnsupportedFeatureError`. `replay` does not take `"cal1"`. From firmware 3.8.1 the pod drives the DAC at its full 16 bits: `voltage` then has
microvolt resolution (about 76 µV a step on `"5v"`) and `code16` is the 16-bit code, while `code`
stays the nearest 8-bit level. On older firmware `code16` is `None` and the steps are 8-bit. `adc_read` routes the source (`"ext"`, `"cal1"`, `"cal2"`, `"current_in"`) and returns one averaged,
calibrated reading; the pod refuses it while the input is still moving (e.g. a DAC left running).

### The ADC input range (analog module v3r4)

Analog module v3r4 (firmware 3.9.5+, `Capabilities.adc_low_range`) adds a ÷2 low range to the
ADC input next to the normal ÷12 one:

| Range | Full scale | Resolution | Input impedance |
|---|---|---|---|
| `"normal"` (default) | ±32.8 V | ~1 mV per count | ~1 MΩ |
| `"low"` | ±5.46 V | ~167 µV per count | 1.82 MΩ: use 1x leads, not 10x probes |

```python
st = bp.adc_range("low")                   # AdcRangeState(range, supported, fullscale, ...)
r = bp.adc_read("ext")                      # read in the low range: r.range == "low"
r = bp.adc_read("current_in", range="low")  # or switch it with the reading
cap = bp.capture_adc(4096, source="ext", range="low")
print(cap.range, cap.overrange)             # a clipped sample: capture again in the normal range
bp.adc_range("normal")
print(bp.adc_range_test().passed)           # self-test through the cal1 loopback (5 V output)
```

The range applies to `ext`, `cal1` and `current_in` (4-20 mA fits inside it); `cal2` has none.
It stays until it is changed, `analog_path("off")` or `analog_path("cal2")`, or the pod restarts.
`analog_path`, `adc_read` and `capture_adc` take `range=` too. Captures are raw counts: the SDK
keeps the fit it scales them with on the range in use (`Capabilities.adc_range`), and re-reads it
whenever a reply says the pod moved to the other range. `AdcReading.overrange` and
`Capture.overrange` (with `clipped_samples`) flag samples on the ADC's rails. A board without the
low range raises `BenchPodError` for `"low"`.

`adc_range_test()` drives the 5 V output into `cal1` at 1 V and 3.5 V, reads each point in both
ranges, and fails (`passed` is `False`) when they disagree by more than 5 % + 30 mV. The low range's
gain is calibrated through `cal1` with `calibrate_adc_gain("cal1", range="low")` (after the offset and
the 5 V DMM line, see below); it serves every source in the low range and shows as
`Calibration.adc_low_gain_ppm`.

### Calibrating the ADC offset

`"current_in"` reads the 4-20 mA terminal (J8) across a 249 Ω resistor, so 4 mA is 0.996 V and 20 mA is
4.98 V. From analog module v4 (firmware 3.10.13+) the resistor is 100 Ω behind a series diode and
a 30 mA current limiter, which lowers what the pod takes from the loop to about 4.3 V at 20 mA:

| | Module up to v3r4 | Module v4 and later |
|---|---|---|
| 4 mA / 20 mA | 0.996 V / 4.98 V | 0.4 V / 2.0 V |
| Voltage taken from the loop at 20 mA | about 5 V | about 4.3 V |
| Range `current_in` reads in by default | the range in use | `low` |
| Resolution | about 4 µA per count | about 1.7 µA per count (low), 10 µA (normal) |
| Measurable current | 0 to about 21 mA in the low range, more in normal | 0 to the limiter, 28.5 to 31.5 mA |

On analog module v4, `adc_read("current_in")`, `analog_path("current_in")` and `capture_adc(source="current_in")`
without `range` switch the ADC to the low range; the next route of another source or a DAC output
gives the normal range back. A reading that sits near 30 mA whatever the loop does is the limiter.
A reversed loop reads 0 (the diode blocks it). `AdcReading.sense_ohms` and
`Calibration.sense_ohms` give the resistance the pod converts with (249, or 100 Ω in parallel with
the ADC input divider: 99.990 normal, 99.995 low); captures of `current_in` use it for `currents`.
Calibration is the same on both: J8 open is 0 V.

Each pod's analog input has its own offset, from part tolerances: anywhere from a few mV
to about 50 mV. With J8 open the terminal is at 0 V, so the pod can measure that offset itself and
keep it in its flash:

```python
cal = bp.calibrate()                       # disconnect J8 first. Calibration(source, calibrated, offset, a, b, ...)
print(cal.offset)                          # 0.004356: what the open input read, now taken out
print(bp.adc_read("current_in").voltage)   # 0.0 with J8 still open
print(bp.adc_read("current_in").current)   # loop current in amps: 0.004 to 0.020 for a live loop

bp.calibration()                           # what the pod has stored
bp.clear_calibration()                     # back to the built-in fit
```

Calibrate once per pod: it survives a reboot and a firmware update. The pod refuses
(`BenchPodError`) when something is driving J8 (more than 150 mV; 50 mV before firmware 3.9.3)
and keeps what it had. `a` and `b` are the fit the pod now uses for `current_in`
(`volts = a + b * count`).

Every ADC source shares the same front end after its input switch, so from firmware 3.9.3 the pod
takes the offset out of all of them: `adc_read` on `ext`, `cal1` and `cal2`, and `capture_adc`,
whose `volts` use the fit the pod reports (on the LAN in `status`, through embeddedci.com in its
capabilities). Older firmware corrects `current_in` only. Needs firmware 3.4.0 (capability
`calibrate`, `Capabilities.calibrate`). The web app has it in the pod's Settings tab, under
Calibration.

#### Calibrating with a DMM

With a bench DMM the pod can also take out what the offset leaves (firmware 3.9.4+, capability
`cal_dmm`, `Capabilities.cal_dmm`): its own DAC line per output, then the ADC gain per source.
Every step is optional and stored on its own; an output or source without one keeps the built-in
fit. Disconnect the DUT from the DAC outputs first: this drives up to ±5 V on them.

```python
points = []
for volts in bp.DMM_SET_POINTS["5v"]:            # 1.0 and 4.0 V; 3v3: 0.5 / 3.0, 12v: -5 / +5
    out = bp.dac_output("5v", volts=volts)
    points.append((out.code16, read_dmm()))      # the DMM on the 5 V output SMA, DC volts
cal = bp.calibrate_dac("5v", points)             # this pod's line for the 5 V output
bp.dac_output("off")
print(cal.dac["5v"])                             # DacLine(a, b, offset=-0.0031, gain_ppm=-996)

bp.calibrate_adc_gain("cal1")                    # 5 V loopback, against the line just stored
bp.calibrate_adc_gain("ext")                     # needs the 12v line and an SMA cable ±12 V -> ADC in
bp.clear_calibration("dac")                      # one part: "offset", "dac" or "adc"; no part = all
```

`calibrate_dac(path, points)` takes two `(code16, volts)` pairs: the 16-bit code `dac_output`
drove and the DMM reading. From then on `dac_output`, the calibrated `generate` and the DAC output
limits convert with this pod's line (`DacLine`: `a`, `b` per 16-bit code, `offset`, `gain_ppm`
against the built-in line). `store_adc_compensation(hf_dc_gain, c_top_pf)` records the ADC input's
measured frequency response on the pod (firmware 3.9.5+), read back as `Calibration.adc_comp`
(`AdcCompensation`), for
reference only: no reading is scaled with it. `calibrate_adc_gain(source)` measures `cal1` through the 5 V output,
`cal2` through the ±12 V output and `ext` (the front SMA, which `capture_adc` and the control loop
use) through an SMA cable from the ±12 V output to the ADC input; `current_in` follows `cal1`. It
needs the ADC offset and the driving output's DMM line, and `Calibration.measured` says what it
saw. The pod refuses (`BenchPodError`) and keeps what it had when a reading cannot be right: two
codes too close, a reading far from what the output should give (the DMM on another SMA), a gain
more than 2 % off. It also refuses while DAC output limits are set. `Calibration.dac`,
`Calibration.adc_gain_ppm` and `Calibration.dmm` show what is stored. The pod stores raw
measurements (the J8-open zero as `Calibration.zero_count`, the DMM points) and derives every fit
from them, so a later firmware with re-measured built-in fits keeps them right. Going back to
firmware 3.4.0 to 3.9.3 keeps the offset; the DMM parts are not used there.

`Calibration.stored` says where the calibration in use is kept (firmware 3.10.1+): `"module"`
when it is also in the analog module's EEPROM, `"pod"` when it is only in the pod's flash, `"none"`.

### The analog module (two-board BenchPod)

On a two-board BenchPod (digital v3r3 with the analog module on a ribbon cable), the module carries
an EEPROM with its identity and, from firmware 3.10.1, its calibration. The calibration belongs to
the analog front end, so it moves with the module: every calibration is saved on the pod and on the
module, and at boot the module's copy is used.

```python
m = bp.module()             # ModuleInfo(present, analog, power, bridge_power, eeprom, type, rev, serial, ...)
print(m.eeprom, m.serial)   # "ok" "am-3f9a1c07b2e4"; "blank" on a module never written
bp.set_module_id("am-3f9a1c07b2e4", "3.3")   # serial (up to 16 characters) and hardware revision
```

`ModuleInfo` has the live `present` flag, `analog` (the module is the pod's analog front end), the
two rails (`power`, `bridge_power`), the EEPROM state (`eeprom`: `"ok"`, `"blank"`, `"bad magic"`,
`"bad crc"`, `"bad length"`, `"none"`), and with `"ok"` the `type`, `rev` and `serial`, plus the stored
calibration blob's `cal_len` and `cal_ok`. `dac_pinout_ok` is `False` when the digital board and
the module use different flat-cable DAC pinouts (a digital v4 or later board needs analog module
3.5 or later): the DAC is then off and every command that drives it raises
`DacPinoutMismatchError` (`BP0080`). `set_module_id(serial, rev, module_type="analog")` writes
the identity and keeps the calibration. A serial names one module for life: a module that already
has another serial is refused unless `replace=True`. The revision (`"3.3"` for analog-v3r3) decides
features such as the ADC low range (3.4 and later) after the pod's next restart. Other boards answer
both with a `BenchPodError`.

### The 4-20 mA output

Terminal J9 is a two-wire 4-20 mA transmitter. `current_out` holds a current on it, in amps:

```python
out = bp.current_out(0.012)                # CurrentOutput(current, code, min_current, max_current)
print(out.current)                         # 0.012: the current actually held (16-bit, 0.25 µA steps)
print(out.min_current, out.max_current)    # 0.004055 0.020094 on a rev3 pod: what the output can do

bp.current_out(0.004)                      # back to the live zero
print(bp.current_out_range().max_current)  # the range only, nothing moves
```

- **It is loop powered.** J9 pin 1 (plus) goes to the plus of an external floating loop supply, pin 2
  (minus) through your receiver to the supply minus. Use 8 V plus 20 mA times the loop
  resistance, 30 V at most (24 V drives up to 800 Ω).
- **The loop must float.** Pin 2 is not pod ground, and nothing in the loop may touch pod
  ground: not the supply minus, and not a receiver that shares a ground with the pod (through
  SWD or UART wiring to the same target). If it does, the current is no longer regulated.
- **Do not wire J9 straight into J8.** The output cannot regulate that way.
- The pod cannot see the loop. `current_out` succeeds with the supply off or the loop open.
- The output cannot go below its live zero (a little above 4 mA) or above about 20.1 mA: there is no
  0 mA and no 21 mA level. `0.004` gives the live zero. Anything else outside the range raises
  `BenchPodError` with the pod's message. A value of 1 or more raises `ValueError` (amps, not mA).
- **The DAC is shared** with the 3.3 V / 5 V / ±12 V outputs. `current_out` switches those off
  first. `dac_output`, `generate` and `replay` also move the loop current. `dac_stop` leaves the
  loop where it was: call `current_out(0.004)` to go back to 4 mA.
- While DAC limits are set (an output stage), the pod refuses `current_out`.

Waveforms work on it too. `generate`, `replay` and `replay_waveform` take
`dac_path="current_out"`, and their levels are then amps:

```python
with bp.generate("sine", freq_hz=2, amplitude=0.006, offset=0.012, dac_path="current_out"):
    ...                                    # 6 to 18 mA; leaving the block returns the loop to 4 mA

ramp = [0.004 + 0.016 * i / 999 for i in range(1000)]
bp.replay(ramp, dac_path="current_out", sample_rate_hz=1000)   # 4 to 20 mA in 1 s, looping

loop = bp.capture_adc(4096, sample_rate_hz=100_000, source="current_in")
print(max(loop.currents))                  # a capture of J8 carries the loop current in amps
bp.replay(loop, dac_path="current_out")    # play the captured loop back on J9
```

- `generate` steps are about 0.25 µA (16-bit). `replay` is 16-bit too.
- Stopping the returned handle returns the loop to 4 mA. A bare `dac_stop()` does not.
- A capture or recording of a voltage has no current to reproduce: on `current_out` it needs
  `mapping="fit"`, which stretches its shape over the output's range. The same holds for a
  current recording (`Waveform.unit == "mA"`) on a voltage output.
- In the cloud library, `save_capture_as_recording` stores a capture of `current_in` as a current
  (`unit="mA"`), and `waveforms.save_segments(..., dac_path="current_out")` takes amps.
- Below 400 kS/s a capture of `current_in` reads about 10 to 20 µA higher than `adc_read`.
- The FPGA timing features work on the 4-20 mA terminals exactly as on the voltage ones, because
  they are the same DAC and ADC: `capture_correlated(..., source="current_in")` captures the loop
  current and the logic channels off one trigger, `on_capture=True` starts a current waveform at
  the capture's t0, `stop_dac_after` freezes it (the loop then holds that current), and
  `trigger=` starts a capture on an LA edge. There is one ADC and one DAC: a capture is the SMA
  voltage or the J8 current, and the output is a voltage or a current, never both at once.

The pod converts with a fit for its board revision (measured on one rev3 pod; the nominal part
values on v2), the same on every pod of that revision: the output has no per-pod calibration.
Expect a few tens of µA between pods. Needs firmware 3.4.0 or later (capability `current_out`, `Capabilities.current_out`).

## Captures

Captures return data with the scaling applied: an ADC `Capture` holds **calibrated volts** (the
same front-end model the web UI uses) next to the raw counts.

```python
from embeddedci.benchpod import decode

cap = bp.capture_adc(4096, sample_rate_hz=1_000_000, source="ext")
assert 3.2 < cap.mean() < 3.4                           # a 3.3 V rail
print(cap.sample_rate_hz, cap.duration, cap.peak_to_peak(), cap.rms_ac())
print(cap.dominant_frequency())                         # needs embeddedci[analysis]

long = bp.capture_adc(800_000, sample_rate_hz=400_000)  # 2 s: above 32768 samples it streams from PSRAM

la = bp.capture_la(8192, sample_rate_hz=1_000_000)
print(la.edges(5), la.channel(5)[:16])
txns = la.decode("i2c", sda=2, scl=1)
frames = bp.decode(la, "uart", rx=5, baud=115200)
print(decode.uart_text(frames))
words = la.decode("spi", sclk=1, mosi=2, miso=3, cs=4)

usb = bp.capture_la(480_000, sample_rate_hz=48_000_000)  # 10 ms of a full-speed bus
enum = usb.decode("usb", dp=7, dm=8)                     # speed from the idle state
for t in enum.transfers:                                  # control transfers: the enumeration
    print(t.request, t.status, t.detail)                  # GET_DESCRIPTOR DEVICE length 64 OK USB 2.00, ...

both = bp.capture_correlated(adc_samples=4096, adc_sample_rate_hz=400_000,
                             la_samples=4096, la_sample_rate_hz=1_000_000)
print(both.adc.duration, both.la.duration)              # one hardware trigger: timebases align
```

For small signals or readings near 0 V at low rates, `oversample=True` converts at 400 kS/s and
averages down to the requested rate:

```python
cap = bp.capture_adc(1000, sample_rate_hz=10_000, source="ext", oversample=True)
print(cap.oversample, cap.sample_rate_hz, cap.mean())   # 40 conversions per sample, at 10 kS/s
```

Measured on the `cal2` loopback at 10 kS/s, noise fell from about 0.87 to 0.13 counts RMS, and the
step in the ADC's readings at 0 V from about 4 counts to about 1.35. `volts` and `mean_counts` keep
the fraction, `counts` are rounded raw codes, and `clipped_conversions` (also `clipped_samples`)
counts conversions on a rail, since an average can hide one. Limits: the conversions must fit in
the pod's memory, 2,097,152 of them or about 5 s (more raises `BenchPodError`), the rate is the
nearest whole ratio of 400 kS/s (check `sample_rate_hz` on the result), and the bandwidth falls to
about 0.44 times the returned rate.


* `capture_adc(samples=4096, *, sample_rate_hz=None, source=None, trigger=None, trigger_timeout=10.0, range=None)` — omitted rate = the device
  maximum; the **achieved** rate is on the result. `source` routes the ADC first (omitted, the
  current routing is left alone); `range` sets the [ADC input range](#the-adc-input-range-analog-module-v3r4). `volts` use the front-SMA calibration; for the other sources
  compare `counts` or use `adc_read`.
* `capture_la(samples=4096, *, sample_rate_hz=None, stop_dac_after=None, trigger=None, trigger_timeout=10.0)` — 12-bit words; bit *n* is
  channel LA*n+1*. The **achieved** rate is on the result and can be below the request: the pod
  runs 24 MHz ÷ a whole divider (2.304 MHz runs at 2.18 MHz; ask for 12, 8, 6, 4.8, 4, 3, 2.4 or
  2 MHz to get it exactly), and slows a capture over 32768 samples until it fits the burst buffer
  (about 4 MS/s at 100 000 samples, 3 MS/s at 300 000). Older firmware did not report it, so
  `sample_rate_hz` was the requested rate there.
* `capture_correlated(...)` — ADC + LA from one trigger; set either count to 0 for a single stream.
* `bp.decode(source, protocol, **channels)` / `LaCapture.decode(...)` — off-device decoding of
  `i2c` (`sda`, `scl`), `uart` (`rx`, `baud`, optional `data_bits`, `parity`, `stop_bits`) and `spi`
  (`sclk`, optional `mosi`, `miso`, `cs`, `mode`, `bits`, `msb_first`). Results are
  `I2CTransaction`, `UartFrame` and `SpiFrame` lists. `usb` (`dp`, `dm`, optional `speed`:
  `full`, `low` or `auto`) returns a `UsbDecode`: `packets` (PID, address, endpoint, frame, data,
  CRC5/CRC16 checked, `error` per packet), `transactions` (token, data, handshake; repeated NAKs
  merged into `count`) and `transfers` (control transfers with the SETUP request and the
  descriptors read back decoded). Tap D+ and D- on two LA channels at the 3.3 V I/O level. Full
  speed needs `sample_rate_hz=48_000_000`; low speed decodes from 6 MS/s. High speed is not
  visible to the LA. `decode.decode_usb(words, dp=, dm=, sample_rate_hz=)` decodes raw words.
* More protocols through the same `bp.decode` / `LaCapture.decode`:

  | Protocol | Arguments | Result |
  |---|---|---|
  | `modbus` (RTU) | `rx`, `baud`; optional `tx` (two-line tap), `data_bits`, `parity` (default `even`), `stop_bits` | `ModbusFrame` list: `role` (request/response), `line` (the channel it was on), `addr`, `function_name`, `start`, `quantity`, `values`, `exception`, `summary`, `reply_to`, `response_us`, CRC checked |
  | `can` (CAN, CAN FD) | `can` (the transceiver's RXD), `bitrate`; optional `data_bitrate`, `sample_point`, `data_sample_point` | `CanFrame` list: `id_hex`, `extended`, `fd`, `brs`, `dlc`, `len`, `data_hex`, `ack`, `kind` (data/remote/error), CRC and stuffing checked |
  | `pwm` | `channels`; optional `servo` (read the high pulses as servo positions) | `PwmDecode`: per channel `periods` (period, frequency, high, low, duty), `freq_hz` / `duty` / `high_us` min, max, mean, `jitter_us`, `glitches`, `servo` (with `servo=True`) |
  | `onewire` | `onewire` | `OneWireTransaction` list: `reset`, `presence`, `overdrive`, `rom_command_name`, `rom`, `rom_crc_ok`, `function`, `bytes_hex`, `detail` (a temperature) |
  | `smbus`, `pmbus` | `sda`, `scl`; optional `pec` (`auto`, `on`, `off`) | `SmbusTransaction` list: `kind`, `command`, `command_name`, `data_hex`, `value` (PMBus: volts, amps, status bits), `pec` |

  Rates: CAN needs 6 samples per data bit (2 Mbit/s data: 12 MS/s), 1-Wire 1 MS/s (4 MS/s in
  overdrive). The modules `modbus_decode`, `can_decode`, `pwm_decode`, `onewire_decode` and
  `smbus_decode` hold the classes and the checksums (`modbus_crc`, `can_crc`, `crc8`, `pec8`,
  `linear11`).

| `Capture` | `LaCapture` |
|---|---|
| `counts`, `volts`, `sample_rate_hz`, `source`, `duration`, `times()` | `words`, `sample_rate_hz`, `channels`, `duration` |
| `mean()`, `min()`, `max()`, `peak_to_peak()`, `rms()`, `rms_ac()` | `channel(la)` → 0/1 list, `edges(la)` → transition count |
| `fft()` → `(freqs_hz, magnitude)`, `dominant_frequency()` — need `[analysis]` | `decode(protocol, **channels)` |
| `crossing_times(threshold, edge, hysteresis=)`, `first_crossing(...)` | `edge_times(la, edge)`, `first_edge(la, edge, after=)`, `level_at(la, t)`, `pulse_widths(la, level)`, `frequency(la)`, `duty_cycle(la)`, `delay(from_la, to_la, ...)` |

### Timing

Timestamps are seconds from the capture's first sample, with one-sample resolution
(`1 / sample_rate_hz`). `edge` is `"rising"`, `"falling"` or `"both"`.

```python
la = bp.capture_la(1_000_000, sample_rate_hz=1_000_000)          # 1 s at 1 µs resolution

latency = la.delay(9, 10)                  # trigger pin (LA9) rising → "result ready" (LA10) rising
assert latency is not None and latency < 0.050

print(la.edge_times(10, "rising")[:5])     # each result's timestamp
print(la.pulse_widths(10, level=1))        # complete high pulses only
print(la.frequency(11), la.duty_cycle(11)) # a PWM or frame-sync line

both = bp.capture_correlated(adc_samples=100_000, adc_sample_rate_hz=100_000,
                             la_samples=1_000_000, la_sample_rate_hz=1_000_000)
rail_up = both.adc.first_crossing(3.0, "rising", hysteresis=0.1)   # one trigger: same timebase
reset_released = both.la.first_edge(3, "rising")
```

`delay` returns `None` when either edge is missing; `first_edge` and `first_crossing` take `after=` to
skip earlier activity. `crossing_times` counts a crossing only once the signal clears
`threshold ± hysteresis / 2`, so noise on a slow edge counts once.

### Triggered captures

A `Trigger` makes the capture start at an event instead of when the command arrives: `rising` or
`falling` on an LA channel, or while it is `high`/`low`. t = 0 is then the trigger moment (and a DAC
co-trigger or `stop_dac_after` counts from it), so a delay measured from the trigger needs no
alignment:

```python
from embeddedci.benchpod import Trigger, TriggerTimeout

la = bp.capture_la(500_000, sample_rate_hz=1_000_000,
                   trigger=Trigger("TRIGGER", "rising"), trigger_timeout=5.0)
latency = la.first_edge(bp.wiring.la("READY"), "rising")        # seconds after the trigger

adc = bp.capture_adc(100_000, sample_rate_hz=100_000, trigger=Trigger(10, "falling"))
```

`capture_adc`, `capture_la` and `capture_correlated` take `trigger` and `trigger_timeout` (seconds, up
to 600). When the condition never happens the pod aborts the capture and raises `TriggerTimeout`
(`.la`, `.edge`). The trigger channel can have any function — triggers observe. Triggers need gateware
v35+ (`Capabilities.capture_trigger`).

`capture_la` can also return what happened before the trigger: `Trigger(..., pre_samples=N)` (gateware
v50+, any rate up to 48 MS/s). The capture's `pre_samples` says how many it holds (fewer when the
capture was armed only shortly before the trigger, or the signals changed very often before it), and
the trigger is at that sample index:

```python
la = bp.capture_la(20_000, sample_rate_hz=24_000_000,
                   trigger=Trigger("CS", "falling", pre_samples=2_000))
t0 = la.pre_samples / la.sample_rate_hz                          # the trigger, in seconds
```

Above 12 MS/s (gateware v50+) the pod records only the changes, at up to about 6 million per second
for all channels together. A signal that changes faster for long ends such a capture early: it then
holds the samples up to that point and `truncated` says why (`"busy"` or `"memory"`).

### Send and capture

`capture_la(..., send=...)` (gateware v55+) runs one transaction right after the capture arms, in the
same command, so a trigger on the bus line catches it. It works over the cloud too, which runs one
command at a time. Trigger on the line the transaction starts with: SDA falling (I2C START), CS
falling (SPI), or the GPIO pin's edge. `send` with an I2C or SPI transaction needs the bus master
the emulation image lacks from gateware v65: it raises `UnsupportedFeatureError` (`BP0059`) while
emulated sensors run and switches to the loop image when none do.

```python
from embeddedci.benchpod import GpioSend, I2CSend, SpiSend, Trigger

bp.enable_pullup(1, 2)
with bp.open_i2c(hz=400_000) as i2c:
    cap = bp.capture_la(8192, sample_rate_hz=12_000_000, trigger=Trigger("i2c_sda", "falling"),
                        send=i2c.send(0x76, b"\xd0", read=1))    # the same as I2CSend(0x76, b"\xd0", 1)
print(cap.send_result)                      # SendResult(bus='i2c', ack=True, data=b'X', ...)
print(bp.decode(cap, "i2c", sda=1, scl=2))  # the transfer as the bus saw it

with bp.open_spi(mode=1) as spi:
    cap = bp.capture_la(4096, trigger=Trigger("spi_cs", "falling"), send=SpiSend(b"\x9f\x00"))

bp.gpio(6, "output", level=1)
cap = bp.capture_la(1024, trigger=Trigger(6, "falling"), send=GpioSend(6, 0))
```

`I2CSend(addr, write=None, read=0)` needs an open `open_i2c` session and writes and reads up to 32
bytes; `SpiSend(tx)` needs an open `open_spi` session and sends 1-32 bytes in one CS frame;
`GpioSend(la, level)` sets a pin that is already a GPIO `output` or `open_drain`. The capture's
`send_result` is a `SendResult`: `bus`, `ack` and `nack_at` (I2C), `data` (the bytes read, or clocked
in on MISO), `la` and `level` (GPIO), `error` (`""`, or why the transfer failed, for example
`"SCL held low"`; the capture still returns) and `ok`. A send that does not validate (no session)
raises before anything is armed. With an edge trigger and no `pre_samples`, a send keeps 64 samples
from before the trigger, so the decoder sees the first START.

## DAC: generator, replay and faults

Every DAC output keeps running until stopped. The returned `DacHandle` / `ReplayHandle` is a
context manager that stops it on exit; `bp.dac_stop()` stops any output (generator, replay or
control loop). DAC paths are `"3v3"`, `"5v"` and `"12v"`. `generate`, `replay` and
`replay_waveform` (for a waveform stored without a path) default to `"3v3"`, the lowest voltage
output: pass the output your DUT pin tolerates, since `"5v"` and `"12v"` can drive more than a
3.3 V pin takes. With `route=False` nothing is switched and the path only scales the volts, so the
default is the output this connection last routed (`analog_path("cal1")` is the 5 V DAC,
`"cal2"` the 12 V one), else `"5v"`.

```python
from embeddedci.benchpod import Fault

# A parametric waveform, in volts: 0-3.3 V square wave that stops by itself after 5 s.
bp.generate("square", freq_hz=10, amplitude=1.65, offset=1.65, dac_path="3v3", duration=5.0)
```

The pod takes the volts (`amplitude_v`, `offset_v` on the wire, or microamps on `current_out`),
applies its own per-path calibration, routes the path (unless `route=False`) and plays the waveform
at the DAC's full 16 bits (about 50 µV a step on `3v3`); both peaks must be on the path (0 to 3.3 V
works: the pod clamps a peak just past an end it cannot quite reach).

```python
golden = bp.capture_adc(16_384, sample_rate_hz=100_000, source="ext")

with bp.replay(golden, dac_path="5v"):                   # loops until the block exits
    la = bp.capture_la(8192, sample_rate_hz=1_000_000)   # runs concurrently with the replay

ramp = [3.3 * i / 1000 for i in range(1000)]             # a list of volts
with bp.replay(ramp, dac_path="3v3", sample_rate_hz=10_000,
               fault=Fault("flatline", start=400, width=100)) as r:
    print(r.samples, r.sample_rate_hz, r.deep)
```

* `generate(waveform, *, freq_hz, amplitude, offset=None, dac_path=None, duration=None,
  sample_rate_hz=None, on_capture=False)` — `sine`, `square` or `sawtooth`. `amplitude` is the peak
  and `offset` the center (default: mid-range of the path). The firmware builds the waveform from
  8-bit levels, so volts are quantised to full-scale / 255.
* `replay(source, *, dac_path=None, mapping="faithful", sample_rate_hz=None, deep=None, fault=None,
  are_codes=False, on_capture=False)` — `source` is a `Capture` (its volts, at its own sample rate
  by default), a sequence of volts, or raw DAC codes with `are_codes=True`. `mapping="faithful"`
  reproduces the voltage (clipping outside the path's range), `"fit"` auto-scales. Above 2048 samples
  the replay streams from PSRAM (`deep`), which needs the deep-replay gateware image: the pod is
  switched to it automatically (see [Gateware images](#gateware-images)), and `switch_image=False`
  raises instead. Replay streams the waveform to the pod, so it needs a **TCP or cloud** connection.
* `Fault(type, start, width, level=None)` — `"flatline"`, `"spike"` or `"stuck"` spliced into the
  replay at sample indices; `level` is an optional raw DAC code.

### DAC ↔ capture co-trigger and auto-stop

For a deterministic stimulus → response run, phase-lock the DAC to the capture instead of racing
them from the host. `on_capture=True` arms the DAC to start on the next capture's hardware t0
(gateware v27+, `capabilities.dac_cotrig`); `stop_dac_after=` on a capture cuts the DAC that many
seconds after the same t0, sample-precise (gateware v21+):

```python
with bp.replay(golden, dac_path="5v", on_capture=True) as r:
    assert r.cotrig                                        # armed, not yet driving
    run = bp.capture_correlated(adc_samples=4096, adc_sample_rate_hz=400_000,
                                la_samples=4096, la_sample_rate_hz=1_000_000,
                                stop_dac_after=0.002)      # DAC starts at t0, stops at 2 ms
```

`generate(..., on_capture=True)` and `capture_la(..., stop_dac_after=...)` work the same way.

### Cloud waveform library

Save captures to the organisation's library and replay them later, on this pod or another:

```python
wf = bp.save_capture_as_recording(golden, "golden-startup")   # raw samples stored server-side

for w in bp.waveforms.list():
    print(w.id, w.name, w.kind, w.sample_count, w.sample_rate_hz)

with bp.replay_waveform(wf, dac_path="5v", mapping="faithful", window_start=0, window_len=8192):
    run = bp.capture_la(8192, sample_rate_hz=1_000_000)

bp.waveforms.delete(wf.id)
```

`bp.waveforms` is a `WaveformLibrary` (`list`, `get`, `find(name)`, `preview`, `save_waveform`,
`save_recording`, `save_segments`, `rename`, `delete`, `download_recording`). `replay_waveform`
takes a waveform id or `Waveform`; on a cloud connection it uses the server's replay DSP by default
(`server_side=`), otherwise it downloads the recording and applies the same DSP client-side before
streaming it over the device connection.

> **Server access.** The library and server-side replay need an embeddedci credential. Over the cloud
> destination the SDK reuses its credential (an API key, the `benchpod login` session, or GitHub Actions
> OIDC), so nothing extra is needed. On a LAN or USB connection pass an API key (`api_key=`,
> `--benchpod-api-key` or `BENCHPOD_API_KEY`). Captures and direct `replay(...)` never need one.

## Gateware images

The pod's FPGA runs one of three gateware images, all stored on the pod:

| Image | Adds | Capability flag |
|---|---|---|
| `FpgaImage.LOOP` (0) | the [in-fabric control loop](#in-fabric-dac-control-loop) | `dac_control_loop` |
| `FpgaImage.DEEP_REPLAY` (1) | replays longer than 2048 samples, streamed from PSRAM | `dac_deep_replay` |
| `FpgaImage.EMULATION` (2) | I2C, SPI and UART sensor emulation (gateware v54+, W25Q slot `gw2`) | none while it runs; `sensors_digital` says it is installed |

Everything else — captures, the generator, DC output, short replays, UART, CAN — works on all three.
I2C, SPI and UART sensor emulation runs only on the emulation image, which `bp.sensors.start()` loads by
itself (see [Emulated sensors](#emulated-sensors)).

**Switching is automatic.** `control_loop()`, `replay()` and `replay_waveform()` switch the pod to
the image they need (`switch_image=True`, the default), log a warning on the `embeddedci.benchpod`
logger, and record the switch on the returned handle as `switched_image` (an `FpgaImageInfo`, or
`None` when no switch was needed). Pass `switch_image=False` to get a `BenchPodError` instead, for a
test that must not disturb the pod. `replay_waveform` through the server switches only for a
recording that would otherwise be downsampled (longer than the shallow replay depth, with no
`target_samples`), and waits until the server sees the new image. From the emulation image (neither
flag, `sensors_digital` set; `fpga_image` says which runs) they switch back when no I2C, SPI or UART
sensor runs and raise naming `bp.sensors.stop()` otherwise: the pod never switches back by itself.
A pod that reports neither flag nor `sensors_digital` has a single image and is never switched.

**Bus masters.** From gateware v65 the emulation image has no SWD, JTAG, SPI master or I2C master
(they are in the loop and deep-replay images; before v65 every image has them).
`bp.has_bus_masters()` says whether the running image does (`None` when the pod does not say).
`flash()` over SWD or JTAG, `open_spi()`, `spi_flash()`, `open_i2c()`, `expansion()` and
`capture_la(send=I2CSend/SpiSend)` check it first:

* On the emulation image with no I2C, SPI or UART sensor running, the SDK switches the pod to the
  loop image (logged, ~3 s) and goes on. The pod's firmware does the same.
* With such a sensor running, they raise `UnsupportedFeatureError` (`BP0059`, `feature` names what
  was asked for): `bp.sensors.stop()` first, or do the flash and the bus work before
  `bp.sensors.start()`. Flashing first costs one image switch (the sensor start loads emulation).

What a switch does:

* It loads the image into the FPGA straight from its slot on the pod, which takes **about 0.1 s**
  (firmware 4.0.6+; older firmware wrote it to the config flash first, 2 to 3 s). Asking for the
  image that already runs does nothing.
* It **resets the FPGA**, so a running DAC output or control loop, an open UART session and I2C
  sensor emulation stop. Switch first, then start those — for example once in a session fixture:

  ```python
  if not bp.capabilities.dac_deep_replay:
      bp.fpga_image(FpgaImage.DEEP_REPLAY)
  ```

* After a restart or a power cycle the pod runs the loop image (firmware 4.0.6+; older firmware
  stayed on the last image switched to). Code that needs another image switches to it, as the
  methods above do.
* In pytest, `@pytest.mark.benchpod_capability("dac_control_loop")` (or `"dac_deep_replay"`)
  switches the image for the test instead of skipping it. `"spi_master"` and `"i2c_master"` switch
  from the emulation image to the loop image (gateware v65+) and skip the test while emulated
  sensors run.

## In-fabric DAC control loop

On the **loop gateware image** (switched to automatically — see [Gateware images](#gateware-images))
the iCE40 runs a control loop in fabric: each tick it takes an input,
looks it up in a reloadable curve (`out = curve[input]`), damps toward that target, clamps to
`[vmin, vmax]` and drives the DAC — no host in the loop. Any transfer function you can tabulate
works; a solar-panel I-V curve is one preset. Curves and inputs are 16-bit codes (0–65535).

**Start open-loop, with the ADC out of the path.** `source="fixed"` holds one point of the curve, so
the DAC and output stage can be metered on their own before trusting the loop (gateware v29+,
`capabilities.dac_loop_sources`):

```python
import time

from embeddedci.benchpod import build_panel_curve, curve_output_at, input_percent_to_code

curve = build_panel_curve(voc_code=52_000, sharpness=6)
# On the deep-replay image this first switches the pod to the loop image (~3 s).
with bp.control_loop(curve=curve, vmax=65_535, source="fixed", input_code=0) as loop:
    for pct in (0, 50, 100):
        code = input_percent_to_code(pct)
        loop.set_input(code)                       # no re-arm, no curve re-upload
        time.sleep(0.2)
        pt = loop.probe()                          # IVPoint(i, v, input_code, source)
        print(pct, pt.loop_input, pt.v, "expected", curve_output_at(curve, code))
```

Then `source="sweep"` (with `step=` ≥ 1) walks the curve as a function of time, and the default source
closes the loop around the live ADC:

```python
from embeddedci.benchpod import LoopInputMap

with bp.control_loop(curve=curve, vmax=52_000) as loop:        # input = live ADC
    print(loop.probe().loop_input, loop.probe().v)

# Gateware v30+ (capabilities.dac_loop_input_map): index the curve in engineering units of the
# sense chain instead of raw ADC counts — here mA through a 0.04 Ω shunt into a 50 V/V amplifier.
shunt = LoopInputMap(mv_per_unit=2.0, range_min=0.0, range_max=1000.0)
with bp.control_loop(curve=curve, input_map=shunt) as loop:
    print(loop.probe())
```

* Curves: `build_panel_curve(voc_code, sharpness=4.0, points=256)`, `build_constant_curve(value_code)`,
  `build_linear_curve(max_code, rising=True)`, or any sequence of codes; `curve_output_at(curve, code)`
  is what the hardware will drive for an input, and `input_percent_to_code(pct)` converts a
  percentage of the input range. `control_loop(voc_code=, sharpness=)` builds the panel curve for you.
* Loop parameters: `k` (Q15 damping, clamped to 1–32767), `vmin`/`vmax` (output clamp; `vmin > vmax`
  raises `ValueError`), `tick_div` (≥ 8).
* `bp.loop_input(...)` / `loop.set_input(...)` re-target a running loop and return a `LoopState`;
  `bp.loop_probe()` / `loop.probe()` return an `IVPoint` — assert against `loop_input`, which is what
  indexed the curve (in a fixed or sweep run the ADC reading `i` is not in the path).
* `loop.switched_image` is the `FpgaImageInfo` (`image`, `version`, `features`) of the switch
  `control_loop` made, or `None` if the pod was already on the loop image; `switch_image=False`
  raises instead of switching.

## CAN

The pod has one CAN node (FDCAN + TCAN1044 transceiver), classic CAN or CAN FD. `open_can`
configures it and returns a `CanBus`, which clears autonomous-responder rules, disables CAN and
switches the 120 Ω termination off on exit.

| `mode` | Behaviour |
|---|---|
| `"normal"` | a regular node; needs another node on the bus to ACK |
| `"internal"` | loopback inside the FDCAN core — self-test with nothing wired |
| `"external"` | loopback through the real transceiver pins — validates the transceiver; the bus must be idle |
| `"listen"` | receive only |

```python
with bp.open_can(bitrate=500_000, mode="internal") as can:
    can.write(0x123, [0xDE, 0xAD])
    frame = can.expect(0x123, timeout=1.0)          # raises CanTimeout (.frames) on timeout
    assert frame.data == b"\xde\xad"

with bp.open_can(bitrate=500_000, term=True) as can:        # on a real bus, 120 Ω termination in
    can.simulate_ecu({0x7DF: (0x7E8, [0x02, 0x41, 0x00])})  # the firmware replies from its RX ISR
    frames = can.collect(1.0, match=0x100)
    can.assert_periodic(0x100, period=0.1, tol=0.2)         # uses the pod's ISR timestamps
```

**CAN FD** (firmware 3.10.2+, `bp.supports("can_fd")`): pass `data_bitrate`. Frames then carry up
to 64 bytes, and with `data_bitrate` above `bitrate` their data phase switches to it (up to
8 Mbit/s; rates with exact timing such as 1, 2, 4, 5 and 8 Mbit/s). Frames on an FD bus are FD with
bit-rate switching unless you pass `fd=False` (a classic frame, for a classic node on the same bus)
or `brs=False`. An FD payload of another length than 0-8, 12, 16, 20, 24, 32, 48 or 64 bytes is
padded with zeros to the next one.

```python
with bp.open_can(bitrate=500_000, data_bitrate=2_000_000, mode="internal") as can:
    can.write(0x123, bytes(range(64)))
    frame = can.expect(0x123, timeout=1.0)
    assert frame.fd and frame.brs and frame.data == bytes(range(64))
```

`CanBus` also has `read(max_frames=8)`, `read_until(match, timeout=)` (returns `None` on timeout),
`add_responder`, `clear_responders`, `status()` and `set_term(on)`. A match is a CAN id or a
`CanFrame -> bool` predicate. `CanFrame` has `id`, `data` (bytes), `ext`, `rtr`, `fd`, `brs`, `dlc`
and `ts` (pod uptime in ms). The command-level methods underneath are on `BenchPod`: `can_config`,
`can_write`, `can_read` (→ `CanReadResult(frames, overflow, pending)`), `can_status`, `can_term`,
`can_respond`, `can_respond_clear`, `can_disable`.

### Step/direction pulse trains

`bp.la_step(PIN3, steps=200, delay=0.001, dir_la=PIN4, direction=1)` emits step pulses on an LA
channel, generated by the FPGA; it returns immediately. `delay` is half the step period: each pulse
is high for `delay`, then low for `delay`, so the example steps every 2 ms (500 steps/s). `steps`
is 1..65535 and `delay` 4 µs..65.535 ms (16-bit counters in the gateware); split a longer move into
several trains.

## AI Vision board

`embeddedci.aivision` is the client for the AI Vision board (ai-vision-2): a camera on the DUT's
screen that reads text, finds buttons and screens, checks the screen's health and presses buttons
through an actuator. Standard library only; a test uses it next to the pod.

```python
from embeddedci.aivision import AIVision

with AIVision("ai-vision-2-5dcec6.local") as v:      # or AIVISION_CONNECTION, or "discover"
    v.calibrate(corners=[(212, 98), (1830, 120), (1822, 760), (205, 741)], screen_size=(1920, 720))
    v.wait_for("Home", timeout=20)
    v.press("Radio", verify={"text": "FM"})
    assert v.compare("radio-fm", mask=["clock"]).score > 0.95
```

A board paired with embeddedci.com is reached from anywhere as `AIVision("embeddedci:<name>")`,
signed in like a cloud pod (`BENCHPOD_API_KEY` or `benchpod login`); `list_boards()` lists them and
`register_cloud(name)` pairs one from its LAN.

In pytest, ask for the `aivision` fixture and pass `--aivision <board>`. Every call, the result
types, the errors and the pytest options:
[docs/aivision.md](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/docs/aivision.md).

## Cloud (`embeddedci:<device-name>`)

The `embeddedci:<device-name>` destination drives a pod that lives somewhere else — behind NAT, in a
lab — through embeddedci.com. The server bridges a raw byte tunnel to the device, so **the full API
works**, including flashing and UART/ADC/LA captures, and the same test you run locally runs
unchanged. Install the `[cloud]` extra.

**Authentication** — the SDK exchanges one of these for a short-lived session token scoped to the
devices you may drive:

1. **An API key** (`eci_…`) via `api_key=`, `--benchpod-api-key` or `BENCHPOD_API_KEY`. Works
   anywhere: your desk, any CI system.
2. **A logged-in user's token** via `cloud_user_token=`, a callable returning the access token.
3. **GitHub Actions OIDC** inside a GitHub Actions job when neither is set. No secret is stored:
   the workflow proves which repo it is, like PyPI Trusted Publishing.
4. **The `benchpod login` session** everywhere else (the default at your desk). Run
   `benchpod login` once and `BenchPod("embeddedci:<name>")` needs no key and no extra argument;
   the SDK refreshes the login when it expires, as the CLI does.

With none of these, the error names all three options, `benchpod login` first.

The SDK renews that session token shortly before it expires (and once more if the server rejects
it), so a connection held for hours keeps working. A token you pass yourself as `cloud_token=` is
used as is. An offline pod fails with "BenchPod '…' is offline", a wrong name with "no cloud
BenchPod named '…'", and a busy pod names who holds it and until when.

```python
from embeddedci.benchpod import BenchPod

# after `benchpod login`
with BenchPod("embeddedci:my-bench-01", la_voltage=3.3) as bp:
    bp.ping()

# or with an API key
with BenchPod("embeddedci:my-bench-01", la_voltage=3.3, api_key="eci_…") as bp:
    bp.ping()
```

```bash
BENCHPOD_API_KEY=eci_… pytest --benchpod-connection=embeddedci:my-bench-01
```

One-time setup (in the EmbeddedCI web app):

1. Give the device a stable name on the **BenchPod** page (the editable name;
   letters/digits/`-_.`, unique per org).
2. For OIDC, on **BenchPod → GitHub Actions**, add your repo (click *Look up* to fill the numeric
   ids) and choose **Any device** or the specific device(s) this repo may drive.

### Running in GitHub Actions

In `.github/workflows/hil.yml`:

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
      # plus an OpenOCD with the cmsis_dap_tcp backend if the tests flash (see Install)
      - run: pytest --benchpod-connection=embeddedci:my-bench-01   # LA voltage comes from conftest.py
```

If the OIDC token can't be minted, the error says exactly why — one of: **not running inside a
GitHub Action**, the job is **missing `id-token: write` permission**, or the **token request itself
failed**. Auth failures raise `CloudAuthError`.

### Device leases

A pod can be driven by one consumer at a time, so a `BenchPod` takes an exclusive **lease** on it
for its lifetime: acquired on connect, renewed by a background heartbeat, released on `close()` (or
it runs out if the process dies). A run that finds the pod busy waits up to `lease_wait` seconds
(default 600, `--benchpod-lease-wait`) and then raises `PodLeasedError` (a `DeviceBusyError`) with
the `holder`, its `purpose` and the seconds it has left (`left_s`), so concurrent runs queue instead
of colliding. `lease=False` / `--benchpod-no-lease` skips locking; `lease_ttl` (default 120 s) sets
the lease length.

- **Cloud** (`embeddedci:<device>`): the lease is the server's.
- **LAN and the USB network** (`usb`): on a pod with the `lease` feature the lease is the pod's
  own, kept in its memory and shared by every transport: while it is held, other connections (from
  this computer or another, over the LAN, USB or the cloud) can read the status but not drive the
  pod. Every request this session sends carries the lease id (`"lease":"<id>"`), which is what makes
  it the owner's; the extra key is only sent to pods with the feature. The holder is `SDK: <user>` and the purpose `lease_purpose=` (default: the suite of the test
  run being recorded, else `sdk`). Without the feature (older firmware) nothing is locked. The USB
  console never leases.
- Several `BenchPod` objects of one process on the same pod share its lease, so a test that opens
  its own connection next to the pytest `benchpod` fixture does not wait for it.
- `bp.local_lease()` reads the pod's lease from its status: a `LocalLease` with `held`, `holder`,
  `purpose`, `left_s`, `mine` (this session holds it) and `reported` (the firmware reports a
  lease). `bp.leased` is true while this session holds either lease.
- If the lease is stopped from the web app, the next command raises `PodLeaseLostError` (with `by`,
  who stopped it) once; the session stops renewing it, later commands run without a lease, and it
  is never taken again. A test run being recorded is marked invalid (`health.lease_lost`). A lease
  that only ran out (the pod restarted, say) is taken again by the heartbeat.

**Joining a runner's lease.** With `BENCHPOD_LEASE` set (`benchpod ci` sets it for the processes it
starts), `BenchPod` joins the lease with that id instead of taking its own: on the LAN and the USB
network it becomes an owner of the pod's lease, over the cloud it uses the id for the server's
lease. Either way it never renews or releases it; the runner does. On a pod without the `lease`
feature it logs one warning and runs without a lease. When the runner's lease is stopped from the
web app, the pytest plugin ends the session after the test that saw it (exit code 2, "run stopped
by ..."); the cases so far, `health.lease_lost` and the reason still go to the run folder.

Over the cloud, ordinary commands go through the server's command channel rather than the byte
tunnel, so they keep working while a UART session holds the tunnel.

| Setting | Default | Purpose |
|---|---|---|
| `api_base=` / `--benchpod-api-base` / `BENCHPOD_API_BASE` | `https://www.embeddedci.com` | embeddedci server |
| `api_key=` / `--benchpod-api-key` / `BENCHPOD_API_KEY` | — | API-key authentication (preferred over OIDC when set) |
| `cloud_token=`, `cloud_audience=` | — | supply a session token / override the OIDC audience |
| `lease=`, `lease_wait=`, `lease_ttl=`, `lease_purpose=` | `True`, `600`, `120`, the run's suite or `"sdk"` | device lease |
| `BENCHPOD_LEASE` | — | join this lease id (set by `benchpod ci`) instead of taking a new lease |

## Build reporting

A test run in GitHub Actions can be recorded on embeddedci.com as a **GitHub-sourced build**: the
tested firmware is uploaded (and can later be flashed from the web UI), the flash wiring is saved as
the UI's defaults, and the pytest result and log become the build status. Request the `build_report`
fixture to opt in:

```python
from embeddedci.benchpod import INTERNAL, PIN11, PIN12


def test_boots(benchpod, firmware, build_report):
    build_report.record_wiring(target="target/stm32f4x.cfg", swclk=11, swdio=12,
                               nreset=True, efuse=1)
    build_report.upload_artifacts([firmware])
    benchpod.flash(file=firmware, target="target/stm32f4x.cfg",
                   swclk=PIN11, swdio=PIN12, nreset=True, target_power=INTERNAL)
    # the pass/fail and the captured pytest output are reported automatically at teardown
```

Reporting is active only inside GitHub Actions with a mintable session token (`id-token: write`,
repo trusted in the web app). Everywhere else the fixture yields an inert reporter
(`build_report.active` is false), so the same test runs unchanged.

**Test results on the build.** When `build_report` reports, the session's tests are also recorded
as a [test run](#test-run-recording) and uploaded at the end, linked to the build the fixture
created (`inputs.dut_firmware.build_id`): every test's outcome, duration, measurements and pod-call
timings, and the traces of the failed tests. The build page shows them under Tests. The fixture
still sends the pass/fail with the build, so older servers keep showing it; current servers keep
the build's own status for the build and show the tests from the run, so a failed test no longer
turns the build red. With several tests using the fixture, the run is linked to the first build. The run is uploaded
with the reporter's own credentials (`build_report.api_base`, `build_report.session_token`).
Recording starts by itself in GitHub Actions when a collected test uses `build_report`; pass
`--embeddedci-run-dir` to choose the folder. `--benchpod-build-target` /
`BENCHPOD_BUILD_TARGET` records a platform id. Outside pytest, `make_build_reporter()` returns the
same `BuildReporter` / `NoopBuildReporter`.

To publish a build **without a pod**, use the `embeddedci-upload-build` command:

```yaml
permissions:
  id-token: write
  contents: read
steps:
  # … build the firmware …
  - run: pip install embeddedci
  - run: >
      embeddedci-upload-build --firmware build/app.elf --build-target stm32f4
      --openocd-target target/stm32f4x.cfg --swclk 11 --swdio 12 --nreset true --efuse 1
```

It uploads the firmware plus any sibling `.elf`/`.bin`/`.hex`/`.uf2` (add more with `--artifact`),
names the build with `--name`, and writes `build_id=…` to `$GITHUB_OUTPUT`. It exits non-zero
without a session unless `--allow-missing-token` is passed.

## Errors

Invalid arguments raise `ValueError` before anything is sent. Everything else raises a
`BenchPodError` subclass, all importable from `embeddedci.benchpod`.

Match a pod refusal on its code or class, not its text: see [Error codes](#error-codes).

| Exception | Raised when | Extra attributes |
|---|---|---|
| `BenchPodError` | base class of all of the below | |
| `ConnectionConfigError` | no connection configured, unparsable string, discovery found zero or several pods | |
| `TransportError` | the pod or tunnel could not be reached / talked to | |
| `TransportTimeout` (a `TransportError` and a `TimeoutError`) | the pod or tunnel did not answer in time | |
| `ConnectionClosedError` (a `TransportError`) | the connection ended or reset before the reply | |
| `TlsPinMismatchError` (a `TransportError`) | the pod's TLS certificate is not the one pinned for it (see [TLS on the LAN](#tls-on-the-lan)) | `addr`, `pinned`, `got` |
| `FirmwareError` | the pod replied with an error: a coded refusal (see [Error codes](#error-codes)), rendered from the catalog, or a refusal without a code, with the pod's text | `code`, `values`, `docs_url`, `firmware_message` (the pod's text, `""` for a coded refusal), `cmd` |
| `FlashError` | OpenOCD failed, is missing, or lacks the TCP backend | |
| `TargetUnreachableError` (a `FlashError`) | the probe worked but no target answered on SWD | |
| `ProgFlashError` (a `FlashError`) | a flash over programming pins (UPDI, PIC, C2, TPI, PDI) failed | `result`, `phase`, `mem`, `addr`, `at_addr` |
| `ProgNoAnswerError`, `ProgLockedError`, `ProgVerifyError`, `ProgDeviceError` (`ProgFlashError`s) | the chip did not answer (also a `TargetUnreachableError`), is locked, read back wrong, or is not in the table / not the `device` named / cannot be programmed at this voltage | as `ProgFlashError` |
| `DeviceBusyError` | a device lease was not granted within `lease_wait` | |
| `CloudAuthError` | no session token: API key rejected, OIDC unavailable, exchange failed | |
| `ServerApiError` | an embeddedci server API call failed | `status` (HTTP) |
| `PodLockedError` (a `FirmwareError`) | the pod's LAN policy keeps the command for the cloud or USB (`BP0040` to `BP0044`), or plaintext 8080 is off on the LAN (`BP0075`) | `code`, `cmd` |
| `PodBusyError` (a `FirmwareError`) | the pod is busy: a session is already open (`BP0003`, `BP0004`, `BP0049`, `BP0050`), the bus engine or the storage is in use (`BP0005`, `BP0016`, `BP0048`), another long command, update or cloud command runs (`BP0047`, `BP0051`, `BP0052`), a step train runs (`BP0066`) | `code`, `cmd` |
| `ReplyTooLargeError` (a `FirmwareError`) | the reply did not fit the pod's reply buffer (`BP0053`) or the cloud channel (`BP0054`) | `code`, `values` |
| `DacPinoutMismatchError` (an `UnsupportedFeatureError`) | the DAC is off: the digital board and the analog module use different flat-cable pinouts (`BP0080`) | `digital`, `module` |
| `PodLeasedError` (a `PodBusyError`, `DeviceBusyError` and `TransportError`) | someone else holds the pod's lease (`BP0025`), a cloud job holds the pod (its refusal on the LAN, or the server's HTTP 409), or the lease did not free within `lease_wait` | `holder`, `purpose`, `left_s`, `expires_at`, `status` |
| `PodLeaseLostError` (a `FirmwareError`) | the pod's lease this session held was stopped from the web app or the cloud (`BP0026`); the command did not run | `by` |
| `PermissionDeniedError` (a `FirmwareError` and `TransportError`) | the pod's `BP0045` (owner or admin only) on a cloud tunnel, or a server HTTP 403 (`ServerPermissionDeniedError`, also a `ServerApiError`, from `ServerApi`) | `status` |
| `UnsupportedFeatureError` (a `FirmwareError`) | the pod lacks the feature: its capabilities say so (raised before anything is sent), it does not know the command (`BP0002`), its gateware is too old (`BP0017`; `feature` names it), or the digital-only board has no analog front end (`BP0046`, `feature="analog"`) | `feature`, `firmware_version`, `code`, `cmd` |
| `UsbHostError` | USB host mode is off for a call that needs it (`usb_wait_device`) | |
| `UsbTimeout` (a `UsbHostError` and a `TimeoutError`) | no (matching) USB device mounted in time, or host mode did not start | |
| `UsbHostUnavailableError` (an `UnsupportedFeatureError`) | the pod cannot host USB: the request came over its own USB link (`BP0076`), it has no such port (`BP0077`), its crystal clock is not running (`BP0078`) | `port`, `feature` (`"usb_host"`) |
| `UsbHostPortError` (a `FirmwareError`) | host mode already runs on the other port (`BP0079`) | `port` |
| `UsbFlashRefusedError` (a `FirmwareError`) | the pod refused to start a USB flash (`BP0084`): host mode off, no drive mounted, nothing staged, a bad mode, name or length | `why` |
| `UsbSerialUnavailableError` (a `FirmwareError`) | the pod has no USB serial port to open (`BP0085`): host mode off, no board attached or no serial port | `why` |
| `UsbSerialBusyError` (a `PodBusyError`) | the board's USB port is in use (`BP0086`): a `usb_flash` or `usb_check` runs | `why` |
| `UsbCheckRefusedError` (a `FirmwareError`) | the pod cannot run the USB check (`BP0087`) | `why` |
| `UsbCheckNoPowerError` (a `FirmwareError`) | the board is not powered from eFuse 1 (`BP0088`) | |
| `XipUnavailableError` (a `FirmwareError`) | the pod cannot run the USB flash or pin-programmer code from its W25Q (`BP0083`): install the blobs of its firmware | `why` |
| `UsbFlashError` (a `FlashError`) | a USB flash failed, timed out or was aborted, or no loader fits the board | `result`, `phase`, `mode`, `state` |
| `WiringError` (a `FirmwareError`) | a wiring-profile operation was refused (`BP0007` to `BP0015`): no such profile, a bad name, a revision conflict, the store is full, too large, not a JSON object, the default cannot be deleted, an upload out of order, the storage failed | `name`, `code`, `values` |
| `ClientTooOldError` (a `FirmwareError`) | this SDK is too old for the pod's firmware: the pod announces a client API level above `CLIENT_API` (raised on connect), or refuses the SDK's level (`BP0030`). Update the SDK: `pip install -U embeddedci` | `client_api`, `api`, `min_client_api`, `firmware_version` |
| `FirmwareTooOldError` (an `UnsupportedFeatureError`) | the pod runs firmware 3.x (client API below 2), which this SDK does not drive (raised on connect). Update the pod: `benchpod flash-self`, or the firmware update in the web app; or use embeddedci 3.x with it | `api`, `firmware_version`, `feature` (`"client_api"`) |
| `UartTimeout` | `UartSession.expect` timed out | `text` |
| `UartLinkError` (a `UartTimeout` and `ConnectionClosedError`) | the UART link died with an error while `UartSession` waited | `text`, `cause` |
| `PinConflictError` | an LA channel is already used by another function (a `FirmwareError`) | `la`, `function` |
| `DirectionConflictError` | a push-pull driver on a pin `la_dir` marks as a DUT output (a `FirmwareError`) | `la` |
| `I2CNackError` | an I2C device did not acknowledge (`I2CMaster.write`, `read`, `write_read`, `read_reg`, `write_reg`) | `addr`, `nack_at` |
| `PullConflictError` | an engaged bias resistor can't work with the channel's function (a `FirmwareError`) | `la` |
| `PullDirectionError` | the channel can't pull that way (BP0024) or has no pull resistor (BP0023); also a `ValueError` | `la`, `want`, `have` |
| `TriggerTimeout` | a triggered capture's condition never happened (a `FirmwareError`) | `la`, `edge` |
| `CanTimeout` | `CanBus.expect` timed out | `frames` |

## Escape hatches

These sit below the stable API and are **not** covered by the stability promise:

* `bp.command({"cmd": "status"})` — send one raw JSON firmware command and get its `data` back
  (raises `FirmwareError` when the pod refuses). Works over TCP, USB and the cloud.
* `bp.transport` — the underlying transport object.
* `bp.lowlevel` — individual analog switches and raw DAC codes for bring-up and diagnostics:
  `dac_mux(ctrl1=, ctrl2=)`, `dac_mux_status()`, `cal_switch(cal1=, cal2=, current_in=, cal_path=)`,
  `cal_switch_status()`, `dac_set(code16=, divider=)` (16-bit; `dac_set(code)` takes the older 8-bit level). Prefer the named paths — these can leave the
  front end in a state no named path describes.

## API reference

Every name in `embeddedci.benchpod.__all__`, in one place. The sections above show how the pieces
fit together; this is the index. `tests/test_docs_coverage.py` fails when a public name is missing
from this README, so the list stays complete.

### `BenchPod`

`BenchPod(connection=None, *, la_voltage=None, timeout=30.0, transport=None, api_base=None,
api_key=None, cloud_token=None, cloud_audience=None, cloud_user_token=None, lease=True,
lease_wait=600.0, lease_ttl=120, wiring=None, lease_purpose=None)`. A context manager; `close()` is idempotent.

| Area | Methods and properties |
|---|---|
| Connection and device | `ping()`, `status()`, `diag()` (heap, per-task stacks, network-stack and console counters: the `diag` command), `mac_addresses(status=None)` (the Ethernet and Wi-Fi MACs, see [MAC addresses](#mac-addresses-managed-networks)), `capabilities`, `refresh_capabilities()` (drop the cached capabilities and read them again), `close()`, `leased` (true while this client holds a lease, see [Device leases](#device-leases)), `local_lease()` (the pod's own lease: `LocalLease`), `wiring`, `save_wiring(wiring=None, name=None)` (store a wiring profile on the pod; firmware before 3.10.8: on embeddedci.com), `signal(name)`, `fpga_image(image)`, `has_bus_masters()` (the running gateware image has SWD, JTAG, the SPI and I2C masters: gateware before v65, or the loop and deep-replay images), `usb_cc()`, `usb_network(on=None)` (the USB network setting; `False` turns the adapter off over the USB link), `lan_tls()`, `set_lan_tls_https_only(on)`, `regenerate_lan_tls()`, `set_lan_tls_cert(cert_pem, key_pem)` (the LAN API over TLS, see [TLS on the LAN](#tls-on-the-lan)) |
| LA voltage | `set_la_voltage(volts)`, `get_la_voltage()` |
| Power and reset | `power_on(efuse=None, *, delay=None)`, `power_off(efuse=None, *, delay=None)`, `target_power(efuse=None, *, on, delay=None)`, `target_status()`, `power_status()`, `measure_power(duration, *, efuse=None, rate_hz=500.0, keep_samples=0)`, `power_profile(*, efuse=None, rate_hz=500.0, keep_samples=4096, max_duration=60.0)`, `reset_target(*, pulse=0.1)`, `set_reset(asserted)`, `reset_state()` |
| USB host and flashing | `usb_host(enable=True, *, port=None, timeout_s=60)`, `usb_host_status()`, `usb_wait_device(vid=None, pid=None, timeout=10)`, `usb_flash(file, *, address=None, name=None, leave=True, mode=None, port=None, timeout=None, device_timeout=10, progress=None, verify=False)`, `usb_flash_status()`, `usb_flash_abort()`, `usb_check(test, *, count=None, off_ms=None, timeout_ms=None, duration_ms=None, settle_ms=None, port=None, device_timeout=10, timeout=None, progress=None)`, `usb_check_status()`, `usb_check_abort()`, `usb_hid_interfaces()`, `usb_hid_capture(enable=True)`, `usb_hid_read(count=1, timeout=2.0, instance=None)` (see [USB host mode and USB flashing](#usb-host-mode-and-usb-flashing)) |
| Flashing | `flash(...)`, `detect(*, invasive=True, methods=None)` (see [Detecting the chip on the board](#detecting-the-chip-on-the-board)), `spi_flash(image, addr=0, *, erase=True, verify=True, hold_reset=False, sck=, mosi=, miso=, cs=)`, `open_spi(...)`, `open_motor_emulator(...)` |
| I2C master | `open_i2c(sda=None, scl=None, *, hz=100_000, pullups="pod")` (see [I2C master](#i2c-master)) |
| GPIO expanders | `expansion(chip="", *, addr=None, sda=None, scl=None, spi=None, load=None, count=None, hz=None, pullups=None, names=None)` (see [GPIO expanders](#gpio-expanders)) |
| UART | `capture_uart(...)`, `power_cycle_and_capture(...)`, `open_uart(...)` (both take `port="usb"`, `dtr` and `rts` for the hosted board's USB serial port) |
| Bias resistors | `enable_pullup(*las)`, `disable_pullup(*las)`, `enable_pulldown(*las)`, `disable_pulldown(*las)` (LA7/LA8), `set_pull(la, enabled)`, `pull_state(la)`, `enabled_pulls()` |
| GPIO and pins | `gpio(la, mode="output", *, level=None)`, `gpio_pins(las, mode="output", *, level=None)`, `configure_gpio(las, mode="output", *, level=None)`, `set_gpio(la, level)` (`level` may be a list, one per channel), `read_gpio(la)`, `wait_for_level(la, level, *, timeout, poll=0.005)` (`False` when `timeout` passes first), `pin_levels()`, `la_pins(la=None)`, `la_dir(la=None, dir=None)`, `pin_directions(wiring=None)`, `apply_pin_directions(wiring=None)`, `pin_directions_status`, `release_gpio(*las)`, `la_step(la, *, steps, delay, dir_la=None, direction=0)` |
| Emulated sensors | `sensors` (a `SensorEmulation`: `start(definition, *, instance=None, pins=None, address=None, baud=None, values=None)`, `define()`, `validate()`, `set()`, `fault()`, `clear_fault()`, `load_stimulus()`, `start_stimulus()`, `stop_stimulus()`, `stop()`, `status()`, `regs()`, `capture()`, `i2c_transactions()`, `library()`); see [Emulated sensors](#emulated-sensors) |
| Analog | `analog_path(path, *, range=None)`, `adc_read(source="ext", *, range=None)`, `adc_range(range=None)`, `adc_range_test()`, `dac_output(path, *, volts=None)`, `current_out(current)`, `current_out_range()`, `calibrate(source="current_in")`, `calibration()`, `clear_calibration(part=None)`, `calibrate_dac(path, points)`, `calibrate_adc_gain(source, *, range="normal")`, `DMM_SET_POINTS` |
| Captures | `capture_adc(...)`, `capture_la(..., send=None)`, `capture_correlated(...)`, `decode(source, protocol="i2c", ...)` |
| DAC | `generate(...)`, `replay(...)`, `replay_waveform(...)`, `dac_stop()`, `control_loop(...)`, `loop_input(...)`, `loop_probe()` |
| CAN | `open_can(*, bitrate=500000, mode="normal", term=False, fd=False, data_bitrate=None)`, `can_config(...)`, `can_disable()`, `can_status()`, `can_term(on)`, `can_write(...)`, `can_read(...)`, `can_respond(...)`, `can_respond_clear()` |
| Cloud library and server | `waveforms`, `save_capture_as_recording(...)`, `server_api` (the `ServerApi`; needs an API key or a cloud session) |
| Escape hatches | `command(cmd)`, `transport`, `lowlevel` (see [Escape hatches](#escape-hatches)) |

### Types

Results are frozen dataclasses unless noted. A result built from a firmware reply keeps that reply in
`raw` and has a `from_reply()` classmethod (`from_dict()`, `from_json()` or `from_chunks()` for the
few built from other shapes); `to_dict()` goes the other way where a type is sent back.

| Type | Returned by / used for | Fields and members |
|---|---|---|
| `LaVoltage` | `get_la_voltage`, `set_la_voltage` | `voltage`, `readback`, `is_set`, `pin21` (a `Pin21`, digital v4 only) |
| `Pin21` | `LaVoltage.pin21` | `source`, `setting`, `measured` (V), `state`, `up`, `ok` |
| `TargetStatus` | `target_status` | `internal`, `external` (each an `EfuseState`), `supported`, `efuse(n)` |
| `EfuseState` | `TargetStatus.efuse` | `enabled`, `fault`, `valid` |
| `PowerStatus` | `power_status` | `internal`, `external` (each a `RailPower`), `pod`, `total_current`, `rail(n)` |
| `RailPower` | `PowerStatus.rail` | `ok`, `bus_voltage`, `current` |
| `PowerProfile` | `measure_power`, `PowerProfileSession.result` | see [Power profiles](#power-profiles) |
| `PowerProfileSession` | `power_profile` (a context manager) | `start()`, `stop()`, `status()`, `result` |
| `ResetState` | `reset_target`, `set_reset`, `reset_state` | `asserted`, `supported` |
| `MacAddresses` | `mac_addresses` | `eth_mac`, `wifi_mac` (None while not known), `has_wifi`, `source` (`pod`, `server` or empty) |
| `LanTls` | `lan_tls`, `set_lan_tls_https_only`, `regenerate_lan_tls`, `set_lan_tls_cert` | `port`, `https_only`, `sessions`, `origin` (`generated`/`user`), `key`, `subject`, `san`, `not_after`, `sha256` (what clients pin), `error` (no certificate) |
| `LanClients` | `lan_clients`, `pair` | `pairing` (computers on the LAN must be paired), `count`, `max`, `clients` (`LanClient`: `name`, `spki_sha256`, `this`), `has(spki_sha256)` |
| `DetectResult` | `detect` | `powered`, `findings`, `skipped`, `notes`, `to_dict()` |
| `DetectFinding` | `DetectResult.findings` | `method`, `chip`, `vendor`, `core`, `id`, `confidence`, `pins`, `raw`, `invasive`, `summary`, `to_dict()` |
| `UsbHostState` | `usb_host`, `usb_host_status` | `mode`, `port`, `remaining_s`, `host_ms`, `enumerations`, `attached`, `enumeration`, `connected`, `line_speed`, `device`, `hosting`, `mounted` |
| `UsbDevice` | `usb_wait_device`, `UsbHostState.device` | `vid`, `pid`, `vid_pid`, `speed`, `usb`, `release`, `device_class`, `subclass`, `protocol`, `ep0_size`, `configurations`, `manufacturer`, `product`, `serial`, `descriptor_ms`, `mounted_ms`, `config`, `interfaces`, `has_interface(interface_class, subclass=None)` |
| `UsbConfig` | `UsbDevice.config` | `value`, `max_power_ma`, `self_powered`, `remote_wakeup`, `interface_count`, `truncated`, `malformed`, `interfaces` |
| `UsbInterface` | `UsbConfig.interfaces` | `number`, `alt`, `interface_class`, `class_name`, `subclass`, `protocol`, `endpoints` |
| `UsbFlashResult` | `usb_flash` | `ok`, `mode`, `reason`, `length`, `address`, `name`, `protocol`, `ms`, `device`, `state`, `note` |
| `UsbFlashState` | `usb_flash_status`, `usb_flash_abort`, `UsbFlashError.state` | `running`, `have_result`, `mode`, `name`, `length`, `address`, `protocol`, `phase`, `done`, `total`, `result`, `ok`, `ms`, `note`, `unit` |
| `UsbCheckState` | `usb_check`, `usb_check_status`, `usb_check_abort` | `running`, `have_result`, `test`, `phase`, `done`, `total`, `ms`, `passed`, `error`, `result`, `ended` |
| `UsbHidState`, `UsbHidInterface` | `usb_hid_interfaces` | `attached`, `interfaces`; `instance`, `interface`, `protocol`, `descriptor_len`, `descriptor`, `truncated` |
| `UsbHidCapture` | `usb_hid_capture` | `capturing`, `buffered`, `dropped` |
| `UsbHidReports`, `UsbHidReport` | `usb_hid_read` | `reports`, `dropped`, `capturing`; `ms`, `instance`, `data` |
| `UsbCcStatus` | `usb_cc` | `orientation`, `advertised`, `advertised_current` (A), `cc1_voltage`, `cc2_voltage` (V) |
| `PullState` | `pull_state`, `set_pull` | `la`, `enabled`, `direction`, `ohms`, `available`, `supported`, `strength` (digital v4) |
| `LaPinState` | `la_pins`, `configure_gpio`, `la_dir` | `la`, `function`, `gpio`, `level`, `pull`, `pull_ohms`, `pull_on`, `pull_dirs`, `dir`, `warn`, `warnings`, `in_use` |
| `GpioPin` (class) | `gpio`, `gpio_pins`, `signal` | `name`, `configure()`, `set(level)`, `high()`, `low()`, `activate()`, `deactivate()`, `is_active()`, `read()`, `wait_for(level, *, timeout)`, `pulse(width, *, count=1)`, `state()`, `release()` |
| `ExtPin` (class) | `gpio`, `gpio_pins`, `signal` on an EXT pin | a `GpioPin` with `ext`, `pin`; `la` is `None`; `pulse()` raises `ExpansionError` |
| `FlashResult` | `flash` | `ok`, `returncode`, `stdout`, `stderr`, `target_unreachable`, `stalled`, `artifact` (the uploaded build, for `flash(build=...)`) |
| `SpiSession` (class) | `open_spi` | `closed`, `flash_id()`, `flash_read(addr, length)`, `flash_erase(addr, length)`, `flash_chip_erase()` (whole chip; seconds taken), `flash_write(addr, data)`, `flash_program(data, addr=0, *, erase=True, verify=True)` (erase, write and verify a whole image), `transfer(data)`, `stream(data, *, head=b"", hold_cs=False)`, `close()` |
| `I2CMaster` (class) | `open_i2c` (a context manager) | `sda`, `scl`, `hz`, `pullups`, `xfer(addr, write=None, read=0)`, `write(addr, data)`, `read(addr, n)`, `write_read(addr, data, n)`, `read_reg(addr, reg, n=1)`, `write_reg(addr, reg, data)`, `probe(addr)`, `scan(start=0x08, end=0x77)`, `send(addr, write=None, read=0)` (an `I2CSend`), `status()`, `close()` |
| `I2CResult` | `I2CMaster.xfer` | `addr`, `ack`, `data`, `nack_at`, `check()` (raises `I2CNackError` on a NACK) |
| `I2CSend`, `SpiSend`, `GpioSend` | `capture_la(send=)` | `addr`, `write`, `read`; `tx`; `la`, `level`; `to_request()` |
| `SendResult` | `LaCapture.send_result` | `bus`, `ack`, `data`, `nack_at`, `la`, `level`, `error`, `ok` |
| `SpiFlashInfo` | `SpiSession.flash_id` | `jedec_id`, `present`, `size`, `status` |
| `SpiFlashResult` | `spi_flash`, `SpiSession.flash_program` | `jedec_id`, `addr`, `length`, `erased`, `verified`, `seconds` |
| `SpiStreamResult` | `SpiSession.stream` | `sent`, `seconds` |
| `UartCapture` | `capture_uart`, `power_cycle_and_capture` | `text`, `lines`, `matched`, `match(pattern)`, `contains(needle)` (also `needle in capture`) |
| `UartEntry` (frozen dataclass) | passed to `open_uart` / `capture_uart` as `entry` | `reset_ms`, `tx_low`, `hold_ms`, `idle_ms`, `data`, `to_request()` |
| `UartSession` (class) | `open_uart` (a context manager) | `write(data)`, `read(*, timeout=0.0)`, `read_until(pattern, *, timeout)`, `expect(pattern, *, timeout)`, `text`, `lines`, `closed`, `error`, `close()` |
| `Sensor` (class) | `sensors.start`, `sensors.sensor` | `instance`, `kind`, `name`, `params`, `info`, `started`, `set(**values)`, `fault(type, *, duration_s=None, **fields)`, `clear_fault()`, `status()`, `regs(start=0, length=None)`, `capture(samples, sample_rate_hz, *, timeout_s, wait_for_traffic)`, `i2c_transactions(...)`, `stop()` |
| `SensorEmulation` (class) | `BenchPod.sensors` | see [Emulated sensors](#emulated-sensors) |
| `SensorLibrary` (class) | `sensor_library`, `sensors.library` | `version`, `source`, `warning`, `root`, `entries`, `find(name)`, `get(name)`, `path(name)`, `text(name)`, `load(name)`, `format_doc()` |
| `LibraryEntry` | `SensorLibrary.entries` | `path`, `name`, `kind`, `label`, `to_dict()` |
| `I2CTransaction` | `Sensor.i2c_transactions`, `decode("i2c")` | `messages`, `complete`, `address`, `to(address)` |
| `I2CMessage` | `I2CTransaction.messages` | `address`, `read`, `address_ack`, `data`, `values` |
| `I2CByte` | `I2CMessage.data` | `value`, `ack` |
| `UartFrame` | `decode("uart")` | `index`, `start_us`, `end_us`, `value`, `hex`, `text`, `ok`, `error` |
| `SpiFrame` | `decode("spi")` | `index`, `start_us`, `end_us`, `mosi`, `miso`, `mosi_hex`, `miso_hex` |
| `UsbDecode` | `decode("usb")` | `speed`, `packets`, `transactions`, `transfers`, `sof_count`, `keep_alive_count`, `error_count`, `as_dict()` |
| `AnalogPathState` | `analog_path` | `path`, `dac_mux_register`, `cal_relay_register`, `range` |
| `AdcReading` | `adc_read` | `source`, `voltage`, `count`, `span`, `offset`, `current`, `range`, `fullscale`, `overrange` |
| `AdcRangeState` | `adc_range` | `range`, `supported`, `fullscale`, `normal_fullscale`, `low_fullscale`, `a`, `b` (volts, the `ext` fit of the range) |
| `AdcRangeTest` | `adc_range_test` | `passed`, `points` (each an `AdcRangeTestPoint`: `set_voltage`, `normal`, `low`, `ok`) |
| `AdcRange` (literal) | `adc_range`, `range=` | `"normal"`, `"low"` |
| `DacOutput` | `dac_output` | `path`, `voltage`, `code` |
| `CurrentOutput` | `current_out`, `current_out_range` | `current`, `code`, `min_current`, `max_current` |
| `Calibration` | `calibrate`, `calibration`, `clear_calibration`, `calibrate_dac`, `calibrate_adc_gain` | `source`, `calibrated`, `offset`, `a`, `b`, `count`, `span`, `samples`, `dac`, `adc_gain_ppm`, `adc_low_gain_ppm`, `dmm`, `measured`, `zero_count` |
| `DacLine` | `Calibration.dac` | `a`, `b`, `offset`, `gain_ppm` |
| `Capture` | `capture_adc` | `counts`, `volts`, `currents`, `sample_rate_hz`, `source`, `trigger`, `range`, `overrange`, `clipped_samples`, `duration`, `times()`, `mean()`, `min()`, `max()`, `rms()`, `rms_ac()`, `peak_to_peak()`, `first_crossing()`, `crossing_times()`, `fft()`, `dominant_frequency()` |
| `LaCapture` | `capture_la` | `words`, `channels`, `sample_rate_hz`, `trigger`, `send_result`, `duration`, `channel(la)`, `level_at()`, `edges()`, `edge_times()`, `first_edge()`, `delay()`, `frequency()`, `duty_cycle()`, `pulse_widths()`, `decode()` |
| `CorrelatedCapture` | `capture_correlated` | `adc` (a `Capture`), `la` (a `LaCapture`), sharing one hardware trigger |
| `Trigger` | `capture_adc`, `capture_la`, `capture_correlated` | `la`, `edge` |
| `DacHandle` (class) | `generate` (a context manager) | `stop()` |
| `ReplayHandle` (class) | `replay`, `replay_waveform` (a context manager) | a `DacHandle` that also knows what it replays: `stop()` |
| `Fault` | `replay(fault=...)` | `type`, `start`, `width`, `level`, `to_dict()` |
| `Segment` | `WaveformLibrary.save_segments` | `shape` (`ramp`, `hold`, `step`), `duration`, `v_start`, `v_end`, `to_dict()` |
| `ControlLoopHandle` (class) | `control_loop` (a context manager) | `probe()`, `set_input(...)`, `stop()` |
| `IVPoint` | `loop_probe`, `ControlLoopHandle.probe` | `i`, `v`, `input_code`, `source`, `tripped`, `loop_input` |
| `LoopInputMap` | `control_loop(input_map=...)` | `mv_per_unit`, `range_min`, `range_max`, `mv_at_zero`, `trip`, `to_request()` |
| `LoopState` | `loop_input` | `source`, `input_code`, `step`, `output_code` |
| `FpgaImage` (enum) | `fpga_image` | `LOOP`, `DEEP_REPLAY`, `EMULATION` |
| `FpgaImageInfo` | `fpga_image` | `image`, `version`, `features` |
| `CanBus` (class) | `open_can` | `write()`, `read()`, `read_until()`, `expect()`, `collect()`, `assert_periodic()`, `add_responder()`, `clear_responders()`, `simulate_ecu()`, `set_term()`, `status()`, `close()` |
| `CanFrame` | `CanBus.read`, `CanBus.expect` | `id`, `data`, `ext`, `rtr`, `ts`, `dlc` |
| `CanReadResult` | `can_read` | `frames`, `overflow` |
| `MotorEmulator` (class) | `open_motor_emulator` | `configure()`, `boards()`, `probe()`, `read()`, `read_signed(reg)`, `write()`, `status()`, `trips()`, `clear_trips()`, `arm()`, `set_control()`, `set_pwm()`, `set_duties(a, b, c)`, `set_shape(shape)`, `set_protection()`, `set_battery()`, `battery_state()`, `set_pv_calibration(offset, gain)`, `sample()`, `time()`, `start_log()`, `stop_log()`, `read_log()`, `eeprom_read(addr, length)`, `eeprom_write(addr, data)`, `close()` |
| `BatteryModel` | `MotorEmulator.set_battery` | `capacity_ah`, `ocv`, `r0_ohm`, `r1_ohm`, `tau_s`, `soc`, `v_min`, `v_max`, `invert_current` |
| `EmulatorCalibration` | `open_motor_emulator(calibration=...)` | `codes_per_amp`, `codes_per_volt`, `offsets`, `sample_rate_hz`, `amps()`, `volts()`, `current_code()`, `bus_code()`, `resistance_code()` |
| `Wiring` | `wiring`, `save_wiring` | see [Wiring profile](#wiring-profile); also `load(path)`, `coerce(value)` (a `Wiring`, dict or file path), `from_dict(data, *, strict=True)` (`strict=False` keeps unknown keys in `extra`), `to_dict()`, `with_changes(**changes)`, `assignments()`, `pins()`, `la(name)`, `signal(name)`, `dut_dir(name)`, `pin_dut_dirs()`, `describe()`, `warnings()`, `la_voltage`, `i2c_address`, `dut_dirs` |
| `Signal` | `Wiring.signals` | `name`, `la`, `direction`, `active_low`, `description`, `to_dict()` |
| `Waveform` | `WaveformLibrary` | `id`, `name`, `kind`, `sample_count`, `sample_rate_hz`, `bits`, `full_scale_v`, `recording_size_bytes`, `created_at`, `dac_path`, `segments`, `unit`, `is_recording` |
| `WaveformLibrary` (class) | `waveforms` | `list()`, `get()`, `find()`, `save_recording()`, `save_waveform()`, `save_segments()`, `rename()`, `delete()`, `preview()`, `download_recording()`, `samples_b64(waveform_id)` |
| `BuildReporter`, `NoopBuildReporter` (classes) | `build_report`, `make_build_reporter` | `active`, `build_id`, `record_wiring()`, `upload_artifact()`, `upload_artifacts()`, `upload_logs()`, `set_result()`, `finalize()` |
| `DeviceLease` (class) | a cloud `BenchPod` holds one | `acquire(*, wait_timeout=600.0, poll_interval=5.0)`, `held`, `lease_id`, `release()` |
| `ConnSpec` | `parse_connection`, `resolve_connection` | `kind`, `addr`, `device`, `device_name`, `is_wifi()`, `is_serial()`, `is_cloud()` |
| `LowLevel` (class) | `lowlevel` | see [Escape hatches](#escape-hatches) |
| `Pin` / `Efuse` (enums) | channel and rail arguments | `PIN1` … `PIN14`, `INTERNAL`, `EXTERNAL` |

### `Capabilities`

`bp.capabilities` (and the `benchpod_capabilities` fixture) parses the pod's `status` reply
(`Capabilities.from_status`) or the server's `cap.*` map for a cloud device
(`Capabilities.from_parameters`); `merge(other)` overlays one on another. A flag that was not
reported keeps its default (`False`, `0` or `""`), so a feature check is just
`if bp.capabilities.spi_master:`. `@pytest.mark.benchpod_capability("name")` takes the same names.

| Group | Fields |
|---|---|
| Identity | `board`, `firmware_version`, `board_rev` (`"v2"`, `"v3"`, `"v3r2"`, `"v4"` (digital v4; firmware 4.0.x says `"v3r4"`), `"unknown"`, or `""`), `la_vccio_mv` (the LA I/O voltage the pod reports, 5000 in 5 V mode) |
| ADC | `adc_bits`, `adc_fullscale_mv`, `adc_channels`, `adc_offset_counts`, `adc_affine` (the front-end fit, of the range in use), `adc_low_range` (analog module v3r4+: the low range), `adc_range` (`"normal"`, `"low"`, or `""` before firmware 3.9.5), `adc_range_fullscale` (volts, ±), `adc_max_count`, `counts_to_volts(count)` |
| DAC and replay | `dac`, `dac_dc`, `dac_replay`, `dac_deep_replay`, `dac_control_loop`, `dac_loop_sources` (gateware v29+), `dac_loop_input_map` (v30+), `dac_cotrig` (v27+), `generate16` (`generate` in calibrated 16-bit volts: every analog pod), `dac_bits`, `dac_replay_bits`, `dac_replay_max_samples`, `dac_fullscale_mv`, `dac_channels`, `dac_limits` (output limits on the DAC paths) |
| Features | `scope`, `analyzer`, `serial`, `tunnel`, `command`, `ota`, `la_pins`, `gpio_read` (direct pin reads, v35+), `capture_trigger` (v35+), `power_profile`, `capture_b64` (faster ADC read-back, used automatically), `nrst_pin` (the dedicated reset pin, rev3), `usb_cc` (rev3), `vref_out` (digital v4: pin 21 switched and measured, 5 V I/O, pulls with a strength on every channel), `spi_master` (v45+), `spi_stream`, `uart_frame` (UART parity and stop bits, gateware v56+), `uart_hd` (one-wire UART and boot-mode entry, gateware v57+), `calibrate`, `cal_dmm` (firmware 3.9.4+: `calibrate_dac`, `calibrate_adc_gain`), `current_out`, `can`, `pod_current` (`PowerStatus.pod`), `sensors` (sensor emulation from definitions), `analog` (`False` on the digital-only board, `None` when not announced) |
| Firmware updates and policies | `flash_kb` (MCU flash in KiB, 0 when not reported), `blob_slots`, `ota_sig`, `sig_policy` (`"audit"`, `"required"`, ...), `sig_keys`, `sig_policy_cmd`, `lan_policy` (`"open"`, `"locked"`, `"off"`), `lan_policy_cmd`, `tunnel_max_tier`, `lease_state` (the pod refuses LAN writes while a cloud job holds it), `cloud_ca`, `cloud_proxy`, `ws_auth_v2` (server only) |
| Boot health | `safe_mode`, `safe_reason`, `last_crash`, `reset_cause`, `boot_warning()` (a one-line warning after safe mode or a crash, else `None`) |
| Source | `raw` (the map it was parsed from) |

### `ServerApi`

A thin client for the embeddedci server's HTTP API (`{api_base}/api/...`), for scripts that need
the server rather than a pod. `bp.server_api` returns one bound to the connection's credentials;
build one yourself with `ServerApi(*, api_base=None, api_key=None, token_provider=None,
timeout=60.0, lease_id=None)`. Errors raise `ServerApiError` (`status`), and an HTTP 403 raises
`ServerPermissionDeniedError`.

| Method | Purpose |
|---|---|
| `list_devices()` | the pods the credentials can see |
| `resolve_device_id(name)` | a device name to its server id (raises when unknown) |
| `device_parameters(name_or_id)` | the server's `cap.*` map for a device (`Capabilities.from_parameters` parses it) |
| `wiring_profile(device_id)` | the stored wiring profile: `{"profile", "stored", "defaults", "warnings"}` |
| `put_wiring(device_id, wiring)` | store a wiring profile (the server validates it; errors name the fields) |
| `scope_capture_start(device_id, *, samples=256, sample_rate_mhz=1.0)` | start a server-side ADC capture; returns its id |
| `dual_capture_start(device_id, *, adc_samples, adc_rate_mhz, la_samples, la_rate_mhz)` | start a server-side ADC + LA capture; returns its id |
| `capture_snapshot(capture_id)` | fetch a server-side capture |
| `replay_start(payload)` | arm a server-side DAC replay (`POST /dac/replay/start`) |
| `build_artifact(*, build="", repo="", ref="", artifact="")` | resolve an uploaded build to the file a flash would use (`job_id`, `pack_id`, `name`, `repo`, `ref`, `sha`, `wiring_defaults`) |
| `download_build_artifact(art)` | download that file's bytes |
| `flash_build(device_id, body, *, timeout=360.0)` | have embeddedci.com flash an uploaded build through a cloud pod (`POST /benchpod/devices/{id}/flash`) |
| `request(method, path, *, query=None, json_body=None, raw_body=None, content_type=None, parse_json=True, timeout=None)` | any other endpoint; returns `(status, body)` |

### Functions, constants and modules

| Name | Purpose |
|---|---|
| `parse_connection(raw)` | parse a connection string into a `ConnSpec` (raises `ConnectionConfigError`) |
| `resolve_connection(connection=None)` | the same, falling back to `BENCHPOD_CONNECTION` |
| `trust_lan_tls(host, port=8443, *, sha256=None)`, `forget_lan_tls(host, port=8443)` | pin the TLS certificate the pod presents now (returns its SHA-256), or drop the pin (see [TLS on the LAN](#tls-on-the-lan)) |
| `sensor_library(version=None, *, cache_dir=None, offline=False, refresh=False, timeout=30.0)` | the sensor library (see [The sensor library](#the-sensor-library)); `SENSOR_LIBRARY_VERSION` is the pinned version |
| `load_definition(source, *, library=None, base_dir=None)`, `load_stimulus(source, *, library=None, base_dir=None)` | a definition or stimulus (name, path, dict or YAML/JSON text) as the dict the pod receives; header problems raise `DefinitionError` |
| `make_build_reporter(*, api_base=None, audience=None, target="", name="")` | a `BuildReporter` inside GitHub Actions, else a `NoopBuildReporter` |
| `build_panel_curve(voc_code, sharpness=4.0, points=256)`, `build_linear_curve(max_code, rising=True, points=256)`, `build_constant_curve(value_code, points=256)` | control-loop curves (see [In-fabric DAC control loop](#in-fabric-dac-control-loop)) |
| `curve_output_at(curve, input_code)`, `input_percent_to_code(percent)` | evaluate a curve the way the gateware does; an input percentage as a code |
| `encode_curve_b64url(codes)` | 16-bit DAC codes as little-endian base64url, the wire form of a curve |
| `INTERNAL`, `EXTERNAL`, `PIN1` … `PIN14`, `BMP280_ADDR_PRIMARY`, `BMP280_ADDR_SECONDARY` | named constants (see [API conventions](#api-conventions-and-stability)) |
| `DacPath`, `DacOutputPath`, `AnalogPath`, `AdcSource`, `CalibrateSource` (`"current_in"`), `DacCalPath` (`"3v3"`, `"5v"`, `"12v"`), `AdcGainSource` (`"cal1"`, `"cal2"`, `"ext"`), `CalibrationPart` (`"offset"`, `"dac"`, `"adc"`), `LoopSource`, `Waveshape`, `ReplayMapping`, `DecodeProtocol`, `CanMode`, `FaultType`, `GpioMode`, `Edge`, `TriggerEdge` | the `Literal` string types |
| `decode`, `i2c`, `sensors`, `can`, `control_loop`, `motor_emulator`, `dsp`, `usb_host` | submodules: off-device decoders, I2C helpers, sensor definitions and the library, CAN types, curve helpers, the emulator's registers, `dsp`, a pure-Python mirror of the server's record-to-replay processing, and `usb_host`, the USB loaders (`MODES`) and the file conversion |

## Examples

* [`examples/test_bmp280.py`](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/examples/test_bmp280.py)
  — flash → emulate a BMP280 → power-cycle → assert on UART and the decoded I2C bus, with a wiring
  table and run command
  ([README](https://github.com/embeddedci-com/embeddedci-python/blob/main/packages/embeddedci/examples/README.md)).
* [`tests/examples/`](https://github.com/embeddedci-com/embeddedci-python/tree/main/packages/embeddedci/tests/examples)
  — a multi-case BMP280 HIL suite, CAN loopback and ECU-simulator demos, and a smoke test.

## Releasing (maintainers)

Releases publish to [PyPI](https://pypi.org/project/embeddedci/) automatically via
`.github/workflows/publish.yml` using PyPI **Trusted Publishing** (OIDC) — no API token or secret is
stored in GitHub.

**One-time setup** on PyPI (or first via [TestPyPI](https://test.pypi.org)): project →
*Settings → Publishing → Add a pending publisher* with owner `embeddedci-com`, repo
`embeddedci-python`, workflow `publish.yml`, environment `pypi`.

This is a monorepo, so each package releases on its **own** tag — `publish.yml` triggers on
`embeddedci-v*` for this package (and `embeddedci-mcp-v*` / `embeddedci-openhtf-v*` for the others).
To cut a release:

```bash
# 1. bump the version in pyproject.toml (e.g. 2.0.0 -> 2.1.0), update CHANGELOG.md, commit
# 2. tag and push — the tag is embeddedci-v<version> and must match the version
git tag embeddedci-v2.1.0
git push origin embeddedci-v2.1.0
```

The tag push builds the sdist + wheel, runs `twine check`, and publishes. The git tag is the source
of truth for what shipped; keep `embeddedci-v<version>` equal to the `version` in `pyproject.toml`.

Any change to the public surface fails `tests/test_api_surface.py` on purpose. Additions are fine in
a minor release; removals, renames and signature changes need a new major version. After reviewing,
refresh the snapshot with `UPDATE_API_SURFACE=1 pytest tests/test_api_surface.py`.

Build and verify locally before tagging:

```bash
python -m pip install build twine
python -m build           # -> dist/embeddedci-*.tar.gz and *.whl
twine check dist/*
```
