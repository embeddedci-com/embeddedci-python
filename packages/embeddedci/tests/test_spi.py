"""SPI master + SPI flash, against a fake pod that speaks the firmware's spi_* contract."""

from __future__ import annotations

import base64
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FirmwareError, SpiFlashInfo, Wiring
from embeddedci.benchpod.spi import ERASE_STEP, READ_MAX, XFER_MAX


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


class SpiPod:
    """The firmware's SPI commands over a 1 MB flash model (limits as in command_handler_spi.c)."""

    def __init__(self, *, caps=None, present=True) -> None:
        self.caps = ["la", "la_pins", "spi_master", "nrst_pin"] if caps is None else caps
        self.mem = bytearray(b"\xff" * (1 << 20))
        self.present = present
        self.armed = None
        self.cs_held = False
        self.commands: List[dict] = []
        self.reset_log: List[bool] = []
        self.fail_write_at = None

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.3.0", "caps": self.caps,
                "nrst_pin": True}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "nrst":
            self.reset_log.append(bool(req["assert"]))
            return {"supported": True, "asserted": bool(req["assert"])}
        if cmd == "spi_start":
            pins = [req[k] for k in ("sck", "mosi", "miso", "cs")]
            assert len(set(pins)) == 4 and all(1 <= p <= 14 for p in pins)
            assert self.armed is None, "spi busy"
            self.armed = req
            half = max(2, min(63, -(-24_000_000 // (2 * req["hz"]))))
            return {**{k: req[k] for k in ("sck", "mosi", "miso", "cs", "mode")}, "hz": 24_000_000 // (2 * half)}
        if cmd == "spi_stop":
            self.armed = None
            return "spi stopped"
        if self.armed is None:
            raise FirmwareError("no SPI session: send spi_start first", cmd=cmd)
        if cmd == "spi_xfer":
            tx = unb64(req["tx"])
            assert 1 <= len(tx) <= 768
            self.cs_held = req.get("cs", "release") == "hold"
            return {"rx": b64(bytes((b ^ 0xFF) for b in tx)), "cs": "held" if self.cs_held else "released"}
        assert cmd == "spi_flash"
        op = req["op"]
        if op == "id":
            return {"id": "ef4014" if self.present else "ffffff", "present": self.present,
                    "size": (1 << 20) if self.present else 0, "status": 0}
        if not self.present:
            raise FirmwareError("write enable did not stick: no flash answering, or it is write-protected", cmd=cmd)
        if op == "read":
            assert 1 <= req["len"] <= 1024
            return {"addr": req["addr"], "len": req["len"],
                    "data": b64(bytes(self.mem[req["addr"]:req["addr"] + req["len"]]))}
        if op == "erase":
            assert 1 <= req["len"] <= 1 << 20
            a = req["addr"] & ~0xFFF
            end = (req["addr"] + req["len"] + 0xFFF) & ~0xFFF
            self.mem[a:end] = b"\xff" * (end - a)
            return {"addr": a, "len": end - a, "ms": 10}
        if op == "write":
            data = unb64(req["data"])
            assert 1 <= len(data) <= 768
            if self.fail_write_at is not None and req["addr"] >= self.fail_write_at:
                raise FirmwareError(f"verify failed at 0x{req['addr']:06x}: not erased, write-protected, or a bad wire", cmd=cmd)
            for i, b in enumerate(data):
                self.mem[req["addr"] + i] &= b
            return {"addr": req["addr"], "len": len(data), "verified": req.get("verify", True), "ms": 1}
        raise AssertionError(op)


@pytest.fixture
def pod():
    fake = SpiPod()
    bp = BenchPod(transport=fake, wiring=Wiring(uart_rx=3, uart_tx=4, spi_sclk=13, spi_mosi=14, spi_miso=6, spi_cs=5))
    return bp, fake


def test_open_spi_takes_pins_from_wiring_and_rounds_hz(pod):
    bp, fake = pod
    with bp.open_spi(hz=5_000_000) as spi:
        assert (spi.sck, spi.mosi, spi.miso, spi.cs) == (13, 14, 6, 5)
        assert spi.hz == 4_000_000                    # 24 MHz / (2 * 3): never faster than asked
    assert fake.armed is None                         # closed: spi_stop sent
    assert fake.commands[-1] == {"cmd": "spi_stop"}


def test_open_spi_needs_the_capability_and_a_valid_mode():
    bp = BenchPod(transport=SpiPod(caps=["la"]), wiring=Wiring(spi_sclk=7, spi_mosi=8, spi_miso=9, spi_cs=10))
    with pytest.raises(BenchPodError):
        bp.open_spi()
    bp = BenchPod(transport=SpiPod(), wiring=Wiring(spi_sclk=7, spi_mosi=8, spi_miso=9, spi_cs=10))
    with pytest.raises(ValueError):
        bp.open_spi(mode=1)


def test_open_spi_without_wiring_pins_says_which_is_missing():
    bp = BenchPod(transport=SpiPod())
    with pytest.raises(ValueError, match="spi_sclk"):
        bp.open_spi()


def test_transfer_splits_long_data_and_holds_cs_between_chunks(pod):
    bp, fake = pod
    data = bytes(range(256)) * 7                      # 1792 bytes: 768 + 768 + 256
    with bp.open_spi() as spi:
        rx = spi.transfer(data)
        xf = [c for c in fake.commands if c["cmd"] == "spi_xfer"]
        assert [len(unb64(c["tx"])) for c in xf] == [768, 768, 256]
        assert [c["cs"] for c in xf] == ["hold", "hold", "release"]
        assert rx == bytes(b ^ 0xFF for b in data)
        spi.transfer(b"\x9f", hold_cs=True)
        assert fake.cs_held


def test_flash_id_read_erase_write_chunking(pod):
    bp, fake = pod
    with bp.open_spi() as spi:
        assert spi.flash_id() == SpiFlashInfo(jedec_id="ef4014", present=True, size=1 << 20, status=0)
        img = bytes((i * 7) & 0xFF for i in range(3000))
        start, length = spi.flash_erase(0x1100, len(img))
        assert (start, length) == (0x1000, 0x1000)                   # 0x1100..0x1cb8: one sector
        spi.flash_write(0x1100, img)
        writes = [c for c in fake.commands if c.get("op") == "write"]
        assert [len(unb64(c["data"])) for c in writes] == [768, 768, 768, 696]
        assert all("verify" not in c for c in writes)                  # verify is the default
        assert spi.flash_read(0x1100, len(img)) == img
        reads = [c for c in fake.commands if c.get("op") == "read"]
        assert [c["len"] for c in reads] == [READ_MAX, READ_MAX, 3000 - 2 * READ_MAX]


def test_erase_is_sent_in_one_megabyte_steps(pod):
    bp, fake = pod
    fake.mem = bytearray(b"\xff" * (4 << 20))
    with bp.open_spi() as spi:
        spi.flash_erase(0, 2 * ERASE_STEP + 10)
    erases = [c for c in fake.commands if c.get("op") == "erase"]
    assert [c["len"] for c in erases] == [ERASE_STEP, ERASE_STEP, 10]


def test_spi_flash_programs_verifies_and_reports_progress(pod, tmp_path):
    bp, fake = pod
    img = bytes((i * 13 + 5) & 0xFF for i in range(10_000))
    path = tmp_path / "fw.bin"
    path.write_bytes(img)
    seen = []
    res = bp.spi_flash(str(path), 0x2000, progress=lambda d, t: seen.append((d, t)))
    assert bytes(fake.mem[0x2000:0x2000 + len(img)]) == img
    assert res.length == len(img) and res.verified and res.erased == 12288 and res.jedec_id == "ef4014"
    assert seen[-1] == (2 * len(img), 2 * len(img)) and all(d <= t for d, t in seen)
    assert fake.armed is None
    assert fake.commands[0]["cmd"] == "spi_start" and fake.commands[0]["hz"] == 6_000_000


def test_spi_flash_holds_reset_and_releases_it_on_failure(pod):
    bp, fake = pod
    fake.fail_write_at = 0x800
    with pytest.raises(FirmwareError, match="verify failed"):
        bp.spi_flash(bytes(4096), hold_reset=True)
    assert fake.reset_log == [True, False]           # held for the job, released after the failure
    assert fake.armed is None                        # and the pins were released


def test_spi_flash_refuses_an_empty_bus_before_erasing():
    fake = SpiPod(present=False)
    bp = BenchPod(transport=fake, wiring=Wiring(spi_sclk=7, spi_mosi=8, spi_miso=9, spi_cs=10))
    with pytest.raises(FirmwareError, match="no SPI flash answers"):
        bp.spi_flash(b"\x01\x02")
    assert not [c for c in fake.commands if c.get("op") in ("erase", "write")]


def test_spi_flash_refuses_an_image_larger_than_the_part(pod):
    bp, fake = pod
    with pytest.raises(ValueError, match="do not fit"):
        bp.spi_flash(bytes(4096), (1 << 20) - 100)


def test_capability_flag_parses_from_status_caps():
    bp = BenchPod(transport=SpiPod())
    assert bp.capabilities.spi_master
    assert not BenchPod(transport=SpiPod(caps=["la"])).capabilities.spi_master
