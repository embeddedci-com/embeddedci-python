"""First-run smoke test: is the pod reachable, and is this bench wired the way its profile says?

Run this before writing your own tests — it answers "is my setup right?" rather than testing any
particular firmware, and every check skips cleanly when its prerequisite is missing::

    # the minimum: a reachable pod
    pytest --benchpod-connection=192.168.1.213 tests/examples

    # add the bench's wiring profile when it is not on the default channels
    pytest --benchpod-connection=192.168.1.213 --benchpod-wiring=wiring.json tests/examples

    # add a firmware image and an OpenOCD target to exercise flashing
    BENCHPOD_TARGET_CFG=target/stm32f4x.cfg pytest --benchpod-connection=192.168.1.213 \\
        --benchpod-firmware=build/app.elf tests/examples

Nothing here hard-codes an LA channel or a power rail: they all come from the wiring profile, which
is the same thing your own tests will use. Without a connection configured the whole file skips.
"""

import os

import pytest

from embeddedci import benchpod as bp


def test_pod_answers(benchpod):
    """The connection works and the pod identifies itself."""
    assert benchpod.ping()
    status = benchpod.status()
    assert status.get("board"), status
    assert status.get("version"), status


def test_wiring_profile_is_consistent(benchpod):
    """The profile in force describes one signal per channel.

    ``warnings()`` catches the mistakes that otherwise show up as silence later — two roles sharing
    a channel, or a bias resistor that fights the role assigned to its pin.
    """
    wiring = benchpod.wiring
    assert wiring.warnings() == [], wiring.warnings()

    # Not a failure, but the thing to check first when a capture comes back empty: "defaults" means
    # no --benchpod-wiring file and no profile stored on the server, so these channels are a guess.
    if wiring.source == "defaults":
        print(f"\nwiring profile: defaults — UART rx=LA{wiring.uart_rx} tx=LA{wiring.uart_tx}, "
              f"SWD clk=LA{wiring.swd_swclk} dio=LA{wiring.swd_swdio}, eFuse {wiring.efuse}. "
              "Pass --benchpod-wiring=wiring.json if your bench differs.")


def test_target_power_switches(benchpod):
    """Turning the DUT rail on and off is visible in the pod's own eFuse and monitor readings."""
    rail = benchpod.wiring.efuse
    try:
        benchpod.power_on(rail)
        status = benchpod.target_status()
        if not status.supported:
            pytest.skip("this pod cannot read its eFuse state back")
        assert status.efuse(rail).enabled, status
        assert not status.efuse(rail).fault, "the eFuse tripped when the rail came up"

        monitor = benchpod.power_status().rail(rail)
        if monitor.ok:
            assert 4.0 < monitor.bus_voltage < 5.5, f"rail reads {monitor.bus_voltage:.2f} V"
    finally:
        benchpod.power_off(rail)

    assert not benchpod.target_status().efuse(rail).enabled


def test_power_profile_measures_the_rail(benchpod):
    """A short profile of the powered rail reports a plausible voltage and no fault."""
    if not benchpod.capabilities.power_profile:
        pytest.skip("this pod's firmware has no power profiles (capability power_profile)")
    rail = benchpod.wiring.efuse
    try:
        benchpod.power_on(rail)
        profile = benchpod.measure_power(0.5, efuse=rail)
    finally:
        benchpod.power_off(rail)

    assert profile.n > 0, profile
    assert 4.0 < profile.avg_voltage < 5.5, profile
    assert not profile.fault, "the eFuse tripped during the profile"


@pytest.mark.skipif(
    "not config.getoption('benchpod_firmware', default=None)",
    reason="set --benchpod-firmware=... to flash a real image",
)
def test_flash_the_attached_board(benchpod_target, firmware):
    """Flash over SWD on the profile's channels.

    Note the fixture: ``benchpod_target`` powers the DUT on for the duration of the test (and off
    afterwards). SWD needs a powered target — flashing an unpowered board fails with
    ``TargetUnreachableError``, which is the most common first-run surprise.

    The OpenOCD target file depends on your DUT, so this one needs it spelled out — there is no
    sensible default to guess (``target/stm32f4x.cfg``, ``target/rp2040.cfg``, …).
    """
    target_cfg = os.environ.get("BENCHPOD_TARGET_CFG")
    if not target_cfg:
        pytest.skip("set BENCHPOD_TARGET_CFG=target/<your-mcu>.cfg to flash")

    result = benchpod_target.flash(file=firmware, target=target_cfg)
    assert result.ok, result.log


def test_i2c_sensor_emulation_activates(benchpod):
    """The pod can pretend to be the BMP280 the DUT expects, on the profile's I2C channels."""
    wiring = benchpod.wiring
    if wiring.i2c_sda is None or wiring.i2c_scl is None:
        pytest.skip("this bench's wiring profile has no I2C channels")

    benchpod.enable_i2c_sensor(bp.Sensor.BMP280)
    try:
        status = benchpod.i2c_sensor_status()
        # `active` explicitly — a status dict is truthy even when the emulator is off.
        assert status.get("active") is True, status
    finally:
        benchpod.disable_i2c_sensor()

    assert benchpod.i2c_sensor_status().get("active") is False
