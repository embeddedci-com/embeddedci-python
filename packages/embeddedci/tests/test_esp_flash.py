"""ESP32 targets through BenchPod.flash (embeddedci.benchpod.esp) against a fake pod."""

import json
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, esp
from embeddedci.benchpod.errors import FlashError
from embeddedci.benchpod.transport.base import Transport


class FakePod:
    """The pod's esp_flash command: a start runs for `polls` status calls, then finishes."""

    def __init__(self, result: str = "ok", polls: int = 2, chip: str = "ESP32-S3") -> None:
        self.result = result
        self.polls = polls
        self.chip = chip
        self.cmds: List[Dict[str, Any]] = []
        self.staged: List[bytes] = []
        self._left = 0
        self._job: Dict[str, Any] = {}

    def stage(self, data: bytes) -> int:
        self.staged.append(bytes(data))
        return len(data)

    def command(self, req: Dict[str, Any]) -> Any:
        self.cmds.append(dict(req))
        if req.get("cmd") != "esp_flash":
            return {}
        op = req["op"]
        if op in ("start", "sync"):
            self._job = dict(req)
            self._left = self.polls
            return {"started": True, "op": op}
        if op == "abort":
            self._left = 0
            return {"running": False, "result": "aborted"}
        self._left -= 1
        running = self._left > 0
        total = (self._job.get("len", 0) + 1023) // 1024
        return {"running": running, "phase": "write" if running else "done",
                "done": total // 2 if running else total, "total": total, "chip": self.chip,
                "baud": self._job.get("baud", 115200), "md5": "ab" * 16, "resends": 0,
                "result": None if running else self.result}

    def starts(self) -> List[Dict[str, Any]]:
        return [c for c in self.cmds if c.get("cmd") == "esp_flash" and c["op"] == "start"]


class FakeTransport(Transport):
    def __init__(self, pod: FakePod) -> None:
        self.pod = pod

    def command(self, req: Dict[str, Any]) -> Any:
        return self.pod.command(req)

    def stage_psram(self, data: bytes) -> int:
        return self.pod.stage(data)

    def close(self) -> None:
        pass

    def status(self) -> Any:
        return {"fpga_image": "deep"}   # ESP32 flashing runs on the deep replay image

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        pass

    def uart_proxy_start(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


PINS = {"rx": 5, "tx": 4, "boot": 6, "en": "nrst"}


def make_pod(pod: FakePod, signals=()) -> BenchPod:
    wiring = {"uart_rx": 5, "uart_tx": 4, "signals": list(signals)}
    return BenchPod(transport=FakeTransport(pod), lease=False, wiring=wiring)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(esp.time, "sleep", lambda s: None)


# -- through flash() ----------------------------------------------------------------------------

def test_flash_routes_esp32_targets(tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"\x01" * 5000)
    pod = FakePod()
    bp = make_pod(pod)
    res = bp.flash(target="esp32s3", file=str(app), load_address="0x10000", boot=6, nreset=True)
    assert res.ok and res.returncode == 0
    assert "ESP32-S3" in res.stdout and "0x010000 5000 bytes ok" in res.stdout
    start = pod.starts()[0]
    assert {k: start[k] for k in ("rx", "tx", "boot", "en", "offset", "len", "run")} == {
        "rx": 5, "tx": 4, "boot": 6, "en": "nrst", "offset": 0x10000, "len": 5000, "run": True}
    assert start["flash_size"] == 16 << 20
    assert pod.staged == [b"\x01" * 5000]


def test_flash_esp32_pins_from_profile_signals(tmp_path):
    app = tmp_path / "merged.bin"
    app.write_bytes(b"\x02" * 100)
    pod = FakePod()
    bp = make_pod(pod, signals=[{"name": "IO0", "la": 7}, {"name": "EN", "la": 8}])
    assert bp.flash(target="esp32", file=str(app)).ok
    start = pod.starts()[0]
    assert start["boot"] == 7 and start["en"] == 8 and start["offset"] == 0
    # nreset=True wins over an EN signal; reset=False leaves the target in its loader.
    bp.flash(target="esp32", file=str(app), nreset=True, reset=False)
    assert pod.starts()[1]["en"] == "nrst" and pod.starts()[1]["run"] is False


def test_flash_esp32_flasher_args(tmp_path):
    (tmp_path / "bootloader").mkdir()
    (tmp_path / "bootloader" / "bootloader.bin").write_bytes(b"L" * 8)
    (tmp_path / "pt.bin").write_bytes(b"P" * 3)
    (tmp_path / "app.bin").write_bytes(b"A" * 10)
    args = {"flash_files": {"0x1000": "bootloader/bootloader.bin", "0x8000": "pt.bin",
                            "0x10000": "app.bin"},
            "flash_settings": {"flash_size": "4MB"}}
    fa = tmp_path / "flasher_args.json"
    fa.write_text(json.dumps(args))
    pod = FakePod()
    assert make_pod(pod).flash(target="esp32", file=str(fa), boot=6).ok
    starts = pod.starts()
    assert [s["offset"] for s in starts] == [0x1000, 0x8000, 0x10000]
    assert [s["run"] for s in starts] == [False, False, True]
    assert {s["flash_size"] for s in starts} == {4 << 20}


def test_flash_esp32_failure_raises_or_reports(tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"x" * 10)
    bp = make_pod(FakePod(result="no answer from the ROM loader"))
    with pytest.raises(FlashError, match="powered"):
        bp.flash(target="esp32", file=str(app), boot=6)
    res = bp.flash(target="esp32", file=str(app), boot=6, check=False)
    assert not res.ok and "ROM loader" in res.stderr


def test_flash_esp32_argument_errors(tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"x")
    bp = make_pod(FakePod())
    with pytest.raises(ValueError, match="boot"):
        bp.flash(target="esp32", file=str(app))
    with pytest.raises(ValueError, match="nreset=True"):
        bp.flash(target="esp32", file=str(app), boot=6, nreset=False)
    with pytest.raises(ValueError, match="file"):
        bp.flash(target="esp32", boot=6)
    with pytest.raises(ValueError, match="not an uploaded build"):
        bp.flash(target="esp32", build="latest", boot=6)


# -- esp.run ------------------------------------------------------------------------------------

def run(pod: FakePod, regions, **kw):
    return esp.run(pod.command, pod.stage, regions, pins=PINS, sleep=lambda s: None, **kw)


def test_run_failure_stops_at_the_first_region():
    pod = FakePod(result="no answer from the ROM loader")
    r = run(pod, [(0, b"x" * 10), (0x1000, b"y" * 10)])
    assert not r.ok and "powered" in r.error and "0x0" in r.error
    assert len(pod.starts()) == 1


def test_run_unverified_counts_as_written():
    r = run(FakePod(result="unverified"), [(0, b"x" * 10)])
    assert r.ok and not r.verified


def test_run_timeout_aborts():
    pod = FakePod(polls=10 ** 9)
    t = [0.0]

    def clock() -> float:
        t[0] += 30.0
        return t[0]

    r = esp.run(pod.command, pod.stage, [(0, b"x" * 10)], pins=PINS, sleep=lambda s: None, clock=clock)
    assert not r.ok and "did not finish" in r.error
    assert pod.cmds[-1]["op"] == "abort"


def test_run_needs_stage_and_fit():
    with pytest.raises(FlashError, match="network, USB or cloud"):
        esp.run(lambda r: {}, None, [(0, b"x")], pins=PINS)
    with pytest.raises(ValueError, match="does not fit"):
        run(FakePod(), [(0x3F0000, b"x" * 0x20000)], flash_size=4 << 20)


# -- images and names ---------------------------------------------------------------------------

def test_load_images(tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"A" * 10)
    assert esp.load_images(str(app)) == ([(0, b"A" * 10)], {})
    assert esp.load_images(str(app), load_address=0x10000) == ([(0x10000, b"A" * 10)], {})
    with pytest.raises(ValueError, match="multiple of 4096"):
        esp.load_images(str(app), load_address=0x100)
    with pytest.raises(ValueError, match="overlaps"):
        esp.load_images({0: b"x" * 5000, 0x1000: b"y"})


def test_parse_helpers_and_targets():
    assert esp.parse_flash_size(None) == 16 << 20
    assert esp.parse_flash_size("4mb") == 4 << 20
    assert esp.parse_address("") == 0 and esp.parse_address("0x10000") == 0x10000
    for t in ("esp32", "ESP32-S3", "esp32c3", "esp32-c6"):
        assert esp.is_esp_target(t)
    for t in ("target/stm32f4x.cfg", "", "esp8266", "nrf52"):
        assert not esp.is_esp_target(t)
