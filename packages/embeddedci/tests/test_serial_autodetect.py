"""Serial auto-detection: probe the ports, never trust the vendor id alone.

The pod's USB vendor ids are shared with other hardware and differ between pod
generations (RP2350 -> 0x2E8A, STM32H563 -> ST's stock 0x0483), so a vendor-id
filter both misses real pods and latches onto impostors. These tests pin the
behaviour that replaced it: rank the ports, then probe each one.
"""

import pytest

from embeddedci.benchpod.errors import TransportError
from embeddedci.benchpod.transport import serial as serial_mod


class FakePort:
    """Minimal pyserial stand-in: replies to `status` with `text`."""

    def __init__(self, device, text=""):
        self.device = device
        self._text = text.encode()
        self._sent = bytearray()
        self.closed = False

    def write(self, data):
        self._sent.extend(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        if b"status" not in self._sent:
            return b""
        out, self._text = self._text[:n], self._text[n:]
        return out

    def close(self):
        self.closed = True


class PortInfo:
    def __init__(self, device, vid=None, product=None, manufacturer=None):
        self.device = device
        self.vid = vid
        self.product = product
        self.manufacturer = manufacturer
        self.description = None


def install(monkeypatch, infos, replies):
    """Wire _import_serial to fake pyserial modules built from `replies`."""
    opened = []

    class FakeSerialMod:
        @staticmethod
        def Serial(device, baud, timeout=None):  # noqa: N802 - pyserial's name
            opened.append(device)
            if device not in replies:
                raise OSError(f"cannot open {device}")
            return FakePort(device, replies[device])

    class FakeListPorts:
        @staticmethod
        def comports():
            return infos

    monkeypatch.setattr(serial_mod, "_import_serial", lambda: (FakeSerialMod, FakeListPorts))
    return opened


POD_STATUS = "  device : benchpod\n  ip     : 192.168.1.213\n> "


def test_finds_stm32_pod_under_st_vendor_id(monkeypatch):
    # The STM32 pod enumerates as 0483:5740. A 0x2E8A-only filter missed it
    # entirely, which broke `--benchpod-connection=serial` on current hardware.
    infos = [PortInfo("/dev/cu.usbmodem-stm", vid=0x0483, product="bench-pod STM32H563 CDC")]
    install(monkeypatch, infos, {"/dev/cu.usbmodem-stm": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.usbmodem-stm"


def test_finds_pod_even_with_unknown_vendor_id(monkeypatch):
    # Ranking is not filtering: a port no rule recognises is still probed.
    infos = [PortInfo("/dev/cu.mystery", vid=0x1234)]
    install(monkeypatch, infos, {"/dev/cu.mystery": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.mystery"


def test_skips_impostor_sharing_the_pod_vendor_id(monkeypatch):
    # 0483:5740 is ST's generic CDC pair, shared with every ST VCP on the bench;
    # the one that answers `status` as a pod is the one to pick.
    infos = [
        PortInfo("/dev/cu.st-vcp", vid=0x0483, product="STM32 Virtual COM Port"),
        PortInfo("/dev/cu.pod", vid=0x0483, product="bench-pod STM32H563 CDC"),
    ]
    opened = install(monkeypatch, infos, {"/dev/cu.st-vcp": "hello\n> ", "/dev/cu.pod": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.pod"
    # The port naming the pod is ranked first, so the impostor is never opened.
    assert opened == ["/dev/cu.pod"]


def test_unopenable_port_does_not_stop_the_search(monkeypatch):
    infos = [PortInfo("/dev/cu.busy", vid=0x2E8A), PortInfo("/dev/cu.pod", vid=0x0483)]
    install(monkeypatch, infos, {"/dev/cu.pod": POD_STATUS})  # busy port absent -> OSError
    assert serial_mod.autodetect_port() == "/dev/cu.pod"


def test_no_ports_at_all_mentions_the_cable(monkeypatch):
    install(monkeypatch, [], {})
    with pytest.raises(TransportError) as exc:
        serial_mod.autodetect_port()
    assert "no USB serial ports found" in str(exc.value)
    assert "charge-only" in str(exc.value)


def test_ports_but_no_pod_points_at_firmware(monkeypatch):
    infos = [PortInfo("/dev/cu.other", vid=0x1234)]
    install(monkeypatch, infos, {"/dev/cu.other": "not a pod\n> "})
    with pytest.raises(TransportError) as exc:
        serial_mod.autodetect_port()
    msg = str(exc.value)
    assert "/dev/cu.other" in msg          # says what it looked at
    assert "flash-self" in msg             # and the most likely fix
