"""User-defined sensors on the pod: :class:`SensorEmulation` (``bp.sensors``) and :class:`Sensor`.

The pod plays I2C, SPI and UART sensors on LA channels and analog sensors on its analog outputs,
from a **definition** the host sends (the sensor library's format, ``docs/format.md``). The pod
keeps nothing: definitions are gone after ``sensor_stop``, a reboot or a firmware update. Up to
4 I2C sensors, 1 SPI, 1 UART and 2 analog sensors (one per analog output) run at once, each an
**instance** with a name the host picks (``[a-z0-9_]{1,16}``, the definition's ``name`` by
default; a longer name needs an explicit ``instance``).

Wire protocol: ``sensor_define`` and ``sensor_stimulus`` (base64 chunks, each command small
enough for the cloud), ``sensor_start``, ``sensor_set``, ``sensor_stimulus_start``/``_stop``,
``sensor_fault``/``sensor_fault_clear``, ``sensor_stop``, ``sensor_status``, ``sensor_regs`` and
``sensor_la``. Firmware without the ``sensors`` capability raises
:class:`~embeddedci.benchpod.errors.UnsupportedFeatureError` before anything is sent.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import math
import os
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Union

from ..errors import BenchPodError, UnsupportedFeatureError
from .definition import (
    DefinitionSource,
    check_definition,
    check_stimulus,
    load_document,
    to_json_bytes,
)
from .library import SensorLibrary, sensor_library

if TYPE_CHECKING:  # pragma: no cover
    from ..client import BenchPod
    from ..i2c import I2CTransaction

#: Largest JSON command a pod takes over the cloud, in bytes.
MAX_COMMAND_BYTES = 1280
#: Bytes kept free for the transport's envelope around a command.
COMMAND_RESERVE_BYTES = 128
#: Raw bytes per ``sensor_define``/``sensor_stimulus`` chunk at most (1024 base64 characters: a
#: command of at most 1152 bytes, the size the firmware documents).
MAX_CHUNK_BYTES = 768
#: Instance names the pod takes (the definition ``name`` allows 32 characters, an instance 16).
INSTANCE_RE = re.compile(r"^[a-z0-9_]{1,16}$")
#: Packed bytes one ``sensor_la`` capture takes at most (4 samples per byte).
MAX_LA_BYTES = 4096
#: The message a pod without the ``sensors`` capability gives.
TOO_OLD = "this pod's firmware is too old for sensor emulation; update the firmware"

#: Pin roles per kind, with the wiring-profile role an omitted one comes from (None: required
#: or optional without a default).
PIN_ROLES: Dict[str, Dict[str, Optional[str]]] = {
    "i2c": {"sda": "i2c_sda", "scl": "i2c_scl", "drdy": None},
    "spi": {"sclk": "spi_sclk", "mosi": "spi_mosi", "miso": "spi_miso", "cs": "spi_cs"},
    "uart": {"tx": None, "rx": None},
    "analog": {"output": None},
}
_REQUIRED_PINS = {"i2c": ("sda", "scl"), "spi": ("sclk", "mosi", "miso", "cs"),
                  "uart": ("tx",), "analog": ("output",)}
#: Analog outputs an analog sensor drives.
ANALOG_OUTPUTS = ("dac", "current")

PinValue = Union[int, str]


def _number(key: str, value: Any) -> Union[int, float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{key} must be finite, got {value!r}")
    return value


def _values(values: Optional[Mapping[str, Any]], extra: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in {**(values or {}), **extra}.items():
        if value is not None:
            out[str(key)] = _number(str(key), value)
    return out


def _instance_name(instance: Any) -> str:
    name = str(instance).strip()
    if not INSTANCE_RE.match(name):
        raise ValueError("an instance name is 1 to 16 characters of a-z, 0-9 and _, got "
                         f"{instance!r}")
    return name


def _default_instance(instance: Optional[str], doc: Mapping[str, Any]) -> str:
    """``instance``, else the definition's ``name`` when it fits an instance name."""
    if instance is not None:
        return _instance_name(instance)
    name = str(doc.get("name", ""))
    if not INSTANCE_RE.match(name):
        raise ValueError(f"the definition's name {name!r} is longer than an instance name (at "
                         "most 16 characters): pass instance=")
    return name


def _address(address: Union[int, str, None]) -> Optional[str]:
    if address is None:
        return None
    a = int(address, 0) if isinstance(address, str) else int(address)
    if not 0 <= a <= 0x3FF:
        raise ValueError(f"an I2C address is 0x00..0x3FF, got {address!r}")
    return f"0x{a:02X}"


def chunk_size(base: Mapping[str, Any], limit: int = MAX_COMMAND_BYTES - COMMAND_RESERVE_BYTES) -> int:
    """The most raw bytes per chunk (a multiple of 3, so only the last chunk is padded) for which
    ``base`` plus ``"data_b64"`` stays within ``limit`` bytes of JSON."""
    overhead = len(json.dumps({**base, "data_b64": ""}, separators=(",", ":")))
    room = limit - overhead
    if room < 4:
        raise ValueError("the command leaves no room for data")
    return min((room // 4) * 3, MAX_CHUNK_BYTES)


def upload_requests(cmd: str, data: bytes, *, instance: Optional[str] = None,
                    dry_run: bool = False) -> List[Dict[str, Any]]:
    """The ``sensor_define``/``sensor_stimulus`` commands that carry ``data``, in order."""
    total = len(data)
    base: Dict[str, Any] = {"cmd": cmd}
    if instance is not None:
        base["instance"] = instance
    # The largest offset and total, so every chunk fits whatever its numbers.
    probe = {**base, "offset": total, "total": total}
    if dry_run:
        probe["dry_run"] = True
    size = chunk_size(probe)
    reqs: List[Dict[str, Any]] = []
    offset = 0
    while True:
        piece = data[offset:offset + size]
        req = {**base, "offset": offset, "total": total,
               "data_b64": base64.b64encode(piece).decode("ascii")}
        offset += len(piece)
        last = offset >= total
        if last and dry_run:
            req["dry_run"] = True
        reqs.append(req)
        if last:
            return reqs


def decode_regs(reply: Mapping[str, Any]) -> List[int]:
    """The bytes of a ``sensor_regs`` reply (``{"instance","start","len","data_b64"}``)."""
    data = reply.get("data_b64") if isinstance(reply, Mapping) else None
    if not isinstance(data, str):
        raise BenchPodError(f"sensor_regs reply has no data_b64: {dict(reply or {})!r}")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BenchPodError(f"sensor_regs reply: data_b64 is not base64 ({exc})") from exc
    return list(raw)


class Sensor:
    """One running sensor instance on the pod (from :meth:`SensorEmulation.start`).

    ``info`` is the pod's compile report (``kind``, ``name``, ``label``, ``params``, ``i2c``...),
    ``started`` its ``sensor_start`` reply (``slot``, ``image``, ``image_loaded``,
    ``unavailable``).
    """

    def __init__(self, emulation: "SensorEmulation", instance: str, *,
                 info: Optional[Mapping[str, Any]] = None,
                 started: Optional[Mapping[str, Any]] = None) -> None:
        self._emulation = emulation
        self.instance = instance
        self.info: Dict[str, Any] = dict(info or {})
        self.started: Dict[str, Any] = dict(started or {})

    def __repr__(self) -> str:
        return f"Sensor(instance={self.instance!r}, kind={self.kind!r}, name={self.name!r})"

    @property
    def kind(self) -> str:
        """``i2c``, ``spi``, ``uart`` or ``analog``."""
        return str(self.info.get("kind", ""))

    @property
    def name(self) -> str:
        """The definition's ``name``."""
        return str(self.info.get("name", ""))

    @property
    def params(self) -> List[Dict[str, Any]]:
        """The values :meth:`set` takes: ``key``, ``unit``, ``min``, ``max``, ``default``."""
        return list(self.info.get("params") or [])

    def set(self, values: Optional[Mapping[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
        """Change readings: ``sensor.set(temperature_c=31.5)``."""
        return self._emulation.set(self.instance, values, **kw)

    def fault(self, type: str, *, duration_s: Optional[float] = None, **fields: Any) -> Dict[str, Any]:
        """Inject a fault now (see :meth:`SensorEmulation.fault`)."""
        return self._emulation.fault(self.instance, type, duration_s=duration_s, **fields)

    def clear_fault(self) -> Dict[str, Any]:
        """End this instance's faults."""
        return self._emulation.clear_fault(self.instance)

    def status(self) -> Dict[str, Any]:
        """This instance's entry of :meth:`SensorEmulation.status` (``values``, ``faults``,
        ``bus`` counters), ``{}`` when it no longer runs."""
        try:
            st = self._emulation.status(self.instance)
        except BenchPodError as exc:
            if "unknown sensor instance" in str(exc):
                return {}
            raise
        for entry in st.get("instances") or []:
            if isinstance(entry, dict) and entry.get("instance") == self.instance:
                return entry
        return {}

    def regs(self, start: int = 0, length: Optional[int] = None) -> List[int]:
        """The register bytes the DUT reads now (I2C and SPI), see :meth:`SensorEmulation.regs`."""
        return self._emulation.regs(self.instance, start=start, length=length)

    def capture(self, samples: int = MAX_LA_BYTES) -> List[int]:
        """A raw capture of this I2C instance's bus pins (``sensor_la``). SPI and UART sensors:
        capture their pins with :meth:`~embeddedci.benchpod.BenchPod.la_capture`."""
        if self.kind and self.kind != "i2c":
            raise ValueError(f"sensor capture records I2C sensors only; capture the pins of "
                             f"{self.instance} (a {self.kind} sensor) with la_capture")
        return self._emulation.capture(self.instance, samples)

    def i2c_transactions(self, samples: int = MAX_LA_BYTES) -> List["I2CTransaction"]:
        """:meth:`capture` of an I2C instance, decoded into transactions."""
        from .. import i2c as _i2c

        return _i2c.decode(self.capture(samples))

    def stop(self) -> List[str]:
        """Stop the instance: the pod releases its pins and forgets the definition. Returns the
        names the pod stopped."""
        return self._emulation.stop(self.instance)

    def __enter__(self) -> "Sensor":
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.stop()
        except BenchPodError:
            if exc[0] is None:
                raise


class SensorEmulation:
    """``bp.sensors``: define, start, drive and stop emulated sensors.

    ``library_version`` picks the sensor library for names such as ``"bme280"`` (``None``: the
    ``BENCHPOD_SENSOR_LIBRARY`` variable, benchpod-cli's ``sensor_library`` setting, else
    :data:`~embeddedci.benchpod.sensors.SENSOR_LIBRARY_VERSION`).
    """

    def __init__(self, pod: "BenchPod", *, library_version: Optional[str] = None) -> None:
        self._pod = pod
        self.library_version = library_version

    # -- plumbing ---------------------------------------------------------------

    def _require(self) -> None:
        if not self._pod.supports("sensors"):
            raise UnsupportedFeatureError(
                TOO_OLD, feature="sensors",
                firmware_version=self._pod.capabilities.firmware_version)

    def _command(self, req: Dict[str, Any]) -> Dict[str, Any]:
        reply = self._pod.command(req)
        return reply if isinstance(reply, dict) else {}

    def library(self, version: Optional[str] = None, *, offline: bool = False) -> SensorLibrary:
        """The sensor library (``version`` overrides :attr:`library_version`)."""
        return sensor_library(version or self.library_version, offline=offline)

    def _document(self, source: DefinitionSource, base_dir: Any = None) -> Dict[str, Any]:
        lib = None
        if isinstance(source, str) and "\n" not in source and not os.path.isfile(source) \
                and not source.lstrip().startswith("{"):
            lib = self.library()
        return load_document(source, library=lib, base_dir=base_dir)

    def _pins(self, kind: str, pins: Optional[Mapping[str, PinValue]]) -> Dict[str, Any]:
        roles = PIN_ROLES.get(kind)
        if roles is None:
            raise ValueError(f"unknown sensor kind {kind!r}")
        given = {str(k).lower(): v for k, v in (pins or {}).items() if v is not None}
        unknown = sorted(set(given) - set(roles))
        if kind == "spi" and "drdy" in unknown:
            raise ValueError("the emulated SPI target has no data-ready pin")
        if unknown:
            raise ValueError(f"a {kind} sensor takes pins {', '.join(roles)}, not "
                             f"{', '.join(unknown)}")
        out: Dict[str, Any] = {}
        if kind == "analog":
            output = str(given.get("output", "dac")).lower()
            if output not in ANALOG_OUTPUTS:
                raise ValueError(f"an analog sensor's output is 'dac' or 'current', got {output!r}")
            return {"output": output}
        for role, wired in roles.items():
            if role in given:
                out[role] = self._pod._resolve_la(given[role], role)[0]
            elif wired is not None and getattr(self._pod.wiring, wired, None) is not None:
                out[role] = int(getattr(self._pod.wiring, wired))
        missing = [r for r in _REQUIRED_PINS[kind] if r not in out]
        if missing:
            raise ValueError(f"a {kind} sensor needs pins {', '.join(missing)} (LA channels 1-14 "
                             "or wiring names)")
        return out

    # -- definitions ------------------------------------------------------------

    def define(self, definition: DefinitionSource, *, instance: Optional[str] = None,
               dry_run: bool = False) -> Dict[str, Any]:
        """Upload and compile a definition (a path, dict, YAML/JSON text or library name) as
        ``instance`` (default: its ``name``). Defining a running instance stops it first.
        Returns the pod's report: ``kind``, ``name``, ``label``, ``program_bytes``, ``params``
        and the bus settings. ``dry_run=True`` compiles and keeps nothing (:meth:`validate`)."""
        self._require()
        doc = self._document(definition)
        check_definition(doc)
        name = _default_instance(instance, doc)
        reply: Dict[str, Any] = {}
        for req in upload_requests("sensor_define", to_json_bytes(doc), instance=name,
                                   dry_run=dry_run):
            reply = self._command(req)
        return reply

    def validate(self, definition: DefinitionSource) -> Dict[str, Any]:
        """Compile a definition on the pod without keeping it. Returns the compile report; a
        definition with an error raises :class:`~embeddedci.benchpod.errors.FirmwareError`
        whose message names the place (``registers[4].s16be: unknown name 'ax' at 17``)."""
        doc = self._document(definition)
        instance = doc.get("name") if isinstance(doc.get("name"), str) and INSTANCE_RE.match(
            str(doc.get("name"))) else "validate"
        return self.define(doc, instance=instance, dry_run=True)

    def start(self, definition: DefinitionSource, *, instance: Optional[str] = None,
              pins: Optional[Mapping[str, PinValue]] = None,
              address: Union[int, str, None] = None, baud: Optional[int] = None,
              values: Optional[Mapping[str, Any]] = None) -> Sensor:
        """Define ``definition`` and start it; returns its :class:`Sensor`.

        ``pins`` maps roles to LA channels or wiring names: I2C ``sda``, ``scl`` (default: the
        profile's ``i2c_sda``/``i2c_scl``) and ``drdy``; SPI ``sclk``, ``mosi``, ``miso``, ``cs``
        (default: the profile's ``spi_*``; the emulated SPI target has no data-ready pin); UART ``tx`` (pod to DUT) and ``rx``;
        analog ``output``, ``"dac"`` (default) or ``"current"``. ``address`` picks one of an I2C
        definition's addresses, ``baud`` overrides a UART one, ``values`` sets initial readings.
        Set the LA voltage and the I2C pull-ups first. I2C, SPI and UART sensors need the
        emulation FPGA image, which the pod loads when another runs (``started["image_loaded"]``);
        it stays loaded after :meth:`stop` until :meth:`~embeddedci.benchpod.BenchPod.fpga_image`
        switches it (the SDK's control loop and deep replay do that when no such sensor runs).
        """
        doc = self._document(definition)
        check_definition(doc)
        name = _default_instance(instance, doc)
        req: Dict[str, Any] = {"cmd": "sensor_start", "instance": name,
                               "pins": self._pins(str(doc["kind"]), pins)}
        addr = _address(address)
        if addr is not None:
            req["address"] = addr
        if baud is not None:
            req["baud"] = int(baud)
        initial = _values(values, {})
        if initial:
            req["values"] = initial
        info = self.define(doc, instance=name)
        started = self._command(req)
        return Sensor(self, name, info=info, started=started)

    def sensor(self, instance: str) -> Sensor:
        """A :class:`Sensor` handle for an instance that already runs (its ``info`` from
        :meth:`status`)."""
        name = _instance_name(instance)
        for entry in self.status(name).get("instances") or []:
            if isinstance(entry, dict) and entry.get("instance") == name:
                return Sensor(self, name, info=entry)
        raise BenchPodError(f"no sensor instance {name!r} runs on the pod")

    # -- values, stimuli, faults ------------------------------------------------------

    def set(self, instance: str, values: Optional[Mapping[str, Any]] = None, **kw: Any) -> Dict[str, Any]:
        """Change an instance's readings (``set("env", temperature_c=31.5)``). The pod refuses a
        value outside its param's range, with the range."""
        self._require()
        vals = _values(values, kw)
        if not vals:
            raise ValueError("sensor set needs at least one value")
        return self._command({"cmd": "sensor_set", "instance": _instance_name(instance),
                              "values": vals})

    def load_stimulus(self, stimulus: DefinitionSource, *,
                      base_dir: Union[str, "os.PathLike[str]", None] = None) -> Dict[str, Any]:
        """Upload a stimulus (a path, dict, YAML/JSON text or library name; ``data`` segment
        files are inlined). It replaces the loaded one; :meth:`start_stimulus` plays it."""
        self._require()
        doc = self._document(stimulus, base_dir=base_dir)
        check_stimulus(doc)
        reply: Dict[str, Any] = {}
        for req in upload_requests("sensor_stimulus", to_json_bytes(doc)):
            reply = self._command(req)
        return reply

    def start_stimulus(self, at_s: Optional[float] = None) -> Dict[str, Any]:
        """Play the loaded stimulus from t = 0 (or ``at_s``)."""
        self._require()
        req: Dict[str, Any] = {"cmd": "sensor_stimulus_start"}
        if at_s is not None:
            req["at_s"] = float(_number("at_s", at_s))
        return self._command(req)

    def stop_stimulus(self) -> Dict[str, Any]:
        """Stop the stimulus; signals hold their last values."""
        self._require()
        return self._command({"cmd": "sensor_stimulus_stop"})

    def fault(self, instance: str, type: str, *, duration_s: Optional[float] = None,
              **fields: Any) -> Dict[str, Any]:
        """Inject a fault now: ``type`` and its fields as in the format's faults section
        (``param``, ``value``, ``at``, ``mask``, ``rate_hz``). Without ``duration_s`` it lasts
        until :meth:`clear_fault`."""
        self._require()
        req: Dict[str, Any] = {"cmd": "sensor_fault", "instance": _instance_name(instance),
                               "type": str(type)}
        if duration_s is not None:
            req["duration_s"] = float(_number("duration_s", duration_s))
        for key, value in fields.items():
            if value is not None:
                req[key] = value
        return self._command(req)

    def clear_fault(self, instance: str) -> Dict[str, Any]:
        """End an instance's faults."""
        self._require()
        return self._command({"cmd": "sensor_fault_clear", "instance": _instance_name(instance)})

    # -- lifecycle and inspection ----------------------------------------------------

    def stop(self, instance: Optional[str] = None) -> List[str]:
        """Stop ``instance``, or every instance when None. The pod releases the pins and forgets
        the definitions; the emulation image stays loaded. Returns the names it stopped."""
        self._require()
        if instance is None:
            reply = self._command({"cmd": "sensor_stop", "all": True})
        else:
            reply = self._command({"cmd": "sensor_stop", "instance": _instance_name(instance)})
        return [str(n) for n in reply.get("stopped") or []]

    def status(self, instance: Optional[str] = None) -> Dict[str, Any]:
        """``image``, ``stimulus`` (``loaded``, ``running``, ``t_s``) and ``instances``, each with
        its ``slot``, ``pins``, ``values``, ``faults`` (objects with ``type`` and ``left_s``) and
        ``bus`` counters (``writes``, ``last_write``...; ``transfers`` is always None: the
        gateware counts written bytes). ``instance`` reports that one only. The pod pages a long
        list (``next``); this asks for every page and returns them as one."""
        self._require()
        req: Dict[str, Any] = {"cmd": "sensor_status"}
        if instance is not None:
            req["instance"] = _instance_name(instance)
        out = self._command(dict(req))
        instances = list(out.get("instances") or [])
        seen = {0}
        while isinstance(out.get("next"), int) and not isinstance(out.get("next"), bool):
            first = int(out["next"])
            if first in seen or first <= 0:
                break
            seen.add(first)
            page = self._command({**req, "first": first})
            instances.extend(page.get("instances") or [])
            out = {**out, **{k: v for k, v in page.items() if k != "instances"}}
            if "next" not in page:
                out.pop("next", None)
        out.pop("next", None)
        out["instances"] = instances
        return out

    def regs(self, instance: str, *, start: int = 0, length: Optional[int] = None) -> List[int]:
        """The register bytes of an I2C/SPI instance the DUT reads now (its own writes
        included): ``length`` bytes from ``start`` (0..255) of its 256-byte image (``i2c.page``
        is the window), default to the end of the image."""
        self._require()
        start = int(start)
        if not 0 <= start <= 255:
            raise ValueError(f"start is 0..255 (the offset in the 256-byte image), got {start}")
        n = 256 - start if length is None else int(length)
        if not 1 <= n <= 256 - start:
            raise ValueError(f"length is 1..{256 - start} from start {start}, got {length!r}")
        reply = self._command({"cmd": "sensor_regs", "instance": _instance_name(instance),
                               "start": f"0x{start:02X}", "len": n})
        return decode_regs(reply)

    def capture(self, instance: str, samples: int = MAX_LA_BYTES) -> List[int]:
        """A raw capture of an I2C instance's bus pins (``sensor_la``): ``samples`` packed bytes
        (1..4096, four ``{SCL,SDA}`` samples per byte). SPI and UART instances are refused by the
        pod: capture their pins with :meth:`~embeddedci.benchpod.BenchPod.la_capture`."""
        self._require()
        n = int(samples)
        if not 1 <= n <= MAX_LA_BYTES:
            raise ValueError(f"samples is 1..{MAX_LA_BYTES} packed bytes, got {samples!r}")
        return self._samples({"cmd": "sensor_la", "instance": _instance_name(instance),
                              "samples": n})

    def running_digital(self) -> List[str]:
        """The I2C, SPI and UART instances that run (they hold the emulation image)."""
        return [str(e.get("instance")) for e in self.status().get("instances") or []
                if isinstance(e, dict) and e.get("running") and e.get("kind") != "analog"]

    def i2c_transactions(self, instance: str, samples: int = MAX_LA_BYTES) -> List["I2CTransaction"]:
        """:meth:`capture` of an I2C instance decoded into transactions (packed ``{SCL,SDA}``
        samples, as the pod's I2C bus capture has always sent them)."""
        from .. import i2c as _i2c

        return _i2c.decode(self.capture(instance, samples))

    def _samples(self, req: Dict[str, Any]) -> List[int]:
        fn = getattr(self._pod.transport, "samples", None)
        if fn is None:
            raise BenchPodError("this needs a transport with chunked JSON replies "
                                "(LAN, USB network or cloud)")
        from ..client import _classified_errors

        with _classified_errors():
            return list(fn(req))
