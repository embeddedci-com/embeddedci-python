"""The transport recorder: every pod call of a test, timed, into the run's trace.

:class:`Recorder` wraps one transport *instance*: it binds wrappers as instance attributes for
``command``, ``command_sequence``, ``samples``, ``stream_chunks``, ``status``,
``status_without_caps``, ``target_power``, ``dap_start`` and ``uart_proxy_start``, so the
transport's class (and ``isinstance``) is unchanged and no transport class needs to know about it.
A call made from inside another recorded call (``status`` sending ``command``) is recorded once,
as the outer call.

When the run it belongs to has ended, each wrapper is one attribute check and a direct call.
"""

from __future__ import annotations

import functools
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .active import RunRecording

#: method name -> trace ``op``
WRAPPED = {
    "command": "command",
    "command_sequence": "sequence",
    "samples": "samples",
    "stream_chunks": "stream",
    "status": "status",
    "status_without_caps": "status",
    "target_power": "power",
    "dap_start": "raw",
    "uart_proxy_start": "raw",
}

_tls = threading.local()


def _depth() -> int:
    return getattr(_tls, "depth", 0)


class _Nested:
    """Marks a recorded call in progress on this thread, so calls it makes are not recorded again."""

    def __enter__(self) -> None:
        _tls.depth = _depth() + 1

    def __exit__(self, *exc: object) -> None:
        _tls.depth = _depth() - 1


def suppressed() -> _Nested:
    """A block whose pod calls are not recorded (the run's own provenance reads)."""
    return _Nested()


def _cmd_of(req: Any) -> str:
    if isinstance(req, dict):
        return str(req.get("cmd") or "")
    return ""


class RecordingLink:
    """A raw link (CMSIS-DAP or UART proxy) that counts bytes and logs one ``raw`` trace line when
    it closes. Every other attribute is the wrapped link's."""

    def __init__(self, link: Any, recorder: "Recorder", mode: str, cmd: str, req: Dict[str, Any],
                 started: float, t: float) -> None:
        self._link = link
        self._recorder = recorder
        self._mode = mode
        self._cmd = cmd
        self._req = req
        self._started = started
        self._t = t
        self.bytes_in = 0
        self.bytes_out = 0
        self._logged = False

    def read(self, n: int) -> bytes:
        data = self._link.read(n)
        self.bytes_in += len(data)
        return data

    def write(self, data: bytes) -> int:
        n = self._link.write(data)
        self.bytes_out += int(n if isinstance(n, int) else len(data))
        return n

    def close(self) -> None:
        try:
            self._link.close()
        finally:
            self._log()

    def _log(self) -> None:
        if self._logged:
            return
        self._logged = True
        err = getattr(self._link, "error", None)
        ms = (time.perf_counter() - self._started) * 1000.0
        self._recorder.run.record(
            t=self._t, op="raw", cmd=self._cmd, req=self._req, ms=ms, ok=err is None,
            error=str(err) if err is not None else "",
            extra={"mode": self._mode, "bytes_in": self.bytes_in, "bytes_out": self.bytes_out},
            recorder=self._recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._link, name)

    def __enter__(self) -> "RecordingLink":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Recorder:
    """Records one transport instance's calls into a :class:`~.active.RunRecording`."""

    def __init__(self, run: "RunRecording", transport: Any) -> None:
        self.run = run
        self.transport = transport
        #: The last status report seen (for the run's ``inputs.pod``).
        self.last_status: Optional[Dict[str, Any]] = None
        self._busy_seen = int(getattr(transport, "busy_retries", 0) or 0)
        self._reconnects_seen = int(getattr(transport, "reconnects", 0) or 0)
        self.originals: Dict[str, Callable[..., Any]] = {}
        #: Names whose original was an instance attribute (put back on uninstall).
        self._own: Dict[str, Callable[..., Any]] = {}

    # -- install -------------------------------------------------------------------------------

    def install(self) -> "Recorder":
        for name, op in WRAPPED.items():
            fn = getattr(self.transport, name, None)
            if fn is None or not callable(fn):
                continue
            self.originals[name] = fn
            if name in getattr(self.transport, "__dict__", {}):
                self._own[name] = fn
            if name == "stream_chunks":
                wrapper = self._wrap_stream(fn)
            elif op == "raw":
                wrapper = self._wrap_raw(name, fn)
            else:
                wrapper = self._wrap_call(name, op, fn)
            setattr(self.transport, name, wrapper)
        self.transport._embeddedci_recorder = self
        return self

    def uninstall(self) -> None:
        for name in self.originals:
            if name in self._own:
                setattr(self.transport, name, self._own[name])
                continue
            try:
                delattr(self.transport, name)
            except AttributeError:
                pass
        self.originals.clear()
        self._own.clear()
        if getattr(self.transport, "_embeddedci_recorder", None) is self:
            try:
                del self.transport._embeddedci_recorder
            except AttributeError:
                pass

    # -- counters ------------------------------------------------------------------------------

    def _counters(self) -> None:
        """Move the transport's busy-retry and reconnect counters into the run's health."""
        busy = int(getattr(self.transport, "busy_retries", 0) or 0)
        if busy > self._busy_seen:
            self.run.add_health("busy_retries", busy - self._busy_seen)
            self._busy_seen = busy
        rec = int(getattr(self.transport, "reconnects", 0) or 0)
        if rec > self._reconnects_seen:
            self.run.add_health("reconnects", rec - self._reconnects_seen)
            self._reconnects_seen = rec

    # -- wrappers ------------------------------------------------------------------------------

    @staticmethod
    def _request(name: str, args: tuple, kwargs: dict) -> Any:
        if name in ("command", "samples", "stream_chunks"):
            return args[0] if args else kwargs.get("req")
        if name == "command_sequence":
            return list(args[0] if args else kwargs.get("reqs") or [])
        if name == "status":
            return {"cmd": "status"}
        if name == "status_without_caps":
            return {"cmd": "status", "caps": False}
        if name == "target_power":
            keys = ("efuse", "on", "delay_ms")
            req = dict(zip(keys, args))
            req.update({k: v for k, v in kwargs.items() if k in keys})
            return {"cmd": "target_power", **req}
        return {}

    def _wrap_call(self, name: str, op: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        run = self.run
        rec = self

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not run.active or _depth():
                return fn(*args, **kwargs)
            req = rec._request(name, args, kwargs)
            if name == "command_sequence":
                cmd = _cmd_of(req[0]) if req else ""
            else:
                cmd = _cmd_of(req)
            t = run.now()
            start = time.perf_counter()
            try:
                with _Nested():
                    result = fn(*args, **kwargs)
            except BaseException as exc:
                ms = (time.perf_counter() - start) * 1000.0
                rec._counters()
                if getattr(exc, "code", "") == "BP0026":
                    run.lease_lost(str(getattr(exc, "by", "") or ""))
                run.record(t=t, op=op, cmd=cmd, req=req, ms=ms, ok=False, error=_error_text(exc),
                           recorder=rec)
                raise
            ms = (time.perf_counter() - start) * 1000.0
            rec._counters()
            extra: Dict[str, Any] = {}
            resp: Any = result
            if op == "samples":
                extra["n"] = len(result) if hasattr(result, "__len__") else 0
            if op == "status" and isinstance(result, dict):
                rec.last_status = result
            if op == "power":
                resp = None
            run.record(t=t, op=op, cmd=cmd, req=req, ms=ms, ok=True, resp=resp, extra=extra,
                       recorder=rec)
            return result

        return wrapper

    def _wrap_stream(self, fn: Callable[..., Iterator[Dict[str, Any]]]) -> Callable[..., Any]:
        run = self.run
        rec = self

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not run.active or _depth():
                return fn(*args, **kwargs)
            return rec._stream(fn, args, kwargs)

        return wrapper

    def _stream(self, fn: Callable[..., Iterator[Dict[str, Any]]], args: tuple,
                kwargs: dict) -> Iterator[Dict[str, Any]]:
        run = self.run
        req = self._request("stream_chunks", args, kwargs)
        cmd = _cmd_of(req)
        t = run.now()
        start = time.perf_counter()
        keep = run.wants_payloads()
        chunks: List[Dict[str, Any]] = []
        n = 0
        count = 0
        ok = False
        error = ""
        try:
            with _Nested():
                it = iter(fn(*args, **kwargs))
            while True:
                with _Nested():
                    try:
                        chunk = next(it)
                    except StopIteration:
                        break
                count += 1
                data = chunk.get("data") if isinstance(chunk, dict) else None
                if isinstance(data, list):
                    n += len(data)
                if keep:
                    chunks.append(chunk)
                yield chunk
            ok = True
        except GeneratorExit:
            ok = True  # the caller stopped reading early: not a pod error
            raise
        except BaseException as exc:
            error = _error_text(exc)
            raise
        finally:
            ms = (time.perf_counter() - start) * 1000.0
            self._counters()
            run.record(t=t, op="stream", cmd=cmd, req=req, ms=ms, ok=ok, error=error,
                       resp=chunks if keep else None, extra={"n": n, "chunks": count}, recorder=self)

    def _wrap_raw(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        run = self.run
        rec = self
        mode = "dap" if name == "dap_start" else "uart"

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not run.active or _depth():
                return fn(*args, **kwargs)
            req = _raw_request(name, fn, args, kwargs)
            t = run.now()
            start = time.perf_counter()
            try:
                with _Nested():
                    link = fn(*args, **kwargs)
            except BaseException as exc:
                ms = (time.perf_counter() - start) * 1000.0
                run.record(t=t, op="raw", cmd=name, req=req, ms=ms, ok=False, error=_error_text(exc),
                           extra={"mode": mode, "bytes_in": 0, "bytes_out": 0}, recorder=rec)
                raise
            return RecordingLink(link, rec, mode, name, req, start, t)

        return wrapper


def _raw_request(name: str, fn: Callable[..., Any], args: tuple, kwargs: dict) -> Dict[str, Any]:
    import inspect

    out: Dict[str, Any] = {"cmd": name}
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return out
    for k, v in bound.arguments.items():
        if v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        elif hasattr(v, "to_request"):
            try:
                out[k] = v.to_request()
            except Exception:  # noqa: BLE001
                out[k] = str(v)
        else:
            out[k] = str(v)
    return out


def _error_text(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    code = getattr(exc, "code", "")
    if code and isinstance(code, str) and code not in text:
        text = f"{code} {text}"
    return text
