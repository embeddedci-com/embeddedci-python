"""Automatic gateware image switching, against a fake two-image pod (no hardware).

``control_loop``, ``replay``, ``replay_waveform`` and the ``benchpod_capability`` marker put the
pod on the image they need; ``switch_image=False`` refuses instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import BenchPod, BenchPodError, FpgaImage


class ImagePod:
    """A fake pod with three gateware images: ``image`` 0 = loop, 1 = deep replay, 2 = sensor
    emulation (it shows neither image feature; ``sensors_digital`` says it is there).

    ``image=None`` is a single-image pod that advertises neither feature; ``switch_works=False``
    makes a switch boot an image without the requested feature.
    """

    def __init__(self, image: Optional[int] = 0, *, switch_works: bool = True) -> None:
        self.image = image
        self.switch_works = switch_works
        self.commands: List[dict] = []
        self.uploads: List[bool] = []
        #: sensor_status instances (kind, running) on the emulation image.
        self.sensors: List[dict] = []

    def status(self) -> Dict[str, Any]:
        caps = ["dac", "dac_replay", "dac_loop_sources"]
        if self.image == 0:
            caps.append("dac_control_loop")
        elif self.image == 1:
            caps.append("dac_deep_replay")
        if self.image is not None:
            caps.append("sensors")
        return {"board": "stm32h563", "adc_bits": 16, "caps": caps,
                "sensors_digital": self.image is not None}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        if req["cmd"] == "fpga_image" and "image" not in req:
            if self.image is None:
                raise BenchPodError("unknown command")
            return {"image": self.image, "version": 54, "features": 1 << self.image}
        if req["cmd"] == "sensor_status":
            return {"image": "emulation", "instances": self.sensors}
        if req["cmd"] == "fpga_image":
            if self.image == 2 and any(i["running"] and i["kind"] != "analog" for i in self.sensors):
                raise BenchPodError("image switch refused")
            if not self.switch_works:
                return {"image": req["image"], "version": 31, "features": 0}
            self.image = req["image"]
            return {"image": req["image"], "version": 54, "features": 1 << self.image}
        if req["cmd"] == "dac_control_loop":
            return {"armed": True, "k": req["k"], "vmin": req["vmin"], "vmax": req["vmax"],
                    "tick_div": req["tick_div"]}
        return {}

    def load_replay(self, *, data: bytes, replay: dict, psram: bool = False) -> Any:
        self.uploads.append(psram)
        return {"samples": replay["samples"]}

    def target_power(self, efuse: int, on: bool, delay_ms: int = 0) -> None:
        pass

    def close(self) -> None:
        pass

    def cmds(self) -> List[str]:
        return [c["cmd"] for c in self.commands]


def _bp(pod: ImagePod) -> BenchPod:
    return BenchPod(transport=pod, lease=False)


# -- control loop --------------------------------------------------------------------------

def test_control_loop_switches_a_deep_replay_pod_to_the_loop_image(caplog):
    pod = ImagePod(image=1)
    bp = _bp(pod)
    with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
        loop = bp.control_loop(curve=[0, 30000, 60000])
    assert pod.cmds().index("fpga_image") < pod.cmds().index("dac_control_loop")
    assert {"cmd": "fpga_image", "image": 0} in pod.commands
    assert loop.switched_image is not None and loop.switched_image.image == FpgaImage.LOOP
    assert bp.capabilities.dac_control_loop
    assert "switching the FPGA to the LOOP gateware image" in caplog.text


def test_control_loop_on_the_loop_image_does_not_switch():
    pod = ImagePod(image=0)
    loop = _bp(pod).control_loop(curve=[0, 60000])
    assert "fpga_image" not in pod.cmds() and loop.switched_image is None


def test_control_loop_switch_image_false_raises_before_touching_the_pod():
    pod = ImagePod(image=1)
    with pytest.raises(BenchPodError, match=r"switch_image=True.*FpgaImage\.LOOP"):
        _bp(pod).control_loop(curve=[0, 60000], switch_image=False)
    assert "fpga_image" not in pod.cmds() and "dac_control_loop" not in pod.cmds()


def test_invalid_loop_arguments_raise_before_switching():
    pod = ImagePod(image=1)
    with pytest.raises(ValueError):
        _bp(pod).control_loop(curve=[0, 60000], vmin=50000, vmax=1000)
    assert "fpga_image" not in pod.cmds()


def test_a_switch_that_does_not_take_raises():
    pod = ImagePod(image=1, switch_works=False)
    with pytest.raises(BenchPodError, match="does not carry"):
        _bp(pod).control_loop(curve=[0, 60000])
    assert "dac_control_loop" not in pod.cmds()


def test_control_loop_switches_back_from_the_emulation_image_once_the_sensors_stopped():
    pod = ImagePod(image=2)
    pod.sensors = [{"instance": "ntc", "kind": "analog", "running": True}]
    loop = _bp(pod).control_loop(curve=[0, 60000])
    assert {"cmd": "fpga_image"} in pod.commands
    assert {"cmd": "fpga_image", "image": 0} in pod.commands
    assert loop.switched_image is not None and loop.switched_image.image == FpgaImage.LOOP


def test_control_loop_on_the_emulation_image_with_digital_sensors_names_the_fix():
    pod = ImagePod(image=2)
    pod.sensors = [{"instance": "env", "kind": "i2c", "running": True}]
    with pytest.raises(BenchPodError, match=r"sensors env: stop them first \(bp\.sensors\.stop\(\)\)"):
        _bp(pod).control_loop(curve=[0, 60000])
    assert {"cmd": "fpga_image", "image": 0} not in pod.commands
    assert "dac_control_loop" not in pod.cmds()


def test_control_loop_on_the_emulation_image_switch_image_false_is_refused():
    pod = ImagePod(image=2)
    with pytest.raises(BenchPodError, match=r"switch_image=True.*FpgaImage\.LOOP"):
        _bp(pod).control_loop(curve=[0, 60000], switch_image=False)
    assert "dac_control_loop" not in pod.cmds()


def test_deep_replay_switches_from_the_emulation_image():
    pod = ImagePod(image=2)
    handle = _bp(pod).replay([1.0] * 4096, dac_path="5v")
    assert {"cmd": "fpga_image", "image": 1} in pod.commands and handle.deep


def test_the_emulation_image_refusals_read_as_sdk_calls():
    from embeddedci.benchpod.errors import UnsupportedFeatureError, firmware_error

    e = firmware_error("", cmd="dac_control_loop", code="BP0059",
                       values={"feature": "control loop", "image": "loop"})
    assert isinstance(e, UnsupportedFeatureError) and e.feature == "control loop"
    assert "bp.sensors.stop()" in str(e)
    e = firmware_error("", cmd="fpga_image", code="BP0057", values={"sensors": "env, imu"})
    assert "env, imu" in str(e) and "bp.sensors.stop()" in str(e)


# -- replay --------------------------------------------------------------------------------

def test_deep_replay_switches_a_loop_pod_to_the_deep_replay_image():
    pod = ImagePod(image=0)
    handle = _bp(pod).replay([1.0] * 4096, dac_path="5v")
    assert {"cmd": "fpga_image", "image": 1} in pod.commands
    assert pod.uploads == [True] and handle.deep
    assert handle.switched_image is not None and handle.switched_image.image == FpgaImage.DEEP_REPLAY


def test_shallow_replay_never_switches():
    pod = ImagePod(image=0)
    handle = _bp(pod).replay([1.0] * 2048, dac_path="5v")
    assert "fpga_image" not in pod.cmds() and handle.switched_image is None
    assert pod.uploads == [False]


def test_deep_replay_switch_image_false_is_refused():
    pod = ImagePod(image=0)
    with pytest.raises(BenchPodError, match=r"switch_image=True.*FpgaImage\.DEEP_REPLAY"):
        _bp(pod).replay([1.0] * 4096, dac_path="5v", switch_image=False)
    assert pod.uploads == [] and "fpga_image" not in pod.cmds()


def test_a_single_image_pod_is_never_switched():
    pod = ImagePod(image=None)
    bp = _bp(pod)
    assert bp.control_loop(curve=[0, 60000]).switched_image is None  # the firmware decides
    with pytest.raises(BenchPodError, match="at most 2048"):
        bp.replay([1.0] * 4096, dac_path="5v")
    assert "fpga_image" not in pod.cmds()


# -- server-side replay_waveform -----------------------------------------------------------

class FakeServer:
    """The server's cached capabilities mirror the pod's running image."""

    def __init__(self, pod: ImagePod) -> None:
        self.pod = pod
        self.replays: List[dict] = []

    def device_parameters(self, name: str) -> Dict[str, Any]:
        return {"cap.dac_replay": "true",
                "cap.dac_control_loop": "true" if self.pod.image == 0 else "false",
                "cap.dac_deep_replay": "true" if self.pod.image == 1 else "false"}

    def resolve_device_id(self, name: str) -> str:
        return "dev-1"

    def replay_start(self, payload: dict) -> Dict[str, Any]:
        self.replays.append(payload)
        return {"samples": 1000}


def _cloud_bp(monkeypatch, pod: ImagePod, sample_count: int):
    server = FakeServer(pod)
    bp = _bp(pod)
    bp._device_name = "bench-pod"
    monkeypatch.setattr(bp, "_try_server_api", lambda: server)
    monkeypatch.setattr(bp, "_require_server_api", lambda: server)
    library = SimpleNamespace(get=lambda wid: SimpleNamespace(id=wid, dac_path="5v",
                                                              sample_count=sample_count))
    monkeypatch.setattr(type(bp), "waveforms", property(lambda self: library))
    bp.refresh_capabilities()
    return bp, server


def test_server_side_replay_switches_for_a_full_length_recording(monkeypatch):
    pod = ImagePod(image=0)
    bp, server = _cloud_bp(monkeypatch, pod, 100_000)
    handle = bp.replay_waveform("wf-1")
    assert {"cmd": "fpga_image", "image": 1} in pod.commands
    assert handle.switched_image is not None and handle.switched_image.image == FpgaImage.DEEP_REPLAY
    assert handle.deep and len(server.replays) == 1


def test_server_side_replay_keeps_the_image_when_it_fits_or_downsampling_is_wanted(monkeypatch):
    pod = ImagePod(image=0)
    bp, server = _cloud_bp(monkeypatch, pod, 100_000)
    assert bp.replay_waveform("wf-1", target_samples=4096).switched_image is None
    assert bp.replay_waveform("wf-1", window_len=1000).switched_image is None
    assert bp.replay_waveform("wf-1", switch_image=False).switched_image is None  # server downsamples
    assert "fpga_image" not in pod.cmds() and len(server.replays) == 3


# -- pytest marker -------------------------------------------------------------------------

def test_capability_marker_switches_the_image_instead_of_skipping(pytester):
    tests_dir = str(Path(__file__).parent)
    pytester.makeconftest(f"""
import sys

import pytest

sys.path.insert(0, {tests_dir!r})
from test_image_switching import ImagePod
from embeddedci.benchpod import BenchPod


@pytest.fixture(scope="session")
def benchpod():
    return BenchPod(transport=ImagePod(image=1), lease=False)
""")
    pytester.makepyfile("""
import pytest


@pytest.mark.benchpod_capability("dac_control_loop")
def test_runs_on_the_loop_image(benchpod):
    assert benchpod.capabilities.dac_control_loop


@pytest.mark.benchpod_capability("adc_low_range")
def test_a_missing_feature_still_skips(benchpod):
    pass
""")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1, skipped=1)


# -- ESP32 flashing (stages the image in PSRAM: the deep-replay image) ------------------------

class EspImagePod(ImagePod):
    """ImagePod with esp_flash and PSRAM staging, which the deep-replay image alone accepts."""

    def __init__(self, image: Optional[int]) -> None:
        super().__init__(image)
        self.staged_on: List[Optional[int]] = []

    def stage_psram(self, data: bytes) -> int:
        if self.image not in (1, None):
            raise BenchPodError("load_bin: deep replay is not in this image")
        self.staged_on.append(self.image)
        return len(data)

    def command(self, req: dict) -> Any:
        if req["cmd"] == "esp_flash":
            self.commands.append(req)
            if req["op"] == "status":
                return {"running": False, "result": "ok", "chip": "ESP32", "md5": "ab" * 16,
                        "done": 1, "total": 1, "baud": 460800}
            return {"started": True}
        return super().command(req)


def _flash_esp(pod: EspImagePod, tmp_path: Path):
    app = tmp_path / "app.bin"
    app.write_bytes(b"\x01" * 100)
    bp = BenchPod(transport=pod, lease=False, wiring={"uart_rx": 3, "uart_tx": 4})
    return bp.flash(target="esp32", file=str(app), boot=6, nreset=True)


@pytest.mark.parametrize("image", [0, 2])
def test_esp_flash_switches_to_the_deep_replay_image_before_staging(image, tmp_path, monkeypatch):
    from embeddedci.benchpod import esp
    monkeypatch.setattr(esp.time, "sleep", lambda s: None)
    pod = EspImagePod(image)
    assert _flash_esp(pod, tmp_path).ok
    assert {"cmd": "fpga_image", "image": 1} in pod.commands
    assert pod.staged_on == [1]


def test_esp_flash_on_the_deep_replay_image_does_not_switch(tmp_path, monkeypatch):
    from embeddedci.benchpod import esp
    monkeypatch.setattr(esp.time, "sleep", lambda s: None)
    pod = EspImagePod(1)
    assert _flash_esp(pod, tmp_path).ok
    assert not any(c["cmd"] == "fpga_image" and "image" in c for c in pod.commands)


# -- bus masters (gateware v65: not in the emulation image) --------------------------------

class SplitPod(ImagePod):
    """An ImagePod whose gateware is ``gateware``; from v65 only loop (0x29) and deep (0x2a)
    have the bus masters (feature bit 0x20), emulation (0x04) does not."""

    def __init__(self, image: int, gateware: int) -> None:
        super().__init__(image=image)
        self.gateware = gateware

    def status(self) -> Dict[str, Any]:
        return dict(super().status(), gateware=self.gateware)

    def command(self, req: dict) -> Any:
        if req["cmd"] == "fpga_image":
            out = super().command(req)
            if isinstance(out, dict):
                loops = {0: 0x29, 1: 0x2A, 2: 0x04}
                out = dict(out, version=self.gateware,
                           features=loops[self.image] if self.gateware >= 65 else 1 << self.image)
            return out
        if req["cmd"] == "spi_start":
            self.commands.append(req)
            return {"active": True}
        return super().command(req)


def test_has_bus_masters_by_gateware_and_image():
    assert _bp(SplitPod(2, 64)).has_bus_masters() is True
    assert _bp(SplitPod(2, 65)).has_bus_masters() is False
    assert _bp(SplitPod(0, 65)).has_bus_masters() is True
    assert _bp(SplitPod(1, 65)).has_bus_masters() is True


def test_bus_master_use_on_the_emulation_image_with_sensors_running_is_refused():
    from embeddedci.benchpod.errors import UnsupportedFeatureError

    pod = SplitPod(2, 65)
    pod.sensors = [{"instance": "env", "kind": "i2c", "running": True}]
    with pytest.raises(UnsupportedFeatureError, match=r"the SPI master.*env") as info:
        _bp(pod)._ensure_bus_masters("the SPI master")
    assert info.value.code == "BP0059" and info.value.feature == "the SPI master"
    assert "bp.sensors.stop()" in str(info.value)
    assert {"cmd": "fpga_image", "image": 0} not in pod.commands


def test_bus_master_use_on_the_emulation_image_without_sensors_switches_to_loop_once(caplog):
    pod = SplitPod(2, 65)
    bp = _bp(pod)
    with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
        info = bp._ensure_bus_masters("SWD")
        assert info is not None and info.image == FpgaImage.LOOP
        assert bp._ensure_bus_masters("SWD") is None
    assert pod.commands.count({"cmd": "fpga_image", "image": 0}) == 1
    assert "switching the FPGA to the LOOP gateware image" in caplog.text


def test_bus_master_use_on_the_loop_or_deep_image_does_nothing():
    for image in (0, 1):
        pod = SplitPod(image, 65)
        assert _bp(pod)._ensure_bus_masters("SWD") is None
        assert not [c for c in pod.commands if "image" in c]


def test_bus_master_use_on_gateware_64_emulation_does_nothing():
    pod = SplitPod(2, 64)
    pod.sensors = [{"instance": "env", "kind": "i2c", "running": True}]
    assert _bp(pod)._ensure_bus_masters("SWD") is None
    assert not [c for c in pod.commands if "image" in c]


def test_open_spi_switches_the_emulation_image_before_spi_start():
    pod = SplitPod(2, 65)
    bp = _bp(pod)
    bp.open_spi(sck=1, mosi=2, miso=3, cs=4)
    cmds = pod.cmds()
    assert cmds.index("fpga_image") < cmds.index("spi_start")
    assert {"cmd": "fpga_image", "image": 0} in pod.commands
