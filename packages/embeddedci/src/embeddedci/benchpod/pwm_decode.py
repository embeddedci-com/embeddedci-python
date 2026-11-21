"""PWM and pulse measurement: one row per period, and a summary per channel.

The Python mirror of the EmbeddedCI server's ``decode/pwm.go`` (held to it by
``pwm_decode_vectors.json``). A period runs from a rising edge to the next. 1-2 sample pulses on a
signal whose typical pulse spans 20 samples or more are glitches, folded into the level around
them. With ``servo=True`` the high pulses also read as servo positions (1 ms = 0 %, 2 ms = 100 %).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ._gojson import go_dict

MAX_PERIODS = 10000


@dataclass
class PwmPeriod:
    index: int
    start_us: float
    period_us: float
    freq_hz: float
    high_us: float
    low_us: float
    duty: float


@dataclass
class PwmStat:
    min: float = 0.0
    max: float = 0.0
    mean: float = 0.0


@dataclass
class PwmServo:
    pulse_ms: PwmStat
    position_pct: PwmStat


@dataclass
class PwmChannel:
    channel: int
    count: int = 0
    level: Optional[int] = None
    freq_hz: PwmStat = field(default_factory=PwmStat)
    period_us: PwmStat = field(default_factory=PwmStat)
    duty: PwmStat = field(default_factory=PwmStat)
    high_us: PwmStat = field(default_factory=PwmStat)
    low_us: PwmStat = field(default_factory=PwmStat)
    jitter_us: float = 0.0
    glitches: int = 0
    servo: Optional[PwmServo] = None
    periods: List[PwmPeriod] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict:
        return go_dict(self, omit_none=("level", "servo"))


@dataclass
class PwmDecode:
    """``channels``: one :class:`PwmChannel` per channel measured."""

    channels: List[PwmChannel] = field(default_factory=list)
    protocol: str = "pwm"

    def __len__(self) -> int:
        return len(self.channels)

    def as_dict(self) -> dict:
        return {"protocol": "pwm", "channels": [c.as_dict() for c in self.channels]}


def _stat(v: List[float]) -> PwmStat:
    if not v:
        return PwmStat()
    s = 0.0
    for x in v:
        s += x
    return PwmStat(min(v), max(v), s / len(v))


def decode_pwm(words: Sequence[int], *, channels: Sequence[int], sample_rate_hz: float,
               servo: bool = False) -> PwmDecode:
    """Measure each of ``channels`` (1-based); ``servo`` adds servo pulse widths and positions."""
    if not channels:
        raise ValueError("pwm needs one or more channels in 1..14")
    if sample_rate_hz <= 0:
        raise ValueError("pwm needs a positive sample rate")
    res = PwmDecode()
    for ch in channels:
        if not 1 <= ch <= 14:
            raise ValueError(f"pwm channel {ch} is not in 1..14")
        res.channels.append(_channel([(w >> (ch - 1)) & 1 for w in words], ch, sample_rate_hz, servo))
    return res


def _channel(line: List[int], ch: int, rate: float, servo: bool) -> PwmChannel:
    c = PwmChannel(ch)
    runs: List[list] = []  # [level, start, n]
    for i, v in enumerate(line):
        if runs and runs[-1][0] == v:
            runs[-1][2] += 1
        else:
            runs.append([v, i, 1])
    if len(runs) > 2:
        ns = sorted(r[2] for r in runs[1:-1])
        if ns[len(ns) // 2] >= 20:
            out: List[list] = []
            k = 0
            while k < len(runs):
                r = runs[k]
                if r[2] <= 2 and out and k + 1 < len(runs):
                    c.glitches += 1
                    out[-1][2] += r[2] + runs[k + 1][2]
                    k += 2
                    continue
                out.append(r)
                k += 1
            runs = out
    us = 1e6 / rate
    per: List[float] = []
    hi: List[float] = []
    lo: List[float] = []
    duty: List[float] = []
    freq: List[float] = []
    for k in range(1, len(runs) - 1):
        if runs[k][0] != 1 or runs[k + 1][0] != 0 or k + 2 >= len(runs):
            continue
        h = runs[k][2] * us
        lw = runs[k + 1][2] * us
        p = h + lw
        per.append(p)
        hi.append(h)
        lo.append(lw)
        duty.append(h / p)
        freq.append(1e6 / p)
        if len(c.periods) < MAX_PERIODS:
            c.periods.append(PwmPeriod(len(c.periods) + 1, runs[k][1] * us, p, 1e6 / p, h, lw, h / p))
        else:
            c.truncated = True
    c.count = len(per)
    if not per:
        if line:
            c.level = line[-1]
        return c
    c.period_us, c.high_us, c.low_us, c.duty, c.freq_hz = _stat(per), _stat(hi), _stat(lo), _stat(duty), _stat(freq)
    sq = 0.0
    for p in per:
        sq += (p - c.period_us.mean) * (p - c.period_us.mean)
    c.jitter_us = math.sqrt(sq / len(per))
    if servo:
        ms = [h / 1000 for h in hi]
        c.servo = PwmServo(_stat(ms), _stat([(m - 1) * 100 for m in ms]))
    return c
