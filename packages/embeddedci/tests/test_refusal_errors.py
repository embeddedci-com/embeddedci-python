"""Typed errors for the pod's policy refusals (locked/busy/forbidden) and the server's 403/409.

The texts are the firmware's (benchpod-firmware stm32h563/src/command_handler.c dispatch_line,
pod_policy.c) and the server's (writeLeaseBusy, writeJSONError)."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from embeddedci.benchpod.errors import (
    DeviceBusyError,
    FirmwareError,
    PermissionDeniedError,
    PinConflictError,
    PodBusyError,
    PodLeasedError,
    PodLockedError,
    PullConflictError,
    TransportError,
    firmware_error,
)
from embeddedci.benchpod.protocol import Reply, raise_for_status
from embeddedci.benchpod.transport.cloud import CloudTransport


@pytest.mark.parametrize("text", [
    "locked: generate needs the cloud or the USB console",
    "sig_policy: change it from the cloud or the USB console",
    "lan_policy: change it from the cloud or the USB console",
    "cloud_ca: change it from the cloud or the USB console",
    "sig_policy: only the USB console can loosen the policy (now required)",
])
def test_locked(text):
    err = firmware_error(text, cmd="x")
    assert type(err) is PodLockedError and isinstance(err, FirmwareError)


def test_leased_carries_holder_and_time_left():
    err = firmware_error("busy: a cloud job holds this pod (octo/repo run 7, 42 s left)", cmd="gpio")
    assert type(err) is PodLeasedError
    assert err.holder == "octo/repo run 7" and err.left_s == 42
    assert isinstance(err, PodBusyError) and isinstance(err, FirmwareError)
    assert isinstance(err, DeviceBusyError)
    assert err.firmware_message.startswith("busy:") and err.cmd == "gpio"


def test_busy_without_a_lease():
    err = firmware_error("busy: a capture or upload is running")
    assert type(err) is PodBusyError


def test_forbidden():
    err = firmware_error("forbidden: generate needs an organization owner or admin", cmd="generate")
    assert type(err) is PermissionDeniedError and isinstance(err, FirmwareError)
    assert err.status is None


def test_raise_for_status_raises_the_typed_error():
    with pytest.raises(PodLockedError):
        raise_for_status(Reply(status="error", message="locked: la needs the cloud or the USB console"),
                         cmd="la")


def test_other_refusals_stay_plain():
    assert type(firmware_error("la voltage not set")) is FirmwareError
    assert type(firmware_error("swd busy: an SPI session is using the engine")) is FirmwareError


def test_server_api_403_is_permission_denied(monkeypatch):
    from embeddedci.benchpod.server_api import ServerApi, ServerApiError

    def deny(*_a, **_k):
        raise urllib.error.HTTPError("https://x.test", 403, "Forbidden", {},
                                     io.BytesIO(b'{"error":"this needs an organization owner or admin"}'))

    monkeypatch.setattr("urllib.request.urlopen", deny)
    api = ServerApi(api_base="https://x.test", api_key="eci_k")
    with pytest.raises(PermissionDeniedError) as info:
        api.list_devices()
    assert isinstance(info.value, ServerApiError) and info.value.status == 403


def _cloud_http_error(monkeypatch, code, body):
    def fail(*_a, **_k):
        raise urllib.error.HTTPError("https://x.test", code, "err", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    return CloudTransport("dev-a", api_base="https://x.test", token="t")


def test_cloud_command_403_is_permission_denied(monkeypatch):
    t = _cloud_http_error(monkeypatch, 403, b'{"error":"this repository is not allowed to drive device dev-a"}')
    with pytest.raises(PermissionDeniedError, match="not allowed to drive") as info:
        t.command({"cmd": "status"})
    assert info.value.status == 403 and isinstance(info.value, TransportError)


def test_cloudflare_403_is_not_a_permission_problem(monkeypatch):
    t = _cloud_http_error(monkeypatch, 403, b"<html>error 1010</html>")
    with pytest.raises(TransportError) as info:
        t.command({"cmd": "status"})
    assert not isinstance(info.value, PermissionDeniedError)


def test_cloud_command_409_is_leased(monkeypatch):
    until = (datetime.now(timezone.utc) + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = json.dumps({"busy": True, "holder": "web UI (ward)", "expires_at": until,
                       "error": "device is in use by web UI (ward)"}).encode()
    t = _cloud_http_error(monkeypatch, 409, body)
    with pytest.raises(PodLeasedError, match="in use by web UI") as info:
        t.command({"cmd": "status"})
    err = info.value
    assert err.holder == "web UI (ward)" and err.expires_at == until and err.status == 409
    assert 85 <= err.left_s <= 90
    assert isinstance(err, TransportError) and isinstance(err, DeviceBusyError)


# -- protocol-level fixes rewritten into SDK calls ----------------------------------------------
# The firmware texts are benchpod-firmware stm32h563/src/la_pins.c (release_hint, the pull and
# gpio-output checks), command_handler.c (require_la_voltage), command_handler_dap.c and
# command_handler_dac.c.

LA_VOLTAGE_FW = "la voltage not set; set it with la_voltage (mv 1800 or 3300) first"


def test_la_voltage_not_set_names_the_sdk_ways_to_set_it():
    err = firmware_error(LA_VOLTAGE_FW, cmd="uart_proxy_start")
    assert type(err) is FirmwareError
    assert str(err) == ("uart_proxy_start: la voltage not set; set the board's I/O voltage first: "
                        "BenchPod(conn, la_voltage=3.3) (1.8 for a 1.8 V target), the "
                        "benchpod_la_voltage fixture in conftest.py, or bp.set_la_voltage(3.3)")
    assert err.firmware_message == LA_VOLTAGE_FW and err.cmd == "uart_proxy_start"
    assert "la voltage not set" in str(err)  # what callers match on


@pytest.mark.parametrize("function, hint, fix", [
    ("gpio", 'release it with {"cmd":"gpio","la":4,"mode":"off"}', "release it with bp.release_gpio(4)"),
    ("uart_tx", "stop the uart proxy first",
     "close the UART session from bp.open_uart() first (session.close(), or leave its with block)"),
    ("swd_clk", "end the SWD session first", "wait for the running flash (its SWD session) to finish"),
    ("i2c_sda", 'stop the sensor emulation first ({"cmd":"sensor_stop"})',
     "stop the sensor emulation first with bp.disable_i2c_sensor()"),
    ("spi_sck", 'stop the SPI session first ({"cmd":"spi_stop"})',
     "close the SPI session from bp.open_spi() first (spi.close(), or leave its with block)"),
    ("gps_tx", 'stop the GPS receiver first ({"cmd":"gps_stop"})',
     "stop the GPS emulation first with bp.disable_gps()"),
    ("step", "wait for the step train to finish", "wait for the step train to finish"),
    ("can_rx", 'stop it with {"cmd":"can_stop"}', 'stop it with {"cmd":"can_stop"}'),  # unknown: as is
])
def test_pin_conflict_names_the_sdk_call(function, hint, fix):
    fw = f"pin conflict: LA4 is in use by {function}; {hint}"
    err = firmware_error(fw, cmd="gpio")
    assert type(err) is PinConflictError and isinstance(err, FirmwareError)
    assert err.la == 4 and err.function == function
    assert str(err) == f"gpio: pin conflict: LA4 is in use by {function}; {fix}"
    assert err.firmware_message == fw


def test_pull_conflict_names_set_pull():
    fw = ("pull conflict: LA7 has its 10k pull-down engaged, which uart_rx can't work with (the line "
          'idles high); disable it with {"cmd":"la","la":7,"pullup":"off"} or use another channel')
    err = firmware_error(fw, cmd="uart_proxy_start")
    assert type(err) is PullConflictError and err.la == 7
    assert str(err).endswith("disable it with bp.set_pull(7, False) or use another channel")
    assert err.firmware_message == fw


@pytest.mark.parametrize("fw, fixed", [
    ('LA4 is not a gpio output (function gpio, gpio input); configure it with '
     '{"cmd":"gpio","la":4,"mode":"output"}', 'configure it with bp.gpio(4, "output")'),
    ('swd busy: an SPI session is using the engine ({"cmd":"spi_stop"})',
     "using the engine (close the SPI session from bp.open_spi() first)"),
    ('control loop not in the running gateware image (switch with {"cmd":"fpga_image","image":0})',
     "(switch with bp.fpga_image(0))"),
])
def test_other_protocol_fixes_name_the_sdk_call(fw, fixed):
    err = firmware_error(fw)
    assert str(err).endswith(fixed) and err.firmware_message == fw


def test_unknown_text_passes_through():
    for text in ("unknown target", 'something odd {"cmd":"mystery"}'):
        err = FirmwareError(text, cmd="x")
        assert str(err) == f"x: {text}" and err.firmware_message == text
