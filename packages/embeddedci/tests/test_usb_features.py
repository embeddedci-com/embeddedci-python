"""The firmware 4.0.3 USB features that sit on top of host mode and flashing: the board's USB serial
port (``open_uart(port="usb")``), ``usb_check``, ``usb_hid``, the PICOBOOT loader and the flash
read-back, against a fake pod (feature table revision 15)."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, usb_host
from embeddedci.benchpod.errors import (
    UnsupportedFeatureError,
    UsbCheckNoPowerError,
    UsbCheckRefusedError,
    UsbFlashError,
    UsbHostError,
    UsbSerialBusyError,
    UsbSerialUnavailableError,
    firmware_error,
)
from embeddedci.benchpod.transport.tcp import TcpTransport

import wire
from test_usb_host import (
    CH340,
    NUCLEO,
    PICO,
    FakePod,
    FakeTransport,
    device,
    iface,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(usb_host.time, "sleep", lambda s: None)
    from embeddedci.benchpod import client
    monkeypatch.setattr(client.time, "sleep", lambda s: None)


class UsbPod(FakePod):
    """FakePod plus usb_check, usb_hid and the raw UART link of ``uart_proxy_start``."""

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("features_rev", 15)
        kw.setdefault("dev", PICO)
        super().__init__(**kw)
        self.check_polls = 2
        self.check_left = 0
        self.check_have = False
        self.check_req: Dict[str, Any] = {}
        self.check_pass = True
        self.check_error = ""
        self.hid_capturing = False
        self.hid_reports: List[Dict[str, Any]] = []
        self.hid_descriptor = bytes(range(40))
        self.hid_page = 16
        self.starts: List[Dict[str, Any]] = []
        self.uart_error: Optional[Exception] = None
        self.link = object()

    def command(self, req: Dict[str, Any]) -> Any:
        cmd = req.get("cmd")
        if cmd == "usb_check":
            self.cmds.append(dict(req))
            if req.get("abort"):
                self.check_left, self.check_have, self.check_error = 0, True, "aborted"
                return self._check_state(False)
            if "test" in req:
                self.seq.append("check_start")
                self.check_req = dict(req)
                self.check_left, self.check_have = self.check_polls, False
                return self._check_state(True)
            if self.check_left > 0:
                self.check_left -= 1
                if self.check_left == 0:
                    self.check_have = True
                return self._check_state(self.check_left > 0)
            return self._check_state(False)
        if cmd == "usb_hid":
            self.cmds.append(dict(req))
            if "capture" in req:
                self.hid_capturing = bool(req["capture"])
                return {"capturing": self.hid_capturing, "buffered": len(self.hid_reports), "dropped": 0}
            if req.get("read"):
                self.hid_capturing = True
                n = int(req.get("count", 1))
                inst = req.get("instance")
                take = [r for r in self.hid_reports if inst is None or r["instance"] == inst][:n]
                return {"reports": take, "count": len(take), "dropped": 0, "capturing": self.hid_capturing}
            off = int(req.get("offset", 0))
            chunk = self.hid_descriptor[off:off + self.hid_page]
            return {"attached": True, "interfaces": [
                {"instance": 0, "interface": 0, "protocol": "keyboard",
                 "descriptor_len": len(self.hid_descriptor), "descriptor_offset": off,
                 "truncated": off + self.hid_page < len(self.hid_descriptor), "descriptor": chunk.hex()}]}
        return super().command(req)

    def _check_state(self, running: bool) -> Dict[str, Any]:
        d: Dict[str, Any] = {"running": running, "have_result": not running,
                             "test": self.check_req.get("test", ""), "phase": "gap" if running else "done",
                             "done": 3 if running else 10, "total": 10, "ms": 120}
        if not running:
            d.update({"pass": self.check_pass, "error": self.check_error,
                      "result": {"vid": 0x2E8A, "pid": 3, "cycles": 10, "ok": 10,
                                 "mount_ms": {"min": 80, "avg": 90, "max": 100}}})
        return d

    def flash_state(self) -> Dict[str, Any]:
        return self._flash_state(False)

    def _flash_state(self, running: bool) -> Dict[str, Any]:
        d = super()._flash_state(running)
        if not running:
            d["note"] = getattr(self, "note", "")
        return d


class UsbTransport(FakeTransport):
    def uart_proxy_start(self, rx: Any, tx: Any, baud: int, **kw: Any) -> Any:
        self.pod.starts.append({"rx": rx, "tx": tx, "baud": baud, **kw})
        if self.pod.uart_error is not None:
            raise self.pod.uart_error
        return self.pod.link


def make(pod: UsbPod, **kw: Any) -> BenchPod:
    return BenchPod(transport=UsbTransport(pod, **kw), lease=False, wiring={})


# -- feature gating -----------------------------------------------------------------------------

@pytest.mark.parametrize("call, feature", [
    (lambda bp: bp.usb_check("power"), "usb_check"),
    (lambda bp: bp.usb_check_status(), "usb_check"),
    (lambda bp: bp.usb_check_abort(), "usb_check"),
    (lambda bp: bp.usb_hid_interfaces(), "usb_hid"),
    (lambda bp: bp.usb_hid_capture(), "usb_hid"),
    (lambda bp: bp.usb_hid_read(), "usb_hid"),
    (lambda bp: bp.open_uart(port="usb"), "uart_usb"),
    (lambda bp: bp.capture_uart(port="usb", duration=1), "uart_usb"),
])
def test_pod_without_revision_15_gets_a_clear_error(call, feature):
    pod = UsbPod(features_rev=14)
    with pytest.raises(UnsupportedFeatureError, match="needs firmware 4.0.3") as info:
        call(make(pod))
    assert info.value.feature == feature
    assert pod.cmds == [] and pod.starts == []     # nothing was sent


def test_the_bundled_table_knows_revision_15():
    from embeddedci.benchpod import feature_map
    assert feature_map.feature_map()["revision"] >= 15
    bp = make(UsbPod())
    for f in ("uart_usb", "usb_check", "usb_hid", "usb_flash_picoboot", "usb_flash_readback"):
        assert bp.supports(f), f


# -- the USB serial port: open_uart / capture_uart with port="usb" -------------------------------

def test_open_uart_usb_sends_no_pins_and_uses_the_defaults():
    pod = UsbPod()
    with make(pod).open_uart(port="usb"):
        pass
    assert pod.starts == [{"rx": None, "tx": None, "baud": 115200, "port": "usb", "parity": "none",
                           "stop_bits": 1, "dtr": True, "rts": True}]


def test_open_uart_usb_takes_baud_frame_and_lines_and_ignores_the_wiring_profile():
    pod = UsbPod()
    bp = BenchPod(transport=UsbTransport(pod), lease=False,
                  wiring={"version": 2, "uart_rx": 5, "uart_tx": 4, "uart_baud": 9600,
                          "uart_parity": "even", "uart_stop_bits": 2})
    bp.open_uart(port="usb", baud=921600, dtr=False, rts=True, parity="odd", stop_bits=2).close()
    bp.open_uart(port="usb").close()
    assert pod.starts[0] == {"rx": None, "tx": None, "baud": 921600, "port": "usb", "parity": "odd",
                             "stop_bits": 2, "dtr": False, "rts": True}
    # Nothing of the profile (pins, 9600 baud, 8E2) reaches the USB serial port.
    assert pod.starts[1] == {"rx": None, "tx": None, "baud": 115200, "port": "usb", "parity": "none",
                             "stop_bits": 1, "dtr": True, "rts": True}


def test_capture_uart_usb_is_a_client_side_capture_on_the_same_link():
    pod = UsbPod()

    class Link:
        def __init__(self) -> None:
            self.chunks = [b"boot\n", b"login: "]
            self.closed = threading.Event()

        def read(self, n: int) -> bytes:
            if self.chunks:
                return self.chunks.pop(0)
            self.closed.wait(2)
            return b""

        def write(self, data: bytes) -> None:
            pass

        def close(self) -> None:
            self.closed.set()

    pod.link = Link()
    cap = make(pod).capture_uart(port="usb", duration=5, until="login:")
    assert cap.matched and "boot" in cap.text
    assert pod.starts[0]["port"] == "usb" and pod.starts[0]["rx"] is None


@pytest.mark.parametrize("kw", [
    {"rx": 5}, {"tx": 4}, {"rx": 5, "tx": 4},
    {"half_duplex": True}, {"de": 9},
    {"port": "usb2"}, {"port": ""},
])
def test_usb_uart_refuses_pin_arguments_and_bad_ports(kw):
    pod = UsbPod()
    kw = {"port": "usb", **kw} if "port" not in kw else kw
    with pytest.raises(ValueError):
        make(pod).open_uart(**kw)
    assert pod.starts == []


def test_entry_with_the_usb_port_is_refused():
    from embeddedci.benchpod import UartEntry
    pod = UsbPod()
    with pytest.raises(ValueError, match="do not apply"):
        make(pod).open_uart(port="usb", entry=UartEntry(reset_ms=5, hold_ms=3, idle_ms=2, data=b"\x00"))


def test_dtr_and_rts_need_the_usb_port():
    pod = UsbPod()
    with pytest.raises(ValueError, match="port"):
        make(pod).open_uart(rx=5, tx=4, dtr=True)
    with pytest.raises(ValueError, match="port"):
        make(pod).capture_uart(rx=5, tx=4, rts=False, duration=1)
    assert pod.starts == []


def test_usb_uart_refusals_are_classified():
    for code, cls in (("BP0085", UsbSerialUnavailableError), ("BP0086", UsbSerialBusyError)):
        pod = UsbPod()
        pod.uart_error = firmware_error("", cmd="uart_proxy_start", code=code, values={"why": "w"})
        with pytest.raises(cls) as info:
            make(pod).open_uart(port="usb")
        assert info.value.why == "w" and info.value.code == code
    assert issubclass(UsbSerialBusyError, __import__("embeddedci").benchpod.PodBusyError)


def test_the_serial_transport_refuses_the_usb_port():
    from embeddedci.benchpod.errors import BenchPodError
    from embeddedci.benchpod.transport.serial import SerialTransport

    t = SerialTransport.__new__(SerialTransport)
    t.device = "/dev/null"
    with pytest.raises(BenchPodError, match="network command"):
        t.uart_proxy_start(None, None, 115200, port="usb")


def _serve_once(seen: List[dict]):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                seen.append(wire.request(buf.partition(b"\n")[0]))
                conn.sendall(b'{"status":"ok","data":"uart ready"}\n')

    threading.Thread(target=run, daemon=True).start()
    return srv, "127.0.0.1:%d" % srv.getsockname()[1]


def test_tcp_sends_the_usb_handshake():
    seen: List[dict] = []
    srv, addr = _serve_once(seen)
    try:
        t = TcpTransport(addr, timeout=2)
        t.uart_proxy_start(None, None, 115200, port="usb").close()
        t.uart_proxy_start(None, None, 9600, parity="even", stop_bits=2, port="usb", dtr=False,
                           rts=True, data_bits=7).close()
        t.uart_proxy_start(5, 4, 115200).close()
    finally:
        srv.close()
    assert seen == [
        {"cmd": "uart_proxy_start", "port": "usb", "baud": 115200, "dtr": True, "rts": True,
         "parity": "none", "stop_bits": 1},
        {"cmd": "uart_proxy_start", "port": "usb", "baud": 9600, "dtr": False, "rts": True,
         "parity": "even", "stop_bits": 2, "data_bits": 7},
        {"cmd": "uart_proxy_start", "rx": 5, "tx": 4, "baud": 115200},   # LA pins: unchanged
    ]


# -- PICOBOOT, read-back and the file time -------------------------------------------------------

def test_a_pico_with_a_bin_is_flashed_through_picoboot(tmp_path):
    f = tmp_path / "app.bin"
    f.write_bytes(b"\x01" * 600)
    pod = UsbPod(dev=PICO)
    res = make(pod).usb_flash(str(f))
    assert res.mode == "picoboot" and res.address == 0x10000000 and "PICOBOOT" in res.reason
    assert pod.flashes() == [{"cmd": "usb_flash", "mode": "picoboot", "len": 600, "address": 0x10000000}]
    make(pod).usb_flash(str(f), address=0x10040000, leave=False)
    assert pod.flashes()[1] == {"cmd": "usb_flash", "mode": "picoboot", "len": 600,
                                "address": 0x10040000, "leave": False}


def test_a_pico_hex_file_takes_its_address_and_a_uf2_still_goes_to_the_drive(tmp_path):
    from test_usb_host import ihex
    f = tmp_path / "app.hex"
    f.write_text(ihex(0, b"\x02" * 16, ext=0x1004))
    pod = UsbPod(dev=PICO)
    res = make(pod).usb_flash(str(f))
    assert res.mode == "picoboot" and res.address == 0x10040000
    u = tmp_path / "app.uf2"
    u.write_bytes(b"UF2\n" + b"\x00" * 508)
    assert make(pod).usb_flash(str(u)).mode == "uf2"


def test_picoboot_needs_the_feature(tmp_path):
    f = tmp_path / "app.bin"
    f.write_bytes(b"\x01" * 16)
    pod = UsbPod(dev=PICO, features_rev=14)   # 4.0.3 before PICOBOOT
    with pytest.raises(UnsupportedFeatureError) as info:
        make(pod).usb_flash(str(f))
    assert info.value.feature == "usb_flash_picoboot" and pod.flashes() == [] and pod.staged == []


def test_a_file_flash_sends_the_computers_time(tmp_path, monkeypatch):
    from embeddedci.benchpod import client
    monkeypatch.setattr(client.time, "time", lambda: 1_790_000_000.9)
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    pod = UsbPod(dev=NUCLEO)
    make(pod).usb_flash(str(f))
    assert pod.flashes()[0] == {"cmd": "usb_flash", "mode": "file", "len": 16, "name": "FW.BIN",
                                "time": 1_790_000_000}
    # An older table (no read-back feature) is sent no time; other modes never are.
    old = UsbPod(dev=NUCLEO, features_rev=14)
    make(old).usb_flash(str(f))
    assert "time" not in old.flashes()[0]
    uno = UsbPod(dev=CH340)
    make(uno).usb_flash(str(f), mode="avr")
    assert "time" not in uno.flashes()[0]


def test_verify_asks_for_the_read_back_by_mode(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    u = tmp_path / "fw.uf2"
    u.write_bytes(b"UF2\n" + b"\x00" * 508)
    pod = UsbPod(dev=NUCLEO)
    make(pod).usb_flash(str(f), verify=True)
    make(pod).usb_flash(str(u), verify=True)
    make(pod).usb_flash(str(f))
    flashes = pod.flashes()
    assert flashes[0]["check_fail_txt"] is True and "verify" not in flashes[0]
    assert flashes[1]["verify"] is True and "check_fail_txt" not in flashes[1]
    assert "check_fail_txt" not in flashes[2] and "verify" not in flashes[2]
    # PICOBOOT and the loaders without a drive read back by themselves: verify adds nothing.
    pico = UsbPod(dev=PICO)
    make(pico).usb_flash(str(f), verify=True)
    assert pico.flashes()[0] == {"cmd": "usb_flash", "mode": "picoboot", "len": 16, "address": 0x10000000}


def test_verify_needs_the_read_back_feature(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    pod = UsbPod(dev=NUCLEO, features_rev=14)
    with pytest.raises(UnsupportedFeatureError) as info:
        make(pod).usb_flash(str(f), verify=True)
    assert info.value.feature == "usb_flash_readback" and pod.flashes() == []


def test_the_note_reaches_the_result_and_the_error(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"\x01" * 16)
    pod = UsbPod(dev=NUCLEO)
    pod.note = "verified 4 blocks"
    res = make(pod).usb_flash(str(f), verify=True)
    assert res.note == "verified 4 blocks" and "[verified 4 blocks]" in str(res)
    bad = UsbPod(dev=NUCLEO, result="the loader reported a failure")
    bad.note = "error: invalid vector table"
    with pytest.raises(UsbFlashError, match="invalid vector table") as info:
        make(bad).usb_flash(str(f), verify=True)
    assert info.value.state.note == "error: invalid vector table"


def test_picoboot_is_a_mode_with_its_unit():
    assert usb_host.MODES["picoboot"].binary and usb_host.MODES["picoboot"].leave
    assert usb_host.UsbFlashState.from_reply({"mode": "picoboot"}).unit == "steps"
    assert usb_host.UsbFlashState.from_reply({}).note == ""


# -- usb_check ----------------------------------------------------------------------------------

def test_usb_check_starts_host_mode_runs_and_returns_the_state():
    pod = UsbPod()
    seen = []
    st = make(pod).usb_check("enumerate", count=10, off_ms=300, progress=lambda d, t: seen.append((d, t)))
    assert st.passed is True and st.ended and st.test == "enumerate" and st.error == ""
    assert st.result["mount_ms"]["avg"] == 90 and st.result["vid"] == 0x2E8A
    assert pod.check_req == {"cmd": "usb_check", "test": "enumerate", "count": 10, "off_ms": 300}
    assert pod.seq == ["host_start", "check_start"]
    assert seen and seen[0] == (3, 10)


def test_usb_check_sends_only_what_was_given_and_takes_lists():
    pod = UsbPod()
    bp = make(pod)
    bp.usb_check("power")
    assert pod.check_req == {"cmd": "usb_check", "test": "power"}
    bp.usb_check("replug", count=5, off_ms=[5, 20, 100], timeout_ms=3000)
    assert pod.check_req == {"cmd": "usb_check", "test": "replug", "count": 5, "off_ms": [5, 20, 100],
                             "timeout_ms": 3000}
    bp.usb_check("suspend", settle_ms=300, duration_ms=2000)
    assert pod.check_req["settle_ms"] == 300 and pod.check_req["duration_ms"] == 2000


def test_a_failing_board_is_a_result_not_an_exception():
    pod = UsbPod()
    pod.check_pass, pod.check_error = False, "not_powered_by_efuse1"
    st = make(pod).usb_check("enumerate")
    assert st.passed is False and st.error == "not_powered_by_efuse1"


def test_usb_check_rejects_an_unknown_test():
    pod = UsbPod()
    with pytest.raises(ValueError, match="test must be one of"):
        make(pod).usb_check("speed")
    assert pod.cmds == []


def test_usb_check_refusals_are_typed():
    for code, cls, values in (("BP0087", UsbCheckRefusedError, {"why": "host mode is off"}),
                              ("BP0088", UsbCheckNoPowerError, {})):
        pod = UsbPod()
        real = pod.command

        def refuse(req, real=real, code=code, values=values):
            if req.get("test"):
                raise firmware_error("", cmd="usb_check", code=code, values=values)
            return real(req)

        pod.command = refuse   # type: ignore[assignment]
        with pytest.raises(cls) as info:
            make(pod).usb_check("power")
        assert info.value.code == code


def test_usb_check_without_a_board_times_out_before_starting(monkeypatch):
    pod = UsbPod(dev=None)
    t = iter(range(10 ** 6))
    orig = usb_host.wait_device
    monkeypatch.setattr(usb_host, "wait_device", lambda *a, **k: orig(*a, **{**k, "clock": lambda: next(t)}))
    from embeddedci.benchpod.errors import UsbTimeout
    with pytest.raises(UsbTimeout):
        make(pod).usb_check("power", device_timeout=3)
    assert not [c for c in pod.cmds if c.get("test")]


def test_usb_check_timeout_aborts():
    pod = UsbPod()
    pod.check_polls = 10 ** 9
    clock = iter(range(0, 10 ** 6, 10))
    with pytest.raises(UsbHostError, match="did not finish within 30 s"):
        usb_host.run_check(pod.command, {"cmd": "usb_check", "test": "reset"}, timeout=30,
                           sleep=lambda s: None, clock=lambda: next(clock))
    assert {"cmd": "usb_check", "abort": True} in pod.cmds


def test_usb_check_interrupt_aborts_and_reraises():
    pod = UsbPod()

    def progress(d, t):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        usb_host.run_check(pod.command, {"cmd": "usb_check", "test": "reset"}, timeout=100,
                           progress=progress, sleep=lambda s: None)
    assert {"cmd": "usb_check", "abort": True} in pod.cmds


def test_status_and_abort():
    pod = UsbPod()
    bp = make(pod)
    bp.usb_host()
    pod.command({"cmd": "usb_check", "test": "reset", "count": 20})
    st = bp.usb_check_status()
    assert st.running and not st.ended and st.passed is None and st.phase == "gap"
    st = bp.usb_check_abort()
    assert not st.running and st.error == "aborted" and st.have_result


def test_check_timeouts_cover_the_worst_case():
    assert usb_host.check_timeout("power", {"duration_ms": 3000}) == 23
    assert usb_host.check_timeout("enumerate", {}) == 20 + 10 * (500 + 5000) / 1000
    assert usb_host.check_timeout("reset", {"count": 20, "timeout_ms": 1000}) == 40
    assert usb_host.check_timeout("replug", {"count": 2, "off_ms": 100, "timeout_ms": 1000}) == 20 + 2 * 1.1
    assert usb_host.check_timeout("replug", {}) > 20 + 10 * (625 + 4 * 5000) / 1000 - 1
    assert usb_host.check_timeout("suspend", {}) > 40
    assert set(usb_host.CHECKS) == {"enumerate", "power", "suspend", "reset", "replug"}


def test_check_state_without_a_verdict():
    st = usb_host.UsbCheckState.from_reply({"running": True, "test": "power"})
    assert st.passed is None and not st.ended and st.result == {}


# -- usb_hid ------------------------------------------------------------------------------------

def test_hid_interfaces_reads_a_long_descriptor_in_pages():
    pod = UsbPod()
    st = make(pod).usb_hid_interfaces()
    itf = st.interfaces[0]
    assert st.attached and itf.protocol == "keyboard" and itf.instance == 0
    assert itf.descriptor == pod.hid_descriptor and itf.descriptor_len == 40 and not itf.truncated
    assert [c.get("offset") for c in pod.cmds if c.get("cmd") == "usb_hid"] == [None, 16, 32]
    assert pod.cmds[1] == {"cmd": "usb_hid", "instance": 0, "offset": 16}


def test_hid_interfaces_keeps_a_short_descriptor_in_one_call():
    pod = UsbPod()
    pod.hid_descriptor = bytes(range(8))
    st = make(pod).usb_hid_interfaces()
    assert st.interfaces[0].descriptor == bytes(range(8))
    assert len(pod.cmds) == 1


def test_hid_capture_and_read():
    pod = UsbPod()
    pod.hid_reports = [{"ms": 812, "instance": 0, "data": "0000040000000000"},
                       {"ms": 830, "instance": 1, "data": "00010000"}]
    bp = make(pod)
    cap = bp.usb_hid_capture()
    assert cap.capturing and cap.buffered == 2 and cap.dropped == 0
    assert pod.cmds[-1] == {"cmd": "usb_hid", "capture": True}
    got = bp.usb_hid_read(count=8, timeout=5.0)
    assert pod.cmds[-1] == {"cmd": "usb_hid", "read": True, "count": 8, "timeout_ms": 5000}
    assert len(got) == 2 and got.capturing and got.dropped == 0
    assert got.reports[0].ms == 812 and got.reports[0].data == bytes.fromhex("0000040000000000")
    assert got.reports[1].instance == 1
    only = bp.usb_hid_read(instance=1)
    assert pod.cmds[-1] == {"cmd": "usb_hid", "read": True, "count": 1, "timeout_ms": 2000, "instance": 1}
    assert [r.instance for r in only.reports] == [1]
    assert not bp.usb_hid_capture(False).capturing
    assert pod.cmds[-1] == {"cmd": "usb_hid", "capture": False}


@pytest.mark.parametrize("kw", [{"count": 0}, {"count": 33}, {"timeout": 0}, {"timeout": 10.5},
                                {"instance": 2}, {"instance": True}])
def test_hid_read_checks_its_arguments(kw):
    pod = UsbPod()
    with pytest.raises(ValueError):
        make(pod).usb_hid_read(**kw)
    assert pod.cmds == []


def test_a_hid_read_waits_as_long_as_the_pod_does():
    from embeddedci.benchpod.transport.base import request_timeout
    assert request_timeout({"cmd": "usb_hid", "read": True, "timeout_ms": 10000}, 30.0) == 40.0
    assert request_timeout({"cmd": "usb_hid", "capture": True}, 30.0) == 30.0
