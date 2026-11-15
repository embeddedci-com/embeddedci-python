"""ESP32 target flashing (embeddedci.benchpod.esp, BenchPod.flash_esp) against a fake pod."""

import json
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import esp
from embeddedci.benchpod.errors import FlashError


class FakePod:
    """The pod's esp_flash command: start/sync run for `polls` status calls, then finish."""

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
        op = req["op"]
        if op in ("start", "sync"):
            self._job = dict(req)
            self._left = self.polls
            return {"started": True, "op": op}
        if op == "abort":
            self._left = 0
            return {"running": False, "result": "aborted"}
        assert op == "status"
        self._left -= 1
        running = self._left > 0
        total = (self._job.get("len", 0) + 1023) // 1024
        return {"running": running, "phase": "write" if running else "done",
                "done": total // 2 if running else total, "total": total, "chip": self.chip,
                "baud": self._job.get("baud", 115200), "md5": "ab" * 16, "resends": 0,
                "result": None if running else self.result}


PINS = {"rx": 5, "tx": 4, "boot": 6, "en": "nrst"}


def run(pod: FakePod, regions, **kw):
    return esp.run(pod.command, pod.stage, regions, pins=PINS, sleep=lambda s: None, **kw)


def test_single_region_ok():
    pod = FakePod()
    seen = []
    r = run(pod, [(0x10000, b"\x01" * 5000)], progress=lambda d, t: seen.append((d, t)))
    assert r.ok and r.verified and r.chip == "ESP32-S3"
    assert pod.staged == [b"\x01" * 5000]
    start = pod.cmds[0]
    assert start == {"cmd": "esp_flash", "op": "start", "rx": 5, "tx": 4, "boot": 6, "en": "nrst",
                     "offset": 0x10000, "len": 5000, "baud": 460800, "flash_size": 4 << 20,
                     "run": True}
    assert seen[-1] == (5000, 5000)
    assert [round(g.offset) for g in r.regions] == [0x10000]


def test_multi_region_runs_only_last():
    pod = FakePod()
    regions = [(0x1000, b"b" * 100), (0x8000, b"p" * 50), (0x10000, b"a" * 3000)]
    r = run(pod, regions)
    starts = [c for c in pod.cmds if c["op"] == "start"]
    assert [c["offset"] for c in starts] == [0x1000, 0x8000, 0x10000]
    assert [c["run"] for c in starts] == [False, False, True]
    assert r.ok and len(r.regions) == 3


def test_failure_stops_and_explains():
    pod = FakePod(result="no answer from the ROM loader")
    r = run(pod, [(0, b"x" * 10), (0x1000, b"y" * 10)])
    assert not r.ok
    assert "powered" in r.error and "0x0" in r.error
    assert len([c for c in pod.cmds if c["op"] == "start"]) == 1


def test_unverified_counts_as_written():
    pod = FakePod(result="unverified")
    r = run(pod, [(0, b"x" * 10)])
    assert r.ok and not r.verified


def test_timeout_aborts():
    pod = FakePod(polls=10 ** 9)
    t = [0.0]

    def clock() -> float:
        t[0] += 30.0
        return t[0]

    r = esp.run(pod.command, pod.stage, [(0, b"x" * 10)], pins=PINS, sleep=lambda s: None, clock=clock)
    assert not r.ok and "did not finish" in r.error
    assert pod.cmds[-1]["op"] == "abort"


def test_needs_stage():
    with pytest.raises(FlashError, match="network, USB or cloud"):
        esp.run(lambda r: {}, None, [(0, b"x")], pins=PINS)


def test_does_not_fit_flash():
    pod = FakePod()
    with pytest.raises(ValueError, match="does not fit"):
        run(pod, [(0x3F0000, b"x" * 0x20000)], flash_size=4 << 20)


def test_sync():
    pod = FakePod(chip="ESP32")
    assert esp.sync(pod.command, pins=PINS, sleep=lambda s: None) == "ESP32"
    assert pod.cmds[0] == {"cmd": "esp_flash", "op": "sync", **PINS}
    bad = FakePod(result="no answer from the ROM loader")
    with pytest.raises(FlashError, match="ROM loader"):
        esp.sync(bad.command, pins=PINS, sleep=lambda s: None)


def test_load_images_forms(tmp_path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"A" * 10)
    assert esp.load_images(str(app)) == ([(0, b"A" * 10)], {})

    regions, _ = esp.load_images({"0x10000": str(app), 0x1000: b"B" * 4})
    assert regions == [(0x1000, b"B" * 4), (0x10000, b"A" * 10)]

    (tmp_path / "bootloader").mkdir()
    (tmp_path / "bootloader" / "bootloader.bin").write_bytes(b"L" * 8)
    (tmp_path / "pt.bin").write_bytes(b"P" * 3)
    args = {"flash_files": {"0x1000": "bootloader/bootloader.bin", "0x8000": "pt.bin",
                            "0x10000": "app.bin"},
            "flash_settings": {"flash_mode": "dio", "flash_size": "8MB", "flash_freq": "40m"},
            "extra_esptool_args": {"chip": "esp32"}}
    fa = tmp_path / "flasher_args.json"
    fa.write_text(json.dumps(args))
    regions, settings = esp.load_images(str(fa))
    assert [o for o, _ in regions] == [0x1000, 0x8000, 0x10000]
    assert settings == {"flash_size": "8MB", "chip": "esp32"}
    assert esp.parse_flash_size(settings["flash_size"]) == 8 << 20


def test_load_images_rejects(tmp_path):
    with pytest.raises(ValueError, match="multiple of 4096"):
        esp.load_images({0x100: b"x"})
    with pytest.raises(ValueError, match="overlaps"):
        esp.load_images({0: b"x" * 5000, 0x1000: b"y"})
    with pytest.raises(ValueError, match="empty"):
        esp.load_images({0: b""})


def test_parse_flash_size_and_targets():
    assert esp.parse_flash_size(None) == 4 << 20
    assert esp.parse_flash_size("16mb") == 16 << 20
    assert esp.parse_flash_size(2097152) == 2 << 20
    with pytest.raises(ValueError):
        esp.parse_flash_size("lots")
    for t in ("esp32", "ESP32-S3", "esp32c3", "esp32-c6"):
        assert esp.is_esp_target(t)
    for t in ("target/stm32f4x.cfg", "", "esp8266", "nrf52"):
        assert not esp.is_esp_target(t)
