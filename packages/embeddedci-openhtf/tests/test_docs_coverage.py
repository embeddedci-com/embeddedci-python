"""Every public name of embeddedci-openhtf is documented in its README.

Covers ``embeddedci_openhtf.__all__``, the public attributes and methods of ``BenchPodPlug``, and
the OpenHTF conf keys the plug declares. Each must appear inside a code span or code block of
``README.md``. A name left out on purpose goes in ``ALLOWLIST`` with the reason.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Dict, List, Set

import embeddedci_openhtf
from embeddedci_openhtf import plug as plug_module

README = Path(__file__).resolve().parents[1] / "README.md"

#: name -> why it is not in the README.
ALLOWLIST: Dict[str, str] = {
    "tearDown": "OpenHTF plug lifecycle hook, called by OpenHTF, not by users",
    "__version__": "standard package metadata",
}


def _code_text() -> str:
    text = README.read_text(encoding="utf-8")
    fenced = re.compile(r"^```.*?^```", re.M | re.S)
    return "\n".join(fenced.findall(text) + re.findall(r"`([^`\n]+)`", fenced.sub("", text)))


def public_names() -> Set[str]:
    names = set(embeddedci_openhtf.__all__)
    for name, value in vars(embeddedci_openhtf.BenchPodPlug).items():
        if not name.startswith("_") and (inspect.isfunction(value) or not callable(value)):
            names.add(name)
    names.add("pod")  # set per instance in __init__
    source = inspect.getsource(plug_module)
    names.update(re.findall(r"htf\.conf\.declare\(\s*\"(\w+)\"", source))
    return names


def _missing() -> List[str]:
    text = _code_text()
    return sorted(n for n in public_names() if n not in ALLOWLIST
                  and not re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", text))


def test_names_are_found():
    assert {"BenchPodPlug", "benchpod_plug", "persistent", "benchpod_timeout"} <= public_names()


def test_every_public_name_is_documented():
    missing = _missing()
    assert not missing, ("names missing from packages/embeddedci-openhtf/README.md (document them "
                         "or add them to ALLOWLIST with a reason): " + ", ".join(missing))


def test_allowlist_is_still_needed():
    stale = sorted(n for n in ALLOWLIST if n not in public_names())
    assert not stale, "ALLOWLIST names that are no longer public: " + ", ".join(stale)
