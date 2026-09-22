"""Text the server hands to agents: the server instructions and the wiring reference resource.

``INSTRUCTIONS`` is sent in the MCP ``initialize`` response, which clients such as Claude Code put
straight into the model's context — so it carries what an agent must know before its first call.
"""

INSTRUCTIONS = """\
Drive an EmbeddedCI BenchPod: a hardware-in-the-loop tester wired to a real target board (the DUT).

Start every session with:
1. connect — a host[:port] or 'embeddedci:<device>' (omit to use the server default). A USB connection ('usb' or a serial device path) only supports status, LA voltage and power on an STM32 pod; everything else needs the network or cloud.
2. set_la_voltage (1.8 or 3.3, the DUT's I/O voltage) unless status already reports one. The pod refuses flash, UART, LA capture, pull resistors and I2C-sensor emulation until it is set.
3. status shows firmware, capabilities, the LA voltage and open sessions.

Wiring: the pod has 14 generic LA channels (LA1-LA14) and nothing is fixed. Call `wiring` first: it is the bench's profile — which DUT signal is on which channel, the target-power rail, the UART baud, the SWD target. Every channel, baud, rail or SWD argument you omit comes from it, and channel arguments also accept its names ("READY", "uart_rx"). Confirm it with the user if it looks wrong; `set_wiring` replaces it (save=true stores it on embeddedci.com). Target power is eFuse 1 (internal 5 V) or 2 (external). The DUT's reset line goes to the pod's own reset pin: flash(nreset=true), reset_target.

GPIO: each LA channel has one owner at a time. gpio_mode claims channels (input/output/open_drain), gpio_write drives them, gpio_read reads any channel's live level, gpio_wait polls for one, gpio_pulse emits FPGA-timed pulses, gpio_release frees them. Release a GPIO channel before uart_open, flash or enable_i2c_sensor uses it — otherwise the pod refuses with "pin conflict: LA4 is in use by gpio" and says how to free it; la_pins shows every channel's owner. Captures observe all 14 channels whatever owns them.

Typical flows:
- Flash and boot: flash(file) then power_cycle_and_capture(until_regex) — with a wiring profile that is all; otherwise pass swclk, swdio, target, nreset and rx, tx.
- Interactive console: uart_open, power_on, uart_read(until_regex), uart_write, uart_close.
- Emulate an I2C sensor: set_pull([sda, scl], true) on LA1-LA6 (LA7/LA8 pull DOWN), enable_i2c_sensor, power_cycle_and_capture, i2c_sensor_capture(address, register).
- Analog: dac_output (DC), generate (sine/square/sawtooth) and replay drive the DAC and route its path; adc_read gives one calibrated value, capture_adc a waveform summary; dac_stop ends any DAC output.
- Gateware images: the FPGA runs either the 'loop' image (control_loop) or the 'deep_replay' image (replays longer than 2048 samples). control_loop, replay and replay_waveform switch automatically (~3 s; switched_image in the result says so). A switch resets the FPGA and stops any DAC output, UART session or I2C sensor emulation, so start those after it. fpga_image switches explicitly.
- Logic: capture_la, then decode_la (i2c/uart/spi) re-decodes that capture without re-capturing, and la_timing measures edge times, pulse widths, frequency and the delay between two channels (e.g. trigger pin to result pin).
- Triggers: capture_adc, capture_la and capture_correlated take trigger_la (+ trigger_edge rising/falling/high/low, trigger_timeout seconds) to start on an event rather than immediately, so t = 0 is that edge. Without the trigger firing the tool fails with "TriggerTimeout: ...".
- Power profiles: measure_power(duration) reports the DUT's average/minimum/peak current, voltage, energy and charge over a window, with an optional trace (points). To profile across other tool calls, bracket them with power_profile_start and power_profile_stop.
- CAN: can_open (mode 'internal' self-tests a lone pod), can_write, can_read, can_respond, can_close.

Units are volts, seconds and hertz. A tool that cannot do what was asked fails with an error that names the cause (for example "FirmwareError: la voltage not set" or "NotConnectedError: ..."). A completed operation with a negative outcome is a normal result: flash returns ok=false with its logs, a UART capture returns matched=false.

flash runs OpenOCD on the machine hosting this server and reads `file` from that machine's filesystem. Over the cloud the device is shared: connect takes an exclusive lease, released by disconnect or after the server's idle timeout (the next call reconnects).
"""

WIRING = """\
BenchPod wiring reference

The pod has no fixed pin roles: 14 generic logic-analyzer channels (LA1-LA14) on the DUT
header, and any DUT signal can be on any of them. Always confirm the real wiring with the
user. The example bench (the BMP280 HIL demo) is wired like this:

  DUT signal                  Pod connection               Tool arguments
  --------------------------  ---------------------------  ---------------------------------
  SWCLK                       LA11                         flash swclk=11
  SWDIO                       LA12                         flash swdio=12
  NRST                        pod reset pin (J1 pin 22)    flash nreset=true, reset_target
  UART: DUT TX -> pod         LA5                          rx=5
  UART: DUT RX <- pod         LA4                          tx=4
  I2C SDA / SCL               LA2 / LA1                    sda=2, scl=1 (engage their pull-ups)
  Target power                eFuse 1 (internal 5 V)       efuse=1 (2 = external supply)

Bias resistors (3V3-referenced, so unavailable while the LA bank is at 1.8 V):
  LA1, LA2   4.7k pull-up        LA3, LA4   2.2k pull-up        LA5, LA6   10k pull-up
  LA7, LA8   10k pull-DOWN       LA9-LA14   none

LA I/O bank: 1.8 V or 3.3 V (set_la_voltage), matching the DUT's I/O voltage.

Analog:
  DAC outputs  '3v3', '5v', '12v' (bipolar +/-12 V)   — dac_output, generate, replay
  ADC sources  'ext' front SMA (true volts, the /12 divider is applied), 'cal1' (5 V DAC loopback),
               'cal2' (12 V DAC loopback), 'amp' (current terminal)   — adc_read, capture_adc

CAN: CAN+/CAN- through a TCAN1044 transceiver; the 120 ohm termination is switchable (can_open term).
"""
