"""Make the package importable when running the tests from a source checkout
without an editable install, and configure the hardware tests' board.

Set EMBEDDEDCI_TEST_INSTALLED=1 to test the installed package instead (e.g. a built wheel)."""

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if os.environ.get("EMBEDDEDCI_TEST_INSTALLED") != "1" and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

pytest_plugins = ["pytester"]


def pytest_configure(config):
    # e2e/conftest.py applies this marker, but e2e_bench.py creates it when that conftest is
    # imported, before the conftest's own pytest_configure could register it.
    config.addinivalue_line(
        "markers", "bench_needs(*names): the e2e test uses these optional bench resources (free "
        "LA channels and the like, e2e/e2e_bench.py NEEDS); it skips when the bench sets them to "
        "'none'")
    config.addinivalue_line(
        "markers", "firmware_3: the test's fake pod is firmware 3.x on purpose (no api in its "
        "status): conftest does not read it as firmware 4.0")


@pytest.fixture(scope="session")
def benchpod_la_voltage():
    """I/O voltage of the bench boards the hardware tests run against: 3.3 V.

    Change this to 1.8 when testing a 1V8 board (or pass --benchpod-la-voltage for one run).
    """
    return 3.3


@pytest.fixture(autouse=True)
def _isolated_benchpod_cli_config(request, tmp_path_factory, monkeypatch):
    """Unit tests never see this machine's benchpod-cli config (its saved connection) or its
    `benchpod login` session: both change what BenchPod() connects to and where its wiring comes
    from. Hardware tests (e2e, examples) keep them."""
    path = str(request.node.fspath)
    if "e2e" in os.path.basename(path) or os.sep + "e2e" + os.sep in path \
            or os.sep + "examples" + os.sep in path:
        return
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))


def as_firmware_4(status):
    """A fake pod's ``status`` written for firmware 3.x (a ``caps`` list, no ``api``) as firmware
    4.0 sends it: ``api`` 2, ``features_rev``, ``gateware`` and the facts the list implied. A
    status that sets ``api`` comes back unchanged (the tests of the api check)."""
    if not isinstance(status, dict) or "api" in status:
        return status
    st = dict(status)
    listed = "caps" in st
    caps = {str(c).lower() for c in st.pop("caps", None) or []}
    st.update(api=2, min_client_api=2)
    st.setdefault("features_rev", 10)
    st.setdefault("gateware", 59)
    if not listed:
        image = "loop"
    elif "dac_deep_replay" in caps:
        image = "deep"
    elif "dac_control_loop" in caps:
        image = "loop"
    else:   # a list without either image's feature: the emulation image, or a single image
        image = "emu" if st.get("sensors_digital") is True else "unknown"
    st.setdefault("fpga_image", image)
    if "sensors_digital" in caps:
        st.setdefault("sensors_digital", True)
    if "nrst_pin" in caps:
        st.setdefault("nrst_pin", True)
    hw = list(st.get("hw") or [])
    for fact, name in (("pod_current", "pod_current"), ("usb_cc", "usb_cc"),
                       ("adc_low_range", "adc_low_range"), ("wifi_scan", "wifi")):
        if fact in caps and name not in hw:
            hw.append(name)
    st["hw"] = hw
    return st


@pytest.fixture(autouse=True)
def _fake_pods_run_firmware_4(request, monkeypatch):
    """Unit tests' fake pods were written for firmware 3.x statuses: the SDK reads them as
    firmware 4.0 (:func:`as_firmware_4`). Hardware tests (e2e, examples) talk to real pods."""
    path = str(request.node.fspath)
    if os.sep + "e2e" + os.sep in path or os.sep + "examples" + os.sep in path:
        return
    if request.node.get_closest_marker("firmware_3"):
        return     # the test's fake is a firmware 3.x pod on purpose
    from embeddedci.benchpod.client import BenchPod

    real = BenchPod._caps_status
    monkeypatch.setattr(BenchPod, "_caps_status", lambda self: as_firmware_4(real(self)))
    # Fakes for firmware before la_dir (3.10.4) expect a connect that sends nothing: keep it so
    # for them (test_pin_directions.py covers the directions sent on connect).
    real_auto = BenchPod._auto_pin_directions

    def auto(self):
        t = getattr(self, "_transport", None)
        try:
            st = real(self) if t is not None and type(t).__name__ != "SerialTransport" else {}
        except Exception:
            st = {}
        if isinstance(st, dict) and "api" not in st and _before_la_dir(st.get("version")):
            self.pin_directions_status = "not sent: a fake pod written for firmware before la_dir"
            return
        real_auto(self)

    monkeypatch.setattr(BenchPod, "_auto_pin_directions", auto)


def _before_la_dir(version):
    import re

    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", str(version or ""))
    return m is None or tuple(int(x) for x in m.groups()) < (3, 10, 4)
