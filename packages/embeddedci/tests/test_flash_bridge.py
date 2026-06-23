"""Flash bridge tested against a fake openocd and a fake pod link.

No hardware and no real openocd: a tiny Python script stands in for openocd,
connecting back to the bridge's loopback port and exiting with a chosen code.
"""

import os
import stat
import threading

import pytest

from embeddedci.benchpod import flash as flashmod
from embeddedci.benchpod.errors import FlashError, TargetUnreachableError

FAKE_OPENOCD = """#!{python}
import os, sys, socket, struct
if any(a == "shutdown" for a in sys.argv):
    sys.exit(0)  # config-stage backend probe (supports_cmsis_dap_tcp)
port = None
for a in sys.argv[1:]:
    if a.startswith("cmsis-dap tcp port "):
        port = int(a.split()[-1])
if port is not None:
    s = socket.create_connection(("127.0.0.1", port))
    # one cmsis_dap_tcp request frame: "DAP\\0" + len(u16) + type(u8) + rsv(u8) + payload
    payload = b"DAPREQ"
    frame = b"DAP\\x00" + struct.pack("<H", len(payload)) + b"\\x00\\x00" + payload
    s.sendall(frame)
    s.settimeout(0.5)
    try:
        s.recv(64)
    except OSError:
        pass
    s.close()
err = os.environ.get("FAKE_OPENOCD_STDERR", "")
if err:
    sys.stderr.write(err)
    sys.stderr.flush()
sys.exit(int(os.environ.get("FAKE_OPENOCD_EXIT", "0")))
"""


class FakePodLink:
    """A RawLink whose read blocks until close() (mimics a quiet bitbang link)."""

    def __init__(self):
        self._closed = threading.Event()
        self.written = bytearray()

    def read(self, n):
        # Block until the bridge closes us; never spuriously signal EOF.
        self._closed.wait()
        return b""

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def close(self):
        self._closed.set()


@pytest.fixture
def fake_openocd(tmp_path):
    import sys

    path = tmp_path / "fake-openocd"
    path.write_text(FAKE_OPENOCD.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(path)


def _flash(fake_openocd, pod_link, **env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return flashmod._run_bridge(
            fake_openocd,
            ["-f", "target/stm32f1x.cfg", "-c", "program fw.elf verify reset exit"],
            pod_link,
            timeout=10,
        )
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_successful_flash(fake_openocd):
    link = FakePodLink()
    result = _flash(fake_openocd, link, FAKE_OPENOCD_EXIT=0)
    assert result.ok
    assert result.returncode == 0
    assert not result.target_unreachable
    # OpenOCD's DAP request frame was translated (2-byte len prefix) onto the pod.
    assert b"DAPREQ" in bytes(link.written)
    assert bytes(link.written)[:2] == b"\x06\x00"  # len=6, little-endian


def test_failed_flash_nonzero_exit(fake_openocd):
    link = FakePodLink()
    result = _flash(fake_openocd, link, FAKE_OPENOCD_EXIT=1)
    assert not result.ok
    assert result.returncode == 1


def test_target_unreachable_detected(fake_openocd):
    link = FakePodLink()
    result = _flash(
        fake_openocd, link,
        FAKE_OPENOCD_EXIT=1,
        FAKE_OPENOCD_STDERR="Error: cannot read IDR\n",
    )
    assert not result.ok
    assert result.target_unreachable
    with pytest.raises(TargetUnreachableError):
        flashmod.raise_for_result(result)


def test_raise_for_result_on_plain_failure():
    from embeddedci.benchpod.flash import FlashResult

    with pytest.raises(FlashError):
        flashmod.raise_for_result(
            FlashResult(ok=False, returncode=1, stdout="", stderr="boom")
        )


def test_build_args_program_command():
    args = flashmod.build_openocd_args(
        "target/stm32f1x.cfg", "fw.elf", "",
        verify=True, reset=True, connect_under_reset=True,
        clear_reset_events=True, extra_configs=(), extra_args=(),
    )
    assert "-f" in args and "target/stm32f1x.cfg" in args
    assert any("connect_assert_srst" in a for a in args)
    assert any(a == "program fw.elf verify reset exit" for a in args)
