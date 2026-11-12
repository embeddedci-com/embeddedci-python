"""Decode the pod's raw I2C-bus capture (``sensor_la``) into transactions.

``sensor_la`` returns packed bytes — **4 samples per byte, oldest in the high
nibble**, each sample two bits ``{SCL(hi), SDA(lo)}`` (see
``ice40/src/i2c_la_capture.v``)::

    bit7=SCL(s0) bit6=SDA(s0)  bit5=SCL(s1) bit4=SDA(s1)
    bit3=SCL(s2) bit2=SDA(s2)  bit1=SCL(s3) bit0=SDA(s3)

This module unpacks that into a chronological ``(scl, sda)`` stream and runs an
edge-based I2C protocol decoder over it: START/STOP (SDA edge while SCL high),
data bits (sampled on SCL rising edges), and the 9th ACK/NACK bit. The result is
a list of :class:`I2CTransaction` you can assert on — e.g. "the DUT addressed
0x76, wrote register 0xD0, and read back 0x58 (the BMP280 chip id)".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

Sample = Tuple[int, int]  # (scl, sda)


@dataclass
class I2CByte:
    """One byte on the bus plus the ACK bit that followed it."""

    value: int
    ack: bool  # True = ACK (SDA pulled low on the 9th clock), False = NACK

    def __str__(self) -> str:
        return f"0x{self.value:02X}{'+' if self.ack else '-'}"


@dataclass
class I2CMessage:
    """An address byte and the data bytes that followed, until a (re)START/STOP."""

    address: Optional[int]      # 7-bit address (None if the stream began mid-message)
    read: Optional[bool]        # True = read (R/W bit set), False = write
    address_ack: Optional[bool]
    data: List[I2CByte] = field(default_factory=list)

    @property
    def values(self) -> List[int]:
        return [b.value for b in self.data]


@dataclass
class I2CTransaction:
    """Everything between a START and its STOP (may hold repeated-START messages)."""

    messages: List[I2CMessage] = field(default_factory=list)
    complete: bool = False  # True if a STOP was seen

    @property
    def address(self) -> Optional[int]:
        return self.messages[0].address if self.messages else None

    def to(self, address: int) -> bool:
        return any(m.address == address for m in self.messages)


# --------------------------------------------------------------------------
# Packing <-> samples
# --------------------------------------------------------------------------

def unpack_samples(packed: Iterable[int]) -> List[Sample]:
    """Expand packed capture bytes into a chronological ``(scl, sda)`` list."""
    out: List[Sample] = []
    for byte in packed:
        for k in range(4):                       # k=0 oldest .. 3 newest
            pair = (byte >> ((3 - k) * 2)) & 0x3
            out.append(((pair >> 1) & 1, pair & 1))
    return out


def pack_samples(samples: Sequence[Sample]) -> bytes:
    """Inverse of :func:`unpack_samples` (pads a short last byte with idle bits)."""
    out = bytearray()
    for i in range(0, len(samples), 4):
        group = list(samples[i:i + 4])
        while len(group) < 4:
            group.append((1, 1))                 # idle = both high
        byte = 0
        for scl, sda in group:                   # oldest first -> high nibble
            byte = (byte << 2) | ((scl & 1) << 1) | (sda & 1)
        out.append(byte)
    return bytes(out)


# --------------------------------------------------------------------------
# Decoder
# --------------------------------------------------------------------------

def decode_samples(samples: Sequence[Sample]) -> List[I2CTransaction]:
    """Decode a ``(scl, sda)`` sample stream into I2C transactions."""
    txns: List[I2CTransaction] = []
    txn: Optional[I2CTransaction] = None
    msg: Optional[I2CMessage] = None
    new_msg_pending = False
    bit_count = 0
    cur_byte = 0
    awaiting_ack = False

    if not samples:
        return txns

    pscl, psda = samples[0]
    for scl, sda in samples[1:]:
        # START / STOP: SDA changes while SCL stays high.
        if pscl == 1 and scl == 1 and psda != sda:
            if psda == 1 and sda == 0:                       # (repeated) START
                if txn is None:
                    txn = I2CTransaction()
                new_msg_pending = True
                msg = None
                bit_count = cur_byte = 0
                awaiting_ack = False
            else:                                            # STOP
                if txn is not None:
                    txn.complete = True
                    txns.append(txn)
                txn = msg = None
                new_msg_pending = awaiting_ack = False
                bit_count = cur_byte = 0
            pscl, psda = scl, sda
            continue

        # Data / ACK bit: sampled on each SCL rising edge.
        if pscl == 0 and scl == 1 and txn is not None:
            if awaiting_ack:
                ack = (sda == 0)
                if new_msg_pending:
                    msg = I2CMessage(address=cur_byte >> 1,
                                     read=bool(cur_byte & 1), address_ack=ack)
                    txn.messages.append(msg)
                    new_msg_pending = False
                else:
                    if msg is None:                          # data before any address
                        msg = I2CMessage(address=None, read=None, address_ack=None)
                        txn.messages.append(msg)
                    msg.data.append(I2CByte(value=cur_byte, ack=ack))
                awaiting_ack = False
                bit_count = cur_byte = 0
            else:
                cur_byte = ((cur_byte << 1) | sda) & 0xFF
                bit_count += 1
                if bit_count == 8:
                    awaiting_ack = True

        pscl, psda = scl, sda

    return txns


def decode(packed: Iterable[int]) -> List[I2CTransaction]:
    """Decode packed ``sensor_la`` capture bytes into I2C transactions."""
    return decode_samples(unpack_samples(packed))


def decode_from_la(words: Iterable[int], sda_ch: int, scl_ch: int) -> List[I2CTransaction]:
    """Decode I2C from a raw 14-channel LA capture (the unified-capture path).

    ``words`` are 12-bit LA samples (bit ``n`` = channel ``LA{n+1}``); ``sda_ch`` /
    ``scl_ch`` are 1-based channel numbers.  This is how I2C is decoded now that the
    FPGA captures raw LA only and protocol interpretation lives off-device — the
    Python mirror of the Go server's ``decodeI2CFromLA`` (verified in lock-step by
    the shared parity vectors).
    """
    sda_bit = sda_ch - 1
    scl_bit = scl_ch - 1
    samples: List[Sample] = [
        ((w >> scl_bit) & 1, (w >> sda_bit) & 1) for w in words
    ]
    return decode_samples(samples)


# --------------------------------------------------------------------------
# Query / format helpers
# --------------------------------------------------------------------------

def addressed(txns: Sequence[I2CTransaction], address: int) -> bool:
    """True if any message in any transaction targets ``address``."""
    return any(t.to(address) for t in txns)


def read_register(txns: Sequence[I2CTransaction], address: int,
                  register: int) -> Optional[List[int]]:
    """Return the bytes read back after a ``register`` pointer write to ``address``.

    Matches the usual "write register pointer, then read" pattern (whether the
    read is a repeated-START in the same transaction or a separate one). Returns
    ``None`` if no such read is found.
    """
    msgs = [m for t in txns for m in t.messages]
    for i, m in enumerate(msgs):
        if (m.address == address and m.read is False
                and m.data and m.data[0].value == register):
            for n in msgs[i + 1:]:
                if n.address == address and n.read:
                    return n.values
                if n.address == address and n.read is False:
                    break  # another write to the same device — restart the search
    return None


def format_transactions(txns: Sequence[I2CTransaction]) -> str:
    """Render transactions as a one-line-each logic-analyzer style trace."""
    lines = []
    for t in txns:
        parts = []
        for j, m in enumerate(t.messages):
            tag = "Sr" if j else "S"
            if m.address is None:
                parts.append(f"{tag} ??")
            else:
                rw = "R" if m.read else "W"
                ackc = "+" if m.address_ack else "-"
                parts.append(f"{tag} 0x{m.address:02X}{rw}{ackc}")
            parts.extend(str(b) for b in m.data)
        parts.append("P" if t.complete else "…")
        lines.append(" ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Waveform synthesis (for demos and tests — no hardware needed)
# --------------------------------------------------------------------------

def synthesize(messages: Sequence[dict], *, n: int = 4) -> List[Sample]:
    """Build a ``(scl, sda)`` stream for one transaction (demo/test helper).

    ``messages`` is a list of dicts: ``{"address": int, "read": bool,
    "address_ack": bool, "data": [(value, ack_bool), ...]}``. ``n`` samples per
    phase keep edges clean. The result round-trips through :func:`decode_samples`.
    """
    s: List[Sample] = []

    def emit(scl: int, sda: int, k: int = n) -> None:
        s.extend([(scl, sda)] * k)

    def emit_byte(value: int, ack: bool) -> None:
        for i in range(7, -1, -1):
            bit = (value >> i) & 1
            emit(0, bit)          # SDA setup while SCL low
            emit(1, bit)          # SCL high -> sampled
        ack_sda = 0 if ack else 1
        emit(0, ack_sda)
        emit(1, ack_sda)          # 9th clock = ACK/NACK
        emit(0, ack_sda)          # leave SCL low

    emit(1, 1)                    # idle
    for m in messages:
        # (repeated) START: release SDA high (SCL low), raise SCL, then SDA falls.
        emit(0, 1)
        emit(1, 1)
        emit(1, 0)
        abyte = (m["address"] << 1) | (1 if m.get("read") else 0)
        emit_byte(abyte, m.get("address_ack", True))
        for value, ack in m.get("data", []):
            emit_byte(value, ack)
    # STOP: SDA low (SCL low), raise SCL, then SDA rises.
    emit(0, 0)
    emit(1, 0)
    emit(1, 1)
    emit(1, 1)                    # idle
    return s


# --------------------------------------------------------------------------
# Timed transactions (the capture view's decode table and lane chips)
# --------------------------------------------------------------------------

def _i2c_direction(read_flags: Sequence[bool]) -> str:
    if not read_flags:
        return "write"
    has_read, has_write = any(read_flags), not all(read_flags)
    if has_read and has_write:
        return "read+write" if read_flags[0] else "write+read"
    return "read" if has_read else "write"


def timed_transactions(words: Sequence[int], sda_ch: int, scl_ch: int,
                       sample_rate_hz: float) -> List[dict]:
    """Decode I2C from raw LA ``words`` into transactions with per-segment timing.

    Each transaction is a dict ``{id, direction, address, start_us, end_us, ok, segments,
    register?, error_kind?}`` and each segment ``{type, start_us, end_us, value?, ack?}``, with
    ``type`` one of start, repeat-start, address-write, address-read, data-write, data-read, ack,
    nack, stop. This is the shape the EmbeddedCI capture view draws (the server's Go decoder emits
    the same); :func:`decode_from_la` gives the same bus content without timing.
    """
    bits_sda, bits_scl = sda_ch - 1, scl_ch - 1
    samples = [((w >> bits_scl) & 1, (w >> bits_sda) & 1) for w in words]
    if len(samples) < 2:
        return []
    us_per = 1e6 / sample_rate_hz if sample_rate_hz > 0 else 0.0
    min_w = us_per if us_per > 0 else 0.001

    def us(i: int) -> float:
        return i * us_per

    txns: List[dict] = []
    st: dict = {}

    def reset(start_idx: int) -> None:
        st.clear()
        st.update(in_txn=True, cur={"address": ""}, segs=[], start=start_idx, read_flags=[],
                  addr_nack=False, data_nack=False, register=None)

    def finish(end_idx: int, complete: bool) -> None:
        if not st.get("in_txn"):
            return
        segs = st["segs"]
        for i, s in enumerate(segs):
            if s["type"] in ("start", "repeat-start"):
                if i + 1 < len(segs):
                    s["end_us"] = segs[i + 1]["start_us"]
                if s["end_us"] <= s["start_us"]:
                    s["end_us"] = s["start_us"] + min_w
        cur = st["cur"]
        cur["segments"] = segs
        cur["start_us"] = us(st["start"])
        if complete:
            cur["end_us"] = us(end_idx)
        elif segs:
            cur["end_us"] = segs[-1]["end_us"]
        else:
            cur["end_us"] = cur["start_us"]
        if cur["end_us"] <= cur["start_us"]:
            cur["end_us"] = cur["start_us"] + min_w
        cur["direction"] = _i2c_direction(st["read_flags"])
        if st["register"] is not None:
            cur["register"] = st["register"]
        if st["addr_nack"]:
            cur["ok"], cur["error_kind"] = False, "address-nack"
        elif st["data_nack"]:
            cur["ok"], cur["error_kind"] = False, "data-nack"
        else:
            cur["ok"] = True
        cur["id"] = len(txns) + 1
        txns.append(cur)
        st["in_txn"] = False

    bit_count = cur_byte = 0
    awaiting_ack = new_msg = msg_read = msg_write = False
    msg_data = byte_start = last_bit = 0
    pscl, psda = samples[0]
    for idx in range(1, len(samples)):
        scl, sda = samples[idx]
        if pscl == 1 and scl == 1 and psda != sda:
            if psda == 1 and sda == 0:                      # (repeated) START
                was = bool(st.get("in_txn"))
                if not was:
                    reset(idx)
                bit_count = cur_byte = 0
                awaiting_ack = False
                msg_data = 0
                new_msg = True
                st["segs"].append({"type": "repeat-start" if was else "start", "start_us": us(idx), "end_us": 0.0})
            else:                                           # STOP
                if st.get("in_txn"):
                    st["segs"].append({"type": "stop", "start_us": us(idx), "end_us": us(idx) + min_w})
                finish(idx, True)
            pscl, psda = scl, sda
            continue
        if pscl == 0 and scl == 1 and st.get("in_txn"):
            if awaiting_ack:
                ack = sda == 0
                period = max(1, idx - last_bit)
                ack_seg = {"type": "ack" if ack else "nack", "start_us": us(idx), "end_us": us(idx + period)}
                if new_msg:
                    addr, read = cur_byte >> 1, bool(cur_byte & 1)
                    addr_s = f"0x{addr:02X}"
                    if not st["cur"]["address"]:
                        st["cur"]["address"] = addr_s
                    st["read_flags"].append(read)
                    msg_read, msg_write, msg_data = read, not read, 0
                    st["segs"] += [{"type": "address-read" if read else "address-write",
                                    "start_us": us(byte_start), "end_us": us(idx), "value": addr_s}, ack_seg]
                    if not ack:
                        st["addr_nack"] = True
                    new_msg = False
                else:
                    val = f"0x{cur_byte:02X}"
                    st["segs"] += [{"type": "data-read" if msg_read else "data-write",
                                    "start_us": us(byte_start), "end_us": us(idx), "value": val, "ack": ack},
                                   ack_seg]
                    if msg_write and st["register"] is None and msg_data == 0:
                        st["register"] = val
                    if msg_write and not ack:
                        st["data_nack"] = True
                    msg_data += 1
                awaiting_ack = False
                bit_count = cur_byte = 0
            else:
                cur_byte = ((cur_byte << 1) | sda) & 0xFF
                bit_count += 1
                if bit_count == 1:
                    byte_start = idx
                last_bit = idx
                if bit_count == 8:
                    awaiting_ack = True
        pscl, psda = scl, sda
    if st.get("in_txn"):
        finish(len(samples) - 1, False)
    return txns
