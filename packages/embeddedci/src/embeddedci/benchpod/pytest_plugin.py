"""pytest integration for BenchPod.

Registered via the ``pytest11`` entry point, so any project that installs this
package gets the options and fixtures automatically. Resolution order for the
connection is: ``--benchpod-connection`` CLI flag > ``benchpod_connection`` ini
option > ``BENCHPOD_CONNECTION`` environment variable. With no connection
configured the fixtures ``skip`` rather than fail, so the suite stays green
without hardware.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator, Optional

import pytest

from .client import BenchPod
from .connection import ENV_VAR

# LA pin / eFuse defaults used by the pin-map options. Override per-wiring with
# the --benchpod-* flags below; they exist so HIL tests aren't hardcoded.
_PIN_OPTS = {
    "swclk": ("--benchpod-swclk", 11, "LA pin for SWCLK"),
    "swdio": ("--benchpod-swdio", 12, "LA pin for SWDIO"),
    "nreset": ("--benchpod-nreset", 3, "LA pin for NRESET (0 = none)"),
    "uart_rx": ("--benchpod-uart-rx", 5, "LA pin the pod samples (DUT TX)"),
    "uart_tx": ("--benchpod-uart-tx", 4, "LA pin the pod drives (DUT RX)"),
    "i2c_sda": ("--benchpod-i2c-sda", 2, "LA pin for I2C SDA (has a pull-up)"),
    "i2c_scl": ("--benchpod-i2c-scl", 1, "LA pin for I2C SCL (has a pull-up)"),
    "efuse": ("--benchpod-efuse", 1, "target-power eFuse (1=int 5V, 2=ext)"),
}


@dataclass
class BenchPodPins:
    """Resolved pin map for HIL tests (LA channels 1-12, eFuse 1/2)."""

    swclk: int
    swdio: int
    nreset: Optional[int]
    uart_rx: int
    uart_tx: int
    i2c_sda: int
    i2c_scl: int
    efuse: int


def pytest_addoption(parser: "pytest.Parser") -> None:
    group = parser.getgroup("benchpod", "BenchPod hardware-in-the-loop options")
    group.addoption(
        "--benchpod-connection",
        action="store",
        default=None,
        dest="benchpod_connection",
        help="BenchPod connection: host[:port], a serial device path, 'serial', or "
        "'embeddedci:<device-name>' to drive a named device through embeddedci.com. "
        f"Falls back to the {ENV_VAR} env var.",
    )
    group.addoption(
        "--benchpod-api-base",
        action="store",
        default=None,
        dest="benchpod_api_base",
        help="embeddedci API base URL for the 'embeddedci:' destination "
        "(default https://embeddedci.com; falls back to BENCHPOD_API_BASE).",
    )
    group.addoption(
        "--benchpod-firmware",
        action="store",
        default=None,
        dest="benchpod_firmware",
        help="Path to a firmware image, for tests that flash a real target.",
    )
    for key, (flag, default, help_text) in _PIN_OPTS.items():
        group.addoption(
            flag, action="store", type=int, default=default,
            dest=f"benchpod_{key}", help=f"{help_text} (default {default}).",
        )
    parser.addini(
        "benchpod_connection",
        help="Default BenchPod connection (host[:port], device path, or 'serial').",
        default=None,
    )


def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers",
        "hardware: test needs a real BenchPod (and usually a wired DUT); "
        "skipped automatically when no --benchpod-connection is configured.",
    )


def _resolve_connection(config: "pytest.Config") -> Optional[str]:
    return (
        config.getoption("benchpod_connection")
        or config.getini("benchpod_connection")
        or os.environ.get(ENV_VAR)
    )


@pytest.fixture(scope="session")
def benchpod_connection(pytestconfig: "pytest.Config") -> str:
    """The configured connection string, or skip the test if none is set."""
    conn = _resolve_connection(pytestconfig)
    if not conn:
        pytest.skip(
            "no BenchPod connection configured; pass --benchpod-connection=... "
            f"or set {ENV_VAR}"
        )
    return conn


@pytest.fixture(scope="session")
def benchpod(benchpod_connection: str, pytestconfig: "pytest.Config") -> Iterator[BenchPod]:
    """A connected :class:`BenchPod` for the test session."""
    api_base = pytestconfig.getoption("benchpod_api_base") or os.environ.get("BENCHPOD_API_BASE")
    device = BenchPod(benchpod_connection, api_base=api_base)
    try:
        yield device
    finally:
        device.close()


@pytest.fixture
def benchpod_target(benchpod: BenchPod) -> Iterator[BenchPod]:
    """A BenchPod whose target is powered on for the test, off at teardown."""
    from .constants import Efuse

    benchpod.power_on(Efuse.INTERNAL)
    try:
        yield benchpod
    finally:
        benchpod.power_off(Efuse.INTERNAL)


@pytest.fixture(scope="session")
def benchpod_pins(pytestconfig: "pytest.Config") -> BenchPodPins:
    """The wiring pin map, from --benchpod-* options (with defaults)."""
    get = pytestconfig.getoption
    nreset = get("benchpod_nreset")
    return BenchPodPins(
        swclk=get("benchpod_swclk"),
        swdio=get("benchpod_swdio"),
        nreset=(None if not nreset else nreset),
        uart_rx=get("benchpod_uart_rx"),
        uart_tx=get("benchpod_uart_tx"),
        i2c_sda=get("benchpod_i2c_sda"),
        i2c_scl=get("benchpod_i2c_scl"),
        efuse=get("benchpod_efuse"),
    )


@pytest.fixture(scope="session")
def pins(benchpod_pins: BenchPodPins) -> BenchPodPins:
    """Short alias for :func:`benchpod_pins` — the wiring pin map."""
    return benchpod_pins


@pytest.fixture
def firmware(pytestconfig: "pytest.Config") -> str:
    """Path to the DUT firmware image (``--benchpod-firmware``), or skip."""
    fw = pytestconfig.getoption("benchpod_firmware")
    if not fw:
        pytest.skip("no DUT firmware set; pass --benchpod-firmware=<path-to.elf>")
    return fw


@pytest.fixture
def benchpod_sensor(benchpod: BenchPod) -> Iterator[BenchPod]:
    """A BenchPod that disarms any emulated I2C sensor at teardown."""
    try:
        yield benchpod
    finally:
        try:
            benchpod.disable_i2c_sensor()
        except Exception:
            pass
