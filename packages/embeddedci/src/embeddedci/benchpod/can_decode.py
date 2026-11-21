"""CAN 2.0 and ISO CAN FD decoding from one LA channel (the transceiver's RXD, low = dominant).

The Python mirror of the EmbeddedCI server's ``decode/can.go`` (held to it by
``can_decode_vectors.json``). Bits are sampled at the sample point and re-synchronized on falling edges
near a bit boundary; a frame starts on a falling edge after 9.5 or more recessive bits. Dynamic
stuffing runs from SOF through the data field (classic: through the CRC); CAN FD switches to the
data bit rate at the BRS sample point and back at the CRC delimiter's, and its CRC17/CRC21 covers
the dynamic stuff bits and the stuff count (ISO 11898-1:2015).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import List, Sequence

from ._gojson import go_dict

FD_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


@dataclass
class CanFrame:
    """One frame; ``kind`` is ``data``, ``remote`` or ``error``."""

    index: int
    start_us: float
    end_us: float
    kind: str = "data"
    id: int = 0
    id_hex: str = ""
    extended: bool = False
    fd: bool = False
    brs: bool = False
    esi: bool = False
    dlc: int = 0
    len: int = 0
    data_hex: str = ""
    crc: str = ""
    ack: bool = False
    stuff_bits: int = 0
    ok: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return go_dict(self, omit_empty=("error",))


def _join(a: str, b: str) -> str:
    return b if not a else f"{a}, {b}"


class _Reader:
    def __init__(self, line, edges, pos, spb, sp):
        self.line, self.edges, self.pos, self.spb, self.sp = line, edges, pos, spb, sp
        self.n = len(line)
        self.ended = False
        self.last_sp = 0.0

    def bit(self) -> int:
        # Only recessive-to-dominant edges re-synchronize: RXD stretches dominant bits.
        win = 0.35 * self.spb
        k = bisect.bisect_left(self.edges, int(math.ceil(self.pos - win)))
        while k < len(self.edges) and self.edges[k] <= self.pos + win:
            if self.line[self.edges[k]] == 0:
                self.pos = float(self.edges[k])
                break
            k += 1
        s = self.pos + self.sp * self.spb
        self.last_sp = s
        idx = int(s)
        if idx >= self.n:
            self.ended = True
            return 1
        self.pos += self.spb
        return self.line[idx]

    def switch_rate(self, spb: float, sp: float) -> None:
        self.pos = self.last_sp + (1 - sp) * spb
        self.spb, self.sp = spb, sp


class _Stuffed:
    def __init__(self, r: _Reader):
        self.r = r
        self.raw: List[int] = []
        self.bits: List[int] = []
        self.run = 0
        self.last = 0
        self.stuffed = 0
        self.err = ""
        # For the bit just read: does it end the FD data field? Then no stuff bit follows it, the
        # CRC field's fixed stuff bit stands instead (ISO 11898-1:2015, 10.5).
        self.final = None

    def raw_bit(self) -> int:
        b = self.r.bit()
        if self.raw and b == self.last:
            self.run += 1
        else:
            self.run = 1
        self.last = b
        self.raw.append(b)
        return b

    def get(self) -> int:
        b = self.raw_bit()
        if self.run == 6:
            self.err = "stuff error"
        self.bits.append(b)
        if self.run == 5 and (self.final is None or not self.final(b)):
            s = self.raw_bit()
            if self.run == 6:
                self.err = "stuff error"
            self.stuffed += 1
            self.run = 1
            self.last = s
        return b

    def field(self, n: int) -> int:
        v = 0
        k = 0
        while k < n and not self.err and not self.r.ended:
            v = v << 1 | self.get()
            k += 1
        return v


def can_crc(bits: Sequence[int], width: int, poly: int, init: int) -> int:
    """The CAN CRC of ``bits``: CRC15 (0x4599, init 0), CRC17 (0x1685B, init 1<<16), CRC21
    (0x102899, init 1<<20)."""
    crc = init
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    for b in bits:
        nxt = b ^ ((crc & top) >> (width - 1))
        crc = (crc << 1) & mask
        if nxt:
            crc ^= poly
    return crc


def stuff_count(n: int) -> List[int]:
    """The FD stuff count field: Gray code of n mod 8, then even parity."""
    g = (n % 8) ^ ((n % 8) >> 1)
    b = [g >> 2 & 1, g >> 1 & 1, g & 1]
    return b + [(b[0] + b[1] + b[2]) & 1]


def decode_can(words: Sequence[int], *, can: int, sample_rate_hz: float, bitrate: float,
               data_bitrate: float = 0.0, sample_point: float = 0.0,
               data_sample_point: float = 0.0) -> List[CanFrame]:
    """Decode CAN on channel ``can``. ``data_bitrate`` is the CAN FD data rate (0: nominal);
    sample points are fractions of a bit (0: 0.75)."""
    if not 1 <= can <= 14:
        raise ValueError("can decode needs a channel in 1..14")
    if sample_rate_hz <= 0 or bitrate <= 0:
        raise ValueError("can decode needs a positive sample rate and bit rate")
    if data_bitrate <= 0:
        data_bitrate = bitrate
    if not 0 < sample_point < 1:
        sample_point = 0.75
    if not 0 < data_sample_point < 1:
        data_sample_point = 0.75
    spb_n, spb_d = sample_rate_hz / bitrate, sample_rate_hz / data_bitrate
    top = max(bitrate, data_bitrate)
    if sample_rate_hz / top < 4:
        raise ValueError(f"sample rate {sample_rate_hz:.0f} Hz is too low for {top:.0f} bit/s CAN: "
                         f"capture at {6 * top:.0f} S/s or more")
    bit = can - 1
    line = [(w >> bit) & 1 for w in words]
    edges = [i for i in range(1, len(line)) if line[i] != line[i - 1]]
    us = 1e6 / sample_rate_hz
    out: List[CanFrame] = []
    idle_need = 9.5 * spb_n
    last_rec = 0.0
    if line and line[0] == 0:
        last_rec = math.inf
    k = 0
    while k < len(edges):
        e = edges[k]
        if line[e] == 1:
            last_rec = float(e)
            k += 1
            continue
        idle = e - last_rec
        if not (idle >= idle_need or (last_rec == 0 and idle >= 2 * spb_n)):
            k += 1
            continue
        r = _Reader(line, edges, float(e), spb_n, sample_point)
        f = _frame(r, spb_n, spb_d, sample_point, data_sample_point)
        f.start_us = e * us
        f.end_us = r.pos * us
        f.index = len(out) + 1
        out.append(f)
        while k + 1 < len(edges) and edges[k + 1] < r.pos - 0.5 * spb_n:
            k += 1
        last_rec = float(edges[k]) if line[edges[k]] == 1 else math.inf
        k += 1
    return out


def _hex(b) -> str:
    return " ".join(f"{v:02X}" for v in b)


def _frame(r: _Reader, spb_n, spb_d, sp_n, sp_d) -> CanFrame:
    c = _Stuffed(r)
    f = CanFrame(0, 0.0, 0.0)

    def fail(msg: str) -> CanFrame:
        f.ok = False
        f.error = msg
        if msg == "stuff error" and c.last == 0:
            while c.r.bit() == 0 and not c.r.ended:
                pass
            f.kind = "error"
            f.error = "stuff error, error frame"
        f.stuff_bits = c.stuffed
        return f

    def end() -> bool:
        return bool(c.err) or r.ended

    def why() -> str:
        return c.err or "capture ended inside the frame"

    c.field(1)
    ident = c.field(11)
    b1 = c.field(1)
    ide = c.field(1)
    rtr = False
    if ide == 0:
        f.id = ident
        f.id_hex = f"0x{ident:03X}"
        if c.field(1) == 1:
            f.fd = True
        else:
            rtr = b1 == 1
    else:
        f.extended = True
        ext = c.field(18)
        f.id = ident << 18 | ext
        f.id_hex = f"0x{f.id:08X}"
        b2 = c.field(1)
        if c.field(1) == 1:
            f.fd = True
        else:
            rtr = b2 == 1
            c.field(1)
    if end():
        return fail(why())
    if f.fd:
        c.field(1)
        f.brs = c.field(1) == 1
        if f.brs:
            r.switch_rate(spb_d, sp_d)
        f.esi = c.field(1) == 1
    f.dlc = c.field(3) << 1
    if f.fd and f.dlc == 0:
        c.final = lambda b: b == 0
    f.dlc |= c.field(1)
    c.final = None
    if end():
        return fail(why())
    f.len = FD_LEN[f.dlc] if f.fd else min(f.dlc, 8)
    if rtr:
        f.kind = "remote"
        f.len = 0
    data = []
    for k in range(f.len):
        if end():
            break
        if f.fd and k == f.len - 1:
            v = c.field(7) << 1
            c.final = lambda b: True
            v |= c.field(1)
            c.final = None
            data.append(v)
            continue
        data.append(c.field(8))
    if end():
        return fail(why())
    f.data_hex = _hex(data)
    if not f.fd:
        want = can_crc(c.bits, 15, 0x4599, 0)
        got = c.field(15)
        if end():
            return fail(why())
        f.crc = f"0x{got:04X}"
        crc_ok = got == want
    else:
        covered = list(c.raw)
        width, poly, init = (21, 0x102899, 1 << 20) if f.len > 16 else (17, 0x1685B, 1 << 16)
        prev = c.raw[-1]
        seq: List[int] = []
        fixed_err = False
        for k in range(4 + width):
            if r.ended:
                break
            if k % 4 == 0:
                fsb = r.bit()
                if fsb == prev:
                    fixed_err = True
                prev = fsb
            b = r.bit()
            seq.append(b)
            prev = b
        if r.ended:
            return fail(why())
        sc = seq[:4]
        got = 0
        for b in seq[4:]:
            got = got << 1 | b
        f.crc = f"0x{got:06X}"
        want = can_crc(covered + sc, width, poly, init)
        crc_ok = got == want
        if fixed_err:
            f.error = _join(f.error, "fixed stuff bit")
        if sc != stuff_count(c.stuffed):
            f.error = _join(f.error, "bad stuff count")
    f.stuff_bits = c.stuffed
    if not crc_ok:
        f.error = _join(f.error, "bad CRC")
    if r.bit() != 1:
        f.error = _join(f.error, "CRC delimiter dominant")
    if f.brs:
        r.switch_rate(spb_n, sp_n)
    # CAN FD: a second delimiter bit before the ACK, and an ACK up to two bits long.
    f.ack = r.bit() == 0
    if not f.ack and f.fd:
        f.ack = r.bit() == 0
    nxt = r.bit()
    if f.ack and f.fd and nxt == 0:
        nxt = r.bit()
    if nxt != 1:
        f.error = _join(f.error, "ACK delimiter dominant")
    for _ in range(7):
        if r.ended:
            break
        if r.bit() != 1:
            f.error = _join(f.error, "EOF dominant")
            break
    if r.ended:
        f.error = _join(f.error, "capture ended inside the frame")
    if not f.ack:
        f.error = _join(f.error, "no ACK")
    f.ok = not f.error
    return f
