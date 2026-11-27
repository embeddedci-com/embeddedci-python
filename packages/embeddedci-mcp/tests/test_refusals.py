"""The pod's policy refusals reach the agent with their type and what to do about them."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci.benchpod import errors

from conftest import call

if not hasattr(errors, "PodLeasedError"):  # embeddedci 2.6.0, the pinned minimum
    pytest.skip("needs the typed refusals of embeddedci after 2.6.0", allow_module_level=True)

FirmwareError, firmware_error = errors.FirmwareError, errors.firmware_error


def _refuse(transport, verb, code, **args):
    """The pod refuses ``verb`` the way firmware 4.0 does: a code and its args, no message."""
    real = transport.command

    def command(req):
        if req["cmd"] == verb:
            raise firmware_error("", cmd=verb, code=code, values=args)
        return real(req)

    transport.command = command


def _pulse_error() -> str:
    with pytest.raises(ToolError) as info:
        call("gpio_pulse", la=3, width_s=0.001, count=5)
    return str(info.value)


def test_leased_names_the_holder_and_the_time_left(connected):
    _refuse(connected, "la_step", "BP0025", holder="octo/ci run 9", purpose="hil", left_s=12)
    msg = _pulse_error()
    assert "PodLeasedError: la_step: BP0025: octo/ci run 9 holds this pod (hil, 12 s left)" in msg


def test_locked_says_to_use_the_cloud_or_usb(connected):
    _refuse(connected, "la_step", "BP0040", cmd="la_step")
    assert "PodLockedError: la_step: BP0040: The pod's LAN is locked: la_step needs the cloud or USB" in _pulse_error()


def test_forbidden_says_who_may(connected):
    _refuse(connected, "la_step", "BP0045", cmd="la_step")
    assert "PermissionDeniedError: la_step: BP0045: la_step needs an organization owner or admin" in _pulse_error()


def test_unknown_command_says_the_firmware_lacks_it(connected):
    _refuse(connected, "la_step", "BP0002", cmd="la_step")
    msg = _pulse_error()
    assert "UnsupportedFeatureError: la_step: BP0002: " in msg and "la_step" in msg


def test_digital_board_says_it_has_no_analog(connected):
    _refuse(connected, "la_step", "BP0046", cmd="la_step")
    assert "UnsupportedFeatureError: la_step: BP0046: This BenchPod has no analog front end" in _pulse_error()


def test_free_text_refusals_keep_their_text(connected):
    def command(req):
        raise FirmwareError("missing steps", cmd=req["cmd"])

    connected.command = command
    assert _pulse_error().endswith("FirmwareError: la_step: missing steps")


def test_la_voltage_not_set_names_set_la_voltage(connected):
    _refuse(connected, "la_step", "BP0001")
    msg = _pulse_error()
    assert "FirmwareError: la_step: BP0001: The I/O voltage is not set; " in msg
    assert "set_la_voltage" in msg


def test_pin_conflict_names_the_tool(connected):
    _refuse(connected, "la_step", "BP0031", la=3, owner="gpio", function="gpio", instance="")
    msg = _pulse_error()
    assert "PinConflictError: la_step: BP0031: LA3 is in use by gpio; free LA3 first: gpio_release(la=[3])" in msg


def test_pull_conflict_names_set_pull(connected):
    _refuse(connected, "la_step", "BP0032", la=7, ohms="10k", function="gpio open_drain",
            reason="a released line would read low")
    msg = _pulse_error()
    assert "PullConflictError: la_step: BP0032: LA7 has its 10k pull-down on" in msg
    assert 'set_pull(la=[7], pull="off")' in msg


def test_dac_pinout_mismatch(connected):
    _refuse(connected, "la_step", "BP0080", cmd="dac_out", digital="v3r4+", module="3.4")
    msg = _pulse_error()
    assert "DacPinoutMismatchError: la_step: BP0080: dac_out refused, the DAC is off" in msg
    assert "the digital board and the analog module do not belong together" in msg


def test_status_warns_about_a_dac_pinout_mismatch(connected):
    real = connected.status
    connected.status = lambda: dict(real(), module={"present": True, "dac_pinout_ok": False})
    assert any("flat-cable pinouts" in w and "BP0080" in w for w in call("status")["warnings"])
    connected.status = lambda: dict(real(), module={"present": True, "dac_pinout_ok": True})
    assert not any("flat-cable" in w for w in call("status")["warnings"])


def test_supplies_are_named_the_same_everywhere():
    import anyio

    from embeddedci_mcp import guide, server

    tools = anyio.run(server.mcp.list_tools)
    texts = [guide.INSTRUCTIONS, guide.WIRING]
    texts += [f"{t.description} {t.inputSchema} {t.outputSchema}" for t in tools]
    joined = "\n".join(texts)
    assert "supply='5v'" in guide.INSTRUCTIONS and "supply='external'" in guide.INSTRUCTIONS
    assert "supply='5v'" in guide.WIRING
    for old in ("j1_3v3", "j1_1v8", "= internal 5 V", "= external supply", "internal 5 V (efuse 1)"):
        assert old not in joined, old


def test_status_warns_about_a_cloud_lease_and_a_locked_lan(connected):
    real = connected.status
    connected.status = lambda: dict(real(), lan_policy="locked", lan_policy_cmd=True,
                                    lease={"held": True, "holder": "octo/ci", "left_s": 30})
    result = call("status")
    warnings = " | ".join(result["warnings"])
    assert "a cloud job holds this pod (octo/ci, 30 s left)" in warnings
    assert "LAN policy is locked" in warnings


def test_status_warns_about_a_weak_usb_port(connected):
    real = connected.status
    connected.status = lambda: dict(real(), usb={"net": True, "link": True, "source": "default", "source_ma": 500})
    assert any("default USB power (500 mA)" in w for w in call("status")["warnings"])
    connected.status = lambda: dict(real(), usb={"net": True, "link": True, "source": "3.0A", "source_ma": 3000})
    assert not any("USB power" in w for w in call("status")["warnings"])


def test_capabilities_report_the_policy_flags(fake_open, fake_transport):
    real = fake_transport.status
    fake_transport.status = lambda: dict(real(), lan_policy="open", ota_sig=True,
                                         sig_policy="audit", tunnel_max_tier=True, analog=True)
    caps = call("connect", connection="192.168.1.50", la_voltage=3.3)["capabilities"]
    assert caps["lan_policy"] == "open" and caps["ota_sig"] and caps["sig_policy"] == "audit"
    assert caps["tunnel_max_tier"] and caps["analog"] is True


def test_status_reports_the_macs(connected):
    real = connected.status
    connected.status = lambda: dict(real(), eth_mac="02:35:c7:76:b7:9b", wifi_mac="7c:e8:b1:c5:ca:6c")
    result = call("status")
    assert result["eth_mac"] == "02:35:c7:76:b7:9b" and result["wifi_mac"] == "7c:e8:b1:c5:ca:6c"


# -- the client API level (embeddedci after 3.0.5) ---------------------------------------------

needs_client_api = pytest.mark.skipif(not hasattr(errors, "ClientTooOldError"),
                                      reason="needs ClientTooOldError (embeddedci after 3.0.5)")


@needs_client_api
def test_requests_name_this_server():
    from embeddedci.benchpod import protocol

    assert protocol.client_name().startswith("mcp ")


@needs_client_api
def test_a_pod_above_this_servers_level_fails_the_connect(fake_open, fake_transport):
    real = fake_transport.status
    fake_transport.status = lambda: {**real(), "version": "5.0.0", "api": 3, "min_client_api": 2}
    with pytest.raises(ToolError) as info:
        call("connect", connection="192.168.1.50", la_voltage=3.3)
    msg = str(info.value)
    assert "ClientTooOldError: this BenchPod MCP server (embeddedci-mcp " in msg
    assert "the pod's firmware 5.0.0 speaks client API 3, this server handles 2" in msg
    assert "uvx embeddedci-mcp@latest" in msg and "pip install -U embeddedci-mcp" in msg
    assert "benchpod plugin" in msg and "pip install -U embeddedci " not in msg


def test_firmware_3_fails_the_connect_with_the_update(fake_open, fake_transport):
    real = fake_transport.status
    fake_transport.status = lambda: {k: v for k, v in {**real(), "version": "3.10.13"}.items()
                                     if k not in ("api", "min_client_api")}
    with pytest.raises(ToolError) as info:
        call("connect", connection="192.168.1.50", la_voltage=3.3)
    msg = str(info.value)
    assert "FirmwareTooOldError: the pod runs firmware 3.10.13 (client API 1)" in msg
    assert "needs firmware 4.0.0 or later" in msg and "benchpod flash-self" in msg


@needs_client_api
def test_bp0030_on_a_tool_says_to_update_this_server(connected):
    def command(req):
        raise firmware_error("update the BenchPod SDK, MCP or CLI", cmd=req["cmd"], code="BP0030",
                             values={"client_api": 1, "min_client_api": 2, "api": 2})

    connected.command = command
    with pytest.raises(ToolError) as info:
        call("gpio_pulse", la=3, width_s=0.001, count=5)
    msg = str(info.value)
    assert "ClientTooOldError: la_step: this BenchPod MCP server" in msg
    assert "needs client API 2 or newer, this server sent 1" in msg
    assert "uvx embeddedci-mcp@latest" in msg
