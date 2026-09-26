"""Tier "pod": every 2.x SDK feature that needs the pod but no DUT.

    pytest packages/embeddedci/tests/e2e/test_e2e_pod.py --benchpod-connection=<pod host>

Analog tests loop the DAC back into the ADC through the pod's internal calibration relays. DAC
outputs stay at or below BENCHPOD_E2E_DAC_MAX_V (3.3 V); the bipolar 12v output is only driven with
BENCHPOD_E2E_ALLOW_12V=1. Each test leaves the pod quiet.
"""

from __future__ import annotations

import time

import pytest

from embeddedci.benchpod import (
    BenchPodError,
    CanReadResult,
    DacOutput,
    FirmwareError,
    FpgaImage,
    FpgaImageInfo,
    LoopInputMap,
    PowerStatus,
    ResetState,
    TargetStatus,
    UsbCcStatus,
    build_constant_curve,
    build_linear_curve,
    curve_output_at,
    dsp,
)
from embeddedci.benchpod.constants import ANALOG_PATHS
from embeddedci.benchpod.control_loop import CURVE_POINTS

from e2e_helpers import SETTLE, adc_levels, events_during, host_clock_rate, p2p, split_levels

pytestmark = pytest.mark.hardware


# -- identity, state, errors ---------------------------------------------------------

def test_status_and_capabilities(pod):
    status = pod.status()
    assert isinstance(status, dict) and status.get("board"), status
    caps = pod.refresh_capabilities()
    assert caps.board == status["board"]
    if caps.board == "stm32h563":
        assert caps.adc_bits == 16 and caps.scope and caps.analyzer


def test_la_voltage_is_selected_from_conftest(pod):
    state = pod.get_la_voltage()
    assert state.voltage == 3.3 and state.is_set


def test_1v8_bank(pod, rev3):
    try:
        if rev3:
            assert pod.set_la_voltage(1.8).voltage == 1.8
        else:
            with pytest.raises(FirmwareError, match="v3"):
                pod.set_la_voltage(1.8)
    finally:
        pod.set_la_voltage(3.3)
    assert pod.get_la_voltage().voltage == 3.3


def test_target_and_power_status_are_typed(pod, bench):
    ts, ps = pod.target_status(), pod.power_status()
    assert isinstance(ts, TargetStatus) and isinstance(ps, PowerStatus)
    rail = ps.rail(bench.efuse)
    assert rail.ok and isinstance(rail.bus_voltage, float) and isinstance(rail.current, float)


def test_reset_line_and_usb_cc(pod, rev3):
    if rev3:
        assert isinstance(pod.reset_state(), ResetState)
        assert isinstance(pod.usb_cc(), UsbCcStatus)
    else:
        with pytest.raises(FirmwareError, match="v3"):
            pod.reset_target(pulse=0.01)
        with pytest.raises(FirmwareError, match="v3"):
            pod.usb_cc()


@pytest.mark.parametrize("bad_call", [
    lambda p: p.analog_path("5v"),
    lambda p: p.dac_output("off", volts=1.0),
    lambda p: p.adc_read("sma"),
    lambda p: p.enable_pullup(7),
    lambda p: p.enable_pulldown(1),
    lambda p: p.set_la_voltage(5),
    lambda p: p.generate("sine", freq_hz=100, amplitude=0.0001),
    lambda p: p.capture_adc(0),
    lambda p: p.capture_la(64, sample_rate_hz=0),
    lambda p: p.replay([1.0], dac_path="24v"),
    lambda p: p.fpga_image(2),
    lambda p: p.la_step(9, steps=0, delay=0.001),
    lambda p: p.can_write(0x10, list(range(9))),
    lambda p: p.decode([0, 1], "can"),
    lambda p: p.control_loop(vmin=50000, vmax=1000),
], ids=["analog_path alias", "volts on off", "adc source", "pullup on LA7", "pulldown on LA1",
        "la voltage", "tiny amplitude", "zero samples", "zero rate", "dac path", "fpga image",
        "zero steps", "9 CAN bytes", "protocol", "inverted clamp"])
def test_invalid_arguments_raise_before_touching_the_pod(pod, bad_call):
    with pytest.raises(ValueError):
        bad_call(pod)


def test_firmware_errors_surface_as_firmware_error(pod):
    with pytest.raises(FirmwareError):
        pod.command({"cmd": "no_such_command"})


# -- analog ----------------------------------------------------------------------------

@pytest.mark.parametrize("path", [p for p in ANALOG_PATHS if p != "dac_12v"])
def test_analog_path_applies(pod, path):
    state = pod.analog_path(path)
    assert state.path == path
    # `path` alone is the pod echoing the string we sent. The mux and relay registers are what the
    # hardware was actually set to, so assert on those — and that re-reading returns the same thing
    # rather than only reflecting the last write.
    assert (state.dac_mux_register, state.cal_relay_register) == \
        (pod.analog_path(path).dac_mux_register, pod.analog_path(path).cal_relay_register)


def test_analog_paths_are_not_all_the_same_switch_setting(pod):
    """Distinct paths must drive distinct hardware, not just return distinct names."""
    settings = {p: (s.dac_mux_register, s.cal_relay_register)
                for p in ANALOG_PATHS if p != "dac_12v"
                for s in [pod.analog_path(p)]}
    assert len(set(settings.values())) > 1, f"every path set the same registers: {settings}"


def test_dac_output_codes_match_the_sdk_volts_mapping(pod, bench):
    rows = []
    points = [("3v3", 0.5), ("3v3", 1.5), ("5v", 1.0), ("5v", min(2.5, bench.dac_max_v))]
    if bench.allow_12v:
        points += [("12v", -1.0), ("12v", 0.0), ("12v", 1.0)]
    for path, volts in points:
        out = pod.dac_output(path, volts=volts)
        lo, hi = dsp.dac_path_range_v(path)
        rows.append((path, volts, out.voltage, out.code, round((volts - lo) / (hi - lo) * 255)))
        assert isinstance(out, DacOutput) and out.path == path
    pod.dac_output("off")
    assert all(abs(fw - sdk) <= 2 for *_, fw, sdk in rows), f"(path, V, achieved, fw code, sdk code): {rows}"
    assert all(abs(achieved - volts) < 0.06 for _, volts, achieved, _, _ in rows), rows


def test_dac_output_reads_back_through_the_adc(pod):
    for volts in (1.0, 2.5):
        pod.dac_output("5v", volts=volts)
        time.sleep(0.1)
        reading = pod.adc_read("cal1")
        assert abs(reading.voltage - volts) < 0.1, (volts, reading)


def test_generate_levels_are_volts(pod):
    pod.analog_path("cal1")
    with pod.generate("square", freq_hz=2, amplitude=0.8, offset=1.5, dac_path="5v", route=False):
        readings = adc_levels(pod, "cal1", 1.3)
    hi, lo = split_levels(readings, 1.5)
    # offset ± amplitude, within one 8-bit step (~20 mV) plus DAC/ADC calibration error
    assert hi is not None and lo is not None, readings
    assert abs(hi - 2.3) < 0.15 and abs(lo - 0.7) < 0.15, (hi, lo, readings)


def test_generate_on_the_bipolar_12v_path_centres_on_zero(pod, bench):
    if not bench.allow_12v:
        pytest.skip("set BENCHPOD_E2E_ALLOW_12V=1 when nothing is wired to the 12v output")
    pod.analog_path("cal2")
    with pod.generate("square", freq_hz=2, amplitude=1.0, dac_path="12v", route=False):
        readings = adc_levels(pod, "cal2", 1.3)
    hi, lo = split_levels(readings, 0.0)
    assert hi is not None and lo is not None, readings
    assert abs(hi - 1.04) < 0.2 and abs(lo + 1.0) < 0.2, (hi, lo)


def test_route_false_keeps_a_loopback_and_route_true_reroutes(pod):
    pytest.importorskip("numpy")
    pod.analog_path("cal1")
    with pod.generate("sine", freq_hz=200, amplitude=0.8, offset=1.5, route=False):
        assert pod.lowlevel.cal_switch_status()["cal1"] == 1
        time.sleep(SETTLE)
        cap = pod.capture_adc(8192, sample_rate_hz=20_000)
    assert abs(cap.dominant_frequency() - 200) < 5 and p2p(cap) > 800, (cap.dominant_frequency(), p2p(cap))
    pod.analog_path("cal1")
    with pod.generate("sine", freq_hz=200, amplitude=0.8, offset=1.5):
        assert pod.lowlevel.cal_switch_status()["cal1"] == 0  # dac_out re-routed the output


def test_capture_adc_source_routes_the_input(pod):
    pod.analog_path("off")
    cap = pod.capture_adc(256, source="cal1")
    assert cap.source == "cal1" and len(cap) == 256
    assert pod.lowlevel.cal_switch_status()["cal1"] == 1


def test_replay_volts_on_the_loopback(pod):
    pytest.importorskip("numpy")
    pod.analog_path("cal1")
    with pod.replay([0.8] * 50 + [2.2] * 50, dac_path="5v", sample_rate_hz=10_000, route=False) as h:
        time.sleep(SETTLE)
        cap = pod.capture_adc(8192, sample_rate_hz=20_000)
    assert h.samples == 100 and not h.deep
    assert abs(cap.dominant_frequency() - 100) < 5 and p2p(cap) > 800


def test_co_triggered_replay_arms_on_the_capture(pod):
    if not pod.capabilities.dac_cotrig:
        pytest.skip("the running gateware has no DAC co-trigger")
    pod.analog_path("cal1")
    with pod.replay([0.8] * 50 + [2.2] * 50, sample_rate_hz=10_000, route=False, on_capture=True) as h:
        assert h.cotrig
        cc = pod.capture_correlated(adc_samples=4096, adc_sample_rate_hz=20_000, la_samples=1024)
    assert p2p(cc.adc) > 800


def test_stop_dac_after_cuts_the_output_mid_capture(pod):
    pod.analog_path("cal1")
    pod.generate("square", freq_hz=200, amplitude=0.8, offset=1.5, route=False)
    time.sleep(SETTLE)
    cc = pod.capture_correlated(adc_samples=8192, adc_sample_rate_hz=20_000, la_samples=1024,
                                stop_dac_after=0.1)
    early, late = cc.adc.counts[200:1800], cc.adc.counts[-1600:]
    assert max(early) - min(early) > 800, "the DAC was not running at the start of the capture"
    assert max(late) - min(late) < 300, "the DAC was still toggling after stop_dac_after"


def test_a_restarted_dac_plays_the_new_waveform_from_its_first_sample(pod):
    # Gateware <= v32 ran the first pass of a DAC start at the PREVIOUS waveform's length, so a
    # 5 kHz square (181 samples) after a 200 Hz one (2034) played ~2 ms of stale 200 Hz levels.
    # The co-trigger puts DAC sample 0 on the capture's t0, so that first pass is in the window.
    np = pytest.importorskip("numpy")
    if not pod.capabilities.dac_cotrig:
        pytest.skip("the running gateware has no DAC co-trigger")
    pod.analog_path("cal1")
    with pod.generate("square", freq_hz=200, amplitude=0.8, offset=1.5, route=False):
        time.sleep(0.05)
    with pod.generate("square", freq_hz=5000, amplitude=0.8, offset=1.5, route=False,
                      on_capture=True) as h:
        assert h.cotrig
        cap = pod.capture_adc(4000, sample_rate_hz=400_000)
    blocks = np.asarray(cap.counts, dtype=np.int64)[:4000].reshape(-1, 100)   # 0.25 ms each
    spread = blocks.max(axis=1) - blocks.min(axis=1)
    flat = np.flatnonzero(spread[1:] <= 800) + 1
    assert flat.size == 0, f"the square stalled in blocks {flat.tolist()} (spreads {spread.tolist()})"


def test_generated_frequency_matches_the_adc_clock(pod):
    # The firmware picks the DAC divider and period from its model of what one DAC sample costs
    # in the gateware (max(divider, 3) + 51 clk48).  If the gateware's frame sequencing drifts
    # from that model, every generated frequency is off by ~2% per clock of difference.  The ADC
    # shares the FPGA clock and its own rate is held to the host clock below, so it is the ruler.
    np = pytest.importorskip("numpy")
    pod.analog_path("cal1")
    freq = 1000.0
    with pod.generate("square", freq_hz=freq, amplitude=0.8, offset=1.5, route=False):
        time.sleep(SETTLE)
        cap = pod.capture_adc(400_000, sample_rate_hz=400_000)
    counts = np.asarray(cap.counts, dtype=np.int64)
    high = counts > (counts.max() + counts.min()) // 2
    rises = np.flatnonzero(~high[:-1] & high[1:]) + 1
    rises = rises[np.insert(np.diff(rises) > 100, 0, True)]      # one per 400-sample period
    assert len(rises) > 900, f"only {len(rises)} rising edges in 1 s of a {freq:.0f} Hz square"
    measured = (len(rises) - 1) * cap.sample_rate_hz / (rises[-1] - rises[0])
    assert measured == pytest.approx(freq, rel=0.002), (
        f"generate({freq:.0f} Hz) plays {measured:.2f} Hz — the firmware's DAC rate model and the "
        f"gateware disagree")


def _measure(pod, waveform: str, samples: int):
    """Volts of one firmware ``measure``: one DAC period of ``samples`` points, captured by the ADC
    phase-locked to it. The SDK does not wrap it, so this goes through the raw command."""
    np = pytest.importorskip("numpy")
    counts = pod.command({"cmd": "measure", "waveform": waveform, "freq": 1000,
                          "samples": samples})
    assert len(counts) == samples, (waveform, samples, len(counts))
    return np.array([pod.capabilities.counts_to_volts(c) for c in counts])


def test_measure_captures_one_period_of_the_played_waveform(pod):
    # measure ties the DAC period to the capture count (gateware MEASURE), so the capture holds
    # exactly one period. It does not route the analog path: without cal1 it reads the idle SMA.
    # The second square uses a different length, so a stale period from the first run shows up.
    np = pytest.importorskip("numpy")
    lat = 4                                   # lead-in: the previous DAC level + 2-3 samples of latency
    pod.analog_path("cal1")
    try:
        runs = [(w, n, _measure(pod, w, n))
                for w, n in (("square", 256), ("sine", 256), ("square", 128))]
    finally:
        pod.analog_path("off")
    for wave, n, v in runs:
        assert v.max() - v.min() > 2.0, (wave, n, v.round(2).tolist())
        if wave == "square":
            # The capture opens on whatever level the DAC held before (a previous test's), and the
            # first played sample reaches the ADC 2-3 samples in (where the DAC update falls against
            # the ADC's sample clock varies by one).  So measure that lead-in: a rise within the
            # first few samples, then exactly one fall half a period after it.
            mid = (np.median(v[4:n // 2]) + np.median(v[n // 2 + 4:])) / 2
            high = v > mid
            edges = np.flatnonzero(high[1:] != high[:-1]) + 1
            lead = 0
            if edges.size and edges[0] <= 4 and high[edges[0]]:
                lead, edges = int(edges[0]), edges[1:]
            assert edges.size == 1, (n, lead, edges.tolist(), v.round(2).tolist())
            fall = int(edges[0])
            if lead:   # the lead-in was visible: the fall is exactly half a period after it
                assert abs(fall - (lead + n // 2)) <= 1, (n, lead, fall)
            else:      # the DAC already sat high: the fall is half a period plus the latency
                assert n // 2 + 1 <= fall <= n // 2 + 4, (n, fall)
        else:
            ac = v[lat:] - v[lat:].mean()
            phase = 2 * np.pi * np.arange(lat, n) / n
            fit = np.hypot(ac @ np.sin(phase), ac @ np.cos(phase)) * 2 / len(ac)
            rms_fit, rms = fit / np.sqrt(2), np.sqrt((ac ** 2).mean())
            assert rms_fit > 0.97 * rms, f"not one sine period: fit {rms_fit:.3f} V of {rms:.3f} V rms"
            assert abs(abs(int(np.argmax(v)) - int(np.argmin(v[lat:]) + lat)) - n // 2) <= 8


# -- captures ----------------------------------------------------------------------------

def test_capture_adc_shallow_and_deep(pod):
    shallow = pod.capture_adc(4096, sample_rate_hz=100_000)
    deep = pod.capture_adc(100_000, sample_rate_hz=400_000)   # above 32768: streams from PSRAM
    assert len(shallow) == 4096 and shallow.sample_rate_hz > 0
    assert len(deep) == 100_000 and deep.duration == pytest.approx(100_000 / deep.sample_rate_hz)


def test_capture_la_and_correlated(pod):
    la = pod.capture_la(65536, sample_rate_hz=1_000_000)
    assert len(la) == 65536 and la.sample_rate_hz == pytest.approx(1e6, rel=0.05)
    cc = pod.capture_correlated(adc_samples=4096, adc_sample_rate_hz=100_000,
                                la_samples=4096, la_sample_rate_hz=1_000_000)
    assert len(cc.adc) == 4096 and len(cc.la) == 4096


def test_la_step_pulses_show_up_in_a_logic_capture(pod, bench):
    ch = bench.free_la[0]
    steps, delay = 400, 0.002
    t0 = time.monotonic()
    pod.la_step(ch, steps=steps, delay=delay)
    try:
        la = pod.capture_la(200_000, sample_rate_hz=1_000_000)
        # 200k samples at 1 MS/s = 0.2 s of a train pulsing every 2 * delay (4 ms) = ~50 pulses,
        # so ~100 edges. `> 0` would pass on a single spurious edge; bound it on both sides and
        # check the measured pulse width matches the delay we asked for.
        edges = la.edges(ch)
        assert 60 < edges < 140, f"expected ~100 edges on LA{ch} in 0.2 s, saw {edges}"
        widths = la.pulse_widths(ch)
        assert widths, f"no complete pulses on LA{ch}"
        median = sorted(widths)[len(widths) // 2]
        assert median == pytest.approx(delay, rel=0.15), \
            f"pulse width {median * 1e3:.2f} ms, asked for {delay * 1e3:.2f} ms"
    finally:
        # la_step returns at once but the FPGA keeps pulsing (one pulse per 2 * delay: ~1.6 s here)
        # and refuses another train as "busy" until it ends, so wait it out for the next test.
        time.sleep(max(0.0, t0 + steps * 2 * delay + 0.1 - time.monotonic()))


# The host clock is the independent reference for the capture clock: events sent at known host
# times land at sample indices, and the fitted slope is the real sample rate. The drift a wrong
# rate causes grows with the capture length (samples / 24 MHz regardless of rate: ~170 ms for
# the LA test, ~80 ms for the ADC test), far above the few ms of network jitter.
def test_la_sample_rate_matches_the_host_clock(pod, bench):
    np = pytest.importorskip("numpy")
    ch = bench.free_la[0]
    la, stamps = events_during(lambda: pod.capture_la(4_000_000, sample_rate_hz=1_000_000),
                               lambda k: pod.la_step(ch, steps=1, delay=0.001),
                               count=25, interval=0.2)
    bits = np.asarray(la.channel(ch), dtype=np.int8)
    rate = host_clock_rate(np.flatnonzero(np.diff(bits) == 1) + 1, stamps)
    assert rate == pytest.approx(la.sample_rate_hz, rel=0.005), (
        f"the LA really samples at {rate:.0f} Hz ({rate / la.sample_rate_hz:.4f}x the reported "
        f"{la.sample_rate_hz:.0f} Hz)")


def test_adc_sample_rate_matches_the_host_clock(pod):
    np = pytest.importorskip("numpy")
    pod.analog_path("cal1")

    def toggle_dac(k):  # a 5 kHz burst on even events, flat on odd ones
        if k % 2 == 0:
            pod.generate("square", freq_hz=5000, amplitude=0.8, offset=1.5, route=False)
        else:
            pod.dac_stop()

    # Every event must land inside the 5 s acquisition (the fire loop starts ~0.4 s in): a command
    # sent after it ends queues behind the read-back (~4 s as base64, ~30 s as decimal text on
    # firmware without capture_b64) and can time out.  18 events end at ~4.7 s; the fit needs 8.
    cap, stamps = events_during(lambda: pod.capture_adc(2_000_000, sample_rate_hz=400_000),
                                toggle_dac, count=18, interval=0.25)
    block = 100
    counts = np.asarray(cap.counts, dtype=np.int64)
    blocks = counts[: len(counts) // block * block].reshape(-1, block)
    active = (blocks.max(axis=1) - blocks.min(axis=1)) > 800
    edges = (np.flatnonzero(np.diff(active.astype(np.int8)) != 0) + 1) * block
    rate = host_clock_rate(edges, stamps)
    assert rate == pytest.approx(cap.sample_rate_hz, rel=0.005), (
        f"the ADC really samples at {rate:.0f} Hz ({rate / cap.sample_rate_hz:.4f}x the reported "
        f"{cap.sample_rate_hz:.0f} Hz)")


# -- DAC replay depth vs gateware image ----------------------------------------------------

def test_deep_replay_is_refused_on_the_loop_image_without_switching(loop_image):
    with pytest.raises(BenchPodError, match="DEEP_REPLAY"):
        loop_image.replay([1.0] * 4096, dac_path="5v", route=False, switch_image=False)
    assert loop_image.refresh_capabilities().dac_control_loop  # still on the loop image


def test_deep_replay_switches_to_the_deep_replay_image(loop_image):
    np = pytest.importorskip("numpy")
    pod = loop_image
    period = [0.8] * 100 + [2.2] * 100                 # 100 Hz at 20 kS/s
    with pod.replay(period * 41, dac_path="5v", sample_rate_hz=20_000, route=False) as h:
        assert h.deep and h.switched_image is not None
        assert h.switched_image.image == FpgaImage.DEEP_REPLAY
        pod.analog_path("cal1")                        # after the switch, which resets the FPGA
        time.sleep(0.5)
        adc = pod.capture_adc(8192, sample_rate_hz=20_000)
    assert pod.refresh_capabilities().dac_deep_replay
    assert abs(adc.dominant_frequency() - 100) < 5 and int(np.ptp(adc.counts)) > 800


def test_control_loop_switches_to_the_loop_image(deep_image):
    pod = deep_image
    if not pod.capabilities.dac_loop_sources:
        pytest.skip("the gateware has no selectable loop input")
    with pod.control_loop(curve=build_linear_curve(30000), source="fixed", input_code=0) as loop:
        assert loop.switched_image is not None and loop.switched_image.image == FpgaImage.LOOP
        pod.analog_path("dac_3v3")
        time.sleep(0.1)
        assert loop.probe().loop_input == 0
    assert pod.refresh_capabilities().dac_control_loop


def test_control_loop_without_switching_is_refused_on_the_deep_replay_image(deep_image):
    with pytest.raises(BenchPodError, match="switch_image"):
        deep_image.control_loop(curve=build_linear_curve(30000), switch_image=False)
    assert deep_image.refresh_capabilities().dac_deep_replay  # still on the deep-replay image


def test_control_loop_fixed_input_indexes_the_curve(loop_image):
    pod = loop_image
    if not pod.capabilities.dac_loop_sources:
        pytest.skip("the running gateware has no selectable loop input")
    pod.analog_path("dac_3v3")  # keep the physical output at or below 3.3 V
    curve = build_linear_curve(60000)
    with pod.control_loop(curve=curve, k=32767, source="fixed", input_code=0) as loop:
        for code in (0, 32768, 65535):
            state = loop.set_input(code)
            assert state.input_code == code
            time.sleep(0.1)
            pt = loop.probe()
            assert pt.loop_input == code and abs(pt.v - curve_output_at(curve, code)) < 1500, (code, pt)


def test_control_loop_sweep_advances(loop_image):
    pod = loop_image
    if not pod.capabilities.dac_loop_sources:
        pytest.skip("the running gateware has no selectable loop input")
    pod.analog_path("dac_3v3")
    with pod.control_loop(curve=build_linear_curve(60000), source="sweep", step=256) as loop:
        first = loop.probe().loop_input
        time.sleep(0.05)
        assert loop.probe().loop_input != first


def test_control_loop_input_map(loop_image):
    pod = loop_image
    if not pod.capabilities.dac_loop_input_map:
        pytest.skip("the running gateware has no loop input map")
    pod.analog_path("dac_3v3")
    with pod.control_loop(curve=build_linear_curve(30000),
                          input_map=LoopInputMap(mv_per_unit=2.0, range_min=0, range_max=500)) as loop:
        assert loop.armed


def _regulator_curve(setpoint_v, lo, hi, n=CURVE_POINTS):
    """out = 2*setpoint - in over a 0-5 V input axis: slope -1 through the setpoint, so through a
    unity wire the only fixed point is in == setpoint."""
    return [int(round((min(max(2 * setpoint_v - 5.0 * i / (n - 1), 0.0), 4.8) - lo) / (hi - lo) * 65535))
            for i in range(n)]


def test_control_loop_regulates_through_the_external_wire(loop_image, bench):
    # The loop closed through real wiring: DAC 0-5 V output -> ADC front SMA.  The input map puts
    # the curve on a 0-5 V axis, so the loop must hold the SMA at the setpoint; then again at a
    # second setpoint (a re-arm must use the new curve).  Then the over-range trip: a curve that
    # drives past 3.5 V must latch the trip, park the output at vmin, and say so.
    if not bench.ext_loop:
        pytest.skip("set BENCHPOD_E2E_EXT_LOOP=1 when the DAC 0-5 V output is wired to the ADC SMA")
    pod = loop_image
    if not pod.capabilities.dac_loop_input_map:
        pytest.skip("the running gateware has no loop input map")
    lo, hi = dsp.dac_path_range_v("5v")
    volts = pod.capabilities.counts_to_volts
    vmap = LoopInputMap(mv_per_unit=1000.0, range_min=0.0, range_max=5.0)
    pod.dac_output("5v")
    pod.adc_read("ext")                                   # route the ADC to the SMA
    for setpoint in (2.0, 3.0):
        with pod.control_loop(curve=_regulator_curve(setpoint, lo, hi), k=8192, source="adc",
                              input_map=vmap) as loop:
            time.sleep(0.5)
            got = [volts(loop.probe().i) for _ in range(10)]
            assert all(abs(v - setpoint) < 0.1 for v in got), (setpoint, [round(v, 3) for v in got])

    trip_map = LoopInputMap(mv_per_unit=1000.0, range_min=0.0, range_max=5.0, trip=3.5)
    with pod.control_loop(curve=build_constant_curve(int(4.5 / hi * 65535)), k=8192, source="adc",
                          input_map=trip_map) as loop:
        time.sleep(0.5)
        pt = loop.probe()
        assert volts(pt.i) < 0.5, f"a tripped loop must park at vmin, the SMA reads {volts(pt.i):.2f} V"
        if pt.tripped is not None:                        # firmware that reports the trip
            assert pt.tripped, pt
            assert pod.status().get("loop_tripped") is True


def test_deep_replay_plays_while_capturing(deep_image):
    pytest.importorskip("numpy")
    pod = deep_image
    pod.analog_path("cal1")
    period = [0.8] * 100 + [2.2] * 100                 # 100 Hz at 20 kS/s
    with pod.replay(period * 41, dac_path="5v", sample_rate_hz=20_000, route=False) as h:
        time.sleep(0.5)
        adc = pod.capture_adc(8192, sample_rate_hz=20_000)
    assert h.deep
    assert abs(adc.dominant_frequency() - 100) < 5 and p2p(adc) > 800


def test_fpga_image_round_trip(pod):
    started_on_loop = pod.refresh_capabilities().dac_control_loop
    other = FpgaImage.DEEP_REPLAY if started_on_loop else FpgaImage.LOOP
    back = FpgaImage.LOOP if started_on_loop else FpgaImage.DEEP_REPLAY
    info = pod.fpga_image(other)
    try:
        assert isinstance(info, FpgaImageInfo) and info.image == int(other)
        caps = pod.capabilities
        assert caps.dac_deep_replay if other == FpgaImage.DEEP_REPLAY else caps.dac_control_loop
        assert len(pod.capture_la(4096, sample_rate_hz=1_000_000)) == 4096  # PSRAM path healthy
    finally:
        assert pod.fpga_image(back).image == int(back)


# -- digital -------------------------------------------------------------------------------

def test_bias_resistors_are_direction_aware(pod, bench):
    states = {la: pod.pull_state(la) for la in range(1, 9)}
    assert [states[la].direction for la in range(1, 9)] == ["up"] * 6 + ["down"] * 2
    ch = bench.free_pull_la
    before = states[ch].enabled
    try:
        assert pod.set_pull(ch, not before).enabled == (not before)
        assert (ch in pod.enabled_pulls()) == (not before)
    finally:
        pod.set_pull(ch, before)


def test_i2c_sensor_emulation(pod, bench):
    pod.enable_i2c_sensor(sda=bench.i2c_sda, scl=bench.i2c_scl, temperature_c=21.0, pressure_pa=100_000)
    try:
        assert pod.i2c_sensor_regs(start=0xD0, length=1) == [0x58]
        before_regs = pod.i2c_sensor_regs(start=0xFA, length=3)
        pod.set_i2c_sensor(temperature_c=30.0)
        assert pod.i2c_sensor_status()["active"]
        # A changed setting must reach the emulator's register file: the BMP280 temperature lives
        # in 0xFA..0xFC, so the raw registers must differ after set_i2c_sensor.
        assert pod.i2c_sensor_regs(start=0xFA, length=3) != before_regs, \
            "set_i2c_sensor did not change the emulated temperature registers"
        assert isinstance(pod.i2c_sensor_capture(1024, sample_rate_hz=1_000_000), list)
    finally:
        pod.disable_i2c_sensor()
    assert not pod.i2c_sensor_status().get("active")


def test_uart_session_on_unwired_channels(pod, bench):
    rx, tx = bench.free_la
    with pod.open_uart(rx=rx, tx=tx) as uart:
        uart.write("ping\r\n")
        uart.read(timeout=0.3)
        assert not uart.closed
        assert uart.read_until("never-appears", timeout=0.1) is None
        uart.read()
    assert pod.ping()  # commands work again once the session is closed


def test_can_loopback_responder_and_typed_reads(pod):
    if pod.capabilities.board != "stm32h563":
        pytest.skip("CAN is on the STM32H563 pod")
    with pod.open_can(bitrate=500_000, mode="internal") as can:
        can.write(0x123, [1, 2, 3])
        assert can.expect(can_id=0x123, timeout=1.0).data == bytes([1, 2, 3])
        can.add_responder(0x7DF, 0x7E8, [0x41, 0x0C])
        can.write(0x7DF, [0x02, 0x01, 0x0C])
        assert can.expect(can_id=0x7E8, timeout=1.0).data == bytes([0x41, 0x0C])
        pod.can_write(0x55, [9])
        time.sleep(0.05)
        result = pod.can_read(max_frames=8)
        assert isinstance(result, CanReadResult) and any(f.id == 0x55 for f in result.frames)
