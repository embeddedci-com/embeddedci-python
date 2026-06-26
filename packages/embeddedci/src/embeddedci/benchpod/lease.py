"""Client for the embeddedci device lease — serialize cloud access to a shared BenchPod.

A physical BenchPod can be driven by only one consumer at a time (a CI run, the web UI, another
repo). Before driving a device over the cloud the client takes a short, renewable *lease*; another
run that finds the device busy waits for it to free rather than colliding. The lease is held for the
whole :class:`~embeddedci.benchpod.client.BenchPod` session: acquired on connect, renewed by a
background heartbeat, released on close (or it simply expires if the process dies).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
import warnings
from typing import Callable, Optional, Tuple
from urllib.parse import quote

from .cloud_auth import USER_AGENT
from .errors import CloudAuthError, DeviceBusyError

# Server defaults: lease TTL 120s. Heartbeat at ~TTL/3 keeps it alive through long flashes.
DEFAULT_LEASE_TTL = 120
DEFAULT_LEASE_WAIT = 600.0  # how long to wait for a busy device before giving up
_LEASE_HTTP_TIMEOUT = 15.0


class _LeaseUnsupported(Exception):
    """The server has no lease endpoint (older deploy). The client degrades to running unlocked."""


def _default_run_label() -> str:
    """A human label identifying this run in 'device busy' messages: the GitHub run when in Actions,
    else host:pid."""
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        parts = [run_id]
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        if attempt:
            parts.append(f"a{attempt}")
        job = os.environ.get("GITHUB_JOB")
        if job:
            parts.append(job)
        return "-".join(parts)
    try:
        import socket

        return f"{socket.gethostname()}:{os.getpid()}"
    except Exception:  # pragma: no cover - hostname lookup is best-effort
        return f"pid{os.getpid()}"


class DeviceLease:
    """Holds an exclusive lease on a named cloud BenchPod for the duration of a session."""

    def __init__(
        self,
        *,
        api_base: str,
        token_provider: Callable[[], str],
        device_name: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL,
        run_label: Optional[str] = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        self._device = device_name
        self._ttl = max(15, int(ttl_seconds))
        self._run_label = run_label or _default_run_label()
        self._lease_id = "lease-" + uuid.uuid4().hex
        self._held = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def lease_id(self) -> str:
        return self._lease_id

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, *, wait_timeout: float = DEFAULT_LEASE_WAIT, poll_interval: float = 5.0) -> None:
        """Acquire the lease, waiting (polling) until the device frees or ``wait_timeout`` elapses.

        Raises :class:`DeviceBusyError` if it does not free in time.
        """
        deadline = time.monotonic() + max(0.0, wait_timeout)
        warned = False
        while True:
            try:
                holder, _ = self._post("lease")
            except _LeaseUnsupported:
                warnings.warn(
                    "embeddedci: server does not support device leases; running without a lock "
                    "(update the server to serialize concurrent runs).",
                    stacklevel=2,
                )
                return
            if holder is None:
                self._held = True
                self._start_heartbeat()
                return
            if time.monotonic() >= deadline:
                raise DeviceBusyError(
                    f"BenchPod {self._device!r} is in use by {holder}; waited {wait_timeout:.0f}s "
                    "for it to free. Increase the wait (--benchpod-lease-wait) or stagger your runs."
                )
            if not warned:
                warnings.warn(
                    f"embeddedci: BenchPod {self._device!r} is busy ({holder}); waiting for it to free…",
                    stacklevel=2,
                )
                warned = True
            time.sleep(poll_interval)

    def release(self) -> None:
        """Release the lease and stop the heartbeat. Safe to call more than once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._held:
            try:
                self._post("lease/release")
            except CloudAuthError:
                pass  # best-effort; the lease will expire on its own
            self._held = False

    # -- internals ---------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        interval = max(10.0, self._ttl / 3.0)

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    holder, _ = self._post("lease/renew")
                except CloudAuthError:
                    continue  # transient; try again next tick
                if holder is not None:
                    warnings.warn(
                        f"embeddedci: lost the lease on {self._device!r} (now held by {holder}); "
                        "another consumer may interfere.",
                        stacklevel=2,
                    )
                    self._held = False
                    return

        self._thread = threading.Thread(target=loop, name="benchpod-lease", daemon=True)
        self._thread.start()

    def _post(self, path: str) -> Tuple[Optional[str], Optional[dict]]:
        """POST to /api/cloud/devices/<path>. Returns (None, payload) on success, or
        (holder, None) when the device is busy (HTTP 409). Raises CloudAuthError on other failures."""
        url = f"{self._api_base}/api/cloud/devices/{path}?device={quote(self._device, safe='')}"
        body = json.dumps(
            {"lease_id": self._lease_id, "run_label": self._run_label, "ttl_seconds": self._ttl}
        ).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self._token_provider()}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(request, timeout=_LEASE_HTTP_TIMEOUT) as resp:
                return None, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                holder = "another run"
                try:
                    holder = json.loads(exc.read().decode("utf-8")).get("holder", holder)
                except Exception:
                    pass
                return holder, None
            if exc.code in (404, 405, 501):
                raise _LeaseUnsupported() from exc
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise CloudAuthError(f"device lease {path} failed (HTTP {exc.code}): {detail}") from exc
        except Exception as exc:
            raise CloudAuthError(f"device lease {path} failed: {exc}") from exc
