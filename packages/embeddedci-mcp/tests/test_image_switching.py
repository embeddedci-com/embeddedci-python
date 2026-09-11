"""Tools switch the gateware image automatically (fake two-image pod, no hardware)."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci_mcp.session import SESSION

from conftest import call


def _on_image(fake, image):
    fake.image = image
    SESSION.require().refresh_capabilities()


def _cmds(fake):
    return [r["cmd"] for r in fake.requests]


def test_control_loop_switches_to_the_loop_image(connected):
    _on_image(connected, 1)
    armed = call("control_loop", curve=[0, 60000])
    assert armed["switched_image"] == "loop"
    assert _cmds(connected).index("fpga_image") < _cmds(connected).index("dac_control_loop")
    assert call("control_loop", curve=[0, 60000])["switched_image"] is None


def test_switch_image_false_fails_and_names_the_fix(connected):
    _on_image(connected, 1)
    with pytest.raises(ToolError, match="BenchPodError: .*switch_image"):
        call("control_loop", curve=[0, 60000], switch_image=False)
    assert "fpga_image" not in _cmds(connected)


def test_long_replay_switches_to_the_deep_replay_image(connected):
    _on_image(connected, 0)
    res = call("replay", volts=[1.0] * 4096)
    assert res["deep"] is True and res["switched_image"] == "deep_replay"
    assert call("replay", volts=[1.0] * 16)["switched_image"] is None


def test_a_pod_without_images_is_left_alone(connected):
    armed = call("control_loop", curve=[0, 60000])
    assert armed["switched_image"] is None and "fpga_image" not in _cmds(connected)
