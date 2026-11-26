# Changelog — `embeddedci`

## Unreleased

- **`power_on`, `power_off` and `target_power` take `j1_rail="3v3" | "1v8"`**: they switch the supply pins on J1 of the digital board
  v4 (pin 17 +3.3 V, pin 18 +1.8 V; feature `dut_rails`, hardware fact `dut_rails`). Both are on
  from boot and independent of the eFuses and of resets. `TargetStatus.j1_rails` (`J1Rails`:
  `v3v3`, `v1v8`) reports them, `None` on boards where both pins are always on. Older boards and
  firmware are refused with `ValueError` before anything is sent. Needs the firmware change
  `dut_rails` (features table revision 22).
- Docs and messages: the digital board v3r4 is now called v4, and analog module v3r5 is v4 (analog
  module v3r4 keeps its name). `board_rev` may be `"v4"` or `"v3r4"` (firmware 4.0.x): both mean the
  v4 board, and the USB-A port check accepts both. The error catalog and feature map follow the
  firmware change in a later sync.

## 4.0.6

For BenchPod firmware with features table revision 21 (feature `dac_out_cal1` at revision 20,
`prog_named` at 21).

- **`dac_output("cal1", volts=...)` and `generate(dac_path="cal1")`.** The level goes on the 5 V DAC
  stage routed into the ADC loopback (the 5 V output is disconnected meanwhile), so a test can set a
  DAC level and read it back with `adc_read("cal1")` without relays clicking each step. `DacPath` and
  `DacOutputPath` gain `"cal1"`; firmware without feature `dac_out_cal1` is refused with
  `UnsupportedFeatureError`, and `replay` refuses it.
- **The pin programmers' device table is generated from one YAML file per chip** (`prog-devices/`
  in the repository, `scripts/gen_prog_devices.py`). `prog_devices.json` is schema 2: each chip has
  `prog`, its protocol options as named fields (`flash_base`, `cmd_set`, `pc`, `cp_addr`, `cp_bit`,
  `write_words`, `page_erase`, ...), and `legacy`, the same as the packed `flags` integer.
- **`flash(target=...)` sends named chip fields to a pod with the `prog_named` feature** (firmware
  4.0.6 and later) and the packed `flags` and `cp_addr` to an older pod, so every pod gets the same
  options as before. `ProgDevice.opts` still returns the legacy form; `ProgDevice.fields(named)` and
  `prog.start_request(..., named=)` are new. `prog.PIC_ENTRY_MASK` is gone: `prog.pic_entry_styles()`
  returns `{"prog": ..., "flags": ...}` entries.
- Docs: `fpga_image` with firmware 4.0.6 loads the image from its slot in about 0.1 s without
  writing the config flash, skips the image that already runs, and a restarted pod runs the loop
  image. No SDK change.
- **`flash(target="msp430")` programs MSP430 FRAM parts over Spy-Bi-Wire** (protocol `sbw`, also accepted
  as a target). `clk` is the chip's TEST pin (wiring-profile signal `SBWTCK` or `TEST`), `data` its RST
  pin (`SBWTDIO` or `RST`); the pod's reset pin is not used and the pod needs gateware v52 or newer. The
  first device-table entry is the MSP430FR2433 (ID `984082`: JTAG ID 0x98, device ID 0x40 0x82; main
  FRAM 0xC400-0xFFFF, information FRAM 0x1800-0x19FF as `config`); the table says where its
  numbers come from. A JTAG-locked chip answers with its JTAG ID only: name it with `device=` and flash
  with `erase="chip"`. Needs firmware with the `sbw` protocol (4.0.6).
- **`bp.detect()` probes `sbw`** (MSP430 over Spy-Bi-Wire, invasive, vendor Texas Instruments, core MSP430) when the
  profile has `SBWTCK`/`SBW_TCK`/`TEST` and `SBWTDIO`/`SBW_TDIO`/`RST`; no reset pin is used. A chip that answers
  with its 1-byte JTAG ID only is reported as "MSP430 (JTAG-locked)" at low confidence. Skipped with a message on
  a pod older than firmware 4.0.6. `METHODS`, `INVASIVE_METHODS` and the MCP `detect` tool list `sbw`.

## 4.0.5

For BenchPod firmware 4.0.5 (features table revision 19, gateware v68).

- **`bp.detect()` finds out which chip is wired to the pod.** It tries every probe the wiring
  profile makes possible and returns a `DetectResult` (`powered`, `findings`, `skipped`, `notes`,
  `to_dict()`); each `DetectFinding` has `method`, `chip`, `vendor`, `core`, `id`, `confidence`
  (`high`, `medium`, `low`), `pins`, `raw` and `invasive`.
  - `swd` and `jtag` read the debug port's IDs without halting, resetting or writing the chip
    (`dap_start` with `op: "id"`, firmware feature `dap_id`; older firmware is reported in `skipped`)
    and name the part from the Arm core's vendor ID register (an STM32's DEV_ID, a Nordic part) or a
    JTAG IDCODE (including the STM32 boundary-scan TAP). `usb` reads the device already mounted on the
    pod's USB host port (host mode is never turned on).
  - `updi`, `pic`, `c2`, `tpi`, `pdi` (the programming-mode ID) and `esp32` (the ROM loader's chip
    name) reset the chip and run with `invasive=True` (the default); they run only when the profile
    has a signal named for the protocol, never on the SWD pins. `invasive=False` runs `swd`, `jtag`
    and `usb` only; `methods=[...]` limits the probes.
  - Target power off, no LA voltage, pins owned by a UART, GPIO or sensor, a probe error or no answer
    are reported in `notes` / `skipped`, not raised. The ID table is
    `embeddedci/benchpod/detect_ids.json`.
- **`flash(target="pic")` identifies PIC16F18xxx and PIC18 Q chips.** The ID read now uses the chip's
  command set: the named `device=`'s, or without a name each set of the device table in turn. These chips
  never answered the old 6-bit entry, so they could not be identified or flashed.
- **Finds the pod under its own USB id `1209:EC10`** (pid.codes), which the firmware after 4.0.4 uses instead
  of ST's stock `0483:5740`. Both ids work, so `usb`, `usb:<name>` and re-finding a pod after a firmware
  update work before and after the switch. The pod's own id is probed first.
- **The emulation image has no SWD, JTAG, SPI master or I2C master** (gateware v65).
  - `bp.has_bus_masters()` says whether the running image has them.
  - `flash()` over SWD or JTAG, `open_spi()`, `spi_flash()`, `open_i2c()`, `expansion()` and
    `capture_la(send=I2CSend/SpiSend)` switch the pod from the emulation image to the loop image (logged,
    about 3 s) when no I2C, SPI or UART sensor runs, and raise `UnsupportedFeatureError` (`BP0059`) while
    one does: stop the sensors first, or flash before starting them.
  - The `benchpod_capability("spi_master")` and `("i2c_master")` markers do the same switch.
- **`bp.lowlevel.dac_set(code16=...)` holds a 16-bit DAC code** (0..65535). The 8-bit
  `dac_set(code)` still works. With `code16` the request also carries the 8-bit level, so firmware
  before 4.0.5 holds the nearest one.
- `Capabilities.dac_bits` reads 16 from firmware 4.0.5 (it reported 8, the old generator depth).
- **The pod's lease is released when the process exits.** A script that ends without `close()` (for
  example `bp = BenchPod(...); bp.generate(...)`) used to leave the pod's lease held for its full TTL,
  so every other client got `BP0025` for about two minutes. An `atexit` hook now releases the leases
  the process still holds (quick, best effort, never blocks exit), and so does SIGTERM when the
  application has not installed its own handler. A lease joined from `BENCHPOD_LEASE` is still the
  runner's and is not released.

## 4.0.4

For BenchPod firmware 4.0.4 (features table revision 17).

- **Flash chips over their own programming pins.** `bp.flash(target=..., file=...)` now takes
  `avr-updi` (AVR tinyAVR 0/1/2, megaAVR 0, AVR DA/DB/DD), `pic` (PIC16F1xxx, PIC16F18xxx, PIC18F
  Q-series), `efm8` (Silicon Labs EFM8 and C8051F), `attiny-tpi` (ATtiny4/5/9/10/20/40/102/104) and
  `xmega`; the protocol names `updi`, `pic`, `c2`, `tpi` and `pdi` are accepted too. The pod runs the
  protocol itself: the SDK reads the chip's ID, finds it in the new device table
  (`embeddedci/benchpod/prog_devices.json`, documented in `prog_devices.README.md`), cuts a `.hex`,
  `.elf` or `.bin` into the chip's memories (flash, EEPROM, fuses or configuration words, user row or
  user ID), writes them one after the other and reads every page back.
- New `flash()` arguments for these targets: `device` (check against a named chip), `clk`, `data` and
  `reset_pin` (LA pins; default a wiring-profile signal named for the protocol, else `swd_swclk` /
  `swd_swdio`; the reset of `pic` and `attiny-tpi` is the pod's reset pin unless `reset_pin` or an `MCLR` /
  `RESET` signal says otherwise), `erase` (`"chip"` by default, `"page"`, `"none"`) and `dry_run`
  (enter programming mode and read the chip's ID, write nothing). `reset` lets the chip start its
  program afterwards. No wiring-profile fields were added.
- New errors, all `FlashError`s: `ProgFlashError` (with `result`, `phase`, `mem`, `addr`, `at_addr`) and
  its subclasses `ProgNoAnswerError` (also a `TargetUnreachableError`), `ProgLockedError`,
  `ProgVerifyError` and `ProgDeviceError` (chip not in the table, not the `device` named, image does
  not fit, voltage). Each message says what to check. The ATtiny4/5/9/10/20/40 program at 5 V only,
  which the pod cannot give: they fail with `ProgDeviceError` before anything is written.
- `supports("prog_flash")` works; a pod without it raises `UnsupportedFeatureError`. The pin
  programmers run from the pod's `xip_prog` blob, installed with the other blobs (`benchpod
  install-blobs`); without it the pod refuses with `XipUnavailableError`.
- The bundled feature table is revision 17.

## 4.0.4

For BenchPod firmware with the `jtag` feature (features table revision 16, gateware v61 or later).
SWD flashing is unchanged.

- **JTAG flashing.** The wiring profile takes `jtag_tck`, `jtag_tms`, `jtag_tdi`, `jtag_tdo` and an
  optional `jtag_trst` (LA channels 1-14). `jtag_tck` may share a channel with `swd_swclk` and
  `jtag_tms` with `swd_swdio`; every other overlap is an error. `bp.flash()` takes `tck`, `tms`,
  `tdi`, `tdo` and `trst` like `swclk` and `swdio` (omitted ones come from the profile).
- **No transport option: the SDK picks SWD or JTAG.** JTAG when all four JTAG pins are known and
  either no SWD pins are, or the target is JTAG only (`esp32`, `esp32s2`, `gd32vf103`, `fe310`,
  `k210`, names starting with `riscv`); otherwise SWD. A pod without the `jtag` feature raises
  `UnsupportedFeatureError` before anything is armed.
- **`debug_port` in the wiring profile** (`"swd"`, `"jtag"` or `None`, the default: automatic) picks the
  port for a board wired for both. `bp.flash()` decides in this order: pins passed in the call
  win (JTAG pins only: JTAG; SWD pins only: SWD; both kinds or neither: go on), then the profile's
  `debug_port` (an incomplete pin set raises, naming the unset profile fields, e.g. "debug_port is
  jtag but jtag_tdo is not set"), then the automatic rule above. `flash` takes no new argument.
  Passing only JTAG pins now means JTAG even when SWD is wired and the target is an ARM part.
- `Transport.dap_start()` takes `tck`, `tms`, `tdi`, `tdo`, `trst` and `port`; `swclk` and `swdio`
  are optional when the JTAG pins are given.
- The bundled feature table is revision 16.

## 4.0.3

For BenchPod firmware 4.0.3 (features `usb_host_mode`, `usb_flash`, `uart_usb`, `usb_check`, `usb_hid`,
`usb_flash_picoboot` and `usb_flash_readback`, features table revision 15).
Works with firmware 4.0.0 and later; the new calls raise `UnsupportedFeatureError` ("needs firmware
4.0.3") on older firmware.

- **USB host mode.** `bp.usb_host(enable=True, *, port=None, timeout_s=60)` makes the pod a USB host
  for the board under test (`usb_a` on digital v3r4 and later, else `usb_c`; the SDK picks the
  port) and waits until it hosts. `bp.usb_host_status()` returns a typed `UsbHostState` (mode, port,
  attached, enumeration, connected, line speed, and the `UsbDevice` with its configuration and
  interfaces). `bp.usb_wait_device(vid=None, pid=None, timeout=10)` waits for a (matching) device.
- **USB flashing.** `bp.usb_flash(file, *, address=None, name=None, leave=True, mode=None, ...)`
  flashes the board over USB and returns a `UsbFlashResult`. There is no mode to choose: the SDK
  picks `uf2`, `file`, `dfu`, `teensy`, `avr`, `samd` or `esp` from the file and the device that
  mounted, and says why (`result.reason`). `.hex` and `.dfu` files are converted to a binary with
  their start address. It starts host mode if needed, stages the file in the pod, polls with a
  `progress(done, total)` callback and raises `UsbFlashError` (with the pod's `result`) on failure.
  `usb_flash_status()` and `usb_flash_abort()` read and stop a flash.
- **Typed errors for BP0076 to BP0084:** `UsbHostUnavailableError` (BP0076 to BP0078, still an
  `UnsupportedFeatureError`), `UsbHostPortError` (BP0079), `XipUnavailableError` (BP0083) and
  `UsbFlashRefusedError` (BP0084). New client-side errors: `UsbHostError`, `UsbTimeout`,
  `UsbFlashError`.
- **The board's USB serial port is just another argument where you would give pins:**
  `bp.open_uart(port="usb", baud=115200, dtr=True, rts=True)` and `bp.capture_uart(port="usb", ...)`
  return the usual `UartSession` and `UartCapture` (feature `uart_usb`). No LA channel, wiring
  profile or I/O voltage is involved; `rx`, `tx`, `half_duplex`, `entry` and `de` with it are a
  `ValueError`, and a pod without the feature raises `UnsupportedFeatureError` before anything is
  sent. Works over the LAN and the cloud (the serial console refuses, as for any UART).
- **USB checks.** `bp.usb_check(test, ...)` runs one of `enumerate`, `power`, `suspend`, `reset`
  and `replug` against the board's own USB firmware (its VBUS on eFuse 1) and returns a
  `UsbCheckState` (`passed`, `error`, `result`); `usb_check_status()` and `usb_check_abort()`.
- **USB HID.** `bp.usb_hid_interfaces()` (report descriptors, read in pages when long),
  `bp.usb_hid_capture(enable=True)` and `bp.usb_hid_read(count, timeout, instance)`.
- **`usb_flash` grows two loaders' worth of features, still with nothing to choose:** a Pico or
  Pico 2 in BOOTSEL with a `.bin` or `.hex` is flashed through PICOBOOT (`mode` `picoboot`, every
  page read back); `verify=True` reads a drive back (`FAIL.TXT`, `CURRENT.UF2`; `result.note`); a
  `file` flash sends the computer's clock for the file's date; FAT32 and exFAT drives work.
- **Typed errors for BP0085 to BP0088:** `UsbSerialUnavailableError`, `UsbSerialBusyError`
  (a `PodBusyError`), `UsbCheckRefusedError` and `UsbCheckNoPowerError`.
- Bundled features table synced to revision 15 and the error catalog to 88 codes.

## 4.0.2

Works with BenchPod firmware 4.0.0 and later. Firmware 4.0.2 also lets the pod's owner change pairing while another client holds the pod.

- **Connecting by host name is fast again.** The SDK opens a new connection per command and
  looked the host name up every time. On macOS a default lookup of a `.local` name waits about
  5 s for an IPv6 answer an IPv4-only pod never sends, so `BenchPod("benchpod-c2f662.local")` took
  about 50 s (10 lookups) against 0.8 s by IP address. Now a transport looks a name up once,
  IPv4 first (any address family only when there is no IPv4 answer), and reuses the address. A
  connect that fails looks the name up again, so a pod that gets a new DHCP lease is still found.
  The TLS pin in `known_pods.json` stays keyed by the name you gave, and the SNI is unchanged.
- **A pod name works as a connection.** `BenchPod("benchpod-c2f662")` (or `"c2f662"`) finds the
  pod on the LAN over mDNS (`[discovery]` extra), else on USB (`usb:benchpod-c2f662`), as the
  `benchpod` CLI does. Without `[discovery]` it is the host name `benchpod-c2f662.local`.
  `benchpod-c2f662.local` stays a host name. New: `discovery.find_pod(short)`, which stops
  browsing as soon as that pod answers.
- **USB decoding from the logic analyzer.** `bp.decode(la, "usb", dp=, dm=)` (and
  `LaCapture.decode`, `decode.decode_usb`) decodes full speed (12 Mbit/s, capture at 48 MS/s) and
  low speed (1.5 Mbit/s, 6 MS/s and more) from a passive D+/D- tap: packets with CRC5/CRC16,
  bit stuffing and PID checks flagged per packet, transactions (token, data, handshake, repeated
  NAKs merged) and control transfers with the SETUP request and the descriptors read back
  decoded. Returns a `UsbDecode`; locked to the server's decoder by shared vectors.
- **Five more LA decoders:** `modbus` (RTU on the UART decoder: request/response pairing,
  function codes, registers and coils, exceptions, CRC), `can` (CAN 2.0 and ISO CAN FD from one
  channel: bit stuffing, CRC15/17/21, BRS, ACK, error frames), `pwm` (per-period frequency, duty
  and widths, summary, jitter, glitches, servo pulses), `onewire` (reset/presence, overdrive,
  ROM commands and search, CRC8, scratchpad temperature) and `smbus`/`pmbus` (SMBus protocols,
  PEC, PMBus command names, LINEAR11/LINEAR16 values, status bits). All locked to the server's
  decoders by shared vectors.
- CAN FD follows ISO 11898-1:2015 at the end of the data field (no dynamic stuff bit where the
  CRC field's fixed stuff bit stands), accepts a late or two-bit ACK, and re-synchronizes on
  falling edges only, so a transceiver that stretches dominant bits still decodes.

## 4.0.1

For BenchPod firmware 4.0.1. Works with firmware 4.0.0 too.

- **LAN pairing.** Firmware 4.0.1 answers a computer on Ethernet or Wi-Fi only after it is paired
  with the pod. The SDK presents this computer's TLS client key on every LAN connection: the same
  `client.key`/`client.crt` as the `benchpod` CLI (next to `known_pods.json`), made on first use.
  CI runners pass `BENCHPOD_CLIENT_KEY_PEM`, or `BENCHPOD_CLIENT_CERT` and `BENCHPOD_CLIENT_KEY`.
  - `bp.pair()` pairs this computer over the pod's USB cable or the cloud, and pins the pod's LAN
    certificate.
  - `bp.lan_clients()` lists the paired computers; its `add`, `remove` and `clear` arguments change the list
    (USB or cloud only). `bp.set_lan_pairing(False)` turns pairing off.
  - An unpaired computer gets `PodNotPairedError` (`BP0081`), which says how to pair. A full list (32 computers) is `BP0082`.
  - Over the USB console (with the USB network off), `lan_clients` and `lan_tls` still work.
  - New dependency: `cryptography>=41`, to make the key and certificate.
- **`la_pins` reads a long listing in pages.** On firmware with the `la_pins_paged` feature
  (features table revision 12), a reply that cannot carry every pin says `next`; the SDK asks
  again with `"from": next` (and the same `la`) and joins the pins, so `bp.la_pins()` and the MCP
  `la_pins` tool get all 14 pins over the cloud even with a long sensor name on every pin. It
  stops after 16 replies, or when `next` does not move forward. Older firmware is read in two
  halves after `BP0053`/`BP0054`, as before.
- Features table revision 13; error codes `BP0081` and `BP0082`.

## 4.0.0

Pairs with BenchPod firmware 4.0.0 (client API 2). Breaking changes:

- **Firmware 4.0 or later only.** `CLIENT_API` is 2 and every request says so. Connecting to a
  pod with firmware 3.x (client API 1, or no `api` in its status) raises the new
  `FirmwareTooOldError` (an `UnsupportedFeatureError`, `feature="client_api"`), which says to
  update the pod with `benchpod flash-self` or the web app, or to stay on embeddedci 3.x. Over the
  USB console, which announces no level, the firmware version decides. A pod whose
  `min_client_api` is above 2 raises `ClientTooOldError`.
- **Features come from the table only.** The `caps` list and every version fallback are gone:
  `Capabilities` reads `features_rev`, the gateware, the image and the facts from `status` (and
  the cloud `cap.features_rev`, `cap.gateware`, `cap.fpga_image`), never `status` with
  `"caps":false`. `Capabilities.sensors_format` and the `signal` alias for the scope are gone;
  `Transport.status_without_caps()` is gone. `diag()` sends `diag` only.
- **USB console:** firmware 4.0's console answers `status`, `ping` and `identity` only, so the
  serial transport no longer sends `la-voltage`, `power`, `dap-start` or `uart-proxy`; they raise
  a `TransportError` that points at `usb` (the USB network adapter) or a network address. The
  parsed `usb` status line reports `net` as a boolean, as the JSON status does.
- **`generate` sends volts only**: `path` with `amplitude_v`/`offset_v`, or `amplitude_ua`/
  `offset_ua` on `current_out`, `freq_hz` and `sample_rate_hz` in hertz; the pod routes the path.
  The 8-bit form is gone. The MCP `generate` result has no `bits`.
- **Captures take base64 by default:** the SDK no longer asks for `"enc":"b64"`; a transport
  without chunked streaming asks for `"enc":"dec"`.
- Pull resistors use `la_pull` and step trains `la_step` (firmware 4.0 split the `la` command).
  `PullState` reads the 4.0 reply (`state`, `dirs`, `pullups_available` as a boolean); the
  resistor value comes from the board table, `la_pins` reports it on every board.
- **Refusals are classified by code only.** Firmware 4.0 sends a code and its args and no text
  (`BP0030` alone carries one). Every catalog code (`BP0001` to `BP0080`) maps to an exception
  class whose fields come from the args, and the message is the catalog's message and SDK fix. The
  text patterns, the raw-command rewrites (`{"cmd":"gpio",...}` to `bp.release_gpio(4)`) and the
  `la voltage not set` text are gone; a refusal without a code is a plain `FirmwareError` with the
  pod's text, whatever it says. `errors.status_too_large`, `errors.la_pins_too_large` and
  `STATUS_FITS_FIRMWARE` are gone. New classes: `ReplyTooLargeError` (`BP0053`, `BP0054`) and
  `DacPinoutMismatchError` (`BP0080`, `digital`, `module`). `PinConflictError` has `owner` and
  `instance` (`BP0031`). Over the cloud the transport retries on `BP0052` (it matched the text
  "device busy").
- **TLS on the LAN by default.** A network address without a port goes to 8443 over TLS (the
  pod's certificate pinned on first use, as before); the TLS-then-plaintext fallback is gone.
  Plaintext 8080 is used only on the USB network adapter (`10.237.x.y`) or when the address names
  `:8080`; a failed plaintext connect to a LAN address says to drop the port. `connection.TLS_PORT`
  and `connection.default_port(host)` are new; `trust_lan_tls`, `forget_lan_tls` and the pin
  helpers default to port 8443. `connection 'usb'` falls back to the pod's LAN address over TLS.
  mDNS: `DiscoveredPod` has `tls_port`, `api`, `https_only` and `supported`; its `addr` uses the
  TLS port, and `discover` refuses a single pod with firmware 3.x (`FirmwareTooOldError`).
- **Firmware 4.0 JSON names.** The public SDK names stay; the wire follows the firmware: rates in
  hertz (`sample_rate_hz` on capture, la_capture, replay, sensor_la and generate;
  `adc_sample_rate_hz`/`la_sample_rate_hz` on capture_dual and in the chunk replies;
  `sample_rate_hz` for power profiles), `freq_hz`, `clock_hz` (i2c_start, spi_start and their
  replies), `dac_out` `v` and reply `code16` (`DacOutput.code` is now its high byte),
  `code16` in the loop probe and input replies, `can_start`/`can_stop`, CAN `ts_ms`,
  `target_power` `enabled` (booleans in `target_status` too), `usb_network` `enabled` (a boolean
  in and out; `status.usb.enabled` and `status.usb.net` are booleans).
- **Removed** (approved for 4.0): `set_pull(la, True|False)` and `set_pull(..., enabled=)` (pass
  `"up"`, `"down"` or `"off"`; the four shortcuts `enable_pullup`, `disable_pullup`,
  `enable_pulldown`, `disable_pulldown` stay); `wiring.REFERENCE_BENCH` (use `DEFAULT_PINS`),
  `wiring.PRESET_ALIASES` and the preset name `"embeddedci-reference"`, `Wiring.defaults()` (use
  `Wiring.preset()`); `constants.PULL_OHMS`, `PULLUP_CHANNELS`, `PULLDOWN_CHANNELS` (use
  `PULL_TABLE`); `transport.serial.RP_VID`; the `serial` connection keyword (use `usb`); the
  CLI config's legacy `bench_pod_addr`; an LA channel number as `upload_build`'s reset input (a
  flag only); `CanBus.expect(can_id=)` and `read_until(can_id=)` (pass the id as `match`).
- `TriggerTimeout.edge` is `"start"` for an emulated sensor's bus capture (`BP0039`, an I2C START);
  it was `"falling"` from the 3.x text. Over the USB console the firmware version is checked on
  connect, and a network-only call says it is a network command.
- `ModuleInfo.dac_pinout_ok`: `False` when the digital board and the analog module use different
  flat-cable DAC pinouts (the DAC is off).
- Error catalog synced (`BP0031` to `BP0080`); features table revision 11.

## 3.0.6

Never released: its changes ship in 4.0.0.

- **Error codes decide the exception class.** Every code in the error catalog (`BP0001` to
  `BP0030`) maps to one class, and the code's values fill its fields; the text decides only for a
  refusal without a code (older firmware) or with a code newer than this SDK. New `WiringError`
  (`BP0007` to `BP0015`, with `name`). `BP0003` and `BP0004` (an I2C or SPI session is open),
  `BP0005` (the SWD/SPI/I2C engine is busy) and `BP0016` (storage busy) raise `PodBusyError`;
  `BP0017` (gateware too old) raises `UnsupportedFeatureError` with the feature from the code.
  Match on `exc.code` or the class, not the text. README: [Error codes](README.md#error-codes).
- **Client API level.** Every JSON command carries `"client_api": 1` and `"client": "sdk
  <version>"` (firmware 3.10.13 and older ignores them). `feature_map.CLIENT_API` is the level this
  SDK handles; `Capabilities.api`, `min_client_api` and `client_api_level` are the pod's (firmware
  after 3.10.13; a pod that does not say counts as 1). Connecting to a pod above the SDK's level,
  or the pod's refusal `BP0030`, raises the new `ClientTooOldError`: update the SDK with
  `pip install -U embeddedci`.
- Error catalog: `BP0030` (client too old). Features table revision 8, with `api` and
  `min_client_api`.

## 3.0.5

- `la_pins(la=...)` lists only those channels (firmware after 3.10.12 sends only them; older
  firmware sends all 14 and the SDK keeps the ones asked for). A full listing the pod answers with
  "reply too large" is read in two parts by itself; on older firmware, which cannot, the error
  says to update the pod or ask for fewer channels.
- **Digital board v3r4:** `PullState.held` says why a pull that was set is off right now
  (`pin21_down`, `dut_off`, `fault`), and `enabled`/`state` are what is on the wire. `la_dir()` and
  `gpio` entries now report both pull directions there, like `la_pins()`.
- **Digital board v3r4:** `set_la_voltage(5.0)` (and `la_mv: 5000` in a wiring profile) on a pod
  with the `la_5v` feature: the bank stays at 3.3 V, J1 pin 21 (the pull-up rail) goes to 5 V; other
  pods raise `ValueError` before anything is sent. `LaVoltage.pin21` (new `Pin21`: `source`,
  `setting`, `measured`, `state`, `up`, `ok`) reports pin 21 there, `None` elsewhere.
  `Capabilities.vref_out` is the hardware fact. `PullState.strength` reports the resistors the pod
  picked (`10k`, `2.2k`, `both`, `none`); every channel pulls up or down there, and `la_pins()` takes
  the reply's `pull_dirs`. No new parameters: the pod picks the strength from the pin's function.
  Error catalog `BP0028` (5 V did not come up) and `BP0029` (pin 21 is down); features table
  revision 7 (`la_5v`, `la_pull_strength`).
- **4-20 mA input on analog module v3r5** (firmware 3.10.13+): the sense resistor is 100 Ω (249 Ω up
  to v3r4). `AdcReading.sense_ohms` and `Calibration.sense_ohms` give the resistance the pod
  converts with, and `capture_adc(source="current_in")` uses it for `currents` (249 Ω when the
  firmware does not say). On v3r5 the pod reads `current_in` in the low range unless `range` is
  given; the SDK follows that range, and gives it back when a DAC output is routed after it.

## 3.0.4

- **Read test runs back:** `embeddedci.benchpod.runs.api` lists the runs on embeddedci.com
  (`list_runs`, by suite, DUT, build, pod or ref), reads one (`get_run`, `summarize_run`: the
  failed cases and the measurements outside their limits), compares two (`compare_runs` with
  `previous`, a run id or `baseline:<branch>`) and fetches their files (`get_run_file`,
  `case_trace`: a case's pod calls). `RunsApi` takes your own `ServerApi`.
- **Console log level:** `bp.log_level()` reads how much the pod prints on its USB console and
  `bp.set_log_level(level)` sets it (`LOG_LEVELS`: `error`, `warn`, `info`, `debug`; default
  `info`, stored on the pod). Firmware newer than 3.10.12 (feature `log_level`); older firmware
  raises `UnsupportedFeatureError`.
- **Upload test runs:** `--embeddedci-upload` (also the `embeddedci_upload` ini option and
  `EMBEDDEDCI_UPLOAD=1`) uploads the recorded run to embeddedci.com when the session ends:
  `run.json`, `junit.xml` and the traces of the failed and error tests. Credentials:
  `BENCHPOD_API_KEY`, else the `benchpod login` session, else GitHub Actions OIDC. A failed upload
  is a warning. Not under `benchpod ci`, which uploads the run itself.
  `embeddedci.benchpod.runs.upload.upload_run(folder)` uploads a folder later.
- **`build_report` on test runs:** in GitHub Actions a session whose tests use `build_report`
  records its run and uploads it linked to the build the fixture created, so the build shows every
  test's result. The fixture still sends the pass/fail to the build for older servers.
  `BuildReporter.api_base` and `BuildReporter.session_token` are new.
- Run folder file names always pass the server's rule (`[A-Za-z0-9._-]`, no leading dot): a case
  id starting with `.` gets a trace name without it, and attachment names keep ASCII letters only.
- `tests/benchpod-ci.yaml` runs this repository's hardware tests with `benchpod ci`.
- Under `benchpod ci`, a stop of the runner's lease from the web app (`BP0026` on a lease joined
  through `BENCHPOD_LEASE`) ends the pytest session after the test that saw it (exit code 2); the
  step's `parts/<step>.json` still has the cases so far, `lease_lost` and the reason. With the SDK's
  own lease the session goes on without it, as before.
- Test run recording: a failed case's `message` starts with the crash line (`ValueError: bad CRC:
  6f72`), then a blank line and pytest's full report, still capped at 16 KB.
- **Pod lease on the LAN and the USB network:** on a pod with the `lease` feature (features table
  revision 5), `BenchPod()` takes the pod's own lease for the session (`lease_acquire`, holder
  `SDK: <user>`, purpose `lease_purpose=` or the recorded run's suite, else `sdk`; 120 s, renewed
  every 40 s, released on `close()`). Every request then carries `"lease":"<id>"`, which makes it
  the owner's (only sent to pods with the feature). A busy pod is polled every 2 s for up to `lease_wait`, then
  `PodLeasedError` names the `holder`, `purpose` and `left_s`. Several `BenchPod` objects of one
  process share the lease. `lease=False` / `--benchpod-no-lease` skip it; older firmware takes none.
- **`BENCHPOD_LEASE`:** joins that lease id instead of taking a new one (on the cloud, as the
  server lease's id), and never renews or releases it: `benchpod ci` sets it for its child
  processes.
- `BenchPod.local_lease()` returns a `LocalLease` (from `status` `local_lease`); `BenchPod.leased`
  also covers the pod's lease. New `PodLeaseLostError` (`BP0026`, the lease was stopped from the web
  app: raised once, then the session runs without a lease and never takes it again); `PodLeasedError` has `purpose`. The error catalog has `BP0025`-`BP0027`. A test run being
  recorded is marked invalid (`health.lease_lost`) when its lease is stopped.
- **Test run recording:** `pytest --embeddedci-run-dir DIR` (or `EMBEDDEDCI_RUN_DIR`) writes a run
  folder: `run.json` (schema version 1: outcome, counts, every test case with its measurements and
  per-command call timings, and the firmware, tests, pod, wiring and tool versions it ran with),
  `junit.xml`, and a trace of every pod call per test (`trace/`, large payloads in `att/`, secrets
  redacted, 50 MB per run). `--embeddedci-suite`, `--embeddedci-dut` and `--embeddedci-keep-traces`
  (`all`, `failed`, `none`) set the rest. Off by default. Under `benchpod ci` (`EMBEDDEDCI_RUN_DIR`
  with `EMBEDDEDCI_RUN_STEP`) the plugin writes `parts/<step>.json` for the runner to merge.
- **`measure` fixture:** `measure(name, value, unit="", min=None, max=None, drift=None)` records a
  number for the test; a value outside `min`/`max` fails it. Numeric `record_property` values are
  recorded as measurements too.
- USB console: a firmware log line printed right after the prompt (`[sig] SPI1 link at ... for
  gateware 58`, on every `status` up to firmware 3.10.12) no longer makes the serial transport
  time out waiting for the prompt. A prompt followed only by complete `[tag] ...` lines ends the
  reply.
- **Pull resistors at 1.8 V:** on firmware with the `la_pull_1v8` feature (newer than 3.10.12) the
  pod's pulls work with the LA bank at 1.8 V (v3 boards pull up to the bank rail), so `power_on`
  applies a 1.8 V profile's `pulls` and `expansion(pullups="pod")` (also its default) works at
  1.8 V. With older firmware both behave as before, and `power_on` logs that the pulls stay off.
  The `Wiring` warning for `pulls` with `la_mv` 1800 now names the firmware it needs.

## 3.0.3

- The preset of the default pins is now `Wiring.preset("embeddedci-default")` (also the default of
  `Wiring.preset()`), and `wiring.DEFAULT_PINS` is its values: they are the pins the pod used to
  assume, not the wiring of a particular bench. The old names `"embeddedci-reference"` and
  `REFERENCE_BENCH` still work. The values are unchanged, so version 1 profiles mean what they did.
- **Pull resistors by direction:** `BenchPod.set_pull(la, "up" | "down" | "off")` (None is off)
  returns a `PullState` with `state`, `supported` (the directions the channel can pull) and `ohms`;
  the pod picks the resistor. `enable_pullup` / `disable_pullup` / `enable_pulldown` /
  `disable_pulldown` stay as shortcuts. `set_pull(la, True/False)` and `enabled=` still work
  (deprecated: True pulls the channel's own direction). A direction the channel can't pull raises
  `PullDirectionError` (code `BP0024`, also a `ValueError`), up/down on a channel without a resistor
  `BP0023`. Firmware with the `la_pull_dir` feature gets `pull`; older firmware gets `pullup` and the
  SDK checks the direction itself. `pull_state` and `enabled_pulls` cover LA1-LA14.
- **Wiring `pulls` map:** `Wiring.pulls` also takes `{"1": "up", "8": "down"}`; the list of channels
  stays valid and means each channel's own direction. `Wiring.pull_directions()` gives either as
  the map, and `power_on` applies it. `to_dict()` writes the list whenever every pull is its
  channel's own direction, so older readers keep taking it.
- `LaPinState.pull_dirs`: every direction the channel's pull resistor can pull.
- The board's pull resistors are one table, `constants.PULL_TABLE` (`PullHw`: dirs, default,
  ohms); `PULL_OHMS`, `PULLUP_CHANNELS` and `PULLDOWN_CHANNELS` come from it.
- Error catalog and features table synced from benchpod-firmware (BP0023, BP0024, `la_pull_dir`).

## 3.0.2

- **The profile on the pod's header:** `BenchPod.board_header()` says which I/O header the pod has
  (`"j1"`, the 2x11 pin headers up to v3-r2, or `"front"`, the 2x13 front connector of digital v3r3
  and later; asked once per connection with the read-only `module` command), and
  `header_map(wiring, header)` writes the profile on it as text: every pin in its place, then what
  to connect to it.
- **UART frame in the wiring profile:** `Wiring.uart_parity` (`"none"`, `"even"`, `"odd"`) and
  `Wiring.uart_stop_bits` (1, 2), next to `uart_baud`. `open_uart`, `capture_uart` and
  `power_cycle_and_capture` take them from the profile when `parity` / `stop_bits` are omitted
  (8N1 unless the profile says otherwise). The web app and the desktop app set them in Pins.
- `flash(target="esp32...")` switches the FPGA to the deep-replay image before staging the image,
  so a pod left on the emulation or loop image no longer refuses with `load_bin: deep replay is
  not in the emulation image`.
- **One-wire UART and boot-mode entry:** `open_uart` and `capture_uart` take `half_duplex=True`
  (rx and tx on one LA pin; the pod only pulls the line low and does not read back its own bytes)
  and `entry=UartEntry(...)`, a reset with TX held low, its release and the first bytes, all timed
  on the pod. Needs gateware v57 (capability `uart_hd`); the entry also needs the reset pin.
- **RS-485 direction driven by the pod:** `open_uart` and `capture_uart` take `de=` (the LA channel
  on a transceiver's DE + /RE) and `de_active_low=`. The pod switches DE itself, from before the
  first start bit to the end of the last stop bit, so a device that answers within a millisecond
  is heard (DE switched from the host with GPIO calls drops about 25 ms after the request over
  LAN). Needs gateware v58 (feature `uart_de`, features table revision 2).

- **Features from the firmware's features table:** the SDK ships benchpod-firmware's
  `features/features.json` (`embeddedci.benchpod.feature_map`) and reads the pod's status with
  `"caps":false`. Firmware after 3.10.10 answers with `features_rev` instead of its `caps` list,
  and `Capabilities` takes the features from the table (`capabilities.features`,
  `capabilities.fpga_image`, `Capabilities.has()`); older firmware sends its list as before.
  `bp.supports()` also takes table names without a flag of their own (`"la_dir"`, `"i2c_master"`,
  `"diag"`, ...). A pod whose firmware has a newer table than the SDK logs one warning to update it.

## 3.0.1

- **Pull resistors in the wiring profile:** `Wiring.pulls` lists the LA channels whose pull
  resistor is on (`{"pulls": [1, 2]}`). `power_on()` switches exactly those on and the others off,
  after the I/O voltage and before the rail; at 1.8 V, over the serial console, or without the key
  it leaves them alone. The LA7/LA8 pull-down warnings only fire while that pull-down may be on.
- **The I/O voltage comes from the profile:** on connect the SDK sets the pod's I/O voltage from
  the pod's profile (`la_mv`) when the pod has none (no extra reads, never fails the connection,
  never changes a voltage the pod has). Firmware 3.10.10+ also takes it from the profile itself on
  the first LA operation. `la voltage not set` now says the profile sets no voltage and how to set
  it there.
- A pin's direction follows from what it is used for (a signal's `direction`); `dut_dirs` is still
  honored for older profiles, but the web and desktop editors no longer write it.
- `bp.diag()`: the pod's heap, per-task stacks, network-stack and console counters. Firmware 3.10.9
  moved them from `status` to the new `diag` command, so the full status keeps room under the
  2047 bytes a LAN connection takes in one reply; with older firmware `diag()` reads them from
  `status`.
- When a LAN or USB-network status comes back as the short form (the full one did not fit the
  connection), the SDK reads it once more and, when it is still short, logs a warning: the ADC
  calibration and feature flags then come from embeddedci.com, else the board's defaults.

## 3.0.0

A major release: wiring profiles live on the pod, nothing is wired by default, and refusals carry
stable error codes. See "Upgrading to 3.0" below.

- **Wiring profiles stored on the pod** (firmware 3.10.8+): up to 16 named profiles,
  one of them the default, the same over LAN, USB and the cloud. `BenchPod(conn,
  wiring_profile="k66")` (or `BENCHPOD_WIRING_PROFILE`, pytest `--benchpod-wiring-profile`) loads a
  named one, else the pod's default. New: `bp.load_wiring(name)`, `bp.stored_wiring(name)`,
  `bp.wiring_profiles()`, `bp.set_default_wiring(name)`, `bp.delete_wiring(name)`.
  `bp.save_wiring(profile=None, name=None)` stores on the pod over any connection, signed in or
  not; saving the loaded profile refuses (`BP0009`) when someone else saved it meanwhile. A file and
  a profile name together is an error.
- **Nothing is wired by default** (schema version 2). `Wiring()` sets no pin, no I/O voltage
  (`la_mv` None) and no power rail (`efuse` None); a call that needs one says which to pass. The old
  defaults are `Wiring.preset("embeddedci-reference")`; `Wiring.defaults()` returns it. Version 1
  data (or data without `version`) still fills in those values. `Wiring.profile_name` and
  `Wiring.profile_rev` say which stored profile a connection uses.
- **`bp.power_on()` applies the wiring profile first**: its I/O voltage (when the pod has none or
  another one), then its rail, and records the profile on the pod. `power_on(wiring_profile=...)`.
  It never changes the I/O voltage under a powered board.
- **Error codes**: `FirmwareError.code` (`"BP0009"`), `.values` and `.docs_url`, with the message and
  the fix from the bundled catalog; `embeddedci.benchpod.errors.explain(code)`.
- UART parity and stop bits: `open_uart`, `capture_uart` and `power_cycle_and_capture` take
  `parity` (`"none"`, `"even"`, `"odd"`) and `stop_bits` (1, 2), for devices that are not 8N1 (8E1,
  8N2, ...). The pod's I/O is 1.8/3.3 V only: a 5 V UART needs a level shifter, and Modbus RTU on
  RS-485 an external RS-485 transceiver, between the pod and the device. Anything but 8N1 needs firmware 3.10.8+ (gateware v56, new capability
  `uart_frame`); older firmware is refused with `UnsupportedFeatureError` before anything is sent.
  The USB console transport stays 8N1.
- `open_uart(encoding=...)`: how the session turns bytes into text (default `"utf-8"`). Use
  `"latin-1"` for binary protocols: `uart.read().encode("latin-1")` gives the
  received bytes back unchanged.

### Upgrading to 3.0

- A script that relied on the default pins (for example `bp.flash()` with no `swclk`/`swdio` on a
  LAN pod with no profile) now raises and names the missing pin. Save the bench's wiring once,
  `bp.save_wiring(Wiring.preset(), name="default")` for the reference bench, or pass `wiring=`.
- `power_on()` without `efuse` needs a rail in the profile.
- `Wiring.la_voltage`, `.efuse` and `.i2c_address` can be None.
- Match refusals on `exc.code`, not on the message text.

## 2.13.21 (never published: these changes ship in 3.0.0)

- GPIO expanders: `bp.expansion("tca9555", addr=0x20, sda=1, scl=2)` attaches an off-the-shelf I2C
  expander breakout on two LA channels. It checks the LA voltage, probes the chip, puts every pin in
  input and names the pins `EXT1`..`EXTn` (or the names from `names=` or the wiring profile's
  `expansion` object). `expansion()` reports the chip and every pin's mode and level;
  `expansion(None)` detaches. I2C chips: TCA9555/TCA9535, PCA9555/PCA9535, MCP23017,
  PCF8574/PCF8574A/PCF8575 (quasi-bidirectional), TCA6416A and TCA6424A, several on one bus with
  `addr=[...]`. SPI chips (`spi=(sck, mosi, miso, cs)`): MCP23S17 by hardware address, 74HC595
  chains (outputs only, `count=`) and 74HC165 chains (inputs only, `load=` on SH/LD).
  `EXPANSION_CHIPS` lists them.
- The GPIO calls take EXT pins next to LA channels: `gpio`, `gpio_pins`, `configure_gpio`,
  `set_gpio`, `read_gpio`, `wait_for_level`, `release_gpio` and `signal`. A write to several EXT pins
  is one bus transaction per chip. `set_gpio` also takes a list of levels, one per channel.
  Pulses, capture triggers, a capture's `send` and `la_dir` refuse EXT pins with the reason.
- The SDK opens the expander's bus when an EXT pin is used and closes it on its own before
  `flash`, `spi_flash`, `open_i2c`, `open_spi` and `close`; an `open_i2c` session on the same pins is
  shared with the expander; so is an `open_spi` session on the same four pins.
- `SpiSession.closed`, like `I2CMaster.closed`.

## 2.13.20

- `BenchPod("embeddedci:<name>")` uses the `benchpod login` session by default: without an API
  key, a `cloud_user_token` or GitHub Actions, it no longer fails with "cannot mint a GitHub OIDC
  token". The order is the MCP server's: API key, `cloud_user_token`, OIDC inside Actions, then
  the login. With none, the error lists `benchpod login`, an API key and OIDC. The device lease and
  AI Vision cloud sessions (`AIVision("embeddedci:<name>")`) follow the same order.
- Cloud connections ask again when the pod answers "device busy". The pod runs one cloud command
  at a time and refuses a second one with that error before running it, so a command that met a
  server push or a browser's power poll failed for no reason. It now waits 0.3 s and retries, for
  up to 15 s. Other "busy" errors (a capture running, the SWD probe in use) are raised as before.
- `adc_read()` reads to the microvolt on firmware 3.10.7 and later (the reply's new `uv`).
  `AdcReading.voltage` used whole millivolts, which rounded away most of what the low range
  resolves (about 167 µV per count). Older firmware still gives millivolts.

## 2.13.19

- `flash()` flashes an ESP32-family target (`target="esp32"`, `"esp32s3"`, `"ESP32-C3"`, ...) over
  its UART ROM bootloader, through the pod: no esptool and no USB cable to the target. `file` is a
  `.bin` (placed at `load_address`, default 0) or an ESP-IDF `flasher_args.json` (every region and
  its flash size). New arguments `rx`, `tx` (the target's TXD0 and RXD0) and `boot` (IO0, IO9 on the
  C3/C6); they default to the wiring profile's `uart_rx` / `uart_tx` and a `BOOT` / `IO0` / `IO9`
  signal. EN goes on the pod's reset pin (`nreset=True`) or an `EN` signal. Written at 460800 baud
  and MD5-verified by the chip. Needs firmware 3.10.7.

## 2.13.18

- Security: a redirect to another host (or from https to http) no longer carries the API key,
  session token, lease or cookies along. Every SDK call to embeddedci.com and AI Vision goes
  through one opener that drops them; nothing redirects today, so this guards the future.

## 2.13.17

- Security: `flash()` puts the firmware path in braces in OpenOCD's `program` command and refuses a
  `load_address` that is not a number, so a file name or address like `[exec ...]` can no longer
  run a command through OpenOCD's Tcl. Windows paths are passed with forward slashes. A path
  holding `{` or `}` is refused.

## 2.13.16

- `open_i2c()` refused as busy names who holds the I2C master. When another connection has it
  open (the desktop app, the web app, an AI agent or another script) the error says so, with its
  pins and rate from `i2c_status`, instead of telling this script to close an `open_i2c()` session
  it never opened. When this `BenchPod` still has its own session open, the error says to close
  that one. `I2CMaster.closed` tells whether `close()` ran.
- Closing a `CanBus` (`close()`, or leaving its `with` block) also switches the 120 Ω termination
  off (`can_term(False)`). Before, a bus opened with `term=True` left the termination in after
  close. Firmware 3.10.6 does this in `can_disable` as well.

## 2.13.15

- A pod whose `status` is too large for the cloud channel (firmware 3.10.4 with the USB network
  adapter up and a lease held) no longer leaves the SDK guessing: the error names the fix,
  firmware 3.10.5, and `bp.capabilities` uses the server's capability set instead of reading the
  status again on every access. Firmware 3.10.5 sends a short `status` over the cloud
  (`"short_form": true`); it leaves out `lan_tls` (use `bp.lan_tls()`), `lease`, the diagnostics
  and the fixed feature flags the server already has.

## 2.13.14

- Pin directions are on by default. On a pod with firmware 3.10.4+ the SDK sends the wiring
  profile's directions with `la_dir` when it connects and whenever `bp.wiring` changes, and marks
  the pins the profile doesn't name `unknown`; older firmware is skipped quietly. The connection
  never fails over it: `bp.pin_directions_status` is `"sent"` or `"not sent: <why>"`.
- Every wired role and signal has a DUT direction. New defaults: `swd_swclk` is a DUT input,
  `swd_swdio` is `bidir` (SWD pins were left out before). The profile's new `dut_dirs` key
  overrides any role or signal (`{"uart_rx": "bidir", "READY": "dut_input"}`). Old profiles stay
  valid. `Wiring.dut_dir(name)`, `Wiring.pin_dut_dirs()`; `describe()` shows the direction.
- `apply_pin_directions()` sends DUT outputs last, so a driven pin refusing its mark leaves the
  other directions in place.
- `Capabilities.la_dir`: the firmware takes pin directions (3.10.4+).

## 2.13.13

- The pod as a bus master (gateware v55+). `bp.open_i2c(sda, scl, *, hz=100_000, pullups="pod")`
  returns an `I2CMaster`: `write()`, `read()`, `write_read()`, `read_reg()`, `write_reg()`,
  `probe()`, `scan()`, `xfer()` (an `I2CResult`) and `close()`, also as a context manager. A NACK
  raises `I2CNackError` with `addr` and `nack_at`. Pins take wiring names.
- `open_spi()` takes modes 1 and 2 on gateware v55+; older gateware raises
  `UnsupportedFeatureError` before anything is sent.
- Send and capture: `capture_la(..., send=I2CSend(...) | SpiSend(...) | GpioSend(...))` runs one
  transaction right after the capture arms, in the same command (over the cloud too). The result
  is `LaCapture.send_result` (a `SendResult`). With an edge trigger and no `pre_samples`, a send
  keeps 64 samples from before the trigger, so the decoder sees the first START.
- Pin directions: `bp.la_dir(la, dir)`, `bp.pin_directions()` (from the wiring profile) and
  `bp.apply_pin_directions()`. A push-pull driver on a DUT output raises
  `DirectionConflictError`. `LaPinState` has `dir`, `warn` and `warnings`.
- `Capabilities.gateware` (the running gateware version) and `Capabilities.bus_master`.
- Pod refusals about the I2C master and pull-ups name the SDK call (`bp.open_i2c()`,
  `bp.enable_pullup(1)`); "needs gateware vNN" refusals raise `UnsupportedFeatureError`.

## 2.13.12

- `Sensor.capture()` / `i2c_transactions()` (and `SensorEmulation.capture()`) wait for the DUT's
  next I2C transaction: the pod starts the capture on the bus's START, with 1/8 of the samples from
  before it, so a DUT that reads its sensor every 200 ms is caught every time. `timeout_s`
  (default 2.0) bounds the wait; a quiet bus raises `TriggerTimeout`. `wait_for_traffic=False`
  captures at once. This needs pod firmware with the new `sensor_la_trigger` capability
  (`Capabilities.sensor_la_trigger`); older firmware keeps capturing at once, as before.

## 2.13.11

- `Sensor.capture()` / `i2c_transactions()` (and `SensorEmulation.capture()`) take
  `sample_rate_hz`, 500 kHz by default. Without a rate the pod captured at its highest LA rate,
  well under a millisecond, too short for one I2C transaction. The capture starts at once: lower
  the rate for a longer window.

## 2.13.10

- User-defined sensors (pod firmware with the `sensors` capability) replace the built-in sensor
  models and the GPS receiver. `bp.sensors` (`SensorEmulation`) sends the pod a sensor definition
  (YAML or JSON, the format of github.com/embeddedci-com/benchpod-sensors) and plays it: I2C, SPI,
  UART and analog sensors, several at once, each a named instance. `start()` returns a `Sensor`
  handle with `set()`, `fault()`, `clear_fault()`, `status()`, `regs()`, `capture()`,
  `i2c_transactions()` and `stop()`; `define()`, `validate()` (a dry-run compile on the pod),
  `load_stimulus()`, `start_stimulus()`, `stop_stimulus()` and `status()` complete it. Definitions
  come from a library name, a file, a dict or YAML/JSON text, read with the YAML 1.2 rules
  (`load_definition`, `load_stimulus`, `DefinitionError`; PyYAML with the new `yaml` extra, else a
  built-in reader). Uploads are chunked so each command fits a cloud command (768 raw bytes at
  most). Instance names are 1 to 16 characters; `status()` reads every page the pod sends;
  `regs()` reads one base64 reply (`start` 0..255); `capture()` is for I2C sensors only; `stop()`
  returns the names the pod stopped. `control_loop()` and the deep replays switch back from the
  emulation image (`FpgaImage.EMULATION`) once no I2C, SPI or UART sensor runs, and raise naming
  `bp.sensors.stop()` while one does.
- The sensor library: `sensor_library()` and `SENSOR_LIBRARY_VERSION` (`0.1.0`, bundled in the
  package). `latest` or another release via an argument, `BENCHPOD_SENSOR_LIBRARY` or
  `sensor_library` in benchpod-cli's config; downloads are cached per version
  (`BENCHPOD_SENSOR_CACHE`), with the cache and then the bundled copy used offline.
- pytest: the `benchpod_sensors` fixture (stops every sensor and the stimulus at teardown) and
  `--benchpod-sensor-library`.
- `Capabilities.sensors`, `Capabilities.sensors_format` and `Capabilities.sensors_digital` (the
  emulation image is installed or running).
- Removed, with no compatibility layer: `enable_i2c_sensor`, `set_i2c_sensor`,
  `i2c_sensor_types`, `disable_i2c_sensor`, `i2c_sensor_status`, `i2c_sensor_regs`,
  `i2c_sensor_capture`, `enable_gps`, `set_gps`, `disable_gps`, `gps_status`, the `Sensor` enum
  (the name is now the handle class), `Capabilities.sensor_types` and `Capabilities.gps`, and the
  `benchpod_sensor` fixture. A pod without the `sensors` capability raises
  `UnsupportedFeatureError`: "this pod's firmware is too old for sensor emulation; update the
  firmware".

## 2.13.9

- `i2c.timed_transactions(words, sda_ch, scl_ch, sample_rate_hz)`: I2C from an LA capture with
  the time of every START, address, data byte, ACK/NACK and STOP, in the shape the EmbeddedCI
  capture view draws (the server's decoder gives the same).
- CAN FD (pod firmware 3.10.2+, capability `can_fd`): `open_can` and `can_config` take
  `data_bitrate`, which makes the bus CAN FD with bit-rate switching above `bitrate` (exact rates up
  to 8 Mbit/s, for example 2 or 5 Mbit/s). `fd=True` without it is CAN FD at `bitrate`. Classic CAN
  stays the default. `can_write`, `CanBus.write`, `can_respond` and `add_responder` take up to 64
  bytes and `fd`/`brs` (`reply_fd`/`reply_brs`); `CanFrame` has `fd` and `brs`, and
  `CanReadResult.pending` counts frames still queued (`CanBus` reads again while it is above 0).
  More than 8 bytes on a pod without `can_fd` raises `UnsupportedFeatureError` instead of
  `ValueError`; more than 64 raises `ValueError`. `can_config` no longer sends `fd`, which the
  firmware never used.

## 2.13.8

- `embeddedci.aivision`: client for the AI Vision board (ai-vision-2), standard library only.
  `AIVision` covers the board's v1 API: calibration, frames and zoom, OCR, find, detect, compare,
  identify, screen health, waits and checks, references, models, actuators (`press`, `tap`,
  `swipe`, `press_key`), recordings, evidence and the lease. mDNS `discover()` with the
  `discovery` extra. pytest: the `aivision` fixture and `--aivision`, `--aivision-token`,
  `--aivision-lease`, `--aivision-artifacts`; a failing test saves the screen it saw.
  See `docs/aivision.md`.
- `embeddedci.aivision` over embeddedci.com: `AIVision("embeddedci:<name>")` reaches a paired board
  from anywhere, with the BenchPod SDK's sign-in (API key, `benchpod login`, GitHub Actions OIDC)
  and embeddedci.com's device lease. `list_boards()` lists the organization's boards;
  `register_cloud(name)`, `cloud()` and `unpair_cloud()` pair a board from its LAN.

## 2.13.7

- `capture_la` goes up to 48 MS/s with gateware v50 (firmware 3.9.6+): above 12 MS/s the pod records
  only the changes and replies in the usual form, so the call is unchanged.
- `Trigger(la, edge, pre_samples=N)`: `capture_la` also returns up to N samples from before the
  trigger (gateware v50+); `LaCapture.pre_samples` says how many it holds.
- `LaCapture.truncated`: `"busy"` or `"memory"` when a capture above 12 MS/s stopped early.

## 2.13.6

- `BenchPod.capture_adc(..., oversample=True)` converts at 400 kS/s and averages down to the requested
  rate, for lower noise and a smaller step at 0 V. New `Capture.oversample`, `Capture.mean_counts` and
  `Capture.clipped_conversions`. Limited by the pod's capture memory, about 5 s.

## 2.13.5

- `BenchPod.store_adc_compensation(hf_dc_gain, c_top_pf)` records the ADC input's measured frequency
  response on the pod (firmware 3.9.5+), and `Calibration.adc_comp` (`AdcCompensation`) reads it.
  Reference only: no reading is scaled with it.

## 2.13.4

- The ADC input range (pod firmware 3.9.5+, analog module v3r4, `Capabilities.adc_low_range`):
  `BenchPod.adc_range(range=None)` reads or switches it (`"normal"` ±32.8 V, `"low"` ±5.46 V at
  about 6x the resolution), `adc_range_test()` self-tests it through the cal1 loopback, and
  `analog_path`, `adc_read`, `capture_adc` take `range=`. `calibrate_adc_gain("cal1", range="low")`
  measures the low range's gain (`Calibration.adc_low_gain_ppm`). Captures scale with the fit of
  the range in use: the SDK re-reads it whenever a reply says the pod changed range.
  `AdcReading` gains `range`, `fullscale`, `overrange`; `Capture` gains `range`, `overrange`,
  `clipped_samples`; `AnalogPathState.range`; `Capabilities.adc_range`, `adc_range_fullscale`.
  New types `AdcRange`, `AdcRangeState`, `AdcRangeTest`, `AdcRangeTestPoint`.
- `capture_adc(source="current_in")` reads the pod's `current_in` fit after routing, so it is the
  fit of the range in use.

## 2.13.3

- The DMM-guided calibration (pod firmware 3.9.4+, `Capabilities.cal_dmm`):
  `BenchPod.calibrate_dac(path, points)` stores this pod's own line for a DAC output from two
  `(code16, volts)` DMM readings, `calibrate_adc_gain(source)` measures an ADC gain (`cal1`,
  `cal2`, `ext`) through a loopback, and `clear_calibration(part)` clears one part (`offset`,
  `dac`, `adc`). `Calibration` gains `dac` (`DacLine`), `adc_gain_ppm`, `dmm` and `measured`;
  `BenchPod.DMM_SET_POINTS` has the set points. New types `DacCalPath`, `AdcGainSource`,
  `CalibrationPart`. `Calibration.zero_count` is the raw J8-open zero the pod derives its offset from.

## 2.13.2

- Docs: `calibrate()` corrects every ADC source from firmware 3.9.3 (they share the front end) and
  the pod accepts an offset up to 150 mV. On the LAN, `capture_adc` volts use the fit the pod now
  reports in `status`, so they include the pod's calibrated offset.
- The LAN API over TLS (pod firmware 3.9.3+): a network address on the default port connects to
  8443 with TLS when the pod has it and falls back to plaintext 8080 only when 8443 refuses and
  nothing is pinned. The pod's certificate is pinned on first use in `known_pods.json`, shared with
  `benchpod-cli`; a different one raises `TlsPinMismatchError`. `BENCHPOD_TLS` (auto/on/off) and
  `BENCHPOD_TLS_CA` (verify against a CA instead). New: `BenchPod.lan_tls()`,
  `set_lan_tls_https_only()`, `regenerate_lan_tls()`, `set_lan_tls_cert()`, `LanTls`,
  `trust_lan_tls()`, `forget_lan_tls()`, `TcpTransport.last_tls`, `TcpTransport.command_sequence()`.
  The transport keeps the last TLS session per pod, so later connections resume in ~35 ms instead
  of the pod's ~0.46 s full handshake.
  The 8443 probe waits 1.5 s, past the host's 1 s SYN retransmit (a pod can leave the first SYN
  unanswered), and a probe that times out is remembered like a refusal, so a pod without LAN TLS
  costs one probe per process instead of 0.5 s on every command.

## 2.13.1

- `Capabilities.scope` is False on the digital-only board. Current firmware lists `signal` on every
  board; it only stands for the scope on firmware without the `analog` flag.
- `generate`, `replay`, `Waveforms.preview` and `replay_waveform` (for a waveform stored without
  a path) default to `dac_path="3v3"` instead of `"5v"`, so a call that leaves the output out can
  no longer put 5 V on a 3.3 V pin. Pass `dac_path="5v"` to keep the old output. With
  `route=False` (a loopback set up with `analog_path`), `generate` and `replay` scale for the
  output this connection last routed (`cal1` = 5 V, `cal2` = 12 V), else `5v` as before.

## 2.13.0

- `generate()` on firmware 3.9.1+ (`Capabilities.generate16`) sends the volts (or microamps on
  `current_out`) and the pod plays the waveform calibrated at the DAC's full 16 bits, instead of
  8-bit levels (256 steps: about 13 mV on `3v3`, 63 µA on the 4-20 mA output). Both peaks must be
  on the path. Older firmware keeps the 8-bit levels. `route=False` still keeps the analog
  switching.
- `@pytest.mark.benchpod_capability("analog")` skips a test on the digital-only board and runs it
  on firmware that does not announce the flag, as `BenchPod.can_analog` does (it used to skip
  there too).
- On the digital-only board, `current_out` and `calibrate` raise `UnsupportedFeatureError` that
  names the missing analog front end (`feature="analog"`) instead of asking for newer firmware.

## 2.12.0

- Firmware 3.9.0 keeps ST's USB id `0483:5740` (pre-release builds used `1209:0001`); auto-detect
  ranks both vendor ids.
- `BenchPod.usb_network(on=None)`: read the pod's USB network setting (firmware 3.9.0+), or turn
  the adapter off over the USB link (`False`). Ethernet, Wi-Fi and the cloud get
  `PodLockedError`; turning it back on is `benchpod usb-network on` on the USB console.
- Connection `usb` finds the pod from this computer's own end of its USB network link (Linux and
  macOS, `getifaddrs`) before opening the console, so it works for a Linux user outside the
  `dialout` group; `usb:<name>` picks among several links by identity. `TcpTransport.usb_net`
  marks a USB network transport.
- On firmware 3.9.0 and later the USB console no longer has `la-voltage` or `power` (they are JSON
  commands, reached over the USB network adapter): a serial device path then fails with an error
  that says to connect with `usb` or the pod's network address (the console answers those with a
  "moved to the network API" line, which the SDK recognizes).
- The serial transport no longer probes the console for a `json` mode (up to 3 s per open) or
  carries the JSON-over-serial code: the STM32 pod's console never had that mode (it was the retired
  RP2350 firmware's). JSON goes over the network, also over USB through the USB network adapter.
  `SerialTransport.json_supported` is always `False`; `samples` and `stream_chunks` over the
  console raise a `TransportError` naming the network ways in.
- README: a compatibility table (SDK version x firmware generation) and the Windows 10 note.
- Connection `usb` on firmware 3.9.0+ whose USB networking is off (Wi-Fi configured) or down now
  uses the pod's Ethernet or Wi-Fi address from its console `status` when it answers on port 8080
  (its console no longer has LA voltage or power), with a warning that names the address; the new
  `TcpTransport.usb_lan_fallback` says why. Firmware before 3.9.0 keeps the console.
- Connection `usb` (and `serial`) uses the pod's USB network adapter on firmware 3.9.0 and later:
  the SDK finds the pod's USB console as before, reads the pod's address on the USB link
  (`10.237.x.y`) from its `status`, and when the link is up and port 8080 answers it returns a
  `TcpTransport` to that address, so the full API works over USB. The transport's new
  `usb_console` attribute names the console's device path. It falls back to the console (status,
  ping, LA voltage, power) on older firmware, when Wi-Fi is configured on the pod (that turns USB
  networking off), when the link is down, or when the computer has not configured the adapter. An
  explicit device path (`/dev/ttyACM0`, `COM3`) always stays the console.
- New connection form `usb:<name>` for one of several pods plugged into one computer:
  `usb:baea06`, `usb:benchpod-baea06` or `usb:BenchPod baea06` (any case, optional `.local`) all
  pick the pod whose identity short id is `baea06` (`ConnSpec.pod`). Auto-detect now reads each
  pod's identity from its console (`identity` command; new `SerialTransport.identity()`). With
  a name it opens that pod, else raises `ConnectionConfigError` listing the pods found. Without a
  name, after the first pod it keeps probing the ports with a pod's USB vendor id: two different
  pods raise `ConnectionConfigError` listing both, and the same pod seen on two ports (macOS
  `/dev/cu.*` and `/dev/tty.*`) counts once. The selected pod then upgrades to its USB network
  as with `usb`. `autodetect_port(name="")` takes the name.
- USB auto-detect ranks the pre-release 3.9.0 USB id `1209:0001` with the older pod ids; a pod
  is still identified by probing its console.
- The console's text `status` parses the `usb` line into the JSON status's shape:
  `{"net": "on"|"off", "link": bool, "ip": ..., "host_ip": ...}`.
- mDNS discovery (`connection="discover"`, `discover()`) keeps every address a pod answers from
  (it advertises on each of its interfaces) and `DiscoveredPod.addr` prefers the pod's USB network
  link (`10.237.0.0/16`) over the LAN when this computer hears it there. New `DiscoveredPod.on_usb`.

## 2.11.0

- `flash` can flash a build uploaded to embeddedci.com (the `upload-artifact` action or the
  `build_report` fixture) instead of a local file: `flash(build="<build id>" | "latest", repo=,
  ref=, artifact=)`. On a cloud connection embeddedci.com runs OpenOCD (no local install); on a LAN
  or USB connection the file is downloaded and flashed by the local OpenOCD. `server_side=` picks
  one explicitly. New `FlashResult.artifact` (which build was flashed) and `ServerApi` methods
  `build_artifact`, `download_build_artifact`, `flash_build`; `ServerApi.request` takes `timeout=`.
  Needs the embeddedci.com server from the same release.
- `dac_output` reads the full 16-bit DAC level on firmware 3.8.1 and later: `DacOutput.voltage` has
  microvolt resolution (the pod's new `uv` field) and the new `DacOutput.code16` is the 16-bit
  code. `code` stays the nearest 8-bit level; on older firmware `code16` is `None`.

## 2.10.0

- One wiring profile everywhere. A LAN or USB `BenchPod` now uses the wiring profile stored on
  embeddedci.com for the pod (the web UI's Wiring tab), as a cloud connection already did, when
  credentials are available (`api_key=` / `BENCHPOD_API_KEY`, `cloud_user_token=`, or the
  `benchpod login` session) and the pod reports (`cloud_status`) a device id on that account;
  `bp.wiring.source` is then `"server"`. The `wiring=` argument, `--benchpod-wiring`, the
  `benchpod_wiring` fixture and `BENCHPOD_WIRING` still win. Any failure (no credentials, pod not
  registered, server unreachable, HTTP 404, another account) logs why and uses the defaults; it
  never raises and gives up after about 3 seconds. (The STM32 pod's USB text console has no
  `cloud_status`, so over USB the defaults still apply there.) New `wiring_source=` argument and
  `BENCHPOD_WIRING_SOURCE` variable: `"local"` skips the embeddedci.com lookup on every
  connection kind, `"auto"` is the default.
  **Behavior change:** a signed-in developer (or a run with `BENCHPOD_API_KEY`) whose LAN or USB
  pod is registered and has a stored profile now gets that profile instead of the defaults, and
  the first `bp.wiring` access on such a connection makes one `cloud_status` call to the pod and
  one request to embeddedci.com. Set `BENCHPOD_WIRING_SOURCE=local` for the old behavior.
- One saved connection. `BenchPod()` with no `connection` argument and no `BENCHPOD_CONNECTION`
  now uses the default connection `benchpod-cli` saved (`benchpod set-connection`,
  `benchpod discover --save`) in `~/.config/benchpod-cli/config.json` (under `$XDG_CONFIG_HOME`
  when set). Precedence: argument, then `BENCHPOD_CONNECTION`, then the saved connection. The file
  is only read; a missing or malformed one counts as nothing saved. The connection string `saved`
  names it explicitly. **Behavior change:** `BenchPod()` used to raise `ConnectionConfigError`
  here; it now connects to the saved pod when there is one.
- The pytest plugin keeps its behavior: without `--benchpod-connection`, the ini option or
  `BENCHPOD_CONNECTION` the hardware fixtures skip, and the CLI's saved connection is never used
  implicitly, so a suite does not start driving hardware on a developer's machine. Opt in with
  `--benchpod-connection=saved`.
- New modules `embeddedci.benchpod.cli_config` (`saved_connection()`, `config_path()`) and
  `embeddedci.benchpod.cli_login` (the `benchpod login` session, as the MCP server reads it).
- Friendlier pod refusals: a `FirmwareError` whose fix the firmware spells as a raw protocol
  command now names the SDK call instead. "la voltage not set" says to set the board's I/O voltage
  with `BenchPod(conn, la_voltage=3.3)`, the `benchpod_la_voltage` fixture or
  `bp.set_la_voltage(3.3)`; a `PinConflictError` says `release it with bp.release_gpio(4)`,
  `bp.disable_i2c_sensor()`, `bp.disable_gps()` or to close the UART or SPI session; a
  `PullConflictError` says `bp.set_pull(7, False)`. The error classes, their attributes and the
  leading words of each message are unchanged, and `firmware_message` still holds the pod's own
  text. Hints the SDK does not know pass through as before.

## 2.9.0

- More emulated I2C sensors: `Sensor.BME280` (temperature, pressure, humidity), `Sensor.SHT4X`
  (temperature, humidity) and `Sensor.MPU6050` (accelerometer, gyroscope, die temperature).
  `enable_i2c_sensor` and `set_i2c_sensor` take any model's readings as keywords
  (`set_i2c_sensor(humidity_pct=80)`, `accel_z_g=-1.0`); `i2c_sensor_types()` lists each model's
  address and readings. An omitted `address` is now the model's default (the wiring profile's
  `i2c_addr` for the BMP280/BME280, as before). Needs firmware with the `sensor_types` capability.
- Emulated GPS receiver: `enable_gps`, `set_gps`, `disable_gps`, `gps_status`: NMEA sentences
  (RMC VTG GGA GSA GSV GLL) on an LA channel for the DUT's UART, from the pod's second UART, so
  `open_uart` keeps the console. Needs gateware v48 (the `gps` capability).
- `Capabilities.sensor_types` and `Capabilities.gps`.

## 2.8.0

- `UnsupportedFeatureError` (with `feature` and `firmware_version`): the pod cannot do what was
  asked. Raised before anything is sent when the pod's capabilities lack the feature (it used to
  be a plain `BenchPodError`), and for the pod's own `unknown cmd` and the digital board's "no
  analog front end" refusal (they used to be plain `FirmwareError`s). It subclasses
  `FirmwareError`, so existing handlers still catch it.
- Capability checks on `BenchPod`: `supports(flag)` for any `Capabilities` flag, and the
  properties `can_gpio`, `can_trigger`, `can_spi`, `can_profile_power`, `can_calibrate`,
  `can_current_out`, `can_reset_target` and `can_analog`, e.g. to skip a test on a pod without the
  feature. (`can_config` … `can_disable` remain the CAN bus commands.)
- pytest plugin: `--benchpod-api-key`, `--benchpod-api-base` and the lease options also reach the
  connection the `BENCHPOD_LIFT_DAC_LIMITS=1` session fixture opens. A cloud run authenticated
  only by the flag used to fail to lift (and restore) the DAC limits there.

## 2.7.1

- Over the cloud, a UART console (`open_uart`) can stay open while a capture, power profile or
  DAC replay runs, as on the LAN. Each streaming tunnel tells the server what it is for
  (`?kind=uart|capture|dac|flash`). A server without kind support still answers HTTP 409
  ("device is busy") for the second tunnel, as before.

## 2.7.0

- `UartSession.write` on a link that is gone (a reset connection, an unplugged pod, a closed
  cloud tunnel) raises `UartLinkError` instead of the raw `BrokenPipeError`/`OSError`.
- Typed refusals. `PodLockedError`: the pod's LAN policy keeps this command for the cloud or the
  USB console (`locked: …`, and a policy change sent over the LAN). `PodLeasedError` (with
  `holder`, `left_s` and, from the server, `expires_at`): a cloud job holds the pod (`busy: a
  cloud job holds this pod (…)` on the LAN, HTTP 409 from the server). `PodBusyError`: any other
  `busy: …`. `PermissionDeniedError` (with `status`): the pod's `forbidden: …` on a cloud tunnel
  and a server HTTP 403 (`ServerPermissionDeniedError` from `ServerApi`). An HTML 403 from the
  Cloudflare edge stays a `TransportError`. Every new class also subclasses what the refusal used
  to raise (`FirmwareError`, `TransportError`, `DeviceBusyError`, `ServerApiError`), so existing
  handlers still catch them. Refusals are classified wherever they arrive, not only in
  `BenchPod.command`.
- `Capabilities` parses every flag firmware 3.6 and the server announce: `analog` (`None` when not
  announced), `dac_limits`, `flash_kb`, `blob_slots`, `ota_sig`, `sig_policy`, `sig_keys`,
  `sig_policy_cmd`, `lan_policy`, `lan_policy_cmd`, `tunnel_max_tier`, `lease_state`, `cloud_ca`,
  `cloud_proxy` and (server only) `ws_auth_v2`.
- Errors: a socket or serial failure no longer escapes as a raw `OSError`. A pod that does not
  answer in time raises `TransportTimeout` (a `TransportError`, and still a `TimeoutError`); a
  connection that ends or resets before the reply raises `ConnectionClosedError` (a
  `TransportError`). Over the cloud a slow pod used to look like a closed connection; it is now
  a timeout too, and a lost connection to the server during a command is a
  `ConnectionClosedError` instead of a raw exception.
- Timeouts follow the request: a blocking `measure_power(duration)` waits for its duration on top
  of the connection timeout (it used to fail after the fixed 30 s), and so do a triggered
  capture's `trigger_timeout` and a `dap_start` wait. The cloud command channel asks the server
  to wait that long too (the server caps it at 120 s).
- The handshake that switches a connection to raw mode (`dap_start`, `uart_proxy_start`) is
  bounded by the timeout. A pod that never acknowledged it used to hang the caller.
- `BenchPod.capabilities` is not cached when the status read fails, so a passing glitch no longer
  leaves the whole session without capabilities. The failure is logged.
- `UartSession`: when the link dies with an error (the connection drops, the pod is unplugged),
  `expect()` and `read_until()` raise `UartLinkError` at once (a `UartTimeout` with the text so
  far and the `cause`), `read()` raises it once nothing is unread, and `UartSession.error` says
  why. They used to wait out the full timeout as if the DUT were quiet. A link closed with
  `close()` is not an error.
- Serial: stale bytes (a reply to a request that timed out) are dropped before each JSON request,
  so they can no longer be taken as the next command's reply.
- Cloud: after a Cloudflare edge error (502/503/504/52x) the command channel repeats only commands
  that read (`status`, `ping`, `target_status`, `power_status`, the read form of `la_voltage`,
  `dac_limits`, `calibrate`, ...). It used to repeat everything except a few target actions, so
  an `spi_xfer`, a `generate` or a delayed `target_power` could run twice. A command the server
  refused before forwarding ("retry shortly") is still repeated, whatever it is.
- `la_step`: the docs said `delay` is the time between pulses; it is half of it (each pulse is
  high for `delay`, then low for `delay`). `steps` above 65535 and a `delay` outside 4 µs..65.535 ms
  now raise `ValueError` before anything is sent: the pod's counters are 16-bit and it refused
  them with a bare "invalid args". `GpioPin.pulse` documents the same bounds.

## 2.6.0

- Cloud: the tunnel WebSocket sends the session token as an `Authorization: Bearer` header (and
  the lease as `X-Benchpod-Lease`) instead of `?token=` in the URL, so the token stays out of
  access logs. A tunnel error message never shows the token.
- Cloud: a client-side upload over `embeddedci:<device>` (`replay()` of a waveform, SPI PSRAM
  staging) goes out in frames of at most 1 KiB, paced to 128 KiB/s. It used to be one WebSocket
  frame, which the pod drops without an error above about 1.5 KB. The upload also skips the
  `{"ack":N}` progress lines the pod sends on a cloud tunnel instead of taking the first one as
  the completion.
- `PowerStatus.pod`: the pod's own 5 V draw, on boards that measure it (INA226 at 0x41, the
  v3r1 analog and v3r2 digital boards). `PowerStatus.total_current` adds the DUT's internal rail
  to it, which is what the USB input delivers. Both are `None` on other boards and on firmware
  up to 3.5.1.
- `Capabilities.pod_current`: the board has that monitor. Parsed from the pod's `status` and the
  server's `cap.pod_current`.

## 2.5.0

- `Capabilities.can`: the pod has classic CAN on CAN+/CAN- (`open_can`). Parsed from the pod's
  `status` and the server's `cap.can`. False on firmware before 3.5.1, which has CAN but does not
  announce it. Firmware 3.5.1 announces it.
- `flash()` with `target_power` waits for the target to answer on every connect attempt, not only
  the first, so a retry after a target that answered too early (a NUCLEO's ST-LINK still holding
  it) waits for it to settle as well.
- `CanReadResult.overflow` is documented as what the pod reports: frames dropped since the last
  `can_config` (a running total), not since the last read.

## 2.4.0

- **Breaking:** the 4-20 mA measurement terminal (J8) is now `"current_in"` as an ADC source
  and analog path, and `lowlevel.cal_switch(current_in=...)` for its relay. The old name read as
  "amplifier". Firmware 3.4.0 no longer accepts the old name, so `AdcSource` and
  `AnalogPath` drop it without a major version. `adc_read("current_in")` also returns the loop
  current in amps as `AdcReading.current`.
- Calibration: `BenchPod.calibrate()` makes the pod measure the offset of its 4-20 mA input
  (`current_in`, J8 disconnected) and store it in flash; `calibration()` reads it back and
  `clear_calibration()` removes it. All three return a `Calibration` (`calibrated`, `offset` in
  volts, the fit `a` / `b`). `adc_read("current_in")` has the offset taken out and reports it as
  `AdcReading.offset`. New `CalibrateSource` and `Capabilities.calibrate`. Needs firmware 3.4.0.
- 4-20 mA output: `BenchPod.current_out(amps)` holds a current on terminal J9 and returns a
  `CurrentOutput` with the current actually held, the 16-bit DAC code and the range the output
  can do; `current_out_range()` reads the range only. The pod converts and refuses a current
  outside its range. The output is loop powered (it needs an external floating supply) and shares the DAC with the voltage outputs,
  which `current_out` switches off. New analog path `"current_out"` and
  `Capabilities.current_out`. Needs firmware 3.4.0.
- Waveforms on the 4-20 mA output: `generate`, `replay` and `replay_waveform` take
  `dac_path="current_out"` (`DacPath` gains it), with levels in amps and the range the pod
  reports. Stopping the returned handle returns the loop to 4 mA.
- Captures of the 4-20 mA input in amps: `capture_adc(source="current_in")` scales with the pod's
  own fit and fills the new `Capture.currents`. `capture_correlated` takes `source` too, so the
  loop current is captured next to the logic channels off one trigger. Such a capture replays as the same current on
  `current_out`, and `save_capture_as_recording` stores it as a current. `Waveform.unit` says
  whether a library entry is in `"V"` or `"mA"`; `WaveformLibrary.save_recording` takes `unit`,
  and `save_segments` takes amps on the `current_out` path. Needs server support for the unit.
- Motor & battery emulator: `BenchPod.open_motor_emulator()` returns a `MotorEmulator` for the
  ECP5 emulator boards on the SPI link. It configures every board at once (`configure`, slave
  SPI with PROGRAMN and DONE), reads and writes the link registers by name (`read`, `write`,
  `broadcast=True`), and converts the common ones to units with an `EmulatorCalibration`: bridge
  PWM and duties, sinc samples, protection and trips, the battery model (`BatteryModel`, OCV curve,
  R0 / RC branch, state of charge), the back-EMF shape, the logging stream (`read_log` returns
  whole sample sets) and the board EEPROM. Needs emulator gateware 0x0007+.
- `SpiSession.stream(data, head=...)` sends data of any size in one CS frame through the pod's
  PSRAM (`load_bin` staging, then the `spi_stream` command). New `SpiStreamResult` and
  `Capabilities.spi_stream`.

## 2.3.0

- SPI master on the LA pins: `BenchPod.open_spi()` returns an `SpiSession` for raw full-duplex
  transfers (`transfer`, CS held across calls with `hold_cs`) and SPI NOR flash work
  (`flash_id`, `flash_read`, `flash_erase`, `flash_write`, `flash_program`).
  `BenchPod.spi_flash(image, addr)` erases, writes and verifies a whole image in one call, and can
  hold the DUT in reset while it runs (`hold_reset=True`). Pins come from the wiring profile
  (`spi_sclk`, `spi_mosi`, `spi_miso`, `spi_cs`) or as arguments; up to 6 MHz, modes 0 and 3.
  Needs firmware 3.3+ with gateware v45 (capability `spi_master`, new `Capabilities.spi_master`).
  Works over the LAN, USB and the cloud.
- pytest plugin: with `BENCHPOD_LIFT_DAC_LIMITS=1` a session fixture clears the pod's DAC output
  limits for the run and writes the same limits back at the end, even when tests fail.

## 2.2.0

- Cloud: the session token the SDK mints is renewed shortly before it expires, and once more if the
  server rejects it (tunnel, command channel and lease heartbeat). A connection held longer than
  the token's hour used to start failing mid-session. A `cloud_token` you pass is not renewed.
- Cloud: new `BenchPod(cloud_user_token=callable)` authenticates with a logged-in user's access
  token (exchanged via `POST /api/auth/token`), between an API key and GitHub Actions OIDC.
  `cloud_auth.mint_session_token()` returns the token with its expiry; `exchange_user_token()` added.
  The server API (wiring profile, waveform library) uses that user token too: the device and
  waveform routes refuse the cloud session token minted from it.
- Cloud: an offline pod fails with "BenchPod '…' is offline" and an unknown name with
  `ConnectionConfigError: no cloud BenchPod named '…'`, instead of the raw HTTP error. The lease used
  to read the server's 404 for an unknown name as "no lease support" and carry on unlocked.
- Cloud: `DeviceBusyError` says until when the holder's lease runs, and how to wait longer outside
  pytest (`lease_wait`).

## 2.1.1

- The README, docstrings and examples say 14 LA channels (LA1-LA14); 2.1.0 still said 12.
  No code changes.
- New e2e tier `tests/e2e/test_e2e_la13_14.py`: LA13/LA14 in the pin table, GPIO drive and
  read-back, captures, triggers, step trains and a UART session. The cloud tier also checks
  LA13/LA14, and it no longer requires every LA word to fit 12 bits.

## 2.1.0

- BenchPod v3 pods now expose LA13/LA14 (14 LA channels instead of 12): every LA-channel
  argument and `Pin` constant accepts 1-14. `Pin.PIN13`/`Pin.PIN14` added; `LaCapture.channels`
  now defaults to 14. Older firmware reporting only 12 channels is unaffected.
- Cloud commands retry twice (0.5 s, then 1.5 s) when Cloudflare answers 502/503/504/52x
  instead of the server, and when the server says the pod is on another instance. One edge blip
  used to fail a CI job. A pod that is offline or timed out still fails at once, and a CAN
  write, reset pulse or step-pulse train is never repeated.
- ADC captures read back about 6.7x faster on firmware that offers base64 samples
  (`capture_b64` in `status.caps`): `capture_adc(2_000_000)` at 400 kS/s now takes 5 s + 4.5 s
  instead of 5 s + 31 s over the LAN. The SDK asks for it on its own; older firmware still gets
  the decimal request. New `Capabilities.capture_b64`.
- `Capabilities` gains `board_rev`, `nrst_pin` and `usb_cc`: the firmware reported them, the SDK
  dropped them.
- Fix: `benchpod_target` powered eFuse 1 whatever the wiring profile said, while
  `power_cycle_and_capture` and `measure_power` used the profile's rail. Without
  `--benchpod-efuse` it now follows the profile.
- Fix: `reset_target(pulse=)` above 1 s raises `ValueError`. The pod clamps the pulse to 1 s, so
  a longer one was silently shortened. The docstring no longer claims it returns before the pulse
  ends.

## 2.0.0 — the frozen API

2.0.0 is the first release with a stability promise. Everything in
`embeddedci.benchpod.__all__` follows [semantic versioning](https://semver.org): within 2.x
names and signatures only grow, never break. `tests/api_surface.json` snapshots that surface and
CI fails on any unreviewed change. `BenchPod.command()`, `BenchPod.transport` and
`BenchPod.lowlevel` are escape hatches **outside** the promise.

### Conventions (apply everywhere)

- **Units are volts, seconds and hertz.** `sample_rate_mhz` → `sample_rate_hz`, `*_us`/`*_ms`
  arguments → seconds, millivolt/microamp readings → volts/amps.
- **Invalid arguments raise `ValueError`.** `BenchPodError` (and subclasses) now means a device,
  transport or server failure only.
- **Device state is typed.** Methods that returned raw firmware dicts return frozen dataclasses
  from `embeddedci.benchpod.state` (each keeps the untouched reply in `.raw`).
- **String options are `Literal` types** (`DacPath`, `AnalogPath`, `AdcSource`, `LoopSource`,
  `Waveshape`, `ReplayMapping`, `DecodeProtocol`, `CanMode`, `FaultType`) and are validated.
- Python **3.10+** (3.9 is end-of-life).

### Migration table

| 1.x / 0.x | 2.0 |
| --- | --- |
| **Connection** | |
| `bp = BenchPod(conn); bp.set_la_voltage(3.3)` | `BenchPod(conn, la_voltage=3.3)` (or `BENCHPOD_LA_VOLTAGE=3.3`) |
| pytest: `--benchpod-la-voltage 3.3` on every run | override the `benchpod_la_voltage` fixture once in `conftest.py` (the flag remains a per-run override) |
| `status()` returned text over serial | `status()` is a dict on every transport |
| `transport.set_la_voltage(mv)` / `get_la_voltage()` | removed — use `BenchPod.set_la_voltage` / `get_la_voltage` |
| **LA voltage** | |
| `set_la_voltage(3300)` (millivolts accepted) | `set_la_voltage(3.3)` — volts only |
| `set_la_voltage(...)` / `get_la_voltage()` → `{"mv", "st"}` | → `LaVoltage(voltage, readback)`; `voltage is None` until selected |
| **Power** | |
| `power_status()` → `{"internal": {"bus_mv", "current_ua"}, …}` | → `PowerStatus`: `.rail(1).bus_voltage` (V), `.current` (A) |
| `command({"cmd": "target_status"})` | `target_status()` → `TargetStatus`: `.efuse(1).fault` |
| — | new: `reset_target(pulse=0.1)`, `set_reset(asserted)`, `reset_state()`, `usb_cc()` |
| **Bias resistors** | |
| `pullup(la, on=True/False)` | `set_pull(la, enabled)` → `PullState` |
| `pullup(la)` (query) | `pull_state(la)` → `PullState(enabled, direction, ohms)` |
| `pullup_status()` → `{"la_pullup_mask"}` | `enabled_pulls()` → `[1, 2]` |
| `enable_pullup(7)` silently engaged a pull-**down** | raises `ValueError`; use `enable_pulldown(7)` / `disable_pulldown(7)` |
| **Flash** | |
| `flash(nreset=PIN3)` — an LA channel wired to the target's NRST | `flash(nreset=True)` — NRST wired to the pod's own reset pin (DUT header J1 pin 22, rev3 pods) |
| **Emulated I2C sensor** | |
| `i2c_sensor_la_decoded(samples, sample_rate_mhz)` | `i2c_sensor_capture(samples, *, sample_rate_hz)` |
| `i2c_sensor_la(...)` (raw packed bytes) | removed — `i2c_sensor_capture` decodes |
| `i2c_sensor_regs(start, length)` | `i2c_sensor_regs(*, start, length)` |
| **Analog** | |
| `analog_path("5v")`, `("ext")`, `("sma")` aliases | canonical names only: `"dac_5v"`, `"adc_ext"`; returns `AnalogPathState` |
| `dac_output(path, volts)` → `{"path", "mv", "code"}` | `dac_output(path, *, volts=)` → `DacOutput(path, voltage, code)` |
| `adc_read(source)` → `{"source", "mv", "count"}` | → `AdcReading(source, voltage, count, span)` |
| `measure_volts(source)` | `adc_read(source).voltage` |
| `route_dac_to_adc("5v")` / `("12v")` | `analog_path("cal1")` / `analog_path("cal2")` |
| `adc_from_sma()` | `analog_path("adc_ext")` |
| `dac_mux`, `dac_mux_status`, `cal_switch`, `cal_switch_status` | `bp.lowlevel.dac_mux(...)` etc. (outside the stability promise) |
| `dac_set(value, channel=)` (`channel` was ignored) | `bp.lowlevel.dac_set(code, divider=)` |
| **Captures** | |
| `capture(samples, sample_rate_mhz=)` → raw `List[int]` | `capture_adc(samples).counts` |
| `scope_capture(samples, *, sample_rate_mhz, source)` | `capture_adc(samples, *, sample_rate_hz, source)` — `source` now actually routes the ADC; default 4096 samples; above 32768 it streams from PSRAM |
| `capture_la(n, *, sample_rate_mhz, stop_dac_after_us)` | `capture_la(n, *, sample_rate_hz, stop_dac_after)` (seconds) |
| `capture_analog(adc_samples, adc_rate_mhz, la_samples, la_rate_mhz, stop_dac_after_us)` | `capture_correlated(adc_samples, adc_sample_rate_hz, la_samples, la_sample_rate_hz, stop_dac_after)` |
| `AnalogCapture` | `CorrelatedCapture` |
| `Capture.duration_s`, `LaCapture.duration_s` | `.duration` |
| `LaCapture.decode_i2c(sda=, scl=)` | `LaCapture.decode("i2c", sda=, scl=)` |
| **DAC** | |
| `generate(w, *, freq, amplitude, offset=0.0, duration_ms, sample_rate_mhz)` — amplitude/offset were firmware 8-bit **codes**, offset defaulted to code 0 | `generate(w, *, freq_hz, amplitude, offset=None, dac_path="5v", duration, sample_rate_hz)` — **volts**; offset defaults to mid-range; routes `dac_path`; returns a `DacHandle` |
| `replay(..., sample_rate_mhz=)` | `replay(..., sample_rate_hz=)` (defaults to a `Capture`'s own rate) |
| `replaying(...)` | `replay(...)` (already a context manager) |
| `replay_waveform(waveform_id="…")` | `replay_waveform("…")` (positional id or `Waveform`) |
| `replay_waveform(..., dac_path="5v")` default | `dac_path=None` → the waveform's own path (else `5v`), used for both routing and scaling |
| `Segment(shape, duration_ms, v_start, v_end)` | `Segment(shape, duration, v_start, v_end)` — `duration` in seconds |
| `dac_stop()` / `ReplayHandle.stop()` returned the reply | return `None` |
| **UART** | |
| `UartSession.read(timeout=)` returned everything received so far | returns the output since the last read and marks it read (everything received is `.text`) |
| `UartSession.drain()` | `read()` |
| `UartSession.read_until` / `expect` searched everything ever received and consumed nothing; both returned the match | both search the unread output and mark it read up to the end of the match; `read_until` returns that output (or `None`), `expect` returns the match (`re.Match` for a regex) |
| **Control loop** | |
| `loop_input(...)` → dict | → `LoopState(source, input_code, step, output_code)` |
| `ControlLoopHandle.set_input(...)` → dict | → `LoopState` |
| `IVPoint.current_code`, `IVPoint.voltage_code` | `IVPoint.i`, `IVPoint.v` |
| `fpga_image(0)` → dict | `fpga_image(FpgaImage.LOOP)` → `FpgaImageInfo(image, version, features)` |
| **CAN** | |
| `can_read(max=8)` → `{"frames", "overflow"}` | `can_read(max_frames=8)` → `CanReadResult(frames: List[CanFrame], overflow)` |
| `CanBus.read(max=8)` | `CanBus.read(max_frames=8)` |
| **Stepper** | |
| `la_step(la, steps, delay_us, dir_la, direction)` | `la_step(la, *, steps, delay, dir_la=None, direction=0)` — `delay` in seconds |
| **Errors** | |
| `coerce_pin` / `coerce_efuse` / decoder and flash argument checks raised `BenchPodError` / `FlashError` | raise `ValueError` |

### Added

- `BenchPod(la_voltage=...)` / `BENCHPOD_LA_VOLTAGE` select the LA bank voltage on connect; the
  `benchpod_la_voltage` pytest fixture sets it once for a test suite from `conftest.py`.
- `target_status()`, `reset_target()`, `set_reset()`, `reset_state()`, `usb_cc()`.
- `enable_pulldown()`, `disable_pulldown()`, `set_pull()`, `pull_state()`, `enabled_pulls()`.
- `LoopInputMap` (`control_loop(input_map=...)`) and `Capabilities.dac_loop_input_map` for
  gateware v30's engineering-units loop input.
- `LaCapture.edges(la)`, `FpgaImage`, `DacHandle`, `CanReadResult`, `CloudAuthError` export,
  the `Literal` option types, and the `embeddedci[pytest]` extra.
- The serial transport probes whether the pod's USB console has a JSON mode. The STM32 pod's does
  not (it is a text shell), so over USB `status()` (parsed from the text report), `ping()`, the LA
  voltage and target power on/off run as text commands, and every other operation raises a
  `TransportError` at once that points at the network/cloud connection (instead of timing out).
  On firmware with a JSON mode, chunked replies now stream too.
- `generate(..., route=False)` / `replay(..., route=False)` keep the current analog switching, so a
  DAC→ADC loopback set up with `analog_path("cal1")` survives starting the output.
- `BenchPod.leased`: whether this client holds a cloud device lease.
- **Automatic gateware image switching.** `control_loop()`, `replay()` and `replay_waveform()` take
  `switch_image=True` and switch the pod to the image they need (loop / deep replay) instead of
  failing, logging a warning; `ControlLoopHandle.switched_image` / `ReplayHandle.switched_image`
  record the switch. `switch_image=False` raises a `BenchPodError` (the control loop used to reach
  the firmware and fail there). The `benchpod_capability` marker switches the image for
  `dac_control_loop` / `dac_deep_replay` instead of skipping. The README has a new
  "Gateware images" section.
- **Wiring profiles.** `Wiring` / `Signal` describe which DUT signal is on which LA channel (UART, I2C,
  SWD, SPI roles, the target-power rail, the LA voltage and named signals) with the same schema the
  embeddedci.com server and web UI store. `BenchPod(wiring=...)`, `bp.wiring` (argument → `BENCHPOD_WIRING`
  file → a cloud device's stored profile → defaults), `bp.save_wiring()`, `bp.signal(name)`, the pytest
  `benchpod_wiring` fixture and `--benchpod-wiring`. Channel arguments of `open_uart`, `capture_uart`,
  `power_cycle_and_capture`, `flash`, `enable_i2c_sensor` and the power methods are now optional and
  fall back to the profile; LA arguments accept role/signal names.
- **GPIO on the LA pins and pin ownership.** `bp.gpio()` → `GpioPin` (`configure`, `set`, `high`/`low`,
  `activate`, `read`, `wait_for`, `pulse`, `release`), `set_gpio`, `read_gpio`, `pin_levels`,
  `wait_for_level`, `release_gpio`, `la_pins()` → `LaPinState`, and `gpio_pins()`/`configure_gpio()` to
  claim several channels as one group (a conflict on any of them claims none). Each channel has one
  function at a time:
  a second one fails with `PinConflictError`, an incompatible bias resistor with `PullConflictError`.
- **Triggered captures.** `Trigger(la, edge)` on `capture_adc`/`capture_la`/`capture_correlated`
  (`trigger_timeout`, `TriggerTimeout`); results carry `.trigger`.
- **Timing helpers.** `LaCapture.edge_times`, `first_edge`, `level_at`, `pulse_widths`, `frequency`,
  `duty_cycle`, `delay`; `Capture.crossing_times`, `first_crossing`; the `Edge` option type.
- **Power profiles.** `bp.measure_power()` and `bp.power_profile()` → `PowerProfile` /
  `PowerProfileSession` (average/min/peak current, voltage, energy, charge, trace) from timestamped
  sampling of the rail monitor, integrated over real time. `rate_hz` is the rate actually delivered
  (measured) and `adc_rate_hz` the sensor's configured conversion rate: ask for 100-500 Hz and the
  pod tracks the request to ~200 Hz, flattening near 365 Hz. The README documents the rails' current
  limits.
- `Capabilities.la_pins`, `gpio_read`, `capture_trigger`, `power_profile`.
- The `hardware` marker now skips a test when no connection is configured, even if the test
  requests no device fixture.

### Fixed

- `generate()` sent `offset=0.0` (8-bit code 0) by default and truncated fractional volts; it now
  converts volts to the generator's codes for the chosen path.
- `BENCHPOD_API_BASE` was ignored by the cloud transport when `BenchPod` was constructed directly.
- `scope_capture(source=...)` only labelled the result; `capture_adc(source=...)` routes it.
- The `benchpod_target` fixture ignored `--benchpod-efuse`.
- Over USB, `target_power` sent a `target-power` verb the STM32 console doesn't have (it is
  `power`); a delayed change over USB now raises instead of being silently ignored.
- `LaVoltage.readback` is `None` (not 0.0 V) on boards that cannot measure the bank.
- `fpga_image` was documented as a ~10 ms warm boot; it reprograms the FPGA (~2-3 s).
- The `12v` DAC path was mapped as 0..12 V; it is bipolar −12..+12 V (0 V at mid-scale), so
  `generate` and `replay` volts on it were wrong — confirmed against the firmware calibration and on
  a pod. `generate`'s default offset on `12v` is now 0 V. (`dsp.volts_to_codes` gained
  `path_min_v`; `dsp.dac_path_range_v` gives each path's range.)
- A replay deeper than 2048 samples on the control-loop gateware image was accepted and then
  produced no output (measured on a pod); it now switches the pod to the deep-replay image first
  (or raises a `BenchPodError` with `switch_image=False`).
- `DacOutput.path` reports the output path you asked for (`5v`), not the firmware's analog-path
  name (`dac_5v`).
- `replay_waveform` scaled a segments waveform for its stored path but routed the `5v` default.
- `capture_adc(source=...)` now waits for the relays to settle before capturing.
- Error messages no longer claim the cloud destination is CI-only or suggest OpenOCD builds that
  lack the `cmsis_dap_tcp` backend.
