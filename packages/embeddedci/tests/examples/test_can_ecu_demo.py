"""CAN HIL demo: the BenchPod as a live CAN test peer.

This shows the two ways to use the pod's CAN interface for hardware testing, and
what each needs on the bench.

──────────────────────────────────────────────────────────────────────────────
 A. ECU SIMULATION (the pod ANSWERS the DUT)         — runs now, no second node
──────────────────────────────────────────────────────────────────────────────
The firmware autonomous responder makes the pod reply to a matching request on
its own, from the RX interrupt — microsecond latency, no host round-trip. So the
pod can stand in for an ECU that your DUT talks to (e.g. answer a UDS/diagnostic
request, or a sensor poll). Here we validate it in *internal loopback* (the pod's
own request stands in for the DUT's), which needs nothing wired to the bus.

With a real DUT attached (see part C), the SAME `simulate_ecu(...)` call makes the
pod answer the DUT's requests on the wire.

──────────────────────────────────────────────────────────────────────────────
 B. RATE / PERIOD ASSERTIONS                          — runs now, no second node
──────────────────────────────────────────────────────────────────────────────
Received frames carry the pod's ISR-stamped timestamp (ms), so you can assert a
node broadcasts at a given cadence. Validated here by replaying a known cadence
through loopback.

──────────────────────────────────────────────────────────────────────────────
 C. OBSERVE / STIMULATE A REAL DUT (`normal` mode)   — needs a DUT on the bus
──────────────────────────────────────────────────────────────────────────────
Wire the DUT's CAN transceiver to the pod's CAN+/CAN- screw terminal, terminate
both ends (the pod's end via `term=True` → its switchable 120 Ω), match the
bitrate, and configure `mode="normal"`. Then the pod ACKs the DUT and you assert
on the DUT's real traffic. Gated on BENCHPOD_CAN_DUT=1 so it skips until you have
that hardware set up.

Run (parts A + B, no extra hardware):
    pytest --benchpod-connection=192.168.1.215:8080 tests/examples/test_can_ecu_demo.py

Run part C too (real DUT wired):
    BENCHPOD_CAN_DUT=1 pytest --benchpod-connection=... tests/examples/test_can_ecu_demo.py
"""

import os

import pytest

from embeddedci.benchpod import BenchPod


def _requires_can(bp: BenchPod) -> None:
    board = str(bp.status().get("board", "")).lower()
    if "stm32" not in board:
        pytest.skip(f"CAN (FDCAN1) API is STM32H563-only; board is {board!r}")


# ── A. ECU simulation ────────────────────────────────────────────────────────

def test_pod_simulates_an_ecu(benchpod: BenchPod):
    """The pod answers a diagnostic request on its own — no host in the loop.

    Models a UDS 'read data by identifier' exchange: request 0x7DF → the ECU
    (here the pod) replies on 0x7E8. Validated in internal loopback; against a
    real DUT the same rule answers the DUT's request on the wire.
    """
    _requires_can(benchpod)
    with benchpod.open_can(bitrate=500_000, mode="internal") as can:
        # The pod now behaves as an ECU: several request→reply rules at once.
        can.simulate_ecu({
            0x7DF: (0x7E8, [0x62, 0x01, 0x00, 0x2A]),   # read DID 0x0100 -> 0x2A
            0x123: (0x124, [0xAA, 0xBB]),               # app-specific command
        })

        # Stand in for the DUT issuing the request (in loopback this echoes to RX,
        # which triggers the responder). On a real bus the DUT sends this.
        can.write(0x7DF, [0x03, 0x22, 0x01, 0x00])
        reply = can.expect(can_id=0x7E8, timeout=1.0)
        assert reply.data == b"\x62\x01\x00\x2a"

        can.write(0x123, [0x01])
        assert can.expect(can_id=0x124, timeout=1.0).data == b"\xaa\xbb"

        # The firmware counts how many auto-replies it emitted.
        assert can.status()["responder_hits"] >= 2


# ── B. rate / period assertions ──────────────────────────────────────────────

def test_frame_timestamps_track_elapsed_time(benchpod: BenchPod):
    """Received frames carry the pod's ISR-stamped uptime (ms), so you can reason
    about timing. We prove it *differentially*: adding a fixed sleep between two
    frames shows up as a larger ts gap.

    NB: in loopback the host is BOTH source and sink, so per-command overhead
    dominates the injection cadence — you can't assert a tight period on
    self-injected frames. Against a real DUT that broadcasts autonomously the pod
    stamps each frame at RX regardless of when you poll, so
    ``can.assert_periodic(id, period=0.1, tol=0.2)`` on its heartbeat is exact
    (that's what part C does).
    """
    _requires_can(benchpod)
    import time

    with benchpod.open_can(bitrate=500_000, mode="internal") as can:
        def stamp():
            can.write(0x100, [0x01])
            return can.expect(can_id=0x100, timeout=1.0).ts

        a, b, c = stamp(), stamp(), stamp()   # back-to-back (overhead only)
        time.sleep(0.3)
        d = stamp()                           # + 300 ms

        assert a > 0 and b > a and c > b and d > c     # running + monotonic
        baseline = c - b
        with_sleep = d - c
        assert with_sleep - baseline >= 150, (baseline, with_sleep)


# ── C. observe / stimulate a real DUT (needs hardware) ───────────────────────

@pytest.mark.skipif(os.environ.get("BENCHPOD_CAN_DUT") != "1",
                    reason="set BENCHPOD_CAN_DUT=1 with a real CAN DUT wired to CAN+/CAN-")
def test_observe_real_dut(benchpod: BenchPod):
    """TEMPLATE for a real DUT on the bus (edit ids/payloads for your device).

    Wiring: DUT CANH/CANL ↔ pod CAN+/CAN-, both ends terminated. The pod enables
    its own 120 Ω via term=True.
    """
    _requires_can(benchpod)
    with benchpod.open_can(bitrate=500_000, mode="normal", term=True) as can:
        # 1. The DUT should broadcast a heartbeat at a known rate.
        can.assert_periodic(0x100, period=0.1, tol=0.25, min_count=5)

        # 2. Request/response: stimulate the DUT and assert its reply.
        can.write(0x7DF, [0x02, 0x10, 0x03])
        reply = can.expect(can_id=0x7E8, timeout=0.5)
        assert reply.data[0] == 0x50

        # 3. The link should be healthy (a lone/ miswired node goes bus-off).
        st = can.status()
        assert not st["bus_off"], f"bus-off (tec={st['tec']} rec={st['rec']}) — check wiring/term/bitrate"
