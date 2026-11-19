"""Console log level (``log_level`` / ``set_log_level``, firmware feature ``log_level``, features
table revision 4): what the SDK sends, the level check, and the clean refusal on firmware without
the feature (3.10.12 and older answer ``unknown cmd``)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import LOG_LEVELS, BenchPod, UnsupportedFeatureError


class Pod:
    def __init__(self, features_rev: int, version: str = "3.10.13") -> None:
        self.features_rev = features_rev
        self.version = version
        self.level = "info"
        self.sent: List[Dict[str, Any]] = []

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": self.version, "la_voltage": 3.3,
                "features_rev": self.features_rev, "gateware": 58, "fpga_image": "deep",
                "hw": ["nrst_pin"]}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def command(self, req: dict) -> Any:
        if req.get("cmd") == "status":
            return self.status()
        if req["cmd"] != "log_level":
            return {}  # connect-time setup (la_dir, ...)
        self.sent.append(req)
        if "set" in req:
            self.level = req["set"]
        return {"level": self.level}


def _pod(features_rev: int = 4, version: str = "3.10.13"):
    pod = Pod(features_rev, version)
    return BenchPod(transport=pod, lease=False, wiring={"version": 2}), pod


def test_levels_quietest_first() -> None:
    assert LOG_LEVELS == ("error", "warn", "info", "debug")


def test_read_sends_no_argument() -> None:
    bp, pod = _pod()
    assert bp.log_level() == "info"
    assert pod.sent == [{"cmd": "log_level"}]


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_set_each_level(level: str) -> None:
    bp, pod = _pod()
    assert bp.set_log_level(level) == level
    assert pod.sent[-1] == {"cmd": "log_level", "set": level}
    assert bp.log_level() == level


def test_set_normalizes_case() -> None:
    bp, pod = _pod()
    assert bp.set_log_level(" DEBUG ") == "debug"
    assert pod.sent[-1] == {"cmd": "log_level", "set": "debug"}


@pytest.mark.parametrize("bad", ["verbose", "", "warning", 3, None])
def test_set_refuses_other_names_without_sending(bad: Any) -> None:
    bp, pod = _pod()
    with pytest.raises(ValueError, match="error, warn, info, debug"):
        bp.set_log_level(bad)
    assert pod.sent == []


def test_old_firmware_is_unsupported() -> None:
    bp, pod = _pod(features_rev=3, version="3.10.12")
    for call in (bp.log_level, lambda: bp.set_log_level("debug")):
        with pytest.raises(UnsupportedFeatureError) as e:
            call()
        assert e.value.feature == "log_level"
        assert "3.10.12" in str(e.value)
    assert pod.sent == []


def test_supports() -> None:
    assert _pod(4)[0].supports("log_level")
    assert not _pod(3)[0].supports("log_level")
