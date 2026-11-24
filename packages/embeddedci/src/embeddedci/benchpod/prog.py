"""Flash a chip over its own programming pins (the pod's ``prog_flash`` command).

Five protocols that need neither SWD, JTAG nor a bootloader: AVR UPDI (``avr-updi``), PIC ICSP
with low-voltage entry (``pic``), Silicon Labs C2 (``efm8``), AVR TPI (``attiny-tpi``) and XMEGA
PDI (``xmega``). The pod runs the whole protocol next to the pins: each memory region is staged
in the pod's PSRAM (``load_bin``), written and read back by the pod in the background, and this
module polls its progress. It works the same over the LAN, USB and the cloud.

The pod holds no device tables. It reads and reports the chip's ID bytes; the table in
``prog_devices.json`` (next to this module, described in ``prog_devices.README.md``) says which
chip that is, how its memories are laid out and which protocol options it needs. This module
reads a ``.hex``, ``.elf`` or ``.bin`` image, cuts it into the chip's memories (flash, EEPROM,
configuration or fuses, user row or user ID) and writes them one after the other.

Use it through :meth:`BenchPod.flash <embeddedci.benchpod.BenchPod.flash>` with one of the
:data:`TARGETS` (``flash(target="avr-updi", file="app.hex")``); this module is its implementation.
"""

from __future__ import annotations

import json
import os
import struct
import time
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import FlashError, TargetUnreachableError

#: ``flash(target=...)`` names and the ``prog_flash`` protocol each one runs. The protocol names
#: themselves are accepted too (``updi``, ``pic``, ``c2``, ``tpi``, ``pdi``).
TARGETS: Dict[str, str] = {
    "avr-updi": "updi", "updi": "updi",
    "pic": "pic",
    "efm8": "c2", "c2": "c2",
    "attiny-tpi": "tpi", "tpi": "tpi",
    "xmega": "pdi", "pdi": "pdi",
}

#: The memories the pod writes, in the order this module writes them: flash first, the
#: configuration (fuses, lock) last, because it can lock the chip or switch off its programming pin.
MEM_ORDER = ("flash", "eeprom", "user", "config")

#: Wiring-profile signal names looked up when a pin is not given (case-insensitive), per protocol.
CLK_SIGNALS = {
    "pic": ("ICSPCLK", "ICSP_CLK", "PGC"), "c2": ("C2CK",), "tpi": ("TPICLK", "TPI_CLK"),
    "pdi": ("PDI_CLK",),
}
DATA_SIGNALS = {
    "updi": ("UPDI",), "pic": ("ICSPDAT", "ICSP_DAT", "PGD"), "c2": ("C2D",),
    "tpi": ("TPIDATA", "TPI_DATA"), "pdi": ("PDI_DATA",),
}
RESET_SIGNALS = ("MCLR", "RESET", "NRST", "PROG_RESET")

POLL_INTERVAL = 0.2
#: Entering programming mode and reading the ID: a few hundred milliseconds, 5 s is generous.
INFO_TIMEOUT = 10.0

#: Progress callback: pages (or blocks) done in the region being written, and the region's total.
Progress = Callable[[int, int], None]


# -- errors ----------------------------------------------------------------------------------

class ProgFlashError(FlashError):
    """A pin-programmer flash did not succeed. :attr:`result` is the pod's outcome (or what the
    SDK found), :attr:`phase` where it stopped (``enter``, ``erase``, ``write``, ``leave``),
    :attr:`mem`/:attr:`addr` the region and :attr:`at_addr` the first address that read back
    wrong. The message says what to check."""

    def __init__(self, message: str, *, result: str = "", phase: str = "", mem: str = "",
                 addr: Optional[int] = None, at_addr: Optional[int] = None) -> None:
        super().__init__(message)
        self.result = result
        self.phase = phase
        self.mem = mem
        self.addr = addr
        self.at_addr = at_addr


class ProgNoAnswerError(ProgFlashError, TargetUnreachableError):
    """The chip did not answer: wiring, power or the I/O voltage (also a
    :class:`~embeddedci.benchpod.errors.TargetUnreachableError`)."""


class ProgLockedError(ProgFlashError):
    """The chip is locked: only a chip erase (``erase="chip"``) unlocks it, and that erases it."""


class ProgVerifyError(ProgFlashError):
    """A page read back different from what was written (``at_addr``)."""


class ProgDeviceError(ProgFlashError):
    """The chip is not in the device table, is not the ``device`` named, or cannot be programmed
    at this I/O voltage."""


# -- the device table --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProgMemory:
    """One memory of a chip. ``addr`` is where the pod addresses its first byte and ``hex`` where
    it sits in a ``.hex`` / ``.elf`` image (avr-gcc: flash 0, EEPROM 0x810000, fuses 0x820000,
    user row 0x850000). ``size`` is in bytes, ``page`` the write page or row in bytes, ``align``
    the unit a region starts and ends on, ``stride`` the hex bytes one byte of this memory takes
    (2 when the image carries each byte in a 16-bit word)."""

    name: str
    size: int
    page: int
    addr: int
    hex: int
    align: int = 1
    stride: int = 1


@dataclass(frozen=True)
class ProgDevice:
    """One chip of the device table."""

    name: str
    family: str
    proto: str
    id: str
    mem: Mapping[str, ProgMemory]
    opts: Mapping[str, int] = field(default_factory=dict)
    vmin_mv: int = 0
    vmax_mv: int = 0
    erase_vmin_mv: int = 0
    vnote: str = ""
    aliases: Tuple[str, ...] = ()
    notes: str = ""
    #: The chip writes fewer words than it erases (PIC16F1827 and 193x: 8 latches, 32-word rows), so
    #: a page erase of a part of a row would erase its neighbours: only ``erase="chip"`` is allowed.
    chip_erase_only: bool = False

    def matches_id(self, chip_id: str) -> bool:
        """Whether ``chip_id`` (the pod's ``status.id`` hex) is this chip's. The table's ``id`` is
        hex bytes with ``??`` for bytes that vary (a revision); only its length is compared."""
        return id_matches(self.id, chip_id)

    @property
    def expect_id(self) -> str:
        """The ``expect_id`` the pod checks before it erases anything: the table's ``id`` up to
        its first ``??``."""
        return self.id.split("??", 1)[0]


def _norm(name: str) -> str:
    return "".join(c for c in str(name).lower() if c.isalnum())


def id_matches(pattern: str, chip_id: str) -> bool:
    """``chip_id`` starts with the hex bytes of ``pattern`` (``??`` matches any byte)."""
    pat = str(pattern).lower().replace(" ", "")
    got = str(chip_id).lower().replace(" ", "").replace("0x", "")
    if not pat or len(got) < len(pat):
        return False
    return all(p == "?" or p == g for p, g in zip(pat, got))


@dataclass(frozen=True)
class ProgFamily:
    """A table family: the chips one ``target`` writes."""

    proto: str
    target: str
    title: str
    devices: Tuple[ProgDevice, ...]
    #: Image regions to leave out without an error (the AVR lock bits and signature rows an
    #: avr-gcc ``.elf`` carries): ``{"hex": int, "size": int, "name": str}``.
    ignore: Tuple[Mapping[str, Any], ...] = ()

    def find(self, name: str) -> Optional[ProgDevice]:
        key = _norm(name)
        for d in self.devices:
            if key == _norm(d.name) or key in {_norm(a) for a in d.aliases}:
                return d
        return None


@lru_cache(maxsize=1)
def load_table() -> Dict[str, Any]:
    """The raw ``prog_devices.json`` (schema in ``prog_devices.README.md``)."""
    text = resources.files(__package__).joinpath("prog_devices.json").read_text("utf-8")
    data = json.loads(text)
    if data.get("schema") != 1:
        raise ValueError("prog_devices.json: unknown schema " + repr(data.get("schema")))
    return data


def _device(raw: Mapping[str, Any], proto: str) -> ProgDevice:
    mems = {}
    for name, m in raw["mem"].items():
        page = int(m.get("page", 1))
        mems[name] = ProgMemory(
            name=name, size=int(m["size"]), page=page, addr=int(m["addr"]), hex=int(m["hex"]),
            align=int(m.get("align", page if name == "flash" else 1)), stride=int(m.get("stride", 1)))
    return ProgDevice(
        name=raw["name"], family=raw.get("family", ""), proto=proto, id=str(raw["id"]).lower(),
        mem=mems, opts={k: int(v) for k, v in (raw.get("opts") or {}).items()},
        vmin_mv=int(raw.get("vmin_mv", 0)), vmax_mv=int(raw.get("vmax_mv", 0)),
        erase_vmin_mv=int(raw.get("erase_vmin_mv", 0)), vnote=str(raw.get("vnote", "")),
        aliases=tuple(raw.get("aliases", ())), notes=str(raw.get("notes", "")),
        chip_erase_only=bool(raw.get("chip_erase_only", False)))


@lru_cache(maxsize=None)
def families() -> Mapping[str, ProgFamily]:
    """The table's families by protocol (``updi``, ``pic``, ``c2``, ``tpi``, ``pdi``)."""
    out: Dict[str, ProgFamily] = {}
    for proto, fam in load_table()["families"].items():
        out[proto] = ProgFamily(
            proto=proto, target=fam["target"], title=fam.get("title", proto),
            devices=tuple(_device(d, proto) for d in fam["devices"]),
            ignore=tuple(fam.get("ignore", ())))
    return out


def protocol_for(target: str) -> str:
    """The ``prog_flash`` protocol of a ``flash(target=...)`` name (``"avr-updi"`` -> ``"updi"``).
    Raises :class:`KeyError` for a name that is not one of :data:`TARGETS`."""
    return TARGETS[str(target).strip().lower()]


def is_prog_target(target: str) -> bool:
    """``target`` names a pin-programmer target (:data:`TARGETS`, case-insensitive)."""
    return str(target).strip().lower() in TARGETS


def find_device(proto: str, *, chip_id: str = "", name: str = "") -> ProgDevice:
    """The table's chip for the pod's ``chip_id`` and/or a ``name`` the caller gave.

    A ``name`` must be in the table and, when ``chip_id`` is known, have that ID. Without a name
    the ID picks the chip; several chips with one ID (an ATxmega16A4 and 16A4U, an EFM8 family)
    must have the same memories, else the caller has to name it. Raises
    :class:`ProgDeviceError`."""
    fam = families()[proto]
    if name:
        dev = fam.find(name)
        if dev is None:
            known = ", ".join(d.name for d in fam.devices[:8])
            raise ProgDeviceError(
                f"device {name!r} is not in the {fam.title} table (it has {len(fam.devices)} chips, "
                f"for example {known}); check the spelling, or leave device out to identify the chip "
                "by its ID", result="unknown device")
        if chip_id and not dev.matches_id(chip_id):
            raise ProgDeviceError(
                f"the chip answers with ID {chip_id}, which is not a {dev.name} (ID {dev.id}); "
                "check the board, or leave device out to identify the chip by its ID",
                result="wrong chip")
        return dev
    if not chip_id:
        raise ProgDeviceError("no chip ID to look up: pass device= or read the chip first")
    found = [d for d in fam.devices if d.matches_id(chip_id)]
    if not found:
        raise ProgDeviceError(
            f"the chip answers with ID {chip_id}, which is not in the {fam.title} table "
            f"({len(fam.devices)} chips). If it is a chip this programmer supports, pass device= "
            "with its name when the table has it, or report the ID", result="unknown chip")
    first = found[0]
    if any(d.mem != first.mem or d.opts != first.opts for d in found[1:]):
        raise ProgDeviceError(
            f"ID {chip_id} fits {', '.join(d.name for d in found)}, which differ: pass device= with "
            "the one on the board", result="ambiguous chip")
    return first


def check_erase(dev: ProgDevice, erase: str) -> None:
    """Refuse ``erase="page"`` on a chip that can only be chip-erased. Raises :class:`ProgDeviceError`."""
    if dev.chip_erase_only and erase == "page":
        raise ProgDeviceError(
            f"the {dev.name} erases 32-word rows but writes 8 words at a time, so erasing single pages "
            "would wipe their neighbours: use erase=\"chip\" (the default) or erase=\"none\" on an "
            "erased chip", result="erase mode")


def check_voltage(dev: ProgDevice, volts: Optional[float], *, erase: str = "none") -> None:
    """Refuse a chip the pod's I/O voltage ``volts`` (1.8 or 3.3; ``None``: not known, no check)
    cannot program, before anything is written. Raises :class:`ProgDeviceError`."""
    if not volts:
        return
    mv = int(round(volts * 1000))
    if dev.vmin_mv and mv < dev.vmin_mv:
        if dev.vmin_mv >= 4000:
            fix = (f"the {dev.name} can only be programmed at {dev.vmin_mv / 1000:g} V or more, and the "
                   f"pod's I/O is {volts:g} V: it cannot program this chip. Use the board's own 5 V with "
                   "a level shifter on the programming pins and a programmer that drives 5 V")
        else:
            fix = (f"the {dev.name} needs at least {dev.vmin_mv / 1000:g} V to be programmed and the pod's "
                   f"I/O is {volts:g} V: set the I/O voltage to the chip's supply (set_la_voltage)")
        raise ProgDeviceError(fix + (f" ({dev.vnote})" if dev.vnote else ""), result="voltage")
    if dev.vmax_mv and mv > dev.vmax_mv:
        raise ProgDeviceError(
            f"the {dev.name} takes at most {dev.vmax_mv / 1000:g} V and the pod's I/O is {volts:g} V: "
            "set the I/O voltage to the chip's supply (set_la_voltage)", result="voltage")
    if erase == "chip" and dev.erase_vmin_mv and mv < dev.erase_vmin_mv:
        raise ProgDeviceError(
            f"the {dev.name}'s chip erase needs the supply above {dev.erase_vmin_mv / 1000:g} V (its "
            f"brown-out level) and the pod's I/O is {volts:g} V, so the erase can be ignored. Power "
            "the chip and set the I/O voltage to 3.3 V, or pass erase=\"page\" for an already "
            "erased chip", result="voltage")


# -- image files -----------------------------------------------------------------------------

#: One run of image bytes at ``addr`` (a ``.hex`` / ``.elf`` address).
Segment = Tuple[int, bytes]


def _merge(parts: Sequence[Segment]) -> List[Segment]:
    """Sorted, with touching and overlapping runs joined (a later byte wins)."""
    cells: Dict[int, int] = {}
    for addr, data in parts:
        for i, b in enumerate(data):
            cells[addr + i] = b
    out: List[Segment] = []
    start = prev = None
    buf = bytearray()
    for a in sorted(cells):
        if prev is None or a != prev + 1:
            if start is not None:
                out.append((start, bytes(buf)))
            start, buf = a, bytearray()
        buf.append(cells[a])
        prev = a
    if start is not None:
        out.append((start, bytes(buf)))
    return out


def parse_ihex(text: str) -> List[Segment]:
    """The runs of an Intel HEX file (record types 0 data, 1 end, 2 and 4 extended address)."""
    parts: List[Segment] = []
    base = 0
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {n}: not an Intel HEX record: {line[:20]!r}")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise ValueError(f"line {n}: not hex: {line[:20]!r}") from None
        if len(raw) < 5 or len(raw) != raw[0] + 5:
            raise ValueError(f"line {n}: the record length does not match its bytes")
        if sum(raw) & 0xFF:
            raise ValueError(f"line {n}: bad checksum")
        count, off, rtype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        body = raw[4:4 + count]
        if rtype == 0:
            parts.append((base + off, bytes(body)))
        elif rtype == 1:
            break
        elif rtype == 2 and count == 2:
            base = ((body[0] << 8) | body[1]) << 4
        elif rtype == 4 and count == 2:
            base = ((body[0] << 8) | body[1]) << 16
        # 3 and 5 (start address) say nothing about the contents
    return _merge(parts)


def parse_elf(data: bytes) -> List[Segment]:
    """The loadable segments of a 32-bit little-endian ELF at their load addresses (``p_paddr``:
    avr-gcc puts the EEPROM at 0x810000 and the fuses at 0x820000 there)."""
    if data[:4] != b"\x7fELF" or len(data) < 52:
        raise ValueError("not an ELF file")
    if data[4] != 1 or data[5] != 1:
        raise ValueError("only 32-bit little-endian ELF files are supported (these chips' toolchains "
                         "write them); convert with objcopy -O ihex")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    parts: List[Segment] = []
    for i in range(phnum):
        p_type, p_off, _vaddr, p_paddr, p_filesz, _memsz, _flags, _align = struct.unpack_from(
            "<IIIIIIII", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz:
            parts.append((p_paddr, data[p_off:p_off + p_filesz]))
    if not parts:
        raise ValueError("the ELF file has no loadable segments")
    return _merge(parts)


def load_image(path: Union[str, "os.PathLike[str]"], *, load_address: int = 0) -> List[Segment]:
    """The runs of image bytes in a ``.hex`` (Intel HEX), ``.elf`` or ``.bin`` file. A ``.bin``
    goes at ``load_address``."""
    p = os.fspath(path)
    with open(p, "rb") as fh:
        raw = fh.read()
    ext = os.path.splitext(p)[1].lower()
    if raw[:4] == b"\x7fELF" or ext in (".elf", ".axf", ".out"):
        segs = parse_elf(raw)
    elif ext in (".hex", ".ihex", ".ihx", ".a43") or raw[:1] == b":":
        segs = parse_ihex(raw.decode("ascii", errors="replace"))
    else:
        segs = [(load_address, raw)] if raw else []
    if not segs:
        raise ValueError(f"{p} holds no image bytes")
    return segs


# -- cutting an image into the chip's memories -------------------------------------------------

@dataclass
class Region:
    """One ``prog_flash`` write: ``data`` into memory ``mem`` at the pod's address ``addr``."""

    mem: str
    addr: int
    data: bytes
    page: int
    hex: int = 0


def _align(v: int, unit: int, up: bool) -> int:
    return -(-v // unit) * unit if up else v // unit * unit


def split_regions(segments: Sequence[Segment], dev: ProgDevice, *,
                  ignore: Sequence[Mapping[str, Any]] = ()) -> Tuple[List[Region], List[str]]:
    """Cut ``segments`` into the chip's memories. Returns the regions in write order (flash,
    EEPROM, user, config; each ascending) and notes about what was left out.

    A region starts and ends on its memory's ``align`` (flash: a page; a partial page is padded with
    0xFF); regions that touch after that are one. Bytes outside every memory of ``dev`` raise
    :class:`ProgDeviceError` (the message says where the memories are), unless ``ignore`` names
    them (lock bits and signature rows the toolchain adds). An image with no bytes for the chip
    raises it too."""
    notes: List[str] = []
    placed: Dict[str, Dict[int, int]] = {m: {} for m in dev.mem}
    for hexaddr, data in segments:
        i = 0
        while i < len(data):
            a = hexaddr + i
            hit = next((m for m in dev.mem.values() if m.hex <= a < m.hex + m.size * m.stride), None)
            if hit is None:
                skip = next((g for g in ignore if int(g["hex"]) <= a < int(g["hex"]) + int(g["size"])), None)
                if skip is not None:
                    n = min(len(data) - i, int(skip["hex"]) + int(skip["size"]) - a)
                    note = f"left out the {skip.get('name', 'region')} at 0x{a:x} ({n} bytes): the programmer does not write it"
                    if note not in notes:
                        notes.append(note)
                    i += n
                    continue
                raise ProgDeviceError(
                    f"the image has data at 0x{a:x} that is not in any memory of the {dev.name}: "
                    + _layout(dev) + "; build for this chip, or pass device= for the right one",
                    result="image does not fit")
            end = min(len(data) - i, hit.hex + hit.size * hit.stride - a)
            for k in range(0, end):
                off = a + k - hit.hex
                if hit.stride > 1:
                    if off % hit.stride:
                        continue  # the high byte of a 16-bit word: only the low byte is the value
                    placed[hit.name][off // hit.stride] = data[i + k]
                else:
                    placed[hit.name][off] = data[i + k]
            i += end
    regions: List[Region] = []
    for name in MEM_ORDER:
        cells = placed.get(name)
        if not cells:
            continue
        m = dev.mem[name]
        # runs of bytes, aligned out to the memory's unit, then joined when they touch
        runs: List[List[int]] = []
        run_start = prev = None
        for off in sorted(cells):
            if prev is None or off != prev + 1:
                if run_start is not None:
                    runs.append([run_start, prev + 1])
                run_start = off
            prev = off
        runs.append([run_start, prev + 1])
        joined: List[List[int]] = []
        for lo, hi in runs:
            lo, hi = _align(lo, m.align, False), _align(hi, m.align, True)
            if joined and lo <= joined[-1][1]:
                joined[-1][1] = max(joined[-1][1], hi)
            else:
                joined.append([lo, hi])
        for lo, hi in joined:
            hi = min(hi, m.size)  # rounding a last page up never goes past the memory
            buf = bytes(cells.get(o, 0xFF) for o in range(lo, hi))
            regions.append(Region(mem=name, addr=m.addr + lo, data=buf, page=m.page,
                                  hex=m.hex + lo * m.stride))
    if not regions:
        raise ProgDeviceError(f"the image has no bytes for the {dev.name}: " + _layout(dev),
                              result="image does not fit")
    return regions, notes


def _layout(dev: ProgDevice) -> str:
    parts = [f"{n} 0x{m.hex:x}..0x{m.hex + m.size * m.stride - 1:x}" for n, m in dev.mem.items()]
    return "it has " + ", ".join(parts)


# -- results from the pod ----------------------------------------------------------------------

#: What each ``result`` of the pod's ``prog_flash`` status means for the user, by the start of the
#: pod's text. (kind, fix.) The pod's own words are kept in :attr:`ProgFlashError.result`.
_FIXES = (
    ("no answer", "no_answer",
     "the chip did not answer. Check the wiring ({pins}), the chip's power (target_power, or its own "
     "supply) with a common ground, that the pod's I/O voltage ({volts}) is the chip's, and that the "
     "chip's programming pin is not disabled by its fuses or program"),
    ("wrong chip", "wrong_chip",
     "a different chip than the one expected answered (ID {id}); check the board, or pass device= "
     "for the chip that is there"),
    ("locked", "locked",
     "the chip is locked: it refuses reads and writes until its memory is erased. Flash with "
     "erase=\"chip\" (that erases the chip, including the lock) or erase it with the vendor's tool"),
    ("verify failed", "verify",
     "a page read back different from what was written (at {at}). Check the supply voltage and the "
     "wiring, lower the load on the pins, and try again; a worn or write-protected region fails "
     "like this too"),
    ("timeout", "timeout",
     "the chip stayed busy: its supply is too low for erase and write (see the chip's minimum VDD), "
     "or the chip is damaged"),
    ("protocol error", "protocol",
     "the chip's answer broke the protocol: check the wiring, long wires or a pull-up that is too "
     "weak (keep the lines short), the I/O voltage and the chip's supply"),
    ("memory, address", "args",
     "the pod refused this memory, address, length or page size for the chip: the device table "
     "and the image disagree with the chip's firmware options; pass device= for the exact chip"),
    ("could not read the staged", "stage",
     "the pod could not read the image it was given: try again, and report it if it repeats"),
    ("not supported", "unsupported",
     "this chip, memory or protocol is not supported by the pod's firmware; update the pod, or "
     "check that the chip is one the table lists"),
    ("aborted", "aborted", "the write was aborted"),
)


def result_error(res: str, *, st: Mapping[str, Any], region: Optional[Region], proto: str,
                 pins: str, volts: str, id_hex: str) -> ProgFlashError:
    """The exception for a failed ``result`` of the pod's status."""
    kind, fix = "failed", ""
    for prefix, k, text in _FIXES:
        if res.startswith(prefix):
            kind, fix = k, text
            break
    at = st.get("at_addr")
    phase = str(st.get("phase") or "")
    where = ""
    if region is not None:
        where = f" writing {region.mem} at 0x{region.addr:x} ({len(region.data)} bytes)"
    fix = fix.format(pins=pins, volts=volts, id=id_hex or "unknown",
                     at=f"0x{int(at):x}" if at is not None else "an unknown address")
    msg = f"{proto} flash failed{where}: {res}" + (f" (phase {phase})" if phase else "")
    if fix:
        msg += "\n" + fix
    kw = dict(result=res, phase=phase, mem=region.mem if region else "",
              addr=region.addr if region else None,
              at_addr=int(at) if at is not None else None)
    cls = {"no_answer": ProgNoAnswerError, "locked": ProgLockedError,
           "verify": ProgVerifyError}.get(kind, ProgFlashError)
    return cls(msg, **kw)  # type: ignore[arg-type]


# -- the run ---------------------------------------------------------------------------------

@dataclass
class ProgRegionResult:
    """One region written: its memory, the pod's address, length in bytes and seconds."""

    mem: str
    addr: int
    length: int
    seconds: float = 0.0


@dataclass
class ProgFlashResult:
    """Outcome of :func:`run`."""

    ok: bool
    device: str = ""
    chip_id: str = ""
    regions: List[ProgRegionResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""


def start_request(proto: str, pins: Mapping[str, Any], *, region: Region, dev: ProgDevice,
                  erase: str, run_after: bool) -> Dict[str, Any]:
    """The ``prog_flash`` ``start`` command for one region."""
    req: Dict[str, Any] = {"cmd": "prog_flash", "op": "start", "proto": proto, **pins,
                           "mem": region.mem, "addr": region.addr, "len": len(region.data),
                           "page": region.page, "erase": erase, "run": bool(run_after)}
    if dev.expect_id:
        req["expect_id"] = dev.expect_id
    for key in ("flags", "baud"):
        if dev.opts.get(key):
            req[key] = dev.opts[key]
    return req


def read_info(command: Callable[[Dict[str, Any]], Any], proto: str, pins: Mapping[str, Any], *,
              poll_interval: float = POLL_INTERVAL, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic, pin_text: str = "",
              volts: str = "") -> str:
    """Enter programming mode and read the chip's ID (nothing is written): the pod's ``info``.
    Returns the ID as hex. Raises :class:`ProgNoAnswerError` and the other
    :class:`ProgFlashError` subclasses on a failure."""
    command({"cmd": "prog_flash", "op": "info", "proto": proto, **pins})
    deadline = clock() + INFO_TIMEOUT
    while True:
        sleep(poll_interval)
        st = command({"cmd": "prog_flash", "op": "status"}) or {}
        if not st.get("running"):
            break
        if clock() > deadline:
            command({"cmd": "prog_flash", "op": "abort"})
            raise ProgNoAnswerError(
                f"the {proto} chip did not finish answering in {INFO_TIMEOUT:g} s. Check the wiring "
                f"({pin_text}) and power.", result="timeout", phase=str(st.get("phase") or ""))
    res = str(st.get("result") or "")
    cid = str(st.get("id") or "")
    if res != "ok":
        raise result_error(res, st=st, region=None, proto=proto, pins=pin_text, volts=volts, id_hex=cid)
    return cid


def run(
    command: Callable[[Dict[str, Any]], Any],
    stage: Optional[Callable[[bytes], Any]],
    proto: str,
    dev: ProgDevice,
    regions: Sequence[Region],
    *,
    pins: Mapping[str, Any],
    erase: str = "chip",
    run_after: bool = True,
    progress: Optional[Progress] = None,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    pin_text: str = "",
    volts: str = "",
    chip_id: str = "",
) -> ProgFlashResult:
    """Write ``regions`` one after the other: stage, ``prog_flash`` start, poll ``status`` until
    done. ``command`` sends one JSON command and returns its ``data``; ``stage`` is the transport's
    ``stage_psram``. ``erase`` ("chip", "page" or "none") is meant for the first region only when
    it is a chip erase (the later regions then erase nothing). Raises :class:`ProgFlashError`
    (or a subclass) at the first failed region."""
    if stage is None:
        raise FlashError("flashing a chip over its programming pins needs a network, USB or cloud "
                         "connection (the image is uploaded into the pod's memory first), not the "
                         "serial console")
    if erase not in ("chip", "page", "none"):
        raise ValueError(f"erase must be 'chip', 'page' or 'none', not {erase!r}")
    out = ProgFlashResult(ok=False, device=dev.name, chip_id=chip_id)
    t_all = clock()
    chip_erased = False
    for i, reg in enumerate(regions):
        t0 = clock()
        stage(reg.data)
        mode = erase
        if erase == "chip":
            mode = "none" if chip_erased else "chip"
        last = i == len(regions) - 1
        command(start_request(proto, pins, region=reg, dev=dev, erase=mode,
                              run_after=run_after and last))
        chip_erased = chip_erased or mode == "chip"
        # Erase plus the pages at the slowest of the five (EEPROM bytes at 10 ms each), plus a margin.
        deadline = t0 + 60.0 + len(reg.data) / 200.0
        st: Mapping[str, Any] = {}
        while True:
            sleep(poll_interval)
            st = command({"cmd": "prog_flash", "op": "status"}) or {}
            if progress:
                progress(int(st.get("done", 0) or 0), int(st.get("total", 0) or 0))
            if not st.get("running"):
                break
            if clock() > deadline:
                command({"cmd": "prog_flash", "op": "abort"})
                out.seconds = clock() - t_all
                raise ProgFlashError(
                    f"the write of the {reg.mem} at 0x{reg.addr:x} did not finish in time (stopped "
                    f"in phase {st.get('phase')}): check the chip's supply and the wiring",
                    result="timeout", phase=str(st.get("phase") or ""), mem=reg.mem, addr=reg.addr)
        res = str(st.get("result") or "")
        out.chip_id = str(st.get("id") or out.chip_id)
        if res != "ok":
            out.seconds = clock() - t_all
            raise result_error(res, st=st, region=reg, proto=proto, pins=pin_text, volts=volts,
                               id_hex=out.chip_id)
        out.regions.append(ProgRegionResult(mem=reg.mem, addr=reg.addr, length=len(reg.data),
                                            seconds=clock() - t0))
        if progress:
            progress(int(st.get("total", 0) or 0), int(st.get("total", 0) or 0))
    out.ok = True
    out.seconds = clock() - t_all
    return out
