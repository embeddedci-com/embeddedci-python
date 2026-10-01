"""Motor & battery emulator driver, against a fake pod: the SPI master, PSRAM staging, GPIO and a
byte-level model of the emulator link (ECP5 slave-SPI configuration, then the register protocol)."""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import pytest

from embeddedci.benchpod import (
    BatteryModel,
    BenchPod,
    BenchPodError,
    Capabilities,
    EmulatorCalibration,
    MotorEmulator,
    Signal,
    SpiStreamResult,
    Wiring,
)
from embeddedci.benchpod import motor_emulator as me
from embeddedci.benchpod.spi import SpiSession

BITSTREAM = b"\xff\x00LSCC\x00\xff" + b"\xff\xff\xbd\xb3" + bytes(range(256)) * 40


def b64(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def unb64(t: str) -> bytes:
    return base64.urlsafe_b64decode(t + "=" * (-len(t) % 4))


class Board:
    """One emulator board's registers, as the gateware answers them."""

    PORTS = {0x69, 0x78}

    def __init__(self, version: int = me.MIN_VERSION) -> None:
        self.regs: Dict[int, int] = {0x00: me.EMULATOR_ID, 0x01: version, 0x02: 0x0300, 0x03: 0x0081}
        self.writes: List[tuple] = []
        self.tables: Dict[int, List[int]] = {0x69: [], 0x78: []}
        self.fifo: List[int] = []
        self.eeprom = bytearray(256)

    def read(self, addr: int) -> int:
        if addr >= 0xC0:
            return self.fifo.pop(0) if self.fifo else 0
        if addr == 0x7A:
            return len(self.fifo)
        return self.regs.get(addr, 0)

    def write(self, addr: int, v: int) -> None:
        self.writes.append((addr, v))
        if addr in self.PORTS:
            self.tables[addr].append(v)
        elif addr == 0x05:
            self.regs[0x03] |= 0x02                  # armed
        elif addr == 0x7D:
            self.eeprom[self.regs.get(0x7C, 0)] = v & 0xFF
        elif addr == 0x7E and v & 1:
            self.regs[0x7D] = self.eeprom[self.regs.get(0x7C, 0)]
        else:
            self.regs[addr] = v


class EmuPod:
    """The pod's spi_* / gpio / load_bin contract with a stack of emulator boards on the link."""

    PROGRAMN, DONE = 9, 10

    def __init__(self, *, boards: Optional[Dict[int, Board]] = None, caps: Optional[List[str]] = None) -> None:
        self.caps = ["la", "la_pins", "gpio_read", "spi_master", "spi_stream"] if caps is None else caps
        self.boards = {0: Board()} if boards is None else boards
        self.commands: List[dict] = []
        self.armed: Optional[dict] = None
        self.gpio_mode: Dict[int, str] = {}
        self.gpio_level: Dict[int, int] = {}
        self.staged: Optional[bytes] = None
        self.configured = False
        self.cfg_frames: List[bytes] = []
        self.frame = bytearray()
        self.frames: List[bytes] = []               # link frames after configuration
        self.accept = lambda bit: bit.startswith(b"\xff\x00")

    # -- transport surface
    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "adc_bits": 16, "version": "3.4.0", "caps": self.caps}

    def ping(self) -> Any:
        return "pong"

    def close(self) -> None:
        pass

    def stage_psram(self, data: bytes) -> int:
        self.staged = bytes(data)
        return len(data)

    def command(self, req: dict) -> Any:
        self.commands.append(req)
        cmd = req["cmd"]
        if cmd == "spi_start":
            assert self.armed is None and req["mode"] == 0 and req["hz"] <= 6_000_000
            self.armed = req
            return {**{k: req[k] for k in ("sck", "mosi", "miso", "cs", "mode")}, "hz": 6_000_000}
        if cmd == "spi_stop":
            self.armed = None
            return "spi stopped"
        if cmd == "gpio":
            return self._gpio(req)
        assert self.armed is not None, "no SPI session"
        if cmd == "spi_xfer":
            tx = unb64(req["tx"])
            assert 1 <= len(tx) <= 768
            rx = self._bytes(tx)
            if req.get("cs", "release") == "release":
                self._end()
            return {"rx": b64(rx), "cs": "released"}
        if cmd == "spi_stream":
            assert self.staged is not None and req["len"] <= len(self.staged)
            self._end()
            self.frame += unb64(req.get("head", "")) + self.staged[:req["len"]]
            if req.get("cs", "release") == "release":
                self._end()
            return {"sent": req["len"], "ms": 1450, "cs": "released"}
        raise AssertionError(cmd)

    def _gpio(self, req: dict) -> Any:
        if "la" not in req:
            done = int(self.configured)
            return {"levels": (done << (self.DONE - 1)) | (1 << (self.PROGRAMN - 1))}
        las = req["la"] if isinstance(req["la"], list) else [req["la"]]
        if "mode" in req:
            for la in las:
                if req["mode"] == "off":
                    self.gpio_mode.pop(la, None)
                else:
                    self.gpio_mode[la] = req["mode"]
                    self.gpio_level[la] = 1 if req["mode"] == "open_drain" else int(req.get("level") or 0)
            return {"pins": []}
        for la in las:
            assert self.gpio_mode.get(la) in ("output", "open_drain")
            if la == self.PROGRAMN and req["level"] == 0:
                self.configured = False              # PROGRAMN low: the FPGAs clear
                self.cfg_frames.clear()
            self.gpio_level[la] = req["level"]
        return {}

    # -- the link
    def _bytes(self, tx: bytes) -> bytes:
        rx = bytearray()
        for b in tx:
            self.frame.append(b)
            rx.append(self._miso())
        return bytes(rx)

    def _miso(self) -> int:
        """The byte the addressed board drives as the last tx byte goes in (reads only)."""
        f = self.frame
        if not self.configured or len(f) < 4 or not f[0] & 0x80:
            return 0
        board = self.boards.get(f[0] >> 4 & 3)
        if board is None:
            return 0xFF                              # nothing drives MISO: the pull-up
        k = len(f) - 4                               # data byte index
        addr = f[1] if f[1] >= 0xC0 else f[1] + k // 2
        if k % 2 == 0:
            self._word = board.read(addr)
            return self._word >> 8
        return self._word & 0xFF

    def _end(self) -> None:
        f, self.frame = bytes(self.frame), bytearray()
        if not f:
            return
        if not self.configured:
            self.cfg_frames.append(f)
            if len(self.cfg_frames) >= 3 and self.cfg_frames[-1] == bytes([0x26, 0, 0, 0]):
                enable, burst = self.cfg_frames[-3], self.cfg_frames[-2]
                self.configured = (enable == bytes([0xC6, 0, 0, 0]) and burst[:4] == bytes([0x7A, 0, 0, 0])
                                   and self.accept(burst[4:]))
            return
        self.frames.append(f)
        if f[0] & 0x80:
            return
        boards = self.boards.values() if f[0] & 0x40 else [self.boards[f[0] >> 4 & 3]]
        for board in boards:
            addr = f[1]
            for i in range(2, len(f) - 1, 2):
                board.write(addr, f[i] << 8 | f[i + 1])
                if addr not in Board.PORTS:
                    addr += 1


WIRING = Wiring(uart_rx=3, uart_tx=4, spi_sclk=13, spi_mosi=14, spi_miso=6, spi_cs=5,
                signals=[Signal(name="PROGRAMN", la=9, direction="open_drain"),
                         Signal(name="DONE", la=10, direction="input")])


@pytest.fixture
def pod():
    fake = EmuPod()
    return BenchPod(transport=fake, wiring=WIRING), fake


def configured(fake: EmuPod) -> EmuPod:
    fake.configured = True
    return fake


def test_configure_loads_every_board_write_only(pod):
    bp, fake = pod
    with bp.open_motor_emulator() as emu:
        assert (emu.programn.la, emu.done.la) == (9, 10)
        r = emu.configure(BITSTREAM)
        assert r.done and r.length == len(BITSTREAM)
    assert fake.staged == BITSTREAM                 # uploaded into PSRAM first
    stream = next(c for c in fake.commands if c["cmd"] == "spi_stream")
    assert stream == {"cmd": "spi_stream", "len": len(BITSTREAM), "cs": "release", "head": "egAAAA"}
    levels = [c["level"] for c in fake.commands if c["cmd"] == "gpio" and c.get("la") == 9 and "level" in c]
    assert levels == [0, 1]                          # the PROGRAMN pulse
    assert fake.armed is None                        # closed: SPI stopped and pins released
    assert {c["la"] for c in fake.commands if c.get("mode") == "off"} == {9, 10}


def test_configure_raises_when_done_stays_low(pod):
    bp, fake = pod
    fake.accept = lambda bit: False
    with bp.open_motor_emulator() as emu:
        with pytest.raises(BenchPodError, match="DONE stayed low"):
            emu.configure(BITSTREAM, timeout=0.05)


def test_configure_needs_programn_and_done():
    fake = EmuPod()
    bp = BenchPod(transport=fake, wiring=Wiring(uart_rx=3, uart_tx=4, spi_sclk=13, spi_mosi=14, spi_miso=6, spi_cs=5))
    with bp.open_motor_emulator() as emu:
        assert emu.programn is None
        with pytest.raises(BenchPodError, match="PROGRAMN"):
            emu.configure(BITSTREAM)


def test_link_bytes_match_the_protocol(pod):
    bp, fake = pod
    configured(fake)
    fake.boards[1] = Board()
    fake.boards[2] = Board()
    with bp.open_motor_emulator() as emu:
        emu.write("DUTY_A", 0x4000, board=2)
        assert fake.frames[-1] == bytes([0x20, 0x0A, 0x40, 0x00])
        fake.boards[1].regs[0x03] = 0x00A3
        st = emu.status(1)
        assert fake.frames[-1][:3] == bytes([0x90, 0x03, 0x00])
        assert st.fault_clear and st.armed and st.pwm_running and st.pll_locked and not st.watchdog_trip
        emu.write("SCRATCH", -2, board=0, broadcast=True)
        assert all(b.regs[0x07] == 0xFFFE for b in fake.boards.values())
        assert emu.read(0x07, board=1) == 0xFFFE and emu.read_signed("scratch", board=1) == -2
        assert emu.read("ID", 3, board=2) == [me.EMULATOR_ID, me.MIN_VERSION, 0x0300]
        assert emu.boards() == [0, 1, 2]
        with pytest.raises(ValueError):
            emu.read("NOPE")
        with pytest.raises(ValueError):
            emu.write("SCRATCH", 0x10000)


def test_probe(pod):
    bp, fake = pod
    configured(fake)
    fake.boards[3] = Board(version=6)
    with bp.open_motor_emulator() as emu:
        info = emu.probe(0)
        assert info.version == me.MIN_VERSION and info.probe_done and info.probe_ok and info.strap_id == 0
        with pytest.raises(BenchPodError, match="no motor emulator"):
            emu.probe(1)
        with pytest.raises(BenchPodError, match="0x0006"):
            emu.probe(3)


def test_arm_pwm_duties_and_sample(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator(calibration=EmulatorCalibration(offsets=(10, 0, 0, 0, 0))) as emu:
        assert emu.arm(0).armed
        assert emu.set_pwm(200_000, deadtime_s=100e-9) == 200_000
        assert (b.regs[0x08], b.regs[0x09]) == (180, 4)
        emu.set_duties(0.5, 0.25, 1.0)
        assert [b.regs[a] for a in (0x0A, 0x0B, 0x0C)] == [0x8000, 0x4000, 0xFFFF]
        emu.set_control(0, pwm=True, sync_master=True)
        assert b.regs[0x04] == 0x0009
        for a, v in zip(range(0x10, 0x16), (2058, 0xF800, 0, 1024, 391 * 24, 77)):
            b.regs[a] = v
        s = emu.sample(0)
        assert s.codes == (2058, -2048, 0, 1024, 9384) and s.count == 77
        assert s.phase_currents == (1.0, -1.0, 0.0) and s.battery_current == 0.5 and s.bus_voltage == 24.0
        with pytest.raises(ValueError):
            emu.set_pwm(10_000)


def test_protection_and_trips(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator() as emu:
        emu.set_protection(0, overcurrent_a=5, overvoltage_on_v=30, overvoltage_off_v=29,
                           hot_swap_limit_v=32, brake_ohm=10, brake_avg_w=5, brake_budget_j=2)
        assert [b.regs[a] for a in range(0x51, 0x58)] == [10240, 2, 11730, 11339, 11, 5, 488]
        assert b.regs[0x5C] == 12512
        b.regs[0x50] = 0x0011
        b.regs[0x5D] = 0x0100 | 0b1101110             # valid, OC_TRIP_N and FAULT low
        t = emu.trips(0)
        assert t.overcurrent == (True, False, False) and t.brake_inhibited and t.tripped
        assert t.sources == ("OC_TRIP_N", "FAULT")
        emu.clear_trips(0)
        assert b.writes[-2:] == [(0x50, 0x000F), (0x06, 1)]
        b.regs[0x5D] = 0
        assert emu.trips(0).sources is None


def test_battery_model(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    model = BatteryModel(capacity_ah=2.0, ocv=[25.2, 22.2, 18.0], r0_ohm=0.020, r1_ohm=0.010,
                         tau_s=10.0, soc=0.5, v_max=26.0)
    with bp.open_motor_emulator() as emu:
        emu.set_battery(model, pv_set=True)
        assert b.writes[0] == (0x60, 0)              # stopped while loading
        q = b.regs[0x61]
        total = 2.0 * 3600 * 281_250 * 2048
        assert 2 ** q * 63 >= total > 2 ** (q - 1) * 63
        assert b.regs[0x62] == 125 and b.regs[0x63] == 63 and b.regs[0x64] == 105
        assert (b.regs[0x65], b.regs[0x66]) == (0, 10166)
        entry = 2 ** q / total
        assert b.regs[0x67] == round(0.5 / entry * 256)
        table = b.tables[0x69]
        assert len(table) == 64 and table[0] == round(25.2 * 391)
        assert table[-1] == round(18.0 * 391)        # past the capacity: empty
        assert table == sorted(table, reverse=True)
        assert (0x6A, 0) not in b.writes and 0x6A not in b.regs   # the burst stayed on BAT_TBL_DATA
        assert b.regs[0x60] == 0b101
        b.regs[0x6C], b.regs[0x6D], b.regs[0x6E] = 391 * 21, 0xFC00, 391 * 22
        st = emu.battery_state()
        assert (st.setpoint, st.current, st.ocv) == (21.0, -0.5, 22.0)
        assert st.soc == pytest.approx(0.5, abs=1e-3)
        with pytest.raises(ValueError):
            emu.set_battery(BatteryModel(capacity_ah=1, ocv=[4.2], r0_ohm=0))
        with pytest.raises(ValueError):
            emu.set_battery(BatteryModel(capacity_ah=1, ocv=[4.2, 3.0], r0_ohm=10))


def test_shape_is_one_burst(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator() as emu:
        emu.set_shape([1.0 if i < 512 else -1.0 for i in range(1024)])
        assert b.tables[0x78] == [32767] * 512 + [0x8001] * 512
        assert b.regs[0x77] == 0
        burst = [f for f in fake.frames if f[1] == 0x78]
        assert len(burst) == 1 and len(burst[0]) == 2 + 2048


def test_log_stream_keeps_sets_whole(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator() as emu:
        with pytest.raises(BenchPodError):
            emu.read_log()
        emu.start_log(["a", "bus"], osr256=True)
        assert b.writes[-2:] == [(0x79, 0x8000), (0x79, 0x00F1)]
        seq = 0
        for _ in range(500):
            b.fifo += [seq, 0xFFFF, 9000]
            seq += 1
        b.fifo += [seq, 0xFFFE]                       # a partial set
        b.regs[0x7B] = 3
        log = emu.read_log()
        assert len(log) == 500 and log.seq == list(range(500)) and log.drops == 3
        assert set(log.channels) == {"a", "bus"} and log.channels["a"][0] == -1 and log.channels["bus"][-1] == 9000
        assert not b.fifo
        b.fifo += [9000]
        log = emu.read_log()
        assert log.seq == [500] and log.channels["a"] == [-2]
        reads = [f for f in fake.frames if f[0] & 0x80 and f[1] == 0xC0]
        assert max(len(f) for f in reads) <= 768
        with pytest.raises(ValueError):
            emu.start_log(["x"])


def test_eeprom(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator() as emu:
        emu.eeprom_write(0x10, b"BP")
        assert b.eeprom[0x10:0x12] == b"BP"
        assert emu.eeprom_read(0x10, 2) == b"BP"
        b.regs[0x7E] = 0x0002
        with pytest.raises(BenchPodError, match="acknowledge"):
            emu.eeprom_read(0, 1)


def test_open_motor_emulator_checks(pod):
    bp, fake = pod
    with pytest.raises(ValueError):
        bp.open_motor_emulator(hz=8_000_000)
    bp2 = BenchPod(transport=EmuPod(caps=["la", "la_pins"]), wiring=WIRING)
    with pytest.raises(BenchPodError):
        bp2.open_motor_emulator()


def test_spi_stream_session():
    cmds: List[dict] = []
    staged: List[bytes] = []

    def command(req):
        cmds.append(req)
        return {"sent": req["len"], "ms": 250, "cs": "held"}

    spi = SpiSession(command, {"hz": 6_000_000}, stage=lambda d: staged.append(d) or len(d))
    r = spi.stream(b"\x01" * 1000, head=b"\x7a\0\0\0", hold_cs=True)
    assert r == SpiStreamResult(sent=1000, seconds=0.25)
    assert staged == [b"\x01" * 1000]
    assert cmds[-1] == {"cmd": "spi_stream", "len": 1000, "cs": "hold", "head": "egAAAA"}
    spi.stream(b"", head=b"\x26\0\0\0")             # head only: nothing staged
    assert len(staged) == 1 and cmds[-1]["len"] == 0
    with pytest.raises(ValueError):
        spi.stream(b"x", head=bytes(65))
    with pytest.raises(ValueError):
        spi.stream(b"")
    with pytest.raises(NotImplementedError):
        SpiSession(command, {}).stream(b"x")


def test_capability():
    assert Capabilities.from_status({"caps": ["spi_master", "spi_stream"]}).spi_stream
    assert Capabilities.from_parameters({"cap.spi_stream": "true"}).spi_stream


def test_configure_from_a_file_and_input_checks(pod, tmp_path):
    bp, fake = pod
    path = tmp_path / "emu.bit"
    path.write_bytes(BITSTREAM)
    with bp.open_motor_emulator(programn=9, done=10) as emu:
        assert emu.configure(path).length == len(BITSTREAM)
        assert fake.staged == BITSTREAM
        with pytest.raises(ValueError, match="empty"):
            emu.configure(b"")
        with pytest.raises(ValueError):
            emu.read("ID", 0)
        with pytest.raises(ValueError):
            emu.read(0x100)
        with pytest.raises(ValueError):
            emu.write("SCRATCH", [])
        with pytest.raises(ValueError):
            emu.read("ID", board=4)
    emu.close()                                      # idempotent
    assert [c for c in fake.commands if c["cmd"] == "spi_stop"] == [{"cmd": "spi_stop"}]


def test_reconfiguring_forgets_the_log_and_battery_state(pod):
    bp, fake = pod
    with bp.open_motor_emulator() as emu:
        emu.configure(BITSTREAM)
        emu.start_log(["a"])
        emu.set_battery(BatteryModel(capacity_ah=1, ocv=[4.2, 3.0], r0_ohm=0.05))
        assert emu.battery_state().soc == pytest.approx(1.0)
        emu.configure(BITSTREAM)
        assert emu.battery_state().soc is None       # the gateware lost the model
        with pytest.raises(BenchPodError):
            emu.read_log()


def test_arm_fails_while_a_fault_holds_the_latch(pod, monkeypatch):
    bp, fake = pod
    b = configured(fake).boards[0]
    monkeypatch.setattr(Board, "write", lambda self, addr, v: self.writes.append((addr, v)))
    with bp.open_motor_emulator() as emu:
        with pytest.raises(BenchPodError, match="did not arm"):
            emu.arm(0, timeout=0.02)
    assert b.writes == [(0x05, 1)]


def test_time_min_on_pv_calibration_and_stop_log(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    with bp.open_motor_emulator() as emu:
        b.regs[0x71], b.regs[0x72] = 1500, 18000
        assert emu.time() == pytest.approx(1.5005)
        assert emu.set_pwm(100_000, min_on_s=250e-9, broadcast=True) == 100_000
        assert (b.regs[0x08], b.regs[0x0D]) == (360, 9)
        emu.set_pv_calibration(-100, 30000)
        assert (b.regs[0x6A], b.regs[0x6B]) == (0xFF9C, 30000)
        emu.start_log(sequence=False)
        assert b.regs[0x79] == 0x005F
        b.fifo += [1, 2, 3, 4, 5]
        log = emu.read_log(max_words=5)
        assert log.seq == [] and len(log) == 1 and log.channels["bus"] == [5]
        emu.stop_log()
        assert b.regs[0x79] == 0
        with pytest.raises(ValueError):
            emu.start_log([])


def test_validation_of_units(pod):
    bp, fake = pod
    configured(fake)
    cal = EmulatorCalibration()
    assert cal.current_code(100) == 32767 and cal.bus_code(24) == 9384
    with pytest.raises(ValueError, match="ohm"):
        cal.resistance_code(6.0)
    with bp.open_motor_emulator() as emu:
        with pytest.raises(ValueError):
            emu.set_protection(0, overcurrent_samples=16)
        with pytest.raises(ValueError):
            emu.set_shape([0.0] * 10)
        with pytest.raises(ValueError, match="soc"):
            emu.set_battery(BatteryModel(capacity_ah=1, ocv=[4.2, 3.0], r0_ohm=0.05, soc=1.5))
        with pytest.raises(ValueError, match="16 ms"):
            emu.set_battery(BatteryModel(capacity_ah=1, ocv=[4.2, 3.0], r0_ohm=0.05, r1_ohm=0.01, tau_s=0.001))
        with pytest.raises(ValueError, match="too large"):
            emu.set_battery(BatteryModel(capacity_ah=1e6, ocv=[4.2, 3.0], r0_ohm=0.05))
        emu.set_protection(0)                        # everything off
        b = fake.boards[0]
        assert [b.regs[a] for a in (0x51, 0x53, 0x54, 0x55, 0x5C)] == [0, 0, 0, 0, 0]


def test_eeprom_stays_busy(pod):
    bp, fake = pod
    b = configured(fake).boards[0]
    b.regs[0x7E] = 0x0001
    with bp.open_motor_emulator() as emu:
        with pytest.raises(BenchPodError, match="busy"):
            emu.eeprom_write(0, b"x", timeout=0.02)


def test_open_releases_spi_when_a_config_pin_is_taken(pod):
    bp, fake = pod
    original = fake._gpio

    def refuse(req):
        if req.get("mode") == "input":
            from embeddedci.benchpod import FirmwareError
            raise FirmwareError("pin conflict: LA10 is in use by uart_rx", cmd="gpio")
        return original(req)

    fake._gpio = refuse
    with pytest.raises(BenchPodError):
        bp.open_motor_emulator()
    assert fake.armed is None and fake.gpio_mode == {}   # SPI stopped, PROGRAMN released again
