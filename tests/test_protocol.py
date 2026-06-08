import json

import pytest

from embeddedci.benchpod import protocol
from embeddedci.benchpod.errors import FirmwareError, TransportError


def test_encode_request_is_one_json_line():
    raw = protocol.encode_request({"cmd": "ping"})
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1
    assert json.loads(raw) == {"cmd": "ping"}


def test_parse_ok_reply():
    r = protocol.parse_reply(b'{"status":"ok","data":"pong"}')
    assert r.status == "ok"
    assert r.data == "pong"
    assert r.more is False


def test_parse_chunked_reply():
    r = protocol.parse_reply(b'{"status":"chunk","data":[1,2,3],"more":true}')
    assert r.status == "chunk"
    assert r.data == [1, 2, 3]
    assert r.more is True


def test_parse_rejects_garbage():
    with pytest.raises(TransportError):
        protocol.parse_reply(b"not json")
    with pytest.raises(TransportError):
        protocol.parse_reply(b"")


def test_raise_for_status_on_error():
    r = protocol.parse_reply(b'{"status":"error","message":"invalid la channel"}')
    with pytest.raises(FirmwareError) as exc:
        protocol.raise_for_status(r, cmd="gpio_set")
    assert "invalid la channel" in str(exc.value)
    assert exc.value.cmd == "gpio_set"


def test_raise_for_status_passes_ok():
    r = protocol.parse_reply(b'{"status":"ok","data":null}')
    assert protocol.raise_for_status(r) is r
