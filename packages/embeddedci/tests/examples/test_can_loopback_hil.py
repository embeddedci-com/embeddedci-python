"""CAN hardware-in-the-loop example (single pod, no external CAN node).

The pod has one CAN transceiver (TCAN1044 on FDCAN1), so a self-contained test
uses FDCAN loopback: the node ACKs its own frames, so every frame written comes
straight back on read.

  * internal loopback — validates the FDCAN core + firmware path; needs nothing
    wired to the CAN screw terminal.
  * external loopback — TX drives the real transceiver pins and loops back to
    RX; validates the transceiver + PCB. Skipped if the bench doesn't echo
    (e.g. the bus is held dominant by another node).

Run against hardware:

    pytest --benchpod-connection=192.168.1.215 \
           tests/examples/test_can_loopback_hil.py

Skipped automatically without a --benchpod-connection.
"""

import pytest

from embeddedci.benchpod import BenchPod
from embeddedci.benchpod.errors import FirmwareError


def _requires_can(bp: BenchPod) -> None:
    """Skip on a board without the v2 CAN command surface (v1 RP2350B)."""
    board = str(bp.status().get("board", "")).lower()
    if "stm32" not in board:
        pytest.skip(f"CAN (FDCAN1) API is STM32H563-only; board is {board!r}")


def test_can_internal_loopback_roundtrip(benchpod: BenchPod):
    """Write two frames in internal loopback; read them back byte-for-byte."""
    _requires_can(benchpod)
    with benchpod.open_can(bitrate=500_000, mode="internal") as can:
        can.write(0x123, [0xDE, 0xAD, 0xBE, 0xEF])
        can.write(0x7A5, [0x01], ext=False)

        f1 = can.expect(can_id=0x123, timeout=1.0)
        assert f1.data == b"\xde\xad\xbe\xef"
        f2 = can.expect(can_id=0x7A5, timeout=1.0)
        assert f2.data == b"\x01"

        st = can.status()
        assert st["enabled"] and st["mode"] == "internal"
        assert not st["bus_off"]


def test_can_extended_id_loopback(benchpod: BenchPod):
    """A 29-bit extended identifier survives the loopback."""
    _requires_can(benchpod)
    with benchpod.open_can(mode="internal") as can:
        can.write(0x18FF50E5, [0x11, 0x22], ext=True)
        f = can.expect(can_id=0x18FF50E5, timeout=1.0)
        assert f.ext and f.data == b"\x11\x22"


def test_can_external_loopback_roundtrip(benchpod: BenchPod):
    """External loopback drives the real transceiver. Skips if it doesn't echo
    (a bench-wiring/bus condition, not a firmware regression)."""
    _requires_can(benchpod)
    with benchpod.open_can(bitrate=500_000, mode="external", term=True) as can:
        can.write(0x2AB, [0xC0, 0xFF, 0xEE])
        f = can.read_until(can_id=0x2AB, timeout=1.0)
        if f is None:
            st = can.status()
            pytest.skip(f"external loopback did not echo (tec={st['tec']} "
                        f"rec={st['rec']} bus_off={st['bus_off']}); check the "
                        f"transceiver/bus")
        assert f.data == b"\xc0\xff\xee"


def test_can_termination_toggle(benchpod: BenchPod):
    """The 120 Ω termination is a plain GPIO — togglable even with CAN down."""
    _requires_can(benchpod)
    try:
        assert benchpod.can_term(True)["term"] is True
        assert benchpod.can_status()["term"] is True
        assert benchpod.can_term(False)["term"] is False
    finally:
        benchpod.can_disable()


def test_can_unsupported_bitrate_rejected(benchpod: BenchPod):
    """A bitrate with no exact bit timing is rejected, not silently mis-clocked."""
    _requires_can(benchpod)
    with pytest.raises(FirmwareError):
        benchpod.can_config(bitrate=777_777, mode="internal")
    benchpod.can_disable()
