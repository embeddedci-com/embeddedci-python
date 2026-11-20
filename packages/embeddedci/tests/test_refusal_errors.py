"""Typed errors for the server's 403 and 409 (writeLeaseBusy, writeJSONError). The pod's own
refusals are classified by their code: test_error_codes.py."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from embeddedci.benchpod.errors import (
    DeviceBusyError,
    PermissionDeniedError,
    PodLeasedError,
    TransportError,
)
from embeddedci.benchpod.transport.cloud import CloudTransport


def test_server_api_403_is_permission_denied(monkeypatch):
    from embeddedci.benchpod.server_api import ServerApi, ServerApiError

    def deny(*_a, **_k):
        raise urllib.error.HTTPError("https://x.test", 403, "Forbidden", {},
                                     io.BytesIO(b'{"error":"this needs an organization owner or admin"}'))

    monkeypatch.setattr("embeddedci._http.urlopen", deny)
    api = ServerApi(api_base="https://x.test", api_key="eci_k")
    with pytest.raises(PermissionDeniedError) as info:
        api.list_devices()
    assert isinstance(info.value, ServerApiError) and info.value.status == 403


def _cloud_http_error(monkeypatch, code, body):
    def fail(*_a, **_k):
        raise urllib.error.HTTPError("https://x.test", code, "err", {}, io.BytesIO(body))

    monkeypatch.setattr("embeddedci._http.urlopen", fail)
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
