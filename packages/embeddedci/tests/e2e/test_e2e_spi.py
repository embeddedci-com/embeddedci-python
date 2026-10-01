"""Tier "spi": the SPI master and SPI NOR flash programming (gateware v45+, capability spi_master).

    BENCHPOD_E2E_SPI=13,14,6,5 pytest packages/embeddedci/tests/e2e/test_e2e_spi.py --benchpod-connection=<pod host>

Needs a 25-series SPI NOR flash (W25Q, MX25, GD25, XT25, ...) wired to the pod: BENCHPOD_E2E_SPI
gives its SCK, MOSI (the chip's DI), MISO (DO) and CS as LA channels, powered at the LA voltage,
/WP and /HOLD tied high. Skips without it. The tests erase and write the 64 KB block at 0x10000.

Two gates. Without BENCHPOD_E2E_SPI the whole tier skips. With it, the tests that need the chip
first ask for its JEDEC ID and skip when nothing answers, so a bench whose flash was taken off
still passes. BENCHPOD_E2E_SPI_REQUIRED=1 turns that skip into a failure, for a bench where the
chip must be there. The pin-ownership test needs no chip and always runs.
"""

from __future__ import annotations

import os

import pytest

from embeddedci.benchpod import FirmwareError, PinConflictError

pytestmark = pytest.mark.hardware

BASE = 0x10000


@pytest.fixture(scope="module")
def spi_pins(pod, bench):
    if bench.spi is None:
        pytest.skip("set BENCHPOD_E2E_SPI=sck,mosi,miso,cs to run the SPI flash tier")
    if not pod.capabilities.spi_master:
        pytest.skip("the pod has no SPI master (gateware v45+, capability spi_master)")
    sck, mosi, miso, cs = bench.spi
    return dict(sck=sck, mosi=mosi, miso=miso, cs=cs)


@pytest.fixture(scope="module")
def spi_flash(pod, spi_pins):
    """The SPI pins, once a flash chip has answered on them. Asked once per run."""
    with pod.open_spi(**spi_pins) as spi:
        info = spi.flash_id()
    if not info.present:
        why = (f"no SPI flash answers on LA{spi_pins['sck']}/{spi_pins['mosi']}/{spi_pins['miso']}/"
               f"{spi_pins['cs']} (JEDEC ID {info.jedec_id or 'none'}): the chip is not on the bench")
        if os.environ.get("BENCHPOD_E2E_SPI_REQUIRED") == "1":
            pytest.fail(why)
        pytest.skip(why + ". Set BENCHPOD_E2E_SPI_REQUIRED=1 to fail instead")
    return spi_pins


@pytest.mark.parametrize("hz,mode", [(1_000_000, 0), (6_000_000, 0), (6_000_000, 3)])
def test_program_and_read_back(pod, spi_flash, hz, mode):
    image = os.urandom(20_000)
    res = pod.spi_flash(image, BASE, hz=hz, mode=mode, **spi_flash)
    assert res.verified and res.length == len(image) and res.erased >= len(image)
    with pod.open_spi(hz=hz, mode=mode, **spi_flash) as spi:
        assert spi.hz <= hz
        assert spi.flash_read(BASE, len(image)) == image


def test_raw_transfer_reads_the_jedec_id(pod, spi_flash):
    with pod.open_spi(**spi_flash) as spi:
        info = spi.flash_id()
        assert info.present, f"no flash answering: {info}"
        rx = spi.transfer(b"\x9f\x00\x00\x00")
        assert rx[1:].hex() == info.jedec_id


def test_writing_over_data_fails_verify(pod, spi_flash):
    with pod.open_spi(**spi_flash) as spi:
        spi.flash_erase(BASE, 4096)
        spi.flash_write(BASE, b"\x00\x00")
        with pytest.raises(FirmwareError, match="verify failed"):
            spi.flash_write(BASE, b"\xff\xff")


def test_pins_are_owned_while_open_and_released_after(pod, spi_pins):
    with pod.open_spi(**spi_pins):
        with pytest.raises(PinConflictError):
            pod.gpio(spi_pins["cs"], "output", level=1)
    pod.gpio(spi_pins["cs"], "output", level=1)       # free again once the session closed
    pod.release_gpio()
