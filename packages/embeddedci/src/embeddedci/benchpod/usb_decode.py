"""USB full-speed (12 Mbit/s) and low-speed (1.5 Mbit/s) decoding from a passive D+/D- tap.

The Python mirror of the EmbeddedCI server's ``decode/usb.go``: the same layers and field names,
so a capture decodes the same whether it goes through the server or stays client-side.

* packets: SYNC, PID, token fields, data and CRC5/CRC16, errors flagged per packet;
* transactions: a token (SETUP/OUT/IN/PING) with its data packet and handshake;
* transfers: control transfers (SETUP, data stage, status stage) with the standard request and
  the descriptors read back decoded, so an enumeration reads as a list.

The bit clock is recovered from the transitions (NRZI, bit stuffing after six 1s). SE0 ends a
packet (EOP) or, at 2.5 us and longer, resets the bus. Full speed idles with D+ high, low speed
with D- high. Full speed needs 48 MS/s (decodes from about 32 MS/s), low speed 6 MS/s.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

FULL_SPEED_BPS = 12e6
LOW_SPEED_BPS = 1.5e6
RESET_US = 2.5

_SE0, _J, _K, _SE1 = 0, 1, 2, 3


@dataclass
class UsbPacket:
    """One packet, or a bus event: ``kind`` is ``packet``, ``reset``, ``keep_alive`` or ``se1``."""

    index: int
    start_us: float
    end_us: float
    kind: str
    pid: str = ""
    pid_value: int = 0
    addr: Optional[int] = None
    endpoint: Optional[int] = None
    frame: Optional[int] = None
    data_hex: str = ""
    crc: str = ""
    ok: bool = True
    error: str = ""
    data: bytes = field(default=b"", repr=False)


@dataclass
class UsbSetup:
    """A SETUP packet's 8 bytes, decoded."""

    bm_request_type: int
    b_request: int
    w_value: int
    w_index: int
    w_length: int
    direction: str
    type: str
    recipient: str
    name: str
    summary: str


@dataclass
class UsbTransaction:
    """A token, its data packet and handshake. Repeated NAKed polls merge into one (``count``)."""

    index: int
    start_us: float
    end_us: float
    type: str
    addr: int = 0
    endpoint: int = 0
    data_pid: str = ""
    data_hex: str = ""
    data_len: int = 0
    handshake: str = ""
    count: int = 1
    setup: Optional[UsbSetup] = None
    packets: List[int] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    data: bytes = field(default=b"", repr=False)


@dataclass
class UsbTransfer:
    """One control transfer; ``status`` is ``OK``, ``STALL`` or ``incomplete``."""

    index: int
    start_us: float
    end_us: float
    addr: int
    endpoint: int
    request: str
    setup: UsbSetup
    data_hex: str = ""
    data_len: int = 0
    status: str = ""
    detail: str = ""
    ok: bool = False
    data: bytes = field(default=b"", repr=False)


@dataclass
class UsbDecode:
    """The whole decode, in the shape of the server's ``POST /analyzer/decode`` for ``usb``."""

    speed: str
    packets: List[UsbPacket] = field(default_factory=list)
    transactions: List[UsbTransaction] = field(default_factory=list)
    transfers: List[UsbTransfer] = field(default_factory=list)
    sof_count: int = 0
    keep_alive_count: int = 0
    error_count: int = 0
    protocol: str = "usb"

    def __len__(self) -> int:
        return len(self.packets)

    def as_dict(self) -> dict:
        """JSON-ready, with the server's keys (no raw ``data`` bytes, no empty optional fields)."""

        def clean(obj):
            d = dataclasses.asdict(obj)
            d.pop("data", None)
            return d

        out = {"protocol": "usb", "speed": self.speed, "sof_count": self.sof_count,
               "keep_alive_count": self.keep_alive_count, "error_count": self.error_count}
        out["packets"] = [{k: v for k, v in clean(p).items() if v not in (None, "")} for p in self.packets]
        out["transactions"] = [clean(t) for t in self.transactions]
        out["transfers"] = [clean(t) for t in self.transfers]
        return out


def decode_usb(words: Sequence[int], *, dp: int, dm: int, sample_rate_hz: float,
               speed: str = "auto") -> UsbDecode:
    """Decode USB from LA channels ``dp`` (D+) and ``dm`` (D-), 1-based.

    ``speed`` is ``full``, ``low`` or ``auto`` (from the idle state).
    """
    if not (1 <= dp <= 14 and 1 <= dm <= 14) or dp == dm:
        raise ValueError("usb decode needs two different channels for D+ and D- in 1..14")
    if sample_rate_hz <= 0:
        raise ValueError("usb decode needs a positive sample rate")
    sp = (speed or "auto").strip().lower()
    if sp == "auto":
        sp = _detect_speed(words, dp, dm)
    elif sp in ("full", "fs", "12m"):
        sp = "full"
    elif sp in ("low", "ls", "1.5m"):
        sp = "low"
    else:
        raise ValueError(f"usb speed must be full, low or auto, got {speed!r}")
    bps = LOW_SPEED_BPS if sp == "low" else FULL_SPEED_BPS
    spb = sample_rate_hz / bps
    if spb < 2.5:  # below ~2.7 samples a bit the run lengths round wrong
        need = "6 MS/s" if sp == "low" else "48 MS/s"
        raise ValueError(f"sample rate {sample_rate_hz:.0f} Hz is too low for {sp}-speed USB: "
                         f"capture at {need} or more")
    runs = _merge_glitches(_runs(words, dp, dm, sp == "low"), spb)
    res = UsbDecode(speed=sp)
    _packets(res, runs, spb, 1e6 / sample_rate_hz)
    res.transactions = _transactions(res.packets)
    res.transfers = _transfers(res.transactions)
    res.error_count = sum(1 for p in res.packets if not p.ok)
    return res


def _detect_speed(words: Sequence[int], dp: int, dm: int) -> str:
    pb, mb = dp - 1, dm - 1
    dp_high = dm_high = 0
    for w in words:
        p, m = (w >> pb) & 1, (w >> mb) & 1
        if p and not m:
            dp_high += 1
        elif m and not p:
            dm_high += 1
    return "low" if dm_high > dp_high * 3 // 2 else "full"


def _runs(words: Sequence[int], dp: int, dm: int, low: bool) -> List[List[float]]:
    """Runs of one line state: [state, start sample, samples]."""
    pb, mb = dp - 1, dm - 1
    j_state, k_state = (_K, _J) if low else (_J, _K)
    runs: List[List[float]] = []
    prev_w = None
    for i, w in enumerate(words):
        if w == prev_w:
            runs[-1][2] += 1
            continue
        prev_w = w
        p, m = (w >> pb) & 1, (w >> mb) & 1
        s = _SE1 if p and m else j_state if p else k_state if m else _SE0
        if runs and runs[-1][0] == s:
            runs[-1][2] += 1
        else:
            runs.append([s, float(i), 1.0])
    return runs


def _merge_glitches(runs: List[List[float]], spb: float) -> List[List[float]]:
    """Fold the short SE0/SE1 the lines show while they cross into their neighbors."""
    limit = max(1.0, float(int(spb / 2)))
    out: List[List[float]] = []
    i = 0
    while i < len(runs):
        r = runs[i]
        short = r[0] in (_SE0, _SE1) and r[2] <= limit
        if short and out and i + 1 < len(runs) and out[-1][0] in (_J, _K):
            prev, nxt = out[-1], runs[i + 1]
            if nxt[0] == prev[0]:
                prev[2] += r[2] + nxt[2]
                i += 2
                continue
            if nxt[0] in (_J, _K):
                prev[2] += r[2] / 2
                nxt[1] -= r[2] / 2
                nxt[2] += r[2] / 2
                i += 1
                continue
        if out and out[-1][0] == r[0]:
            out[-1][2] += r[2]
        else:
            out.append(list(r))
        i += 1
    return out


def _join(a: str, b: str) -> str:
    return b if not a else f"{a}, {b}"


def _hex(b: bytes) -> str:
    return " ".join(f"{v:02X}" for v in b)


_PID_NAMES = {0x1: "OUT", 0x9: "IN", 0x5: "SOF", 0xD: "SETUP", 0x3: "DATA0", 0xB: "DATA1",
              0x7: "DATA2", 0xF: "MDATA", 0x2: "ACK", 0xA: "NAK", 0xE: "STALL", 0x6: "NYET",
              0xC: "PRE", 0x8: "SPLIT", 0x4: "PING", 0x0: "RESERVED"}
_DATA = ("DATA0", "DATA1", "DATA2", "MDATA")
_HANDSHAKE = ("ACK", "NAK", "STALL", "NYET")


def crc5_field(field_: int) -> int:
    """The CRC5 of an 11-bit token field as it sits in the packet's bits 11-15."""
    crc = 0x1F
    for b in range(11):
        bit = (field_ >> b) & 1
        crc = (((crc << 1) ^ 0x05) if ((crc >> 4) & 1) ^ bit else (crc << 1)) & 0x1F
    crc = ~crc & 0x1F
    return int(f"{crc:05b}"[::-1], 2)


def crc16(data: bytes) -> int:
    """The data packet CRC16, sent low byte first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


def _packets(res: UsbDecode, runs: List[List[float]], spb: float, us: float) -> None:
    reset_samples = RESET_US / us

    def add(p: UsbPacket) -> None:
        p.index = len(res.packets) + 1
        res.packets.append(p)

    i, n = 0, len(runs)
    while i < n:
        st, start, length = runs[i]
        if st == _J:
            i += 1
            continue
        if st == _SE1:
            if length > spb:
                add(UsbPacket(0, start * us, (start + length) * us, "se1", ok=False,
                              error="SE1: D+ and D- both high"))
            i += 1
            continue
        if st == _SE0:
            if length >= reset_samples:
                add(UsbPacket(0, start * us, (start + length) * us, "reset"))
            elif i > 0 and runs[i - 1][0] == _J and res.speed == "low":
                res.keep_alive_count += 1
                add(UsbPacket(0, start * us, (start + length) * us, "keep_alive"))
            i += 1
            continue
        if i == 0:
            while i < n and runs[i][0] != _SE0:
                i += 1
            continue
        if runs[i - 1][0] not in (_J, _SE0):
            i += 1
            continue
        j = i
        while j < n and runs[j][0] in (_J, _K):
            if runs[j][0] == _J and runs[j][2] > 8.5 * spb:
                break
            j += 1
        end = j
        p = _packet_from_runs(runs[i:end], spb)
        p.start_us = runs[i][1] * us
        last = runs[end - 1]
        end_sample = last[1] + last[2]
        if end < n and runs[end][0] == _SE0 and runs[end][2] < reset_samples:
            end_sample = runs[end][1] + runs[end][2]
        elif end >= n:
            p.ok = False
            p.error = _join(p.error, "capture ended inside the packet")
        elif runs[end][0] != _SE0:
            p.ok = False
            p.error = _join(p.error, "no EOP")
        p.end_us = end_sample * us
        if p.pid == "SOF":
            res.sof_count += 1
        add(p)
        i = end
        if end < n and runs[end][0] == _SE0 and runs[end][2] < reset_samples:
            i += 1


def _packet_from_runs(runs: List[List[float]], spb: float) -> UsbPacket:
    p = UsbPacket(0, 0.0, 0.0, "packet")
    raw: List[int] = []
    for r in runs:
        nb = max(1, int(_round(r[2] / spb)))
        raw.append(0)
        raw.extend([1] * (nb - 1))
    bits: List[int] = []
    ones = 0
    stuff_err = False
    for b in raw:
        if ones == 6:
            ones = 0
            if b == 0:
                continue
            stuff_err = True
        bits.append(b)
        ones = ones + 1 if b else 0
    z = 0
    while z < len(bits) and bits[z] == 0:
        z += 1
    if z >= len(bits):
        p.ok, p.error = False, "no SYNC"
        return p
    err = ""
    if z < 3 or z > 7:
        err = _join(err, "bad SYNC")
    payload = bits[z + 1:]
    n_bytes, left = divmod(len(payload), 8)
    if left > 1:
        err = _join(err, "partial byte")
    if stuff_err:
        err = _join(err, "bit stuffing")
    data = bytes(sum(payload[k * 8 + b] << b for b in range(8)) for k in range(n_bytes))
    if not data:
        p.ok, p.error = False, _join(err, "no PID")
        return p
    pid = data[0]
    p.pid_value = pid
    p.pid = _PID_NAMES[pid & 0xF]
    if pid >> 4 != (~pid) & 0xF:
        err = _join(err, "bad PID check")
    body = data[1:]
    if p.pid in ("OUT", "IN", "SETUP", "PING", "SOF"):
        if len(body) != 2:
            err = _join(err, f"bad length: {len(body)} bytes after the PID, want 2")
        else:
            w = body[0] | body[1] << 8
            fld, crc = w & 0x7FF, w >> 11
            p.crc = f"0x{crc:02X}"
            if p.pid == "SOF":
                p.frame = fld
            else:
                p.addr, p.endpoint = fld & 0x7F, fld >> 7
            if crc5_field(fld) != crc:
                err = _join(err, "bad CRC5")
    elif p.pid in _DATA:
        if len(body) < 2:
            err = _join(err, "bad length: no CRC16")
        else:
            pay = bytes(body[:-2])
            got = body[-2] | body[-1] << 8
            p.crc = f"0x{got:04X}"
            p.data = pay
            p.data_hex = _hex(pay)
            if crc16(pay) != got:
                err = _join(err, "bad CRC16")
    elif p.pid in _HANDSHAKE and body:
        err = _join(err, f"bad length: {len(body)} bytes after a handshake")
    p.error = err
    p.ok = not err
    return p


def _round(x: float) -> float:
    """Round half away from zero, as Go's math.Round (Python's round() goes to even)."""
    return float(int(x + 0.5)) if x >= 0 else -float(int(-x + 0.5))


def _transactions(pk: List[UsbPacket]) -> List[UsbTransaction]:
    out: List[UsbTransaction] = []
    lst = [p for p in pk if p.kind != "keep_alive" and p.pid != "SOF"]

    def add(t: UsbTransaction) -> None:
        if out and t.handshake == "NAK" and t.ok and t.data_len == 0 and t.setup is None:
            prev = out[-1]
            if (prev.handshake == "NAK" and prev.ok and prev.type == t.type and prev.addr == t.addr
                    and prev.endpoint == t.endpoint and prev.data_len == 0):
                prev.count += 1
                prev.end_us = t.end_us
                prev.packets.extend(t.packets)
                return
        t.index = len(out) + 1
        out.append(t)

    li = 0
    while li < len(lst):
        p = lst[li]
        nxt = lst[li + 1] if li + 1 < len(lst) and lst[li + 1].kind == "packet" else None
        if p.kind == "reset":
            add(UsbTransaction(0, p.start_us, p.end_us, "RESET", packets=[p.index]))
        elif p.kind != "packet":
            add(UsbTransaction(0, p.start_us, p.end_us, p.kind.upper(), packets=[p.index], ok=False,
                               error=p.error))
        elif p.pid in ("SETUP", "OUT", "IN", "PING"):
            t = UsbTransaction(0, p.start_us, p.end_us, p.pid, packets=[p.index], ok=p.ok, error=p.error)
            if p.addr is not None:
                t.addr, t.endpoint = p.addr, p.endpoint or 0
            if p.pid != "PING":
                if nxt is not None and nxt.pid in _DATA:
                    li += 1
                    t.data_pid, t.data, t.data_hex, t.data_len = nxt.pid, nxt.data, nxt.data_hex, len(nxt.data)
                    t.end_us = nxt.end_us
                    t.packets.append(nxt.index)
                    if not nxt.ok:
                        t.ok = False
                        t.error = _join(t.error, f"{nxt.pid}: {nxt.error}")
                elif p.pid != "IN":
                    t.ok = False
                    t.error = _join(t.error, "no data packet")
            nxt = lst[li + 1] if li + 1 < len(lst) and lst[li + 1].kind == "packet" else None
            if nxt is not None and nxt.pid in _HANDSHAKE:
                li += 1
                t.handshake = nxt.pid
                t.end_us = nxt.end_us
                t.packets.append(nxt.index)
                if not nxt.ok:
                    t.ok = False
                    t.error = _join(t.error, f"{nxt.pid}: {nxt.error}")
            if p.pid == "SETUP" and len(t.data) == 8:
                t.setup = parse_setup(t.data)
            add(t)
        else:
            t = UsbTransaction(0, p.start_us, p.end_us, p.pid, packets=[p.index], ok=False,
                               error=_join(p.error, f"no token before {p.pid}"))
            if p.pid in _DATA:
                t.data_pid, t.data, t.data_hex, t.data_len = p.pid, p.data, p.data_hex, len(p.data)
            add(t)
        li += 1
    return out


_STD_REQUESTS = {0: "GET_STATUS", 1: "CLEAR_FEATURE", 3: "SET_FEATURE", 5: "SET_ADDRESS",
                 6: "GET_DESCRIPTOR", 7: "SET_DESCRIPTOR", 8: "GET_CONFIGURATION",
                 9: "SET_CONFIGURATION", 10: "GET_INTERFACE", 11: "SET_INTERFACE", 12: "SYNCH_FRAME"}
_CLASS_REQUESTS = {0x01: "GET_REPORT", 0x02: "GET_IDLE", 0x03: "GET_PROTOCOL", 0x09: "SET_REPORT",
                   0x0A: "SET_IDLE", 0x0B: "SET_PROTOCOL", 0x20: "SET_LINE_CODING",
                   0x21: "GET_LINE_CODING", 0x22: "SET_CONTROL_LINE_STATE", 0x23: "SEND_BREAK",
                   0xFE: "GET_MAX_LUN", 0xFF: "BULK_ONLY_RESET"}
_DESC_TYPES = {1: "DEVICE", 2: "CONFIGURATION", 3: "STRING", 4: "INTERFACE", 5: "ENDPOINT",
               6: "DEVICE_QUALIFIER", 7: "OTHER_SPEED_CONFIGURATION", 8: "INTERFACE_POWER",
               0x0B: "INTERFACE_ASSOCIATION", 0x0F: "BOS", 0x21: "HID", 0x22: "HID_REPORT", 0x29: "HUB"}


def _desc_name(t: int) -> str:
    return _DESC_TYPES.get(t, f"type 0x{t:02X}")


def parse_setup(d: bytes) -> UsbSetup:
    """Decode a SETUP packet's 8 bytes."""
    rt, rq = d[0], d[1]
    value, index, length = d[2] | d[3] << 8, d[4] | d[5] << 8, d[6] | d[7] << 8
    direction = "in" if rt & 0x80 else "out"
    typ = ("standard", "class", "vendor", "reserved")[(rt >> 5) & 3]
    recipient = {0: "device", 1: "interface", 2: "endpoint"}.get(rt & 0x1F, "other")
    if typ == "standard":
        name = _STD_REQUESTS.get(rq, f"request 0x{rq:02X}")
    elif typ == "class":
        name = f"class {_CLASS_REQUESTS[rq]}" if rq in _CLASS_REQUESTS else f"class request 0x{rq:02X}"
    else:
        name = f"{typ} request 0x{rq:02X}"
    s = name
    if typ == "standard":
        if rq in (6, 7):
            dt, di = value >> 8, value & 0xFF
            s += " " + _desc_name(dt)
            if di != 0 or dt == 3:
                s += f" index {di}"
            if dt == 3 and index != 0:
                s += f" lang 0x{index:04X}"
        elif rq in (5, 9):
            s += f" {value}"
        elif rq == 11:
            s += f" interface {index} alt {value}"
        elif rq in (1, 3):
            if recipient == "endpoint" and value == 0:
                s += f" ENDPOINT_HALT ep 0x{index:02X}"
            elif recipient == "device" and value == 1:
                s += " DEVICE_REMOTE_WAKEUP"
            else:
                s += f" {value}"
        elif rq in (0, 10) and recipient != "device":
            s += f" {recipient} {index}"
        if rq in (6, 0, 8, 10):
            s += f" length {length}"
    else:
        s += f" value 0x{value:04X} index 0x{index:04X} length {length}"
    return UsbSetup(rt, rq, value, index, length, direction, typ, recipient, name, s)


def _transfers(tx: List[UsbTransaction]) -> List[UsbTransfer]:
    out: List[UsbTransfer] = []
    # (addr, endpoint) -> [transfer, last data PID, got data, data stage done, data]
    open_: Dict[Tuple[int, int], list] = {}

    def finish(k: Tuple[int, int], status: str) -> None:
        p = open_.pop(k, None)
        if p is None:
            return
        t: UsbTransfer = p[0]
        t.data = bytes(p[4])
        t.status, t.ok = status, status == "OK"
        t.data_len, t.data_hex = len(t.data), _hex(t.data)
        if t.ok and t.setup.direction == "in":
            t.detail = describe(t.setup, t.data)
        out.append(t)

    for t in tx:
        if t.type == "RESET":
            for k in list(open_):
                finish(k, "incomplete")
            continue
        k = (t.addr, t.endpoint)
        if t.type == "SETUP":
            if t.setup is None or t.handshake != "ACK" or not t.ok:
                continue
            if k in open_:
                finish(k, "incomplete")
            open_[k] = [UsbTransfer(0, t.start_us, t.end_us, t.addr, t.endpoint, t.setup.summary, t.setup),
                        "", False, t.setup.w_length == 0, bytearray()]
            continue
        p = open_.get(k)
        if p is None or t.type not in ("IN", "OUT") or not t.ok:
            continue
        xfer: UsbTransfer = p[0]
        data_dir = xfer.setup.direction.upper()
        if t.handshake == "STALL":
            xfer.end_us = t.end_us
            finish(k, "STALL")
            continue
        if t.handshake != "ACK":
            continue
        if not p[3] and t.type == data_dir:
            if not (p[2] and t.data_pid == p[1]):
                p[4].extend(t.data)
            p[2], p[1] = True, t.data_pid
            xfer.end_us = t.end_us
            if len(p[4]) >= xfer.setup.w_length:
                p[3] = True
            continue
        if (t.type != data_dir or p[3]) and t.data_len == 0 and (t.type != data_dir or xfer.setup.w_length == 0):
            xfer.end_us = t.end_us
            finish(k, "OK")
    for k in list(open_):
        finish(k, "incomplete")
    out.sort(key=lambda x: x.start_us)
    for n, x in enumerate(out):
        x.index = n + 1
    return out


def describe(s: UsbSetup, d: bytes) -> str:
    """A descriptor that GET_DESCRIPTOR read back, decoded."""
    if s.type != "standard" or s.b_request != 6 or len(d) < 2:
        return ""
    dt = s.w_value >> 8
    if dt == 1:
        if len(d) < 8:
            return ""
        parts = [f"USB {d[3]:x}.{d[2]:02x}"]
        if d[4]:
            parts.append(f"class 0x{d[4]:02X}")
        parts.append(f"max packet {d[7]}")
        if len(d) >= 12:
            parts.append(f"VID 0x{d[8] | d[9] << 8:04X} PID 0x{d[10] | d[11] << 8:04X}")
        if len(d) >= 18:
            parts.append(f"{d[17]} configuration(s)")
        return ", ".join(parts)
    if dt == 2:
        if len(d) < 9:
            return ""
        parts = [f"total length {d[2] | d[3] << 8}, {d[4]} interface(s), {d[8] * 2} mA"]
        o = d[0]
        while o + 1 < len(d) and d[o] >= 2:
            ln = d[o]
            if o + ln > len(d):
                break
            if d[o + 1] == 4 and ln >= 9:
                parts.append(f"interface {d[o + 2]} alt {d[o + 3]} class 0x{d[o + 5]:02X}")
            elif d[o + 1] == 5 and ln >= 7:
                kind = ("control", "isochronous", "bulk", "interrupt")[d[o + 3] & 3]
                parts.append(f"endpoint 0x{d[o + 2]:02X} {kind} {d[o + 4] | d[o + 5] << 8} bytes")
            o += ln
        return "; ".join(parts)
    if dt == 3:
        units = [d[o] | d[o + 1] << 8 for o in range(2, len(d) - 1, 2) if o + 1 < d[0]]
        if s.w_value & 0xFF == 0:
            return "languages " + " ".join(f"0x{u:04X}" for u in units)
        text = "".join(chr(u) for u in units)
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return ""


def format_transfers(res: UsbDecode) -> List[str]:
    """One line per control transfer: what an enumeration looks like."""
    lines = []
    for t in res.transfers:
        line = f"{t.start_us / 1e6:.6f}s addr {t.addr} {t.request}: {t.status}"
        if t.detail:
            line += f" ({t.detail})"
        elif t.data_hex:
            line += f" data {t.data_hex}"
        lines.append(line)
    return lines


def format_transactions(res: UsbDecode) -> List[str]:
    """One line per transaction (token, data, handshake), NAKed polls merged."""
    lines = []
    for t in res.transactions:
        if t.type == "RESET":
            lines.append(f"{t.start_us / 1e6:.6f}s bus reset {t.end_us - t.start_us:.1f} us")
            continue
        line = f"{t.start_us / 1e6:.6f}s {t.type} addr {t.addr} ep {t.endpoint}"
        if t.data_pid:
            line += f" {t.data_pid} [{t.data_len}] {t.data_hex}".rstrip()
        line += f" {t.handshake or 'no handshake'}"
        if t.count > 1:
            line += f" x{t.count}"
        if t.setup is not None:
            line += f" ({t.setup.summary})"
        if not t.ok:
            line += f" error={t.error}"
        lines.append(line)
    return lines
