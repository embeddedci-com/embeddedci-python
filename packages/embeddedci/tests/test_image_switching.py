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
    """A fake pod with two gateware images: ``image`` 0 = loop, 1 = deep replay.

    ``image=None`` is a single-image pod that advertises neither feature; ``switch_works=False``
    makes a switch boot an image without the requested feature.
    """

    def __init__(self, image: Optional[int] = 0, *, switch_works: bool = True) -> None:
        self.image = image
        self.switch_works = switch_works
        self.commands: List[dict] = []
        self.uploads: List[bool] = []

    def status(self) -> Dict[str, Any]:
        caps = ["dac", "dac_replay", "dac_loop_sources"]
        if self.image == 0:
            caps.append("dac_control_loop")
        elif self.image == 1:
            caps.append("dac_deep_replay")
        return {"board": "stm32h563", "adc_bits": 16, "caps": caps}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        if req["cmd"] == "fpga_image":
            if not self.switch_works:
                return {"image": req["image"], "version": 31, "features": 0}
            self.image = req["image"]
            return {"image": req["image"], "version": 31, "features": 0x01 if self.image == 0 else 0x02}
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


@pytest.mark.benchpod_capability("dac_cotrig")
def test_a_missing_feature_still_skips(benchpod):
    pass
""")
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1, skipped=1)
