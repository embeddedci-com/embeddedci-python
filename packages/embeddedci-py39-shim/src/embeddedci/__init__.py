"""Placeholder for Python 3.9: the real ``embeddedci`` needs Python 3.10 or newer.

Importing this raises, on purpose. See the package's pyproject.toml for why it exists.
"""

import sys

__version__ = "0.2.4"

_MESSAGE = """\
embeddedci 2.x requires Python 3.10 or newer, and you are on Python {version}.

pip could not install 2.x here, so it fell back to this placeholder rather than silently giving \
you the old pre-2.0 API (that is what happened before this release existed).

To fix it, use Python 3.10+ and then:

    pip install "embeddedci>=2,<3"

If you genuinely need the old 0.x API, pin it explicitly:

    pip install "embeddedci==0.2.3"

Docs: https://embeddedci.com/docs/benchpod-pytest\
"""

raise ImportError(_MESSAGE.format(version=".".join(str(n) for n in sys.version_info[:3])))
