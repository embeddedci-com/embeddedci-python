"""header_map: a wiring profile on the pod's header, as text; board_header: which header a pod has."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from embeddedci.benchpod import BenchPod, Wiring, header_map
from embeddedci.benchpod.errors import BenchPodError, UnsupportedFeatureError
from embeddedci.benchpod.header import connections, header_from_status

W = Wiring.from_dict({
    "version": 2, "la_mv": 3300, "uart_rx": 3, "uart_tx": 4, "swd_swclk": 11, "swd_swdio": 12,
    "swd_nreset": True, "pulls": [3],
    "signals": [{"name": "HEATER_EN", "la": 9, "direction": "output"}],
})


def test_j1_places_pins_and_lists_connections() -> None:
    text = header_map(W, "j1")
    assert "Pin headers J1 (2x11)" in text
    rows = text.splitlines()
    # Pin 1/2 is the last row (bottom), pin 21/22 the first.
    assert rows[2].strip().startswith("22 NRST_CONTROL DUT reset")
    assert rows[2].rstrip().endswith("| 21 LA_REF")
    assert rows[12].strip().startswith("2 LA2 -")
    assert rows[12].rstrip().endswith("1 LA1 -")
    assert "pin 3 (LA3, UART RX) -> board TX [pull-up 2.2k on]" in text
    assert "pin 9 (LA9, HEATER_EN) -> HEATER_EN (the pod drives it)" in text
    assert "pin 22 (NRST_CONTROL, Reset) -> board NRST" in text
    assert "pin 15, 16 (GND, Ground)" in text


def test_front_columns_left_to_right() -> None:
    text = header_map(W, "front")
    lines = text.splitlines()
    assert lines[2].split()[:3] == ["1", "25", "GND"]
    last = lines[14]
    assert last.split()[:4] == ["13", "1", "LA1", "-"]
    assert "pin 15, 16, 23, 24, 25, 26 (GND" in text


def test_both_when_unknown_and_bad_header() -> None:
    text = header_map(W, None)
    assert "Pin headers J1" in text and "Front connector" in text
    with pytest.raises(ValueError):
        header_map(W, "side")


def test_connections_and_status() -> None:
    assert connections(W)[0] == {"pin": 3, "name": "LA3", "used_for": "UART RX", "connect": "board TX",
                                 "pull": "pull-up 2.2k on"}
    assert header_from_status({"board_rev": "v3r2"}) == "j1"
    assert header_from_status({"board_rev": "v3", "module": {"present": True}}) == "front"
    assert header_from_status({"board_rev": "v3", "short_form": True}) is None


class _Pod:
    def __init__(self, module: Any, status: Dict[str, Any]) -> None:
        self._module, self._status, self.calls = module, status, 0

    def command(self, req: Dict[str, Any]) -> Any:
        self.calls += 1
        if isinstance(self._module, Exception):
            raise self._module
        return self._module

    def status(self) -> Dict[str, Any]:
        return self._status


@pytest.mark.parametrize("module,status,want", [
    ({"present": True}, {}, "front"),
    (BenchPodError("module: this BenchPod has no module link (digital v3r3 only)"), {}, "j1"),
    (UnsupportedFeatureError("unknown command: module"), {"board_rev": "v3r2"}, "j1"),
    (UnsupportedFeatureError("unknown command: module"), {"board_rev": "v3"}, None),
])
def test_board_header(module: Any, status: Dict[str, Any], want: Any) -> None:
    pod = _Pod(module, status)
    assert BenchPod.board_header(pod) == want  # type: ignore[arg-type]
    assert BenchPod.board_header(pod) == want  # type: ignore[arg-type]
    assert pod.calls == 1  # asked once per connection
