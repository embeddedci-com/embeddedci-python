"""Serial auto-detection: probe the ports, never trust the vendor id alone.

The pod's USB vendor ids are shared with other hardware and differ between pod
generations (RP2350 -> 0x2E8A, STM32H563 -> ST's stock 0x0483), so a vendor-id
filter both misses real pods and latches onto impostors. These tests pin the
behaviour that replaced it: rank the ports, then probe each one.
"""

import pytest

from embeddedci.benchpod.errors import ConnectionConfigError, TransportError
from embeddedci.benchpod.transport import serial as serial_mod


class FakePort:
    """Minimal pyserial stand-in: replies to `status` with `text`, to `identity` with `ident`."""

    instances: list = []

    def __init__(self, device, text="", ident=None):
        self.device = device
        self._text = text.encode()
        self._ident = ident
        self._sent = bytearray()
        self.closed = False
        FakePort.instances.append(self)

    def write(self, data):
        self._sent.extend(data)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        if b"identity" in self._sent and self._ident is not None:
            reply = f"identity\r\n{self._ident}\r\n> "
            self._text, self._ident = reply.encode(), None
            self._sent = bytearray(b"status")
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


def install(monkeypatch, infos, replies, idents=None):
    """Wire _import_serial to fake pyserial modules built from `replies` (status text per port)
    and `idents` (the `identity` reply line per port; none = firmware without the command)."""
    opened = []
    idents = idents or {}
    FakePort.instances = []

    class FakeSerialMod:
        @staticmethod
        def Serial(device, baud, timeout=None):  # noqa: N802 - pyserial's name
            opened.append(device)
            if device not in replies:
                raise OSError(f"cannot open {device}")
            return FakePort(device, replies[device], idents.get(device, "unknown command: identity"))

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


def test_pid_codes_vendor_id_ranks_as_a_pod_port(monkeypatch):
    # Firmware 3.9.0 enumerates as 1209:0001 (console + USB network adapter). It ranks with the
    # other pod vendor ids, ahead of an unknown port, but is still identified by probing.
    infos = [PortInfo("/dev/cu.aaa-other", vid=0x1234), PortInfo("/dev/cu.zzz-pod", vid=0x1209)]
    opened = install(monkeypatch, infos, {"/dev/cu.aaa-other": "not a pod\n> ",
                                          "/dev/cu.zzz-pod": POD_STATUS})
    assert 0x1209 in serial_mod.POD_VIDS
    assert serial_mod._candidate_ports() == ["/dev/cu.zzz-pod", "/dev/cu.aaa-other"]
    assert serial_mod.autodetect_port() == "/dev/cu.zzz-pod"
    assert opened == ["/dev/cu.zzz-pod"]


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
    # The port naming the pod is ranked first; the impostor shares a pod vendor id, so it is
    # still probed for a second pod, but it is not one.
    assert opened == ["/dev/cu.pod", "/dev/cu.st-vcp"]


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


# -- several pods: usb:<name> ----------------------------------------------------------------

def _two_pods(monkeypatch, *, same=False, tty_vid=None):
    infos = [
        PortInfo("/dev/cu.usbmodemA", vid=0x1209),
        PortInfo("/dev/tty.usbmodemA", vid=tty_vid),
        PortInfo("/dev/cu.usbmodemB", vid=0x1209),
        PortInfo("/dev/cu.other", vid=0x1234),
    ]
    replies = {"/dev/cu.usbmodemA": POD_STATUS, "/dev/tty.usbmodemA": POD_STATUS,
               "/dev/cu.usbmodemB": POD_STATUS, "/dev/cu.other": "not a pod\n> "}
    a, b = "identity aaaaaa AAAApubkey", "identity bbbbbb BBBBpubkey"
    idents = {"/dev/cu.usbmodemA": a, "/dev/tty.usbmodemA": a,
              "/dev/cu.usbmodemB": a if same else b}
    return install(monkeypatch, infos, replies, idents)


def _all_closed():
    return all(p.closed for p in FakePort.instances)


def test_parse_identity():
    assert serial_mod.parse_identity("identity\r\nidentity baea06 Zm9vYmFy\r\n> ") == "baea06"
    assert serial_mod.parse_identity("identity\r\nidentity unknown no key yet\r\n> ") == ""
    assert serial_mod.parse_identity("identity\r\nunknown command\r\n> ") == ""


def test_identity_on_the_transport():
    port = FakePort("/dev/x", "", "identity BAEA06 Zm9vYmFy")
    t = serial_mod.SerialTransport("/dev/x", port=port, timeout=1)
    assert t.identity() == "baea06"


@pytest.mark.parametrize("name", ["bbbbbb", "benchpod-bbbbbb", "BenchPod BBBBBB", "benchpod-bbbbbb.local"])
def test_name_selects_that_pod(monkeypatch, name):
    _two_pods(monkeypatch)
    assert serial_mod.autodetect_port(name) == "/dev/cu.usbmodemB"
    assert _all_closed()  # every probe console is closed; the caller opens the returned path


def test_name_selects_the_first_pod_without_probing_further(monkeypatch):
    opened = _two_pods(monkeypatch)
    assert serial_mod.autodetect_port("aaaaaa") == "/dev/cu.usbmodemA"
    assert opened == ["/dev/cu.usbmodemA"]


def test_unknown_name_lists_the_pods_found(monkeypatch):
    _two_pods(monkeypatch)
    with pytest.raises(ConnectionConfigError) as exc:
        serial_mod.autodetect_port("cccccc")
    assert str(exc.value) == ("no pod named cccccc on USB (found: benchpod-aaaaaa on "
                              "/dev/cu.usbmodemA, benchpod-bbbbbb on /dev/cu.usbmodemB)")
    assert _all_closed()


def test_two_pods_without_a_name_is_an_error_listing_both(monkeypatch):
    opened = _two_pods(monkeypatch)
    with pytest.raises(ConnectionConfigError) as exc:
        serial_mod.autodetect_port()
    assert str(exc.value) == (
        "2 pods on USB (benchpod-aaaaaa on /dev/cu.usbmodemA, benchpod-bbbbbb on "
        "/dev/cu.usbmodemB): pick one with connection usb:<name>, e.g. usb:benchpod-aaaaaa")
    # The tty twin has no vendor id and the other port is not a pod's: neither is probed
    # once a pod has been found.
    assert opened == ["/dev/cu.usbmodemA", "/dev/cu.usbmodemB"]
    assert _all_closed()


def test_one_pod_seen_on_two_ports_is_one_pod(monkeypatch):
    # The tty twin enumerates with the pod's vendor id here, so it is probed: same identity.
    opened = _two_pods(monkeypatch, same=True, tty_vid=0x1209)
    assert serial_mod.autodetect_port() == "/dev/cu.usbmodemA"
    assert opened == ["/dev/cu.usbmodemA", "/dev/cu.usbmodemB", "/dev/tty.usbmodemA"]
    assert _all_closed()


def test_two_ports_without_identity_count_as_two(monkeypatch):
    # Old firmware has no identity command: two pods cannot be told apart from twins, so the
    # error asks for a name (or a device path) instead of guessing.
    infos = [PortInfo("/dev/cu.a", vid=0x0483), PortInfo("/dev/cu.b", vid=0x0483)]
    install(monkeypatch, infos, {"/dev/cu.a": POD_STATUS, "/dev/cu.b": POD_STATUS})
    with pytest.raises(ConnectionConfigError, match="a pod with no identity on /dev/cu.a"):
        serial_mod.autodetect_port()
