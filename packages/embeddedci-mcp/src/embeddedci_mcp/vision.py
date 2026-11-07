"""``vision_*`` tools: the AI Vision board (ai-vision-2) next to the BenchPod tools.

The board watches the device's screen through a camera. An agent looks at the screen
(``vision_look`` returns the image itself), finds text and buttons, presses them through the
board's actuator and waits for the result. The connection is separate from the BenchPod one, so
one session drives both devices.

Errors follow the server's contract: a failed call is a tool error whose message starts with the
cause (``NotFoundError: text "Radio" not on the screen``).
"""

from __future__ import annotations

import threading
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Tuple, TypeVar

import anyio
from mcp.server.fastmcp import Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from embeddedci.aivision import AIVision, AIVisionError, NotFoundError, WaitTimeout, list_boards

T = TypeVar("T")


def _ann(title: str, *, read_only: bool = False, destructive: bool = False,
         idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=None if read_only else destructive,
                           idempotentHint=idempotent, openWorldHint=False)


class VisionSession:
    """The open AI Vision connection (one per server), guarded by a lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.client: Optional[AIVision] = None

    def connect(self, connection: Optional[str], token: Optional[str], lease_as: Optional[str]) -> AIVision:
        with self.lock:
            self.disconnect()
            self.client = AIVision(connection, token=token, lease=lease_as or None)
            return self.client

    def disconnect(self) -> None:
        with self.lock:
            c, self.client = self.client, None
            if c is not None:
                c.close()

    def require(self) -> AIVision:
        """The open client; connects from ``AIVISION_CONNECTION`` on first use."""
        with self.lock:
            if self.client is None:
                try:
                    self.client = AIVision()
                except AIVisionError as exc:
                    raise ToolError(f"not connected to an AI Vision board: call vision_connect ({exc})") from exc
            return self.client


VISION = VisionSession()


def _hint(exc: AIVisionError) -> str:
    if isinstance(exc, NotFoundError) and isinstance(exc.detail, dict) and exc.detail.get("visible_text"):
        return "Visible text: " + ", ".join(map(str, exc.detail["visible_text"][:40]))
    if exc.code == "not_calibrated":
        return ("Calibrate first with vision_calibrate (corners from vision_look(space='camera'), or "
                "method='auto' while the screen shows white).")
    if exc.code == "busy":
        return "Someone else holds the board's lease; wait and retry. Reads (look, ocr, find, wait) still work."
    if exc.code == "unauthorized":
        return ("Over embeddedci.com: run `benchpod login` or set BENCHPOD_API_KEY (an API key with the Control "
                "AI Vision scope). On the LAN: pass the board's token to vision_connect (or set AIVISION_TOKEN).")
    if isinstance(exc, WaitTimeout):
        return "The condition never held; vision_look shows what is on the screen now."
    return ""


async def _call(fn: Callable[[], T]) -> T:
    def run() -> T:
        with VISION.lock:
            return fn()

    try:
        return await anyio.to_thread.run_sync(run)
    except AIVisionError as exc:
        hint = _hint(exc)
        msg = f"{type(exc).__name__}: {exc}"
        raise ToolError(f"{msg}. {hint}" if hint else msg) from exc
    except ValueError as exc:
        raise ToolError(f"invalid argument: {exc}") from exc


def _wrap(fn: Callable[[], T]) -> T:
    """``fn`` with an AI Vision error turned into a tool error (for calls that need no session)."""
    try:
        return fn()
    except AIVisionError as exc:
        hint = _hint(exc)
        msg = f"{type(exc).__name__}: {exc}"
        raise ToolError(f"{msg}. {hint}" if hint else msg) from exc


# -- results -----------------------------------------------------------------------------

class VBox(BaseModel):
    x: float
    y: float
    w: float
    h: float
    space: str = "screen"


class VElement(BaseModel):
    kind: str = Field(description="button, icon, text, or a live-model class.")
    label: str = ""
    text: str = ""
    confidence: float = 0.0
    box: Optional[VBox] = None
    center: Optional[Tuple[float, float]] = Field(None, description="Where a press taps, in screen pixels.")


def _el(e: Any) -> VElement:
    b = VBox(x=e.box.x, y=e.box.y, w=e.box.w, h=e.box.h, space=e.box.space) if e.box else None
    return VElement(kind=e.kind, label=e.label, text=e.text, confidence=e.confidence, box=b, center=e.center)


class CloudBoard(BaseModel):
    id: str
    name: str
    online: bool
    connection: str = Field(description="What vision_connect takes: 'embeddedci:<name>'.")
    last_active_at: Optional[str] = None


class CloudBoards(BaseModel):
    boards: List[CloudBoard]


class VisionStatus(BaseModel):
    connected: bool
    url: str = ""
    version: str = ""
    calibrated: bool = False
    screen_size: Optional[Tuple[int, int]] = None
    camera_locked: Optional[bool] = None
    actuators: List[str] = []
    lease: Optional[str] = Field(None, description="Who holds the board's lease, if anyone.")
    capabilities: List[str] = []
    warnings: List[str] = []


class OcrOut(BaseModel):
    text: str
    lines: List[VElement]
    frame_id: int


class FindOut(BaseModel):
    found: bool
    matches: List[VElement]


class WaitOut(BaseModel):
    met: bool
    condition: str
    elapsed_s: float
    match: Optional[VElement] = None


class CompareOut(BaseModel):
    score: float = Field(description="0..1; 1 = identical to the reference.")
    changed_pct: float
    reference: str


class HealthOut(BaseModel):
    ok: bool
    problems: List[str]
    black: bool
    dim: bool
    frozen: bool
    flicker_hz: float
    mean_luma: float


class CalibrationOut(BaseModel):
    corners: List[Tuple[float, float]]
    screen_size: Tuple[int, int]
    method: str
    quality: float


class ReferenceOut(BaseModel):
    name: str
    kind: str
    size: Optional[List[int]] = None


class PressOut(BaseModel):
    found: Optional[VElement] = None
    tapped_at: Optional[Tuple[float, float]] = None
    verified: Optional[bool] = Field(None, description="None when no verify condition was given.")
    verify_condition: str = ""
    evidence: str = ""


# -- tools --------------------------------------------------------------------------------

Region = Annotated[Optional[List[float]], Field(min_length=4, max_length=4, description=(
    "[x, y, w, h] in screen pixels (camera pixels before calibration). Omit for the whole screen."))]
Match = Annotated[Optional[Literal["exact", "contains", "regex", "fuzzy"]], Field(description=(
    "How text is compared; default exact (case-insensitive)."))]


def _status(c: Optional[AIVision]) -> VisionStatus:
    if c is None:
        return VisionStatus(connected=False, warnings=["not connected: call vision_connect"])
    s = c.status()
    scr = s.get("screen") or {}
    cal = scr.get("calibration") or {}
    cam = s.get("camera") or {}
    lease = s.get("lease") or None
    warnings = []
    if not scr.get("calibrated"):
        warnings.append("screen not calibrated: vision_calibrate before find, press or compare")
    elif not cam.get("locked"):
        warnings.append("camera exposure not locked: screen health cannot see a dim backlight")
    acts = [str(a.get("id")) for a in s.get("actuators") or [] if isinstance(a, dict)]
    if not acts:
        warnings.append("no actuator configured: vision_press and vision_tap will fail")
    size = cal.get("screen_size")
    return VisionStatus(connected=True, url=f"embeddedci:{c.cloud_name}" if getattr(c, "cloud_name", None) else c.url, version=str(s.get("version", "")),
                        calibrated=bool(scr.get("calibrated")), screen_size=tuple(size) if size else None,
                        camera_locked=cam.get("locked"), actuators=acts,
                        lease=lease.get("who") if isinstance(lease, dict) else None,
                        capabilities=c.capabilities(), warnings=warnings)


def register(mcp: Any) -> None:
    """Add the vision_* tools to the server."""

    @mcp.tool(annotations=_ann("Connect to an AI Vision board", idempotent=True))
    async def vision_connect(
        connection: Annotated[Optional[str], Field(description=(
            "host[:port] or URL of the ai-vision-2 board, 'discover' (mDNS), or 'embeddedci:<name>' for a "
            "board on embeddedci.com (vision_cloud_boards lists them). Omit for AIVISION_CONNECTION."))] = None,
        token: Annotated[Optional[str], Field(description=(
            "The board's API token on the LAN; omit for AIVISION_TOKEN. Over embeddedci.com the "
            "`benchpod login` session or BENCHPOD_API_KEY is used instead."))] = None,
        lease_as: Annotated[Optional[str], Field(description=(
            "Take the board's lease under this name, so nobody else drives the camera or the actuator "
            "until vision_disconnect."))] = None,
    ) -> VisionStatus:
        """Open the AI Vision connection (closing a previous one) and report the board's status."""
        return await _call(lambda: _status(VISION.connect(connection, token, lease_as)))

    @mcp.tool(annotations=_ann("List AI Vision boards on embeddedci.com", read_only=True))
    async def vision_cloud_boards() -> CloudBoards:
        """The AI Vision boards registered to your embeddedci.com organisation, and whether each is
        online. Connect to one with vision_connect(connection) using its `connection`
        ('embeddedci:<name>'). Authenticates with BENCHPOD_API_KEY, else the `benchpod login`
        session."""
        def op() -> CloudBoards:
            boards = [CloudBoard(id=str(d.get("id", "")), name=str(d.get("name", "")), online=bool(d.get("online")),
                                 connection=str(d.get("connection") or f"embeddedci:{d.get('name', '')}"),
                                 last_active_at=d.get("last_active_at"))
                      for d in list_boards()]
            return CloudBoards(boards=boards)

        return await anyio.to_thread.run_sync(lambda: _wrap(op))

    @mcp.tool(annotations=_ann("Disconnect from the AI Vision board", idempotent=True))
    async def vision_disconnect() -> VisionStatus:
        """Close the AI Vision connection and release its lease. Safe when not connected."""
        await _call(VISION.disconnect)
        return VisionStatus(connected=False)

    @mcp.tool(annotations=_ann("AI Vision board status", read_only=True))
    async def vision_status() -> VisionStatus:
        """Version, calibration, camera lock, actuators, lease and capabilities, with warnings for
        what will block the next steps."""
        return await _call(lambda: _status(VISION.require()))

    @mcp.tool(annotations=_ann("Look at the screen", read_only=True))
    async def vision_look(
        space: Annotated[Literal["screen", "camera"], Field(description=(
            "screen: the rectified screen in its own pixels (needs a calibration); camera: what the "
            "camera sees, to find the screen's corners."))] = "screen",
        overlay: Annotated[Optional[Literal["elements", "text", "detections"]], Field(description=(
            "Draw what the board recognised on the image."))] = None,
        region: Region = None,
    ) -> Image:
        """The screen as an image, so you can read it yourself. With a region: that part at full
        sensor resolution (small text, icons)."""
        def op() -> Image:
            c = VISION.require()
            if region is not None:
                img = c.zoom(region)
            elif space == "camera":
                img = c.frame(space="camera", res="preview", overlay=overlay)
            else:
                img = c.frame(space="screen", overlay=overlay)
            return Image(data=img.data, format="png" if "png" in img.content_type else "jpeg")

        return await _call(op)

    @mcp.tool(annotations=_ann("Read text on the screen", read_only=True))
    async def vision_ocr(region: Region = None,
                         zoom: Annotated[Optional[float], Field(gt=0, le=4, description=(
                             "Read at this many image pixels per screen pixel (small text)."))] = None) -> OcrOut:
        """All text on the screen (or a region) in reading order, with boxes."""
        def op() -> OcrOut:
            o = VISION.require().ocr(region, zoom=zoom)
            lines = [VElement(kind="text", label=b.text, text=b.text, confidence=b.confidence,
                              box=VBox(**b.raw["box"]) if b.raw.get("box") else None,
                              center=b.box.center if b.box else None) for b in o.blocks]
            return OcrOut(text=o.text, lines=lines, frame_id=o.frame_id)

        return await _call(op)

    @mcp.tool(annotations=_ann("Find on the screen", read_only=True))
    async def vision_find(
        text: Annotated[Optional[str], Field(description="Text to look for.")] = None,
        label: Annotated[Optional[str], Field(description="Element kind instead: button, icon, text.")] = None,
        template: Annotated[Optional[str], Field(description="Name of a template reference instead.")] = None,
        match: Match = None,
        region: Region = None,
    ) -> FindOut:
        """Elements matching text, a kind or a template, best first. Text inside a button returns the
        button (its center is where a press taps)."""
        def op() -> FindOut:
            ms = VISION.require().find(text, label=label, template=template, match=match, region=region)
            return FindOut(found=bool(ms), matches=[_el(e) for e in ms[:20]])

        return await _call(op)

    @mcp.tool(annotations=_ann("Wait for the screen", read_only=True))
    async def vision_wait_for(
        text: Annotated[str, Field(description="Text to wait for.")],
        timeout_s: Annotated[float, Field(gt=0, le=600)] = 10.0,
        gone: Annotated[bool, Field(description="Wait until the text has left the screen instead.")] = False,
        match: Match = None,
        region: Region = None,
    ) -> WaitOut:
        """Wait until text appears (or, with gone, disappears). Returns met=false on timeout."""
        def op() -> WaitOut:
            c = VISION.require()
            if gone:
                r = c.wait_until_gone(text, timeout=timeout_s, match=match, region=region, raise_on_timeout=False)
            else:
                r = c.wait_for(text, timeout=timeout_s, match=match, region=region, raise_on_timeout=False)
            return WaitOut(met=r.met, condition=r.condition, elapsed_s=r.elapsed_ms / 1000,
                           match=_el(r.match) if r.match else None)

        return await _call(op)

    @mcp.tool(annotations=_ann("Compare with a reference screen", read_only=True))
    async def vision_compare(
        reference: Annotated[str, Field(description="Name of a reference screen (vision_add_reference).")],
        mask: Annotated[Optional[List[Any]], Field(description=(
            "Areas to ignore: names of the reference's masks ('clock') or [x, y, w, h]."))] = None,
    ) -> CompareOut:
        """How close the screen is to a stored reference screen."""
        def op() -> CompareOut:
            r = VISION.require().compare(reference, mask=mask)
            return CompareOut(score=r.score, changed_pct=r.changed_pct, reference=r.reference)

        return await _call(op)

    @mcp.tool(annotations=_ann("Screen health", read_only=True))
    async def vision_screen_health(
        window_ms: Annotated[int, Field(ge=100, le=10000)] = 1000,
        expect_motion: Annotated[bool, Field(description="Count a frozen screen as a problem.")] = False,
    ) -> HealthOut:
        """Black, dim, frozen, flickering or stuck pixels over a window of frames."""
        def op() -> HealthOut:
            h = VISION.require().screen_health(window_ms=window_ms, expect_motion=expect_motion)
            return HealthOut(ok=h.ok, problems=h.problems, black=h.black, dim=h.dim, frozen=h.frozen,
                             flicker_hz=h.flicker_hz, mean_luma=h.mean_luma)

        return await _call(op)

    @mcp.tool(annotations=_ann("Calibrate the screen", idempotent=True))
    async def vision_calibrate(
        corners: Annotated[Optional[List[List[float]]], Field(min_length=4, max_length=4, description=(
            "The screen's top-left, top-right, bottom-right, bottom-left corners in camera pixels "
            "(read them from vision_look(space='camera')). Or omit and use method."))] = None,
        screen_size: Annotated[Optional[List[int]], Field(min_length=2, max_length=2, description=(
            "The screen's own resolution [w, h], e.g. [1920, 720]."))] = None,
        method: Annotated[Optional[Literal["auto", "markers"]], Field(description=(
            "auto: find the bright screen (show white on it); markers: the screen shows markers.png."))] = None,
    ) -> CalibrationOut:
        """Map camera pixels to screen pixels; every later box and tap is in screen pixels."""
        def op() -> CalibrationOut:
            cal = VISION.require().calibrate(corners=[tuple(p) for p in corners] if corners else None,
                                             screen_size=tuple(screen_size) if screen_size else None,
                                             method=method)
            return CalibrationOut(corners=cal.corners, screen_size=cal.screen_size, method=cal.method,
                                  quality=cal.quality)

        return await _call(op)

    @mcp.tool(annotations=_ann("Store a reference screen", idempotent=True))
    async def vision_add_reference(
        name: str,
        kind: Literal["screen", "template"] = "screen",
        region: Region = None,
        overwrite: bool = False,
    ) -> ReferenceOut:
        """Store the current screen (or a region of it, e.g. an icon as a template) as a reference."""
        def op() -> ReferenceOut:
            r = VISION.require().add_reference(name, kind=kind, region=region, overwrite=overwrite)
            return ReferenceOut(name=str(r.get("name", name)), kind=str(r.get("kind", kind)), size=r.get("size"))

        return await _call(op)

    @mcp.tool(annotations=_ann("Press on the screen", destructive=True))
    async def vision_press(
        text: Annotated[str, Field(description="Text of the button or element to press.")],
        verify_text: Annotated[Optional[str], Field(description="Then wait for this text to appear.")] = None,
        verify_timeout_s: Annotated[float, Field(gt=0, le=90, description=(
            "Seconds to wait for verify_text; the board caps one request at 90 s."))] = 5.0,
        match: Match = None,
        actuator: Annotated[Optional[str], Field(description="Actuator id; omit for the first one.")] = None,
    ) -> PressOut:
        """Find the element, tap its center with the actuator, and optionally verify the result. Fails
        with the visible text when the element is not on the screen."""
        def op() -> PressOut:
            r = VISION.require().press(text, match=match, actuator=actuator,
                                       verify={"text": verify_text} if verify_text else None,
                                       verify_timeout=verify_timeout_s if verify_text else None)
            return PressOut(found=_el(r.found) if r.found else None, tapped_at=r.tapped_at, verified=r.verified,
                            verify_condition=r.verify.condition if r.verify else "", evidence=r.evidence)

        return await _call(op)

    @mcp.tool(annotations=_ann("Tap a point", destructive=True))
    async def vision_tap(
        x: Annotated[float, Field(ge=0, description="Screen pixels.")],
        y: Annotated[float, Field(ge=0, description="Screen pixels.")],
        hold_ms: Annotated[int, Field(ge=0, le=10000)] = 0,
        actuator: Annotated[Optional[str], Field(description="Actuator id; omit for the first one.")] = None,
    ) -> PressOut:
        """Tap a point in screen pixels (hold_ms > 0 for a long press)."""
        def op() -> PressOut:
            r = VISION.require().tap(x, y, hold_ms=hold_ms or None, actuator=actuator)
            return PressOut(tapped_at=r.tapped_at, evidence=r.evidence)

        return await _call(op)
