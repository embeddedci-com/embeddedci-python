"""Pod refusals are classified by their stable error code only; firmware 4.0 sends no text with
one (BP0030 alone carries a message, for clients from before the codes).

One coded reply without a message per catalog code gives the class and fields of
``errors._BY_CODE`` and a message rendered from the catalog; a refusal without a code keeps its
text and is never classified from it."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from embeddedci.benchpod import errors
from embeddedci.benchpod.errors import (
    ClientTooOldError,
    DacPinoutMismatchError,
    DirectionConflictError,
    FirmwareError,
    PermissionDeniedError,
    PinConflictError,
    PodBusyError,
    PodLeasedError,
    PodLeaseLostError,
    PodLockedError,
    PodNotPairedError,
    PullConflictError,
    PullDirectionError,
    ReplyTooLargeError,
    TriggerTimeout,
    UnsupportedFeatureError,
    UsbCheckNoPowerError,
    UsbCheckRefusedError,
    UsbFlashRefusedError,
    UsbHostPortError,
    UsbHostUnavailableError,
    UsbSerialBusyError,
    UsbSerialUnavailableError,
    WiringError,
    XipUnavailableError,
    classify_firmware_error,
    error_catalog,
    firmware_error,
)
from embeddedci.benchpod.protocol import Reply, raise_for_status

#: code -> (the class, the fields its args fill). The args come from the catalog (_args).
CODED: Dict[str, Any] = {
    "BP0001": (FirmwareError, {}),
    "BP0002": (UnsupportedFeatureError, {"feature": "v_cmd"}),
    "BP0003": (PodBusyError, {}),
    "BP0004": (PodBusyError, {}),
    "BP0005": (PodBusyError, {}),
    "BP0006": (FirmwareError, {}),
    **{f"BP00{n:02d}": (WiringError, {"name": "v_name" if n in (7, 8, 9, 13) else ""})
       for n in range(7, 16)},
    "BP0016": (PodBusyError, {}),
    "BP0017": (UnsupportedFeatureError, {"feature": "v_feature"}),
    **{f"BP00{n}": (FirmwareError, {}) for n in range(18, 23)},
    "BP0023": (PullDirectionError, {"la": 7, "want": "", "have": ""}),
    "BP0024": (PullDirectionError, {"la": 7, "want": "v_want", "have": "v_have"}),
    "BP0025": (PodLeasedError, {"holder": "v_holder", "purpose": "v_purpose", "left_s": 7}),
    "BP0026": (PodLeaseLostError, {"by": "v_by"}),
    "BP0027": (FirmwareError, {}),
    "BP0028": (FirmwareError, {}),
    "BP0029": (FirmwareError, {}),
    "BP0030": (ClientTooOldError, {"client_api": 7, "min_client_api": 7, "api": 7}),
    "BP0031": (PinConflictError, {"la": 7, "function": "v_function", "owner": "v_owner",
                                  "instance": "v_instance"}),
    "BP0032": (PullConflictError, {"la": 7}),
    "BP0033": (PullConflictError, {"la": 7}),
    "BP0034": (DirectionConflictError, {"la": 7}),
    "BP0035": (DirectionConflictError, {"la": 7}),
    "BP0036": (FirmwareError, {}),
    "BP0037": (FirmwareError, {}),
    "BP0038": (TriggerTimeout, {"la": 7, "edge": "v_edge"}),
    "BP0039": (TriggerTimeout, {"la": 7, "edge": "start"}),
    **{f"BP00{n}": (PodLockedError, {}) for n in (40, 41, 42, 43, 44)},
    "BP0045": (PermissionDeniedError, {}),
    "BP0046": (UnsupportedFeatureError, {"feature": "analog"}),
    **{f"BP00{n}": (PodBusyError, {}) for n in (47, 48, 49, 50, 51, 52)},
    "BP0053": (ReplyTooLargeError, {}),
    "BP0054": (ReplyTooLargeError, {}),
    "BP0055": (FirmwareError, {}),
    "BP0056": (UnsupportedFeatureError, {"feature": "module"}),
    "BP0057": (FirmwareError, {}),
    "BP0058": (UnsupportedFeatureError, {"feature": "v_feature"}),
    "BP0059": (UnsupportedFeatureError, {"feature": "v_feature"}),
    "BP0060": (UnsupportedFeatureError, {"feature": "v_target"}),
    **{f"BP00{n}": (FirmwareError, {}) for n in (61, 62, 63)},
    "BP0064": (UnsupportedFeatureError, {"feature": "dac_loop_input_map"}),
    "BP0065": (FirmwareError, {}),
    "BP0066": (PodBusyError, {}),
    **{f"BP00{n}": (FirmwareError, {}) for n in range(67, 75)},
    "BP0075": (PodLockedError, {}),
    "BP0076": (UsbHostUnavailableError, {"feature": "usb_host", "port": ""}),
    "BP0077": (UsbHostUnavailableError, {"feature": "usb_host", "port": "v_port"}),
    "BP0078": (UsbHostUnavailableError, {"feature": "usb_host", "port": ""}),
    "BP0079": (UsbHostPortError, {"port": "v_port"}),
    "BP0080": (DacPinoutMismatchError, {"digital": "v_digital", "module": "v_module",
                                        "feature": "dac"}),
    "BP0081": (PodNotPairedError, {}),
    "BP0082": (FirmwareError, {}),
    "BP0083": (XipUnavailableError, {"why": "v_why"}),
    "BP0084": (UsbFlashRefusedError, {"why": "v_why"}),
    "BP0085": (UsbSerialUnavailableError, {"why": "v_why"}),
    "BP0086": (UsbSerialBusyError, {"why": "v_why"}),
    "BP0087": (UsbCheckRefusedError, {"why": "v_why"}),
    "BP0088": (UsbCheckNoPowerError, {}),
    "BP0089": (WiringError, {}),
    "BP0090": (FirmwareError, {}),
    "BP0091": (FirmwareError, {}),
}


def _args(code: str) -> Dict[str, Any]:
    """The args a pod sends with ``code``: every arg the catalog names, "v_<name>" or 7."""
    spec = error_catalog()["codes"][code].get("args") or {}
    return {name: (7 if kind == "int" else f"v_{name}") for name, kind in spec.items()}


def test_every_catalog_code_has_a_class():
    """A sync that adds a code fails here until the code is in both tables."""
    assert set(error_catalog()["codes"]) == set(errors._BY_CODE) == set(CODED)


@pytest.mark.parametrize("code", sorted(CODED))
def test_coded_reply_without_a_message(code):
    """The firmware 4.0 reply: code and args, no message. The class, its fields and the message
    all come from the code."""
    args = _args(code)
    with pytest.raises(FirmwareError) as info:
        raise_for_status(Reply(status="error", code=code, args=args), cmd="x")
    err = info.value
    cls, fields = CODED[code]
    assert type(err) is cls
    assert err.code == code and err.values == args and err.docs_url.endswith("#" + code)
    assert err.firmware_message == ""
    for name, want in fields.items():
        assert getattr(err, name) == want, name
    assert classify_firmware_error(err) is err
    entry = error_catalog()["codes"][code]
    msg = str(err)
    assert msg.startswith(f"x: {code}: ")
    assert errors._fill(entry["message"], args) in msg and errors._fill(entry["fix"]["sdk"], args) in msg
    assert "{" not in msg.replace(err.docs_url, "") and "unknown firmware error" not in msg


def test_a_plain_firmware_error_with_a_code_is_classified():
    """A fake transport may raise FirmwareError and attach the code: the client's
    re-classification gives the code's class."""
    plain = FirmwareError("", cmd="gpio")
    errors._attach_code(plain, "BP0031", {"la": 4, "owner": "gpio", "function": "gpio", "instance": ""})
    err = classify_firmware_error(plain)
    assert type(err) is PinConflictError and err.la == 4 and err.function == "gpio"


def test_bp0030_keeps_its_message_and_names_the_update():
    err = firmware_error("Update the BenchPod SDK, MCP or CLI: this firmware needs client API 3",
                         cmd="gpio", code="BP0030",
                         values={"client_api": 2, "min_client_api": 3, "api": 3})
    assert type(err) is ClientTooOldError and err.min_client_api == 3
    assert err.firmware_message.startswith("Update the BenchPod SDK")
    assert "pip install -U embeddedci" in str(err)


def test_la_voltage_names_the_sdk_ways_to_set_it():
    err = firmware_error("", cmd="uart_proxy_start", code="BP0001")
    assert str(err).startswith("uart_proxy_start: BP0001: The I/O voltage is not set")
    assert "bp.set_la_voltage(3.3)" in str(err)


def test_a_code_newer_than_the_sdk():
    err = firmware_error("", cmd="gpio", code="BP0999", values={"la": 4})
    assert type(err) is FirmwareError and err.code == "BP0999" and err.values == {"la": 4}
    assert "update it (pip install -U embeddedci)" in str(err) and "#BP0999" in str(err)
    said = firmware_error("something new", cmd="gpio", code="BP0999")
    assert "something new" in str(said)


@pytest.mark.parametrize("text", [
    "pin conflict: LA4 is in use by gpio",
    "locked: generate needs the cloud or USB",
    "busy: a cloud job holds this pod (octo run 7, 42 s left)",
    "unknown cmd",
    "this BenchPod has no analog front end",
    "la voltage not set",
    "missing steps",
])
def test_text_without_a_code_is_never_classified(text):
    """Free-text refusals keep their message as it is, and their class is plain FirmwareError."""
    err = firmware_error(text, cmd="cmd")
    assert type(err) is FirmwareError and err.code == ""
    assert err.firmware_message == text and str(err) == f"cmd: {text}"
    with pytest.raises(FirmwareError) as info:
        raise_for_status(Reply(status="error", message=text), cmd="cmd")
    assert type(info.value) is FirmwareError and str(info.value) == f"cmd: {text}"


def test_a_reply_without_code_or_message():
    with pytest.raises(FirmwareError, match="unknown firmware error"):
        raise_for_status(Reply(status="error"), cmd="x")


def test_explain():
    assert "BP0080 dac_pinout_mismatch" in errors.explain("bp0080")
    assert "not in this SDK's catalog" in errors.explain("BP0999")
