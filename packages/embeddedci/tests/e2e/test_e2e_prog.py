"""Tier "prog": flashing a chip over its own programming pins (UPDI, PIC, C2, TPI, PDI).

    BENCHPOD_E2E_PROG=avr-updi,data=12 BENCHPOD_E2E_PROG_FILE=app.hex \
        pytest packages/embeddedci/tests/e2e/test_e2e_prog.py --benchpod-connection=<pod host>

Needs a chip of one of the pin-programmer families wired to the pod, powered at the LA voltage.
BENCHPOD_E2E_PROG is the ``flash(target=...)`` name followed by the pins as ``name=LA`` pairs
(``clk``, ``data``, ``reset_pin``): ``pic,clk=3,data=4`` or ``attiny-tpi,clk=3,data=4,reset_pin=5``.
Without it the whole tier skips. BENCHPOD_E2E_PROG_DEVICE names the chip to check (optional) and
BENCHPOD_E2E_PROG_FILE is a ``.hex``, ``.elf`` or ``.bin`` for the chip: the write tests skip
without it and ERASE and WRITE the chip. Also skips on firmware without ``prog_flash``.
BENCHPOD_E2E_PROG_REQUIRED=1 turns "no chip answers" into a failure.
"""

from __future__ import annotations

import os

import pytest

from embeddedci.benchpod import ProgNoAnswerError

pytestmark = [pytest.mark.hardware, pytest.mark.prog]


def _spec():
    raw = os.environ.get("BENCHPOD_E2E_PROG")
    if not raw:
        pytest.skip("set BENCHPOD_E2E_PROG=<target>,<pin>=<LA>,... to run the pin-programmer tier "
                    "(for example avr-updi,data=12)")
    target, *pins = [p.strip() for p in raw.split(",") if p.strip()]
    kw = {}
    for p in pins:
        k, _, v = p.partition("=")
        kw[k] = int(v)
    if os.environ.get("BENCHPOD_E2E_PROG_DEVICE"):
        kw["device"] = os.environ["BENCHPOD_E2E_PROG_DEVICE"]
    return target, kw


@pytest.fixture(scope="module")
def prog_chip(pod):
    target, kw = _spec()
    if not pod.supports("prog_flash"):
        pytest.skip("the pod's firmware has no prog_flash (features table revision 17)")
    try:
        res = pod.flash(target=target, dry_run=True, **kw)
    except ProgNoAnswerError as exc:
        why = f"no chip answers on {target} {kw}: {exc}"
        if os.environ.get("BENCHPOD_E2E_PROG_REQUIRED") == "1":
            pytest.fail(why)
        pytest.skip(why + ". Set BENCHPOD_E2E_PROG_REQUIRED=1 to fail instead")
    return target, kw, res


def test_dry_run_names_the_chip_and_writes_nothing(prog_chip):
    _target, _kw, res = prog_chip
    assert res.ok and "nothing was written" in res.stdout and "ID " in res.stdout


def test_write_the_image_and_read_it_back(pod, prog_chip):
    path = os.environ.get("BENCHPOD_E2E_PROG_FILE")
    if not path:
        pytest.skip("set BENCHPOD_E2E_PROG_FILE to a .hex, .elf or .bin for the chip (it is erased and written)")
    target, kw, _res = prog_chip
    res = pod.flash(target=target, file=path, **kw)   # the pod reads every page back
    assert res.ok and "written and verified" in res.stdout


def test_wrong_device_is_refused_before_erasing(pod, prog_chip):
    target, kw, _res = prog_chip
    kw = {k: v for k, v in kw.items() if k != "device"}
    from embeddedci.benchpod import ProgDeviceError

    with pytest.raises(ProgDeviceError):
        pod.flash(target=target, dry_run=True, device="NoSuchChip-9999", **kw)
