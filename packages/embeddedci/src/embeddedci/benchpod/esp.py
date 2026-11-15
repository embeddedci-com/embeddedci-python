"""Flash an ESP32-family target over its UART ROM bootloader (the pod's ``esp_flash`` command).

The pod drives the target's own ROM loader, the way ``esptool`` does, over two LA pins plus the
boot strap and EN: no USB cable to the target and no esptool on this machine. Each region is
uploaded into the pod's PSRAM (``load_bin``), then written by the pod in the background while this
module polls its progress. Works the same over the LAN, USB and the cloud.

Use it through :meth:`BenchPod.flash <embeddedci.benchpod.BenchPod.flash>` with an ESP32-family
``target`` (``flash(target="esp32", file=..., boot=...)``); this module is its implementation.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from .errors import FlashError

#: ``images`` forms: a path (a ``.bin``, or an ESP-IDF ``flasher_args.json``), or a mapping of flash
#: offset (int, or a ``"0x1000"`` string) to a path or the bytes themselves.
Images = Union[str, "os.PathLike[str]", Mapping[Union[int, str], Union[str, bytes, "os.PathLike[str]"]]]

#: Progress callback: bytes written so far over all regions, and the total.
Progress = Callable[[int, int], None]

#: Wiring-profile signal names looked up when ``boot`` / ``en`` are not given (case-insensitive).
BOOT_SIGNALS = ("BOOT", "IO0", "GPIO0", "IO9", "GPIO9", "ESP_BOOT")
EN_SIGNALS = ("EN", "CHIP_EN", "ESP_EN", "CHIP_PU")

DEFAULT_BAUD = 460800
#: SPI_SET_PARAMS size when the image does not name one: only an upper bound for the ROM's writes,
#: so a size larger than the real chip costs nothing (a write past the chip fails at the erase).
DEFAULT_FLASH_SIZE = 16 * 1024 * 1024
POLL_INTERVAL = 0.25

_SIZES = {"256KB": 256 * 1024, "512KB": 512 * 1024, "1MB": 1 << 20, "2MB": 2 << 20, "4MB": 4 << 20,
          "8MB": 8 << 20, "16MB": 16 << 20, "32MB": 32 << 20, "64MB": 64 << 20, "128MB": 128 << 20}


@dataclass
class EspRegion:
    """One region written: its flash ``offset``, ``length`` in bytes, the chip's MD5 of it and the
    pod's ``result`` (``"ok"``, ``"unverified"`` or the failure)."""

    offset: int
    length: int
    md5: str = ""
    result: str = ""
    seconds: float = 0.0
    resends: int = 0


@dataclass
class EspFlashResult:
    """Outcome of :func:`run`: what ``flash()`` reports for an ESP32 target."""

    ok: bool
    chip: str = ""
    regions: List[EspRegion] = field(default_factory=list)
    baud: int = 0
    seconds: float = 0.0
    #: The failure in words when not ok, ``""`` otherwise.
    error: str = ""

    @property
    def verified(self) -> bool:
        """Every region's MD5 matched (``ok`` regions only, none ``unverified``)."""
        return self.ok and all(r.result == "ok" for r in self.regions)


def parse_flash_size(value: Union[int, str, None]) -> int:
    """``"4MB"`` / ``4194304`` / ``None`` (the default, 16 MB) as bytes."""
    if value is None or value == "":
        return DEFAULT_FLASH_SIZE
    if isinstance(value, int):
        return value
    v = str(value).strip().upper().replace(" ", "")
    if v in _SIZES:
        return _SIZES[v]
    if v.isdigit():
        return int(v)
    raise ValueError(f"flash_size must be like 4MB or a byte count, got {value!r}")


def _offset(key: Union[int, str]) -> int:
    if isinstance(key, int):
        return key
    return int(str(key).strip(), 0)


def _read(src: Union[str, bytes, "os.PathLike[str]"], base: str = "") -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    path = os.fspath(src)
    if base and not os.path.isabs(path):
        path = os.path.join(base, path)
    with open(path, "rb") as fh:
        return fh.read()


def parse_address(value: Union[int, str, None]) -> int:
    """``flash(load_address=...)`` for an ESP32: ``"0x10000"`` / ``65536`` / ``""`` (0)."""
    if value is None or value == "":
        return 0
    return value if isinstance(value, int) else int(str(value).strip(), 0)


def load_images(images: Images, *, load_address: int = 0) -> Tuple[List[Tuple[int, bytes]], Dict[str, Any]]:
    """The regions to write, sorted by offset, and settings found with them (``flash_size``,
    ``chip``) from a ``flasher_args.json``. A ``.bin`` goes at ``load_address``. Overlapping regions
    raise :class:`ValueError`."""
    settings: Dict[str, Any] = {}
    regions: List[Tuple[int, bytes]] = []
    if isinstance(images, Mapping):
        regions = [(_offset(k), _read(v)) for k, v in images.items()]
    else:
        path = os.fspath(images)
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as fh:
                args = json.load(fh)
            files = args.get("flash_files")
            if not isinstance(files, Mapping) or not files:
                raise ValueError(f"{path}: no flash_files (is this an ESP-IDF flasher_args.json?)")
            base = os.path.dirname(os.path.abspath(path))
            regions = [(_offset(k), _read(v, base)) for k, v in files.items()]
            fs = (args.get("flash_settings") or {}).get("flash_size")
            if fs and fs not in ("keep", "detect"):
                settings["flash_size"] = fs
            chip = (args.get("extra_esptool_args") or {}).get("chip")
            if chip:
                settings["chip"] = chip
        else:
            regions = [(load_address, _read(path))]
    regions.sort(key=lambda r: r[0])
    for off, data in regions:
        if not data:
            raise ValueError(f"the image at 0x{off:x} is empty")
        if off % 4096:
            raise ValueError(f"offset 0x{off:x} is not a multiple of 4096 (a flash sector)")
    for (a, da), (b, _db) in zip(regions, regions[1:]):
        if a + len(da) > b:
            raise ValueError(f"the image at 0x{a:x} ({len(da)} bytes) overlaps the one at 0x{b:x}")
    return regions, settings


def _status_error(st: Mapping[str, Any]) -> str:
    res = str(st.get("result") or "")
    phase = str(st.get("phase") or "")
    if res == "no answer from the ROM loader":
        return ("no answer from the ROM loader: check that the target is powered, TXD0 goes to rx "
                "and RXD0 to tx, the boot pin (IO0, IO9 on the C3/C6) is on boot, and EN on en")
    return f"{res} (phase {phase})" if phase else res


def run(
    command: Callable[[Dict[str, Any]], Any],
    stage: Optional[Callable[[bytes], Any]],
    regions: List[Tuple[int, bytes]],
    *,
    pins: Mapping[str, Any],
    baud: int = DEFAULT_BAUD,
    flash_size: int = DEFAULT_FLASH_SIZE,
    run_after: bool = True,
    progress: Optional[Progress] = None,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> EspFlashResult:
    """Write ``regions`` one after the other: stage, ``esp_flash`` start, poll ``status`` until
    done. ``command`` sends one JSON command and returns its ``data``; ``stage`` is the transport's
    ``stage_psram``. Stops at the first failed region."""
    if stage is None:
        raise FlashError("flashing an ESP32 needs a network, USB or cloud connection (the image is "
                         "uploaded into the pod's memory first), not the serial console")
    total = sum(len(d) for _, d in regions)
    for off, data in regions:
        if off + len(data) > flash_size:
            raise ValueError(f"the image at 0x{off:x} ({len(data)} bytes) does not fit a "
                             f"{flash_size // 1024} KB flash; pass flash_size")
    out = EspFlashResult(ok=False, baud=baud)
    t_all = clock()
    written = 0
    for i, (off, data) in enumerate(regions):
        t0 = clock()
        stage(data)
        req: Dict[str, Any] = {"cmd": "esp_flash", "op": "start", **pins, "offset": off,
                               "len": len(data), "baud": baud, "flash_size": flash_size,
                               "run": bool(run_after and i == len(regions) - 1)}
        command(req)
        # Erase (FLASH_BEGIN) plus the data at the chosen rate, twice over, plus a margin.
        deadline = t0 + 60.0 + 2.0 * len(data) * 10.0 / max(baud, 9600) + len(data) / 50_000
        st: Mapping[str, Any] = {}
        while True:
            sleep(poll_interval)
            st = command({"cmd": "esp_flash", "op": "status"}) or {}
            done_blocks = int(st.get("done", 0) or 0)
            if progress:
                progress(min(written + done_blocks * 1024, written + len(data)), total)
            if not st.get("running"):
                break
            if clock() > deadline:
                command({"cmd": "esp_flash", "op": "abort"})
                out.error = f"the write at 0x{off:x} did not finish in time (stopped at {st.get('phase')})"
                out.seconds = clock() - t_all
                return out
        out.chip = str(st.get("chip") or out.chip)
        out.baud = int(st.get("baud") or out.baud)
        res = str(st.get("result") or "")
        out.regions.append(EspRegion(offset=off, length=len(data), md5=str(st.get("md5") or ""),
                                     result=res, seconds=clock() - t0,
                                     resends=int(st.get("resends", 0) or 0)))
        if res not in ("ok", "unverified"):
            out.error = f"the write at 0x{off:x} failed: {_status_error(st)}"
            out.seconds = clock() - t_all
            return out
        written += len(data)
        if progress:
            progress(written, total)
    out.ok = True
    out.seconds = clock() - t_all
    return out


def is_esp_target(target: str) -> bool:
    """``target`` names an ESP32-family chip (``esp32``, ``esp32s3``, ``ESP32-C3``)."""
    return bool(re.fullmatch(r"esp32(-?(s2|s3|c2|c3|c5|c6|h2|p4))?", str(target).strip().lower()))
