"""Client for the ai-vision-2 board's HTTP API (``/api/v1``).

Standard library only (``urllib``). One method per endpoint; the board does the work, so a
``press`` (find + tap + verify) is one request.

    from embeddedci.aivision import AIVision

    with AIVision("ai-vision-2-5dcec6.local") as v:
        v.wait_for("Home", timeout=20)
        v.press("Radio", verify={"text": "FM"})
        assert v.screen_health().ok
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from . import errors
from .results import (Box, Calibration, CheckResult, Comparison, Element, Health, Identification, Image,
                      Lease, OCRResult, PressResult, Snapshot, WaitResult, elements)

#: Environment variable with the default connection (host, host:port, URL or ``discover``).
ENV_CONNECTION = "AIVISION_CONNECTION"
#: Environment variable with the API token.
ENV_TOKEN = "AIVISION_TOKEN"

#: Longest single ``/wait`` request. Longer waits are split into several requests, so a wait
#: survives proxies that cut long requests (Cloudflare drops them at about 100 s).
MAX_WAIT_REQUEST_S = 60.0

#: Longest ``verify_timeout`` a press, tap or swipe may ask for: the board answers within one
#: request, which it caps at 90 s (``max_wait_ms`` in its capabilities).
MAX_VERIFY_S = 90.0

Region = Union[Sequence[float], Mapping[str, Any], Box]
Point = Union[Tuple[float, float], Sequence[float]]
PathLike = Union[str, Path]


def resolve_connection(connection: Optional[str]) -> str:
    """Turn a connection string into the board's base URL (``http://host[:port]``).

    Accepts ``host``, ``host:port``, ``http(s)://host[:port]``, or ``discover`` (exactly one board
    on the LAN, via mDNS). ``None`` reads ``AIVISION_CONNECTION``.
    """
    conn = (connection or os.environ.get(ENV_CONNECTION, "")).strip()
    if not conn:
        raise errors.TransportError(
            f"no AI Vision board given: pass a host, set {ENV_CONNECTION}, or use 'discover'")
    if conn in ("discover", "mdns", "auto"):
        from .discovery import discover

        boards = discover()
        if not boards:
            raise errors.TransportError("no AI Vision board answered on the LAN (mDNS _aivision._tcp)")
        if len(boards) > 1:
            names = ", ".join(b.hostname for b in boards)
            raise errors.TransportError(f"several AI Vision boards on the LAN ({names}); pass one by name")
        conn = boards[0].addr
    if "://" not in conn:
        conn = "http://" + conn
    u = urllib.parse.urlsplit(conn)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise errors.TransportError(f"not an AI Vision address: {connection!r}")
    return f"{u.scheme}://{u.netloc}"


def _region(r: Optional[Region]) -> Any:
    if r is None:
        return None
    if isinstance(r, Box):
        return {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "space": r.space}
    if isinstance(r, Mapping):
        return dict(r)
    vals = list(r)
    if len(vals) != 4:
        raise ValueError("a region is (x, y, w, h)")
    return vals


def _verify_ms(verify_timeout: Optional[float]) -> Optional[int]:
    if not verify_timeout:
        return None
    if verify_timeout > MAX_VERIFY_S:
        raise ValueError(f"verify_timeout at most {MAX_VERIFY_S:g} s: the board answers within one request; "
                         "for a longer wait, press without verify and call wait_for")
    return int(round(verify_timeout * 1000))


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop arguments left at None, so the board applies its own defaults."""
    return {k: v for k, v in d.items() if v is not None}


def _query(text: Optional[str] = None, *, label: Optional[str] = None, template: Optional[str] = None,
           match: Optional[str] = None, region: Optional[Region] = None, near: Any = None,
           classes: Optional[Sequence[str]] = None, zoom: Optional[float] = None,
           index: Optional[int] = None, case_sensitive: Optional[bool] = None,
           min_score: Optional[float] = None, fresh: Optional[bool] = None) -> Dict[str, Any]:
    if not (text or label or template):
        raise ValueError("give text=, label= or template=")
    if isinstance(near, (tuple, list)):
        near = list(near)
    elif isinstance(near, str):
        near = {"text": near}
    return _clean({"text": text, "label": label, "template": template, "match": match,
                   "region": _region(region), "near": near, "classes": list(classes) if classes else None,
                   "zoom": zoom, "index": index, "case_sensitive": case_sensitive,
                   "min_score": min_score, "fresh": fresh})


def _condition(text: Optional[str], condition: Optional[Mapping[str, Any]], kw: Dict[str, Any],
               absent: bool = False) -> Dict[str, Any]:
    if condition is not None:
        if text is not None or any(v is not None for v in kw.values()):
            raise ValueError("give either condition= or find arguments, not both")
        return dict(condition)
    q = _query(text, **kw)
    if absent:
        q["absent"] = True
    return {"find": q}


class AIVision:
    """A connection to one ai-vision-2 board.

    ``connection``: host, ``host:port``, URL, or ``"discover"``; default ``AIVISION_CONNECTION``.
    ``token``: the board's API token; default ``AIVISION_TOKEN`` (open boards need none).
    ``lease``: take the board's lease under this name on open (renewed in the background,
    released on close), so nobody else drives the camera or the actuator meanwhile.
    """

    def __init__(self, connection: Optional[str] = None, *, token: Optional[str] = None,
                 timeout: float = 30.0, lease: Optional[str] = None, lease_ttl_s: int = 300) -> None:
        self.url = resolve_connection(connection)
        self.token = token if token is not None else os.environ.get(ENV_TOKEN) or None
        self.timeout = timeout
        self._lease: Optional[Lease] = None
        self._lease_stop = threading.Event()
        self._lease_thread: Optional[threading.Thread] = None
        if lease:
            self.acquire_lease(lease, ttl_s=lease_ttl_s)

    # ---- plumbing ----

    def __enter__(self) -> "AIVision":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Release the lease if this client holds one."""
        if self._lease is not None:
            try:
                self.release_lease()
            except errors.AIVisionError:
                pass

    def __repr__(self) -> str:
        return f"AIVision({self.url!r})"

    def _open(self, method: str, path: str, *, body: Optional[bytes] = None, content_type: str = "",
              params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None):
        url = self.url + "/api/v1" + path
        if params:
            q = {k: ("true" if v is True else "false" if v is False else v)
                 for k, v in params.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, data=body, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if self._lease is not None:
            req.add_header("X-Lease-Id", self._lease.id)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw or b"null")
            except ValueError:
                parsed = None
            raise errors.from_answer(e.code, parsed, raw.decode("utf-8", "replace")[:200]) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise errors.TransportError(f"{method} {url}: {reason}") from None

    def _json(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None, *,
              params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        with self._open(method, path, body=body, content_type="application/json" if body else "",
                        params=params, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def _image(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None, *,
               params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None) -> Image:
        body = json.dumps(payload).encode() if payload is not None else None
        with self._open(method, path, body=body, content_type="application/json" if body else "",
                        params=params, timeout=timeout) as resp:
            data = resp.read()
            h = {k: v for k, v in resp.headers.items()}
        fid = h.get("X-Frame-Id")
        return Image(data, h.get("Content-Type", ""), int(fid) if fid and fid.isdigit() else None,
                     h.get("X-Timestamp", ""), h.get("X-Space", ""), h)

    def download(self, path: str, dest: PathLike) -> Path:
        """Save a board file (a ``diff_image``, ``evidence_zip`` or snapshot URL) to ``dest``."""
        if path.startswith("/api/v1"):
            path = path[len("/api/v1"):]
        dest = Path(dest)
        with self._open("GET", path, timeout=max(self.timeout, 120)) as resp, dest.open("wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        return dest

    # ---- device ----

    def status(self) -> Dict[str, Any]:
        """Version, camera, Hailo, calibration, lease, actuators, storage."""
        return self._json("GET", "/status")

    def capabilities(self) -> List[str]:
        """Feature names the board supports (``ocr``, ``find.text``, ``actuator.tap``, ...)."""
        return list(self._json("GET", "/capabilities").get("capabilities") or [])

    def has(self, capability: str) -> bool:
        return capability in self.capabilities()

    # ---- lease ----

    @property
    def lease(self) -> Optional[Lease]:
        """The lease this client holds, or None."""
        return self._lease

    def acquire_lease(self, who: str, *, ttl_s: int = 300, renew: bool = True) -> Lease:
        """Take the board's lease (``BusyError`` when someone else holds it).

        With ``renew`` a background thread renews it at a third of ``ttl_s`` until
        :meth:`release_lease` or :meth:`close`.
        """
        d = self._json("POST", "/lease", {"who": who, "ttl_s": ttl_s})
        self._lease = Lease(str(d["lease_id"]), str(d.get("who", who)), str(d.get("expires", "")),
                            int(d.get("ttl_s", ttl_s)))
        if renew and self._lease_thread is None:
            self._lease_stop.clear()
            t = threading.Thread(target=self._renew_loop, args=(who, ttl_s), daemon=True,
                                 name="aivision-lease")
            self._lease_thread = t
            t.start()
        return self._lease

    def _renew_loop(self, who: str, ttl_s: int) -> None:
        while not self._lease_stop.wait(max(ttl_s / 3.0, 1.0)):
            if self._lease is None:
                return
            try:
                d = self._json("POST", "/lease", {"who": who, "ttl_s": ttl_s})
                self._lease = Lease(str(d["lease_id"]), who, str(d.get("expires", "")), ttl_s)
            except errors.AIVisionError:
                pass  # the next driving call reports the problem

    def release_lease(self) -> None:
        """Release the lease this client holds (no-op without one)."""
        self._lease_stop.set()
        t, self._lease_thread = self._lease_thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)
        lease, self._lease = self._lease, None
        if lease is not None:
            try:
                self._json("DELETE", f"/lease/{lease.id}")
            except errors.NotFoundError:
                pass  # expired already

    # ---- camera and frames ----

    def camera(self) -> Dict[str, Any]:
        """Exposure, gain, lock, rotation, sensor and live sizes."""
        return self._json("GET", "/camera")

    def set_camera(self, *, lock: Optional[bool] = None, exposure_ms: Optional[float] = None,
                   gain: Optional[int] = None, rotation: Optional[int] = None) -> Dict[str, Any]:
        """Lock auto exposure / white balance (do this after calibrating), or set them by hand."""
        return self._json("PUT", "/camera", _clean({"lock": lock, "exposure_ms": exposure_ms,
                                                    "gain": gain, "rotation": rotation}))

    def frame(self, *, space: Optional[str] = None, res: Optional[str] = None,
              overlay: Optional[str] = None, quality: Optional[int] = None, scale: Optional[float] = None,
              after: Optional[Union[int, str]] = None, format: Optional[str] = None) -> Image:
        """The current frame. ``space="screen"`` is the rectified screen (needs a calibration);
        ``res="full"`` a full-resolution still; ``after=<frame id>`` waits for a newer frame."""
        return self._image("GET", "/frame.jpg", params={"space": space, "res": res, "overlay": overlay,
                                                        "quality": quality, "scale": scale,
                                                        "after": after, "format": format})

    def zoom(self, region: Region, *, zoom: Optional[float] = None, width: Optional[int] = None,
             rectify: Optional[bool] = None, format: Optional[str] = None) -> Image:
        """A region at full sensor resolution (real detail up to the sensor's density)."""
        return self._image("POST", "/zoom", _clean({"region": _region(region), "zoom": zoom, "width": width,
                                                    "rectify": rectify, "format": format}))

    def snapshot(self, label: str = "") -> Snapshot:
        """Freeze the current frame (full resolution + rectified screen) with its detections."""
        return Snapshot.from_dict(self._json("POST", "/snapshots", {"label": label}))

    # ---- screen ----

    def calibrate(self, corners: Optional[Sequence[Point]] = None, screen_size: Optional[Point] = None, *,
                  method: Optional[str] = None) -> Calibration:
        """Map camera pixels to screen pixels.

        ``corners``: the screen's TL, TR, BR, BL in camera pixels, with ``screen_size`` (w, h).
        Or ``method="auto"`` (the screen shows white) or ``"markers"`` (it shows ``markers.png``).
        """
        if corners is not None:
            if screen_size is None:
                raise ValueError("corners need screen_size=(w, h)")
            payload: Dict[str, Any] = {"corners": [list(c) for c in corners], "screen_size": list(screen_size)}
        elif method:
            payload = _clean({"method": method, "screen_size": list(screen_size) if screen_size else None})
        else:
            raise ValueError("give corners= and screen_size=, or method='auto'|'markers'")
        d = self._json("POST", "/screen/calibrate", payload)
        return Calibration.from_dict(d.get("calibration"))  # type: ignore[return-value]

    def screen(self) -> Optional[Calibration]:
        """The current calibration, or None."""
        return Calibration.from_dict(self._json("GET", "/screen").get("calibration"))

    def forget_calibration(self) -> None:
        self._json("DELETE", "/screen")

    def screen_health(self, *, window_ms: Optional[int] = None, expect_motion: Optional[bool] = None,
                      stuck_pixels: Optional[bool] = None, black_luma: Optional[float] = None,
                      dim_luma: Optional[float] = None) -> Health:
        """Black, dim, frozen (with ``expect_motion``), flicker and stuck pixels over a window."""
        return Health.from_dict(self._json("GET", "/screen/health", params={
            "window_ms": window_ms, "expect_motion": expect_motion, "stuck_pixels": stuck_pixels,
            "black_luma": black_luma, "dim_luma": dim_luma}, timeout=self.timeout + (window_ms or 1000) / 1000))

    def identify(self, *, min_score: Optional[float] = None) -> Identification:
        """Which reference screen is showing."""
        return Identification.from_dict(self._json("GET", "/screen/identify", params={"min_score": min_score}))

    # ---- vision ----

    def detect(self, *, engine: Optional[str] = None, region: Optional[Region] = None,
               classes: Optional[Sequence[str]] = None, min_confidence: Optional[float] = None,
               frames: Optional[int] = None, space: Optional[str] = None) -> List[Element]:
        """Elements on the screen: ``engine="ui"`` buttons, icons and text; ``"live"`` the Hailo model."""
        d = self._json("POST", "/detect", _clean({"engine": engine, "region": _region(region),
                                                  "classes": list(classes) if classes else None,
                                                  "min_confidence": min_confidence, "frames": frames,
                                                  "space": space}))
        return elements(d.get("elements"))

    def ocr(self, region: Optional[Region] = None, *, zoom: Optional[float] = None,
            scale: Optional[float] = None, fresh: Optional[bool] = None, enhance: Optional[str] = None,
            space: Optional[str] = None) -> OCRResult:
        """Text on the screen or in a region, in reading order (cached while the screen is still)."""
        return OCRResult.from_dict(self._json("POST", "/ocr", _clean({
            "region": _region(region), "zoom": zoom, "scale": scale, "fresh": fresh, "enhance": enhance,
            "space": space}), timeout=max(self.timeout, 60)))

    def find(self, text: Optional[str] = None, *, label: Optional[str] = None, template: Optional[str] = None,
             match: Optional[str] = None, region: Optional[Region] = None, near: Any = None,
             classes: Optional[Sequence[str]] = None, zoom: Optional[float] = None,
             case_sensitive: Optional[bool] = None, min_score: Optional[float] = None,
             fresh: Optional[bool] = None) -> List[Element]:
        """Elements matching text (``match``: exact, contains, regex, fuzzy), a label or a template,
        best first. A text inside a button returns the button."""
        q = _query(text, label=label, template=template, match=match, region=region, near=near,
                   classes=classes, zoom=zoom, case_sensitive=case_sensitive, min_score=min_score, fresh=fresh)
        return elements(self._json("POST", "/find", q, timeout=max(self.timeout, 60)).get("matches"))

    def compare(self, reference: str, *, mask: Optional[Sequence[Any]] = None, region: Optional[Region] = None,
                evidence: Optional[bool] = None) -> Comparison:
        """Similarity of the screen to a reference screen (``mask``: names or ``[x, y, w, h]``)."""
        return Comparison.from_dict(self._json("POST", "/compare", _clean({
            "reference": reference, "mask": list(mask) if mask is not None else None,
            "region": _region(region), "evidence": evidence})))

    # ---- waits and checks ----

    def wait_until(self, condition: Mapping[str, Any], *, timeout: float = 10.0, stable: float = 0.0,
                   poll: Optional[float] = None, raise_on_timeout: bool = True) -> WaitResult:
        """Wait on the board until ``condition`` holds (see the API reference for conditions).

        Raises :class:`WaitTimeout` after ``timeout`` seconds unless ``raise_on_timeout=False``.
        Waits longer than :data:`MAX_WAIT_REQUEST_S` run as several requests; ``stable`` then
        restarts with each request.
        """
        deadline = time.monotonic() + timeout
        elapsed_ms = 0
        evaluations = 0
        while True:
            left = deadline - time.monotonic()
            last = left <= MAX_WAIT_REQUEST_S
            chunk = max(min(left, MAX_WAIT_REQUEST_S), 0.0)
            payload = _clean({"condition": dict(condition), "timeout_ms": int(round(chunk * 1000)),
                              "stable_ms": int(stable * 1000) or None,
                              "poll_ms": int(poll * 1000) if poll else None,
                              "error_on_timeout": True if (last and raise_on_timeout) else None})
            try:
                d = self._json("POST", "/wait", payload, timeout=chunk + max(self.timeout, 30))
            except errors.WaitTimeout as e:
                det = e.detail if isinstance(e.detail, dict) else {}
                e.detail = WaitResult.from_dict(det, elapsed_ms + int(det.get("elapsed_ms", 0) or 0),
                                                evaluations + int(det.get("evaluations", 0) or 0))
                raise
            elapsed_ms += int(d.get("elapsed_ms", 0) or 0)
            evaluations += int(d.get("evaluations", 0) or 0)
            if d.get("met") or last:
                return WaitResult.from_dict(d, elapsed_ms, evaluations)

    def wait_for(self, text: Optional[str] = None, *, condition: Optional[Mapping[str, Any]] = None,
                 timeout: float = 10.0, stable: float = 0.0, raise_on_timeout: bool = True,
                 label: Optional[str] = None, template: Optional[str] = None, match: Optional[str] = None,
                 region: Optional[Region] = None, near: Any = None, zoom: Optional[float] = None) -> WaitResult:
        """Wait until text (or a label, a template, or any ``condition``) is on the screen."""
        cond = _condition(text, condition, dict(label=label, template=template, match=match, region=region,
                                                near=near, zoom=zoom))
        return self.wait_until(cond, timeout=timeout, stable=stable, raise_on_timeout=raise_on_timeout)

    def wait_until_gone(self, text: Optional[str] = None, *, timeout: float = 10.0, stable: float = 0.0,
                        raise_on_timeout: bool = True, label: Optional[str] = None,
                        template: Optional[str] = None, match: Optional[str] = None,
                        region: Optional[Region] = None) -> WaitResult:
        """Wait until text (or a label or template) has left the screen."""
        cond = _condition(text, None, dict(label=label, template=template, match=match, region=region),
                          absent=True)
        return self.wait_until(cond, timeout=timeout, stable=stable, raise_on_timeout=raise_on_timeout)

    def check(self, checks: Union[Mapping[str, Mapping[str, Any]], Sequence[Mapping[str, Any]]], *,
              label: str = "") -> CheckResult:
        """Run named conditions once; one evidence bundle for the test report.

        ``checks``: ``{"name": condition, ...}`` or ``[{"name", "condition"}, ...]``.
        """
        if isinstance(checks, Mapping):
            items = [{"name": k, "condition": dict(v)} for k, v in checks.items()]
        else:
            items = [dict(c) for c in checks]
        return CheckResult.from_dict(self._json("POST", "/check", _clean({"checks": items, "label": label or None}),
                                                timeout=max(self.timeout, 60)))

    # ---- references ----

    def references(self) -> List[Dict[str, Any]]:
        d = self._json("GET", "/references")
        return list(d.get("references") or []) if isinstance(d, dict) else list(d or [])

    def add_reference(self, name: str, *, kind: str = "screen", image: Optional[Union[PathLike, bytes]] = None,
                      from_snapshot: Optional[str] = None, region: Optional[Region] = None,
                      mask: Optional[Sequence[Mapping[str, Any]]] = None, overwrite: bool = False) -> Dict[str, Any]:
        """Store a reference screen or template: from the live screen (default), a snapshot, or an
        image file (PNG/JPEG)."""
        if image is None:
            return self._json("POST", "/references", _clean({
                "name": name, "kind": kind, "from_snapshot": from_snapshot, "region": _region(region),
                "mask": [dict(m) for m in mask] if mask else None, "overwrite": overwrite or None}))
        data = image if isinstance(image, bytes) else Path(image).read_bytes()
        fields = {"name": name, "kind": kind, "overwrite": "true" if overwrite else "false"}
        if mask:
            fields["mask"] = json.dumps([dict(m) for m in mask])
        body, ctype = _multipart(fields, "image", "image.png" if data[:4] == b"\x89PNG" else "image.jpg", data)
        with self._open("POST", "/references", body=body, content_type=ctype) as resp:
            return json.loads(resp.read())

    def delete_reference(self, name: str) -> None:
        self._json("DELETE", "/references/" + urllib.parse.quote(name, safe=""))

    def export_references(self, dest: PathLike) -> Path:
        """Save every reference as a zip, to commit with the test suite."""
        return self.download("/references/export.zip", dest)

    def import_references(self, zip_path: PathLike, *, overwrite: bool = False) -> Dict[str, Any]:
        data = Path(zip_path).read_bytes()
        with self._open("POST", "/references/import", body=data, content_type="application/zip",
                        params={"overwrite": overwrite or None}, timeout=max(self.timeout, 120)) as resp:
            return json.loads(resp.read())

    # ---- models ----

    def models(self) -> Dict[str, Any]:
        return self._json("GET", "/models")

    def set_live_model(self, name: str, *, batch: Optional[int] = None) -> Dict[str, Any]:
        """Restart the live graph with another installed ``.hef`` model."""
        return self._json("PUT", "/models/live", _clean({"name": name, "batch": batch}), timeout=max(self.timeout, 60))

    # ---- actuators ----

    def actuators(self) -> List[Dict[str, Any]]:
        return list(self._json("GET", "/actuators").get("actuators") or [])

    def add_actuator(self, id: str, kind: str, **settings: Any) -> Dict[str, Any]:  # noqa: A002
        """Configure an actuator (``kind`` ``simulated`` or ``http``; see the board's actuator docs)."""
        return self._json("POST", "/actuators", {"id": id, "kind": kind, **settings})

    def remove_actuator(self, id: str) -> None:  # noqa: A002
        self._json("DELETE", "/actuators/" + urllib.parse.quote(id, safe=""))

    def press(self, text: Optional[str] = None, *, verify: Optional[Mapping[str, Any]] = None,
              verify_timeout: Optional[float] = None, hold_ms: Optional[int] = None,
              offset: Optional[Point] = None, actuator: Optional[str] = None, label: Optional[str] = None,
              template: Optional[str] = None, match: Optional[str] = None, region: Optional[Region] = None,
              near: Any = None, index: Optional[int] = None) -> PressResult:
        """Find an element, tap its centre, and optionally wait for ``verify`` (a condition).

        Raises :class:`NotFoundError` (``detail["visible_text"]``) when it is not on the screen.
        """
        target = _query(text, label=label, template=template, match=match, region=region, near=near)
        payload = _clean({"target": target, "verify": dict(verify) if verify else None,
                          "verify_timeout_ms": _verify_ms(verify_timeout),
                          "hold_ms": hold_ms, "offset": list(offset) if offset else None,
                          "index": index, "actuator": actuator})
        return PressResult.from_dict(self._json("POST", "/press", payload,
                                                timeout=max(self.timeout, 60) + (verify_timeout or 10)))

    def tap(self, x: Optional[float] = None, y: Optional[float] = None, *, hold_ms: Optional[int] = None,
            actuator: Optional[str] = None, verify: Optional[Mapping[str, Any]] = None,
            verify_timeout: Optional[float] = None) -> PressResult:
        """Tap a point in screen pixels."""
        if x is None or y is None:
            raise ValueError("tap needs x and y (use press() to tap a found element)")
        payload = _clean({"x": x, "y": y, "hold_ms": hold_ms, "actuator": actuator,
                          "verify": dict(verify) if verify else None,
                          "verify_timeout_ms": _verify_ms(verify_timeout)})
        return PressResult.from_dict(self._json("POST", "/tap", payload,
                                                timeout=max(self.timeout, 60) + (verify_timeout or 10)))

    def swipe(self, start: Optional[Point] = None, end: Optional[Point] = None, *,
              direction: Optional[str] = None, distance: Optional[float] = None,
              duration_ms: Optional[int] = None, actuator: Optional[str] = None,
              verify: Optional[Mapping[str, Any]] = None, verify_timeout: Optional[float] = None) -> PressResult:
        """Swipe from ``start`` to ``end`` (screen pixels), or in a ``direction``."""
        payload = _clean({"from": list(start) if start else None, "to": list(end) if end else None,
                          "direction": direction, "distance": distance, "duration_ms": duration_ms,
                          "actuator": actuator, "verify": dict(verify) if verify else None,
                          "verify_timeout_ms": _verify_ms(verify_timeout)})
        return PressResult.from_dict(self._json("POST", "/swipe", payload,
                                                timeout=max(self.timeout, 60) + (verify_timeout or 10)))

    def press_key(self, button: str, *, hold_ms: Optional[int] = None, actuator: Optional[str] = None) -> Dict[str, Any]:
        """Press a physical key (``VOL_UP``, ...) on an actuator that has it."""
        act = actuator or self._first_actuator()
        return self._json("POST", f"/actuators/{urllib.parse.quote(act, safe='')}/press",
                          _clean({"button": button, "hold_ms": hold_ms}))

    def _first_actuator(self) -> str:
        acts = self.actuators()
        if not acts:
            raise errors.ActuatorError("no actuator configured on the board: add_actuator() first")
        return str(acts[0]["id"])

    # ---- recordings and evidence ----

    @contextmanager
    def record(self, dest: Optional[PathLike] = None, *, format: str = "mp4",
               max_seconds: Optional[int] = None, label: str = "") -> Iterator[Dict[str, Any]]:
        """Record the live view for the duration of the ``with`` block; saved to ``dest`` if given."""
        rec = self._json("POST", "/recordings", _clean({"format": format, "max_seconds": max_seconds,
                                                        "label": label or None}))
        rid = str(rec["id"])
        try:
            yield rec
        finally:
            done = self._json("POST", f"/recordings/{rid}/stop", timeout=max(self.timeout, 120))
            if isinstance(done, dict):
                rec.update(done)
            if dest is not None:
                self.download(f"/recordings/{rid}.{format}", dest)

    def evidence(self, evidence_id: str, dest: PathLike) -> Path:
        """Save an evidence bundle (request, result, images) as a zip."""
        return self.download(f"/evidence/{evidence_id}.zip", dest)


def _multipart(fields: Mapping[str, str], file_field: str, filename: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "aiv" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    ctype = "image/png" if filename.endswith(".png") else "image/jpeg"
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {ctype}\r\n\r\n").encode()
    out += data + f"\r\n--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
