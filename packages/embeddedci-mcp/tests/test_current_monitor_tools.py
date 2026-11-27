"""INA current monitors through expansion, measure_power, power_status and power_profile_*, against
the fake pod's INA226 at 0x40."""

from __future__ import annotations

import time

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import call


def test_attach_read_profile_and_detach(connected):
    r = call("expansion", chip="ina226", shunt_ohm=10.0, sda=1, scl=2, averaging=16)
    assert r["attached"] and r["pins"] is None and r["la"] == {"sda": 1, "scl": 2}
    [mon] = r["monitors"]
    assert mon["name"] == "ina226@0x40" and mon["addr"] == 0x40 and mon["averaging"] == 16
    assert mon["current_a"] == pytest.approx(1e-6) and mon["bus_voltage_v"] == pytest.approx(3.3)
    assert call("expansion")["monitors"][0]["current_a"] == pytest.approx(1e-6)

    ps = call("power_status")
    assert ps["monitors"]["ina226@0x40"]["monitor_ok"] is True
    assert ps["monitors"]["ina226@0x40"]["current"] == pytest.approx(1e-6)

    prof = call("measure_power", duration_s=0.05, monitor=True)
    assert prof["efuse"] == 0 and prof["avg_current"] == pytest.approx(1e-6, abs=1e-7)
    assert prof["avg_voltage"] == pytest.approx(3.3)

    started = call("power_profile_start", monitor="ina226@0x40")
    assert started["efuse"] == 0
    time.sleep(0.03)
    stopped = call("power_profile_stop")
    assert stopped["efuse"] == 0 and stopped["n"] >= 1 and stopped["avg_current"] == pytest.approx(1e-6, abs=1e-7)

    assert call("expansion", detach=True)["attached"] is False
    assert call("power_status").get("monitors") is None


def test_connect_attaches_the_profiles_monitors(fake_open, fake_transport, monkeypatch):
    from embeddedci.benchpod import BenchPod, Wiring
    from embeddedci_mcp import session as session_mod

    wiring = Wiring.preset().with_changes(monitors=[
        {"chip": "ina226", "addr": 64, "shunt_ohm": 10.0, "name": "dut"}])
    monkeypatch.setattr(session_mod, "BenchPod", lambda connection, **kw: BenchPod(
        transport=fake_transport, lease=False, la_voltage=kw.get("la_voltage"), wiring=wiring))
    connect = call("connect", connection="192.168.1.50", la_voltage=3.3)
    assert not any("current monitors" in w for w in connect.get("warnings", []))
    assert call("wiring")["monitors"] == "attached dut"
    prof = call("measure_power", duration_s=0.05, monitor=True)
    assert prof["efuse"] == 0 and prof["avg_current"] == pytest.approx(1e-6, abs=1e-7)
    assert call("expansion")["monitors"][0]["name"] == "dut"
    assert call("expansion", detach=True, name="dut")["attached"] is False


def test_detach_one_monitor_by_address_and_the_refusals(connected):
    call("expansion", chip="ina226", shunt_ohm=10.0, name="dut")
    r = call("expansion", detach=True, addr=0x40)
    assert r["attached"] is False and r.get("monitors") is None
    with pytest.raises(ToolError, match="no current monitor at addr=64"):
        call("expansion", detach=True, addr=0x40)
    with pytest.raises(ToolError, match="one chip per expansion"):
        call("expansion", chip="ina226", shunt_ohm=10.0, addr=[0x40, 0x41])


def test_refusals(connected):
    with pytest.raises(ToolError, match="no external current monitor is attached"):
        call("measure_power", duration_s=0.1, monitor=True)
    with pytest.raises(ToolError, match="shunt_ohm"):
        call("expansion", chip="ina226")
    with pytest.raises(ToolError, match="no ina226 answers at 0x41"):
        call("expansion", chip="ina226", shunt_ohm=10.0, addr=0x41)
    with pytest.raises(ToolError, match="for the INA current monitors"):
        call("expansion", chip="tca9555", shunt_ohm=10.0)
    call("expansion", chip="ina226", shunt_ohm=10.0)
    with pytest.raises(ToolError, match="no current monitor named 'x'"):
        call("measure_power", duration_s=0.1, monitor="x")
    with pytest.raises(ToolError, match="exclusive"):
        call("measure_power", duration_s=0.1, monitor=True, efuse=1)
