"""Emulated I2C sensor helpers.

Thin wrappers over the pod's ``sensor_*`` JSON commands: the pod acts as an I2C
target (a BMP280, BME280, SHT4x or MPU-6050) on two LA channels so a DUT's I2C
controller can read it.
See ``bench-pod-firmware/docs/API.md`` ("Emulated I2C sensor").

These call ``transport.command``/``transport.samples`` directly, so they require
a transport that speaks JSON: TCP and the cloud natively, serial via its ``json``
console mode.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from .constants import Sensor, coerce_pin
from .errors import BenchPodError
from .transport.base import Transport


def _require_command(transport: Transport) -> Callable[[dict], Any]:
    fn = getattr(transport, "command", None)
    if fn is None:
        raise BenchPodError(
            "I2C sensor emulation needs a transport with JSON command support"
        )
    return fn


def sensor_start(transport, sensor, *, sda, scl, address: Optional[int] = None) -> dict:
    """Arm an emulated sensor on the given SDA/SCL LA channels.

    ``address`` None leaves the model's default address (``sensor_types`` lists it).
    """
    command = _require_command(transport)
    sensor_name = sensor.value if isinstance(sensor, Sensor) else str(sensor)
    req: dict = {
        "cmd": "sensor_start",
        "type": sensor_name,
        "sda": coerce_pin(sda, "sda"),
        "scl": coerce_pin(scl, "scl"),
    }
    if address is not None:
        req["addr"] = hex(int(address))
    return command(req)


def sensor_set(transport, values: Optional[Mapping[str, float]] = None, *,
               temperature_c: Optional[float] = None,
               pressure_pa: Optional[float] = None) -> dict:
    """Set what the emulated sensor reports: any of the active model's parameters.

    ``values`` maps parameter keys (``"humidity_pct"``, ``"accel_z_g"``, ...; ``sensor_types``
    lists them per model) to numbers; ``temperature_c``/``pressure_pa`` are shorthands. At least
    one value is required. The pod checks every value before applying any.
    """
    command = _require_command(transport)
    req: Dict[str, Any] = {"cmd": "sensor_set"}
    for key, value in (values or {}).items():
        if value is not None:
            req[str(key)] = float(value)
    if temperature_c is not None:
        req["temperature_c"] = float(temperature_c)
    if pressure_pa is not None:
        req["pressure_pa"] = float(pressure_pa)
    if len(req) == 1:
        raise BenchPodError("sensor_set needs at least one value (sensor_types lists each model's)")
    return command(req)


def sensor_types(transport) -> List[Dict[str, Any]]:
    """The models the pod emulates: type, label, addresses and the parameters ``sensor_set`` takes."""
    reply = _require_command(transport)({"cmd": "sensor_types"})
    types = reply.get("types") if isinstance(reply, dict) else None
    return list(types or [])


def sensor_stop(transport) -> None:
    """Disarm the emulated sensor (safe even if none is active)."""
    _require_command(transport)({"cmd": "sensor_stop"})


def sensor_status(transport) -> dict:
    """Return the sensor + I2C-bus activity status."""
    return _require_command(transport)({"cmd": "sensor_status"})


def sensor_regs(transport, start: int = 0, length: int = 256) -> List[int]:
    """Read ``length`` bytes of the emulated register image from ``start``."""
    transport_samples = getattr(transport, "samples", None)
    if transport_samples is None:
        raise BenchPodError(
            "reading the sensor register image needs a transport with chunked JSON replies"
        )
    return transport_samples({
        "cmd": "sensor_regs", "start": hex(int(start)), "len": int(length),
    })


def sensor_la(transport, samples: int = 1024,
              sample_rate_hz: Optional[float] = None) -> List[int]:
    """Raw capture of the emulated sensor's I2C bus (packed bytes; 4 {SCL,SDA} samples each)."""
    transport_samples = getattr(transport, "samples", None)
    if transport_samples is None:
        raise BenchPodError(
            "capturing the sensor bus needs a transport with chunked JSON replies"
        )
    if int(samples) <= 0:
        raise ValueError(f"samples must be > 0, got {samples!r}")
    req: dict = {"cmd": "sensor_la", "samples": int(samples)}
    if sample_rate_hz is not None:
        if sample_rate_hz <= 0:
            raise ValueError(f"sample_rate_hz must be > 0, got {sample_rate_hz!r}")
        req["sample_rate_mhz"] = float(sample_rate_hz) / 1e6
    return transport_samples(req)
