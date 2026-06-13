"""Emulated I2C sensor helpers.

Thin wrappers over the pod's ``sensor_*`` JSON commands — the pod acts as an I2C
slave (currently a BMP280) on two LA channels so a DUT's I2C master can read it.
See ``bench-pod-firmware/docs/API.md`` ("Emulated I2C sensor").

These call ``transport.command``/``transport.samples`` directly, so they require
a transport that speaks JSON: TCP natively, serial via its ``json`` console mode.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

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


def sensor_start(transport, sensor, *, sda, scl, address: int = 0x76) -> dict:
    """Arm an emulated sensor on the given SDA/SCL LA channels."""
    command = _require_command(transport)
    sensor_name = sensor.value if isinstance(sensor, Sensor) else str(sensor)
    return command({
        "cmd": "sensor_start",
        "type": sensor_name,
        "addr": hex(int(address)),
        "sda": coerce_pin(sda, "sda"),
        "scl": coerce_pin(scl, "scl"),
    })


def sensor_set(transport, *, temperature_c: Optional[float] = None,
               pressure_pa: Optional[float] = None) -> dict:
    """Set what the emulated sensor reports. At least one value is required."""
    command = _require_command(transport)
    if temperature_c is None and pressure_pa is None:
        raise BenchPodError("sensor_set needs temperature_c and/or pressure_pa")
    req: dict = {"cmd": "sensor_set"}
    if temperature_c is not None:
        req["temperature_c"] = float(temperature_c)
    if pressure_pa is not None:
        req["pressure_pa"] = float(pressure_pa)
    return command(req)


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
            "I2C sensor emulation is only available on the TCP transport"
        )
    return transport_samples({
        "cmd": "sensor_regs", "start": hex(int(start)), "len": int(length),
    })


def sensor_la(transport, samples: int = 256,
              sample_rate_mhz: Optional[float] = None) -> List[int]:
    """Raw I2C-bus logic capture (packed bytes; 4 {SCL,SDA} samples each)."""
    transport_samples = getattr(transport, "samples", None)
    if transport_samples is None:
        raise BenchPodError(
            "I2C sensor emulation is only available on the TCP transport"
        )
    req: dict = {"cmd": "sensor_la", "samples": int(samples)}
    if sample_rate_mhz is not None:
        req["sample_rate_mhz"] = float(sample_rate_mhz)
    return transport_samples(req)
