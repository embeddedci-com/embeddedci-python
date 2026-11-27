"""Find out which chip is wired to the pod (:meth:`BenchPod.detect <embeddedci.benchpod.BenchPod.detect>`).

One call tries every way the wiring profile makes possible and reports what answered:

* ``swd`` and ``jtag``: the pod's debug engine reads the debug port's IDs (``dap_start`` with
  ``op: "id"``, firmware feature ``dap_id``). Nothing is halted, reset or written. The SDK maps the
  answer: the Arm core from the CPUID, then the vendor's ID register for that core (an STM32's
  DBGMCU_IDCODE, a Nordic FICR) to a part name; a JTAG chain's IDCODEs the same way.
* ``updi``, ``pic``, ``c2``, ``tpi``, ``pdi``, ``sbw``: the pod enters the chip's programming mode and reads
  its ID (the ``prog_flash`` ``info`` op, the same read as ``flash(..., dry_run=True)``). Invasive:
  it resets the chip.
* ``esp32``: the pod syncs with the chip's ROM loader (``esp_flash`` ``sync``) and the loader names
  the chip. Invasive: it resets the chip into its loader.
* ``usb``: the device already mounted on the pod's USB host port (host mode is never turned on
  here): its VID:PID, strings and, for a known bootloader, which one.

A method is tried only when the wiring profile wires its pins. The programming protocols and the
ESP32 are tried only when the profile has a signal *named for the protocol* (``ICSPCLK`` and
``ICSPDAT``, ``UPDI``, ``C2CK`` and ``C2D``, ``TPICLK`` and ``TPIDATA``, ``PDI_CLK`` and
``PDI_DATA``, ``SBWTCK`` or ``TEST`` and ``SBWTDIO`` or ``RST``; a ``BOOT`` / ``IO0`` / ``IO9`` signal next to ``uart_rx`` and ``uart_tx``): unlike
``flash()`` there is no fallback to the SWD pins, because detect runs without being told what the
chip is, and driving an SWD line as a programming pin would be a guess.

The ID table is ``detect_ids.json`` next to this module. This module holds the pure mapping
functions (reply to :class:`DetectFinding`) and :func:`run`, the orchestration over a
:class:`DetectEnv` of callables, so it is tested with a fake ``command`` like :mod:`.prog`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import esp as _esp
from . import prog as _prog
from . import usb_host as _usb
from .errors import BenchPodError, DeviceBusyError, DirectionConflictError, TransportError
from .wiring import Wiring

#: Every method :func:`run` knows, in the order it tries them.
METHODS: Tuple[str, ...] = ("swd", "jtag", "updi", "pic", "c2", "tpi", "pdi", "sbw", "esp32", "usb")
#: The methods that reset the chip or drive its programming pins; ``invasive=False`` leaves them out.
INVASIVE_METHODS: Tuple[str, ...] = ("updi", "pic", "c2", "tpi", "pdi", "sbw", "esp32")

CONFIDENCES = ("low", "medium", "high")

#: Why SWD and JTAG are not tried on firmware without the ``dap_id`` feature.
DAP_ID_REASON = "needs pod firmware with dap_id (update the pod)"

#: Addresses one ``dap_start`` ``op: "id"`` reads at most (the firmware takes up to 8).
MAX_READS = 8
#: How long the first SWD or JTAG ID read may retry for an answer (``wait_ms`` of ``dap_start``
#: ``op: "id"``): a NUCLEO's ST-LINK boots first and SWD answers about 2 s after power on. The
#: firmware returns as soon as the target answers, so a present target costs nothing extra.
SWD_ANSWER_WAIT_MS = 3000
#: The longest the ESP32 ROM loader gets to answer.
ESP_TIMEOUT = 10.0
ESP_POLL_INTERVAL = 0.25

_VENDOR = {"updi": "Microchip (Atmel)", "pdi": "Microchip (Atmel)", "tpi": "Microchip (Atmel)",
           "pic": "Microchip", "c2": "Silicon Labs", "sbw": "Texas Instruments"}
_ARCH = {"updi": "AVR", "pdi": "AVR", "tpi": "AVR", "pic": "PIC", "c2": "8051", "sbw": "MSP430"}


# -- result types ------------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectFinding:
    """One thing that answered.

    ``method`` is the probe (``swd``, ``jtag``, ``updi``, ``pic``, ``c2``, ``tpi``, ``pdi``, ``sbw``,
    ``esp32``, ``usb``); ``chip`` the part name (``"STM32F446"``), or ``None`` when only the vendor
    or the debug port is known (for a debug port alone it names the port); ``vendor`` and ``core``
    (``"Cortex-M4"``) as far as known; ``id`` the number the match rests on (``"0x10006421"``, the
    STM32's DBGMCU_IDCODE; the programming-mode ID bytes; ``"16c0:0478"`` for USB). ``confidence``:
    ``"high"`` for an exact table match (an STM32 DEV_ID at its family's address, a programming
    ID in the device table, an ESP chip name, a known VID:PID), ``"medium"`` when the vendor and
    core are known but not the part, ``"low"`` when only a debug port answered. ``pins`` are the LA
    channels the probe used (``"nrst"`` for the pod's reset pin), ``raw`` the pod's reply, and
    ``invasive`` whether the probe reset or drove the chip."""

    method: str
    chip: Optional[str] = None
    vendor: Optional[str] = None
    core: Optional[str] = None
    id: Optional[str] = None
    confidence: str = "low"
    pins: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    invasive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"method": self.method, "chip": self.chip, "vendor": self.vendor, "core": self.core,
                "id": self.id, "confidence": self.confidence, "pins": dict(self.pins),
                "raw": dict(self.raw), "invasive": self.invasive}

    @property
    def summary(self) -> str:
        """One line: ``"STM32F446 (STMicroelectronics, Cortex-M4) by swd, high"``."""
        what = self.chip or " ".join(x for x in (self.vendor, self.core) if x) or "unknown"
        if not self.chip and self.id:
            what += f", id {self.id}"
        extra = ", ".join(x for x in (self.vendor if self.chip else None, self.core if self.chip else None) if x)
        return f"{what}{f' ({extra})' if extra else ''} by {self.method}, {self.confidence}"


@dataclass
class DetectResult:
    """What :meth:`BenchPod.detect <embeddedci.benchpod.BenchPod.detect>` found.

    ``powered``: the target's power rail is on (when it is off nothing is probed). ``findings``:
    what answered, in probe order. ``skipped``: ``(method, reason)`` for a probe that was wired but
    not run or got no answer (a pin owned by something else, firmware too old, an invasive probe
    with ``invasive=False``, no answer from the chip). ``notes``: everything else worth saying,
    including the methods not tried because the profile does not wire them."""

    powered: bool = True
    findings: List[DetectFinding] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """``{"powered", "findings": [...], "skipped": [{"method", "reason"}], "notes": [...]}``."""
        return {"powered": self.powered, "findings": [f.to_dict() for f in self.findings],
                "skipped": [{"method": m, "reason": r} for m, r in self.skipped],
                "notes": list(self.notes)}


# -- the ID table ------------------------------------------------------------------------------

def _int(value: Any) -> Optional[int]:
    """``"0x10006421"`` / ``0x10006421`` / ``"10006421"`` (hex) as an int; ``None`` when it is none."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def hex32(value: int) -> str:
    """``0x%08x``, the form the pod's replies and this table use."""
    return "0x%08x" % (value & 0xFFFFFFFF)


@lru_cache(maxsize=1)
def load_table() -> Dict[str, Any]:
    """The raw ``detect_ids.json``."""
    text = resources.files(__package__).joinpath("detect_ids.json").read_text("utf-8")
    data = json.loads(text)
    if data.get("version") != 1:
        raise ValueError("detect_ids.json: unknown version " + repr(data.get("version")))
    return data


@dataclass(frozen=True)
class IdRegister:
    """One ``id_registers`` entry: the register at ``addr`` of a vendor's chips whose cores are in
    ``cores`` (empty: any), and ``parts`` (the register value masked with ``mask``) by name."""

    vendor: str
    what: str
    addr: int
    mask: int
    cores: Tuple[str, ...]
    parts: Mapping[int, str]
    stm32: bool = False


@lru_cache(maxsize=1)
def id_registers() -> Tuple[IdRegister, ...]:
    out = []
    for r in load_table()["id_registers"]:
        out.append(IdRegister(
            vendor=r["vendor"], what=r.get("what", ""), addr=int(r["addr"], 16),
            mask=int(r["mask"], 16), cores=tuple(r.get("cores", ())),
            parts={int(k, 16): v for k, v in r["parts"].items()}, stm32=bool(r.get("stm32"))))
    return tuple(out)


def _masked(section: str) -> List[Tuple[int, int, Mapping[str, Any]]]:
    return [(int(e["mask"], 16), int(e["value"], 16), e) for e in load_table().get(section, ())]


def _match_port(section: str, value: int) -> Optional[Mapping[str, Any]]:
    for mask, want, entry in _masked(section):
        if value & mask == want:
            return entry
    return None


def jep106_name(designer: int) -> Optional[str]:
    """The vendor of a JEP106 designer code (an IDCODE's or TARGETID's 11-bit manufacturer field)."""
    return load_table().get("jep106", {}).get("0x%03x" % designer)


def stm32_name(dev_id: int) -> Optional[str]:
    """The STM32 whose DEV_ID (the low 12 bits of DBGMCU_IDCODE) is ``dev_id``, from any family's
    table; ``None`` when none has it. Used where only the ID is known, not the register's address
    (a boundary-scan IDCODE, a TARGETID)."""
    for reg in id_registers():
        if reg.stm32 and dev_id in reg.parts:
            return reg.parts[dev_id]
    return None


def core_from_cpuid(cpuid: Any) -> Optional[str]:
    """The Arm core of a CPUID (``0x410fc241`` is ``"Cortex-M4"``); ``"Arm part 0x..."`` for a part
    the table lacks, ``None`` for a value that is not an Arm CPUID."""
    value = _int(cpuid)
    if value is None or (value >> 24) != 0x41:
        return None
    part = (value >> 4) & 0xFFF
    return load_table().get("arm_cores", {}).get("0x%03x" % part) or f"Arm part 0x{part:03x}"


def candidate_registers(core: Optional[str]) -> List[IdRegister]:
    """The ID registers worth reading on a chip with ``core`` (every one for an unknown core)."""
    regs = [r for r in id_registers() if core is None or not r.cores or core in r.cores]
    out: List[IdRegister] = []
    for r in regs:
        if all(r.addr != o.addr for o in out):
            out.append(r)
    return out[:MAX_READS]


def candidate_addresses(core: Optional[str]) -> List[str]:
    """:func:`candidate_registers` as the ``read`` list of ``dap_start`` ``op: "id"``."""
    return [hex32(r.addr) for r in candidate_registers(core)]


def lookup_part(addr: Any, value: Any) -> Optional[Tuple[IdRegister, str]]:
    """The register at ``addr`` and the part its ``value`` names, or ``None``."""
    a, v = _int(addr), _int(value)
    if a is None or v is None:
        return None
    for reg in id_registers():
        if reg.addr == a and (v & reg.mask) in reg.parts:
            return reg, reg.parts[v & reg.mask]
    return None


# -- mapping: debug port replies ---------------------------------------------------------------

def _plausible_stm32_idcode(value: int) -> bool:
    """A DBGMCU_IDCODE has a DEV_ID and a revision, so it is neither 0, nor all ones, nor a small
    number: the test that keeps a read of another vendor's address from being taken for an unknown
    STM32."""
    return value not in (0, 0xFFFFFFFF) and (value >> 16) != 0 and (value & 0xFFF) != 0


def _reads(reply: Mapping[str, Any]) -> List[Tuple[int, int]]:
    out = []
    for r in reply.get("reads") or ():
        if isinstance(r, Mapping):
            a, v = _int(r.get("addr")), _int(r.get("value"))
            if a is not None and v is not None:
                out.append((a, v))
    return out


def finding_from_swd(reply: Mapping[str, Any], *, pins: Optional[Mapping[str, Any]] = None
                     ) -> Optional[DetectFinding]:
    """A ``dap_start`` ``op: "id"`` SWD reply as a finding; ``None`` when nothing answered.

    The best evidence wins: an ID register read that names a part (high), then an ST TARGETID
    (medium: the part number in it is the DEV_ID by ST's convention), then an ST DBGMCU_IDCODE
    that is not in the table (medium: vendor known, part not), then the debug port alone (low)."""
    if not reply.get("found"):
        return None
    dpidr, targetid, cpuid = _int(reply.get("dpidr")), _int(reply.get("targetid")), _int(reply.get("cpuid"))
    core = core_from_cpuid(cpuid)
    base: Dict[str, Any] = {"method": "swd", "core": core, "pins": dict(pins or {}), "raw": dict(reply)}

    reads = _reads(reply)
    for addr, value in reads:
        hit = lookup_part(addr, value)
        if hit is not None:
            reg, name = hit
            return DetectFinding(chip=name, vendor=reg.vendor, id=hex32(value), confidence="high", **base)
    if targetid is not None and (targetid >> 1) & 0x7FF == 0x020:
        name = stm32_name((targetid >> 12) & 0xFFF)
        return DetectFinding(chip=name, vendor=jep106_name(0x020), id=hex32(targetid),
                             confidence="medium", **base)
    for addr, value in reads:
        reg = next((r for r in id_registers() if r.addr == addr and r.stm32), None)
        if reg is not None and _plausible_stm32_idcode(value):
            return DetectFinding(chip=None, vendor=reg.vendor, id=hex32(value), confidence="medium", **base)
    port = _match_port("dpidr", dpidr) if dpidr is not None else None
    return DetectFinding(chip=port["name"] if port else None, vendor=None,
                         id=hex32(dpidr) if dpidr is not None else None, confidence="low", **base)


def findings_from_jtag(reply: Mapping[str, Any], *, pins: Optional[Mapping[str, Any]] = None
                       ) -> List[DetectFinding]:
    """The TAPs of a ``dap_start`` ``op: "id"`` JTAG reply (``idcodes``, the one nearest TDO first)
    as findings, one per device.

    An Arm debug port is low (the chip behind it is not named), an Espressif IDCODE is high. An ST
    IDCODE that is not in the table is a boundary-scan TAP: its bits 12 to 23 are the STM32's DEV_ID
    (``0x06421041`` is DEV_ID ``0x421``, an STM32F446), high when the DEV_ID is in the table and
    medium when it is not. Any other IDCODE is low, with the vendor from its JEP106 code."""
    out: List[DetectFinding] = []
    if not reply.get("found"):
        return out
    for index, raw_code in enumerate(reply.get("idcodes") or ()):
        code = _int(raw_code)
        if code is None or code in (0, 0xFFFFFFFF):
            continue
        base = {"method": "jtag", "pins": dict(pins or {}),
                "raw": {**reply, "index": index}, "id": hex32(code)}
        entry = _match_port("jtag", code)
        if entry is not None:
            debug_port = bool(entry.get("debug_port"))
            out.append(DetectFinding(chip=entry["name"], vendor=entry.get("vendor"),
                                     confidence="low" if debug_port else "high", **base))
            continue
        designer = (code >> 1) & 0x7FF
        if designer == 0x020:
            name = stm32_name((code >> 12) & 0xFFF)
            out.append(DetectFinding(chip=name, vendor=jep106_name(designer),
                                     confidence="high" if name else "medium", **base))
        else:
            out.append(DetectFinding(chip=None, vendor=jep106_name(designer), confidence="low", **base))
    return out


# -- mapping: USB ------------------------------------------------------------------------------

def vid_pid(vid: int, pid: int) -> str:
    return f"{vid:04x}:{pid:04x}"


def usb_name(device: Any) -> Tuple[Optional[str], str]:
    """What a USB device is: ``(name, confidence)``. The table's VID:PID is high. Otherwise the
    classifier of :mod:`.usb_host` (the facts ``usb_flash`` picks its loader from: a DFU interface,
    the Espressif and Raspberry Pi IDs, USB-serial chips, Arduino AVR and SAMD boards) gives a
    medium name. Anything else has none."""
    vid, pid = int(device.vid), int(device.pid)
    named = load_table().get("usb", {}).get(vid_pid(vid, pid))
    if named:
        return named, "high"
    if vid == _usb.VID_RPI and pid in _usb.PICOBOOT_PIDS:
        return "Raspberry Pi Pico (BOOTSEL)", "medium"
    if device.has_interface(_usb.CLASS_DFU, 1):
        return "USB device in a DFU bootloader", "medium"
    if vid == _usb.VID_ESPRESSIF:
        return "Espressif USB device", "medium"
    if vid in _usb.SERIAL_CHIPS:
        return f"{_usb.SERIAL_CHIPS[vid]} USB-serial chip", "medium"
    if _usb._arduino_samd(vid, pid):
        return "Arduino-compatible SAMD board", "medium"
    if _usb._arduino_avr(vid, pid):
        return "Arduino-compatible AVR board", "medium"
    return None, "low"


def finding_from_usb(device: Any, *, port: str = "") -> DetectFinding:
    """The device mounted on the pod's USB host port (a :class:`~embeddedci.benchpod.usb_host.UsbDevice`)
    as a finding. ``vendor`` is the device's manufacturer string, ``chip`` what :func:`usb_name`
    says (``None`` for an unknown device; its strings are in ``raw``)."""
    name, confidence = usb_name(device)
    raw = {"vid": device.vid, "pid": device.pid, "vid_pid": vid_pid(device.vid, device.pid),
           "manufacturer": device.manufacturer, "product": device.product, "speed": device.speed}
    return DetectFinding(method="usb", chip=name, vendor=device.manufacturer or None,
                         core=None, id=vid_pid(device.vid, device.pid), confidence=confidence,
                         pins={"port": port} if port else {}, raw=raw)


# -- wiring: which pins a method has ------------------------------------------------------------

def _signal_la(wiring: Wiring, names: Sequence[str]) -> Optional[int]:
    for s in wiring.signals:
        if s.name.upper() in names:
            return s.la
    return None


def swd_pins(wiring: Wiring) -> Optional[Dict[str, int]]:
    if wiring.swd_swclk is None or wiring.swd_swdio is None:
        return None
    return {"swclk": wiring.swd_swclk, "swdio": wiring.swd_swdio}


def jtag_pins(wiring: Wiring) -> Optional[Dict[str, int]]:
    keys = ("tck", "tms", "tdi", "tdo")
    vals = [getattr(wiring, "jtag_" + k) for k in keys]
    if any(v is None for v in vals):
        return None
    pins = dict(zip(keys, vals))
    if wiring.jtag_trst is not None:
        pins["trst"] = wiring.jtag_trst
    return pins


@dataclass(frozen=True)
class ProgPins:
    """The ``prog_flash`` pins of one protocol from the wiring profile, in words in ``text``."""

    pins: Dict[str, Any]
    text: str


def prog_pins(wiring: Wiring, proto: str) -> Tuple[Optional[ProgPins], str, bool]:
    """``(pins, reason, silent)`` for protocol ``proto``. ``pins`` is ``None`` when the method
    cannot be tried; ``silent`` says the profile has nothing for it at all (it is then only a note
    that it was not tried), else ``reason`` is why a partly wired one is skipped.

    Only a profile signal named for the protocol counts (:data:`~embeddedci.benchpod.prog.CLK_SIGNALS`
    and ``DATA_SIGNALS``), never the SWD pins. PIC and TPI also drive the chip's reset: a profile
    signal named MCLR, RESET, NRST or PROG_RESET, else the pod's reset pin when the profile says it
    is wired (``swd_nreset``), else the chip cannot be entered and the method is skipped."""
    data_names = _prog.DATA_SIGNALS.get(proto, ())
    clk_names = _prog.CLK_SIGNALS.get(proto, ())
    data = _signal_la(wiring, data_names)
    clk = _signal_la(wiring, clk_names) if clk_names else None
    if data is None and clk is None:
        return None, "", True
    one_wire = not clk_names
    if data is None or (not one_wire and clk is None):
        have = "/".join(clk_names if data is None else data_names)
        need = "/".join(data_names if data is None else clk_names)
        return None, f"the profile has a {have} signal but no {need}", False
    pins: Dict[str, Any] = {"data": data}
    text = [f"data on LA{data}"]
    if not one_wire:
        pins["clk"] = clk
        text.insert(0, f"clk on LA{clk}")
        if clk == data:
            return None, "clk and data are the same LA pin", False
    if proto in ("pic", "tpi"):
        reset = _signal_la(wiring, _prog.RESET_SIGNALS)
        if reset is not None:
            pins["reset"] = reset
            text.append(f"reset on LA{reset}")
        elif wiring.swd_nreset:
            pins["reset"] = "nrst"
            text.append("reset on the pod's reset pin")
        else:
            return None, ("the chip's reset is needed to enter programming mode: name a profile "
                          "signal MCLR or RESET, or set swd_nreset when it is on the pod's reset pin"), False
        if pins["reset"] in (pins.get("clk"), pins["data"]):
            return None, "the reset pin is the same LA pin as clk or data", False
    return ProgPins(pins, ", ".join(text)), "", False


def esp_pins(wiring: Wiring) -> Tuple[Optional[Dict[str, Any]], str, bool]:
    """``(pins, reason, silent)`` for the ESP32 ROM loader: ``uart_rx`` and ``uart_tx`` plus a
    profile signal named BOOT, IO0, GPIO0, IO9, GPIO9 or ESP_BOOT; EN is a signal named EN, else the
    pod's reset pin when the profile says it is wired (``swd_nreset``)."""
    boot = _signal_la(wiring, _esp.BOOT_SIGNALS)
    if boot is None:
        return None, "", True
    if wiring.uart_rx is None or wiring.uart_tx is None:
        return None, "the profile has a boot signal but no uart_rx and uart_tx", False
    en: Any = _signal_la(wiring, _esp.EN_SIGNALS)
    if en is None:
        if not wiring.swd_nreset:
            return None, ("EN is needed to reset the chip into its loader: name a profile signal EN, "
                          "or set swd_nreset when it is on the pod's reset pin"), False
        en = "nrst"
    return {"rx": wiring.uart_rx, "tx": wiring.uart_tx, "boot": boot, "en": en}, "", False


def _la_pins_of(pins: Mapping[str, Any]) -> Dict[str, int]:
    """The entries of ``pins`` that are LA channels (not ``"nrst"``)."""
    return {k: v for k, v in pins.items() if isinstance(v, int) and not isinstance(v, bool)}


# -- orchestration -----------------------------------------------------------------------------

@dataclass
class DetectEnv:
    """What :func:`run` needs from the pod, as callables, so it runs against a fake.

    ``command`` sends one JSON command and returns its data. ``wiring`` is the profile.
    ``supports(feature)``. ``target_status()`` returns a :class:`~embeddedci.benchpod.state.TargetStatus`.
    ``la_voltage()`` the LA I/O voltage in volts, ``None`` when none is set. ``pin_owners()`` maps
    each LA channel that something owns to that owner's name (omit it when the pod cannot say).
    ``usb_state()`` the :class:`~embeddedci.benchpod.usb_host.UsbHostState`. ``prepare(what)``
    readies the pod for ``"swd"``, ``"jtag"`` or ``"prog"`` (gateware image, firmware feature) and
    raises a :class:`~embeddedci.benchpod.errors.BenchPodError` that says why it cannot."""

    command: Callable[[Dict[str, Any]], Any]
    wiring: Wiring
    supports: Callable[[str], bool]
    target_status: Callable[[], Any]
    la_voltage: Callable[[], Optional[float]]
    pin_owners: Optional[Callable[[], Dict[int, str]]] = None
    usb_state: Optional[Callable[[], Any]] = None
    prepare: Callable[[str], None] = lambda what: None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def normalize_methods(methods: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """``methods`` as names from :data:`METHODS`, in probe order. Raises :class:`ValueError`."""
    if methods is None:
        return METHODS
    if isinstance(methods, str):
        methods = [methods]
    wanted = [str(m).strip().lower() for m in methods]
    bad = [m for m in wanted if m not in METHODS]
    if bad:
        raise ValueError(f"unknown detect method {', '.join(map(repr, bad))}; use {', '.join(METHODS)}")
    return tuple(m for m in METHODS if m in wanted)


def _reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 300 else text[:297] + "..."


def _is_connection_problem(exc: BaseException) -> bool:
    """A lost or refused connection ends the whole detect; every other pod error only ends its probe."""
    return isinstance(exc, (TransportError, DeviceBusyError))


class _Run:
    def __init__(self, env: DetectEnv, invasive: bool, selected: Tuple[str, ...], explicit: bool) -> None:
        self.env = env
        self.invasive = invasive
        self.selected = selected
        self.explicit = explicit
        self.result = DetectResult()
        self.owners: Dict[int, str] = {}
        self.voltage_set = True
        self.unwired: List[str] = []

    # -- bookkeeping --

    def skip(self, method: str, reason: str) -> None:
        self.result.skipped.append((method, reason))

    def not_wired(self, method: str, why: str = "") -> None:
        """A method the profile does not wire: skipped when it was asked for, else one note."""
        if self.explicit:
            self.skip(method, why or "not wired in the profile")
        else:
            self.unwired.append(method)

    def owned(self, method: str, pins: Mapping[str, Any]) -> bool:
        """Skip ``method`` when something else owns one of its LA pins."""
        for name, la in _la_pins_of(pins).items():
            owner = self.owners.get(la)
            if owner:
                self.skip(method, f"LA{la} ({name}) is in use by {owner}; free it first "
                                  "(gpio_release, uart_close, sensor_stop)")
                return True
        return False

    def attempt(self, method: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except (BenchPodError, ValueError) as exc:
            if _is_connection_problem(exc):
                raise
            self.skip(method, _reason(exc))

    # -- the probes --

    def swd(self) -> None:
        pins = swd_pins(self.env.wiring)
        if pins is None:
            return self.not_wired("swd", "the profile has no swd_swclk and swd_swdio")
        if self.owned("swd", pins):
            return
        if not self.voltage_set:
            return self.skip("swd", "the LA voltage is not set")
        if not self.env.supports("dap_id"):
            return self.skip("swd", DAP_ID_REASON)
        self.env.prepare("swd")
        reply = _dict(self.env.command(
            {"cmd": "dap_start", "op": "id", **pins, "wait_ms": SWD_ANSWER_WAIT_MS}))
        if not reply.get("found"):
            return self.skip("swd", f"nothing answered on SWD (SWCLK LA{pins['swclk']}, SWDIO LA{pins['swdio']}): "
                                    "check the wiring, the target's power and that it is not held in reset")
        addrs = candidate_addresses(core_from_cpuid(reply.get("cpuid"))) if reply.get("cpuid") else []
        if addrs:
            try:
                second = _dict(self.env.command({"cmd": "dap_start", "op": "id", **pins, "read": addrs}))
            except BenchPodError as exc:
                if _is_connection_problem(exc):
                    raise
                self.result.notes.append(f"swd: reading the ID registers failed ({_reason(exc)}); "
                                         "the debug port's answer is reported")
            else:
                if second.get("found"):
                    reply = second
        finding = finding_from_swd(reply, pins=pins)
        if finding is not None:
            self.result.findings.append(finding)

    def jtag(self) -> None:
        pins = jtag_pins(self.env.wiring)
        if pins is None:
            return self.not_wired("jtag", "the profile has no jtag_tck, jtag_tms, jtag_tdi and jtag_tdo")
        if self.owned("jtag", pins):
            return
        if not self.voltage_set:
            return self.skip("jtag", "the LA voltage is not set")
        if not self.env.supports("dap_id"):
            return self.skip("jtag", DAP_ID_REASON)
        if not self.env.supports("jtag"):
            return self.skip("jtag", "needs pod firmware with the jtag feature (update the pod)")
        self.env.prepare("jtag")
        reply = _dict(self.env.command(
            {"cmd": "dap_start", "op": "id", **pins, "wait_ms": SWD_ANSWER_WAIT_MS}))
        found = findings_from_jtag(reply, pins=pins)
        if not found:
            return self.skip("jtag", "no JTAG device answered (check TCK, TMS, TDI, TDO, the target's "
                                     "power and that it is not held in reset)")
        self.result.findings.extend(found)

    def prog(self, proto: str) -> None:
        got, reason, silent = prog_pins(self.env.wiring, proto)
        if got is None:
            if silent:
                return self.not_wired(proto, "the profile has no signal named for this protocol")
            return self.skip(proto, reason)
        if not self.invasive:
            return self.skip(proto, "invasive probe (it resets the chip): run detect with invasive=True")
        if self.owned(proto, got.pins):
            return
        if not self.voltage_set:
            return self.skip(proto, "the LA voltage is not set")
        if not self.env.supports("prog_flash"):
            return self.skip(proto, "needs pod firmware with prog_flash (update the pod)")
        if proto == "sbw" and not self.env.supports("prog_named"):
            # Spy-Bi-Wire shipped with firmware 4.0.6, the release that added prog_named
            return self.skip(proto, "needs pod firmware with the sbw protocol (4.0.6 or newer; update the pod)")
        self.env.prepare("prog")
        fam = _prog.families()[proto]
        pins, text = got.pins, got.text
        try:
            if proto == "pic":
                chip_id, dev = _prog.identify(self.env.command, proto, pins, pin_text=text,
                                              named=self.env.supports("prog_named"),
                                              sleep=self.env.sleep, clock=self.env.clock)
                matches = [dev]
            else:
                chip_id = _prog.read_info(self.env.command, proto, pins, pin_text=text,
                                          sleep=self.env.sleep, clock=self.env.clock)
                matches = [d for d in fam.devices if d.matches_id(chip_id)]
        except _prog.ProgDeviceError as exc:
            # a chip answered that the table does not have (or the answer is not one chip)
            self.result.findings.append(DetectFinding(
                method=proto, vendor=_VENDOR[proto], core=_ARCH[proto], confidence="medium",
                pins=dict(pins), raw={"error": _reason(exc), "result": exc.result}, invasive=True))
            return
        except _prog.ProgFlashError as exc:
            return self.skip(proto, _reason(exc))
        except DirectionConflictError as exc:
            return self.skip(proto, _reason(_prog.direction_error(exc, text)))
        raw = {"id": chip_id, "family": fam.title}
        if proto == "sbw" and len(str(chip_id).lower().replace("0x", "").strip()) == 2:
            # only the JTAG ID answered: the chip is JTAG-locked, its device ID cannot be read
            raw["locked"] = True
            self.result.findings.append(DetectFinding(
                method=proto, chip="MSP430 (JTAG-locked)", vendor=_VENDOR[proto], core=_ARCH[proto],
                id="0x" + chip_id.lower(), confidence="low", pins=dict(pins), raw=raw, invasive=True))
            self.result.notes.append("sbw: only the JTAG ID answered, so the chip is JTAG-locked; name it "
                                     "(device=) and flash with erase=\"chip\" to erase and unlock it")
            return
        names = sorted({d.name for d in matches})
        if not names:
            self.result.findings.append(DetectFinding(
                method=proto, vendor=_VENDOR[proto], core=_ARCH[proto], id="0x" + chip_id.lower(),
                confidence="medium", pins=dict(pins), raw=raw, invasive=True))
            return
        same = all(d.mem == matches[0].mem and d.opts == matches[0].opts for d in matches[1:])
        raw["candidates"] = names
        self.result.findings.append(DetectFinding(
            method=proto, chip=matches[0].name if len(names) == 1 else " / ".join(names),
            vendor=_VENDOR[proto], core=_ARCH[proto], id="0x" + chip_id.lower(),
            confidence="high" if len(names) == 1 or same else "medium",
            pins=dict(pins), raw=raw, invasive=True))

    def esp32(self) -> None:
        pins, reason, silent = esp_pins(self.env.wiring)
        if pins is None:
            if silent:
                return self.not_wired("esp32", "the profile has no BOOT, IO0 or IO9 signal")
            return self.skip("esp32", reason)
        if not self.invasive:
            return self.skip("esp32", "invasive probe (it resets the chip): run detect with invasive=True")
        if self.owned("esp32", pins):
            return
        if not self.voltage_set:
            return self.skip("esp32", "the LA voltage is not set")
        cmd = self.env.command
        cmd({"cmd": "esp_flash", "op": "sync", **pins, "baud": _esp.DEFAULT_BAUD})
        deadline = self.env.clock() + ESP_TIMEOUT
        st: Mapping[str, Any] = {}
        while True:
            self.env.sleep(ESP_POLL_INTERVAL)
            st = _dict(cmd({"cmd": "esp_flash", "op": "status"}))
            if not st.get("running"):
                break
            if self.env.clock() > deadline:
                cmd({"cmd": "esp_flash", "op": "abort"})
                return self.skip("esp32", f"the ROM loader did not answer in {ESP_TIMEOUT:g} s")
        chip = str(st.get("chip") or "")
        if not chip:
            return self.skip("esp32", _esp._status_error(st) or "no answer from the ROM loader")
        self.result.findings.append(DetectFinding(
            method="esp32", chip=chip, vendor="Espressif", pins=dict(pins), raw=dict(st),
            confidence="high", invasive=True))
        self.result.notes.append("esp32: the chip was reset into its ROM loader; reset or power-cycle "
                                 "it to run its program again")

    def usb(self) -> None:
        if self.env.usb_state is None or not self.env.supports("usb_host_mode"):
            return self.not_wired("usb", "needs pod firmware with usb_host_mode (update the pod)")
        st = self.env.usb_state()
        if not st.hosting:
            return self.not_wired("usb", f"USB host mode is {st.mode}: detect never starts it (usb_host)")
        if not st.mounted:
            return self.not_wired("usb", "USB host mode is on but no device is mounted")
        self.result.findings.append(finding_from_usb(st.device, port=st.port))


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _power_state(env: DetectEnv) -> Tuple[Optional[bool], str]:
    """``(on, words)``: whether the profile's rail (any rail without one) is enabled."""
    ts = env.target_status()
    rail = env.wiring.efuse
    if isinstance(rail, str):   # a J1 supply (digital board v4): on or off, no fault state
        j1 = getattr(ts, "j1_rails", None)
        if j1 is None:
            return None, f"the pod cannot read the state of {rail}"
        on = j1.v3v3 if rail == "j1_3v3" else j1.v1v8
        if on:
            return True, f"the {rail} supply on J1 is on"
        return False, f"target power is off ({rail} on J1): power the target (power_on) and detect again"
    states = [(rail, ts.efuse(rail))] if rail else [(1, ts.internal), (2, ts.external)]
    name = {1: "internal 5 V (efuse 1)", 2: "external (efuse 2)"}
    if not getattr(ts, "supported", True):
        return None, "the pod cannot read the target-power state"
    for n, st in states:
        if st.enabled and not st.fault:
            return True, f"the {name.get(int(n), 'efuse ' + str(n))} rail is on"
    fault = [name.get(int(n), str(n)) for n, st in states if st.fault]
    if fault:
        return False, f"the {' and '.join(fault)} rail has tripped: clear the fault and power the target"
    which = name.get(int(rail), "efuse " + str(rail)) if rail else "target"
    return False, f"target power is off ({which}): power the target (power_on) and detect again"


def run(env: DetectEnv, *, invasive: bool = True, methods: Optional[Sequence[str]] = None) -> DetectResult:
    """Detect the chip: see the module docstring. ``invasive=False`` leaves out the probes that
    reset the chip (everything but ``swd``, ``jtag`` and ``usb``). ``methods`` limits the probes
    (names from :data:`METHODS`); a named method that is not wired is reported in ``skipped``."""
    selected = normalize_methods(methods)
    r = _Run(env, invasive, selected, explicit=methods is not None)
    on, why = _power_state(env)
    if on is False:
        r.result.powered = False
        r.result.notes.append(why)
        return r.result
    if on is None:
        r.result.notes.append(why + "; probing anyway")
    mv = env.la_voltage()
    if not mv:
        r.voltage_set = False
        r.result.notes.append("the LA I/O voltage is not set: set it to the target's (set_la_voltage, "
                              "or la_mv in the wiring profile) to probe its pins")
    if env.pin_owners is not None:
        try:
            r.owners = env.pin_owners()
        except BenchPodError as exc:
            if _is_connection_problem(exc):
                raise
            r.result.notes.append(f"the owners of the LA pins could not be read ({_reason(exc)})")
    steps: Dict[str, Callable[[], None]] = {
        "swd": r.swd, "jtag": r.jtag, "esp32": r.esp32, "usb": r.usb,
        **{p: (lambda p=p: r.prog(p)) for p in ("updi", "pic", "c2", "tpi", "pdi", "sbw")}}
    for method in selected:
        r.attempt(method, steps[method])
    if r.unwired:
        r.result.notes.append("not tried (the profile does not wire them): " + ", ".join(r.unwired))
    return r.result
