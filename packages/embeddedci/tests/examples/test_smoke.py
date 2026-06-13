"""Real-hardware smoke test (documentation + opt-in).

Skipped unless a pod is reachable. Run against hardware with, e.g.::

    pytest --benchpod-connection=192.168.1.213 tests/examples
    BENCHPOD_CONNECTION=serial pytest tests/examples

These exercise the live device, so they only run when the `benchpod` fixture
(provided by the installed pytest plugin) has a connection configured.
"""

import pytest

from embeddedci import benchpod as bp


def test_ping(benchpod):
    # `ping` returns "pong" over TCP, or status text over serial — both truthy.
    assert benchpod.ping()


def test_power_cycle(benchpod):
    benchpod.power_on(bp.INTERNAL)
    benchpod.power_off(bp.INTERNAL)


@pytest.mark.skipif(
    "not config.getoption('benchpod_firmware', default=None)",
    reason="set --benchpod-firmware=... to flash a real image",
)
def test_flash_known_good(benchpod, request):
    firmware = request.config.getoption("benchpod_firmware")
    result = benchpod.flash(
        file=firmware,
        target="target/stm32f1x.cfg",
        swclk=bp.PIN1,
        swdio=bp.PIN2,
        nreset=bp.PIN3,
        target_power=bp.INTERNAL,
    )
    assert result.ok


def test_i2c_sensor_enable_disable(benchpod):
    benchpod.enable_i2c_sensor(bp.Sensor.BMP280, sda=bp.PIN1, scl=bp.PIN2)
    try:
        assert benchpod.i2c_sensor_status()
    finally:
        benchpod.disable_i2c_sensor()
