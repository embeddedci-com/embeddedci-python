"""embeddedci-openhtf — drive an EmbeddedCI BenchPod from OpenHTF.

An `OpenHTF <https://www.openhtf.com/>`_ plug (and a few phase helpers) that wrap
the :mod:`embeddedci` BenchPod SDK, for teams that want OpenHTF's test-sequencing
and record/GUI stack while connecting **directly** to a pod over TCP or serial —
no EmbeddedCI cloud account or web UI required.

    import openhtf as htf
    from embeddedci_openhtf import benchpod_plug, flash_phase, boot_banner_phase

    bench = benchpod_plug("192.168.1.50:8080")   # or "/dev/ttyACM0"

    test = htf.Test(
        flash_phase(bench, file="fw.elf", target="target/stm32f4x.cfg",
                    swclk=11, swdio=12, nreset=3),
        boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"),
    )
    test.execute(test_start=lambda: "SN-0001")
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .analog import (
    adc_capture_phase,
    loopback_measure_phase,
    measure,
    signal_generate,
    signal_generate_phase,
    signal_stop,
)
from .measurements import (
    flash_ok_measurement,
    record_flash,
    record_samples,
    record_uart,
    uart_matched_measurement,
)
from .phases import boot_banner_phase, flash_phase, power_phase
from .plug import BenchPodPlug, benchpod_plug, close_persistent_benchpods

try:
    __version__ = version("embeddedci-openhtf")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = [
    "BenchPodPlug",
    "benchpod_plug",
    "close_persistent_benchpods",
    # phase factories
    "power_phase",
    "flash_phase",
    "boot_banner_phase",
    "signal_generate_phase",
    "adc_capture_phase",
    "loopback_measure_phase",
    # analog low-level helpers
    "signal_generate",
    "signal_stop",
    "measure",
    # measurement helpers
    "flash_ok_measurement",
    "uart_matched_measurement",
    "record_flash",
    "record_uart",
    "record_samples",
    "__version__",
]
