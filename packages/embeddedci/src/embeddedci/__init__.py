"""embeddedci — Python tooling for EmbeddedCI hardware.

The :mod:`embeddedci.benchpod` subpackage is a pytest-friendly client for a
BenchPod device. Import it as::

    from embeddedci import benchpod
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from . import benchpod

try:
    __version__ = version("embeddedci")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+unknown"

__all__ = ["benchpod", "__version__"]
