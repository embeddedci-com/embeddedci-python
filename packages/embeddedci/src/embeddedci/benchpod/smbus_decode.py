"""SMBus and PMBus decoding on top of the I2C decoder.

The Python mirror of the EmbeddedCI server's ``decode/smbus.go`` (held to it by
``smbus_decode_vectors.json``). Each I2C transaction becomes an SMBus protocol (quick command,
send/receive byte, write/read byte or word, block write/read, process call) with its command and
data; a trailing PEC (CRC-8 0x07 over the address and data bytes) is checked. PMBus names the
command and decodes LINEAR11, LINEAR16 (with the exponent from the device's VOUT_MODE once one is
seen), status bits and text blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from ._gojson import go_dict
from .i2c import timed_transactions

# command -> (name, format, unit); formats as in the Go decoder.
PMBUS_COMMANDS = {
    0x00: ("PAGE", "byte", ""), 0x01: ("OPERATION", "byte", ""), 0x02: ("ON_OFF_CONFIG", "byte", ""),
    0x03: ("CLEAR_FAULTS", "send", ""), 0x04: ("PHASE", "byte", ""), 0x10: ("WRITE_PROTECT", "byte", ""),
    0x11: ("STORE_DEFAULT_ALL", "send", ""), 0x12: ("RESTORE_DEFAULT_ALL", "send", ""),
    0x15: ("STORE_USER_ALL", "send", ""), 0x16: ("RESTORE_USER_ALL", "send", ""),
    0x19: ("CAPABILITY", "byte", ""), 0x1B: ("SMBALERT_MASK", "word", ""),
    0x20: ("VOUT_MODE", "vmode", ""), 0x21: ("VOUT_COMMAND", "L16", "V"), 0x22: ("VOUT_TRIM", "L16", "V"),
    0x23: ("VOUT_CAL_OFFSET", "L16", "V"), 0x24: ("VOUT_MAX", "L16", "V"), 0x25: ("VOUT_MARGIN_HIGH", "L16", "V"),
    0x26: ("VOUT_MARGIN_LOW", "L16", "V"), 0x27: ("VOUT_TRANSITION_RATE", "L11", "mV/µs"),
    0x28: ("VOUT_DROOP", "L11", "mΩ"), 0x29: ("VOUT_SCALE_LOOP", "L11", ""), 0x2A: ("VOUT_SCALE_MONITOR", "L11", ""),
    0x2B: ("VOUT_MIN", "L16", "V"), 0x31: ("POUT_MAX", "L11", "W"), 0x32: ("MAX_DUTY", "L11", "%"),
    0x33: ("FREQUENCY_SWITCH", "L11", "kHz"), 0x35: ("VIN_ON", "L11", "V"), 0x36: ("VIN_OFF", "L11", "V"),
    0x38: ("IOUT_CAL_GAIN", "L11", "mΩ"), 0x39: ("IOUT_CAL_OFFSET", "L11", "A"),
    0x40: ("VOUT_OV_FAULT_LIMIT", "L16", "V"), 0x41: ("VOUT_OV_FAULT_RESPONSE", "byte", ""),
    0x42: ("VOUT_OV_WARN_LIMIT", "L16", "V"), 0x43: ("VOUT_UV_WARN_LIMIT", "L16", "V"),
    0x44: ("VOUT_UV_FAULT_LIMIT", "L16", "V"), 0x45: ("VOUT_UV_FAULT_RESPONSE", "byte", ""),
    0x46: ("IOUT_OC_FAULT_LIMIT", "L11", "A"), 0x47: ("IOUT_OC_FAULT_RESPONSE", "byte", ""),
    0x4A: ("IOUT_OC_WARN_LIMIT", "L11", "A"), 0x4F: ("OT_FAULT_LIMIT", "L11", "°C"),
    0x50: ("OT_FAULT_RESPONSE", "byte", ""), 0x51: ("OT_WARN_LIMIT", "L11", "°C"),
    0x52: ("UT_WARN_LIMIT", "L11", "°C"), 0x53: ("UT_FAULT_LIMIT", "L11", "°C"),
    0x55: ("VIN_OV_FAULT_LIMIT", "L11", "V"), 0x57: ("VIN_OV_WARN_LIMIT", "L11", "V"),
    0x58: ("VIN_UV_WARN_LIMIT", "L11", "V"), 0x59: ("VIN_UV_FAULT_LIMIT", "L11", "V"),
    0x5B: ("IIN_OC_FAULT_LIMIT", "L11", "A"), 0x5D: ("IIN_OC_WARN_LIMIT", "L11", "A"),
    0x5E: ("POWER_GOOD_ON", "L16", "V"), 0x5F: ("POWER_GOOD_OFF", "L16", "V"),
    0x60: ("TON_DELAY", "L11", "ms"), 0x61: ("TON_RISE", "L11", "ms"), 0x62: ("TON_MAX_FAULT_LIMIT", "L11", "ms"),
    0x64: ("TOFF_DELAY", "L11", "ms"), 0x65: ("TOFF_FALL", "L11", "ms"), 0x66: ("TOFF_MAX_WARN_LIMIT", "L11", "ms"),
    0x6A: ("POUT_OP_WARN_LIMIT", "L11", "W"), 0x6B: ("PIN_OP_WARN_LIMIT", "L11", "W"),
    0x78: ("STATUS_BYTE", "sb", ""), 0x79: ("STATUS_WORD", "sw", ""), 0x7A: ("STATUS_VOUT", "bits", ""),
    0x7B: ("STATUS_IOUT", "bits", ""), 0x7C: ("STATUS_INPUT", "bits", ""), 0x7D: ("STATUS_TEMPERATURE", "bits", ""),
    0x7E: ("STATUS_CML", "bits", ""), 0x7F: ("STATUS_OTHER", "bits", ""), 0x80: ("STATUS_MFR_SPECIFIC", "bits", ""),
    0x81: ("STATUS_FANS_1_2", "bits", ""), 0x88: ("READ_VIN", "L11", "V"), 0x89: ("READ_IIN", "L11", "A"),
    0x8A: ("READ_VCAP", "L11", "V"), 0x8B: ("READ_VOUT", "L16", "V"), 0x8C: ("READ_IOUT", "L11", "A"),
    0x8D: ("READ_TEMPERATURE_1", "L11", "°C"), 0x8E: ("READ_TEMPERATURE_2", "L11", "°C"),
    0x8F: ("READ_TEMPERATURE_3", "L11", "°C"), 0x90: ("READ_FAN_SPEED_1", "L11", "RPM"),
    0x91: ("READ_FAN_SPEED_2", "L11", "RPM"), 0x94: ("READ_DUTY_CYCLE", "L11", "%"),
    0x95: ("READ_FREQUENCY", "L11", "kHz"), 0x96: ("READ_POUT", "L11", "W"), 0x97: ("READ_PIN", "L11", "W"),
    0x98: ("PMBUS_REVISION", "byte", ""), 0x99: ("MFR_ID", "block", ""), 0x9A: ("MFR_MODEL", "block", ""),
    0x9B: ("MFR_REVISION", "block", ""), 0x9C: ("MFR_LOCATION", "block", ""), 0x9D: ("MFR_DATE", "block", ""),
    0x9E: ("MFR_SERIAL", "block", ""),
}
_STATUS_LOW = ("NONE_OF_THE_ABOVE", "CML", "TEMPERATURE", "VIN_UV_FAULT", "IOUT_OC_FAULT", "VOUT_OV_FAULT", "OFF", "BUSY")
_STATUS_HIGH = ("UNKNOWN", "OTHER", "FANS", "POWER_GOOD#", "MFR_SPECIFIC", "INPUT", "IOUT/POUT", "VOUT")


@dataclass
class SmbusTransaction:
    index: int
    start_us: float
    end_us: float
    addr: int
    addr_hex: str
    kind: str = ""
    command: Optional[int] = None
    command_name: str = ""
    data_hex: str = ""
    value: str = ""
    pec: str = "none"
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return go_dict(self, omit_empty=("command_name", "value", "error"), omit_none=("command",))


def pec8(b: bytes) -> int:
    """The SMBus packet error code: CRC-8, polynomial 0x07, init 0."""
    crc = 0
    for v in b:
        crc ^= v
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def linear11(w: int) -> float:
    """A PMBus LINEAR11 word's value."""
    exp = w >> 11 & 0x1F
    if exp > 15:
        exp -= 32
    man = w & 0x7FF
    if man > 1023:
        man -= 2048
    return man * 2.0 ** exp


def _join(a: str, b: str) -> str:
    return b if not a else f"{a}, {b}"


def _hex(b) -> str:
    return " ".join(f"{v:02X}" for v in b)


def _g4(v: float) -> str:
    return f"{v:.4g}"


def decode_smbus(words: Sequence[int], *, sda: int, scl: int, sample_rate_hz: float, pmbus: bool = False,
                 pec: str = "auto") -> List[SmbusTransaction]:
    """Decode SMBus (``pmbus=True``: PMBus) on ``sda``/``scl``. ``pec`` is ``auto`` (a trailing
    byte that matches is a PEC), ``on`` or ``off``."""
    pec = pec or "auto"
    if pec not in ("auto", "on", "off"):
        raise ValueError(f"pec must be auto, on or off, got {pec!r}")
    out: List[SmbusTransaction] = []
    vout_exp: Dict[int, int] = {}
    for t in timed_transactions(words, sda, scl, sample_rate_hz):
        msgs: List[dict] = []
        addr = -1
        segs = t["segments"]
        for k, s in enumerate(segs):
            typ = s["type"]
            if typ.startswith("address"):
                a = int(s["value"], 16)
                if addr < 0:
                    addr = a
                msgs.append({"read": typ == "address-read", "bytes": [],
                             "nack": k + 1 < len(segs) and segs[k + 1]["type"] == "nack"})
            elif typ.startswith("data") and msgs:
                msgs[-1]["bytes"].append(int(s["value"], 16))
        if addr < 0:
            continue
        x = _read(msgs, addr, pec)
        x.start_us, x.end_us = t["start_us"], t["end_us"]
        if not t["ok"]:
            x.error = _join((t.get("error_kind") or "").replace("-", " "), x.error)
        if pmbus:
            _value(x, vout_exp)
        x.ok = not x.error
        x.index = len(out) + 1
        out.append(x)
    return out


def _read(msgs: List[dict], addr: int, pec: str) -> SmbusTransaction:
    x = SmbusTransaction(0, 0.0, 0.0, addr, f"0x{addr:02X}")
    covered: List[int] = []
    for m in msgs:
        covered.append((addr << 1 | (1 if m["read"] else 0)) & 0xFF)
        covered.extend(m["bytes"])
        if m["nack"] and not m["bytes"]:
            x.kind = "address NACK"
            return x
    last = msgs[-1]
    if pec != "off" and last["bytes"] and len(covered) >= 2:
        match = pec8(bytes(covered[:-1])) == covered[-1]
        total = sum(len(m["bytes"]) for m in msgs)
        if pec == "on" or (match and total >= 2):
            if match:
                x.pec = "ok"
            else:
                x.pec = "bad"
                x.error = "bad PEC"
            last["bytes"] = last["bytes"][:-1]
    if len(msgs) == 1 and not msgs[0]["bytes"]:
        x.kind = "quick command (read)" if msgs[0]["read"] else "quick command"
    elif len(msgs) == 1 and not msgs[0]["read"]:
        w = msgs[0]["bytes"]
        x.command = w[0]
        d = w[1:]
        if not d:
            x.kind = "send byte"
        elif len(d) == 1:
            x.kind = "write byte"
        elif len(d) == 2:
            x.kind = "write word"
        elif d[0] == len(d) - 1:
            x.kind = "block write"
            d = d[1:]
        else:
            x.kind = f"write {len(d)} bytes"
        x.data_hex = _hex(d)
    elif len(msgs) == 1:
        r = msgs[0]["bytes"]
        x.kind = f"read {len(r)} bytes" if len(r) > 1 else "receive byte"
        x.data_hex = _hex(r)
    elif len(msgs) == 2 and not msgs[0]["read"] and msgs[1]["read"] and msgs[0]["bytes"]:
        x.command = msgs[0]["bytes"][0]
        wd, r = msgs[0]["bytes"][1:], msgs[1]["bytes"]
        if not wd and len(r) == 1:
            x.kind = "read byte"
        elif not wd and len(r) == 2:
            x.kind = "read word"
        elif not wd and r and r[0] == len(r) - 1:
            x.kind = "block read"
            r = r[1:]
        elif not wd:
            x.kind = f"read {len(r)} bytes"
        elif len(wd) == 2 and len(r) == 2:
            x.kind = "process call"
        else:
            x.kind = "block process call"
        x.data_hex = _hex(r)
    else:
        x.kind = f"{len(msgs)} messages"
        x.data_hex = _hex([b for m in msgs for b in m["bytes"]])
    return x


def _value(x: SmbusTransaction, vout_exp: Dict[int, int]) -> None:
    if x.command is None:
        return
    c = PMBUS_COMMANDS.get(x.command)
    if c is None:
        if x.command >= 0xD0:
            x.command_name = f"MFR_SPECIFIC_{x.command:02X}"
        return
    name, fmt, unit = c
    x.command_name = name
    d = [int(p, 16) for p in x.data_hex.split()] if x.data_hex else []
    word = d[0] | d[1] << 8 if len(d) == 2 else None
    if fmt == "L11" and word is not None:
        x.value = f"{_g4(linear11(word))} {unit}".strip()
    elif fmt == "L16" and word is not None:
        if x.addr in vout_exp:
            x.value = f"{_g4(word * 2.0 ** vout_exp[x.addr])} V"
        else:
            x.value = f"0x{word:04X} (VOUT_MODE not seen)"
    elif fmt == "vmode" and len(d) == 1:
        mode = d[0] >> 5
        if mode == 0:
            e = d[0] & 0x1F
            if e > 15:
                e -= 32
            vout_exp[x.addr] = e
            x.value = f"linear, exponent {e}"
        else:
            x.value = f"mode {mode}"
    elif fmt in ("sb", "sw"):
        v = d[0] if d else 0
        if len(d) == 2 and fmt == "sw":
            v |= d[1] << 8
        names = [(_STATUS_LOW[b] if b < 8 else _STATUS_HIGH[b - 8]) for b in range(16) if v & (1 << b)]
        x.value = ", ".join(names) if names else "no faults"
    elif fmt == "block":
        if d and all(0x20 <= b <= 0x7E for b in d):
            s = "".join(chr(b) for b in d)
            x.value = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
