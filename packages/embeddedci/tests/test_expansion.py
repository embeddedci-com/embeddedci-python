"""GPIO expanders (bp.expansion and EXT pins in the GPIO calls), against a fake pod whose I2C master
talks to register models of the chips."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import (
    EXPANSION_CHIPS,
    BenchPod,
    ExpansionError,
    ExtPin,
    FirmwareError,
    Trigger,
    Wiring,
    expansion_chip,
)
from embeddedci.benchpod.wiring import Signal


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4)) if t else b""


# -- chip models ---------------------------------------------------------------------------------

class Tca9555:
    """Input 0-1, output 2-3, polarity 4-5, config 6-7; the pointer toggles within a pair."""

    def __init__(self) -> None:
        self.regs = [0, 0, 0xFF, 0xFF, 0x0F, 0, 0x00, 0xFF]  # dirty: polarity set, port 0 outputs
        self.ptr = 0
        self.pins_in = 0  # what the outside world drives on input pins

    def _port(self, p: int) -> int:
        cfg, out = self.regs[6 + p], self.regs[2 + p]
        level = (out & ~cfg) | ((self.pins_in >> (8 * p)) & cfg)
        return (level ^ self.regs[4 + p]) & 0xFF

    def write(self, data: bytes) -> None:
        self.ptr = data[0]
        for b in data[1:]:
            if self.ptr >= 2:
                self.regs[self.ptr] = b
            self.ptr ^= 1

    def read(self, n: int) -> bytes:
        out = []
        for _ in range(n):
            out.append(self._port(self.ptr) if self.ptr < 2 else self.regs[self.ptr])
            self.ptr ^= 1
        return bytes(out)

    @property
    def config(self) -> int:
        return self.regs[6] | self.regs[7] << 8

    @property
    def output(self) -> int:
        return self.regs[2] | self.regs[3] << 8


class Mcp23017:
    """BANK=0 register map, sequential addressing; starts in BANK=1 to test the reset."""

    def __init__(self) -> None:
        self.regs = bytearray(0x16)
        self.regs[0] = self.regs[1] = 0xFF
        self.bank1 = True
        self.ptr = 0
        self.pins_in = 0

    def write(self, data: bytes) -> None:
        self.ptr = data[0]
        for b in data[1:]:
            if self.bank1:
                assert self.ptr == 0x05 and b & 0x80 == 0, "first write must clear BANK"
                self.bank1 = False
            else:
                self.regs[self.ptr] = b
            self.ptr = (self.ptr + 1) % 0x16

    def read(self, n: int) -> bytes:
        out = []
        for _ in range(n):
            r = self.ptr
            if r in (0x12, 0x13):
                side = r - 0x12
                iodir = self.regs[side]
                v = (self.regs[0x14 + side] & ~iodir) | ((self.pins_in >> (8 * side)) & iodir)
                out.append(v & 0xFF)
            else:
                out.append(self.regs[r])
            self.ptr = (self.ptr + 1) % 0x16
        return bytes(out)

    @property
    def iodir(self) -> int:
        return self.regs[0] | self.regs[1] << 8

    @property
    def olat(self) -> int:
        return self.regs[0x14] | self.regs[0x15] << 8


class ExpPod:
    def __init__(self, chips: Dict[int, Any], *, mv: int = 3300) -> None:
        self.chips = chips
        self.mv = mv
        self.commands: List[dict] = []
        self.session: Optional[dict] = None
        self.pullups: set = set()
        self.gpio: Dict[int, dict] = {}

    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "version": "3.10.4", "gateware": 55,
                "caps": ["la", "la_pins", "gpio_read", "capture_trigger", "spi_master"]}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    @property
    def xfers(self) -> List[dict]:
        return [c for c in self.commands if c["cmd"] == "i2c_xfer"]

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "la_voltage":
            return {"mv": self.mv}
        if cmd == "la" and "pullup" in req:
            (self.pullups.add if req["pullup"] == "on" else self.pullups.discard)(req["la"])
            return {"la": req["la"], "pullup": req["pullup"]}
        if cmd == "i2c_start":
            if self.session:
                raise FirmwareError("i2c busy: send i2c_stop first", cmd=cmd)
            if req["pullups"] == "pod":
                assert {req["sda"], req["scl"]} <= self.pullups, "pod pull-ups must be on first"
            self.session = req
            return {k: req[k] for k in ("sda", "scl", "hz", "pullups")}
        if cmd == "i2c_stop":
            self.session = None
            return "i2c stopped"
        if cmd == "i2c_status":
            return {"active": bool(self.session), **(self.session or {})}
        if cmd in ("i2c_xfer", "i2c_scan") and not self.session:
            raise FirmwareError("no I2C session: send i2c_start first", cmd=cmd)
        if cmd == "i2c_scan":
            return {"found": sorted(self.chips)}
        if cmd == "i2c_xfer":
            chip = self.chips.get(req["addr"])
            if chip is None:
                return {"addr": req["addr"], "ack": False, "nack_at": 0, "rx": ""}
            w = unb64(req.get("write", ""))
            if w:
                chip.write(w)
            rx = chip.read(req["read"]) if req.get("read") else b""
            return {"addr": req["addr"], "ack": True, "rx": b64(rx)}
        if cmd == "gpio":
            if "mode" in req or "level" in req:
                return {"pins": []}
            return {"levels": 0b101}
        if cmd == "la_dir":
            return {"pins": []}
        raise AssertionError(f"unexpected {req}")


def attach(chips=None, *, wiring=None, mv=3300, **kw):
    fake = ExpPod(chips if chips is not None else {0x20: Tca9555()}, mv=mv)
    bp = BenchPod(transport=fake, wiring=wiring or Wiring(i2c_sda=1, i2c_scl=2))
    if "chip" not in kw:
        kw["chip"] = "tca9555"
    st = bp.expansion(kw.pop("chip"), **kw)
    return bp, fake, st


# -- the chip list -------------------------------------------------------------------------------

def test_chip_list_is_the_one_source():
    names = [c.name for c in EXPANSION_CHIPS]
    assert len(names) == len(set(names))
    assert expansion_chip("TCA9535").name == "tca9555"
    assert expansion_chip("mcp23017").pins == 16
    with pytest.raises(ValueError, match="the SDK drives: tca9555"):
        expansion_chip("ds2413")
    for c in EXPANSION_CHIPS:
        assert c.bus in ("i2c", "spi") and c.directions in ("in/out", "quasi", "out", "in")
        assert set(c.voltages) <= {1.8, 3.3} and c.voltages


# -- attach / status / detach --------------------------------------------------------------------

def test_attach_probes_resets_and_names_ext_pins():
    chip = Tca9555()
    bp, fake, st = attach({0x20: chip})
    assert st.attached and st.chip == "tca9555" and st.la == {"sda": 1, "scl": 2}
    assert st.names == [f"EXT{n}" for n in range(1, 17)]
    assert [p.chip_pin for p in st.pins[:2]] + [st.pins[8].chip_pin] == ["P00", "P01", "P10"]
    assert chip.config == 0xFFFF and chip.regs[4:6] == [0, 0]   # all input, polarity cleared
    assert st.pullups == "pod" and fake.pullups == {1, 2}        # 3.3 V on LA1/LA2: pod pull-ups
    assert all(p.mode == "none" for p in st.pins)
    assert [c["cmd"] for c in fake.commands].index("la_voltage") < [
        c["cmd"] for c in fake.commands].index("i2c_start")


def test_status_and_detach():
    bp, fake, _ = attach()
    bp.gpio("EXT1", "output", level=1)
    st = bp.expansion()
    assert st.pins[0].mode == "output" and st.pins[0].level == 1 and st.pins[0].live == 1
    bp.expansion(None)
    assert fake.chips[0x20].config == 0xFFFF                     # every pin back to input
    assert fake.session is None and fake.pullups == set()
    assert bp.expansion().attached is False
    with pytest.raises(ExpansionError, match="no expander is attached"):
        bp.read_gpio("EXT1")


def test_no_chip_at_address_names_what_the_scan_found():
    with pytest.raises(ExpansionError, match=r"0x21.*a scan found 0x20"):
        attach(addr=0x21)


def test_voltage_checks():
    with pytest.raises(ExpansionError, match="pca9555 does not run at the LA voltage 1.8 V"):
        attach(chip="pca9555", mv=1800, pullups="external")
    fake = ExpPod({0x20: Tca9555()}, mv=0)
    with pytest.raises(ExpansionError, match="set the LA voltage first"):
        BenchPod(transport=fake).expansion("tca9555")
    with pytest.raises(ExpansionError, match="pullups='pod' needs the LA voltage at 3.3 V"):
        attach(mv=1800, pullups="pod")
    bp, fake, st = attach(mv=1800)                               # 1.8 V: the breakout's pull-ups
    assert st.pullups == "external" and fake.pullups == set()


def test_argument_validation():
    with pytest.raises(ValueError, match="addresses 0x20..0x27"):
        attach(addr=0x40)
    with pytest.raises(ValueError, match="listed twice"):
        attach(addr=[0x20, 0x20])
    with pytest.raises(ValueError, match="I2C chip: spi, load and count"):
        attach(count=2)
    with pytest.raises(ValueError, match="name the chip"):
        BenchPod(transport=ExpPod({})).expansion(addr=0x20)
    with pytest.raises(ValueError, match="takes no other"):
        BenchPod(transport=ExpPod({})).expansion(None, addr=0x20)


def test_two_chips_one_bus():
    a, b = Tca9555(), Tca9555()
    bp, fake, st = attach({0x20: a, 0x21: b}, addr=[0x20, 0x21])
    assert len(st.pins) == 32 and st.pins[16].addr == 0x21 and st.pins[16].chip_pin == "P00"
    bp.gpio_pins(["EXT1", "EXT17"], "output", level=1)
    assert a.output & 1 and b.output & 1 and a.config & 1 == 0 and b.config & 1 == 0


# -- GPIO calls on EXT pins ----------------------------------------------------------------------

def test_gpio_output_input_and_open_drain():
    chip = Tca9555()
    bp, fake, _ = attach({0x20: chip})
    led = bp.gpio("EXT3", "output")
    assert isinstance(led, ExtPin) and led.la is None and led.name == "EXT3"
    assert chip.config & 0b100 == 0 and chip.output & 0b100 == 0
    led.high()
    assert chip.output & 0b100
    assert led.read() == 1
    button = bp.gpio("EXT10", "input")
    chip.pins_in = 1 << 9
    assert button.read() == 1 and bp.read_gpio("EXT10") == 1
    od = bp.gpio("EXT4", "open_drain")                           # starts released
    assert chip.config & 0b1000
    od.low()
    assert chip.config & 0b1000 == 0 and chip.output & 0b1000 == 0
    od.high()
    assert chip.config & 0b1000
    led.release()
    assert chip.config & 0b100 and bp.expansion().pins[2].mode == "none"


def test_multi_pin_write_is_one_transaction_per_chip():
    chip = Tca9555()
    bp, fake, _ = attach({0x20: chip})
    bp.gpio_pins(["EXT1", "EXT2", "EXT9"], "output")
    before = len(fake.xfers)
    bp.set_gpio(["EXT1", "EXT2", "EXT9"], 1)
    assert len(fake.xfers) == before + 1                         # only the output registers
    assert chip.output & 0x0103 == 0x0103
    bp.set_gpio(["EXT1", "EXT2", "EXT9"], [0, 1, 0])             # per-pin levels
    assert len(fake.xfers) == before + 2
    assert chip.output & 0x0103 == 0x0002


def test_mixed_la_and_ext_write():
    bp, fake, _ = attach()
    bp.gpio_pins([5, "EXT1"], "output")
    bp.set_gpio([5, 6, "EXT1"], [1, 0, 1])
    gpio = [c for c in fake.commands if c["cmd"] == "gpio" and "level" in c and "mode" not in c]
    assert gpio == [{"cmd": "gpio", "la": 5, "level": 1}, {"cmd": "gpio", "la": 6, "level": 0}]
    assert fake.chips[0x20].output & 1
    with pytest.raises(ValueError, match="one level per channel"):
        bp.set_gpio([5, "EXT1"], [1])


def test_level_on_a_pin_that_is_not_an_output():
    bp, fake, _ = attach()
    with pytest.raises(ExpansionError, match="EXT2 is not an output"):
        bp.set_gpio("EXT2", 1)


def test_wait_for_level_polls_the_chip():
    chip = Tca9555()
    bp, fake, _ = attach({0x20: chip})
    bp.gpio("EXT5", "input")
    chip.pins_in = 1 << 4
    assert bp.wait_for_level("EXT5", 1, timeout=0.1)
    assert not bp.wait_for_level("EXT5", 0, timeout=0.02, poll=0.01)


def test_release_all_includes_ext_pins():
    chip = Tca9555()
    bp, fake, _ = attach({0x20: chip})
    bp.gpio_pins(["EXT1", "EXT2"], "output")
    bp.release_gpio()
    assert {"cmd": "gpio", "la": "all", "mode": "off"} in fake.commands
    assert chip.config == 0xFFFF


def test_configure_gpio_returns_ext_states():
    bp, fake, _ = attach()
    states = bp.configure_gpio(["EXT2", 7], "output", level=1)
    assert states[0].pin == "EXT2" and states[0].mode == "output" and states[0].level == 1
    assert states[1].la == 7


def test_names_from_argument_and_wiring():
    bp, fake, st = attach(names={"EXT1": "LED1", "EXT9": "BUTTON"})
    assert st.pins[0].name == "LED1"
    pin = bp.gpio("led1", "output")
    assert isinstance(pin, ExtPin) and pin.name == "LED1" and pin.ext == 1
    assert isinstance(bp.signal("BUTTON"), ExtPin)
    wiring = Wiring.from_dict({"i2c_sda": 3, "i2c_scl": 4, "uart_tx": 6, "uart_rx": 7,
                               "expansion": {"chip": "tca9555", "addr": "0x21",
                                             "names": ["RELAY1", "RELAY2"]}})
    bp2, fake2, st2 = attach({0x21: Tca9555()}, wiring=wiring)
    assert st2.addrs == (0x21,) and st2.la == {"sda": 3, "scl": 4}
    assert [p.name for p in st2.pins[:3]] == ["RELAY1", "RELAY2", None]
    assert wiring.to_dict()["expansion"]["addr"] == "0x21"      # kept as is


def test_names_must_not_clash():
    with pytest.raises(ValueError, match="already name LA channels"):
        attach(names=["READY"], wiring=Wiring(i2c_sda=1, i2c_scl=2, signals=[Signal("READY", 9)]))
    with pytest.raises(ValueError, match="more than one pin"):
        attach(names={"EXT1": "A", "EXT2": "a"})
    with pytest.raises(ValueError, match="not one of EXT1..EXT16"):
        attach(names={"EXT17": "X"})


# -- refusals ------------------------------------------------------------------------------------

def test_fpga_timed_calls_refuse_ext_pins_with_a_reason():
    bp, fake, _ = attach(names={"EXT1": "LED"})
    led = bp.gpio("LED", "output")
    with pytest.raises(ExpansionError, match="pulses are timed by the FPGA"):
        led.pulse(0.001)
    with pytest.raises(ExpansionError, match="EXT1, tca9555 pin P00.*pulses are timed"):
        bp.la_step("LED", steps=1, delay=0.001)
    with pytest.raises(ExpansionError, match="la_dir marks LA channels"):
        bp.la_dir("EXT2", "dut_input")
    with pytest.raises(ExpansionError, match="triggers sample the LA channels only"):
        bp._resolve_trigger(Trigger("EXT1", "rising"))


def test_ext_name_without_an_expander():
    bp = BenchPod(transport=ExpPod({}), wiring=Wiring())
    with pytest.raises(ExpansionError, match="no expander is attached"):
        bp.gpio("EXT1")
    with pytest.raises(ValueError, match="does not exist"):
        attach()[0].gpio("EXT17")


def test_chip_specific_mode_refusals():
    bp, fake, st = attach()
    exp = bp._expansion
    exp.chip = type(exp.chip)(**{**exp.chip.__dict__, "directions": "out"})
    with pytest.raises(ExpansionError, match="outputs only"):
        bp.gpio("EXT1", "input")


# -- the bus -------------------------------------------------------------------------------------

def test_bus_opens_lazily_and_yields_to_open_i2c_on_the_same_pins():
    bp, fake, _ = attach()
    bp.gpio("EXT1", "output")
    starts = [c for c in fake.commands if c["cmd"] == "i2c_start"]
    assert len(starts) == 1                                      # kept open between calls
    with bp.open_i2c() as i2c:                                    # ours closes first, theirs is reused
        assert fake.session is not None
        n = len(fake.commands)
        bp.set_gpio("EXT1", 1)
        assert not any(c["cmd"] == "i2c_start" for c in fake.commands[n:])
        assert i2c.probe(0x20)
    bp.set_gpio("EXT1", 0)                                        # reopened on demand
    assert sum(c["cmd"] == "i2c_start" for c in fake.commands) == 3


def test_user_session_on_other_pins_is_named():
    bp, fake, _ = attach()
    bp.enable_pullup(3, 4)
    with bp.open_i2c(3, 4):
        with pytest.raises(ExpansionError, match="LA3 SDA / LA4 SCL.*one I2C master"):
            bp.read_gpio("EXT1")


def test_flash_and_close_release_the_bus():
    bp, fake, _ = attach()
    bp.gpio("EXT1", "output")
    assert fake.session is not None
    bp._yield_expansion_bus()                                     # what flash/open_spi call first
    assert fake.session is None
    bp.read_gpio("EXT1")
    assert fake.session is not None
    bp.close()
    assert fake.session is None


# -- MCP23017 ------------------------------------------------------------------------------------

def test_mcp23017_reset_and_drive():
    chip = Mcp23017()
    chip.regs[0x0C] = 0xFF                                        # stale pull-ups
    bp, fake, st = attach({0x20: chip}, chip="mcp23017")
    assert not chip.bank1 and chip.regs[0x0A] == 0 and chip.regs[0x0C] == 0
    assert [p.chip_pin for p in st.pins[7:9]] == ["GPA7", "GPB0"]
    bp.gpio_pins(["EXT1", "EXT16"], "output", level=1)
    assert chip.iodir == 0x7FFE and chip.olat == 0x8001
    before = len(fake.xfers)
    bp.set_gpio(["EXT1", "EXT16"], 0)
    assert len(fake.xfers) == before + 1 and chip.olat == 0
    bp.gpio("EXT9", "input")
    chip.pins_in = 1 << 8
    assert bp.read_gpio("EXT9") == 1
