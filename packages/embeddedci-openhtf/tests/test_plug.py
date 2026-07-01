"""Plug-level tests: construction from an injected transport, attribute proxying,
teardown, and the missing-connection error. No hardware, no OpenHTF executor."""

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.errors import ConnectionConfigError

from embeddedci_openhtf import BenchPodPlug, benchpod_plug
from _fake import FakeTransport


def test_plug_builds_pod_and_proxies():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    assert isinstance(plug.pod, BenchPod)
    # unknown attributes proxy through to the SDK client
    assert plug.status()["fake"] is True
    plug.power_on(1)
    plug.power_off(1)
    assert [c["on"] for c in tx.power_calls] == [True, False]


def test_teardown_closes_connection():
    tx = FakeTransport()
    plug = benchpod_plug(transport=tx)()
    plug.tearDown()
    assert tx.closed is True


def test_internal_attrs_do_not_proxy():
    # dunder / private lookups must not be forwarded to the pod
    plug = benchpod_plug(transport=FakeTransport())()
    with pytest.raises(AttributeError):
        _ = plug._nope


def test_missing_connection_raises(monkeypatch):
    monkeypatch.delenv("BENCHPOD_CONNECTION", raising=False)
    with pytest.raises(ConnectionConfigError):
        BenchPodPlug()  # no bound connection, no config, no env, no transport
