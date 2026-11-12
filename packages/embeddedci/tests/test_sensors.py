"""User-defined sensors: YAML 1.2 loading, definitions, chunked uploads and the bp.sensors API
against a fake pod (no hardware)."""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, Sensor, load_definition, load_stimulus, sensors
from embeddedci.benchpod.errors import FirmwareError, UnsupportedFeatureError
from embeddedci.benchpod.sensors import yaml12
from embeddedci.benchpod.sensors.definition import DefinitionError


# -- YAML ---------------------------------------------------------------------------

def test_yaml_12_booleans_keep_on_off_yes_no_as_words():
    doc = sensors.load_yaml("on: conversion\noff: 1\nyes: no\nflag: true\nx: 012\no: 0o17\n"
                            "e: 1e5\nd: 2026-01-01\nh: 0x76\n")
    assert doc == {"on": "conversion", "off": 1, "yes": "no", "flag": True, "x": 12, "o": 15,
                   "e": 100000.0, "d": "2026-01-01", "h": 0x76}


@pytest.mark.parametrize("reader", ["pyyaml", "mini"])
def test_both_readers_refuse_duplicate_keys(reader):
    text = "a: 1\na: 2\n"
    with pytest.raises(yaml12.YamlError, match="duplicate key"):
        yaml12.load_yaml(text) if reader == "pyyaml" else yaml12.MiniYaml(text).parse()


def test_the_builtin_reader_matches_pyyaml_on_every_library_file():
    lib = sensors.bundled_library()
    assert lib.entries
    for entry in lib.entries:
        text = lib.text(entry.name)
        assert yaml12.MiniYaml(text).parse() == yaml12.load_yaml(text), entry.path


def test_the_builtin_reader_is_used_without_pyyaml(monkeypatch):
    def no_pyyaml():
        raise ImportError("no yaml")

    monkeypatch.setattr(yaml12, "_pyyaml_loader", no_pyyaml)
    assert sensors.load_yaml("on: 1\nlist: [a, 0x10]\n") == {"on": 1, "list": ["a", 16]}


# -- definitions ----------------------------------------------------------------------

MINIMAL = {"format": 1, "kind": "i2c", "name": "probe", "label": "a probe",
           "params": [{"key": "t", "min": 0, "max": 10, "default": 1}],
           "i2c": {"address": 0x40}, "registers": [{"at": 0, "u8": "t"}]}


def test_load_definition_from_dict_text_json_file_yaml_file_and_library(tmp_path):
    assert load_definition(MINIMAL) == MINIMAL
    assert load_definition(json.dumps(MINIMAL)) == MINIMAL
    yaml_text = ("format: 1\nkind: i2c\nname: probe\nlabel: a probe\n"
                 "params:\n  - { key: t, min: 0, max: 10, default: 1 }\n"
                 "i2c: { address: 0x40 }\nregisters:\n  - { at: 0, u8: t }\n")
    assert load_definition(yaml_text) == MINIMAL
    (tmp_path / "p.yaml").write_text(yaml_text)
    (tmp_path / "p.json").write_text(json.dumps(MINIMAL))
    assert load_definition(tmp_path / "p.yaml") == MINIMAL
    assert load_definition(str(tmp_path / "p.json")) == MINIMAL
    bme = load_definition("bme280")
    assert bme["name"] == "bme280" and bme["i2c"]["address"] == 0x76
    assert load_definition("sensors/i2c/bme280.yaml") == bme


def test_header_problems_are_listed_together():
    with pytest.raises(DefinitionError) as info:
        load_definition({"format": 2, "kind": "can", "name": "Bad Name", "params": [{}]})
    msg = str(info.value)
    for part in ("format: 2", "kind: 'can'", "name: 'Bad Name'", "params[0]: needs a key"):
        assert part in msg
    with pytest.raises(DefinitionError, match="neither a file nor an entry"):
        load_definition("no_such_sensor")
    with pytest.raises(DefinitionError, match="this is a stimulus"):
        load_definition({"format": 1, "kind": "stimulus", "name": "s"})
    with pytest.raises(DefinitionError, match="more than the pod's 32768"):
        load_definition({**MINIMAL, "description": "x" * 33000})
    with pytest.raises(DefinitionError, match="invalid YAML"):
        load_definition("format: 1\n  bad: [\n")


def test_stimulus_data_files_are_inlined(tmp_path):
    (tmp_path / "trace.csv").write_text("t,value\n0,20\n10,25.5\n")
    (tmp_path / "s.yaml").write_text(
        "format: 1\nkind: stimulus\nname: warm\nsignals:\n  temperature_c:\n"
        "    - { shape: data, file: trace.csv, interpolate: linear }\n"
        "    - shape: repeat\n      count: 2\n      segments:\n"
        "        - { shape: data, file: trace.csv }\n")
    doc = load_stimulus(tmp_path / "s.yaml")
    segs = doc["signals"]["temperature_c"]
    assert segs[0] == {"shape": "data", "points": [[0.0, 20.0], [10.0, 25.5]], "interpolate": "linear"}
    assert segs[1]["segments"][0]["points"] == [[0.0, 20.0], [10.0, 25.5]]
    text = "format: 1\nkind: stimulus\nname: w\nsignals:\n  t:\n    - { shape: data, file: trace.csv }\n"
    assert load_stimulus(text, base_dir=tmp_path)["signals"]["t"][0]["points"][1] == [10.0, 25.5]
    with pytest.raises(DefinitionError, match="must be stimulus"):
        load_stimulus(MINIMAL)


# -- chunked uploads ------------------------------------------------------------------

@pytest.mark.parametrize("size", [1, 500, 2000, 32768])
def test_upload_chunks_fit_a_cloud_command_and_reassemble(size):
    data = bytes((i * 7) % 256 for i in range(size))
    reqs = sensors.upload_requests("sensor_define", data, instance="x" * 32, dry_run=True)
    out = b""
    for req in reqs:
        assert len(json.dumps(req, separators=(",", ":"))) <= sensors.MAX_COMMAND_BYTES - 128
        assert req["offset"] == len(out) and req["total"] == size
        out += base64.b64decode(req["data_b64"])
    assert out == data
    assert [r.get("dry_run") for r in reqs] == [None] * (len(reqs) - 1) + [True]
    assert "instance" not in sensors.upload_requests("sensor_stimulus", data)[0]


# -- the pod API -----------------------------------------------------------------------

class FakePod:
    """A transport that plays the draft sensor commands."""

    def __init__(self, caps=("sensors",)) -> None:
        self.caps = list(caps)
        self.commands: List[Dict[str, Any]] = []
        self.uploads: Dict[str, bytes] = {}
        self.instances: Dict[str, Dict[str, Any]] = {}

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "firmware_version": "3.11.0", "caps": self.caps,
                "sensors_format": 1}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def samples(self, req: dict) -> List[int]:
        self.commands.append(req)
        return list(range(int(req.get("len", req.get("samples", 0)))))

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd in ("sensor_define", "sensor_stimulus"):
            key = req.get("instance", "_stimulus")
            buf = self.uploads.get(key, b"") if req["offset"] else b""
            if req["offset"] != len(buf):
                raise FirmwareError(f"chunk at offset {req['offset']}, expected {len(buf)}", cmd=cmd)
            buf += base64.b64decode(req["data_b64"])
            self.uploads[key] = buf
            if len(buf) < req["total"]:
                return {"instance": key, "received": len(buf)}
            doc = json.loads(buf)
            if cmd == "sensor_stimulus":
                return {"name": doc["name"], "signals": len(doc.get("signals", {}))}
            if doc.get("registers") == "broken":
                raise FirmwareError("invalid definition: registers: must be a list", cmd=cmd)
            info = {"instance": key, "kind": doc["kind"], "name": doc["name"],
                    "params": doc.get("params", []), "program_bytes": 100}
            if not req.get("dry_run"):
                self.instances[key] = dict(info)
            return info
        if cmd == "sensor_start":
            self.instances[req["instance"]].update(running=True, pins=req["pins"])
            return {"instance": req["instance"], "slot": "i2c0", "image": "emulation",
                    "image_loaded": True, "unavailable": ["dac_control_loop", "deep_replay"]}
        if cmd == "sensor_status":
            return {"image": "emulation", "instances": [dict(i, instance=k) for k, i in self.instances.items()]}
        if cmd == "sensor_stop":
            if req.get("all"):
                self.instances.clear()
            else:
                self.instances.pop(req["instance"], None)
            return {}
        return {}


def make_pod(**kw) -> "tuple[BenchPod, FakePod]":
    fake = FakePod(**kw)
    bp = BenchPod(transport=fake, lease=False,
                  wiring={"i2c_sda": 2, "i2c_scl": 1, "signals": [{"name": "DRDY", "la": 7}]})
    return bp, fake


def test_old_firmware_gets_a_clear_error_before_anything_is_sent():
    bp, fake = make_pod(caps=("dac",))
    for call in (lambda: bp.sensors.start("bme280"), lambda: bp.sensors.status(),
                 lambda: bp.sensors.validate("bme280"), lambda: bp.sensors.stop()):
        with pytest.raises(UnsupportedFeatureError) as info:
            call()
        assert str(info.value) == ("this pod's firmware is too old for sensor emulation; "
                                   "update the firmware")
        assert info.value.feature == "sensors"
    assert fake.commands == []


def test_start_defines_in_chunks_then_starts_with_wired_pins():
    bp, fake = make_pod()
    s = bp.sensors.start("bme280", instance="env", pins={"drdy": "DRDY"}, address=0x77,
                         values={"temperature_c": 30})
    assert isinstance(s, Sensor) and s.instance == "env" and s.kind == "i2c" and s.name == "bme280"
    assert s.started["image_loaded"] is True
    defines = [c for c in fake.commands if c["cmd"] == "sensor_define"]
    assert len(defines) > 1 and all(c["instance"] == "env" for c in defines)
    assert json.loads(fake.uploads["env"]) == load_definition("bme280")
    start = fake.commands[-1]
    assert start == {"cmd": "sensor_start", "instance": "env", "pins": {"sda": 2, "scl": 1, "drdy": 7},
                     "address": "0x77", "values": {"temperature_c": 30}}
    assert [p["key"] for p in s.params][:2] == ["temperature_c", "pressure_pa"]
    assert s.status()["running"] is True


def test_pins_are_checked_per_kind():
    bp, _ = make_pod()
    uart = {"format": 1, "kind": "uart", "name": "gps", "uart": {"baud": 9600}}
    with pytest.raises(ValueError, match="needs pins tx"):
        bp.sensors.start(uart)
    with pytest.raises(ValueError, match="not mosi"):
        bp.sensors.start("bmp280", pins={"mosi": 3})
    analog = {"format": 1, "kind": "analog", "name": "ntc"}
    bp.sensors.start(analog)
    assert bp.transport.commands[-1]["pins"] == {"output": "dac"}
    with pytest.raises(ValueError, match="'dac' or 'current'"):
        bp.sensors.start(analog, pins={"output": "adc"})
    with pytest.raises(ValueError, match=r"\[a-z0-9_\]"):
        bp.sensors.start("bmp280", instance="Bad-Name")


def test_handle_drives_set_fault_regs_capture_and_stop():
    bp, fake = make_pod()
    with bp.sensors.start("bmp280", pins={"sda": "i2c_sda", "scl": 1}) as s:
        s.set(temperature_c=31.5, pressure_pa=None)
        assert fake.commands[-1] == {"cmd": "sensor_set", "instance": "bmp280",
                                     "values": {"temperature_c": 31.5}}
        s.fault("no_response", duration_s=2)
        assert fake.commands[-1] == {"cmd": "sensor_fault", "instance": "bmp280",
                                     "type": "no_response", "duration_s": 2.0}
        s.fault("offset", param="temperature_c", value=1.5)
        assert fake.commands[-1]["param"] == "temperature_c" and fake.commands[-1]["value"] == 1.5
        s.clear_fault()
        assert fake.commands[-1] == {"cmd": "sensor_fault_clear", "instance": "bmp280"}
        assert s.regs(0xF7, 8) == list(range(8))
        assert fake.commands[-1] == {"cmd": "sensor_regs", "instance": "bmp280", "start": "0xF7", "len": 8}
        assert len(s.capture(64)) == 64
        with pytest.raises(ValueError, match="at least one value"):
            s.set()
        with pytest.raises(ValueError, match="must be a number"):
            s.set(temperature_c="hot")
    assert fake.commands[-1] == {"cmd": "sensor_stop", "instance": "bmp280"}
    assert bp.sensors.status()["instances"] == []


def test_stimulus_upload_start_stop_and_stop_all(tmp_path):
    bp, fake = make_pod()
    (tmp_path / "pts.csv").write_text("0,1\n1,2\n")
    (tmp_path / "stim.yaml").write_text(
        "format: 1\nkind: stimulus\nname: ramp\nrepeat: true\nsignals:\n"
        "  env.temperature_c:\n    - { shape: ramp, start: 20, end: 30, duration_s: 5 }\n"
        "  env.pressure_pa:\n    - { shape: data, file: pts.csv }\n")
    reply = bp.sensors.load_stimulus(tmp_path / "stim.yaml")
    assert reply == {"name": "ramp", "signals": 2}
    sent = json.loads(fake.uploads["_stimulus"])
    assert sent["signals"]["env.pressure_pa"][0]["points"] == [[0.0, 1.0], [1.0, 2.0]]
    bp.sensors.start_stimulus(at_s=3)
    assert fake.commands[-1] == {"cmd": "sensor_stimulus_start", "at_s": 3.0}
    bp.sensors.stop_stimulus()
    assert fake.commands[-1] == {"cmd": "sensor_stimulus_stop"}
    bp.sensors.stop()
    assert fake.commands[-1] == {"cmd": "sensor_stop", "all": True}


def test_validate_is_a_dry_run_and_reports_compile_errors():
    bp, fake = make_pod()
    report = bp.sensors.validate("sht4x")
    assert report["name"] == "sht4x" and fake.instances == {}
    defines = [c for c in fake.commands if c["cmd"] == "sensor_define"]
    assert defines[-1]["dry_run"] is True and all("dry_run" not in c for c in defines[:-1])
    with pytest.raises(FirmwareError, match="invalid definition"):
        bp.sensors.validate({**MINIMAL, "registers": "broken"})


def test_sensor_handle_for_a_running_instance():
    bp, _ = make_pod()
    bp.sensors.start("bmp280", instance="baro")
    assert bp.sensors.sensor("baro").name == "bmp280"
    with pytest.raises(Exception, match="no sensor instance 'nope'"):
        bp.sensors.sensor("nope")
