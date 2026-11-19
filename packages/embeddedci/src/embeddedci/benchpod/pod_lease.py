"""The pod's own lease, on the LAN and the USB network (feature ``lease``).

A pod with the ``lease`` feature keeps one lease in RAM: an id, a holder label, a purpose and a
deadline. While it is held, only connections that sent ``lease_acquire`` with that id (its
*owners*) may drive the pod; everyone else gets light reads and ``BP0025``. Every command of the
TCP transport is its own connection, so a held lease is joined at the start of each one
(``TcpTransport._dial``); a heartbeat renews it while the session is idle.

``BENCHPOD_LEASE`` (set by ``benchpod ci`` for the processes it starts) joins the runner's lease
instead: the same id, never renewed nor released by this process.

Several :class:`~embeddedci.benchpod.BenchPod` objects of one process on the same pod share one
lease (a test opening its own pod next to the session's), so they never wait for each other.
"""

from __future__ import annotations

import getpass
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Dict, Mapping, Optional

from .errors import FirmwareError, PodLeasedError, PodLeaseLostError, UnsupportedFeatureError
from .protocol import encode_request, parse_reply, raise_for_status

_log = logging.getLogger("embeddedci.benchpod")

#: The runner's lease id: join it, never renew or release it.
LEASE_ENV = "BENCHPOD_LEASE"
DEFAULT_POD_LEASE_TTL = 120
#: Renew this often (TTL 120 s: three chances before it runs out).
HEARTBEAT_S = 40.0
#: How often a busy pod is asked again.
POLL_S = 2.0
HOLDER_MAX = 40
PURPOSE_MAX = 64

_registry: Dict[str, "PodLease"] = {}
_registry_lock = threading.Lock()


def default_holder() -> str:
    """``SDK: <user>`` (else ``SDK: <host>``), at most 40 characters."""
    who = ""
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name on this system
        who = ""
    if not who:
        try:
            who = socket.gethostname()
        except OSError:
            who = ""
    return f"SDK: {who or 'unknown'}"[:HOLDER_MAX]


def default_purpose(explicit: Optional[str] = None) -> str:
    """``explicit``, else the suite of the test run being recorded, else ``sdk``."""
    if explicit:
        return explicit[:PURPOSE_MAX]
    try:
        from .runs.active import active_run

        run = active_run()
        suite = getattr(run, "suite", "") if run is not None else ""
    except Exception:  # noqa: BLE001
        suite = ""
    return (suite or "sdk")[:PURPOSE_MAX]


def _lease_view(data: Any) -> Dict[str, Any]:
    if isinstance(data, Mapping):
        inner = data.get("lease")
        if isinstance(inner, Mapping):
            return dict(inner)
        return dict(data)
    return {}


def _busy_text(holder: str, purpose: str, left_s: Optional[int]) -> str:
    left = f", {left_s} s left" if left_s is not None else ""
    return f"{holder or 'someone'} holds this pod ({purpose or 'no purpose given'}{left})"


class PodLease:
    """One lease on one pod, shared by every transport of this process that talks to it."""

    def __init__(self, key: str, *, lease_id: str, holder: str, purpose: str, ttl_s: int,
                 joined: bool) -> None:
        self.key = key
        self.lease_id = lease_id
        self.holder = holder[:HOLDER_MAX]
        self.purpose = purpose[:PURPOSE_MAX]
        self.ttl_s = max(10, min(1800, int(ttl_s)))
        #: Joined the runner's lease (``BENCHPOD_LEASE``): no heartbeat, no release.
        self.joined = joined
        #: Stopped from the web app or the cloud (BP0026): connections no longer join it.
        self.lost = False
        self.lost_by = ""
        self.held = False
        self.left_s: Optional[int] = None
        self._users = 0
        self._transport: Any = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self.held and not self.lost

    # -- the per-connection join ---------------------------------------------------------------

    def _request(self) -> Dict[str, Any]:
        return {"cmd": "lease_acquire", "id": self.lease_id, "holder": self.holder,
                "purpose": self.purpose, "ttl_s": self.ttl_s}

    def join(self, transport: Any, sock: Any, timeout: Optional[float]) -> None:
        """Send ``lease_acquire`` on a new connection so it is an owner. Raises
        :class:`PodLeasedError` when someone else holds the pod now and
        :class:`PodLeaseLostError` (once) when the lease was stopped from the web app."""
        if not self.active:
            return
        sock.sendall(encode_request(self._request()))
        line = transport._recv_line_exact(sock)
        self._check(parse_reply(line))

    def _check(self, reply: Any) -> Dict[str, Any]:
        try:
            raise_for_status(reply, cmd="lease_acquire")
        except PodLeaseLostError as exc:
            self._mark_lost(exc.by)
            raise
        view = _lease_view(reply.data)
        left = view.get("left_s")
        if isinstance(left, int):
            self.left_s = left
        return view

    def _mark_lost(self, by: str) -> None:
        self.lost = True
        self.lost_by = by
        self._stop.set()
        _log.warning("the pod lease %s was stopped by %s; this session goes on without a lease",
                     self.lease_id, by or "someone")

    # -- acquire, heartbeat, release -------------------------------------------------------------

    def _command(self, req: Dict[str, Any]) -> Any:
        """One ``lease_*`` command on its own connection (not joining the lease first)."""
        transport = self._transport
        sock = transport._dial(transport.timeout, join_lease=False)
        try:
            sock.sendall(encode_request(req))
            reply = parse_reply(transport._recv_line_exact(sock))
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return reply

    def acquire(self, transport: Any, *, wait: float) -> None:
        """Take (or join) the lease, waiting up to ``wait`` seconds while someone else holds it."""
        self._transport = transport
        deadline = time.monotonic() + max(0.0, wait)
        warned = False
        while True:
            try:
                self._check(self._command(self._request()))
                break
            except PodLeasedError as exc:
                if time.monotonic() + POLL_S > deadline:
                    raise PodLeasedError(
                        f"busy: {_busy_text(exc.holder, exc.purpose, exc.left_s)}; waited "
                        f"{wait:.0f} s for it to free. Wait longer (lease_wait, or "
                        "--benchpod-lease-wait under pytest) or try again when it is done.",
                        cmd="lease_acquire", holder=exc.holder, purpose=exc.purpose,
                        left_s=exc.left_s) from None
                if not warned:
                    _log.warning("embeddedci: the pod is busy (%s); waiting for it to free",
                                 _busy_text(exc.holder, exc.purpose, exc.left_s))
                    warned = True
                time.sleep(POLL_S)
        self.held = True
        if not self.joined:
            self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        def loop() -> None:
            from .runs.recorder import suppressed

            while not self._stop.wait(HEARTBEAT_S):
                try:
                    with suppressed():
                        self._renew()
                except Exception as exc:  # noqa: BLE001 - transient; try again next tick
                    _log.debug("pod lease renew failed: %s", exc)
                if self.lost:
                    return

        self._thread = threading.Thread(target=loop, name="benchpod-pod-lease", daemon=True)
        self._thread.start()

    def _renew(self) -> None:
        reply = self._command({"cmd": "lease_renew", "id": self.lease_id, "ttl_s": self.ttl_s})
        try:
            self._check(reply)
        except PodLeaseLostError:
            return
        except PodLeasedError as exc:
            # It ran out and someone else took it: nothing to renew any more.
            self._mark_lost(exc.holder)
        except FirmwareError as exc:
            if getattr(exc, "code", "") == "BP0027":  # nothing held (ran out, pod restarted)
                self._check(self._command(self._request()))
            else:
                raise

    def release(self) -> None:
        """Stop the heartbeat and free the lease (not a joined one). Never raises."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.held and not self.joined and not self.lost and self._transport is not None:
            try:
                self._command({"cmd": "lease_release", "id": self.lease_id})
            except Exception as exc:  # noqa: BLE001 - best effort; it runs out on its own
                _log.debug("pod lease release failed: %s", exc)
        self.held = False


def lease_key(transport: Any) -> str:
    return str(getattr(transport, "addr", "") or id(transport))


def open_pod_lease(transport: Any, *, supported: bool, purpose: Optional[str], ttl_s: int,
                   wait: float) -> Optional[PodLease]:
    """The lease a new BenchPod session on ``transport`` holds, or None.

    Without the ``lease`` feature: None (and one warning when ``BENCHPOD_LEASE`` asked to join
    one). A lease this process already holds on the same pod is shared; else
    ``BENCHPOD_LEASE``'s id is joined, else a new ``L-<uuid4>`` lease is taken."""
    env_id = os.environ.get(LEASE_ENV, "").strip()
    if not supported:
        if env_id:
            _log.warning("%s is set but this pod has no lease support (feature 'lease'): "
                         "running without the lease", LEASE_ENV)
        return None
    key = lease_key(transport)
    with _registry_lock:
        lease = _registry.get(key)
        if lease is not None and lease.active:
            lease._users += 1
            transport.pod_lease = lease
            return lease
        lease = PodLease(key, lease_id=env_id or f"L-{uuid.uuid4()}", holder=default_holder(),
                         purpose=default_purpose(purpose), ttl_s=ttl_s, joined=bool(env_id))
        try:
            lease.acquire(transport, wait=wait)
        except UnsupportedFeatureError:
            # The table said yes, the pod does not know the command (another firmware build).
            if env_id:
                _log.warning("%s is set but this pod does not take a lease: running without it",
                             LEASE_ENV)
            return None
        lease._users = 1
        _registry[key] = lease
        transport.pod_lease = lease
        return lease


def close_pod_lease(transport: Any, lease: Optional[PodLease]) -> None:
    """End one BenchPod session's use of ``lease``; the last one releases it."""
    if lease is None:
        return
    if getattr(transport, "pod_lease", None) is lease:
        transport.pod_lease = None
    with _registry_lock:
        lease._users -= 1
        if lease._users > 0:
            return
        if _registry.get(lease.key) is lease:
            del _registry[lease.key]
    lease.release()
