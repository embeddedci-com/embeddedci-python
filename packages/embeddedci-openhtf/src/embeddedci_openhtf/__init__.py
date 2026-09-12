"""embeddedci-openhtf — drive an EmbeddedCI BenchPod from OpenHTF.

An `OpenHTF <https://www.openhtf.com/>`_ plug (and phase helpers) that wrap the
:mod:`embeddedci` BenchPod SDK, for teams that want OpenHTF's test-sequencing
and record/GUI stack while connecting **directly** to a pod over TCP or serial —
no EmbeddedCI cloud account or web UI required.

    import openhtf as htf
    from embeddedci_openhtf import benchpod_plug, flash_phase, boot_banner_phase

    bench = benchpod_plug("192.168.1.50:8080", la_voltage=3.3)   # or "/dev/ttyACM0"

    test = htf.Test(
        flash_phase(bench, file="fw.elf", target="target/stm32f4x.cfg",
                    swclk=11, swdio=12, nreset=True),
        boot_banner_phase(bench, rx=1, tx=2, expect="APP_OK"),
    )
    test.execute(test_start=lambda: "SN-0001")

Units follow the SDK: volts, seconds and hertz.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .analog import (
    adc_capture,
    adc_capture_phase,
    adc_read,
    adc_read_phase,
    analog_path,
    control_loop,
    control_loop_phase,
    dac_output,
    dac_output_phase,
    dac_replay_phase,
    fpga_image,
    loopback_measure_phase,
    replay,
    replay_waveform,
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
from .pins import (
    gpio,
    gpio_phase,
    la_delay,
    la_delay_phase,
    read_gpio,
    release_gpio,
    set_gpio,
)
from .plug import BenchPodPlug, benchpod_plug, close_persistent_benchpods
from .power import measure_power, measure_power_phase

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
    "dac_output_phase",
    "adc_read_phase",
    "adc_capture_phase",
    "loopback_measure_phase",
    "control_loop_phase",
    "dac_replay_phase",
    "gpio_phase",
    "measure_power_phase",
    "la_delay_phase",
    # pin + power helpers
    "gpio",
    "set_gpio",
    "read_gpio",
    "release_gpio",
    "measure_power",
    "la_delay",
    # analog low-level helpers
    "signal_generate",
    "signal_stop",
    "analog_path",
    "dac_output",
    "adc_read",
    "adc_capture",
    "replay",
    "replay_waveform",
    "control_loop",
    "fpga_image",
    # measurement helpers
    "flash_ok_measurement",
    "uart_matched_measurement",
    "record_flash",
    "record_uart",
    "record_samples",
    "__version__",
]
