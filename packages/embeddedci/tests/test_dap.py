"""Tests for the CMSIS-DAP-over-tunnel host path (embeddedci.benchpod.dap).

A Python loopback emulates the pod's PROTO_DAP framed processor, so we can
exercise (a) our own length-framing and (b) pyOCD's real CMSIS-DAP command
layer riding on top of our Interface — without hardware.
"""
from __future__ import annotations

import struct

import pytest

from embeddedci.benchpod import dap as dapmod


# --------------------------------------------------------------------------
# Fake pod firmware: a minimal CMSIS-DAP responder (subset used on connect).
# --------------------------------------------------------------------------
DPIDR = 0x2BA01477


def fake_dap_process(req: bytes) -> bytes:
    cmd = req[0]
    if cmd == 0x00:  # DAP_Info
        info = req[1]
        if info == 0xF0:      # CAPABILITIES
            return bytes([0x00, 0x01, 0x01])
        if info == 0xFE:      # PACKET_COUNT
            return bytes([0x00, 0x01, 0x01])
        if info == 0xFF:      # PACKET_SIZE
            return bytes([0x00, 0x02, 0x00, 0x01])  # 256
        if info == 0x04:      # FW_VER
            s = b"1.2.0\x00"
            return bytes([0x00, len(s)]) + s
        return bytes([0x00, 0x00])  # empty string / unsupported
    if cmd == 0x01:  # HostStatus
        return bytes([0x01, 0x00])
    if cmd == 0x02:  # Connect -> SWD
        return bytes([0x02, 0x01])
    if cmd == 0x03:  # Disconnect
        return bytes([0x03, 0x00])
    if cmd == 0x04:  # TransferConfigure
        return bytes([0x04, 0x00])
    if cmd == 0x05:  # Transfer
        count = req[2]
        out = bytearray([0x05, 0x00, 0x01])  # filled below: count, ack
        done = 0
        p = 3
        for _ in range(count):
            if p >= len(req):
                break
            request = req[p]; p += 1
            if request & 0x02:  # read
                out += struct.pack("<I", DPIDR)
            else:               # write: 4 data bytes in request
                p += 4
            done += 1
        out[1] = done
        out[2] = 0x01  # ACK OK
        return bytes(out)
    if cmd in (0x11, 0x12, 0x13):  # SWJ_Clock / SWJ_Sequence / SWD_Configure
        return bytes([cmd, 0x00])
    return bytes([0xFF])  # DAP_Invalid


class FakeFirmwareLink:
    """RawLink loopback that runs fake_dap_process on each complete frame."""

    def __init__(self) -> None:
        self._in = bytearray()
        self._out = bytearray()
        self.closed = False
        self.left_dap = False

    def write(self, data: bytes) -> int:
        self._in.extend(data)
        while len(self._in) >= 2:
            n = self._in[0] | (self._in[1] << 8)
            if len(self._in) < 2 + n:
                break
            payload = bytes(self._in[2:2 + n])
            del self._in[:2 + n]
            if n == 0:
                self.left_dap = True
                continue
            resp = fake_dap_process(payload)
            self._out += bytes((len(resp) & 0xFF, (len(resp) >> 8) & 0xFF)) + resp
        return len(data)

    def read(self, n: int) -> bytes:
        take = bytes(self._out[:n])
        del self._out[:n]
        return take

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self) -> None:
        self.link = FakeFirmwareLink()
        self.dap_start_args = None

    def dap_start(self, swclk, swdio, nreset):
        self.dap_start_args = (swclk, swdio, nreset)
        return self.link


# --------------------------------------------------------------------------
# Pure framing (no pyOCD).
# --------------------------------------------------------------------------
def test_framer_roundtrip():
    link = FakeFirmwareLink()
    fr = dapmod._Framer(link)
    fr.send(bytes([0x00, 0xF0]))            # DAP_Info CAPABILITIES
    assert fr.recv() == bytes([0x00, 0x01, 0x01])
    fr.send(bytes([0x00, 0xFF]))            # PACKET_SIZE
    assert fr.recv() == bytes([0x00, 0x02, 0x00, 0x01])


def test_framer_split_reads():
    """A response delivered in dribs and drabs still reassembles."""
    class Drip(FakeFirmwareLink):
        def read(self, n):  # one byte at a time
            return super().read(1)
    link = Drip()
    fr = dapmod._Framer(link)
    fr.send(bytes([0x00, 0xF0]))
    assert fr.recv() == bytes([0x00, 0x01, 0x01])


# --------------------------------------------------------------------------
# pyOCD interface + DAP access (needs the pyocd extra).
# --------------------------------------------------------------------------
def test_interface_lazy_open_and_io():
    pytest.importorskip("pyocd")
    t = FakeTransport()
    iface = dapmod.build_dap_interface(t, swclk=11, swdio=12, nreset=3)
    # construction does NOT open the tunnel
    assert t.dap_start_args is None
    iface.open()
    assert t.dap_start_args == (11, 12, 3)
    iface.write([0x00, 0xF0])
    assert bytes(iface.read()) == bytes([0x00, 0x01, 0x01])
    iface.close()
    assert t.link.left_dap is True   # zero-length frame sent on close
    assert t.link.closed is True


def test_interface_identity():
    pytest.importorskip("pyocd")
    iface = dapmod.build_dap_interface(FakeTransport(), swclk=1, swdio=2)
    assert iface.is_bulk is True
    assert iface.has_swo_ep is False
    assert iface.get_packet_size() == dapmod.DAP_PACKET_SIZE
    assert "BenchPod" in iface.product_name


def test_pyocd_dapaccess_drives_our_interface():
    """pyOCD's real DAPAccess command layer talks to the pod through our framing."""
    pytest.importorskip("pyocd")
    from pyocd.probe.pydapaccess.dap_access_cmsis_dap import DAPAccessCMSISDAP

    t = FakeTransport()
    iface = dapmod.build_dap_interface(t, swclk=11, swdio=12, nreset=3)
    dap_access = DAPAccessCMSISDAP(None, interface=iface)
    dap_access.open()
    try:
        caps = dap_access.identify(DAPAccessCMSISDAP.ID.CAPABILITIES)
        assert caps & 0x01            # SWD capable, reported by the fake pod
        size = dap_access.identify(DAPAccessCMSISDAP.ID.MAX_PACKET_SIZE)
        assert size == dapmod.DAP_PACKET_SIZE
    finally:
        dap_access.close()
