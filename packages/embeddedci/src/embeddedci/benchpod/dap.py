"""Drive a BenchPod as a CMSIS-DAP probe over our own framed transport.

This is the fast flash/debug path: instead of tunnelling per-bit OpenOCD
``remote_bitbang`` (one byte per SWCLK edge, blocking on every sample), the pod
runs a real CMSIS-DAP command processor locally and we batch *DAP transfers*
across the internet. pyOCD's mature CMSIS-DAP stack — ``DAP_TransferBlock``
batching, posted-read/RDBUFF handling, WAIT retries and every vendor's flash
algorithm — rides on top unchanged.

The seam is :class:`BenchPodDAPInterface`, a pyOCD ``Interface`` backend whose
``write``/``read`` length-frame CMSIS-DAP packets onto a BenchPod
:class:`~embeddedci.benchpod.transport.base.RawLink` (TCP socket or cloud WS
tunnel). Wire framing (ours):

    request   host -> pod:  [len_lo][len_hi][CMSIS-DAP command bytes]
    response  pod -> host:  [len_lo][len_hi][CMSIS-DAP response bytes]

A zero-length frame leaves DAP mode. ``pyocd`` is an optional dependency:
``pip install 'embeddedci[pyocd]'``.

Typical use (also wrapped by ``BenchPod.flash_pyocd``)::

    from embeddedci.benchpod import open_transport_for, dap
    with dap.dap_session(transport, swclk=11, swdio=12, nreset=3,
                         target="stm32h563xx") as session:
        from pyocd.flash.file_programmer import FileProgrammer
        FileProgrammer(session).program("fw.elf")
"""

from __future__ import annotations

from typing import Any, Optional

from .transport.base import RawLink, Transport

# Must match DAP_PACKET_SIZE in the firmware (dap.h).
DAP_PACKET_SIZE = 256
_LEAVE_DAP = b"\x00\x00"   # zero-length frame: firmware returns to JSON mode


def _require_pyocd() -> None:
    try:
        import pyocd  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "CMSIS-DAP/pyOCD support needs the pyocd extra: "
            "pip install 'embeddedci[pyocd]'"
        ) from exc


def _make_interface_base():
    """Return the pyOCD ``Interface`` base class (imported lazily)."""
    _require_pyocd()
    from pyocd.probe.pydapaccess.interface.interface import Interface
    return Interface


class _Framer:
    """Length-prefix framing over a :class:`RawLink` byte stream."""

    def __init__(self, link: RawLink) -> None:
        self._link = link

    def send(self, packet: bytes) -> None:
        n = len(packet)
        self._link.write(bytes((n & 0xFF, (n >> 8) & 0xFF)) + packet)

    def recv(self) -> bytes:
        hdr = self._read_exact(2)
        n = hdr[0] | (hdr[1] << 8)
        return self._read_exact(n)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._link.read(n - len(buf))
            if not chunk:
                raise IOError("BenchPod DAP tunnel closed mid-packet")
            buf.extend(chunk)
        return bytes(buf)


def build_dap_interface(transport: Transport, swclk: int, swdio: int,
                        nreset: Optional[int] = None):
    """Construct a :class:`BenchPodDAPInterface` bound to ``transport``.

    The interface is *lazy*: it calls ``transport.dap_start(...)`` only when
    pyOCD opens it, so it fits pyOCD's open/close lifecycle for both the
    ``dap_session`` helper and the ``pyocd`` CLI plugin.
    """
    Interface = _make_interface_base()

    class BenchPodDAPInterface(Interface):
        """pyOCD Interface that ships CMSIS-DAP packets over a BenchPod link."""

        def __init__(self) -> None:
            super().__init__()
            self._transport = transport
            self._pins = (swclk, swdio, nreset)
            self._framer: Optional[_Framer] = None
            self._link: Optional[RawLink] = None
            # Plain attributes pyOCD reads at construction time.
            self.vendor_name = "EmbeddedCI"
            self.product_name = "BenchPod CMSIS-DAP"
            self.serial_number = "benchpod"
            self.vid = 0
            self.pid = 0
            self.packet_size = DAP_PACKET_SIZE
            self.packet_count = 1   # strict request/response: one packet in flight

        # -- identity --------------------------------------------------------
        @property
        def has_swo_ep(self) -> bool:
            return False

        @property
        def is_bulk(self) -> bool:
            # Bulk (CMSIS-DAP v2) semantics: pyOCD sends exact-length packets,
            # no HID report padding — so our frames stay minimal.
            return True

        def get_serial_number(self) -> str:
            return self.serial_number

        # -- lifecycle -------------------------------------------------------
        def open(self) -> None:
            if self._framer is None:
                sw, dio, nr = self._pins
                self._link = self._transport.dap_start(sw, dio, nr)
                self._framer = _Framer(self._link)

        def close(self) -> None:
            if self._link is not None:
                try:
                    self._link.write(_LEAVE_DAP)   # leave DAP mode cleanly
                except Exception:
                    pass
                try:
                    self._link.close()
                except Exception:
                    pass
            self._link = None
            self._framer = None

        # -- packet sizing ---------------------------------------------------
        def get_packet_count(self) -> int:
            return self.packet_count

        def set_packet_count(self, count: int) -> None:
            # Keep one in flight regardless of what pyOCD requests: our framed
            # transport is strict request/response.
            self.packet_count = 1

        def get_packet_size(self) -> int:
            return self.packet_size

        def set_packet_size(self, size: int) -> None:
            self.packet_size = size

        # -- I/O -------------------------------------------------------------
        def write(self, data) -> None:
            if self._framer is None:
                raise IOError("BenchPod DAP interface not open")
            self._framer.send(bytes(data))

        def read(self) -> bytearray:
            if self._framer is None:
                raise IOError("BenchPod DAP interface not open")
            return bytearray(self._framer.recv())

    return BenchPodDAPInterface()


def open_dap_probe(transport: Transport, *, swclk: int, swdio: int,
                   nreset: Optional[int] = None):
    """Build a pyOCD ``CMSISDAPProbe`` driving the pod over ``transport``."""
    _require_pyocd()
    from pyocd.probe.cmsis_dap_probe import CMSISDAPProbe
    from pyocd.probe.pydapaccess.dap_access_cmsis_dap import DAPAccessCMSISDAP

    interface = build_dap_interface(transport, swclk, swdio, nreset)
    dap_access = DAPAccessCMSISDAP(None, interface=interface)
    return CMSISDAPProbe(dap_access)


def dap_session(transport: Transport, *, swclk: int, swdio: int,
                nreset: Optional[int] = None, target: Optional[str] = None,
                options: Optional[dict] = None):
    """Return a pyOCD ``Session`` whose probe is this BenchPod over ``transport``.

    ``target`` is a pyOCD target type name (e.g. ``"stm32h563xx"`` or
    ``"cortex_m"``). Use as a context manager; opening the session calls
    ``dap_start`` and closing it returns the pod to JSON mode.
    """
    _require_pyocd()
    from pyocd.core.session import Session

    probe = open_dap_probe(transport, swclk=swclk, swdio=swdio, nreset=nreset)
    opts: dict = {}
    if target:
        opts["target_override"] = target
    if options:
        opts.update(options)
    return Session(probe, options=opts)


def flash(transport: Transport, firmware: str, *, target: str,
          swclk: int, swdio: int, nreset: Optional[int] = None,
          options: Optional[dict] = None) -> None:
    """Flash ``firmware`` onto the DUT via pyOCD over ``transport``.

    Raises pyOCD exceptions on failure (no target, flash error, etc.).
    """
    _require_pyocd()
    from pyocd.flash.file_programmer import FileProgrammer

    with dap_session(transport, swclk=swclk, swdio=swdio, nreset=nreset,
                     target=target, options=options) as session:
        FileProgrammer(session, chip_erase="sector").program(firmware)


# ---------------------------------------------------------------------------
# pyocd CLI plugin: `pyocd flash --uid benchpod:<connection> ...`
#
# Registered via the `pyocd.probe` entry point (see pyproject.toml). Optional —
# the primary path is dap_session()/BenchPod.flash_pyocd() from pytest.
# ---------------------------------------------------------------------------

def _probe_plugin():  # pragma: no cover - only imported by the pyocd CLI
    _require_pyocd()
    from pyocd.core.plugin import Plugin
    from pyocd.probe.cmsis_dap_probe import CMSISDAPProbe
    from pyocd.probe.pydapaccess.dap_access_cmsis_dap import DAPAccessCMSISDAP

    from .connection import resolve_connection
    from .transport import open_transport

    _SCHEME = "benchpod:"

    class BenchPodProbe(CMSISDAPProbe):
        """A CMSIS-DAP probe whose transport is a BenchPod connection string.

        unique_id form: ``benchpod:<connection>`` where ``<connection>`` is any
        BenchPod destination, e.g. ``benchpod:embeddedci:my-device`` or
        ``benchpod:192.168.1.5:8080``. Default SWD pins (11/12, nRESET 3) can be
        overridden with ``?swclk=&swdio=&nreset=``.
        """

        @classmethod
        def get_all_connected_probes(cls, unique_id=None, is_explicit=False):
            if unique_id and unique_id.startswith(_SCHEME):
                p = cls.get_probe_with_id(unique_id, is_explicit)
                return [p] if p else []
            return []

        @classmethod
        def get_probe_with_id(cls, unique_id, is_explicit=False):
            if not unique_id or not unique_id.startswith(_SCHEME):
                return None
            rest = unique_id[len(_SCHEME):]
            conn, _, query = rest.partition("?")
            pins = {"swclk": 11, "swdio": 12, "nreset": 3}
            for kv in query.split("&"):
                k, _, v = kv.partition("=")
                if k in pins and v:
                    pins[k] = int(v)
            transport = open_transport(resolve_connection(conn))
            interface = build_dap_interface(
                transport, pins["swclk"], pins["swdio"], pins["nreset"])
            return cls(DAPAccessCMSISDAP(None, interface=interface))

    class BenchPodProbePlugin(Plugin):
        def load(self):
            return BenchPodProbe

        @property
        def name(self) -> str:
            return "benchpod"

        @property
        def description(self) -> str:
            return "EmbeddedCI BenchPod CMSIS-DAP over TCP / cloud tunnel"

    return BenchPodProbePlugin


def __getattr__(name: str) -> Any:
    # Expose the plugin *class* lazily so importing this module never requires
    # pyocd. pyOCD's aggregator resolves the `benchpod` entry point and calls it
    # (``entry_point.load()()``) to instantiate the Plugin, so this must be the
    # class, not an instance.
    if name == "BenchPodProbePlugin":
        return _probe_plugin()
    raise AttributeError(name)
