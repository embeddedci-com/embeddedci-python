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


def _http_error(code, body):
    import io
    import urllib.error

    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


def _scripted(monkeypatch, outcomes):
    import io
    import urllib.request

    calls = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(request, timeout=None):
        calls.append(request.get_header("Authorization"))
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return _Resp(b'{"lease_id":"x"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def test_unknown_device_is_an_error_not_an_old_server(monkeypatch):
    from embeddedci.benchpod.errors import ConnectionConfigError

    _scripted(monkeypatch, [_http_error(404, b'{"error":"device not found: dev"}')])
    lease = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="dev")
    with pytest.raises(ConnectionConfigError, match="no cloud BenchPod named 'dev'"):
        lease.acquire(wait_timeout=0)


def test_bare_404_still_means_no_lease_route(monkeypatch):
    _scripted(monkeypatch, [_http_error(404, b"404 page not found")])
    lease = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="dev")
    with pytest.warns(UserWarning, match="does not support device leases"):
        lease.acquire(wait_timeout=0)
    assert not lease.held


def test_busy_error_says_until_when(monkeypatch):
    body = b'{"busy":true,"holder":"user a@b run host:1","expires_at":"2026-09-29T15:00:00Z"}'
    _scripted(monkeypatch, [_http_error(409, body)])
    lease = DeviceLease(api_base="https://x", token_provider=lambda: "t", device_name="dev")
    with pytest.raises(DeviceBusyError, match=r"user a@b run host:1 \(its lease runs until 2026-09-29T15:00:00Z"):
        lease.acquire(wait_timeout=0)


def test_rejected_token_is_renewed_once(monkeypatch):
    tokens = iter(["old", "new"])
    current = {"t": next(tokens)}

    def invalidate():
        current["t"] = next(tokens)
        return True

    calls = _scripted(monkeypatch, [_http_error(401, b'{"error":"invalid session token"}'), None])
    lease = DeviceLease(api_base="https://x", token_provider=lambda: current["t"], device_name="dev",
                        invalidate_token=invalidate)
    lease.acquire(wait_timeout=0)
    assert lease.held and calls == ["Bearer old", "Bearer new"]
    lease._stop.set()
    lease._held = False
