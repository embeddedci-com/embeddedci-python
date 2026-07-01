"""An OpenHTF plug that wraps the EmbeddedCI BenchPod SDK.

This plug is built for driving a BenchPod **directly** — over a TCP socket
(``host[:port]``) or a serial device path — with no EmbeddedCI cloud account or
web UI involved. (A direct TCP/serial connection never takes a cloud lease, so
nothing here talks to embeddedci.com.)

Two ways to point it at a pod:

* **Inline (most direct).** Bind the connection in the test file with
  :func:`benchpod_plug`::

      import openhtf as htf
      from embeddedci_openhtf import benchpod_plug

      @htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
      def power_up(test, bench):
          bench.power_on()                  # methods proxy to the BenchPod SDK
          bench.pod.flash(...)              # or reach the full client via .pod

* **Via OpenHTF config.** Use :class:`BenchPodPlug` unbound and supply the
  connection through OpenHTF's ``conf`` (a YAML file, ``--config-value``, or
  :func:`openhtf.conf.load`), falling back to the ``BENCHPOD_CONNECTION`` env
  var::

      htf.conf.load(benchpod_connection="/dev/ttyACM0")

      @htf.plug(bench=BenchPodPlug)
      def power_up(test, bench):
          bench.power_on()

**Connection lifecycle.** By default a fresh connection is opened on each test
execution and closed in :meth:`tearDown`, so OpenHTF owns the lifecycle. On a
station that cycles many DUTs back-to-back, pass ``persistent=True`` to
:func:`benchpod_plug` to keep one connection open across executions (re-checked
with a ping each run and reconnected if it dropped); close it explicitly with
:func:`close_persistent_benchpods` (also run automatically at process exit).

Unknown attribute access proxies to the underlying
:class:`~embeddedci.benchpod.BenchPod`, so ``bench.flash(...)`` /
``bench.power_on()`` / ``bench.capture_uart(...)`` work directly; ``bench.pod``
is the full client when you need it.
"""

from __future__ import annotations

import atexit
import os
import threading
from typing import Any, Dict, Optional

import openhtf as htf
from openhtf.plugs import BasePlug

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.connection import ENV_VAR
from embeddedci.benchpod.errors import ConnectionConfigError

# Mirror the SDK's connection sources as OpenHTF config keys, so a station can be
# configured from a YAML file / --config-value instead of code.
htf.conf.declare(
    "benchpod_connection",
    description=(
        "BenchPod connection for direct (non-cloud) access: host[:port], a "
        f"serial device path, or 'serial'. Falls back to the {ENV_VAR} env var."
    ),
    default_value=None,
)
htf.conf.declare(
    "benchpod_timeout",
    description="BenchPod transport timeout in seconds (default 30).",
    default_value=30.0,
)

# Process-wide pool of persistent connections, keyed by the bound plug class (one
# per benchpod_plug(persistent=True) call). Survives across test executions.
_PERSISTENT_POOL: Dict[type, BenchPod] = {}
_PERSISTENT_LOCK = threading.Lock()


def close_persistent_benchpods() -> None:
    """Close every persistent BenchPod connection and empty the pool.

    Call at the end of a station run (or rely on the automatic ``atexit`` hook).
    Idempotent — a plug with ``persistent=True`` reopens on its next execution.
    """
    with _PERSISTENT_LOCK:
        pods = list(_PERSISTENT_POOL.values())
        _PERSISTENT_POOL.clear()
    for pod in pods:
        try:
            pod.close()
        except Exception:
            pass


atexit.register(close_persistent_benchpods)


def _acquire_persistent(cls: type, conn: Optional[str],
                        pod_kwargs: Dict[str, Any], health_check: bool) -> BenchPod:
    """Return the pooled connection for ``cls``, opening (or reopening, if a
    health-check ping fails) it as needed."""
    with _PERSISTENT_LOCK:
        pod = _PERSISTENT_POOL.get(cls)
        if pod is not None and health_check:
            try:
                pod.ping()  # cheap liveness check; reconnect if the link died
            except Exception:
                try:
                    pod.close()
                except Exception:
                    pass
                pod = None
                _PERSISTENT_POOL.pop(cls, None)
        if pod is None:
            pod = BenchPod(conn, **pod_kwargs)
            _PERSISTENT_POOL[cls] = pod
        return pod


class BenchPodPlug(BasePlug):
    """OpenHTF plug wrapping a directly-connected :class:`BenchPod`.

    Use unbound with OpenHTF config / ``BENCHPOD_CONNECTION``, or bind a
    connection with :func:`benchpod_plug`. Subclasses may override the class
    attributes below (that is what :func:`benchpod_plug` produces); leave
    :data:`connection` / :data:`pod_kwargs` at their defaults to resolve from
    config then env.
    """

    #: Connection override set by :func:`benchpod_plug`. ``None`` => use config/env.
    connection: Optional[str] = None
    #: Extra keyword args forwarded to ``BenchPod(...)`` (e.g. ``transport=`` for
    #: tests). Set by :func:`benchpod_plug`.
    pod_kwargs: Dict[str, Any] = {}
    #: Keep one connection open across test executions (station mode).
    persistent: bool = False
    #: In persistent mode, ping a pooled connection before reuse and reconnect
    #: if it has dropped.
    health_check: bool = True

    @htf.conf.inject_positional_args
    def __init__(self, benchpod_connection: Optional[str] = None,
                 benchpod_timeout: float = 30.0) -> None:
        super().__init__()
        cls = type(self)
        pod_kwargs = dict(cls.pod_kwargs)
        conn = self.connection or benchpod_connection or os.environ.get(ENV_VAR)
        if not conn and "transport" not in pod_kwargs:
            raise ConnectionConfigError(
                "no BenchPod connection: bind one with benchpod_plug('host:port'), "
                "set the OpenHTF 'benchpod_connection' config, or export "
                f"{ENV_VAR}=<host:port|/dev/tty...>"
            )
        pod_kwargs.setdefault("timeout", benchpod_timeout)
        if cls.persistent:
            #: The connected SDK client (shared, kept open across executions).
            self.pod: BenchPod = _acquire_persistent(
                cls, conn, pod_kwargs, cls.health_check)
        else:
            # Direct TCP/serial connections never lease.
            self.pod = BenchPod(conn, **pod_kwargs)
        self.logger.info("BenchPod %s (%s)",
                         "reusing connection" if cls.persistent else "connected",
                         conn or "injected transport")

    def tearDown(self) -> None:
        """Close the connection — unless it is persistent, in which case it is
        left open for the next test execution."""
        if type(self).persistent:
            return
        try:
            self.pod.close()
        except Exception:  # teardown must not mask a phase failure
            self.logger.exception("error closing BenchPod")

    def __getattr__(self, name: str) -> Any:
        # Proxy unknown attributes (flash/power_on/capture_uart/...) to the SDK
        # client. Only consulted when normal lookup fails, so it never shadows
        # plug internals (logger, tearDown, pod). Guarded so attribute probes
        # before pod is set, and dunder lookups, don't recurse or leak.
        if name.startswith("_"):
            raise AttributeError(name)
        pod = self.__dict__.get("pod")
        if pod is None:
            raise AttributeError(name)
        return getattr(pod, name)


def benchpod_plug(connection: Optional[str] = None, *, persistent: bool = False,
                  health_check: bool = True, **pod_kwargs: Any) -> type:
    """Return a :class:`BenchPodPlug` subclass bound to a specific connection.

    Ergonomic for direct bench use — put the pod's address right in the test::

        @htf.plug(bench=benchpod_plug("192.168.1.50:8080"))
        def phase(test, bench): ...

        @htf.plug(bench=benchpod_plug("/dev/ttyACM0"))
        def phase(test, bench): ...

    Set ``persistent=True`` to keep one connection open across test executions
    (reuse the *same* returned class for every ``Test.execute()`` so they share
    it); see :func:`close_persistent_benchpods`. Extra keyword args are forwarded
    to ``BenchPod(...)`` (``timeout=``, or ``transport=`` to inject a fake
    backend in tests).
    """
    return type(
        "BoundBenchPodPlug",
        (BenchPodPlug,),
        {
            "connection": connection,
            "pod_kwargs": pod_kwargs,
            "persistent": persistent,
            "health_check": health_check,
        },
    )
