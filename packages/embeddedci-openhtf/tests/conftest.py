"""Make the package importable when running the tests from a source checkout
without an editable install.

Set EMBEDDEDCI_TEST_INSTALLED=1 to test the installed package instead (e.g. a built wheel)."""

import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if os.environ.get("EMBEDDEDCI_TEST_INSTALLED") != "1" and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(autouse=True)
def _isolated_benchpod_cli_config(request, tmp_path_factory, monkeypatch):
    """Unit tests never see this machine's benchpod-cli config (its saved connection) or its
    `benchpod login` session: both change what BenchPod() connects to and where its wiring comes
    from. Hardware tests (e2e, examples) keep them."""
    path = str(request.node.fspath)
    if "e2e" in os.path.basename(path) or os.sep + "e2e" + os.sep in path \
            or os.sep + "examples" + os.sep in path:
        return
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg")))
