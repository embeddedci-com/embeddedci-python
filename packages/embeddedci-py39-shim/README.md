# embeddedci 0.2.4 — placeholder for Python 3.9

This release exists only to stop Python 3.9 from silently installing the **pre-2.0** `embeddedci`
API.

The real package — the BenchPod SDK and pytest plugin — is **2.x and requires Python 3.10+**. On
Python 3.9 pip skips 2.x (its `Requires-Python` excludes it) and, before this release, quietly
resolved to 0.2.3 instead: a completely different API, installed without a word of warning. Yanking
the old releases does not help, because pip's install path allows yanked candidates and merely
prefers unyanked ones — when the yanked releases are the only candidates, it installs one anyway.

So this version is published for `>=3.9,<3.10` only. On Python 3.9 it is the newest installable
version, so pip picks it and the import fails with an actionable message. On Python 3.10+ it is not
a candidate at all, and `pip install embeddedci` resolves to 2.x as normal.

```
ImportError: embeddedci 2.x requires Python 3.10 or newer, and you are on Python 3.9.13.
...
    pip install "embeddedci>=2,<3"
```

**What you probably want:** upgrade to Python 3.10 or newer, then `pip install "embeddedci>=2,<3"`.

**If you really need the old API:** `pip install "embeddedci==0.2.3"` still works — an exact pin is
always honoured.

Documentation: <https://embeddedci.com/docs/benchpod-pytest>
