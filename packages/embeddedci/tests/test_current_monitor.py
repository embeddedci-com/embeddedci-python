"""External INA current monitors (bp.expansion("ina226", ...) and monitor= on the power calls), against a fake pod whose I2C master talks to
register models of the chips."""

from __future__ import annotations

import time
import types
from typing import Dict, Optional

import pytest

from embeddedci.benchpod import EXPANSION_CHIPS, BenchPod, ExpansionError, Wiring, expansion_chip

from test_expansion import ExpPod, Tca9555


class Ina:
    """A register file: ``widths`` gives each register's size in bytes; reads continue to the next
    register (auto-increment), writes take one 16-bit value."""

    def __init__(self, widths: Dict[int, int], init: Dict[int, int]) -> None:
        self.widths = widths
        self.regs = dict(init)
        self.ptr = 0
        self.writes = []

    def write(self, data: bytes) -> None:
        self.ptr = data[0]
        if len(data) == 3:
            self.regs[self.ptr] = int.from_bytes(data[1:], "big")
            self.writes.append((self.ptr, self.regs[self.ptr]))

    def read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            w = self.widths.get(self.ptr, 2)
            out += self.regs.get(self.ptr, 0).to_bytes(w, "big")
            self.ptr += 1
        return out[:n]


def s16(v: int) -> int:
    return v & 0xFFFF


def ina226(shunt=0, bus=0) -> Ina:
    return Ina({}, {0xFE: 0x5449, 0xFF: 0x2260, 1: s16(shunt), 2: bus, 0: 0x4127})


def ina219(shunt=0, bus_raw=0) -> Ina:
    return Ina({}, {0: 0x399F, 1: s16(shunt), 2: bus_raw})


def ina228(shunt24=0, bus24=0) -> Ina:
    return Ina({4: 3, 5: 3}, {0x3E: 0x5449, 0x3F: 0x2281, 4: shunt24 & 0xFFFFFF, 5: bus24})


def ina238(shunt=0, bus=0) -> Ina:
    return Ina({}, {0x3E: 0x5449, 0x3F: 0x2381, 4: s16(shunt), 5: bus})


def ina260(cur=0, bus=0) -> Ina:
    return Ina({}, {0xFE: 0x5449, 0xFF: 0x2270, 1: s16(cur), 2: bus, 0: 0x6127})


def ina3221(regs: dict) -> Ina:
    return Ina({}, {0xFE: 0x5449, 0xFF: 0x3220, 0: 0x7127, **regs})


def attach(chips: Optional[dict] = None, chip="ina226", *, mv=3300, **kw):
    fake = ExpPod(chips if chips is not None else {0x40: ina226()}, mv=mv, features_rev=0)
    bp = BenchPod(transport=fake, wiring=Wiring(i2c_sda=1, i2c_scl=2))
    kw.setdefault("shunt_ohm", 10.0)
    bp.expansion(chip, **kw)
    return bp, fake, bp._monitors[-1]


def test_chips_are_in_the_expansion_table():
    inas = [c for c in EXPANSION_CHIPS if c.kind == "current"]
    assert [c.name for c in inas] == ["ina226", "ina228", "ina238", "ina219", "ina260", "ina3221"]
    assert expansion_chip("INA226").addrs[0] == 0x40
    assert all(1.8 not in c.voltages and c.pins == 0 for c in inas)


def pod_with(chips: dict, mv=3300):
    fake = ExpPod(chips, mv=mv, features_rev=0)
    return BenchPod(transport=fake, wiring=Wiring(i2c_sda=1, i2c_scl=2)), fake


def test_ina226_attach_configures_and_reads():
    chip = ina226(shunt=4, bus=2640)    # 4 * 2.5 uV = 10 uV -> 1 uA with 10 ohm; 2640 * 1.25 mV = 3.3 V
    bp, fake, mon = attach({0x40: chip})
    assert chip.writes[0] == (0, 0x8000)                      # reset
    assert chip.writes[-1] == (0, 0x4000 | 2 << 9 | 4 << 6 | 4 << 3 | 7)  # avg 16, 1.1 ms, continuous
    assert fake.pullups == {1, 2}
    r = mon.read()
    assert r.current == pytest.approx(1e-6) and r.bus_voltage == pytest.approx(3.3)
    assert r.power == pytest.approx(3.3e-6) and not r.overflow
    chip.regs[1] = s16(-8)
    assert mon.read().current == pytest.approx(-2e-6)         # signed
    st = bp.expansion()                                       # the status lists it with a live reading
    assert st.attached and st.chip is None and st.pins == () and st.la == {"sda": 1, "scl": 2}
    [m] = st.monitors.values()
    assert m.name == "ina226@0x40" and m.shunt_ohm == 10 and m.averaging == 16
    assert m.reading.current == pytest.approx(-2e-6) and m.error is None


def test_configuration_is_an_attribute_of_expansion():
    chip = ina226()
    bp, fake, mon = attach({0x40: chip}, averaging=64, conversion_time=1e-3)
    assert chip.regs[0] == 0x4000 | 3 << 9 | 4 << 6 | 4 << 3 | 7 and mon.averaging == 64
    assert mon.cycle == pytest.approx(2 * 1.1e-3 * 64)
    with pytest.raises(ValueError, match="averaging for the ina226"):
        bp.expansion("ina226", shunt_ohm=10, averaging=3)
    assert len(bp._monitors) == 1                              # the failed call changed nothing
    with pytest.raises(ValueError, match="for the INA current monitors"):
        pod_with({0x20: Tca9555()})[0].expansion("tca9555", shunt_ohm=10)


def test_wrong_chip_and_missing_chip_and_voltage():
    with pytest.raises(ExpansionError, match="reads 0x2260, not the ina260's 0x2270"):
        attach({0x40: ina226()}, "ina260", shunt_ohm=None)
    with pytest.raises(ExpansionError, match="no ina226 answers at 0x41 .*0x40"):
        attach({0x40: ina226()}, addr=0x41)
    with pytest.raises(ExpansionError, match="does not run at the LA voltage 1.8 V"):
        attach(mv=1800)
    with pytest.raises(ValueError, match="shunt_ohm"):
        attach(shunt_ohm=0)
    with pytest.raises(ValueError, match="shunt_ohm is the shunt resistor"):
        pod_with({0x40: ina226()})[0].expansion("ina226")
    with pytest.raises(ValueError, match="addresses 0x40"):
        attach(addr=0x50)


def test_failed_attach_cleans_up():
    bp, fake = pod_with({})
    with pytest.raises(ExpansionError):
        bp.expansion("ina226", shunt_ohm=10)
    assert fake.session is None and not fake.pullups and bp._monitors == []
    assert not bp.expansion().attached


def test_ina219():
    # 100 uV = 10 steps of 10 uV; bus 3.3 V: (825 << 3) * 4 mV with OVF set
    chip = ina219(shunt=10, bus_raw=825 << 3 | 1)
    bp, fake, mon = attach({0x40: chip}, "ina219", max_current=0.001)
    r = mon.read()
    assert r.shunt_voltage == pytest.approx(100e-6) and r.current == pytest.approx(10e-6)
    assert r.bus_voltage == pytest.approx(3.3) and r.overflow
    # max_current 1 mA x 10 ohm = 10 mV -> PGA /1 (+-40 mV); 16 samples averaged = field 0b1100
    assert chip.regs[0] >> 11 & 3 == 0 and chip.regs[0] >> 7 & 0xF == 0b1100
    bp.expansion("ina219", shunt_ohm=10, averaging=1, conversion_time=150e-6)
    assert chip.regs[0] >> 7 & 0xF == 1 and bp._monitors[0].averaging == 1   # 10 bit, 148 us
    with pytest.raises(ValueError, match="averaging for the ina219"):
        bp.expansion("ina219", shunt_ohm=10, averaging=3)


def test_ina228_and_ina238_ranges():
    # INA228 fine range: 78.125 nV per step: 128 steps = 10 uV; value sits in bits 23:4
    chip = ina228(shunt24=128 << 4, bus24=(16896 << 4))   # 16896 * 195.3125 uV = 3.3 V
    bp, fake, mon = attach({0x40: chip}, "ina228", max_current=1e-3)
    assert chip.regs[0] == 0x10                            # ADCRANGE = 1 (+-40.96 mV)
    r = mon.read()
    assert r.current == pytest.approx(1e-6) and r.bus_voltage == pytest.approx(3.3)
    bp.expansion("ina228", shunt_ohm=10, max_current=0.0105)  # 105 mV: wide range, 312.5 nV per step
    assert chip.regs[0] == 0 and bp._monitors[0].read().current == pytest.approx(128 * 312.5e-9 / 10)
    chip.regs[4] = (-16 << 4) & 0xFFFFFF
    assert bp._monitors[0].read().current == pytest.approx(-16 * 312.5e-9 / 10)

    c238 = ina238(shunt=8, bus=1056)                       # fine: 8 * 1.25 uV = 10 uV; 1056 * 3.125 mV
    bp, fake, mon = attach({0x40: c238}, "ina238", max_current=1e-3)
    r = mon.read()
    assert c238.regs[0] == 0x10 and r.current == pytest.approx(1e-6) and r.bus_voltage == pytest.approx(3.3)
    assert c238.regs[1] >> 12 == 0xB and c238.regs[1] & 7 == 2   # continuous shunt+bus, avg 16
    with pytest.raises(ValueError, match="takes at most 163.84 mV"):
        attach({0x40: ina238()}, "ina238", max_current=1.0)


def test_ina260_internal_shunt():
    chip = ina260(cur=8, bus=2640)
    bp, fake, mon = attach({0x40: chip}, "ina260", shunt_ohm=None)
    r = mon.read()
    assert r.current == pytest.approx(0.01) and r.shunt_voltage == pytest.approx(0.01 * 0.002)
    with pytest.raises(ValueError, match="own 2 mohm"):
        attach({0x40: ina260()}, "ina260", shunt_ohm=10)


def test_ina3221_channels_share_the_chip():
    # CH2: shunt 40 uV steps, data in bits 15:3; bus 8 mV steps
    chip = ina3221({3: 5 << 3, 4: 412 << 3})
    bp, fake = pod_with({0x40: chip})
    bp.expansion("ina3221", shunt_ohm=10, channel=2, name="a")
    bp.expansion("ina3221", shunt_ohm=1, channel=3, name="b")
    assert [m.name for m in bp._monitors] == ["a", "b"] and bp._monitors[1].shunt_ohm == 1
    r = bp._monitors[0].read()
    assert r.shunt_voltage == pytest.approx(200e-6) and r.current == pytest.approx(20e-6)
    assert r.bus_voltage == pytest.approx(3.296)
    assert len([w for w in chip.writes if w == (0, 0x8000)]) == 1   # the second channel did not reset
    with pytest.raises(ValueError, match="channel is 1..3"):
        bp.expansion("ina3221", shunt_ohm=10, channel=4)
    bp.expansion("ina3221", shunt_ohm=100, channel=2)                # same address and channel: replaced
    assert [m.name for m in bp._monitors] == ["b", "ina3221@0x40.2"]


def test_one_chip_per_call_replace_and_detach_one():
    a, b = ina226(shunt=4), ina226(shunt=40)
    bp, fake = pod_with({0x40: a, 0x41: b})
    for bad in ({"addr": [0x40, 0x41]}, {"shunt_ohm": [10, 10]}):
        with pytest.raises(ValueError, match="one chip per expansion"):
            bp.expansion("ina226", **{"shunt_ohm": 10, **bad})
    bp.expansion("ina226", addr=0x40, shunt_ohm=10)
    bp.expansion("ina226", addr=0x41, shunt_ohm=10, name="dut")
    assert [m.name for m in bp._monitors] == ["ina226@0x40", "dut"]
    st = bp.expansion()
    assert [round(m.reading.current * 1e6, 3) for m in st.monitors.values()] == [1.0, 10.0]
    assert sum(c["cmd"] == "i2c_start" for c in fake.commands) == 1
    with pytest.raises(ExpansionError, match="same two pins"):
        bp.expansion("ina226", addr=0x42, shunt_ohm=10, sda=3, scl=4)
    bp.expansion("ina226", addr=0x40, shunt_ohm=20, averaging=64)        # same address: reconfigured
    assert len(bp._monitors) == 2 and bp._monitor("ina226@0x40").shunt_ohm == 20 and a.regs[0] >> 9 & 7 == 3
    bp.expansion(None, name="dut")                                       # detach one, by name
    assert [m.name for m in bp._monitors] == ["ina226@0x40"] and fake.session is not None
    with pytest.raises(ValueError, match="no current monitor at addr=65"):
        bp.expansion(None, addr=0x41)
    with pytest.raises(ValueError, match="detaches everything"):
        bp.expansion(None, sda=1)
    bp.expansion(None, addr=0x40)                                        # detach one, by address
    assert bp._monitors == [] and fake.session is None and not fake.pullups


def test_expansion_none_detaches_everything():
    bp, fake = pod_with({0x40: ina226(), 0x41: ina226()})
    bp.expansion("ina226", addr=0x40, shunt_ohm=10)
    bp.expansion("ina226", addr=0x41, shunt_ohm=10)
    bp.expansion(None)
    assert fake.session is None and not fake.pullups and not bp.expansion().attached


def test_monitor_selection():
    bp, fake = pod_with({0x40: ina226(), 0x41: ina226()})
    with pytest.raises(ExpansionError, match="no external current monitor"):
        bp.measure_power(0.1, monitor=True)
    bp.expansion("ina226", addr=0x40, shunt_ohm=10)
    assert bp._monitor(True).addr == 0x40 and bp._monitor(None).addr == 0x40
    with pytest.raises(ValueError, match="no current monitor named 'ext'"):    # no "ext" alias
        bp._monitor("ext")
    bp.expansion("ina226", addr=0x41, shunt_ohm=10)
    with pytest.raises(ValueError, match="several current monitors"):
        bp._monitor(True)
    assert bp._monitor("ina226@0x41").addr == 0x41
    with pytest.raises(ValueError, match="no current monitor named 'x'"):
        bp._monitor("x")
    with pytest.raises(ValueError, match="name 'ext' is not usable|not usable"):
        bp.expansion("ina226", addr=0x42, shunt_ohm=10, name="ext")


def test_measure_power_monitor_and_profile_session():
    chip = ina226(shunt=4, bus=2640)
    bp, fake, mon = attach({0x40: chip}, averaging=1, conversion_time=140e-6)  # cycle 280 us: reads every 5 ms
    now = [0.0]
    seq = iter([8, 12, 8, 4])

    def sleep(dt: float) -> None:
        now[0] += dt
        chip.regs[1] = next(seq)

    mon.sleep, mon.clock = sleep, lambda: now[0]
    prof = bp.measure_power(0.02, monitor=True, keep_samples=2)
    assert prof.supply is None and prof.monitor == "ina226@0x40" and prof.n == 4 and prof.duration == pytest.approx(0.015)
    assert prof.min_current == pytest.approx(1e-6) and prof.peak_current == pytest.approx(3e-6)
    assert prof.avg_voltage == pytest.approx(3.3) and len(prof.samples) == 2
    assert prof.charge == pytest.approx((1.5e-6 + 2.5e-6 + 2.5e-6) * 0.005)
    assert prof.avg_current == pytest.approx(prof.charge / prof.duration)
    with pytest.raises(ValueError, match=r"monitor= and supply= \(or efuse=\) are exclusive"):
        bp.measure_power(0.1, monitor=True, efuse=1)
    with pytest.raises(ValueError, match=r"monitor= and supply= \(or efuse=\) are exclusive"):
        bp.measure_power(0.1, monitor=True, supply="5v")

    chip.regs[1] = 4                                           # a profile around a block of code
    with bp.power_profile(monitor=True) as p:
        deadline = time.time() + 2
        while p.status()["n"] < 3 and time.time() < deadline:
            time.sleep(0.005)
    res = p.result
    assert res.supply is None and res.monitor == "ina226@0x40" and res.n >= 3 and res.avg_voltage == pytest.approx(3.3)
    assert res.min_current == pytest.approx(1e-6) and not res.truncated
    with pytest.raises(ValueError, match=r"monitor= and supply= \(or efuse=\) are exclusive"):
        bp.power_profile(monitor=True, efuse=2)


def test_power_status_lists_the_monitors():
    chip = ina226(shunt=4, bus=2640)
    bp, fake, mon = attach({0x40: chip})
    orig = fake.command
    fake.command = lambda req: ({"internal": {"ok": True, "bus_mv": 5000, "current_ua": 1000},
                                 "external": {"ok": False}} if req["cmd"] == "power_status" else orig(req))
    ps = bp.power_status()
    assert ps.supplies["5v"].current == pytest.approx(1e-3)
    assert ps.monitors["ina226@0x40"].ok and ps.monitors["ina226@0x40"].current == pytest.approx(1e-6)
    chip.regs.pop(1)
    fake.chips = {}                                            # the chip stops answering
    ps = bp.power_status()
    assert ps.monitors["ina226@0x40"].ok is False


def test_shares_bus_with_gpio_expander_and_releases_for_flash_paths():
    chip = ina226(shunt=4)
    bp, fake = pod_with({0x40: chip, 0x20: Tca9555()})
    bp.expansion("tca9555", addr=0x20)
    bp.expansion("ina226", shunt_ohm=10)                       # beside the GPIO expander, not instead
    mon = bp._monitors[0]
    assert sum(c["cmd"] == "i2c_start" for c in fake.commands) == 1
    st = bp.expansion()
    assert st.chip == "tca9555" and len(st.pins) == 16 and len(st.monitors) == 1
    bp.read_gpio("EXT1")
    assert mon.read().current == pytest.approx(1e-6)
    bp._yield_expansion_bus()                                  # what flash/open_spi do first
    assert fake.session is None
    assert mon.read().current == pytest.approx(1e-6)           # the next read reopens it
    assert sum(c["cmd"] == "i2c_start" for c in fake.commands) == 2
    bp.expansion("tca9555", addr=0x20)                         # re-attaching the expander keeps the monitor
    assert len(bp._monitors) == 1
    bp.expansion(None)
    assert not fake.pullups and fake.session is None


# -- the wiring profile's monitors -------------------------------------------------------------------

def profile_pod(chips: dict, monitors, *, status_mv=3300, **wiring):
    fake = ExpPod(chips, mv=status_mv, features_rev=0)
    w = Wiring(i2c_sda=13, i2c_scl=14, la_mv=3300, monitors=monitors, **wiring)
    return BenchPod(transport=fake, wiring=w), fake


def test_wiring_monitors_validate_and_round_trip():
    spec = {"chip": "ina226", "addr": 64, "shunt_ohm": 10.0, "name": "dut", "averaging": 64,
            "conversion_time": 0.0011, "max_current": 0.001}
    w = Wiring(i2c_sda=13, i2c_scl=14, monitors=[spec])
    assert w.to_dict()["monitors"] == [spec] and Wiring.from_dict(w.to_dict()) == w
    assert "monitors" not in Wiring().to_dict()
    for bad, text in [
        ({"chip": "tca9555", "addr": 32, "shunt_ohm": 1}, "monitors[0].chip must be one of ina226"),
        ({"chip": "ina226", "addr": 0x20, "shunt_ohm": 1}, "monitors[0].addr must be an integer from 64 to 79"),
        ({"chip": "ina226", "addr": 64}, "shunt_ohm is required"),
        ({"chip": "ina260", "addr": 64, "shunt_ohm": 10}, "has its own 2 mohm shunt"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1, "channel": 2}, "channel is 1..1"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1, "name": "ext"}, "name must start with a letter"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1, "averaging": 3}, "averaging is one of 1, 4, 16"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1, "sda": 3, "scl": 3}, "sda and scl must be different"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1000, "max_current": 1}, "takes at most 81.92 mV"),
        ({"chip": "ina226", "addr": 64, "shunt_ohm": 1, "bogus": 1}, "unknown key(s) bogus"),
    ]:
        with pytest.raises(ValueError, match=text.replace("[", r"\[").replace("]", r"\]").replace("(", r"\(").replace(")", r"\)")):
            Wiring(monitors=[bad])
    ok = {"chip": "ina226", "addr": 64, "shunt_ohm": 10}
    with pytest.raises(ValueError, match="same address"):
        Wiring(monitors=[ok, dict(ok, name="x")])
    with pytest.raises(ValueError, match="same name"):
        Wiring(monitors=[ok, {"chip": "ina226", "addr": 65, "shunt_ohm": 10, "name": "INA226@0X40"}])
    with pytest.raises(ValueError, match="same two I2C pins"):
        Wiring(i2c_sda=1, i2c_scl=2, monitors=[ok, {"chip": "ina226", "addr": 65, "shunt_ohm": 10, "sda": 3, "scl": 4}])
    with pytest.raises(ValueError, match="at most 8"):
        Wiring(monitors=[{"chip": "ina226", "addr": 64 + i, "shunt_ohm": 10} for i in range(9)])


def test_connect_attaches_the_profiles_monitors():
    chip = ina226(shunt=4, bus=2640)
    bp, fake = profile_pod({0x40: chip}, [{"chip": "ina226", "addr": 64, "shunt_ohm": 10, "name": "dut",
                                           "averaging": 64}])
    assert bp.monitors_status == "attached dut" and [m.name for m in bp._monitors] == ["dut"]
    assert bp._monitors[0].from_profile and bp._monitors[0].averaging == 64
    assert bp._monitors[0].sda == 13 and fake.pullups == set()       # LA13: no pod pull-ups, external
    assert bp.measure_power(0.01, monitor="dut").avg_current == pytest.approx(1e-6)


def test_a_monitor_that_fails_to_attach_is_said_when_used():
    bp, fake = profile_pod({}, [{"chip": "ina226", "addr": 64, "shunt_ohm": 10}])    # nothing answers
    assert bp.monitors_status.startswith("not attached: ina226@0x40: no ina226 answers at 0x40")
    with pytest.raises(ExpansionError, match="the profile's monitors: not attached: ina226@0x40"):
        bp.measure_power(0.1, monitor=True)


def test_a_new_profile_replaces_the_profile_monitors_but_not_manual_ones():
    chip = ina226(shunt=4)
    bp, fake = profile_pod({0x40: chip, 0x41: ina226(), 0x42: ina226()},
                           [{"chip": "ina226", "addr": 64, "shunt_ohm": 10}])
    bp.expansion("ina226", addr=0x42, shunt_ohm=5, sda=13, scl=14)         # a manual one
    bp.wiring = Wiring(i2c_sda=13, i2c_scl=14, la_mv=3300,
                       monitors=[{"chip": "ina226", "addr": 65, "shunt_ohm": 10}])
    assert sorted(m.addr for m in bp._monitors) == [0x41, 0x42]
    bp.wiring = Wiring(i2c_sda=13, i2c_scl=14, la_mv=3300)
    assert [m.addr for m in bp._monitors] == [0x42]


def test_save_wiring_stores_what_is_attached():
    bp, fake = profile_pod({0x40: ina226(), 0x41: ina226()}, [])
    bp.expansion("ina226", addr=0x40, shunt_ohm=10, name="dut", sda=13, scl=14)
    bp.expansion("ina226", addr=0x41, shunt_ohm=2.5, averaging=16, sda=13, scl=14)
    stored = {}
    bp._pod_wiring_list = lambda: None                        # the cloud-device path: one profile
    bp._device_name = "bench"
    bp._require_server_api = lambda: types.SimpleNamespace(
        resolve_device_id=lambda n: 1, put_wiring=lambda dev, d: stored.update(d))
    saved = bp.save_wiring()
    assert [m.to_dict() for m in saved.monitors] == [
        {"chip": "ina226", "addr": 64, "shunt_ohm": 10.0, "name": "dut", "sda": 13, "scl": 14},
        {"chip": "ina226", "addr": 65, "shunt_ohm": 2.5, "sda": 13, "scl": 14, "averaging": 16}]
    assert stored["monitors"] == [m.to_dict() for m in saved.monitors]
    bp.expansion(None)
    assert bp.save_wiring().monitors == ()                    # detached: the profile has none
