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

# Fixed bias network per LA channel, one for each of the pod's CTRL1..CTRL8
# lines. LA1-LA6 pull UP to +3V3; LA7/LA8 pull DOWN. LA9-LA12 have no network at
# all. Source of truth: benchpod-firmware i2c_bus.c (`pca9555_set_la_pullup`).
_PULL_OHMS: Dict[int, str] = {
    1: "4.7k", 2: "4.7k", 3: "2.2k", 4: "2.2k",
    5: "10k", 6: "10k", 7: "10k", 8: "10k",
}
#: The channels whose network pulls DOWN rather than up.
_PULLDOWN_CHANNELS = frozenset({7, 8})


class BenchPodPins:
    """The pod's 12 generic logic-analyzer channels (``pin_1`` .. ``pin_12``)
    plus the target-power ``efuse``.

    The pod has **no dedicated SWD/UART/I2C pins** — it exposes 12 identical LA
    channels (LA1..LA12) and any DUT signal can be wired to any of them. So this
    fixture names the channels by number, not by role: ``pins.pin_11`` is LA
    channel 11, nothing more. A test maps its own bench wiring at the top of the
    file, e.g. ``swclk = pins.pin_11`` — that mapping is bench-specific and lives
    with the test, not here.

    Eight of the twelve channels carry a fixed bias network, and the direction is
    NOT the same for all of them:

    ===========  =========  =====================================
    Channels     Network    Value
    ===========  =========  =====================================
    LA1, LA2     pull-up    4.7k
    LA3, LA4     pull-up    2.2k
    LA5, LA6     pull-up    10k
    LA7, LA8     pull-down  10k
    LA9 - LA12   none       --
    ===========  =========  =====================================

    Use :meth:`has_pullup` before relying on one for an open-drain bus (I2C):
    LA7/LA8 would pull the bus the wrong way. The networks are referenced to
    +3V3, so the pod only engages them while the LA bank is at 3.3 V.
    """

    #: channel -> resistance, for the channels that pull UP.
    PULLUP_OHMS: ClassVar[Dict[int, str]] = {
        ch: ohms for ch, ohms in _PULL_OHMS.items() if ch not in _PULLDOWN_CHANNELS
    }
    #: channel -> resistance, for the channels that pull DOWN.
    PULLDOWN_OHMS: ClassVar[Dict[int, str]] = {
        ch: ohms for ch, ohms in _PULL_OHMS.items() if ch in _PULLDOWN_CHANNELS
    }

    def __init__(self, efuse: int = 1) -> None:
        # LA1..LA12 are identity-numbered: pin_<n> is simply channel <n>.
        for channel in range(1, 13):
            setattr(self, f"pin_{channel}", channel)
        #: target-power eFuse rail (1 = internal 5V, 2 = external).
        self.efuse = efuse

    @staticmethod
    def has_pullup(channel: int) -> bool:
        """True if LA ``channel``'s fixed resistor pulls UP (LA1-LA6 only).

        False for LA7/LA8, whose resistors pull DOWN — asking for a pull-up
        there and getting one would silently drive an open-drain bus low.
        """
        return channel in _PULL_OHMS and channel not in _PULLDOWN_CHANNELS

    @staticmethod
    def has_pulldown(channel: int) -> bool:
        """True if LA ``channel``'s fixed resistor pulls DOWN (LA7/LA8 only)."""
        return channel in _PULLDOWN_CHANNELS

    @staticmethod
    def pullup_ohms(channel: int) -> Optional[str]:
        """The pull-UP value on LA ``channel`` (e.g. ``"4.7k"``), or None.

        None for LA7/LA8 even though they carry a resistor: it pulls down.
        Use :meth:`pull_ohms` for the value regardless of direction.
        """
        if channel in _PULLDOWN_CHANNELS:
            return None
        return _PULL_OHMS.get(channel)

    @staticmethod
    def pull_ohms(channel: int) -> Optional[str]:
        """The fixed resistance on LA ``channel``, whichever way it pulls."""
        return _PULL_OHMS.get(channel)

    @staticmethod
    def pull_direction(channel: int) -> Optional[str]:
        """``"up"``, ``"down"``, or None when the channel has no resistor."""
        if channel not in _PULL_OHMS:
            return None
        return "down" if channel in _PULLDOWN_CHANNELS else "up"


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
        "--benchpod-api-key",
        action="store",
        default=None,
        dest="benchpod_api_key",
        help="embeddedci API key (eci_…) for the cloud waveform library + server-side DAC "
        "replay on a LAN/serial connection. Not needed over the cloud destination "
        "('embeddedci:<device>'), which reuses its session token (incl. GitHub OIDC). "
        "Falls back to BENCHPOD_API_KEY.",
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
        "--benchpod-la-voltage", action="store", type=float, default=None,
        dest="benchpod_la_voltage",
        help="LA I/O-bank voltage for the DUT: 1.8 or 3.3 (volts; 1800/3300 mV also "
        "accepted). Set on the pod once per session before any LA op (flash/SWD, "
        "UART, LA capture, pull-ups, I2C-sensor). Required — the pod refuses LA ops "
        "until a voltage is chosen. Falls back to BENCHPOD_LA_VOLTAGE.",
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
    config.addinivalue_line(
        "markers",
        "benchpod_capability(name): skip the test unless the connected device advertises the "
        "given capability (e.g. 'scope', 'analyzer', 'dac_replay', 'dac_deep_replay').",
    )


@pytest.fixture(autouse=True)
def _benchpod_capability_gate(request: "pytest.FixtureRequest") -> None:
    """Honor ``@pytest.mark.benchpod_capability(...)`` — skip when the device lacks it.

    Autouse so it runs during setup (when fixtures resolve): a no-op unless the test carries the
    marker, in which case it resolves the ``benchpod`` device and skips on any missing capability.
    """
    marks = list(request.node.iter_markers(name="benchpod_capability"))
    if not marks:
        return
    caps = request.getfixturevalue("benchpod").capabilities  # may itself skip if no connection
    for mark in marks:
        for name in mark.args:
            if not getattr(caps, str(name), False):
                pytest.skip(f"device does not advertise capability {name!r}")


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
    api_key = pytestconfig.getoption("benchpod_api_key") or os.environ.get("BENCHPOD_API_KEY")
    device = BenchPod(
        benchpod_connection,
        api_base=api_base,
        api_key=api_key,
        lease=not pytestconfig.getoption("benchpod_no_lease"),
        lease_wait=pytestconfig.getoption("benchpod_lease_wait"),
    )
    try:
        la_voltage = pytestconfig.getoption("benchpod_la_voltage")
        if la_voltage is None:
            env_v = os.environ.get("BENCHPOD_LA_VOLTAGE")
            la_voltage = float(env_v) if env_v else None
        if la_voltage is not None:
            # Select the LA I/O-bank voltage once, before any LA-bank op runs.
            device.set_la_voltage(la_voltage)
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


@pytest.fixture(scope="session")
def benchpod_capabilities(benchpod: BenchPod):
    """The connected device's resolved :class:`~embeddedci.benchpod.capabilities.Capabilities`."""
    return benchpod.capabilities


@pytest.fixture
def benchpod_dac(benchpod: BenchPod) -> Iterator[BenchPod]:
    """A BenchPod that stops any running DAC output (generate/replay) at teardown."""
    try:
        yield benchpod
    finally:
        try:
            benchpod.dac_stop()
        except Exception:
            pass


@pytest.fixture
def benchpod_waveforms(benchpod: BenchPod):
    """The cloud waveform library, with automatic cleanup of anything created during the test.

    Yields the :class:`~embeddedci.benchpod.waveforms.WaveformLibrary`; any waveform saved through
    it (``save_recording`` / ``save_waveform`` / ``save_segments``) is tracked and deleted at
    teardown so a test never leaves library litter behind. Needs an API key
    (``--benchpod-api-key`` / ``BENCHPOD_API_KEY``); skips otherwise.
    """
    try:
        lib = benchpod.waveforms
    except Exception as exc:  # no API key / server access
        pytest.skip(f"cloud waveform library unavailable: {exc}")
        return

    created: list = []
    for meth in ("save_recording", "save_waveform", "save_segments"):
        orig = getattr(lib, meth)

        def wrap(*a, _orig=orig, **k):
            wf = _orig(*a, **k)
            wid = getattr(wf, "id", None)
            if wid:
                created.append(wid)
            return wf

        setattr(lib, meth, wrap)
    try:
        yield lib
    finally:
        for wid in created:
            try:
                lib.delete(wid)
            except Exception:
                pass
