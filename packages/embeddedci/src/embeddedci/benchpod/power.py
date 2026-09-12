"""Power profiles: the DUT's supply current and voltage over time, with average, peak and energy.

The pod samples the target-power rail's INA238 monitor a few hundred times a second — ask for
100-500 Hz and it delivers what its sampling loop allows, flattening near 365 Hz (``rate_hz`` in the
result is the measured rate, ``adc_rate_hz`` the sensor's configured conversion rate). Each sample
averages its whole conversion window and carries its own timestamp, so energy and charge are
integrated over real time rather than estimated from snapshots; a load that switches faster than a
few milliseconds is averaged, not resolved.

Rail limits: the internal rail (eFuse 1, 5 V) trips at about 2.0 A and the external rail (eFuse 2,
5-20 V) at about 3.0 A; a trip shorter than a sample does not show in the profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterable, Optional, Tuple

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

    efuse: int
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
    def avg_power(self) -> float:
        """Average power in watts (energy over duration)."""
        return self.energy / self.duration if self.duration > 0 else 0.0

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
        return cls(
            efuse=int(stats.get("efuse", 0) or 0), rate_hz=float(stats.get("rate_hz", 0) or 0),
            adc_rate_hz=float(stats.get("adc_rate_hz", stats.get("rate_hz", 0)) or 0),
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
        """``{"running", "efuse", "elapsed_ms", "n"}`` from the pod."""
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
