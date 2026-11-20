"""Capabilities.scope from a firmware 4.0 status: the features table decides, so the digital-only
board has no scope and the analog one has it."""

from embeddedci.benchpod.capabilities import Capabilities

_FW4 = {"board": "stm32h563", "adc_bits": 16, "version": "4.0.0", "api": 2, "min_client_api": 2,
        "features_rev": 10, "gateware": 59, "fpga_image": "loop"}


def test_digital_board_has_no_scope():
    c = Capabilities.from_status({**_FW4, "analog": False})
    assert c.scope is False and c.dac is False and c.analyzer is True


def test_analog_board_has_the_scope():
    assert Capabilities.from_status({**_FW4, "analog": True}).scope is True


def test_a_caps_list_is_ignored():
    """Firmware 4.0 sends no caps list; one in a status decides nothing."""
    c = Capabilities.from_status({"board": "stm32h563", "caps": ["signal", "la", "scope", "dac"]})
    assert not c.scope and not c.dac and not c.analyzer
