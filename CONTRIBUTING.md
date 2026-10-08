# Contributing

Thanks for helping out. Issues and pull requests are welcome.

## Before you open a pull request

- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- Install the packages from this checkout and run the unit tests (no hardware needed):

  ```bash
  python -m venv .venv
  .venv/bin/pip install -e "packages/embeddedci[dev]" -e "packages/embeddedci-mcp[dev]" -e "packages/embeddedci-openhtf[dev]"
  make test PYTHON=.venv/bin/python
  ```

- If you have a BenchPod, run the hardware tier too: `make e2e POD=<pod host>`.
- The 2.x SDK API and MCP tool surface are frozen (snapshot tests fail on any change). Additions
  are fine; renames and removals wait for a major version.
- Keep commits small and focused, and describe what changed and why.

## Releases

Maintainers release by tag; see "Releasing" in [README.md](README.md).

## Security

Report vulnerabilities privately, see [SECURITY.md](SECURITY.md).
