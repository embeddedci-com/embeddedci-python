"""The capture-view resources: the last LA/ADC capture in full and the last decode, timed."""

import json

import anyio
import pytest

from embeddedci.benchpod.results import LaCapture
from embeddedci_mcp.server import (capture_adc_resource, capture_decode_resource, capture_la_resource,
                                   mcp)
from embeddedci_mcp.session import SESSION

from conftest import call


def test_resources_are_registered():
    uris = {str(r.uri) for r in anyio.run(mcp.list_resources)}
    assert {"benchpod://capture/la", "benchpod://capture/adc", "benchpod://capture/decode"} <= uris


def test_nothing_captured_yet(connected):
    for res in (capture_la_resource, capture_adc_resource, capture_decode_resource):
        assert json.loads(res()) == {"seq": 0}


def test_la_is_run_length_edges(connected):
    SESSION.last_la = LaCapture(words=[0, 0, 1, 1, 3, 3, 3, 0], sample_rate_hz=1000.0)
    d = json.loads(capture_la_resource())
    assert d["edges"] == [[0, 0], [2, 1], [4, 3], [7, 0]]
    assert d["count"] == 8 and d["sample_rate_hz"] == 1000.0 and d["seq"] == SESSION.la_seq > 0
    assert d["correlated"] is False and d["edges_truncated"] is False


def test_adc_samples_and_correlated_seq(connected):
    call("capture_correlated", adc_samples=500, la_samples=100, points=10)
    adc, la = json.loads(capture_adc_resource()), json.loads(capture_la_resource())
    assert len(adc["samples"]) == 500 and adc["unit"] == "V"
    assert adc["correlated"] and la["correlated"] and la["seq"] == adc["seq"] + 1
    call("capture_adc", samples=200, sample_rate_hz=100_000)
    assert json.loads(capture_adc_resource())["correlated"] is False


def test_decode_is_kept_for_the_view(connected):
    call("capture_la", samples=1000, sample_rate_hz=1e6)
    call("decode_la", protocol="uart", rx=1, baud=9600)
    d = json.loads(capture_decode_resource())
    assert d["protocol"] == "uart" and d["seq"] == SESSION.la_seq and d["channels"]["rx"] == 1
    assert isinstance(d["frames"], list)
    if d["frames"]:
        assert {"start_us", "end_us", "hex", "text", "ok"} <= set(d["frames"][0])
    call("capture_la", samples=100, sample_rate_hz=1e6)  # a new capture drops the old decode
    assert json.loads(capture_decode_resource()) == {"seq": 0}


def test_i2c_decode_has_timed_segments(connected):
    # START, 0x50 write, ACK, 0xAB, ACK, STOP on SDA=LA1, SCL=LA2 (the shared parity vector)
    words = [3, 3, 2, 2, 1, 3, 1, 0, 2, 0, 1, 3, 1, 0, 2, 0, 0, 2, 0, 0, 2, 0, 0, 2, 0, 0, 2, 0, 0, 2, 0, 1, 3,
             1, 0, 2, 0, 1, 3, 1, 0, 2, 0, 1, 3, 1, 0, 2, 0, 1, 3, 1, 1, 3, 1, 0, 2, 0, 0, 2, 2, 3, 3]
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=1e6)
    call("decode_la", protocol="i2c", sda=1, scl=2)
    t = json.loads(capture_decode_resource())["transactions"][0]
    assert t["address"] == "0x50" and t["ok"] and t["direction"] == "write"
    assert [s["type"] for s in t["segments"]][:3] == ["start", "address-write", "ack"]


try:  # the min-SDK CI job runs these tests against an embeddedci without the LA decoders
    import embeddedci.benchpod.usb_decode  # noqa: F401
    _no_usb = False
except ImportError:
    _no_usb = True
needs_usb_sdk = pytest.mark.skipif(_no_usb, reason="needs embeddedci 4.0.2 (the LA decoders)")


def _usb_vector_words():
    """The SDK's shared full-speed enumeration vector (48 MS/s, D+ LA1, D- LA2) as dense words."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "embeddedci" / "tests" / "testdata" / "usb_decode_vectors.json"
    case = json.loads(path.read_text())["cases"][0]
    words, edges = [0] * case["count"], case["edges"]
    for k, (at, w) in enumerate(edges):
        end = edges[k + 1][0] if k + 1 < len(edges) else case["count"]
        words[at:end] = [w] * (end - at)
    return words, case["sample_rate_hz"]


@needs_usb_sdk
def test_usb_decode_lists_the_enumeration(connected):
    words, rate = _usb_vector_words()
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=rate)
    r = call("decode_la", protocol="usb", dp=1, dm=2)
    assert r["speed"] == "full" and r["errors"] == 0 and r["sof_count"] == 2
    assert any("SET_ADDRESS 5: OK" in t for t in r["transfers"])
    assert any("NAK x2" in t for t in r["items"])
    d = json.loads(capture_decode_resource())
    assert d["protocol"] == "usb" and d["channels"] == {"dp": 1, "dm": 2, "speed": "auto"}
    assert {"packets", "transactions", "transfers"} <= set(d)
    assert d["transfers"][0]["request"] == "GET_DESCRIPTOR DEVICE length 64"


@needs_usb_sdk
def test_usb_decode_needs_both_lines(connected):
    SESSION.last_la = LaCapture(words=[1] * 100, sample_rate_hz=48e6)
    with pytest.raises(Exception, match="usb needs dp and dm"):
        call("decode_la", protocol="usb", dp=1)


def _vector(name: str, case: str):
    """A shared decode vector as dense words, with its case."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "embeddedci" / "tests" / "testdata" / f"{name}_decode_vectors.json"
    c = next(x for x in json.loads(path.read_text())["cases"] if x["name"] == case)
    words, edges = [0] * c["count"], c["edges"]
    for k, (at, w) in enumerate(edges):
        end = edges[k + 1][0] if k + 1 < len(edges) else c["count"]
        words[at:end] = [w] * (end - at)
    return words, c


@needs_usb_sdk
def test_fieldbus_decoders_through_the_tool(connected):
    words, c = _vector("modbus", "rtu_19200_session")
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=c["sample_rate_hz"])
    r = call("decode_la", protocol="modbus", rx=3, baud=19200)
    assert r["errors"] == 0 and "READ_HOLDING_REGISTERS 107: 555 0 100" in r["items"][1]
    assert json.loads(capture_decode_resource())["frames"][1]["reply_to"] == 1

    words, c = _vector("can", "fd_500k_2m_48msps")
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=c["sample_rate_hz"])
    r = call("decode_la", protocol="can", can=4, bitrate=500000, data_bitrate=2000000)
    assert any("FD BRS [64]" in i for i in r["items"]) and any("error frame" in i for i in r["items"])

    words, c = _vector("pwm", "fan_and_servo_1msps")
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=c["sample_rate_hz"])
    r = call("decode_la", protocol="pwm", channels=[1, 2], servo=True)
    assert r["count"] == 2 and "servo" in r["items"][1]
    assert [ch["channel"] for ch in json.loads(capture_decode_resource())["channels"]] == [1, 2]

    words, c = _vector("onewire", "standard_and_overdrive_4msps")
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=c["sample_rate_hz"])
    r = call("decode_la", protocol="onewire", onewire=6)
    assert any("READ_SCRATCHPAD 21.5 °C" in i for i in r["items"])

    words, c = _vector("smbus", "pmbus_100k_1msps")
    SESSION.last_la = LaCapture(words=words, sample_rate_hz=c["sample_rate_hz"])
    r = call("decode_la", protocol="pmbus", sda=1, scl=2)
    assert any("READ_VOUT 00 06 3 V PEC ok" in i for i in r["items"])
    assert json.loads(capture_decode_resource())["transactions"][0]["command_name"] == "VOUT_MODE"


@pytest.mark.parametrize("args, msg", [
    ({"protocol": "modbus", "rx": 1}, "modbus needs rx and baud"),
    ({"protocol": "can", "can": 1}, "can needs"),
    ({"protocol": "pwm"}, "pwm needs channels"),
    ({"protocol": "onewire"}, "onewire needs"),
    ({"protocol": "smbus", "sda": 1}, "smbus needs sda and scl"),
])
def test_fieldbus_decoders_need_their_lines(connected, args, msg):
    SESSION.last_la = LaCapture(words=[1] * 100, sample_rate_hz=1e6)
    with pytest.raises(Exception, match=msg):
        call("decode_la", **args)
