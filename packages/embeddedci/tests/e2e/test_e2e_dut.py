"""Tier "dut": the pod driving a real board — flash, boot, console, sensor emulation, bus decode.

    pytest packages/embeddedci/tests/e2e/test_e2e_dut.py --benchpod-connection=<pod host> \
        --benchpod-firmware=../examples/scenario-sensors-stm32/build/scenario-sensors.elf

Written for the examples/scenario-sensors-stm32 app on a NUCLEO-F446RE; the wiring comes from the
``bench`` fixture (conftest.py). The DUT is flashed once, then power-cycled per test and left off.
"""

from __future__ import annotations

import re
import time

import pytest

from embeddedci.benchpod import decode, i2c

pytestmark = pytest.mark.hardware

BOOT_TIMEOUT = 25.0     # seconds from power-on to APP_OK (the app probes sensors with retries)
APP_OK = re.compile(r"APP_OK")


@pytest.fixture(scope="module")
def flashed(pod, bench, pytestconfig):
    """Flash the firmware once for the module; yields the FlashResult. Powers the DUT off after."""
    firmware = pytestconfig.getoption("benchpod_firmware")
    if not firmware:
        pytest.skip("pass --benchpod-firmware=<scenario-sensors.elf> to run the DUT tier")
    result = pod.flash(file=firmware, target=bench.target_cfg, swclk=bench.swclk, swdio=bench.swdio,
                       nreset=bench.nreset, target_power=bench.efuse, check=False)
    yield result
    try:
        pod.power_off(bench.efuse)
    except Exception:
        pass


@pytest.fixture
def dut(flashed, pod, bench):
    """The pod with a successfully flashed, powered-off DUT."""
    if not flashed.ok:
        pytest.skip("flashing failed (see test_flash)")
    pod.power_off(bench.efuse)
    time.sleep(0.5)
    yield pod
    pod.power_off(bench.efuse)


def test_flash(flashed):
    assert flashed.ok, f"rc={flashed.returncode} unreachable={flashed.target_unreachable}\n{flashed.stderr[-2000:]}"


def test_power_cycle_captures_the_boot_banner(dut, bench):
    boot = dut.power_cycle_and_capture(rx=bench.uart_rx, tx=bench.uart_tx, efuse=bench.efuse,
                                       delay=0.5, duration=BOOT_TIMEOUT, until=APP_OK)
    assert boot.matched, f"no APP_OK within {BOOT_TIMEOUT}s:\n{boot.text[-2000:]}"
    assert "SCENARIO:" in boot.text


def test_power_monitor_and_efuse_see_the_dut(dut, bench):
    dut.power_on(bench.efuse)
    time.sleep(1.0)
    rail = dut.power_status().rail(bench.efuse)
    assert 4.5 < rail.bus_voltage < 5.5 and rail.current > 0.01, rail
    assert dut.target_status().efuse(bench.efuse).enabled
    dut.power_off(bench.efuse)
    time.sleep(0.5)
    assert dut.power_status().rail(bench.efuse).current < 0.005


def test_interactive_console(dut, bench):
    with dut.open_uart(rx=bench.uart_rx, tx=bench.uart_tx) as uart:
        dut.power_on(bench.efuse)                       # immediate: the session is already buffering
        uart.expect(APP_OK, timeout=BOOT_TIMEOUT)
        uart.expect("> ", timeout=5)
        uart.write("help\r")
        uart.expect("commands:", timeout=5)
        uart.write("status\r")
        uart.expect("bmp280_detected=", timeout=5)
        uart.write("reset\r")                           # the app reboots itself over the console
        uart.expect("RESET: rebooting", timeout=5)
        uart.expect(APP_OK, timeout=BOOT_TIMEOUT)


def test_firmware_detects_the_emulated_bmp280(dut, bench):
    dut.enable_pullup(bench.i2c_sda, bench.i2c_scl)
    dut.enable_i2c_sensor(sda=bench.i2c_sda, scl=bench.i2c_scl, address=0x76,
                          temperature_c=22.5, pressure_pa=101_000)
    try:
        boot = dut.power_cycle_and_capture(rx=bench.uart_rx, tx=bench.uart_tx, efuse=bench.efuse,
                                           delay=0.5, duration=BOOT_TIMEOUT, until=APP_OK)
        assert "chip id match=0x58" in boot.text, boot.text[-2000:]
        assert dut.i2c_sensor_status().get("transactions", 0) > 0
    finally:
        dut.disable_i2c_sensor()
        dut.disable_pullup(bench.i2c_sda, bench.i2c_scl)


# The DUT probes the BMP280 (0x76) only in the first ~30-45 ms of its boot I2C burst, which starts
# ~2.30 s after power_on() returns; the rest of the ~170 ms burst probes the VL53L0X (0x29). A
# 4096-byte sensor capture at 500 kHz is 16384 samples = ~33 ms and calls are ~250 ms apart, so
# polling misses the probe about half the time: time one window to it instead. On the bench a call
# made 2.28-2.34 s after power_on() catches it.
BMP280_PROBE_AT = 2.31


def test_i2c_sensor_capture_sees_the_boot_probe(dut, bench):
    dut.enable_pullup(bench.i2c_sda, bench.i2c_scl)
    dut.enable_i2c_sensor(sda=bench.i2c_sda, scl=bench.i2c_scl, address=0x76)
    try:
        lead, misses = BMP280_PROBE_AT, []
        for _ in range(3):
            dut.power_off(bench.efuse)
            time.sleep(0.5)
            dut.power_on(bench.efuse)
            time.sleep(lead)
            txns = dut.i2c_sensor_capture(4096, sample_rate_hz=500_000)  # 5 samples per 100 kHz bit
            if i2c.addressed(txns, 0x76):
                break
            misses.append((lead, len(txns)))
            lead += 0.02 if not txns else -0.02    # an idle window was early, a ToF-probe one late
        assert i2c.addressed(txns, 0x76), f"(lead s, transactions) per miss: {misses}\n" \
                                          f"{i2c.format_transactions(txns)}"
    finally:
        dut.disable_i2c_sensor()
        dut.disable_pullup(bench.i2c_sda, bench.i2c_scl)


def test_firmware_reports_a_missing_sensor(dut, bench):
    boot = dut.power_cycle_and_capture(rx=bench.uart_rx, tx=bench.uart_tx, efuse=bench.efuse,
                                       delay=0.5, duration=BOOT_TIMEOUT, until=APP_OK)
    assert "BMP280 init FAILED" in boot.text, boot.text[-2000:]


@pytest.fixture(scope="module")
def boot_capture(flashed, pod, bench):
    """One 4 s logic capture of a whole boot with the emulated BMP280 answering, and the number of
    I2C transactions the pod served during it."""
    if not flashed.ok:
        pytest.skip("flashing failed (see test_flash)")
    pod.enable_pullup(bench.i2c_sda, bench.i2c_scl)
    pod.enable_i2c_sensor(sda=bench.i2c_sda, scl=bench.i2c_scl, address=0x76)
    try:
        pod.power_off(bench.efuse)
        time.sleep(1.5)
        before = pod.i2c_sensor_status().get("transactions", 0)
        pod.power_on(bench.efuse, delay=0.1)    # pod-side delay, so the capture below is armed first
        la = pod.capture_la(4_000_000, sample_rate_hz=1_000_000)  # the NUCLEO boots ~2.4 s in
        served = pod.i2c_sensor_status().get("transactions", 0) - before
    finally:
        pod.power_off(bench.efuse)
        pod.disable_i2c_sensor()
        pod.disable_pullup(bench.i2c_sda, bench.i2c_scl)
    return la, served


def test_logic_capture_decodes_the_boot_i2c(boot_capture, pod, bench):
    la, served = boot_capture
    txns = pod.decode(la, "i2c", sda=bench.i2c_sda, scl=bench.i2c_scl)
    assert i2c.read_register(txns, 0x76, 0xD0) == [0x58], i2c.format_transactions(txns[:20])
    # the decoder saw every transaction the pod's emulated sensor served
    assert served > 0 and abs(len(txns) - served) <= 2, (len(txns), served)


def _samples_per_bit(bits, guess: float) -> float:
    """Fit the bit period (in samples) from the run lengths of a serial line."""
    runs, count = [], 1
    for prev, cur in zip(bits, bits[1:]):
        if cur == prev:
            count += 1
        else:
            runs.append(count)
            count = 1
    runs = [r for r in runs[1:] if r < guess * 7]
    spb = guess
    for _ in range(20):
        spb = sum(runs) / sum(max(1, round(r / spb)) for r in runs)
    return spb


def test_la_sample_rate_matches_the_dut_uart(boot_capture, bench):
    """The DUT's USART (115200 from its 16 MHz HSI, ±1%) is an independent clock: the bit period
    measured in samples must match the capture's reported sample rate."""
    la, _ = boot_capture
    expected = la.sample_rate_hz / (16e6 / 139)
    spb = _samples_per_bit(la.channel(bench.uart_rx), expected)
    assert abs(spb / expected - 1) < 0.015, (
        f"{spb:.3f} samples/bit measured, {expected:.3f} expected at the reported "
        f"{la.sample_rate_hz:.0f} Hz: the real rate is {spb / expected:.4f}x the reported one")


def test_logic_capture_decodes_the_boot_uart(boot_capture, pod, bench):
    la, _ = boot_capture
    text = decode.uart_text(pod.decode(la, "uart", rx=bench.uart_rx, baud=115200))
    assert "I2C1 init OK" in text and "APP_OK" in text, text[:500]


# -- power profiles -------------------------------------------------------------------------

@pytest.fixture
def power_pod(dut):
    if not dut.capabilities.power_profile:
        pytest.skip("the pod firmware has no power profiles (capability power_profile)")
    return dut


def test_power_profile_of_a_boot(power_pod, bench):
    with power_pod.power_profile(efuse=bench.efuse, keep_samples=1000, max_duration=10.0) as session:
        power_pod.power_on(bench.efuse)
        time.sleep(3.0)
    prof = session.result
    assert 4.5 < prof.avg_voltage < 5.5 and 0.01 < prof.avg_current < 1.0, prof
    assert prof.min_current <= prof.avg_current <= prof.peak_current
    assert prof.energy == pytest.approx(prof.avg_power * prof.duration, rel=1e-6)
    assert prof.charge == pytest.approx(prof.avg_current * prof.duration, rel=0.05)
    assert prof.n == pytest.approx(prof.rate_hz * prof.duration, rel=0.2)
    assert 0 < len(prof.samples) <= 1000 and not prof.fault and not prof.truncated


def test_power_profile_sees_the_delayed_power_on(power_pod, bench):
    power_pod.power_on(bench.efuse, delay=0.5)            # pod-side timer: lands inside the profile
    prof = power_pod.measure_power(2.0, efuse=bench.efuse, keep_samples=2000)
    first_on = next((t for t, amps, _ in prof.samples if amps > 0.005), None)
    assert first_on is not None and 0.3 < first_on < 0.9, first_on
    assert prof.peak_current > prof.min_current + 0.005


def test_power_profile_of_an_unpowered_rail(power_pod, bench):
    power_pod.power_off(bench.efuse)
    time.sleep(0.5)
    prof = power_pod.measure_power(0.5, efuse=bench.efuse)
    assert prof.avg_current < 0.005 and prof.energy < 0.01
