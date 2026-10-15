"""Capabilities.scope from a pod's status: the digital-only board has no scope."""

from embeddedci.benchpod.capabilities import Capabilities


def test_digital_board_has_no_scope():
    # Firmware 3.6+ lists "signal" on every board and names "scope" itself on the analog one.
    status = {"board": "stm32h563", "adc_bits": 16, "analog": False,
              "caps": ["signal", "gpio", "power", "la", "analyzer", "command"]}
    assert Capabilities.from_status(status).scope is False


def test_analog_board_has_the_scope():
    status = {"board": "stm32h563", "adc_bits": 16, "analog": True,
              "caps": ["signal", "la", "analog", "scope", "dac"]}
    assert Capabilities.from_status(status).scope is True


def test_firmware_without_the_analog_flag_keeps_signal_as_scope():
    status = {"board": "stm32h563", "adc_bits": 16, "caps": ["signal", "la"]}
    assert Capabilities.from_status(status).scope is True
