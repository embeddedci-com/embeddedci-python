"""The JSON the SDK sends for every command firmware 4.0 renamed (benchpod-firmware docs/API.md,
features json_names_4, json_booleans_4, can_start_stop, la_step_pull_split, generate_volts_only),
and the renamed reply fields it reads. The public SDK names (volts, seconds, hertz) are unchanged."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

from embeddedci.benchpod import BenchPod, Wiring
from embeddedci.benchpod.state import DacOutput


class Pod:
    """Records every request; replies come from ``replies`` (by command) as firmware 4.0 sends them."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.power: List[Any] = []
        self.replies: Dict[str, Any] = {
            "i2c_start": lambda r: {"sda": r["sda"], "scl": r["scl"], "clock_hz": r["clock_hz"],
                                    "pullups": r["pullups"]},
            "spi_start": lambda r: {**{k: r[k] for k in ("sck", "mosi", "miso", "cs", "mode")},
                                    "clock_hz": 4_000_000},
            "dac_out": lambda r: {"path": "dac_" + r["path"], "mv": 2500, "uv": 2500000, "code16": 40000},
            "dac_loop_probe": {"i": 1, "in": 2, "source": "adc", "code16": 1234, "tripped": False},
            "dac_loop_input": {"source": "fixed", "input": 5, "step": 0, "code16": 99},
            "can_read": {"frames": [{"id": 1, "ext": False, "rtr": False, "fd": False, "brs": False,
                                     "dlc": 1, "data": [7], "ts_ms": 4242}], "overflow": 0},
            "usb_network": lambda r: {"enabled": r.get("enabled", True)},
            "la_pull": lambda r: ({"la": r["la"], "state": r.get("pull", "off"), "dirs": ["up"],
                                   "pullups_available": True} if "la" in r
                                  else {"la_pullup_mask": 1, "pullups_available": True}),
            "la_step": lambda r: {"la": r["la"], "steps": r["steps"], "delay_us": r["delay_us"],
                                  "status": "started"},
        }

    def status(self) -> Dict[str, Any]:
        return {"version": "4.0.0", "board": "stm32h563", "adc_bits": 16, "api": 2, "min_client_api": 2,
                "features_rev": 11, "gateware": 59, "fpga_image": "deep", "hw": [], "analog": True}

    def command(self, req: Dict[str, Any]) -> Any:
        if req["cmd"] in ("wiring_list", "la_dir"):
            return {}
        self.sent.append(req)
        r = self.replies.get(req["cmd"], {})
        return r(req) if callable(r) else r

    def stream_chunks(self, req: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
        self.sent.append(req)
        yield {"status": "ok", "data": [1, 2], "adc_sample_rate_hz": 400000,
               "la_sample_rate_hz": 1000000, "more": False}

    def load_replay(self, *, data: bytes, replay: Dict[str, Any], psram: bool = False) -> Any:
        self.sent.append(replay)
        return {"samples": replay["samples"], "sample_rate_hz": 100000}

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        self.power.append((efuse, on))

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass


def _bp() -> "tuple[BenchPod, Pod]":
    pod = Pod()
    return BenchPod(transport=pod, lease=False, wiring=Wiring(i2c_sda=1, i2c_scl=2)), pod


def test_rates_are_hertz():
    bp, pod = _bp()
    bp.generate("sine", freq_hz=1000, amplitude=1.0, dac_path="3v3", sample_rate_hz=500_000)
    assert pod.sent[-1] == {"cmd": "generate", "waveform": "sine", "freq_hz": 1000, "path": "3v3",
                            "amplitude_v": 1.0, "offset_v": 1.65, "sample_rate_hz": 500000}
    bp.capture_la(16, sample_rate_hz=2e6)
    assert pod.sent[-1]["cmd"] == "la_capture" and pod.sent[-1]["sample_rate_hz"] == 2000000
    cap = bp.capture_correlated(adc_samples=2, la_samples=2, adc_sample_rate_hz=1e5,
                                la_sample_rate_hz=1e6)
    req = pod.sent[-1]
    assert req["cmd"] == "capture_dual" and req["adc_sample_rate_hz"] == 100000
    assert req["la_sample_rate_hz"] == 1000000 and "adc_rate_mhz" not in req
    assert cap.adc.sample_rate_hz == 400000          # read from adc_sample_rate_hz
    bp.replay([0, 100], are_codes=True, dac_path="5v", sample_rate_hz=100_000)
    assert pod.sent[-1]["sample_rate_hz"] == 100000 and "sample_rate_mhz" not in pod.sent[-1]


def test_clock_hz_for_the_bus_masters():
    bp, pod = _bp()
    i2c = bp.open_i2c(hz=100_000)
    assert pod.sent[-1] == {"cmd": "i2c_start", "sda": 1, "scl": 2, "clock_hz": 100000, "pullups": "pod"}
    assert i2c.hz == 100000
    spi = bp.open_spi(sck=7, mosi=8, miso=9, cs=10, hz=4_000_000)
    assert pod.sent[-1]["cmd"] == "spi_start" and pod.sent[-1]["clock_hz"] == 4000000
    assert spi.hz == 4000000


def test_dac_out_takes_v_and_replies_code16():
    bp, pod = _bp()
    out = bp.dac_output("5v", volts=2.5)
    assert pod.sent[-1] == {"cmd": "dac_out", "path": "5v", "v": 2.5}
    assert out.voltage == 2.5 and out.code16 == 40000 and out.code == 40000 >> 8
    assert DacOutput.from_reply({"path": "dac_5v"}).voltage is None   # routed only


def test_loop_replies_are_code16():
    bp, pod = _bp()
    assert bp.loop_probe().v == 1234
    assert bp.loop_input(input_code=5, source="fixed").output_code == 99


def test_power_profile_sample_rate_hz():
    bp, pod = _bp()
    req = bp._power_request("5v", None, 400.0, 0)
    assert req["cmd"] == "power_profile" and req["sample_rate_hz"] == 400 and "rate_hz" not in req


def test_can_start_stop_and_ts_ms():
    bp, pod = _bp()
    bp.can_config(bitrate=500_000, mode="internal")
    assert pod.sent[-1]["cmd"] == "can_start"
    assert bp.can_read().frames[0].ts == 4242
    bp.can_disable()
    assert pod.sent[-1] == {"cmd": "can_stop"}


def test_la_step_and_la_pull():
    bp, pod = _bp()
    bp.la_step(3, steps=10, delay=0.001)
    assert pod.sent[-1]["cmd"] == "la_step" and pod.sent[-1]["steps"] == 10
    bp.set_pull(1, "up")
    assert pod.sent[-1] == {"cmd": "la_pull", "la": 1, "pull": "up"}
    assert bp.enabled_pulls() == [1] and pod.sent[-1] == {"cmd": "la_pull"}


def test_booleans():
    bp, pod = _bp()
    assert bp.usb_network(False) is False
    assert pod.sent[-1] == {"cmd": "usb_network", "enabled": False}
    assert bp.usb_network() is True and pod.sent[-1] == {"cmd": "usb_network"}
    pod.replies["target_status"] = {"efuse1": {"enabled": True, "fault": False, "valid": True},
                                    "efuse2": {"enabled": False, "fault": True, "valid": True}}
    st = bp.target_status()
    assert st.supplies["5v"].enabled and not st.supplies["5v"].fault and st.supplies["external"].fault


def test_target_power_over_tcp_sends_enabled():
    import json
    from embeddedci.benchpod.transport.tcp import TcpTransport

    sent: List[Dict[str, Any]] = []
    t = TcpTransport("10.237.8.85:8080")
    t.command = lambda req: sent.append(json.loads(json.dumps(req)))  # type: ignore[method-assign]
    t.target_power(2, True, delay_ms=5)
    t.target_power(1, False)
    assert sent == [{"cmd": "target_power", "efuse": 2, "enabled": True, "delay_ms": 5},
                    {"cmd": "target_power", "efuse": 1, "enabled": False}]
