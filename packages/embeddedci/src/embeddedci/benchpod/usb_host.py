"""USB host mode and USB flashing (the pod's ``usb_host`` and ``usb_flash`` commands).

The pod's USB port becomes a full-speed USB host for the board under test, reports what
enumerates, and can flash the board the way a computer would: a UF2 drive, a drag-and-drop drive
(DAPLink), DFU and STM32 DfuSe, the Teensy loader, the Arduino AVR and SAMD bootloaders and an
ESP32 ROM loader and the PICOBOOT interface of an RP2040 or RP2350. Firmware 4.0.3 and later
(features ``usb_host_mode`` and ``usb_flash``). The same host port also carries the board's HID
interfaces (:meth:`~embeddedci.benchpod.BenchPod.usb_hid_interfaces`, feature ``usb_hid``), the
checks of the board's own USB firmware (:meth:`~embeddedci.benchpod.BenchPod.usb_check`, feature
``usb_check``) and its serial port (``open_uart(port="usb")``, feature ``uart_usb``).

Use it through :meth:`BenchPod.usb_host <embeddedci.benchpod.BenchPod.usb_host>`,
:meth:`~embeddedci.benchpod.BenchPod.usb_host_status`,
:meth:`~embeddedci.benchpod.BenchPod.usb_wait_device` and
:meth:`~embeddedci.benchpod.BenchPod.usb_flash`; this module holds the typed results, the image
conversion (Intel HEX and DfuSe files to a binary with its start address), the choice of the
loader from the file and the mounted device, and the poll loop.

The loader table (:data:`MODES`) and the device rules (:func:`choose_mode`) are the one place a new
USB command extends: a new loader is a new entry.
"""

from __future__ import annotations

import os
import re
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from .errors import UsbFlashError, UsbHostError, UsbTimeout
from .state import _d, _opt_int, _raw

#: Progress callback of a flash: units done and total (sectors for ``uf2`` and ``file``, blocks
#: for ``dfu``, ``teensy`` and ``esp``, bytes for ``avr`` and ``samd``, steps for ``picoboot``).
Progress = Callable[[int, int], None]

POLL_INTERVAL = 0.25
#: ``usb_host`` timeout the SDK asks for when it starts host mode for a flash.
FLASH_HOST_TIMEOUT_S = 300
#: Renew host mode before a flash when less than this many seconds are left.
MIN_HOST_REMAINING_S = 120

#: USB class codes the loader choice looks at.
CLASS_CDC = 0x02
CLASS_MSC = 0x08
CLASS_DFU = 0xFE   # application specific; subclass 1 = DFU


def _hex(v: Optional[int], width: int = 4) -> str:
    return "????" if v is None else f"{v:0{width}x}"


def _parse_id(value: Union[int, str, None], what: str) -> Optional[int]:
    """A vendor or product ID as an int: ``0x16c0``, ``"16c0"`` (hex, like ``lsusb``) or ``5824``."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{what} must be an int or a hex string, not {value!r}")
    if isinstance(value, int):
        n = value
    else:
        text = str(value).strip().lower()
        try:
            n = int(text, 16) if not text.startswith("0x") else int(text, 0)
        except ValueError:
            raise ValueError(f"{what} must be a number or a hex string like '16c0', got {value!r}") from None
    if not 0 <= n <= 0xFFFF:
        raise ValueError(f"{what} must be 0 to 0xffff, got {value!r}")
    return n


# -- typed host state ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsbInterface:
    """One interface of the device's first configuration."""

    number: int
    alt: int
    #: The USB interface class code (``0x08`` mass storage, ``0x02`` CDC, ``0xFE`` DFU, ...).
    interface_class: int
    #: The pod's name for the class (``"msc"``, ``"cdc"``, ``"cdc_data"``, ``"hid"``, ...).
    class_name: str
    subclass: int
    protocol: int
    endpoints: int

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbInterface":
        d = _d(reply)
        return cls(number=_opt_int(d, "number") or 0, alt=_opt_int(d, "alt") or 0,
                   interface_class=_opt_int(d, "class") or 0, class_name=str(d.get("class_name") or ""),
                   subclass=_opt_int(d, "subclass") or 0, protocol=_opt_int(d, "protocol") or 0,
                   endpoints=_opt_int(d, "endpoints") or 0)


@dataclass(frozen=True)
class UsbConfig:
    """The first configuration of the device on the pod's host port."""

    value: int
    #: What the device declares it draws, in mA.
    max_power_ma: int
    self_powered: bool
    remote_wakeup: bool
    #: Interfaces counted with their alternate settings.
    interface_count: int
    #: The descriptor was longer than the pod reads.
    truncated: bool
    #: A descriptor in it had a bad length, so ``interfaces`` stops there.
    malformed: bool
    #: Up to 8 interfaces.
    interfaces: Tuple[UsbInterface, ...] = ()
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbConfig":
        d = _d(reply)
        ifs = d.get("interfaces")
        return cls(value=_opt_int(d, "value") or 0, max_power_ma=_opt_int(d, "max_power_ma") or 0,
                   self_powered=d.get("self_powered") is True, remote_wakeup=d.get("remote_wakeup") is True,
                   interface_count=_opt_int(d, "interface_count") or 0,
                   truncated=d.get("truncated") is True, malformed=d.get("malformed") is True,
                   interfaces=tuple(UsbInterface.from_reply(i) for i in ifs) if isinstance(ifs, list) else (),
                   raw=d)


@dataclass(frozen=True)
class UsbDevice:
    """The device that enumerated on the pod's host port."""

    vid: int
    pid: int
    #: ``"full"`` or ``"low"``.
    speed: str
    #: ``bcdUSB`` and ``bcdDevice`` as ``"major.minor"``.
    usb: str
    release: str
    #: ``bDeviceClass``, ``bDeviceSubClass``, ``bDeviceProtocol`` (0 when the interfaces say it).
    device_class: int
    subclass: int
    protocol: int
    ep0_size: int
    configurations: int
    #: String descriptors in ASCII, ``""`` when the device has none.
    manufacturer: str
    product: str
    serial: str
    #: Milliseconds from host start (or the previous detach) to the device descriptor and to the
    #: configured device.
    descriptor_ms: int
    mounted_ms: int
    config: Optional[UsbConfig] = None
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbDevice":
        d = _d(reply)
        cfg = d.get("config")
        return cls(vid=_opt_int(d, "vid") or 0, pid=_opt_int(d, "pid") or 0,
                   speed=str(d.get("speed") or ""), usb=str(d.get("usb") or ""),
                   release=str(d.get("release") or ""), device_class=_opt_int(d, "class") or 0,
                   subclass=_opt_int(d, "subclass") or 0, protocol=_opt_int(d, "protocol") or 0,
                   ep0_size=_opt_int(d, "ep0_size") or 0, configurations=_opt_int(d, "configurations") or 0,
                   manufacturer=str(d.get("manufacturer") or ""), product=str(d.get("product") or ""),
                   serial=str(d.get("serial") or ""), descriptor_ms=_opt_int(d, "descriptor_ms") or 0,
                   mounted_ms=_opt_int(d, "mounted_ms") or 0,
                   config=UsbConfig.from_reply(cfg) if isinstance(cfg, Mapping) else None, raw=d)

    @property
    def vid_pid(self) -> str:
        """``"16c0:0478"``, as ``lsusb`` writes it."""
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def interfaces(self) -> Tuple[UsbInterface, ...]:
        return self.config.interfaces if self.config else ()

    def has_interface(self, interface_class: int, subclass: Optional[int] = None) -> bool:
        """The device has an interface of this class (and subclass)."""
        return any(i.interface_class == interface_class and (subclass is None or i.subclass == subclass)
                   for i in self.interfaces)

    def __str__(self) -> str:
        name = " ".join(x for x in (self.manufacturer, self.product) if x)
        return f"{self.vid_pid}" + (f" {name}" if name else "")


@dataclass(frozen=True)
class UsbHostState:
    """The pod's USB host state (:meth:`BenchPod.usb_host_status
    <embeddedci.benchpod.BenchPod.usb_host_status>`)."""

    #: ``"device"`` (normal), ``"starting"``, ``"host"`` or ``"stopping"``.
    mode: str
    #: ``"usb_a"``, ``"usb_c"``, or ``"none"`` in device mode.
    port: str
    #: Seconds until the host timeout, and milliseconds in host mode so far.
    remaining_s: int
    host_ms: int
    #: Devices that enumerated since host mode started (a replug counts again).
    enumerations: int
    #: A device is enumerated now.
    attached: bool
    #: ``"none"``, ``"running"``, ``"done"`` or ``"stuck"`` (no enumeration 2 s after the device
    #: descriptor).
    enumeration: str
    #: A device's pull-up is on the port, before any enumeration. ``False`` while a board draws
    #: power means its USB lines do not reach the pod.
    connected: bool
    #: ``"full"``, ``"low"`` (D+ and D- swapped on a full-speed board) or ``"none"``.
    line_speed: str
    #: The device that enumerated last; kept after it detaches until the next one.
    device: Optional[UsbDevice] = None
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbHostState":
        d = _d(reply)
        dev = d.get("device")
        return cls(mode=str(d.get("mode") or "device"), port=str(d.get("port") or "none"),
                   remaining_s=_opt_int(d, "remaining_s") or 0, host_ms=_opt_int(d, "host_ms") or 0,
                   enumerations=_opt_int(d, "enumerations") or 0, attached=d.get("attached") is True,
                   enumeration=str(d.get("enumeration") or "none"), connected=d.get("connected") is True,
                   line_speed=str(d.get("line_speed") or "none"),
                   device=UsbDevice.from_reply(dev) if isinstance(dev, Mapping) else None, raw=d)

    @property
    def hosting(self) -> bool:
        """Host mode is on (``mode == "host"``)."""
        return self.mode == "host"

    @property
    def mounted(self) -> bool:
        """A device is attached and fully enumerated."""
        return self.attached and self.enumeration == "done" and self.device is not None


# -- waiting ------------------------------------------------------------------------------------

def _no_device_hint(st: UsbHostState, vid: Optional[int], pid: Optional[int]) -> str:
    if not st.hosting:
        return f"USB host mode is {st.mode}: start it with usb_host()"
    if st.enumeration == "stuck":
        return "the board's configuration descriptor stopped enumeration (stuck)"
    if st.mounted and st.device is not None:
        want = f"{_hex(vid)}:{_hex(pid)}"
        return f"{st.device.vid_pid} is mounted, not {want}"
    if st.enumeration == "running":
        return "a device sent its descriptor but did not finish enumerating"
    if not st.connected:
        return ("no device pull-up on the port: check the cable, that the board is powered "
                "and that D+/D- and GND reach the pod")
    if st.line_speed == "low":
        return "the board shows a low-speed line: D+ and D- are probably swapped"
    return "a device is connected but did not enumerate"


def wait_device(read_state: Callable[[], UsbHostState], *, vid: Optional[int] = None,
                pid: Optional[int] = None, timeout: float = 10.0,
                poll_interval: float = POLL_INTERVAL,
                sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic) -> UsbDevice:
    """Poll ``read_state`` until a device (matching ``vid`` / ``pid`` when given) is mounted and
    return it. Raises :class:`UsbHostError` when host mode is off, :class:`UsbTimeout` otherwise."""
    deadline = clock() + timeout
    st = read_state()
    while True:
        if st.mode in ("device", "stopping"):
            raise UsbHostError("USB host mode is off: start it with usb_host()")
        if st.mounted and st.device is not None \
                and (vid is None or st.device.vid == vid) and (pid is None or st.device.pid == pid):
            return st.device
        if clock() >= deadline:
            raise UsbTimeout(f"no USB device mounted within {timeout:g} s: {_no_device_hint(st, vid, pid)}")
        sleep(poll_interval)
        st = read_state()


# -- images -------------------------------------------------------------------------------------

#: Gaps up to this size inside a HEX or DfuSe file are filled with 0xFF to make one binary.
MAX_GAP_FILL = 64 * 1024
UF2_MAGIC = b"UF2\n"
DFUSE_MAGIC = b"DfuSe"
ESP_IMAGE_MAGIC = 0xE9
#: Teensy 4.x link its flash at this address; the loader's own address starts at 0.
TEENSY_FLASH_BASE = 0x60000000


def parse_intel_hex(text: str) -> List[Tuple[int, bytes]]:
    """The data records of an Intel HEX file as contiguous ``(address, bytes)`` segments, in order
    of address. Checks every record's checksum."""
    base = 0
    cells: Dict[int, int] = {}
    seen_eof = False
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {n}: not an Intel HEX record")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError:
            raise ValueError(f"line {n}: bad hex digits in an Intel HEX record") from None
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise ValueError(f"line {n}: bad record length")
        if sum(rec) & 0xFF:
            raise ValueError(f"line {n}: bad checksum")
        count, addr, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
        data = rec[4:4 + count]
        if typ == 0:
            for i, b in enumerate(data):
                cells[base + addr + i] = b
        elif typ == 1:
            seen_eof = True
            break
        elif typ == 2 and count == 2:
            base = int.from_bytes(data, "big") << 4
        elif typ == 4 and count == 2:
            base = int.from_bytes(data, "big") << 16
        # 03 and 05 (start addresses) say where to run, not what to write.
    if not seen_eof:
        raise ValueError("the Intel HEX file has no end-of-file record")
    if not cells:
        raise ValueError("the Intel HEX file holds no data")
    segments: List[Tuple[int, bytearray]] = []
    for a in sorted(cells):
        if segments and a == segments[-1][0] + len(segments[-1][1]):
            segments[-1][1].append(cells[a])
        else:
            segments.append((a, bytearray([cells[a]])))
    return [(a, bytes(b)) for a, b in segments]


def parse_dfuse(raw: bytes) -> List[Tuple[int, bytes]]:
    """The elements of a DfuSe file (``.dfu``) as ``(address, bytes)``, in order of address."""
    if len(raw) < 11 + 274 or raw[:5] != DFUSE_MAGIC:
        raise ValueError("not a DfuSe file")
    targets = raw[10]
    pos = 11
    elements: List[Tuple[int, bytes]] = []
    for _ in range(targets):
        if raw[pos:pos + 6] != b"Target":
            raise ValueError("bad DfuSe target header")
        n_elems = struct.unpack_from("<I", raw, pos + 270)[0]
        pos += 274
        for _ in range(n_elems):
            if pos + 8 > len(raw):
                raise ValueError("the DfuSe file is cut short")
            addr, size = struct.unpack_from("<II", raw, pos)
            pos += 8
            if pos + size > len(raw):
                raise ValueError("the DfuSe file is cut short")
            elements.append((addr, raw[pos:pos + size]))
            pos += size
    if not elements:
        raise ValueError("the DfuSe file holds no elements")
    return sorted(elements, key=lambda e: e[0])


def join_segments(segments: List[Tuple[int, bytes]], what: str) -> Tuple[int, bytes]:
    """One binary from ``segments`` (sorted): its start address, gaps up to :data:`MAX_GAP_FILL`
    filled with 0xFF. A larger gap raises :class:`ValueError`."""
    start = segments[0][0]
    out = bytearray(segments[0][1])
    for addr, data in segments[1:]:
        end = start + len(out)
        if addr < end:
            raise ValueError(f"{what}: the data at 0x{addr:x} overlaps the data before it")
        if addr - end > MAX_GAP_FILL:
            raise ValueError(f"{what}: a gap of {addr - end} bytes at 0x{end:x} is too large to fill "
                             f"for one binary; flash its parts separately")
        out.extend(b"\xff" * (addr - end))
        out.extend(data)
    return start, bytes(out)


def fat_name(path: str) -> str:
    """An 8.3 file name for the drive (``my-firmware.bin`` is ``MY-FIRMW.BIN``)."""
    base = os.path.basename(path)
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""

    def clean(s: str, n: int) -> str:
        return re.sub(r"[^A-Z0-9_!#$%&'()@^`{}~-]", "_", s.upper().replace(" ", ""))[:n]

    stem8, ext3 = clean(stem, 8), clean(ext, 3)
    if not stem8:
        stem8 = "FIRMWARE"
    return stem8 + ("." + ext3 if ext3 else "")


@dataclass
class UsbImage:
    """A file to flash: its bytes as given, what they are (:attr:`kind`) and, for the loaders that
    take a binary, the binary and its start address (:meth:`binary`)."""

    raw: bytes
    #: ``"uf2"``, ``"hex"``, ``"dfu"`` or ``"bin"``.
    kind: str
    #: The file's name (``""`` for bytes).
    path: str = ""

    @property
    def is_uf2(self) -> bool:
        return self.kind == "uf2"

    @property
    def looks_like_esp(self) -> bool:
        """A ``.bin`` that starts with the ESP image magic (0xE9): an ESP32 bootloader or a
        merged image. An application image starts with it too."""
        return self.kind == "bin" and bool(self.raw) and self.raw[0] == ESP_IMAGE_MAGIC

    def binary(self) -> Tuple[bytes, Optional[int]]:
        """The image as a binary and its start address (``None`` when the file does not say).
        ``.hex`` and ``.dfu`` files are converted; a ``.bin`` comes back as it is."""
        if self.kind == "uf2":
            raise ValueError("this loader needs a .bin, .hex or .dfu file, not a UF2 file")
        if self.kind == "hex":
            try:
                text = self.raw.decode("ascii")
            except UnicodeDecodeError:
                raise ValueError(f"{self.path or 'the file'} is not an Intel HEX file") from None
            start, data = join_segments(parse_intel_hex(text), self.path or "the HEX file")
            return data, start
        if self.kind == "dfu":
            if self.raw[:5] == DFUSE_MAGIC:
                start, data = join_segments(parse_dfuse(self.raw), self.path or "the DfuSe file")
                return data, start
            # A plain DFU file: the payload, then a 16-byte suffix ending in "UFD".
            if len(self.raw) > 16 and self.raw[-8:-5] == b"UFD":
                return self.raw[:-16], None
            return self.raw, None
        return self.raw, None


def load_image(file: Union[str, "os.PathLike[str]", bytes, bytearray]) -> UsbImage:
    """Read ``file`` (a path, or the bytes) and say what it is, from its content and its
    extension: ``.uf2``, ``.hex`` / ``.ihx``, ``.dfu`` or anything else a binary."""
    if isinstance(file, (bytes, bytearray)):
        raw, path = bytes(file), ""
    else:
        path = os.fspath(file)
        with open(path, "rb") as fh:
            raw = fh.read()
    if not raw:
        raise ValueError(f"{path or 'the image'} is empty")
    ext = os.path.splitext(path)[1].lower()
    if raw[:4] == UF2_MAGIC or ext == ".uf2":
        kind = "uf2"
    elif raw[:5] == DFUSE_MAGIC or ext == ".dfu":
        kind = "dfu"
    elif ext in (".hex", ".ihx") or (ext not in (".bin", ".img") and raw[:1] == b":"
                                     and re.fullmatch(rb":[0-9A-Fa-f]+\s*", raw.split(b"\n", 1)[0] + b"\n")):
        kind = "hex"
    else:
        kind = "bin"
    return UsbImage(raw=raw, kind=kind, path=path)


# -- loaders and the choice of one --------------------------------------------------------------

@dataclass(frozen=True)
class UsbMode:
    """One ``usb_flash`` loader: whether it takes a binary with an address (the other modes take
    the file as it is), and whether it can leave the board in its loader (``leave``)."""

    name: str
    binary: bool
    leave: bool
    #: Counted in this unit by ``done`` / ``total``.
    unit: str


#: The loaders firmware 4.0.3 has. A loader added later is one more entry here and one more rule
#: in :func:`choose_mode`.
MODES: Dict[str, UsbMode] = {m.name: m for m in (
    UsbMode("uf2", binary=False, leave=False, unit="sectors"),
    UsbMode("file", binary=False, leave=False, unit="sectors"),
    UsbMode("dfu", binary=True, leave=True, unit="blocks"),
    UsbMode("teensy", binary=True, leave=True, unit="blocks"),
    UsbMode("avr", binary=True, leave=True, unit="bytes"),
    UsbMode("samd", binary=True, leave=True, unit="bytes"),
    UsbMode("esp", binary=True, leave=True, unit="blocks"),
    UsbMode("picoboot", binary=True, leave=True, unit="steps"),
)}
#: The loader a feature gates (the others are in ``usb_flash`` itself).
MODE_FEATURES: Dict[str, str] = {"picoboot": "usb_flash_picoboot"}

VID_TEENSY, PID_TEENSY = 0x16C0, 0x0478
VID_ST = 0x0483
#: DfuSe loaders that put flash at 0x08000000: ST, GigaDevice, Artery.
DFUSE_VIDS = {0x0483, 0x28E9, 0x2E3C}
STM32_FLASH = 0x08000000
VID_ESPRESSIF = 0x303A
#: A Raspberry Pi RP2040 (0003) or RP2350 (000F) in BOOTSEL: a UF2 drive plus the PICOBOOT interface.
VID_RPI = 0x2E8A
PICOBOOT_PIDS = {0x0003, 0x000F}
PICO_FLASH = 0x10000000
VID_ARDUINO, VID_ARDUINO_ORG, VID_SPARKFUN, VID_ADAFRUIT, VID_SEEED = 0x2341, 0x2A03, 0x1B4F, 0x239A, 0x2886
#: USB-serial chips: CP210x, CH34x, FTDI. An ESP32 dev kit or an Arduino clone sits behind one.
SERIAL_CHIPS = {0x10C4: "CP210x", 0x1A86: "CH34x", 0x0403: "FTDI"}
#: Arduino SAMD boards (Zero, MKR family, Nano 33 IoT): the low byte of the product ID, with the
#: sketch (0x80, 0x82) or bootloader (0x00, 0x02) in the high byte.
_ARDUINO_SAMD_LOW = {0x4D, 0x4E, 0x4F, 0x50, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x59}
_ARDUINO_PID_HIGH = {0x00, 0x02, 0x80, 0x82}
#: Adafruit 32u4 boards (Feather, ItsyBitsy) run the Caterina bootloader; its other boards that
#: show up as a serial port are SAMD.
_ADAFRUIT_AVR = {0x000C, 0x800C, 0x000E, 0x800E}
SAMD21_START, SAMD51_START = 0x2000, 0x4000
#: SAMD51 boards, whose bootloader is 16 KB (best effort: pass address= for any other).
_SAMD51 = {(VID_ADAFRUIT, p) for p in (0x0021, 0x8021, 0x0022, 0x8022, 0x002B, 0x802B,
                                       0x0031, 0x8031, 0x0035, 0x8035)} | \
          {(VID_SEEED, 0x002D), (VID_SEEED, 0x802D)}


def _arduino_samd(vid: int, pid: int) -> bool:
    if vid == VID_ARDUINO:
        return (pid & 0xFF) in _ARDUINO_SAMD_LOW and (pid >> 8) in _ARDUINO_PID_HIGH
    if vid == VID_ADAFRUIT:
        return pid not in _ADAFRUIT_AVR
    return vid == VID_SEEED


def _arduino_avr(vid: int, pid: int) -> bool:
    if vid in (VID_ARDUINO, VID_ARDUINO_ORG, VID_SPARKFUN):
        return not _arduino_samd(vid, pid)
    return vid == VID_ADAFRUIT and pid in _ADAFRUIT_AVR


@dataclass(frozen=True)
class Plan:
    """What :func:`choose_mode` decided: the loader, the address for it (``None`` to send none)
    and why, in words."""

    mode: str
    address: Optional[int]
    reason: str


def choose_mode(image: UsbImage, device: UsbDevice, *, address: Optional[int] = None,
                mode: Optional[str] = None) -> Plan:
    """Pick the ``usb_flash`` loader for ``image`` on ``device``, and the address for it.

    Rules, first match wins: a Teensy loader (16C0:0478) is ``teensy``; a DFU interface is ``dfu``;
    a Pico in BOOTSEL (2E8A:0003 or 000F) is ``uf2`` for a UF2 file, else ``picoboot``;
    a mass-storage drive is ``uf2`` for a UF2 file, ``esp`` for an ESP image on an Espressif
    device, else ``file``; an Espressif device, or a USB-serial chip with an ESP image (starts with
    0xE9), is ``esp``; an Arduino AVR board or a USB-serial chip is ``avr``; a SAMD board is
    ``samd``. ``mode`` overrides the choice (the address rules still apply); ``address`` overrides
    the one from the file. Raises :class:`~embeddedci.benchpod.errors.UsbFlashError` when no rule
    fits."""
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        chosen, why = mode, f"mode {mode} was requested"
    else:
        chosen, why = _rule(image, device)
    spec = MODES[chosen]
    if not spec.binary:
        if chosen == "uf2" and not image.is_uf2:
            raise UsbFlashError(f"{chosen} mode needs a .uf2 file, got a .{image.kind} file", mode=chosen)
        return Plan(chosen, None, why)
    if image.is_uf2:
        raise UsbFlashError(
            f"{device} needs a binary (.bin, .hex or .dfu), not a UF2 file: only a board with a UF2 "
            f"drive takes UF2", mode=chosen)
    _data, file_address = image.binary()
    addr, how = _address(chosen, device, file_address, address)
    return Plan(chosen, addr, f"{why}; {how}" if how else why)


def _rule(image: UsbImage, dev: UsbDevice) -> Tuple[str, str]:
    vid, pid = dev.vid, dev.pid
    name = str(dev)
    if (vid, pid) == (VID_TEENSY, PID_TEENSY):
        return "teensy", f"{name} is the Teensy loader"
    if dev.has_interface(CLASS_DFU, 1):
        return "dfu", f"{name} has a DFU interface"
    if vid == VID_RPI and pid in PICOBOOT_PIDS and not image.is_uf2:
        return "picoboot", f"{name} is a Pico in BOOTSEL and the file is not UF2: written through PICOBOOT"
    if dev.has_interface(CLASS_MSC):
        if image.is_uf2:
            return "uf2", f"{name} shows a drive and the file is UF2"
        if vid == VID_ESPRESSIF and image.looks_like_esp:
            return "esp", f"{name} is an Espressif chip and the file is an ESP image"
        return "file", f"{name} shows a drive, so the file is copied onto it"
    if image.is_uf2:
        raise UsbFlashError(
            f"{name} shows no UF2 drive (is it in its bootloader?) and a UF2 file only goes to a drive: "
            f"press the board's boot button, or pass a .bin or .hex")
    if vid == VID_ESPRESSIF:
        return "esp", f"{name} is an Espressif chip"
    if vid in SERIAL_CHIPS and image.looks_like_esp:
        return "esp", f"{name} is a {SERIAL_CHIPS[vid]} USB-serial chip and the file is an ESP image (starts with 0xE9)"
    if _arduino_avr(vid, pid):
        return "avr", f"{name} is an Arduino AVR board"
    if _arduino_samd(vid, pid):
        return "samd", f"{name} is a SAMD board"
    if vid in SERIAL_CHIPS:
        return "avr", f"{name} is a {SERIAL_CHIPS[vid]} USB-serial chip and the file is not an ESP image: an Arduino AVR board"
    if image.looks_like_esp and dev.has_interface(CLASS_CDC):
        return "esp", f"{name} is a serial device and the file is an ESP image"
    raise UsbFlashError(
        f"cannot tell how to flash {name}: it is not a Teensy, has no DFU interface or drive and is "
        f"not a known Arduino, SAMD, ESP or USB-serial board. Pass mode=... (one of "
        f"{', '.join(MODES)})")


def _address(mode: str, dev: UsbDevice, file_address: Optional[int],
             given: Optional[int]) -> Tuple[Optional[int], str]:
    """The ``address`` to send for ``mode`` and a note on where it came from."""
    if given is not None:
        return given, f"address 0x{given:x} as given"
    addr = file_address
    if mode == "teensy":
        if addr is None:
            return 0, "address 0 (the whole image)"
        if addr >= TEENSY_FLASH_BASE:
            addr -= TEENSY_FLASH_BASE
        return addr, f"address 0x{addr:x} from the file"
    if addr is not None:
        return addr, f"address 0x{addr:x} from the file"
    if mode == "dfu":
        if dev.vid in DFUSE_VIDS:
            return STM32_FLASH, f"address 0x{STM32_FLASH:x} (the start of the STM32 flash)"
        return None, "no address (a plain DFU loader takes none)"
    if mode == "samd":
        start = SAMD51_START if (dev.vid, dev.pid) in _SAMD51 else SAMD21_START
        return start, f"address 0x{start:x} (where a sketch starts on this SAMD board; pass address= to change)"
    if mode in ("avr", "esp"):
        return 0, "address 0"
    if mode == "picoboot":
        return PICO_FLASH, f"address 0x{PICO_FLASH:x} (the start of the Pico's flash)"
    return None, ""


# -- the flash ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class UsbFlashState:
    """The pod's ``usb_flash`` state (:meth:`BenchPod.usb_flash_status
    <embeddedci.benchpod.BenchPod.usb_flash_status>`): the running flash, or the last one's
    outcome."""

    #: A flash is in progress (captures, DAC playback, uploads and updates are refused meanwhile).
    running: bool
    #: There is an outcome to read (:attr:`result`).
    have_result: bool
    mode: str
    #: The drive file name (``file`` mode).
    name: str
    length: int
    address: int
    #: ``avr``: the bootloader found (``avr109``, ``stk500v1``, ``stk500v2``); ``samd``:
    #: ``sam-ba``; ``esp``: the chip (``ESP32-S3``); empty otherwise.
    protocol: str
    #: The step the flash is in (``erase``, ``write``, ``verify``, ``done``, ...).
    phase: str
    #: Sectors, blocks or bytes done and in all (:data:`MODES` names the unit).
    done: int
    total: int
    #: ``"ok"``, ``"aborted"`` or what went wrong, once it ended.
    result: str
    ok: bool
    ms: int
    #: What the read-back found: ``FAIL.TXT``'s first line, ``verified N blocks``, ``verify
    #: skipped: ...``; empty when there is nothing to say.
    note: str = ""
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbFlashState":
        d = _d(reply)
        return cls(running=d.get("running") is True, have_result=d.get("have_result") is True,
                   mode=str(d.get("mode") or ""), name=str(d.get("name") or ""),
                   length=_opt_int(d, "len") or 0, address=_opt_int(d, "address") or 0,
                   protocol=str(d.get("protocol") or ""), phase=str(d.get("phase") or ""),
                   done=_opt_int(d, "done") or 0, total=_opt_int(d, "total") or 0,
                   result=str(d.get("result") or ""), ok=d.get("ok") is True,
                   ms=_opt_int(d, "ms") or 0, note=str(d.get("note") or ""), raw=d)

    @property
    def unit(self) -> str:
        m = MODES.get(self.mode)
        return m.unit if m else ""


@dataclass(frozen=True)
class UsbFlashResult:
    """A successful :meth:`BenchPod.usb_flash <embeddedci.benchpod.BenchPod.usb_flash>` (a failed
    one raises :class:`~embeddedci.benchpod.errors.UsbFlashError`)."""

    ok: bool
    #: The loader used, and why it was chosen, in words.
    mode: str
    reason: str
    #: What was written: bytes, the start address (``None`` when the loader takes none) and the
    #: drive file name (``file`` mode).
    length: int
    address: Optional[int]
    name: str
    #: ``avr``, ``samd`` or ``esp``: the bootloader or chip found.
    protocol: str
    #: Milliseconds the pod took.
    ms: int
    device: Optional[UsbDevice] = None
    state: Optional[UsbFlashState] = None
    #: What the read-back found (``verified N blocks``, ``verify skipped: ...``); empty without one.
    note: str = ""

    def __str__(self) -> str:
        proto = f" ({self.protocol})" if self.protocol else ""
        note = f" [{self.note}]" if self.note else ""
        return (f"flashed {self.length} bytes with the {self.mode} loader{proto} in "
                f"{self.ms / 1000:.1f} s: {self.reason}{note}")


def flash_timeout(length: int) -> float:
    """How long a flash of ``length`` bytes may take before the SDK aborts it: a minute plus the
    slowest loader's rate (an AVR bootloader writes, then reads back, at about 5 KB/s)."""
    return 60.0 + length / 2500.0


def run_flash(
    command: Callable[[Dict[str, Any]], Any],
    stage: Callable[[bytes], Any],
    data: bytes,
    request: Mapping[str, Any],
    *,
    timeout: float,
    progress: Optional[Progress] = None,
    poll_interval: float = POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> UsbFlashState:
    """Stage ``data`` in the pod's PSRAM, start ``request`` (a ``usb_flash`` command) and poll its
    state until it ends. ``command`` sends one JSON command and returns its ``data``. Returns the
    final state of a flash that ended ``ok``; raises :class:`UsbFlashError` (with the pod's
    ``result``) for any other end, and aborts the flash on a timeout or an interrupt."""
    stage(data)
    command(dict(request))
    mode = str(request.get("mode") or "")
    deadline = clock() + timeout
    st = UsbFlashState.from_reply(None)
    try:
        while True:
            sleep(poll_interval)
            st = UsbFlashState.from_reply(command({"cmd": "usb_flash"}))
            if progress and st.total:
                progress(min(st.done, st.total), st.total)
            if not st.running:
                break
            if clock() > deadline:
                command({"cmd": "usb_flash", "abort": True})
                raise UsbFlashError(
                    f"the {mode} flash did not finish within {timeout:g} s (stopped in phase "
                    f"{st.phase or '?'}) and was aborted", result="timeout", phase=st.phase,
                    mode=mode, state=st)
    except KeyboardInterrupt:
        try:
            command({"cmd": "usb_flash", "abort": True})
        except Exception:   # the interrupt is what the caller needs to see
            pass
        raise
    if st.result == "ok" or (st.ok and not st.result):
        return st
    where = f" in phase {st.phase}" if st.phase else ""
    proto = f" ({st.protocol})" if st.protocol else ""
    note = f" ({st.note})" if st.note else ""
    raise UsbFlashError(f"the {mode} flash{proto} failed{where}: {st.result or 'no result'}{note}",
                        result=st.result, phase=st.phase, mode=mode, state=st)


# -- HID interfaces and input reports (usb_hid) -------------------------------------------------

#: The most reports one ``usb_hid`` read returns, and how long it may wait (the pod's limits).
HID_MAX_COUNT = 32
HID_MAX_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class UsbHidInterface:
    """One HID interface of the board on the host port (:meth:`BenchPod.usb_hid_interfaces
    <embeddedci.benchpod.BenchPod.usb_hid_interfaces>`)."""

    #: The number to pass back (``instance=``): 0 or 1, the first two HID interfaces.
    instance: int
    #: The USB interface number.
    interface: int
    #: ``"keyboard"``, ``"mouse"`` or ``"none"``.
    protocol: str
    #: Bytes the report descriptor has (0 when the pod could not read it).
    descriptor_len: int
    #: The whole report descriptor (the SDK reads a long one in pages).
    descriptor: bytes
    #: The descriptor is still cut (only when a page could not be read).
    truncated: bool = False
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, d: Mapping[str, Any]) -> "UsbHidInterface":
        return cls(instance=_opt_int(d, "instance") or 0, interface=_opt_int(d, "interface") or 0,
                   protocol=str(d.get("protocol") or "none"), descriptor_len=_opt_int(d, "descriptor_len") or 0,
                   descriptor=_hex_bytes(d.get("descriptor")), truncated=d.get("truncated") is True,
                   raw=dict(d))


def _hex_bytes(value: Any) -> bytes:
    try:
        return bytes.fromhex(str(value or ""))
    except ValueError:
        return b""


@dataclass(frozen=True)
class UsbHidReport:
    """One input report: when it arrived, from which interface, and its bytes (with several report
    IDs the first byte is the ID)."""

    #: Milliseconds since the capture started.
    ms: int
    instance: int
    data: bytes

    @classmethod
    def from_reply(cls, d: Mapping[str, Any]) -> "UsbHidReport":
        return cls(ms=_opt_int(d, "ms") or 0, instance=_opt_int(d, "instance") or 0,
                   data=_hex_bytes(d.get("data")))


@dataclass(frozen=True)
class UsbHidState:
    """The board's HID interfaces (:meth:`BenchPod.usb_hid_interfaces
    <embeddedci.benchpod.BenchPod.usb_hid_interfaces>`)."""

    attached: bool
    interfaces: Tuple[UsbHidInterface, ...] = ()
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbHidState":
        d = _d(reply)
        items = d.get("interfaces")
        return cls(attached=d.get("attached") is True,
                   interfaces=tuple(UsbHidInterface.from_reply(i) for i in items
                                    if isinstance(i, Mapping)) if isinstance(items, list) else (),
                   raw=d)


@dataclass(frozen=True)
class UsbHidCapture:
    """The state of the input report capture (:meth:`BenchPod.usb_hid_capture
    <embeddedci.benchpod.BenchPod.usb_hid_capture>`)."""

    capturing: bool
    #: Reports waiting in the pod's ring of 64.
    buffered: int
    #: Reports that arrived while the ring was full, since the capture started.
    dropped: int

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbHidCapture":
        d = _d(reply)
        return cls(capturing=d.get("capturing") is True, buffered=_opt_int(d, "buffered") or 0,
                   dropped=_opt_int(d, "dropped") or 0)


@dataclass(frozen=True)
class UsbHidReports:
    """What :meth:`BenchPod.usb_hid_read <embeddedci.benchpod.BenchPod.usb_hid_read>` returned."""

    reports: Tuple[UsbHidReport, ...]
    #: Reports lost to a full ring since the capture started.
    dropped: int
    #: ``False`` when the capture had ended (the board detached or host mode ended).
    capturing: bool

    def __len__(self) -> int:
        return len(self.reports)

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbHidReports":
        d = _d(reply)
        items = d.get("reports")
        return cls(reports=tuple(UsbHidReport.from_reply(i) for i in items
                                 if isinstance(i, Mapping)) if isinstance(items, list) else (),
                   dropped=_opt_int(d, "dropped") or 0, capturing=d.get("capturing") is True)


# -- checks of the board's USB firmware (usb_check) ---------------------------------------------

#: The checks of ``usb_check`` and what each measures.
CHECKS: Dict[str, str] = {
    "enumerate": "power-on to pull-up, descriptor and mounted over count power cycles",
    "power": "eFuse 1's current against the configuration's bMaxPower",
    "suspend": "the suspended current against 2.5 mA, resume, and the board still answers",
    "reset": "count port resets and the time to enumerate again",
    "replug": "VBUS dropouts of several lengths: does the board come back",
}
CHECK_POLL_INTERVAL = 0.5


@dataclass(frozen=True)
class UsbCheckState:
    """The pod's ``usb_check`` state: the running check, or the last one's outcome."""

    running: bool
    have_result: bool
    test: str
    #: The step (``power_off``, ``power_on``, ``gap``, ``reset``, ``sample``, ``suspend``,
    #: ``settle``, ``measure``, ``resume``, ``check``, ``recover``, ``done``).
    phase: str
    #: Cycles done and in all (milliseconds for ``power`` and ``suspend``).
    done: int
    total: int
    ms: int
    #: The verdict once it ended; ``None`` while it runs or before the first check.
    passed: Optional[bool]
    #: Why the check stopped early (``aborted``, ``host_stopped``, ``not_powered_by_efuse1``,
    #: ``no_device``, ``monitor_error``, ``efuse_error``, ``bus_busy``, ``bus_timeout``); empty when it
    #: ran to its end.
    error: str
    #: The numbers of the test, as the pod reports them (docs/API.md, ``usb_check``), with the
    #: board's ``vid`` and ``pid``. Currents are in uA, times in ms.
    result: Dict[str, Any]
    raw: Dict[str, Any] = _raw()

    @classmethod
    def from_reply(cls, reply: Any) -> "UsbCheckState":
        d = _d(reply)
        res = d.get("result")
        return cls(running=d.get("running") is True, have_result=d.get("have_result") is True,
                   test=str(d.get("test") or ""), phase=str(d.get("phase") or ""),
                   done=_opt_int(d, "done") or 0, total=_opt_int(d, "total") or 0,
                   ms=_opt_int(d, "ms") or 0,
                   passed=d["pass"] if isinstance(d.get("pass"), bool) else None,
                   error=str(d.get("error") or ""), result=dict(res) if isinstance(res, Mapping) else {},
                   raw=d)

    @property
    def ended(self) -> bool:
        return not self.running and self.have_result


def check_timeout(test: str, args: Mapping[str, Any]) -> float:
    """How long a check may take before the SDK aborts it: its own worst case (every cycle at its
    timeout) plus slack for the recovery at the end."""
    def num(key: str, default: float) -> float:
        v = args.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default

    count, wait = num("count", 20.0 if test == "reset" else 10.0), num("timeout_ms", 5000.0)
    if test == "enumerate":
        ms = count * (num("off_ms", 500.0) + wait)
    elif test == "power":
        ms = num("duration_ms", 3000.0)
    elif test == "suspend":
        ms = num("settle_ms", 200.0) + num("duration_ms", 1000.0) + 20000.0
    elif test == "reset":
        ms = count * wait
    else:
        off = args.get("off_ms")
        lengths = [float(x) for x in off] if isinstance(off, (list, tuple)) and off else \
            [float(off)] if isinstance(off, (int, float)) and not isinstance(off, bool) else [5.0, 20.0, 100.0, 500.0]
        ms = count * (sum(lengths) + wait * len(lengths))
    return 20.0 + ms / 1000.0


def run_check(
    command: Callable[[Dict[str, Any]], Any],
    request: Mapping[str, Any],
    *,
    timeout: float,
    progress: Optional[Progress] = None,
    poll_interval: float = CHECK_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> UsbCheckState:
    """Start ``request`` (a ``usb_check`` command) and poll until the check ends; returns its final
    state (whatever the verdict). Aborts the check on a timeout (:class:`UsbHostError`) or an
    interrupt."""
    command(dict(request))
    test = str(request.get("test") or "")
    deadline = clock() + timeout
    try:
        while True:
            sleep(poll_interval)
            st = UsbCheckState.from_reply(command({"cmd": "usb_check"}))
            if progress and st.total:
                progress(min(st.done, st.total), st.total)
            if st.ended:
                return st
            if clock() > deadline:
                command({"cmd": "usb_check", "abort": True})
                raise UsbHostError(f"the {test} check did not finish within {timeout:g} s (stopped in "
                                   f"phase {st.phase or '?'}) and was aborted")
    except KeyboardInterrupt:
        try:
            command({"cmd": "usb_check", "abort": True})
        except Exception:   # the interrupt is what the caller needs to see
            pass
        raise


__all__: List[str] = ["UsbCheckState", "UsbConfig", "UsbDevice", "UsbFlashResult", "UsbFlashState",
                      "UsbHidCapture", "UsbHidInterface", "UsbHidReport", "UsbHidReports",
                      "UsbHidState", "UsbHostState", "UsbInterface"]
