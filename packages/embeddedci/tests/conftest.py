"""Make the package importable when running the tests from a source checkout
without an editable install, and configure the hardware tests' board."""

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

pytest_plugins = ["pytester"]


@pytest.fixture(scope="session")
def benchpod_la_voltage():
    """I/O voltage of the bench boards the hardware tests run against: 3.3 V.

    Change this to 1.8 when testing a 1V8 board (or pass --benchpod-la-voltage for one run).
    """
    return 3.3
