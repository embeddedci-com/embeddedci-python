"""End-to-end tests of the 2.x SDK against a real BenchPod — see README.md in this directory.

The LA voltage comes from ``tests/conftest.py`` (3.3 V). The bench wiring below matches the
EmbeddedCI bench (NUCLEO-F446RE running examples/scenario-sensors-stm32); override any of it with
``BENCHPOD_E2E_*`` environment variables for another bench.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FpgaImage


def pytest_report_header(config):
    import embeddedci
    from importlib.metadata import PackageNotFoundError, version
    try:
        ver = version("embeddedci")
    except PackageNotFoundError:
        ver = "not installed"
    return f"embeddedci under test: {os.path.dirname(embeddedci.__file__)} (installed dist: {ver})"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"BENCHPOD_E2E_{name}", default)


@dataclass(frozen=True)
class Bench:
    """How the DUT is wired to the pod, and what the tests may drive."""

    uart_rx: int          # LA channel wired to the DUT's TX (the pod samples it)
    uart_tx: int          # LA channel wired to the DUT's RX (the pod drives it)
    i2c_sda: int
    i2c_scl: int
    swclk: int
    swdio: int
    nreset: bool          # DUT reset wired to the pod's reset pin (rev3 pods only)
    efuse: int            # target-power rail the DUT is on
    target_cfg: str       # OpenOCD target config
    free_la: Tuple[int, int]   # two LA channels nothing is wired to
    free_pull_la: int     # a biased channel (LA1-LA8) nothing depends on
    dac_max_v: float      # the highest voltage the tests drive on a DAC output
    allow_12v: bool       # whether the bipolar 12v output may be driven (±1 V)


@pytest.fixture(scope="session")
def bench() -> Bench:
    free = tuple(int(x) for x in _env("FREE_LA", "9,10").split(","))
    return Bench(
        uart_rx=int(_env("UART_RX", "3")),
        uart_tx=int(_env("UART_TX", "4")),
        i2c_sda=int(_env("I2C_SDA", "2")),
        i2c_scl=int(_env("I2C_SCL", "1")),
        swclk=int(_env("SWCLK", "11")),
        swdio=int(_env("SWDIO", "12")),
        nreset=_env("NRESET", "0") == "1",
        efuse=int(_env("EFUSE", "1")),
        target_cfg=_env("TARGET_CFG", "target/stm32f4x.cfg"),
        free_la=(free[0], free[1]),
        free_pull_la=int(_env("FREE_PULL_LA", "6")),
        dac_max_v=float(_env("DAC_MAX_V", "3.3")),
        allow_12v=_env("ALLOW_12V", "0") == "1",
    )


def quiesce(pod: BenchPod) -> None:
    """Stop every output a test may have left running (never raises)."""
    for step in (pod.dac_stop, lambda: pod.analog_path("off"), pod.disable_i2c_sensor,
                 pod.can_respond_clear, pod.can_disable,
                 lambda: pod.release_gpio() if pod.capabilities.la_pins else None):
        try:
            step()
        except Exception:
            pass


@pytest.fixture(scope="session")
def pod(benchpod: BenchPod):
    """The session's pod; the gateware image it started on is restored at the end."""
    started_on_loop = benchpod.refresh_capabilities().dac_control_loop
    yield benchpod
    quiesce(benchpod)
    try:
        if benchpod.refresh_capabilities().dac_control_loop != started_on_loop:
            benchpod.fpga_image(FpgaImage.LOOP if started_on_loop else FpgaImage.DEEP_REPLAY)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _leave_the_pod_quiet(request):
    yield
    if "pod" in request.fixturenames:
        quiesce(request.getfixturevalue("pod"))


def _on_image(pod: BenchPod, image: FpgaImage, flag: str) -> BenchPod:
    if not getattr(pod.refresh_capabilities(), flag):
        try:
            pod.fpga_image(image)
        except BenchPodError as exc:
            pytest.skip(f"cannot switch to the {image.name} gateware image: {exc}")
        if not getattr(pod.refresh_capabilities(), flag):
            pytest.skip(f"the {image.name} image does not advertise {flag}")
    return pod


@pytest.fixture
def loop_image(pod: BenchPod) -> BenchPod:
    """The pod running the control-loop gateware image."""
    return _on_image(pod, FpgaImage.LOOP, "dac_control_loop")


@pytest.fixture
def deep_image(pod: BenchPod) -> BenchPod:
    """The pod running the deep-replay gateware image."""
    return _on_image(pod, FpgaImage.DEEP_REPLAY, "dac_deep_replay")


@pytest.fixture(scope="session")
def rev3(pod: BenchPod) -> bool:
    """Whether the pod has the rev3 extras (reset pin, USB-C CC, 1.8 V bank)."""
    return bool(pod.status().get("nrst_pin"))
