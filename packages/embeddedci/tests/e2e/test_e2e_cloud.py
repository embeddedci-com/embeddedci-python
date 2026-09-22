"""Tier "cloud": the pod through embeddedci.com — lease, command channel, byte tunnel, library.

    BENCHPOD_API_KEY=eci_… BENCHPOD_E2E_CLOUD_DEVICE=benchpod-v2.0.0 \
        pytest packages/embeddedci/tests/e2e/test_e2e_cloud.py

Run it on its own (not alongside a LAN session on the same pod).
"""

from __future__ import annotations

import os
import time

import pytest

from embeddedci.benchpod import BenchPod

# Not marked `hardware`: that gate wants --benchpod-connection, and this tier opens its own cloud
# connection (the `cloud` fixture skips without BENCHPOD_E2E_CLOUD_DEVICE + BENCHPOD_API_KEY).


@pytest.fixture(scope="module")
def cloud(benchpod_la_voltage):
    name = os.environ.get("BENCHPOD_E2E_CLOUD_DEVICE")
    if not name or not os.environ.get("BENCHPOD_API_KEY"):
        pytest.skip("set BENCHPOD_E2E_CLOUD_DEVICE and BENCHPOD_API_KEY to run the cloud tier")
    with BenchPod(f"embeddedci:{name}", la_voltage=benchpod_la_voltage, lease_wait=120) as pod:
        yield pod


def test_lease_and_status(cloud):
    assert cloud.leased
    assert cloud.status().get("board")
    assert cloud.get_la_voltage().voltage == 3.3


def test_commands_over_the_command_channel(cloud):
    assert cloud.power_status().internal.ok
    assert cloud.analog_path("cal1").path == "cal1"
    cloud.analog_path("off")


def test_captures_over_the_tunnel(cloud):
    """Length alone would pass on 2048 zeros — check the payload survived the tunnel intact."""
    adc = cloud.capture_adc(2048, sample_rate_hz=100_000)
    assert len(adc) == 2048
    assert adc.sample_rate_hz == pytest.approx(100_000, rel=0.05), adc.sample_rate_hz
    # Real ADC counts, not a zero-filled or truncated frame.
    assert all(0 <= c <= 65535 for c in adc.counts)
    assert len(set(adc.counts)) > 1, "every ADC sample identical: the tunnel delivered filler"

    la = cloud.capture_la(8192, sample_rate_hz=1_000_000)
    assert len(la) == 8192
    assert la.sample_rate_hz == pytest.approx(1_000_000, rel=0.05), la.sample_rate_hz
    assert all(0 <= w < 1 << la.channels for w in la.words), f"LA words must fit {la.channels} bits"


def test_commands_work_while_a_uart_session_holds_the_tunnel(cloud):
    """The point is that the command channel still answers while the tunnel is busy."""
    with cloud.open_uart(rx=9, tx=10) as uart:
        assert cloud.power_status().internal.ok
        # Nothing is wired to these channels, so the content is not the point — but the session
        # must stay usable and return promptly rather than raising or hanging.
        assert isinstance(uart.read(timeout=0.2), str)
        assert cloud.ping()                      # the command channel survived the tunnel read


def test_waveform_library_save_and_replay(cloud):
    cloud.analog_path("cal1")
    cloud.dac_output("5v", volts=1.5)
    cap = cloud.capture_adc(2048, sample_rate_hz=100_000)
    cloud.dac_output("off")
    wf = cloud.save_capture_as_recording(cap, f"e2e-{int(time.time())}")
    try:
        assert wf.is_recording and any(w.id == wf.id for w in cloud.waveforms.list())
        with cloud.replay_waveform(wf.id, dac_path="5v", target_samples=1024) as handle:
            assert handle.samples > 0
    finally:
        cloud.waveforms.delete(wf.id)
        cloud.dac_stop()


def test_top_la_channels_over_the_cloud(cloud):
    """LA13/LA14 (firmware 3.1+) through the command channel and the tunnel: drive, read, capture."""
    if len(cloud.la_pins()) < 14:
        pytest.skip("the pod firmware has 12 LA channels; LA13/LA14 need 3.1+")
    try:
        cloud.gpio(13, "output", level=1)
        cloud.gpio(14, "output", level=0)
        levels = cloud.pin_levels()
        assert (levels[13], levels[14]) == (1, 0)
        la = cloud.capture_la(4096, sample_rate_hz=1_000_000)
        assert la.duty_cycle(13) == 1.0 and la.duty_cycle(14) == 0.0
    finally:
        cloud.release_gpio(13, 14)
