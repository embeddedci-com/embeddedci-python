"""Serial auto-detection: open only ports that identify themselves as a pod, then probe them.

0483:5740 is ST's generic virtual COM port id, shared with spectrum analyzers (a tinySA froze when
"status" was written into it), ST-Links and customers' own boards, so a port is opened only when its
USB id is 1209:EC10, its manufacturer is EmbeddedCI, or its product contains "bench". An explicit
device path is not filtered.
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
    def __init__(self, device, vid=None, product=None, manufacturer=None, pid=None):
        self.device = device
        self.vid = vid
        self.pid = pid
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
    # Firmware up to 3.8.x: 0483:5740, product "bench-pod STM32H563 CDC".
    infos = [PortInfo("/dev/cu.usbmodem-stm", vid=0x0483, product="bench-pod STM32H563 CDC")]
    install(monkeypatch, infos, {"/dev/cu.usbmodem-stm": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.usbmodem-stm"


@pytest.mark.parametrize("kw", [
    {"vid": 0x1209, "pid": 0xEC10},                                    # firmware 4.0.5+, no strings
    {"vid": 0x0483, "pid": 0x5740, "product": "BenchPod"},             # firmware 3.9.0 to 4.0.4
    {"vid": 0x0483, "pid": 0x5740, "manufacturer": " embeddedci "},    # manufacturer only
    {"vid": 0x1209, "pid": 0x0001, "manufacturer": "EmbeddedCI", "product": "BenchPod"},
    {"vid": 0x0483, "pid": 0x5740, "product": "bench-pod STM32H563 CDC"},
])
def test_pod_ports_are_found(monkeypatch, kw):
    opened = install(monkeypatch, [PortInfo("/dev/cu.pod", **kw)], {"/dev/cu.pod": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.pod"
    assert opened == ["/dev/cu.pod"]


def test_product_in_description_only_counts(monkeypatch):
    # Some platforms leave `product` empty and put the string in `description`.
    info = PortInfo("/dev/cu.pod", vid=0x0483, pid=0x5740)
    info.description = "BenchPod console"
    install(monkeypatch, [info], {"/dev/cu.pod": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.pod"


def test_other_instruments_are_never_opened(monkeypatch):
    # A tinySA (0483:5740), an ST-Link, a customer board, other pid.codes / RPi devices and a port
    # without USB info must not get a single byte; only the pod on 1209:EC10 is opened.
    infos = [
        PortInfo("/dev/cu.tinysa", vid=0x0483, pid=0x5740, product="tinySA4", manufacturer="tinySA"),
        PortInfo("/dev/cu.stlink", vid=0x0483, pid=0x374B, product="STM32 STLink"),
        PortInfo("/dev/cu.stvcp", vid=0x0483, pid=0x5740, product="STM32 Virtual ComPort"),
        PortInfo("/dev/cu.pidcodes", vid=0x1209, pid=0x0001, product="Some gadget"),
        PortInfo("/dev/cu.rpi", vid=0x2E8A, pid=0x000A, product="Pico"),
        PortInfo("/dev/cu.bluetooth"),
        PortInfo("/dev/cu.pod", vid=0x1209, pid=0xEC10),
    ]
    replies = {i.device: POD_STATUS for i in infos}
    opened = install(monkeypatch, infos, replies)
    assert serial_mod._candidate_ports() == ["/dev/cu.pod"]
    assert serial_mod.autodetect_port() == "/dev/cu.pod"
    assert opened == ["/dev/cu.pod"]


def test_port_without_usb_info_is_not_opened(monkeypatch):
    opened = install(monkeypatch, [PortInfo("/dev/cu.mystery")], {"/dev/cu.mystery": POD_STATUS})
    with pytest.raises(TransportError, match="explicit device path"):
        serial_mod.autodetect_port()
    assert opened == []


def test_macos_twin_of_an_allowed_port_is_allowed(monkeypatch):
    infos = [PortInfo("/dev/cu.usbmodemA", vid=0x1209, pid=0xEC10), PortInfo("/dev/tty.usbmodemA"),
             PortInfo("/dev/tty.usbmodemT", vid=0x0483, pid=0x5740, product="tinySA4"),
             PortInfo("/dev/cu.usbmodemT", vid=0x0483, pid=0x5740, product="tinySA4")]
    install(monkeypatch, infos, {})
    assert serial_mod._candidate_ports() == ["/dev/cu.usbmodemA", "/dev/tty.usbmodemA"]


def test_explicit_device_is_used_without_detection(monkeypatch):
    # An explicit path never goes through the port listing (no filter).
    def boom():
        raise AssertionError("must not enumerate ports for an explicit device")
    monkeypatch.setattr(serial_mod, "_candidate_ports_vid", boom)
    port = FakePort("/dev/cu.tinysa-or-anything", "", "identity abc123 key")
    t = serial_mod.SerialTransport("/dev/cu.tinysa-or-anything", port=port, timeout=1)
    assert t.identity() == "abc123"


def test_pod_vendor_ids_alone_are_not_enough(monkeypatch):
    infos = [PortInfo("/dev/cu.aaa-other", vid=0x1209, pid=0x0001),
             PortInfo("/dev/cu.zzz-pod", vid=0x1209, pid=0xEC10)]
    opened = install(monkeypatch, infos, {"/dev/cu.aaa-other": "not a pod\n> ", "/dev/cu.zzz-pod": POD_STATUS})
    assert serial_mod.autodetect_port() == "/dev/cu.zzz-pod"
    assert opened == ["/dev/cu.zzz-pod"]


def test_unopenable_port_does_not_stop_the_search(monkeypatch):
    infos = [PortInfo("/dev/cu.busy", vid=0x0483, product="BenchPod"),
             PortInfo("/dev/cu.pod", vid=0x0483, product="BenchPod")]
    install(monkeypatch, infos, {"/dev/cu.pod": POD_STATUS})  # busy port absent -> OSError
    assert serial_mod.autodetect_port() == "/dev/cu.pod"


def test_no_ports_at_all_mentions_the_cable(monkeypatch):
    install(monkeypatch, [], {})
    with pytest.raises(TransportError) as exc:
        serial_mod.autodetect_port()
    assert "1209:EC10" in str(exc.value)
    assert "charge-only" in str(exc.value)
    assert "explicit device path" in str(exc.value)


def test_pod_port_that_is_not_a_pod_points_at_firmware(monkeypatch):
    infos = [PortInfo("/dev/cu.other", vid=0x1209, pid=0xEC10)]
    install(monkeypatch, infos, {"/dev/cu.other": "not a pod\n> "})
    with pytest.raises(TransportError) as exc:
        serial_mod.autodetect_port()
    msg = str(exc.value)
    assert "/dev/cu.other" in msg          # says what it looked at
    assert "1209:EC10" in msg and "explicit device path" in msg
    assert "flash-self" in msg             # and the most likely fix


# -- several pods: usb:<name> ----------------------------------------------------------------

def _two_pods(monkeypatch, *, same=False, tty_vid=None):
    infos = [
        PortInfo("/dev/cu.usbmodemA", vid=0x1209, pid=0xEC10),
        PortInfo("/dev/tty.usbmodemA", vid=tty_vid),
        PortInfo("/dev/cu.usbmodemB", vid=0x1209, pid=0xEC10),
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
    infos = [PortInfo("/dev/cu.a", vid=0x0483, product="BenchPod"), PortInfo("/dev/cu.b", vid=0x0483, product="BenchPod")]
    install(monkeypatch, infos, {"/dev/cu.a": POD_STATUS, "/dev/cu.b": POD_STATUS})
    with pytest.raises(ConnectionConfigError, match="a pod with no identity on /dev/cu.a"):
        serial_mod.autodetect_port()


def test_own_usb_id_ranks_after_string_matches(monkeypatch):
    # Candidates are only pod ports; those naming the pod in their strings come first.
    infos = [
        PortInfo("/dev/cu.a-unknown", vid=0x1234),
        PortInfo("/dev/cu.b-st", vid=0x0483, pid=0x5740, product="tinySA4"),
        PortInfo("/dev/cu.c-own-id", vid=0x1209, pid=0xEC10),
        PortInfo("/dev/cu.d-old", vid=0x0483, pid=0x5740, manufacturer="EmbeddedCI", product="BenchPod"),
    ]
    install(monkeypatch, infos, {"/dev/cu.c-own-id": POD_STATUS})
    assert serial_mod._candidate_ports() == ["/dev/cu.d-old", "/dev/cu.c-own-id"]


def test_pod_is_refound_by_name_when_the_usb_id_changes_across_an_update(monkeypatch):
    # Firmware before the id switch: 0483:5740. After the update the same pod (same identity)
    # re-enumerates as 1209:EC10 on another device node; `usb:<name>` must still find it.
    before = [PortInfo("/dev/cu.usbmodem1", vid=0x0483, pid=0x5740, manufacturer="EmbeddedCI", product="BenchPod")]
    install(monkeypatch, before, {"/dev/cu.usbmodem1": POD_STATUS}, {"/dev/cu.usbmodem1": "identity abc123 key"})
    assert serial_mod.autodetect_port("benchpod-abc123") == "/dev/cu.usbmodem1"
    after = [PortInfo("/dev/cu.usbmodem2", vid=0x1209, pid=0xEC10),
             PortInfo("/dev/cu.other", vid=0x0483, pid=0x5740, product="STM32 Virtual COM Port")]
    install(monkeypatch, after, {"/dev/cu.usbmodem2": POD_STATUS, "/dev/cu.other": "hi\n> "},
            {"/dev/cu.usbmodem2": "identity abc123 key"})
    assert serial_mod.autodetect_port("benchpod-abc123") == "/dev/cu.usbmodem2"
