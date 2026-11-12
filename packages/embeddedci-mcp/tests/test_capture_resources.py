"""The capture-view resources: the last LA/ADC capture in full and the last decode, timed."""

import json

import anyio

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
