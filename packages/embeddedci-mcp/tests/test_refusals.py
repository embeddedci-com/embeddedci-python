"""The pod's policy refusals reach the agent with their type and what to do about them."""

from __future__ import annotations

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from embeddedci.benchpod import errors

from conftest import call

if not hasattr(errors, "PodLeasedError"):  # embeddedci 2.6.0, the pinned minimum
    pytest.skip("needs the typed refusals of embeddedci after 2.6.0", allow_module_level=True)

FirmwareError, firmware_error = errors.FirmwareError, errors.firmware_error


def _refuse(transport, cmd, message):
    real = transport.command

    def command(req):
        if req["cmd"] == cmd:
            raise firmware_error(message, cmd=cmd)
        return real(req)

    transport.command = command


def test_leased_names_the_holder_and_the_time_left(connected):
    _refuse(connected, "la", "busy: a cloud job holds this pod (octo/ci run 9, 12 s left)")
    with pytest.raises(ToolError) as info:
        call("gpio_pulse", la=3, width=0.001, count=5)
    msg = str(info.value)
    assert "PodLeasedError: la: busy: a cloud job holds this pod (octo/ci run 9, 12 s left)" in msg
    assert msg.endswith("Wait for that job to finish and retry; reads such as status still work "
                        "meanwhile.")


def test_locked_says_to_use_the_cloud_or_usb(connected):
    _refuse(connected, "la", "locked: la needs the cloud or the USB console")
    with pytest.raises(ToolError, match=r"PodLockedError: .*connect with 'embeddedci:<device>'"):
        call("gpio_pulse", la=3, width=0.001, count=5)


def test_forbidden_says_who_may(connected):
    _refuse(connected, "la", "forbidden: la needs an organization owner or admin")
    with pytest.raises(ToolError, match=r"PermissionDeniedError: .*benchpod:admin scope"):
        call("gpio_pulse", la=3, width=0.001, count=5)


def test_other_firmware_errors_have_no_hint(connected):
    def command(req):
        raise FirmwareError("la voltage not set", cmd=req["cmd"])

    connected.command = command
    with pytest.raises(ToolError) as info:
        call("gpio_pulse", la=3, width=0.001, count=5)
    assert str(info.value).endswith("FirmwareError: la: la voltage not set")


def test_status_warns_about_a_cloud_lease_and_a_locked_lan(connected):
    real = connected.status
    connected.status = lambda: dict(real(), lan_policy="locked", lan_policy_cmd=True,
                                    lease={"held": True, "holder": "octo/ci", "left_s": 30})
    result = call("status")
    warnings = " | ".join(result["warnings"])
    assert "a cloud job holds this pod (octo/ci, 30 s left)" in warnings
    assert "LAN policy is locked" in warnings


def test_capabilities_report_the_policy_flags(fake_open, fake_transport):
    real = fake_transport.status
    fake_transport.status = lambda: dict(real(), lan_policy="open", ota_sig=True,
                                         sig_policy="audit", tunnel_max_tier=True, analog=True)
    caps = call("connect", connection="192.168.1.50", la_voltage=3.3)["capabilities"]
    assert caps["lan_policy"] == "open" and caps["ota_sig"] and caps["sig_policy"] == "audit"
    assert caps["tunnel_max_tier"] and caps["analog"] is True
