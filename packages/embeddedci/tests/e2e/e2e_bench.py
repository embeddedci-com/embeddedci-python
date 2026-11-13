"""The bench profile: how the DUT is wired to the pod and which LA channels the tests may drive.

Read from ``BENCHPOD_E2E_*`` environment variables (README.md in this directory). No pytest
fixtures here, so the unit tests can check the parsing without a pod.

Some tests drive LA channels as outputs and need channels nothing is wired to. A bench that wires
every channel to the DUT sets those variables to ``none``; the tests that need them then skip
(the ``bench_needs`` marker, applied in conftest.py) instead of driving a line the DUT drives too,
or back-powering a DUT that is switched off through its I/O pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple

import pytest

PREFIX = "BENCHPOD_E2E_"

#: What each optional bench resource is, for the skip reason: (variables that set it, description).
NEEDS: Dict[str, Tuple[str, str]] = {
    "free_la": ("BENCHPOD_E2E_FREE_LA",
                "two LA channels nothing is wired to (the test drives them)"),
    "top_la": ("BENCHPOD_E2E_TOP_LA",
               "LA13/LA14 with nothing driving them (the test drives both)"),
    "free_pull_la": ("BENCHPOD_E2E_FREE_PULL_LA",
                     "a biased channel (LA1-LA6) nothing depends on (the test toggles its pull-up "
                     "and drives it)"),
    "free_pulldown_la": ("BENCHPOD_E2E_FREE_PULLDOWN_LA",
                         "LA7 or LA8 with nothing attached (the test toggles its pull-down and "
                         "drives it)"),
    "uart_sensor_tx": ("BENCHPOD_E2E_UART_SENSOR_TX or BENCHPOD_E2E_FREE_LA",
                       "a channel the pod may transmit on: a DUT UART RX line or a free channel"),
}


def bench_needs(*names: str) -> pytest.MarkDecorator:
    """Mark a test that uses these optional bench resources: it skips when the bench has none."""
    unknown = [n for n in names if n not in NEEDS]
    if unknown:
        raise ValueError(f"unknown bench resources {unknown}; known: {sorted(NEEDS)}")
    return pytest.mark.bench_needs(*names)


FREE_LA = bench_needs("free_la")
TOP_LA = bench_needs("top_la")
FREE_PULL_LA = bench_needs("free_pull_la")
FREE_PULLDOWN_LA = bench_needs("free_pulldown_la")
UART_SENSOR_TX = bench_needs("uart_sensor_tx")


@dataclass(frozen=True)
class Bench:
    """How the DUT is wired to the pod, and what the tests may drive.

    ``None`` in an optional channel field means the bench has no such channel (``none`` in its
    variable): the tests marked with ``bench_needs`` for it skip.
    """

    uart_rx: int          # LA channel wired to the DUT's TX (the pod samples it)
    uart_tx: int          # LA channel wired to the DUT's RX (the pod drives it)
    i2c_sda: int
    i2c_scl: int
    swclk: int
    swdio: int
    nreset: bool          # DUT reset wired to the pod's reset pin (rev3 pods only)
    efuse: int            # target-power rail the DUT is on
    target_cfg: str       # OpenOCD target config
    free_la: Optional[Tuple[int, int]]   # two LA channels nothing is wired to
    top_la: Optional[Tuple[int, int]]    # LA13/LA14 (firmware 3.1+), nothing driving them either
    free_pull_la: Optional[int]          # a biased channel (LA1-LA6) nothing depends on
    dac_max_v: float      # the highest voltage the tests drive on a DAC output
    allow_12v: bool       # whether the bipolar 12v output may be driven (±1 V)
    board_rev: str        # the PCB revision this bench's pod must report ("" = accept any)
    nrst_la: Optional[int]  # LA channel jumpered to the pod's reset pin (J1 pin 22), if any
    ext_loop: bool = False  # the DAC 0-5 V output is wired to the ADC's front SMA
    spi: Optional[Tuple[int, int, int, int]] = None   # SPI flash on (sck, mosi, miso, cs), or none
    free_pulldown_la: Optional[int] = 7  # a pulled-down channel (LA7/LA8) nothing is attached to
    uart_sensor_tx: Optional[int] = None  # where an emulated UART sensor transmits (a DUT RX line
    #                                       the pod drives, else the first free channel)


def _is_none(text: str) -> bool:
    return text.strip().lower() in ("none", "")


def parse_channels(name: str, raw: str, count: int,
                   allowed: Sequence[int] = range(1, 15)) -> Optional[Tuple[int, ...]]:
    """``count`` comma-separated LA channels from variable ``name``, or ``None`` for ``none``."""
    if _is_none(raw):
        return None
    try:
        channels = tuple(int(x) for x in raw.split(","))
    except ValueError:
        channels = ()
    if len(channels) != count or any(ch not in allowed for ch in channels) \
            or len(set(channels)) != count:
        want = "one LA channel" if count == 1 else f"{count} different LA channels, comma-separated,"
        raise ValueError(f"{PREFIX}{name}={raw!r}: want {want} out of "
                         f"{min(allowed)}-{max(allowed)}, or 'none'")
    return channels


def bench_from_env(environ: Mapping[str, str]) -> Bench:
    """The bench profile from ``BENCHPOD_E2E_*`` variables (defaults: the free-channel bench)."""

    def env(name: str, default: str) -> str:
        return environ.get(PREFIX + name, default)

    def one(name: str, default: str, allowed: Sequence[int] = range(1, 15)) -> Optional[int]:
        chans = parse_channels(name, env(name, default), 1, allowed)
        return chans[0] if chans else None

    free = parse_channels("FREE_LA", env("FREE_LA", "9,10"), 2)
    top = parse_channels("TOP_LA", env("TOP_LA", "13,14"), 2)
    sensor_tx = one("UART_SENSOR_TX", "")
    return Bench(
        uart_rx=int(env("UART_RX", "3")),
        uart_tx=int(env("UART_TX", "4")),
        i2c_sda=int(env("I2C_SDA", "2")),
        i2c_scl=int(env("I2C_SCL", "1")),
        swclk=int(env("SWCLK", "11")),
        swdio=int(env("SWDIO", "12")),
        nreset=env("NRESET", "0") == "1",
        efuse=int(env("EFUSE", "1")),
        target_cfg=env("TARGET_CFG", "target/stm32f4x.cfg"),
        free_la=free,  # type: ignore[arg-type]
        top_la=top,  # type: ignore[arg-type]
        free_pull_la=one("FREE_PULL_LA", "6", range(1, 7)),
        dac_max_v=float(env("DAC_MAX_V", "3.3")),
        allow_12v=env("ALLOW_12V", "0") == "1",
        board_rev=env("BOARD_REV", ""),
        nrst_la=int(env("NRST_LA", "0")) or None,
        ext_loop=env("EXT_LOOP", "0") == "1",
        spi=tuple(int(x) for x in env("SPI", "").split(",")) if env("SPI", "") else None,  # type: ignore[arg-type]
        free_pulldown_la=one("FREE_PULLDOWN_LA", "7", range(7, 9)),
        uart_sensor_tx=sensor_tx if sensor_tx is not None else (free[0] if free else None),
    )


def missing_reason(bench: Bench, names: Sequence[str]) -> Optional[str]:
    """Why a test that needs ``names`` cannot run on ``bench``, or ``None`` when it can."""
    missing = [n for n in names if getattr(bench, n) is None]
    if not missing:
        return None
    parts = [f"needs {NEEDS[n][1]}; set {NEEDS[n][0]}" for n in missing]
    return ("; ".join(parts)
            + " (this bench set it to 'none': it wires every LA channel to the DUT)")
