"""Make the package importable when running the tests from a source checkout
without an editable install.

Set EMBEDDEDCI_TEST_INSTALLED=1 to test the installed package instead (e.g. a built wheel)."""

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if os.environ.get("EMBEDDEDCI_TEST_INSTALLED") != "1" and _SRC not in sys.path:
    sys.path.insert(0, _SRC)
