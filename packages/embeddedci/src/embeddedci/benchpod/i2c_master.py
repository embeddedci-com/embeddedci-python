"""The pod as an I2C master on two LA pins, and the transactions a capture can send.

Firmware with gateware v55+ runs an I2C master on any two LA channels (SDA, SCL), 10 kHz to 1 MHz.
The pins are open drain, so the bus needs pull-ups: the pod's own (LA1..LA6, switched on with
:meth:`BenchPod.enable_pullup <embeddedci.benchpod.BenchPod.enable_pullup>`) or the board's. SWD,
SPI and I2C sessions share one engine in the FPGA, so they exclude each other. Every command and
reply fits one cloud frame, so this works over the LAN, USB and the cloud alike.

Open a session with :meth:`BenchPod.open_i2c <embeddedci.benchpod.BenchPod.open_i2c>`.

:class:`I2CSend`, :class:`SpiSend` and :class:`GpioSend` are what
:meth:`BenchPod.capture_la <embeddedci.benchpod.BenchPod.capture_la>` takes as ``send``: the pod
runs that one transaction right after the capture is armed, in the same command.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .errors import BenchPodError

#: Bytes one ``i2c_xfer`` writes after the address, and reads.
WRITE_MAX = 128
READ_MAX = 248
#: Bytes a send-and-capture I2C transfer writes and reads; an SPI send's frame.
SEND_MAX = 32
#: The SCL range the pod runs.
HZ_MIN = 10_000
HZ_MAX = 1_000_000
#: ``pullups`` values: the pod's own switched pull-ups, or the board's.
PULLUPS = ("pod", "external")

Data = Union[bytes, bytearray, memoryview, Sequence[int]]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: Any) -> bytes:
    s = str(text or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)) if s else b""


def _bytes(data: Optional[Data], name: str, limit: int) -> bytes:
    out = b"" if data is None else bytes(data)
    if len(out) > limit:
        raise ValueError(f"{name} is at most {limit} bytes, got {len(out)}")
    return out


def _addr(addr: Any) -> int:
    if isinstance(addr, bool) or not isinstance(addr, int) or not 0 <= addr <= 127:
        raise ValueError(f"addr must be a 7-bit I2C address 0..127, got {addr!r}")
    return addr


def _count(n: Any, limit: int) -> int:
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= limit:
        raise ValueError(f"read must be 0..{limit} bytes, got {n!r}")
    return n


def _reg(reg: Union[int, Data]) -> bytes:
    if isinstance(reg, int) and not isinstance(reg, bool):
        if not 0 <= reg <= 0xFF:
            raise ValueError(f"reg must be 0..255 (pass bytes for a 16-bit register), got {reg!r}")
        return bytes([reg])
    return bytes(reg)


class I2CNackError(BenchPodError):
    """A device did not acknowledge (:meth:`I2CMaster.write`, :meth:`I2CMaster.read`, ...).

    :attr:`addr` is the 7-bit address and :attr:`nack_at` which byte was not acknowledged: 0 the
    address, 1..N the Nth written byte, N+1 the address of the read after a write.
    """

    def __init__(self, addr: int, nack_at: int) -> None:
        self.addr = addr
        self.nack_at = nack_at
        what = "its address" if nack_at == 0 else f"byte {nack_at}"
        super().__init__(f"I2C device 0x{addr:02x} did not acknowledge {what} (nack_at={nack_at})")


@dataclass(frozen=True)
class I2CResult:
    """One I2C transfer as the pod reports it (:meth:`I2CMaster.xfer`)."""

    addr: int
    #: True when every byte was acknowledged.
    ack: bool
    #: The bytes read (empty when nothing was read or the device did not answer).
    data: bytes = b""
    #: Which byte was not acknowledged when ``ack`` is False (see :class:`I2CNackError`).
    nack_at: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_reply(cls, reply: Any, addr: int = 0) -> "I2CResult":
        r = reply if isinstance(reply, dict) else {}
        nack = r.get("nack_at")
        return cls(addr=int(r.get("addr", addr)), ack=bool(r.get("ack", False)),
                   data=_unb64(r.get("rx")), nack_at=None if nack is None else int(nack), raw=dict(r))

    def check(self) -> "I2CResult":
        """Raise :class:`I2CNackError` unless every byte was acknowledged; else return self."""
        if not self.ack:
            raise I2CNackError(self.addr, self.nack_at or 0)
        return self


class I2CMaster:
    """An armed I2C master (from :meth:`BenchPod.open_i2c <embeddedci.benchpod.BenchPod.open_i2c>`).
    Use it as a context manager, or call :meth:`close` to release the two pins::

        with bp.open_i2c("i2c_sda", "i2c_scl", hz=400_000) as i2c:
            print([hex(a) for a in i2c.scan()])
            chip_id = i2c.read_reg(0x76, 0xD0)[0]
    """

    def __init__(self, command: Callable[[Dict[str, Any]], Any], info: Dict[str, Any]) -> None:
        self._command = command
        self.sda = int(info.get("sda", 0))
        self.scl = int(info.get("scl", 0))
        #: The SCL rate the pod runs (at or below the one asked for). A real bus runs somewhat
        #: slower: the SCL rise time adds to each period (400 kHz measured about 333 kHz with the
        #: pod's pull-ups on a short cable).
        self.hz = int(info.get("hz", 0))
        #: ``pod`` or ``external``.
        self.pullups = str(info.get("pullups", "pod"))
        self._closed = False

    # -- lifetime ----------------------------------------------------------------------------
    def close(self) -> None:
        """Stop the I2C master and release its pins. Idempotent."""
        if not self._closed:
            self._closed = True
            self._command({"cmd": "i2c_stop"})

    def __enter__(self) -> "I2CMaster":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def status(self) -> Dict[str, Any]:
        """The pod's ``i2c_status``: ``{"active": False}`` or the session's pins, rate and pull-ups."""
        r = self._command({"cmd": "i2c_status"})
        return r if isinstance(r, dict) else {}

    # -- transfers ---------------------------------------------------------------------------
    def xfer(self, addr: int, write: Optional[Data] = None, read: int = 0) -> I2CResult:
        """Write ``write`` (up to 128 bytes), then read ``read`` bytes (up to 248) after a repeated
        START. With neither, the address alone is sent (a probe). Returns the
        :class:`I2CResult` whether or not the device acknowledged; :meth:`write`, :meth:`read` and
        :meth:`write_read` raise :class:`I2CNackError` instead."""
        req: Dict[str, Any] = {"cmd": "i2c_xfer", "addr": _addr(addr)}
        tx = _bytes(write, "write", WRITE_MAX)
        if tx:
            req["write"] = _b64(tx)
        n = _count(read, READ_MAX)
        if n:
            req["read"] = n
        return I2CResult.from_reply(self._command(req), addr)

    def write(self, addr: int, data: Data) -> None:
        """Write ``data`` to the device at ``addr``. Raises :class:`I2CNackError` on a NACK."""
        self.xfer(addr, data).check()

    def read(self, addr: int, n: int) -> bytes:
        """Read ``n`` bytes from the device at ``addr``. Raises :class:`I2CNackError` on a NACK."""
        if n < 1:
            raise ValueError("read needs at least one byte")
        return self.xfer(addr, read=n).check().data

    def write_read(self, addr: int, data: Data, n: int) -> bytes:
        """Write ``data``, then read ``n`` bytes after a repeated START (no STOP between).
        Raises :class:`I2CNackError` on a NACK."""
        if n < 1:
            raise ValueError("write_read needs at least one byte to read")
        return self.xfer(addr, data, n).check().data

    def read_reg(self, addr: int, reg: Union[int, Data], n: int = 1) -> bytes:
        """Read ``n`` bytes from register ``reg`` (an int 0..255, or bytes for a wider register
        address)."""
        return self.write_read(addr, _reg(reg), n)

    def write_reg(self, addr: int, reg: Union[int, Data], data: Data) -> None:
        """Write ``data`` to register ``reg`` (an int 0..255, or bytes for a wider address)."""
        self.write(addr, _reg(reg) + bytes(data))

    def probe(self, addr: int) -> bool:
        """True when a device acknowledges ``addr``."""
        return self.xfer(addr).ack

    def scan(self, start: int = 0x08, end: int = 0x77) -> List[int]:
        """The addresses in ``start..end`` that acknowledge (the reserved ones are left out by
        default)."""
        r = self._command({"cmd": "i2c_scan", "from": _addr(start), "to": _addr(end)})
        r = r if isinstance(r, dict) else {}
        return [int(a) for a in r.get("found") or []]

    def send(self, addr: int, write: Optional[Data] = None, read: int = 0) -> "I2CSend":
        """An :class:`I2CSend` for :meth:`BenchPod.capture_la
        <embeddedci.benchpod.BenchPod.capture_la>`: this transfer, run right after the capture arms."""
        return I2CSend(addr, write, read)


# -- send and capture -------------------------------------------------------------------------

@dataclass(frozen=True)
class I2CSend:
    """An I2C transfer for ``capture_la(send=...)``: needs an open :meth:`BenchPod.open_i2c
    <embeddedci.benchpod.BenchPod.open_i2c>` session; writes and reads up to 32 bytes each."""

    addr: int
    write: Optional[Data] = None
    read: int = 0

    def to_request(self) -> Dict[str, Any]:
        req: Dict[str, Any] = {"bus": "i2c", "addr": _addr(self.addr)}
        tx = _bytes(self.write, "write", SEND_MAX)
        if tx:
            req["write"] = _b64(tx)
        n = _count(self.read, SEND_MAX)
        if n:
            req["read"] = n
        return req


@dataclass(frozen=True)
class SpiSend:
    """One SPI frame (1..32 bytes, CS asserted around it) for ``capture_la(send=...)``: needs an
    open :meth:`BenchPod.open_spi <embeddedci.benchpod.BenchPod.open_spi>` session, CS not held."""

    tx: Data

    def to_request(self) -> Dict[str, Any]:
        tx = _bytes(self.tx, "tx", SEND_MAX)
        if not tx:
            raise ValueError("an SPI send needs at least one byte")
        return {"bus": "spi", "tx": _b64(tx)}


@dataclass(frozen=True)
class GpioSend:
    """Set a GPIO pin's level for ``capture_la(send=...)``: the pin must already be a GPIO
    ``output`` or ``open_drain`` (:meth:`BenchPod.gpio <embeddedci.benchpod.BenchPod.gpio>`).
    ``la`` is 1-14 or a wiring-profile name."""

    la: Union[int, str]
    level: int

    def to_request(self) -> Dict[str, Any]:
        if not isinstance(self.la, int) or isinstance(self.la, bool) or not 1 <= self.la <= 14:
            raise ValueError(f"la must be an LA channel 1-14, got {self.la!r}")
        if self.level not in (0, 1):
            raise ValueError(f"level must be 0 or 1, got {self.level!r}")
        return {"bus": "gpio", "la": self.la, "level": int(self.level)}


Send = Union[I2CSend, SpiSend, GpioSend]


@dataclass(frozen=True)
class SendResult:
    """What a capture's ``send`` did (:attr:`LaCapture.send_result
    <embeddedci.benchpod.results.LaCapture.send_result>`).

    ``bus`` is ``i2c``, ``spi`` or ``gpio``. ``error`` is ``""`` or why the transfer failed
    (``"SCL held low"``); the capture still returns then. For I2C, ``ack`` and ``nack_at`` are as
    in :class:`I2CResult`; ``data`` is the bytes read (I2C) or clocked in on MISO (SPI).
    """

    bus: str
    ack: Optional[bool] = None
    data: bytes = b""
    nack_at: Optional[int] = None
    la: Optional[int] = None
    level: Optional[int] = None
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def ok(self) -> bool:
        """No error, and for I2C every byte acknowledged."""
        return not self.error and self.ack is not False

    @classmethod
    def from_reply(cls, reply: Any) -> "SendResult":
        r = reply if isinstance(reply, dict) else {}
        ack = r.get("ack")
        nack = r.get("nack_at")
        la = r.get("la")
        level = r.get("level")
        return cls(bus=str(r.get("bus", "")), ack=None if ack is None else bool(ack),
                   data=_unb64(r.get("rx")), nack_at=None if nack is None else int(nack),
                   la=None if la is None else int(la), level=None if level is None else int(level),
                   error=str(r.get("error") or ""), raw=dict(r))
