"""1-Wire decoding from the bus line's low pulses.

The Python mirror of the EmbeddedCI server's ``decode/onewire.go`` (held to it by
``onewire_decode_vectors.json``): resets (480 µs, overdrive 48-80 µs) and presence pulses, time
slots (a short low is a 1: under 15 µs, overdrive 2.5 µs), bytes LSB first, the ROM command and
ROM ID (READ/MATCH ROM, SEARCH ROM triplets) with its CRC8, then the function bytes; temperature
sensor commands are named and READ SCRATCHPAD gives the temperature.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from ._gojson import go_dict

ROM_COMMANDS = {
    0x33: "READ_ROM", 0x55: "MATCH_ROM", 0xCC: "SKIP_ROM", 0xF0: "SEARCH_ROM", 0xEC: "ALARM_SEARCH",
    0x3C: "OVERDRIVE_SKIP_ROM", 0x69: "OVERDRIVE_MATCH_ROM", 0xA5: "RESUME",
}
TEMP_FUNCTIONS = {
    0x44: "CONVERT_T", 0xBE: "READ_SCRATCHPAD", 0x4E: "WRITE_SCRATCHPAD", 0x48: "COPY_SCRATCHPAD",
    0xB8: "RECALL_E2", 0xB4: "READ_POWER_SUPPLY",
}
_TEMP_FAMILIES = {0x10, 0x22, 0x28, 0x3B, 0x42}


@dataclass
class OneWireTransaction:
    """Everything from one reset to the next."""

    index: int
    start_us: float
    end_us: float = 0.0
    reset: bool = False
    presence: bool = False
    overdrive: bool = False
    rom_command: Optional[int] = None
    rom_command_name: str = ""
    rom: str = ""
    rom_crc_ok: Optional[bool] = None
    function: str = ""
    bytes_hex: str = ""
    detail: str = ""
    ok: bool = True
    error: str = ""

    def as_dict(self) -> dict:
        return go_dict(self, omit_empty=("rom_command_name", "rom", "function", "detail", "error"),
                       omit_none=("rom_command", "rom_crc_ok"))


def crc8(b: bytes) -> int:
    """The Maxim/Dallas CRC8 (x^8+x^5+x^4+1, reflected)."""
    crc = 0
    for v in b:
        for _ in range(8):
            mix = (crc ^ v) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            v >>= 1
    return crc


def _join(a: str, b: str) -> str:
    return b if not a else f"{a}, {b}"


def _hex(b) -> str:
    return " ".join(f"{v:02X}" for v in b)


def decode_onewire(words: Sequence[int], *, onewire: int, sample_rate_hz: float) -> List[OneWireTransaction]:
    """Decode the 1-Wire line on channel ``onewire``."""
    if not 1 <= onewire <= 14:
        raise ValueError("1-wire decode needs a channel in 1..14")
    if sample_rate_hz < 200e3:
        raise ValueError(f"sample rate {sample_rate_hz:.0f} Hz is too low for 1-Wire: capture at 1 MS/s or "
                         "more (4 MS/s for overdrive)")
    bit = onewire - 1
    us = 1e6 / sample_rate_hz
    pulses = []
    start = -1
    for i, w in enumerate(words):
        v = (w >> bit) & 1
        if v == 0 and start < 0:
            start = i
        if v == 1 and start >= 0:
            if start > 0:
                pulses.append((start * us, (i - start) * us))
            start = -1
    out: List[OneWireTransaction] = []
    st = {"od": False, "cur": None, "bits": [], "last_end": 0.0}

    def finish() -> None:
        cur = st["cur"]
        if cur is None:
            return
        _bytes(cur, st["bits"])
        cur.end_us = st["last_end"]
        cur.index = len(out) + 1
        out.append(cur)
        st["cur"], st["bits"] = None, []

    k = 0
    while k < len(pulses):
        ps, pd = pulses[k]
        if pd >= 400 or (st["od"] and pd >= 40):
            finish()
            if pd >= 400:
                st["od"] = False
            cur = OneWireTransaction(0, ps, reset=True, overdrive=st["od"])
            st["cur"] = cur
            st["last_end"] = ps + pd
            wait, mn, mx = (10.0, 6.0, 40.0) if st["od"] else (70.0, 50.0, 300.0)
            if k + 1 < len(pulses):
                qs, qd = pulses[k + 1]
                gap = qs - (ps + pd)
                if 0 <= gap <= wait and mn <= qd <= mx:
                    cur.presence = True
                    st["last_end"] = qs + qd
                    k += 1
            k += 1
            continue
        if st["cur"] is None:
            st["cur"] = OneWireTransaction(0, ps, overdrive=st["od"])
        limit = 2.5 if st["od"] else 15.0
        st["bits"].append(1 if pd < limit else 0)
        st["last_end"] = ps + pd
        bits = st["bits"]
        if not st["od"] and len(bits) == 8 and st["cur"].reset:
            v = sum(bits[b] << b for b in range(8))
            if v in (0x3C, 0x69):
                st["od"] = True
                st["cur"].overdrive = True
        k += 1
    finish()
    return out


def _bytes(t: OneWireTransaction, bits: List[int]) -> None:
    t.ok = True
    pos = 0

    def byte_at():
        nonlocal pos
        if pos + 8 > len(bits):
            return None
        v = sum(bits[pos + b] << b for b in range(8))
        pos += 8
        return v

    if t.reset and not t.presence:
        t.error = "no presence pulse"
    family = -1
    if t.reset:
        cmd = byte_at()
        if cmd is not None:
            t.rom_command = cmd
            t.rom_command_name = ROM_COMMANDS.get(cmd, f"ROM command 0x{cmd:02X}")
            rom: List[int] = []
            if cmd in (0x33, 0x55, 0x69):
                for _ in range(8):
                    v = byte_at()
                    if v is None:
                        break
                    rom.append(v)
            elif cmd in (0xF0, 0xEC):
                ident = [0] * 8
                n = 0
                while n < 64 and pos + 3 <= len(bits):
                    b, nb, d = bits[pos], bits[pos + 1], bits[pos + 2]
                    pos += 3
                    if b == 1 and nb == 1:
                        t.error = _join(t.error, "no device answered the search")
                        break
                    ident[n // 8] |= d << (n % 8)
                    n += 1
                if n == 64:
                    rom = ident
            if len(rom) == 8:
                t.rom = _hex(rom)
                ok = crc8(bytes(rom[:7])) == rom[7]
                t.rom_crc_ok = ok
                if not ok:
                    t.error = _join(t.error, "bad ROM CRC")
                family = rom[0]
            elif cmd in (0x33, 0x55, 0x69, 0xF0, 0xEC):
                t.error = _join(t.error, "ROM ID cut short")
    fb: List[int] = []
    while True:
        v = byte_at()
        if v is None:
            break
        fb.append(v)
    rest = len(bits) - pos
    if rest > 0:
        t.error = _join(t.error, f"{rest} bits left over")
    t.bytes_hex = _hex(fb)
    if fb and (family < 0 or family in _TEMP_FAMILIES):
        name = TEMP_FUNCTIONS.get(fb[0])
        if name:
            t.function = name
            if fb[0] == 0xBE and len(fb) >= 10:
                sp = fb[1:10]
                crc_ok = crc8(bytes(sp[:8])) == sp[8]
                raw = sp[1] << 8 | sp[0]
                if raw >= 0x8000:
                    raw -= 0x10000
                c = raw / 2 if family == 0x10 else raw / 16
                t.detail = f"{c:.4g} °C"
                if not crc_ok:
                    t.error = _join(t.error, "bad scratchpad CRC")
    if not t.function and fb:
        t.function = f"0x{fb[0]:02X}"
    t.error = t.error.strip()
    t.ok = not t.error
