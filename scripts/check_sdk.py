"""Fail unless `import embeddedci` resolves to this checkout (used by `make e2e`).

Installing a package that depends on embeddedci (the OpenHTF plug, say) can quietly replace
the editable install with the PyPI release; the e2e tiers then exercise old code.
"""

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WANT = os.path.realpath(os.path.join(HERE, "packages", "embeddedci", "src", "embeddedci"))
FIX = (f"{sys.executable} -m pip install -e packages/embeddedci -e packages/embeddedci-mcp "
       "-e packages/embeddedci-openhtf")

try:
    import embeddedci
except ImportError:
    sys.exit(f"check-sdk: embeddedci is not installed for {sys.executable}. Run: {FIX}")

got = os.path.realpath(os.path.dirname(embeddedci.__file__))
if got != WANT:
    sys.exit(f"check-sdk: tests would import embeddedci from {got}, not this checkout. Run: {FIX}")
