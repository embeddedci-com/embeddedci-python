"""Hardware controls below the named analog-path API (``BenchPod.lowlevel``).

The named paths (:meth:`~embeddedci.benchpod.client.BenchPod.analog_path`, ``dac_output``,
``adc_read``) set a whole, known-good switch state in one step. The methods here flip the
individual muxes and relays and drive raw DAC codes — useful for board bring-up and diagnostics,
easy to leave the analog front end in a state no named path describes.

**Not covered by the API stability guarantee**: these track the board's schematic and may change
with a hardware revision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .client import BenchPod

#: DAC output paths for :meth:`LowLevel.dac_mux` ``ctrl1`` (U47 TMUX).
DAC_RAILS = {"3v3": 0, "5v": 1, "12v": 2, "12v_adc": 3}
#: Buffer VMID references for :meth:`LowLevel.dac_mux` ``ctrl2`` (U48 TMUX).
DAC_VMIDS = {"12v_vmid": 0, "adc_vmid": 1, "gnd": 2}


def _dict(data: Any) -> Dict[str, Any]:
    return data if isinstance(data, dict) else {}


class LowLevel:
    """Raw mux / relay / DAC-code access for one :class:`~embeddedci.benchpod.client.BenchPod`."""

    def __init__(self, pod: "BenchPod") -> None:
        self._pod = pod

    def dac_mux(self, *, ctrl1: Optional[str] = None, ctrl2: Optional[str] = None) -> Dict[str, Any]:
        """Route the buffered DAC through the analog muxes (U55 → U47/U48).

        ``ctrl1`` selects the output path the DAC drives (``3v3``/``5v``/``12v``/``12v_adc``, or
        ``off``); ``ctrl2`` the buffer VMID reference (``12v_vmid``/``adc_vmid``/``gnd``/``off``).
        ``None`` leaves that control unchanged. Returns the pod's decoded mux state.
        """
        req: Dict[str, Any] = {"cmd": "dac_mux"}
        if ctrl1 is not None:
            if ctrl1 == "off":
                req["ctrl1_en"] = 0
            elif ctrl1 in DAC_RAILS:
                req["ctrl1_en"] = 1
                req["ctrl1_sel"] = DAC_RAILS[ctrl1]
            else:
                raise ValueError(f"ctrl1 must be one of {list(DAC_RAILS)}, 'off', or None")
        if ctrl2 is not None:
            if ctrl2 == "off":
                req["ctrl2_en"] = 0
            elif ctrl2 in DAC_VMIDS:
                req["ctrl2_en"] = 1
                req["ctrl2_sel"] = DAC_VMIDS[ctrl2]
            else:
                raise ValueError(f"ctrl2 must be one of {list(DAC_VMIDS)}, 'off', or None")
        return _dict(self._pod.command(req))

    def dac_mux_status(self) -> Dict[str, Any]:
        """Report the DAC-mux (U55) state without changing it."""
        return _dict(self._pod.command({"cmd": "dac_mux"}))

    def cal_switch(self, *, cal1: bool = False, cal2: bool = False,
                   amp_measure: bool = False, cal_path: bool = False) -> Dict[str, Any]:
        """Set ALL calibration relays (U58 → U53) at once — every relay not named is switched off.

        ``cal1``/``cal2`` route the 5V/12V DAC path to the ADC (mutually exclusive);
        ``amp_measure`` switches the ADC to the amps terminal; ``cal_path`` switches the ADC from
        the front SMA to the calibration path. Use :meth:`cal_switch_status` to read them.
        """
        if cal1 and cal2:
            raise ValueError("cal1 and cal2 are mutually exclusive")
        return _dict(self._pod.command({
            "cmd": "cal_switch",
            "cal1": int(cal1), "cal2": int(cal2),
            "amp_measure": int(amp_measure), "cal_path": int(cal_path),
        }))

    def cal_switch_status(self) -> Dict[str, Any]:
        """Report the calibration-relay (U58) state without changing it."""
        return _dict(self._pod.command({"cmd": "cal_switch"}))

    def dac_set(self, code: int, *, divider: Optional[int] = None) -> Dict[str, Any]:
        """Hold a raw 8-bit DAC code (0..255) on whatever path is routed, uncalibrated.

        ``divider`` is the DAC sequencer clock divider. Prefer
        :meth:`~embeddedci.benchpod.client.BenchPod.dac_output`, which takes calibrated volts.
        """
        if not 0 <= int(code) <= 255:
            raise ValueError(f"code must be 0..255, got {code!r}")
        req: Dict[str, Any] = {"cmd": "dac_set", "value": int(code)}
        if divider is not None:
            req["divider"] = int(divider)
        return _dict(self._pod.command(req))
