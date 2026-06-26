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
from typing import ClassVar, Dict, Iterator, Optional

import pytest

from .client import BenchPod
from .connection import ENV_VAR

# Pull-up resistors exist only on LA1-8, with a value fixed per channel (the pod
# has none on LA9-12). Source of truth: bench-pod-firmware/docs/API.md (`pullup`).
_PULLUP_OHMS: Dict[int, str] = {
    1: "4.7k", 2: "4.7k", 3: "2.2k", 4: "2.2k",
    5: "10k", 6: "10k", 7: "10k", 8: "10k",
}


class BenchPodPins:
    """The pod's 12 generic logic-analyzer channels (``pin_1`` .. ``pin_12``)
    plus the target-power ``efuse``.

    The pod has **no dedicated SWD/UART/I2C pins** — it exposes 12 identical LA
    channels (LA1..LA12) and any DUT signal can be wired to any of them. So this
    fixture names the channels by number, not by role: ``pins.pin_11`` is LA
    channel 11, nothing more. A test maps its own bench wiring at the top of the
    file, e.g. ``swclk = pins.pin_11`` — that mapping is bench-specific and lives
    with the test, not here.

    Pull-ups are available on **LA1-8 only** (LA1/2=4.7k, LA3/4=2.2k, LA5-8=10k);
    LA9-12 have none. Use :meth:`has_pullup` / :data:`pullup_ohms` to check before
    relying on one (e.g. for an open-drain I2C bus).
    """

    #: channel -> fixed pull-up resistance, for channels that have one.
    PULLUP_OHMS: ClassVar[Dict[int, str]] = dict(_PULLUP_OHMS)

    def __init__(self, efuse: int = 1) -> None:
        # LA1..LA12 are identity-numbered: pin_<n> is simply channel <n>.
        for channel in range(1, 13):
            setattr(self, f"pin_{channel}", channel)
        #: target-power eFuse rail (1 = internal 5V, 2 = external).
        self.efuse = efuse

    @staticmethod
    def has_pullup(channel: int) -> bool:
        """True if LA ``channel`` has a (fixed) pull-up resistor (LA1-8 only)."""
        return channel in _PULLUP_OHMS

    @staticmethod
    def pullup_ohms(channel: int) -> Optional[str]:
        """The pull-up value on LA ``channel`` (e.g. ``"4.7k"``), or None."""
        return _PULLUP_OHMS.get(channel)


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
    group.addoption(
        "--benchpod-efuse", action="store", type=int, default=1,
        dest="benchpod_efuse",
        help="Target-power eFuse rail: 1 = internal 5V, 2 = external (default 1).",
    )
    group.addoption(
        "--benchpod-discover",
        action="store_true",
        default=False,
        dest="benchpod_discover",
        help="When no connection is configured, find a BenchPod on the LAN via "
        "mDNS (needs the 'zeroconf' extra). Errors if zero or several are found.",
    )
    group.addoption(
        "--benchpod-build-target",
        action="store",
        default=None,
        dest="benchpod_build_target",
        help="Target/platform id recorded with a reported build (e.g. 'stm32f4'); "
        "used by the 'build_report' fixture. Falls back to BENCHPOD_BUILD_TARGET.",
    )
    group.addoption(
        "--benchpod-lease-wait",
        action="store",
        type=float,
        default=600.0,
        dest="benchpod_lease_wait",
        help="For the cloud ('embeddedci:') destination, how long (seconds) to wait for a busy "
        "shared device to free before failing (default 600). Concurrent runs queue on the device.",
    )
    group.addoption(
        "--benchpod-no-lease",
        action="store_true",
        default=False,
        dest="benchpod_no_lease",
        help="Do not take an exclusive lease on the cloud device (allows concurrent access; "
        "only safe when you know no other run will use the same device).",
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
    explicit = (
        config.getoption("benchpod_connection")
        or config.getini("benchpod_connection")
        or os.environ.get(ENV_VAR)
    )
    if explicit:
        return explicit
    # No explicit target: opt into LAN auto-discovery. "discover" is turned into
    # an mDNS lookup by connection.parse_connection().
    if config.getoption("benchpod_discover"):
        return "discover"
    return None


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
    """A connected :class:`BenchPod` for the test session.

    For the cloud (``embeddedci:``) destination this takes an exclusive lease on the shared device,
    waiting up to ``--benchpod-lease-wait`` seconds if another run is using it (so concurrent CI
    runs queue instead of colliding). Disable with ``--benchpod-no-lease``.
    """
    api_base = pytestconfig.getoption("benchpod_api_base") or os.environ.get("BENCHPOD_API_BASE")
    device = BenchPod(
        benchpod_connection,
        api_base=api_base,
        lease=not pytestconfig.getoption("benchpod_no_lease"),
        lease_wait=pytestconfig.getoption("benchpod_lease_wait"),
    )
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
    """The pod's generic LA channels (``pin_1`` .. ``pin_12``) and the eFuse rail.

    Channels are not roles — map your bench wiring (which signal is on which LA
    channel) in the test itself. The eFuse rail comes from ``--benchpod-efuse``.
    """
    return BenchPodPins(efuse=pytestconfig.getoption("benchpod_efuse"))


@pytest.fixture(scope="session")
def pins(benchpod_pins: BenchPodPins) -> BenchPodPins:
    """Short alias for :func:`benchpod_pins` — the pod's LA channels + eFuse."""
    return benchpod_pins


@pytest.fixture
def firmware(pytestconfig: "pytest.Config") -> str:
    """Path to the DUT firmware image (``--benchpod-firmware``), or skip."""
    fw = pytestconfig.getoption("benchpod_firmware")
    if not fw:
        pytest.skip("no DUT firmware set; pass --benchpod-firmware=<path-to.elf>")
    return fw


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: "pytest.Item", call: "pytest.CallInfo"):  # type: ignore[name-defined]
    """Stash each phase's report on the item so the ``build_report`` fixture's teardown can read
    the test outcome (``item.rep_call`` etc.)."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def build_report(request: "pytest.FixtureRequest", pytestconfig: "pytest.Config") -> Iterator[object]:
    """Opt a test into reporting its run as a GitHub-sourced build on embeddedci.com.

    Requesting this fixture is the explicit opt-in: a test that does not request it never makes any
    cloud build call. Even when requested, reporting is *active only* inside GitHub Actions with a
    mintable OIDC token — locally and in non-GitHub CI the fixture yields an inert no-op reporter, so
    the same test keeps running unchanged.

    Use it to upload the firmware that was tested and record the wiring, e.g.::

        def test_boots(dut, wiring, firmware, build_report):
            build_report.record_wiring(target="target/stm32f4x.cfg", swclk=11, swdio=12, nreset=3)
            build_report.upload_artifacts([firmware])
            ...  # the pytest pass/fail is captured automatically

    The build status (pass/fail) is captured automatically from the test result at teardown.
    """
    from .ci import make_build_reporter

    target = pytestconfig.getoption("benchpod_build_target") or os.environ.get("BENCHPOD_BUILD_TARGET") or ""
    reporter = make_build_reporter(
        api_base=pytestconfig.getoption("benchpod_api_base") or os.environ.get("BENCHPOD_API_BASE"),
        target=target,
    )
    try:
        yield reporter
    finally:
        rep_call = getattr(request.node, "rep_call", None)
        rep_setup = getattr(request.node, "rep_setup", None)
        if rep_setup is not None and not rep_setup.passed:
            reporter.set_result(False, "test setup failed")
        elif rep_call is not None:
            reporter.set_result(rep_call.passed, "" if rep_call.passed else rep_call.longreprtext)
        else:
            reporter.set_result(False, "test did not run")
        log_text = _collect_pytest_log(request.node)
        if log_text:
            reporter.upload_logs("pytest.log", log_text)
        reporter.finalize()


def _collect_pytest_log(node: "pytest.Item") -> str:
    """Assemble the captured pytest output (stdout/stderr/log + any traceback) across the test's
    setup/call/teardown phases, so it can be uploaded as the build's pytest log."""
    parts = []
    for phase in ("setup", "call", "teardown"):
        rep = getattr(node, f"rep_{phase}", None)
        if rep is None:
            continue
        for label, attr in (("stdout", "capstdout"), ("stderr", "capstderr"), ("log", "caplog")):
            text = (getattr(rep, attr, "") or "").rstrip()
            if text:
                parts.append(f"===== {phase} {label} =====\n{text}")
        longrepr = (getattr(rep, "longreprtext", "") or "").rstrip()
        if longrepr:
            parts.append(f"===== {phase} traceback =====\n{longrepr}")
    return "\n\n".join(parts)


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
