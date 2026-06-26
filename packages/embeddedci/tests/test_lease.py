"""Tests for the device lease client (embeddedci.benchpod.lease)."""

from __future__ import annotations

import pytest

from embeddedci.benchpod.lease import DeviceLease
from embeddedci.benchpod.errors import DeviceBusyError


def _lease(posts):
    """A DeviceLease whose _post is driven by a list of canned results (holder, payload)."""
    lease = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="dev", ttl_seconds=120)
    calls = []

    def fake_post(path):
        calls.append(path)
        # renew/release always succeed; acquire follows the scripted sequence.
        if path != "lease":
            return None, {}
        return posts.pop(0) if posts else (None, {})

    lease._post = fake_post  # type: ignore[assignment]
    lease._calls = calls  # type: ignore[attr-defined]
    return lease


def test_acquire_succeeds_when_free():
    lease = _lease([(None, {"lease_id": "x"})])
    lease.acquire(wait_timeout=1.0, poll_interval=0.01)
    assert lease.held
    lease.release()
    assert not lease.held
    assert "lease/release" in lease._calls  # type: ignore[attr-defined]


def test_acquire_waits_then_succeeds():
    # Busy twice, then free — acquire should poll and eventually win.
    lease = _lease([("runA", None), ("runA", None), (None, {})])
    lease.acquire(wait_timeout=5.0, poll_interval=0.01)
    assert lease.held
    lease.release()


def test_acquire_times_out_when_busy():
    lease = _lease([("runA", None)] * 50)
    with pytest.raises(DeviceBusyError) as exc:
        lease.acquire(wait_timeout=0.05, poll_interval=0.01)
    assert "runA" in str(exc.value)
    assert not lease.held


def test_acquire_degrades_when_unsupported():
    from embeddedci.benchpod.lease import _LeaseUnsupported

    lease = DeviceLease(api_base="x", token_provider=lambda: "t", device_name="d")

    def fake_post(_path):
        raise _LeaseUnsupported()

    lease._post = fake_post  # type: ignore[assignment]
    # Should not raise — runs unlocked against an older server.
    lease.acquire(wait_timeout=1.0, poll_interval=0.01)
    assert not lease.held


def test_lease_id_is_unique():
    a = DeviceLease(api_base="x", token_provider=lambda: "t", device_name="d")
    b = DeviceLease(api_base="x", token_provider=lambda: "t", device_name="d")
    assert a.lease_id != b.lease_id
    assert a.lease_id.startswith("lease-")
