"""The I2C master, la_dir, la_pins dir/warn and send-and-capture, against a fake pod that speaks the
firmware's contract (docs/API.md "I2C master", la_dir, la_capture "Send and capture")."""

from __future__ import annotations

import base64
from typing import Any, Dict, Iterator, List, Optional

import pytest

from embeddedci.benchpod import (
    BenchPod,
    DirectionConflictError,
    FirmwareError,
    GpioSend,
    I2CNackError,
    I2CSend,
    PinConflictError,
    SpiSend,
    Trigger,
    UnsupportedFeatureError,
    Wiring,
)
from embeddedci.benchpod.wiring import Signal


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)) if t else b""


class BusPod:
    """An I2C target at 0x42 with a 256-byte register file (as the process-node bench), la_dir and
    la_capture with send."""

    TARGET = 0x42

    def __init__(self, *, gateware: Optional[int] = 55) -> None:
        self.gateware = gateware
        self.commands: List[dict] = []
        self.session: Optional[dict] = None
        self.mem = bytearray(range(256))
        self.ptr = 0
        self.dirs: Dict[int, str] = {}
        self.driven: Dict[int, str] = {}

    # transport surface
    def status(self) -> Dict[str, Any]:
        st = {"board": "stm32h563", "adc_bits": 16, "version": "3.10.4",
              "caps": ["la", "la_pins", "gpio_read", "capture_trigger", "spi_master"]}
        if self.gateware is not None:
            st["gateware"] = self.gateware
        return st

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def _pin(self, la: int) -> dict:
        e: Dict[str, Any] = {"la": la, "function": self.driven.get(la, "none"), "gpio": None,
                             "level": None, "pull": None}
        if la in self.dirs and self.dirs[la] != "unknown":
            e["dir"] = self.dirs[la]
        if la == 3:
            e["warn"] = 3
        return e

    def _xfer(self, addr: int, write: bytes, read: int) -> dict:
        if addr != self.TARGET:
            return {"addr": addr, "ack": False, "nack_at": 0, "rx": ""}
        if write:
            self.ptr = write[0]
            for i, b in enumerate(write[1:]):
                self.mem[(self.ptr + i) & 0xFF] = b
        rx = bytes(self.mem[(self.ptr + i) & 0xFF] for i in range(read))
        return {"addr": addr, "ack": True, "rx": b64(rx)}

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "i2c_start":
            if (self.gateware or 0) < 55:
                raise FirmwareError("the I2C master needs gateware v55+: update the pod's firmware", cmd=cmd)
            if req["pullups"] == "pod" and req["sda"] > 6:
                raise FirmwareError(f"LA{req['sda']} has no pod pull-up (only LA1..LA6 do)", cmd=cmd)
            if self.session:
                raise FirmwareError("i2c busy: send i2c_stop first", cmd=cmd)
            self.session = req
            self.driven[req["sda"]] = "i2cm_sda"
            self.driven[req["scl"]] = "i2cm_scl"
            return {"sda": req["sda"], "scl": req["scl"], "hz": 363636 if req["hz"] == 400_000 else req["hz"],
                    "pullups": req["pullups"]}
        if cmd == "i2c_stop":
            self.session = None
            self.driven = {k: v for k, v in self.driven.items() if not v.startswith("i2cm")}
            return "i2c stopped"
        if cmd == "i2c_status":
            return {"active": bool(self.session)}
        if cmd in ("i2c_xfer", "i2c_scan") and not self.session:
            raise FirmwareError("no I2C session: send i2c_start first", cmd=cmd)
        if cmd == "i2c_xfer":
            write = unb64(req.get("write", ""))
            assert len(write) <= 128 and req.get("read", 0) <= 248
            return self._xfer(req["addr"], write, req.get("read", 0))
        if cmd == "i2c_scan":
            lo, hi = req.get("from", 8), req.get("to", 119)
            return {"from": lo, "to": hi, "found": [self.TARGET] if lo <= self.TARGET <= hi else []}
        if cmd == "la_dir":
            if "la" not in req:
                return {"pins": [self._pin(la) for la in range(1, 15)]}
            las = list(range(1, 15)) if req["la"] == "all" else (
                req["la"] if isinstance(req["la"], list) else [req["la"]])
            for la in las:
                if req["dir"] == "dut_output" and self.driven.get(la) == "gpio_output":
                    raise FirmwareError(f"direction conflict: LA{la} is driven by gpio output, so it "
                                        "can't be wired to a DUT output; stop that first", cmd=cmd)
            for la in las:
                self.dirs[la] = req["dir"]
            return {"pins": [self._pin(la) for la in las]}
        if cmd == "gpio" and req.get("mode") == "output":
            la = req["la"]
            if self.dirs.get(la) == "dut_output":
                raise FirmwareError(f"direction conflict: LA{la} is wired to a DUT output and gpio "
                                    "output would drive against it; use gpio open_drain, or fix the "
                                    f"wiring if LA{la} is not a DUT output", cmd=cmd)
            self.driven[la] = "gpio_output"
            return {"pins": [{"la": la, "function": "gpio", "gpio": "output", "level": 0}]}
        if cmd == "la_pins":
            return {"pins": [self._pin(la) for la in range(1, 15)], "levels": 0}
        raise AssertionError(f"unexpected {req}")

    def stream_chunks(self, req: dict) -> Iterator[Dict[str, Any]]:
        self.commands.append(req)
        assert req["cmd"] == "la_capture"
        send = req.get("send")
        result = None
        if send and send["bus"] == "i2c":
            if not self.session:
                raise FirmwareError("send: no I2C session: send i2c_start first", cmd="la_capture")
            r = self._xfer(send["addr"], unb64(send.get("write", "")), send.get("read", 0))
            result = {"bus": "i2c", **{k: v for k, v in r.items() if k != "addr"}}
        elif send and send["bus"] == "spi":
            result = {"bus": "spi", "rx": b64(bytes(b ^ 0xFF for b in unb64(send["tx"])))}
        elif send:
            result = {"bus": "gpio", "la": send["la"], "level": send["level"]}
        pre = (req.get("trigger") or {}).get("pre_samples", 0)
        first: Dict[str, Any] = {"status": "ok", "bits": 16, "la_rate_hz": 12_000_000, "la": True,
                                 "la_edges": [[0, 3], [10, 1]], "la_upto": 64, "more": True}
        if pre:
            first["pre_samples"] = min(pre, 16)
        yield first
        last: Dict[str, Any] = {"status": "ok", "la": True, "la_edges": [[64, 3]], "la_upto": 128,
                                "more": False}
        if result is not None:
            last["send"] = result
        yield last


def pod(**kw) -> "tuple[BenchPod, BusPod]":
    fake = BusPod(**kw)
    return BenchPod(transport=fake, wiring=Wiring(i2c_sda=1, i2c_scl=2)), fake


# -- I2C master ---------------------------------------------------------------------------------

def test_open_i2c_pins_from_wiring_and_rate_reply():
    bp, fake = pod()
    with bp.open_i2c(hz=400_000) as i2c:
        assert (i2c.sda, i2c.scl, i2c.hz, i2c.pullups) == (1, 2, 363636, "pod")
        assert fake.commands[-1] == {"cmd": "i2c_start", "sda": 1, "scl": 2, "hz": 400_000, "pullups": "pod"}
    assert fake.commands[-1] == {"cmd": "i2c_stop"}
    assert fake.session is None


def test_open_i2c_accepts_wiring_names_and_validates():
    bp, fake = pod()
    with bp.open_i2c("i2c_scl", "i2c_sda", pullups="external") as i2c:
        assert (i2c.sda, i2c.scl) == (2, 1)
    with pytest.raises(ValueError, match="hz"):
        bp.open_i2c(hz=5_000)
    with pytest.raises(ValueError, match="pullups"):
        bp.open_i2c(pullups="weak")
    with pytest.raises(ValueError, match="different"):
        bp.open_i2c(1, 1)


def test_write_read_and_register_helpers():
    bp, fake = pod()
    with bp.open_i2c() as i2c:
        i2c.write_reg(0x42, 0x20, b"\xa5\x5a")
        assert fake.mem[0x20:0x22] == b"\xa5\x5a"
        assert i2c.read_reg(0x42, 0x20, 2) == b"\xa5\x5a"
        assert i2c.write_read(0x42, b"\x80", 4) == bytes([0x80, 0x81, 0x82, 0x83])
        i2c.write(0x42, [0x30])
        assert i2c.read(0x42, 2) == bytes([0x30, 0x31])
        sent = [c for c in fake.commands if c["cmd"] == "i2c_xfer"]
        assert sent[0] == {"cmd": "i2c_xfer", "addr": 0x42, "write": b64(b"\x20\xa5\x5a")}
        assert sent[1] == {"cmd": "i2c_xfer", "addr": 0x42, "write": b64(b"\x20"), "read": 2}


def test_nack_raises_with_nack_at_and_xfer_reports_it():
    bp, fake = pod()
    with bp.open_i2c() as i2c:
        with pytest.raises(I2CNackError) as ei:
            i2c.read_reg(0x50, 0x00, 2)
        assert ei.value.addr == 0x50 and ei.value.nack_at == 0
        r = i2c.xfer(0x50, b"\x00", 2)
        assert r.ack is False and r.nack_at == 0 and r.data == b""


def test_probe_and_scan():
    bp, fake = pod()
    with bp.open_i2c() as i2c:
        assert i2c.probe(0x42) is True
        assert i2c.probe(0x43) is False
        assert fake.commands[-1] == {"cmd": "i2c_xfer", "addr": 0x43}
        assert i2c.scan() == [0x42]
        assert fake.commands[-1] == {"cmd": "i2c_scan", "from": 8, "to": 119}
        assert i2c.scan(0x50, 0x60) == []


def test_limits_are_checked_before_sending():
    bp, fake = pod()
    with bp.open_i2c() as i2c:
        n = len(fake.commands)
        with pytest.raises(ValueError):
            i2c.write(0x42, bytes(129))
        with pytest.raises(ValueError):
            i2c.read(0x42, 249)
        with pytest.raises(ValueError):
            i2c.probe(128)
        assert len(fake.commands) == n


def test_old_gateware_refused_before_sending():
    bp, fake = pod(gateware=53)
    with pytest.raises(UnsupportedFeatureError, match="gateware v55"):
        bp.open_i2c()
    assert not [c for c in fake.commands if c["cmd"] == "i2c_start"]


def test_firmware_gateware_refusal_is_unsupported_feature():
    fake = BusPod(gateware=None)
    fake.gateware = None
    bp = BenchPod(transport=fake, wiring=Wiring())
    fake.gateware = 40  # the pod itself refuses; the SDK saw no gateware in status
    with pytest.raises(UnsupportedFeatureError):
        bp.open_i2c()


def test_refusals_name_sdk_calls():
    bp, fake = pod()
    i2c = bp.open_i2c()
    with pytest.raises(FirmwareError, match=r"bp\.open_i2c\(\)"):
        bp.open_i2c()
    i2c.close()
    i2c.close()  # idempotent
    with pytest.raises(FirmwareError, match=r"open one with bp\.open_i2c\(\)"):
        i2c.xfer(0x42)


# -- la_dir and la_pins -------------------------------------------------------------------------

def test_la_dir_sets_and_lists():
    bp, fake = pod()
    pins = bp.la_dir([9, 10], "dut_output")
    assert fake.commands[-1] == {"cmd": "la_dir", "la": [9, 10], "dir": "dut_output"}
    assert [(p.la, p.dir) for p in pins] == [(9, "dut_output"), (10, "dut_output")]
    bp.la_dir("all", "unknown")
    assert fake.commands[-1]["la"] == "all"
    listed = bp.la_dir()
    assert fake.commands[-1] == {"cmd": "la_dir"}
    assert len(listed) == 14 and all(p.dir is None for p in listed)
    with pytest.raises(ValueError):
        bp.la_dir(9, "out")
    with pytest.raises(ValueError):
        bp.la_dir(9)


def test_direction_conflict_is_its_own_error():
    bp, fake = pod()
    bp.la_dir(5, "dut_output")
    with pytest.raises(DirectionConflictError) as ei:
        bp.gpio(5, "output")
    assert ei.value.la == 5


def test_la_pins_dir_and_warn():
    bp, fake = pod()
    bp.la_dir(4, "dut_input")
    pins = {p.la: p for p in bp.la_pins()}
    # LA1 is i2c_sda in the wiring: bidir since connect. LA3 is not wired: unknown.
    assert pins[4].dir == "dut_input" and pins[1].dir == "bidir" and pins[3].dir is None
    assert pins[3].warn == 3 and len(pins[3].warnings) == 2 and pins[4].warnings == []


def test_pin_directions_from_wiring():
    w = Wiring(uart_rx=5, uart_tx=4, i2c_sda=1, i2c_scl=2, spi_sclk=7, spi_mosi=8, spi_miso=9, spi_cs=10, swd_swclk=None, swd_swdio=None,
               signals=(Signal("ALARM", 11, "input"), Signal("TRIG", 12, "output"),
                        Signal("RST", 13, "open_drain")))
    bp, fake = pod()
    dirs = bp.pin_directions(w)
    assert dirs == {5: "dut_output", 4: "dut_input", 1: "bidir", 2: "bidir", 7: "dut_input",
                    8: "dut_input", 9: "dut_output", 10: "dut_input", 11: "dut_output",
                    12: "dut_input", 13: "bidir"}
    fake.commands.clear()
    bp.apply_pin_directions(w)
    sent = [c for c in fake.commands if c["cmd"] == "la_dir"]
    # Unwired pins first (unknown), DUT outputs last.
    assert [c["dir"] for c in sent] == ["unknown", "dut_input", "bidir", "dut_output"]
    assert {c["dir"]: c["la"] for c in sent} == {"bidir": [1, 2, 13], "dut_input": [4, 7, 8, 10, 12],
                                                 "dut_output": [5, 9, 11], "unknown": [3, 6, 14]}
    assert fake.dirs[11] == "dut_output"


# -- send and capture ---------------------------------------------------------------------------

def test_capture_with_i2c_send_returns_result_and_defaults_pre_samples():
    bp, fake = pod()
    with bp.open_i2c() as i2c:
        cap = bp.capture_la(128, sample_rate_hz=12e6, trigger=Trigger("i2c_sda", "falling"),
                            send=i2c.send(0x42, b"\x10", read=2))
    req = [c for c in fake.commands if c["cmd"] == "la_capture"][0]
    assert req["send"] == {"bus": "i2c", "addr": 0x42, "write": b64(b"\x10"), "read": 2}
    assert req["trigger"] == {"la": 1, "edge": "falling", "pre_samples": 64}
    r = cap.send_result
    assert r.bus == "i2c" and r.ack is True and r.ok and r.data == bytes([0x10, 0x11])
    assert len(cap) == 128 and cap.pre_samples == 16


def test_capture_send_keeps_explicit_pre_samples_and_level_triggers():
    bp, fake = pod()
    bp.open_i2c()
    bp.capture_la(128, trigger=Trigger(1, "falling", pre_samples=500), send=I2CSend(0x42, read=1))
    bp.capture_la(128, trigger=Trigger(1, "low"), send=I2CSend(0x42, read=1))
    caps = [c for c in fake.commands if c["cmd"] == "la_capture"]
    assert caps[0]["trigger"]["pre_samples"] == 500
    assert "pre_samples" not in caps[1]["trigger"]


def test_capture_without_send_is_unchanged():
    bp, fake = pod()
    cap = bp.capture_la(128, trigger=Trigger(1, "falling"))
    req = fake.commands[-1]
    assert "send" not in req and "pre_samples" not in req["trigger"]
    assert cap.send_result is None


def test_capture_spi_and_gpio_sends():
    bp, fake = pod()
    cap = bp.capture_la(128, send=SpiSend(b"\x9f\x00"))
    assert fake.commands[-1]["send"] == {"bus": "spi", "tx": b64(b"\x9f\x00")}
    assert cap.send_result.bus == "spi" and cap.send_result.data == b"\x60\xff"
    w = Wiring(signals=(Signal("STROBE", 6, "output"),))
    bp2 = BenchPod(transport=fake, wiring=w)
    cap = bp2.capture_la(128, trigger=Trigger("STROBE", "falling"), send=GpioSend("STROBE", 0))
    assert fake.commands[-1]["send"] == {"bus": "gpio", "la": 6, "level": 0}
    assert cap.send_result.la == 6 and cap.send_result.level == 0 and cap.send_result.ok


def test_send_nack_and_error_land_on_the_result():
    bp, fake = pod()
    bp.open_i2c()
    cap = bp.capture_la(128, send=I2CSend(0x50, b"\x00", 1))
    assert cap.send_result.ack is False and cap.send_result.nack_at == 0 and not cap.send_result.ok
    from embeddedci.benchpod import SendResult
    err = SendResult.from_reply({"bus": "i2c", "error": "SCL held low"})
    assert err.error == "SCL held low" and not err.ok


def test_send_validation_and_gateware():
    bp, fake = pod()
    with pytest.raises(ValueError):
        bp.capture_la(128, send=SpiSend(bytes(33)))
    with pytest.raises(ValueError):
        bp.capture_la(128, send=I2CSend(0x42, read=33))
    with pytest.raises(ValueError):
        bp.capture_la(128, send={"bus": "i2c"})  # type: ignore[arg-type]
    old, fake_old = pod(gateware=54)
    with pytest.raises(UnsupportedFeatureError):
        old.capture_la(128, send=SpiSend(b"\x01"))
    assert not [c for c in fake_old.commands if c["cmd"] == "la_capture"]


def test_send_without_session_names_open_i2c():
    bp, fake = pod()
    with pytest.raises(FirmwareError, match=r"bp\.open_i2c\(\)"):
        bp.capture_la(128, send=I2CSend(0x42, read=1))


def test_pin_conflict_with_i2c_master_names_the_session():
    from embeddedci.benchpod.errors import firmware_error

    e = firmware_error('pin conflict: LA1 is in use by i2cm_sda; stop the I2C session first '
                       '({"cmd":"i2c_stop"})', cmd="gpio")
    assert isinstance(e, PinConflictError) and e.function == "i2cm_sda"
    assert "bp.open_i2c()" in str(e)
