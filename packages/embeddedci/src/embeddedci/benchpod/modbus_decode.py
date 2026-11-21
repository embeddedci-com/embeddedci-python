"""Modbus RTU decoding on top of the UART decoder.

The Python mirror of the EmbeddedCI server's ``decode/modbus.go`` (same fields, held to it by
``modbus_decode_vectors.json``). Bytes are split into frames on silence (2.5 character times;
1.25 ms above 19200 baud), then address, function, data and CRC16. A frame that answers the open
request to the same address with the same function (or bit 7 set: an exception) is its response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ._gojson import go_dict
from .decode import decode_uart

FUNCTIONS = {
    1: "READ_COILS", 2: "READ_DISCRETE_INPUTS", 3: "READ_HOLDING_REGISTERS", 4: "READ_INPUT_REGISTERS",
    5: "WRITE_SINGLE_COIL", 6: "WRITE_SINGLE_REGISTER", 7: "READ_EXCEPTION_STATUS", 8: "DIAGNOSTICS",
    11: "GET_COMM_EVENT_COUNTER", 12: "GET_COMM_EVENT_LOG", 15: "WRITE_MULTIPLE_COILS",
    16: "WRITE_MULTIPLE_REGISTERS", 17: "REPORT_SERVER_ID", 20: "READ_FILE_RECORD", 21: "WRITE_FILE_RECORD",
    22: "MASK_WRITE_REGISTER", 23: "READ_WRITE_MULTIPLE_REGISTERS", 24: "READ_FIFO_QUEUE",
    43: "ENCAPSULATED_INTERFACE",
}
EXCEPTIONS = {
    1: "ILLEGAL_FUNCTION", 2: "ILLEGAL_DATA_ADDRESS", 3: "ILLEGAL_DATA_VALUE", 4: "SERVER_DEVICE_FAILURE",
    5: "ACKNOWLEDGE", 6: "SERVER_DEVICE_BUSY", 8: "MEMORY_PARITY_ERROR", 10: "GATEWAY_PATH_UNAVAILABLE",
    11: "GATEWAY_TARGET_NO_RESPONSE",
}


@dataclass
class ModbusFrame:
    """One RTU frame; ``role`` is ``request`` or ``response``."""

    index: int
    start_us: float
    end_us: float
    role: str = ""
    addr: int = 0
    function: int = 0
    function_name: str = ""
    exception: str = ""
    start: Optional[int] = None
    quantity: Optional[int] = None
    values: List[int] = field(default_factory=list)
    summary: str = ""
    data_hex: str = ""
    crc: str = ""
    reply_to: int = 0
    response_us: float = 0.0
    ok: bool = True
    error: str = ""
    _body: bytes = field(default=b"", repr=False)

    def as_dict(self) -> dict:
        return go_dict(self, omit_empty=("exception", "values", "reply_to", "response_us", "error"),
                       omit_none=("start", "quantity"))


def modbus_crc(b: bytes) -> int:
    """The Modbus RTU CRC16 (reflected 0x8005, init 0xFFFF), sent low byte first."""
    crc = 0xFFFF
    for v in b:
        crc ^= v
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _join(a: str, b: str) -> str:
    return b if not a else f"{a}, {b}"


def _hex(b: bytes) -> str:
    return " ".join(f"{v:02X}" for v in b)


def _func_name(fc: int) -> str:
    if fc & 0x80:
        return _func_name(fc & 0x7F) + " exception"
    return FUNCTIONS.get(fc, f"function {fc}")


def decode_modbus(words: Sequence[int], *, rx: int, baud: float, sample_rate_hz: float, tx: int = 0,
                  data_bits: int = 8, parity: str = "even", stop_bits: float = 1.0) -> List[ModbusFrame]:
    """Decode Modbus RTU from the UART line on ``rx`` (and ``tx`` for a full-duplex tap)."""
    data_bits = data_bits or 8
    parity = parity or "even"
    stop_bits = stop_bits if stop_bits and stop_bits > 0 else 1.0
    raw = []  # (value, start, end, error, line)
    for i, ch in enumerate((rx, tx)):
        if i == 1 and (ch == 0 or ch == rx):
            continue
        try:
            frames = decode_uart(words, rx=ch, baud=baud, sample_rate_hz=sample_rate_hz,
                                 data_bits=data_bits, parity=parity, stop_bits=stop_bits)
        except ValueError as e:
            raise ValueError(f"modbus: {e}") from None
        raw.extend((f.value & 0xFF, f.start_us, f.end_us, f.error, i) for f in frames)
    # One time order (stable, as the Go insertion sort).
    raw.sort(key=lambda b: b[1])
    bits = 1 + data_bits + stop_bits + (1 if parity in ("even", "odd") else 0)
    char_us = bits / baud * 1e6
    gap_us = 2.5 * char_us if baud <= 19200 else 1250.0
    out: List[ModbusFrame] = []
    cur: list = []
    for k, b in enumerate(raw):
        if k > 0 and (b[4] != raw[k - 1][4] or b[1] - (raw[k - 1][2] + char_us / bits / 2) > gap_us):
            out.append(_frame(cur, len(out) + 1))
            cur = []
        cur.append(b)
    if cur:
        out.append(_frame(cur, len(out) + 1))
    _pair(out)
    return out


def _frame(bs: list, index: int) -> ModbusFrame:
    f = ModbusFrame(index, bs[0][1], bs[-1][2])
    raw = bytes(b[0] for b in bs)
    for b in bs:
        if b[3] and not f.error:
            f.error = "UART " + b[3]
    if len(raw) < 4:
        f.error = _join(f.error, f"too short: {len(raw)} bytes")
        f.data_hex = _hex(raw)
        f.addr = raw[0] if raw else 0
        f.function = raw[1] if len(raw) > 1 else 0
        f.function_name = _func_name(f.function)
        f.ok = False
        return f
    f.addr, f.function = raw[0], raw[1]
    body = raw[2:-2]
    got = raw[-2] | raw[-1] << 8
    f.crc = f"0x{got:04X}"
    if modbus_crc(raw[:-2]) != got:
        f.error = _join(f.error, "bad CRC")
    f.data_hex = _hex(body)
    f._body = body
    f.function_name = _func_name(f.function)
    f.ok = not f.error
    return f


def _looks_request(fc: int, b: bytes) -> bool:
    if fc in (1, 2, 3, 4, 5, 6):
        return len(b) == 4
    if fc in (15, 16):
        return len(b) >= 5 and b[4] == len(b) - 5
    return False


def _looks_response(fc: int, b: bytes) -> bool:
    if fc & 0x80:
        return len(b) == 1
    if fc in (1, 2, 3, 4):
        return len(b) >= 1 and b[0] == len(b) - 1
    if fc in (5, 6, 15, 16):
        return len(b) == 4
    return False


def _pair(fr: List[ModbusFrame]) -> None:
    open_ = -1
    for k, f in enumerate(fr):
        body = f._body
        is_resp = False
        if open_ >= 0:
            q = fr[open_]
            if q.addr == f.addr and (q.function == f.function or q.function | 0x80 == f.function):
                is_resp = True
        if not is_resp and open_ < 0 and _looks_response(f.function, body) and not _looks_request(f.function, body):
            is_resp = True
        if is_resp:
            f.role = "response"
            if open_ >= 0:
                f.reply_to = fr[open_].index
                f.response_us = f.start_us - fr[open_].end_us
                _decode(f, body, fr[open_])
            else:
                _decode(f, body, None)
            open_ = -1
            continue
        f.role = "request"
        _decode(f, body, None)
        open_ = k if f.addr != 0 else -1


def _be16(b: bytes, o: int) -> int:
    return b[o] << 8 | b[o + 1]


def _values(v: List[int]) -> str:
    if not v:
        return "none"
    parts = [str(x) for x in v[:32]]
    if len(v) > 32:
        parts.append(f"... ({len(v)} values)")
    return " ".join(parts)


def _decode(f: ModbusFrame, b: bytes, req: Optional[ModbusFrame]) -> None:
    if not f.crc:
        f.summary = f.function_name
        return
    fc = f.function
    if fc & 0x80:
        if len(b) >= 1:
            name = EXCEPTIONS.get(b[0], f"exception {b[0]}")
            f.exception = name
            f.summary = f"{_func_name(fc & 0x7F)}: {name}"
        return

    def bad() -> None:
        f.summary = f.function_name
        f.error = _join(f.error, "bad length")
        f.ok = False

    if f.role == "request" and 1 <= fc <= 4:
        if len(b) != 4:
            return bad()
        f.start, f.quantity = _be16(b, 0), _be16(b, 2)
        f.summary = f"{f.function_name} {f.start} x{f.quantity}"
    elif f.role == "response" and 1 <= fc <= 4:
        if len(b) < 1 or b[0] != len(b) - 1:
            return bad()
        data = b[1:]
        if fc <= 2:
            n = len(data) * 8
            if req is not None and req.quantity is not None and req.quantity <= n:
                n = req.quantity
            f.values = [(data[k // 8] >> (k % 8)) & 1 for k in range(n)]
        else:
            f.values = [_be16(data, o) for o in range(0, len(data) - 1, 2)]
        if req is not None and req.start is not None:
            f.start = req.start
            f.summary = f"{f.function_name} {req.start}: {_values(f.values)}"
        else:
            f.summary = f"{f.function_name}: {_values(f.values)}"
    elif fc == 5:
        if len(b) != 4:
            return bad()
        v = _be16(b, 2)
        f.start = _be16(b, 0)
        state = "ON"
        if v == 0:
            state = "OFF"
        elif v != 0xFF00:
            state = f"0x{v:04X} (not ON or OFF)"
            f.error = _join(f.error, "bad coil value")
            f.ok = False
        f.values = [1 if v == 0xFF00 else 0]
        f.summary = f"{f.function_name} {f.start} {state}"
    elif fc == 6:
        if len(b) != 4:
            return bad()
        f.start = _be16(b, 0)
        f.values = [_be16(b, 2)]
        f.summary = f"{f.function_name} {f.start} = {f.values[0]}"
    elif fc in (15, 16) and f.role == "request":
        if len(b) < 5 or b[4] != len(b) - 5:
            return bad()
        f.start, f.quantity = _be16(b, 0), _be16(b, 2)
        data = b[5:]
        if fc == 15:
            f.values = [(data[k // 8] >> (k % 8)) & 1 for k in range(f.quantity) if k // 8 < len(data)]
        else:
            f.values = [_be16(data, o) for o in range(0, len(data) - 1, 2)]
        f.summary = f"{f.function_name} {f.start}: {_values(f.values)}"
    elif fc in (15, 16) and f.role == "response":
        if len(b) != 4:
            return bad()
        f.start, f.quantity = _be16(b, 0), _be16(b, 2)
        f.summary = f"{f.function_name} {f.start} x{f.quantity} done"
    else:
        f.summary = f.function_name + (f" {f.data_hex}" if f.data_hex else "")
