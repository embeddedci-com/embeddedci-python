"""Power profiles: the DUT's supply current and voltage over time, with average, peak and energy.

The pod samples the target-power supply's INA238 monitor a few hundred times a second — ask for
100-500 Hz and it delivers what its sampling loop allows, flattening near 365 Hz (``rate_hz`` in the
result is the measured rate, ``adc_rate_hz`` the sensor's configured conversion rate). Each sample
averages its whole conversion window and carries its own timestamp, so energy and charge are
integrated over real time rather than estimated from snapshots; a load that switches faster than a
few milliseconds is averaged, not resolved.

Supply limits: the ``"5v"`` supply (eFuse 1) trips at about 2.0 A and the ``"external"`` supply (eFuse 2,
2.7-20 V) at about 3.0 A; a trip shorter than a sample does not show in the profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .constants import coerce_supply, supply_efuse, warn_efuse
from .errors import BenchPodError

if TYPE_CHECKING:  # pragma: no cover
    from .client import BenchPod


@dataclass(frozen=True)
class PowerProfile:
    """The result of :meth:`BenchPod.measure_power` or a :meth:`BenchPod.power_profile` session.

    Currents are amps, voltages volts, ``energy`` joules, ``charge`` coulombs, ``duration`` seconds.
    Statistics cover every raw sample; ``samples`` holds ``(t, current, voltage)`` points averaged down
    to the ``keep_samples`` that were asked for.
    """

    #: The supply that was profiled (``"5v"`` or ``"external"``), or ``None`` for an external INA
    #: monitor (then :attr:`monitor` names it). Exactly one of the two is set.
    supply: Optional[str]
    #: The external INA monitor that was profiled (its name), else ``None``.
    monitor: Optional[str]
    #: Samples per second actually delivered (measured: ``n`` over ``duration``). The pod reads one
    #: sensor register per firmware pass, so this lands below the rate you asked for — roughly
    #: 350-450 Hz. Each sample carries its own timestamp, so the trace stays exact either way.
    rate_hz: float
    #: The conversion rate the current sensor was configured for — the ceiling ``rate_hz`` works
    #: towards, not what arrived.
    adc_rate_hz: float
    n: int
    duration: float
    avg_current: float
    min_current: float
    peak_current: float
    avg_voltage: float
    min_voltage: float
    max_voltage: float
    energy: float
    charge: float
    #: The eFuse reported a fault (over-current / short) during the profile.
    fault: bool = False
    #: The sampler stopped at its ``max_duration`` rather than on request.
    truncated: bool = False
    samples: Tuple[Tuple[float, float, float], ...] = ()
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def efuse(self) -> Optional[int]:
        """Deprecated: :attr:`supply`. ``1`` for ``"5v"``, ``2`` for ``"external"``, ``None`` for a
        monitor profile."""
        warn_efuse("PowerProfile.efuse")
        return supply_efuse(self.supply) if self.supply is not None else None

    @property
    def avg_power(self) -> float:
        """Average power in watts (energy over duration)."""
        return self.energy / self.duration if self.duration > 0 else 0.0

    @classmethod
    def from_readings(cls, readings: Sequence[Tuple[float, Any]], *, adc_rate_hz: float = 0.0,
                      keep_samples: int = 0, truncated: bool = False,
                      monitor: str = "") -> "PowerProfile":
        """A profile from an external monitor's ``(t seconds, CurrentReading)`` pairs
        (:meth:`BenchPod.measure_power(monitor=...) <embeddedci.benchpod.BenchPod.measure_power>` and
        :meth:`BenchPod.power_profile(monitor=...) <embeddedci.benchpod.BenchPod.power_profile>`).
        ``monitor`` names the monitor (:attr:`PowerProfile.monitor`); energy and charge integrate the readings over their timestamps."""
        if not readings:
            raise BenchPodError("the current monitor returned no readings")
        ts = [float(t) for t, _ in readings]
        amps = [r.current for _, r in readings]
        volts = [r.bus_voltage for _, r in readings]
        n = len(readings)
        dur = ts[-1] - ts[0]
        charge = energy = 0.0
        for i in range(1, n):
            dt = ts[i] - ts[i - 1]
            charge += (amps[i] + amps[i - 1]) / 2 * dt
            energy += (amps[i] * volts[i] + amps[i - 1] * volts[i - 1]) / 2 * dt
        if dur <= 0:   # one reading: no span to integrate over
            dur = 0.0
        trace: List[Tuple[float, float, float]] = list(zip(ts, amps, volts))
        if keep_samples and n > keep_samples:
            step = n / keep_samples
            binned = []
            for b in range(keep_samples):
                lo, hi = int(b * step), max(int((b + 1) * step), int(b * step) + 1)
                part = trace[lo:hi]
                binned.append(tuple(sum(p[k] for p in part) / len(part) for k in range(3)))
            trace = binned  # type: ignore[assignment]
        elif not keep_samples:
            trace = []
        return cls(supply=None, monitor=monitor or "external", rate_hz=(n - 1) / dur if dur > 0 else 0.0, adc_rate_hz=adc_rate_hz, n=n,
                   duration=dur, avg_current=charge / dur if dur > 0 else amps[0],
                   min_current=min(amps), peak_current=max(amps), avg_voltage=sum(volts) / n,
                   min_voltage=min(volts), max_voltage=max(volts), energy=energy, charge=charge,
                   truncated=truncated, samples=tuple(trace))

    @classmethod
    def from_chunks(cls, chunks: Iterable[Dict[str, Any]]) -> "PowerProfile":
        """Assemble a profile from the pod's chunked ``power_profile`` reply."""
        t_us: list = []
        ua: list = []
        mv: list = []
        stats: Optional[Dict[str, Any]] = None
        for chunk in chunks:
            body = chunk.get("data") if isinstance(chunk.get("data"), dict) else chunk
            t_us.extend(body.get("t_us") or [])
            ua.extend(body.get("current_ua") or [])
            mv.extend(body.get("bus_mv") or [])
            if isinstance(body.get("stats"), dict):
                stats = body["stats"]
        if stats is None:
            raise BenchPodError("the pod's power_profile reply carried no statistics")
        samples = tuple((t / 1e6, i / 1e6, v / 1000.0) for t, i, v in zip(t_us, ua, mv))
        if stats.get("supply"):
            supply = coerce_supply(stats["supply"])
        elif stats.get("efuse"):
            supply = coerce_supply(int(stats["efuse"]))
        else:
            supply = None
        return cls(
            supply=supply, monitor=None,
            rate_hz=float(stats.get("sample_rate_hz", 0) or 0),
            adc_rate_hz=float(stats.get("adc_sample_rate_hz", stats.get("sample_rate_hz", 0)) or 0),
            n=int(stats.get("n", 0) or 0), duration=float(stats.get("duration_ms", 0) or 0) / 1000.0,
            avg_current=_ua(stats, "avg_ua"), min_current=_ua(stats, "min_ua"),
            peak_current=_ua(stats, "peak_ua"), avg_voltage=_mv(stats, "avg_mv"),
            min_voltage=_mv(stats, "min_mv"), max_voltage=_mv(stats, "max_mv"),
            energy=float(stats.get("energy_uj", 0) or 0) / 1e6,
            charge=float(stats.get("charge_uc", 0) or 0) / 1e6,
            fault=bool(stats.get("fault", False)), truncated=bool(stats.get("truncated", False)),
            samples=samples, raw=dict(stats))


def _ua(stats: Dict[str, Any], key: str) -> float:
    return float(stats.get(key, 0) or 0) / 1e6


def _mv(stats: Dict[str, Any], key: str) -> float:
    return float(stats.get(key, 0) or 0) / 1000.0


class PowerProfileSession:
    """A running power profile; use it as a context manager (from :meth:`BenchPod.power_profile`)::

        with bp.power_profile(keep_samples=2000) as prof:
            bp.signal("TRIGGER").pulse(0.001)      # start an inference
            time.sleep(0.5)
        print(prof.result.avg_current, prof.result.peak_current, prof.result.energy)
    """

    def __init__(self, pod: "BenchPod", request: Dict[str, Any]) -> None:
        self._pod = pod
        self._request = request
        self._result: Optional[PowerProfile] = None
        self._started = False

    def start(self) -> "PowerProfileSession":
        """Start sampling (the context manager calls this)."""
        self._pod.command(dict(self._request, action="start"))
        self._started = True
        self._result = None
        return self

    def status(self) -> Dict[str, Any]:
        """``{"running", "supply", "elapsed_ms", "n"}`` from the pod (``"efuse"`` too, deprecated)."""
        reply = self._pod.command({"cmd": "power_profile", "action": "status"})
        return reply if isinstance(reply, dict) else {}

    def stop(self) -> PowerProfile:
        """Stop sampling and return the :class:`PowerProfile` (idempotent)."""
        if self._result is None:
            if not self._started:
                raise BenchPodError("the power profile was never started")
            self._result = PowerProfile.from_chunks(
                self._pod._power_profile_chunks({"cmd": "power_profile", "action": "stop"}))
        return self._result

    @property
    def result(self) -> PowerProfile:
        """The profile, once :meth:`stop` ran (the context manager stops on exit)."""
        if self._result is None:
            raise BenchPodError("the power profile is still running; call stop() or leave the with-block")
        return self._result

    def __enter__(self) -> "PowerProfileSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
