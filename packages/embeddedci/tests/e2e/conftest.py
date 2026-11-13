"""End-to-end tests of the 2.x SDK against a real BenchPod — see README.md in this directory.

The LA voltage comes from ``tests/conftest.py`` (3.3 V). The bench wiring below matches the
EmbeddedCI bench (NUCLEO-F446RE running examples/scenario-sensors-stm32); override any of it with
``BENCHPOD_E2E_*`` environment variables for another bench (parsed in e2e_bench.py).

Tests that drive "free" LA channels carry ``bench_needs`` markers (``FREE_LA`` and friends from
e2e_bench.py). A bench that wires every channel to the DUT sets those variables to ``none`` and
the marked tests skip, saying which variable to set.

On the digital-only board (no analog front end) the analog tests skip: they carry
``ANALOG`` (``@pytest.mark.benchpod_capability("analog")``) or call ``require_analog`` (both in
e2e_helpers.py).
``BENCHPOD_EXPECT_BOARD=analog|digital`` makes the run fail when the pod reports the other board,
so a pod whose analog section went undetected cannot skip half the suite and pass green.
"""

from __future__ import annotations

import os

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FpgaImage

from e2e_bench import Bench, bench_from_env, missing_reason
from e2e_helpers import check_expected_board, require_analog


def pytest_report_header(config):
    import embeddedci
    from importlib.metadata import PackageNotFoundError, version
    try:
        ver = version("embeddedci")
    except PackageNotFoundError:
        ver = "not installed"
    return f"embeddedci under test: {os.path.dirname(embeddedci.__file__)} (installed dist: {ver})"


def _bench_or_usage_error() -> Bench:
    try:
        return bench_from_env(os.environ)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None


def pytest_collection_modifyitems(config, items):
    """Skip the tests whose ``bench_needs`` resources this bench does not have (set to 'none')."""
    marked = [item for item in items if item.get_closest_marker("bench_needs")]
    if not marked:
        return
    bench = _bench_or_usage_error()
    for item in marked:
        names = [n for mark in item.iter_markers("bench_needs") for n in mark.args]
        reason = missing_reason(bench, names)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session")
def bench() -> Bench:
    return _bench_or_usage_error()


def quiesce(pod: BenchPod) -> None:
    """Stop every output a test may have left running (never raises)."""
    for step in (pod.dac_stop, lambda: pod.analog_path("off") if pod.can_analog else None,
                 lambda: pod.sensors.stop() if pod.supports("sensors") else None,
                 pod.can_respond_clear, pod.can_disable,
                 lambda: pod.release_gpio() if pod.capabilities.la_pins else None):
        try:
            step()
        except Exception:
            pass


@pytest.fixture(scope="session")
def pod(benchpod: BenchPod):
    """The session's pod; the gateware image it started on is restored at the end (the sensor
    tests leave the emulation image loaded: the pod never switches back by itself)."""
    started = benchpod._running_image()
    check_expected_board(benchpod)
    yield benchpod
    quiesce(benchpod)
    try:
        now = benchpod._running_image()
        if started is not None and now is not None and now.image != started.image:
            benchpod.fpga_image(started.image)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _leave_the_pod_quiet(request):
    yield
    if "pod" in request.fixturenames:
        quiesce(request.getfixturevalue("pod"))


def _on_image(pod: BenchPod, image: FpgaImage, flag: str) -> BenchPod:
    require_analog(pod)  # both images are for the DAC; the digital board has neither feature
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
def rev3(pod: BenchPod, bench: Bench) -> bool:
    """Whether the pod has the rev3 extras (reset pin, USB-C CC, 1.8 V bank).

    With BENCHPOD_E2E_BOARD_REV set, a pod that reports another revision fails here. Without it a
    v3 board whose strap is misread as v2 would take every test's v2 branch and pass green.
    """
    status = pod.status()
    detected = str(status.get("board_rev", ""))
    if bench.board_rev and detected != bench.board_rev:
        pytest.fail(f"the pod reports board_rev={detected!r} (strap {status.get('board_rev_mv')} mV), "
                    f"the bench expects {bench.board_rev!r} (BENCHPOD_E2E_BOARD_REV)")
    return bool(status.get("nrst_pin"))
