"""Pod refusals are classified by their stable error code; the text only decides without one.

One coded reply per catalog code gives the class and fields of ``errors._BY_CODE``; one text-only
reply (firmware without codes) per text pattern gives the class it always did."""

from __future__ import annotations

import pytest

from embeddedci.benchpod import errors
from embeddedci.benchpod.errors import (
    ClientTooOldError,
    DirectionConflictError,
    FirmwareError,
    PermissionDeniedError,
    PinConflictError,
    PodBusyError,
    PodLeasedError,
    PodLeaseLostError,
    PodLockedError,
    PullConflictError,
    PullDirectionError,
    TriggerTimeout,
    UnsupportedFeatureError,
    WiringError,
    classify_firmware_error,
    error_catalog,
    firmware_error,
)

#: code -> (args the pod sends, the class, the fields it fills)
CODED = {
    "BP0001": ({}, FirmwareError, {}),
    "BP0002": ({"cmd": "i2c_start"}, UnsupportedFeatureError, {"feature": "i2c_start"}),
    "BP0003": ({}, PodBusyError, {}),
    "BP0004": ({}, PodBusyError, {}),
    "BP0005": ({"want": "i2c"}, PodBusyError, {}),
    "BP0006": ({"arg": "name"}, FirmwareError, {}),
    "BP0007": ({"name": "k66"}, WiringError, {"name": "k66"}),
    "BP0008": ({"name": "K 66"}, WiringError, {"name": "K 66"}),
    "BP0009": ({"name": "k66", "rev": 2}, WiringError, {"name": "k66"}),
    "BP0010": ({"max": 8}, WiringError, {"name": ""}),
    "BP0011": ({"bytes": 5000, "max": 4096}, WiringError, {"name": ""}),
    "BP0012": ({}, WiringError, {"name": ""}),
    "BP0013": ({"name": "default"}, WiringError, {"name": "default"}),
    "BP0014": ({"expected": 0}, WiringError, {"name": ""}),
    "BP0015": ({}, WiringError, {"name": ""}),
    "BP0016": ({}, PodBusyError, {}),
    "BP0017": ({"feature": "half_duplex", "min": 57}, UnsupportedFeatureError, {"feature": "half_duplex"}),
    "BP0018": ({"la": 4}, FirmwareError, {}),
    "BP0019": ({"rx": 4, "tx": 5}, FirmwareError, {}),
    "BP0020": ({}, FirmwareError, {}),
    "BP0021": ({"arg": "hold_ms", "max": 5000}, FirmwareError, {}),
    "BP0022": ({"la": 4}, FirmwareError, {}),
    "BP0023": ({"la": 9}, PullDirectionError, {"la": 9, "want": "", "have": ""}),
    "BP0024": ({"la": 7, "want": "up", "have": "down"}, PullDirectionError,
               {"la": 7, "want": "up", "have": "down"}),
    "BP0025": ({"holder": "ci run 7", "purpose": "hil", "left_s": 42}, PodLeasedError,
               {"holder": "ci run 7", "purpose": "hil", "left_s": 42}),
    "BP0026": ({"by": "ward"}, PodLeaseLostError, {"by": "ward"}),
    "BP0027": ({"id": "abc"}, FirmwareError, {}),
    "BP0028": ({"mv": 4120}, FirmwareError, {}),
    "BP0029": ({"mv": 120}, FirmwareError, {}),
    "BP0030": ({"client_api": 0, "min_client_api": 1, "api": 1}, ClientTooOldError,
               {"client_api": 0, "min_client_api": 1, "api": 1}),
}


def test_every_catalog_code_has_a_class():
    """A sync that adds a code fails here until the code is in the table."""
    assert set(error_catalog()["codes"]) == set(errors._BY_CODE) == set(CODED)


@pytest.mark.parametrize("code", sorted(CODED))
def test_coded_reply(code):
    args, cls, fields = CODED[code]
    # The text is one that would classify as something else, so only the code can decide.
    err = firmware_error("pin conflict: LA4 is in use by gpio", cmd="x", code=code, values=args)
    assert type(err) is cls
    assert err.code == code and err.values == args and err.docs_url.endswith("#" + code)
    for name, want in fields.items():
        assert getattr(err, name) == want, name
    assert classify_firmware_error(err) is err   # the client's re-classification keeps it
    assert str(err).startswith(f"x: {code}: ")


def test_la_voltage_keeps_the_sdk_text():
    err = firmware_error("la voltage not set; set la_mv ...", cmd="uart_proxy_start", code="BP0001")
    assert "bp.set_la_voltage(3.3)" in str(err) and "set la_mv in it" in str(err)
    assert str(err).startswith("uart_proxy_start: BP0001: la voltage not set")


def test_client_too_old_names_the_update():
    err = firmware_error("update the BenchPod SDK", cmd="gpio", code="BP0030",
                         values={"client_api": 0, "min_client_api": 1, "api": 1})
    assert "pip install -U embeddedci" in str(err)
    assert not isinstance(err, UnsupportedFeatureError)


def test_unknown_code_falls_back_to_the_text():
    """A code newer than this SDK: the text still classifies it, the code is kept."""
    err = firmware_error("pin conflict: LA4 is in use by gpio", cmd="gpio", code="BP0999")
    assert type(err) is PinConflictError and err.la == 4 and err.code == "BP0999"


# -- text only (firmware without codes): the classes they always had ---------------------------

TEXT_ONLY = [
    ("pin conflict: LA4 is in use by gpio; release it with x", PinConflictError, {"la": 4, "function": "gpio"}),
    ("pull conflict: LA7 pull-down under a UART line", PullConflictError, {"la": 7}),
    ("direction conflict: LA3 is a DUT output", DirectionConflictError, {"la": 3}),
    ("the I2C master needs gateware v55+: update the pod's firmware", UnsupportedFeatureError,
     {"feature": "cmd"}),
    ("trigger timeout: no rising edge on LA2", TriggerTimeout, {"la": 2, "edge": "rising"}),
    ("locked: generate needs the cloud or USB", PodLockedError, {}),
    ("busy: a cloud job holds this pod (octo run 7, 42 s left)", PodLeasedError,
     {"holder": "octo run 7", "left_s": 42}),
    ("busy: stop the DAC or wait for the capture, then save", PodBusyError, {}),
    ("forbidden: generate needs an organization owner or admin", PermissionDeniedError, {}),
    ("unknown cmd", UnsupportedFeatureError, {"feature": "cmd"}),
    ("this BenchPod has no analog front end", UnsupportedFeatureError, {"feature": "analog"}),
    ("la voltage not set; set la_mv (1800 or 3300) in the active wiring profile", FirmwareError, {}),
    ("i2c busy: send i2c_stop first", FirmwareError, {}),
    ("no such profile", FirmwareError, {}),
]


@pytest.mark.parametrize("text,cls,fields", TEXT_ONLY, ids=[t[0][:24] for t in TEXT_ONLY])
def test_text_only_reply(text, cls, fields):
    err = firmware_error(text, cmd="cmd")
    assert type(err) is cls and err.code == ""
    for name, want in fields.items():
        assert getattr(err, name) == want, name
    assert err.firmware_message == text
