"""SPI master on the LA pins: raw transfers and SPI NOR flash programming.

Firmware 3.x with gateware v45+ (capability ``spi_master``) runs an SPI master on any four LA
channels (SCK, MOSI, MISO, CS), up to 6 MHz, modes 0 and 3. It shares the SWD engine, so an SPI
session and an SWD flash exclude each other. The flash operations are the pod's own
``spi_flash`` command: standard 25-series parts (W25Q, MX25, GD25, IS25, ...), 3-byte addresses
(the first 16 MB). Every request and reply fits one cloud command frame, so all of this works over
the LAN, the USB console and the cloud alike.

Open a session with :meth:`BenchPod.open_spi`; program a whole image with :meth:`BenchPod.spi_flash`.
:meth:`SpiSession.stream` sends data too big for one command (an FPGA bitstream) in a single CS
frame: it is staged in the pod's PSRAM first (TCP or cloud connection, capability ``spi_stream``).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

#: Bytes per ``spi_xfer`` / ``spi_flash write`` command, and per ``spi_flash read``.
XFER_MAX = 768
READ_MAX = 1024
#: Bytes ``spi_stream`` sends ahead of the staged data in the same frame (``head``).
STREAM_HEAD_MAX = 64
#: Bytes per ``spi_flash erase`` command: at most 16 block erases, a few seconds.
ERASE_STEP = 1 << 20

Progress = Callable[[int, int], None]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SpiFlashInfo:
    """What :meth:`SpiSession.flash_id` found on the bus."""

    #: JEDEC ID as 6 hex digits, e.g. ``"ef4017"`` (Winbond W25Q64).
    jedec_id: str
    #: False when the ID read all 00 or all FF: nothing is answering on those pins.
    present: bool
    #: Capacity in bytes from the ID, or 0 when the ID does not encode it.
    size: int
    #: Status register 1.
    status: int


@dataclass(frozen=True)
class SpiFlashResult:
    """The outcome of :meth:`SpiSession.flash_program` / :meth:`BenchPod.spi_flash`."""

    jedec_id: str
    addr: int
    length: int
    #: Bytes erased (whole 4 KB sectors covering the image), 0 with ``erase=False``.
    erased: int
    verified: bool
    seconds: float


@dataclass(frozen=True)
class SpiStreamResult:
    """The outcome of :meth:`SpiSession.stream`."""

    #: Bytes of the staged data clocked out (``head`` not counted).
    sent: int
    #: Seconds the pod took to clock them out (the upload not included).
    seconds: float


class SpiSession:
    """An armed SPI master (from :meth:`BenchPod.open_spi`). Use it as a context manager, or call
    :meth:`close` to release the four pins."""

    def __init__(self, command: Callable[[Dict[str, Any]], Any], info: Dict[str, Any], *,
                 stage: Optional[Callable[[bytes], int]] = None) -> None:
        self._command = command
        self._stage = stage
        self.sck = int(info.get("sck", 0))
        self.mosi = int(info.get("mosi", 0))
        self.miso = int(info.get("miso", 0))
        self.cs = int(info.get("cs", 0))
        #: The SCK rate the pod picked (at or below the one asked for).
        self.hz = int(info.get("hz", 0))
        self.mode = int(info.get("mode", 0))
        self._closed = False

    # -- lifetime ----------------------------------------------------------------------------
    def close(self) -> None:
        """Stop the SPI master and release its pins (they go back to high-Z). Idempotent."""
        if not self._closed:
            self._closed = True
            self._command({"cmd": "spi_stop"})

    def __enter__(self) -> "SpiSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- raw transfers -----------------------------------------------------------------------
    def transfer(self, data: bytes, *, hold_cs: bool = False) -> bytes:
        """Full duplex: clock ``data`` out on MOSI and return the bytes clocked in on MISO.

        CS is asserted before the first byte and released after the last, unless ``hold_cs``
        keeps it asserted for the next :meth:`transfer` (one transaction over several calls).
        Longer data is split into 768-byte commands with CS held between them."""
        data = bytes(data)
        if not data:
            raise ValueError("transfer needs at least one byte")
        out = bytearray()
        for off in range(0, len(data), XFER_MAX):
            last = off + XFER_MAX >= len(data)
            reply = self._command({"cmd": "spi_xfer", "tx": _b64(data[off:off + XFER_MAX]),
                                   "cs": "release" if last and not hold_cs else "hold"})
            out += _unb64(str(reply["rx"]))
        return bytes(out)

    def stream(self, data: bytes, *, head: bytes = b"", hold_cs: bool = False) -> SpiStreamResult:
        """Send ``head`` (up to 64 bytes, e.g. a command opcode) and then ``data``, of any size, in
        one CS frame, without reading anything back.

        ``data`` is uploaded into the pod's PSRAM first (``load_bin``), then the pod clocks it out
        512 bytes at a time with CS asserted throughout (SCK pauses between chunks). This is how
        an FPGA's slave-SPI configuration port takes a whole bitstream. Needs a TCP or cloud
        connection and firmware with the ``spi_stream`` command. ``hold_cs`` keeps CS asserted
        afterwards, for a following :meth:`transfer`."""
        head = bytes(head)
        data = bytes(data)
        if len(head) > STREAM_HEAD_MAX:
            raise ValueError(f"head is at most {STREAM_HEAD_MAX} bytes")
        if not head and not data:
            raise ValueError("stream needs at least one byte")
        if data:
            if self._stage is None:
                raise NotImplementedError("streaming needs a TCP or cloud connection (the data is "
                                          "uploaded into the pod's PSRAM first)")
            self._stage(data)
        req: Dict[str, Any] = {"cmd": "spi_stream", "len": len(data),
                               "cs": "hold" if hold_cs else "release"}
        if head:
            req["head"] = _b64(head)
        r = self._command(req)
        r = r if isinstance(r, dict) else {}
        return SpiStreamResult(sent=int(r.get("sent", len(data))),
                               seconds=int(r.get("ms", 0)) / 1000.0)

    # -- SPI NOR flash -----------------------------------------------------------------------
    def flash_id(self) -> SpiFlashInfo:
        """Read the JEDEC ID and status register."""
        r = self._command({"cmd": "spi_flash", "op": "id"})
        return SpiFlashInfo(jedec_id=str(r["id"]), present=bool(r["present"]),
                            size=int(r.get("size", 0)), status=int(r.get("status", 0)))

    def flash_read(self, addr: int, length: int, *, progress: Optional[Progress] = None) -> bytes:
        """Read ``length`` bytes from ``addr`` (1 KB per command)."""
        if length <= 0:
            raise ValueError("length must be positive")
        out = bytearray()
        while len(out) < length:
            n = min(READ_MAX, length - len(out))
            r = self._command({"cmd": "spi_flash", "op": "read", "addr": addr + len(out), "len": n})
            out += _unb64(str(r["data"]))
            if progress:
                progress(len(out), length)
        return bytes(out)

    def flash_erase(self, addr: int, length: int, *, progress: Optional[Progress] = None) -> Tuple[int, int]:
        """Erase every 4 KB sector ``[addr, addr+length)`` touches (64 KB blocks where whole).
        Returns the range actually erased as ``(start, length)``; it is sector-aligned."""
        if length <= 0:
            raise ValueError("length must be positive")
        start = end = None
        pos, stop = addr, addr + length
        while pos < stop:
            n = min(ERASE_STEP, stop - pos)
            r = self._command({"cmd": "spi_flash", "op": "erase", "addr": pos, "len": n})
            s, l = int(r["addr"]), int(r["len"])
            start = s if start is None else start
            end = s + l
            pos = max(pos + n, end)
            if progress:
                progress(min(pos, stop) - addr, length)
        assert start is not None and end is not None
        return start, end - start

    def flash_chip_erase(self) -> float:
        """Erase the whole chip; returns the seconds the pod reported. A large part can take
        minutes, longer than a cloud command may wait: prefer :meth:`flash_erase` there."""
        r = self._command({"cmd": "spi_flash", "op": "chip_erase"})
        return int(r.get("ms", 0)) / 1000.0

    def flash_write(self, addr: int, data: bytes, *, verify: bool = True,
                    progress: Optional[Progress] = None) -> None:
        """Program ``data`` at ``addr`` (768 bytes per command; the range must be erased).
        With ``verify`` the pod reads every chunk back and fails on the first difference."""
        data = bytes(data)
        for off in range(0, len(data), XFER_MAX):
            req: Dict[str, Any] = {"cmd": "spi_flash", "op": "write", "addr": addr + off,
                                   "data": _b64(data[off:off + XFER_MAX])}
            if not verify:
                req["verify"] = False
            self._command(req)
            if progress:
                progress(min(off + XFER_MAX, len(data)), len(data))

    def flash_program(self, data: bytes, addr: int = 0, *, erase: bool = True, verify: bool = True,
                      progress: Optional[Progress] = None) -> SpiFlashResult:
        """Erase (unless ``erase=False``) and write a whole image, verifying every chunk.

        ``progress(done, total)`` counts erase bytes first, then written bytes.
        Raises :class:`~embeddedci.benchpod.errors.FirmwareError` when no flash answers."""
        data = bytes(data)
        if not data:
            raise ValueError("the image is empty")
        t0 = time.monotonic()
        info = self.flash_id()
        if not info.present:
            from .errors import FirmwareError
            raise FirmwareError(f"no SPI flash answers on SCK LA{self.sck} / MISO LA{self.miso} "
                                f"(JEDEC ID {info.jedec_id})", cmd="spi_flash")
        if info.size and addr + len(data) > info.size:
            raise ValueError(f"{len(data)} bytes at 0x{addr:x} do not fit the {info.size}-byte flash")
        total = (len(data) if erase else 0) + len(data)
        erased = 0
        if erase:
            _, erased = self.flash_erase(addr, len(data),
                                         progress=(lambda d, t: progress(d, total)) if progress else None)
        base = len(data) if erase else 0
        self.flash_write(addr, data, verify=verify,
                         progress=(lambda d, t: progress(base + d, total)) if progress else None)
        return SpiFlashResult(jedec_id=info.jedec_id, addr=addr, length=len(data), erased=erased,
                              verified=verify, seconds=time.monotonic() - t0)
