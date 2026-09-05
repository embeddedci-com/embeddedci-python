"""Publish a firmware build to embeddedci.com from CI, without a BenchPod.

This is the command behind the ``embeddedci-com/embeddedci-github-action/upload-artifact``
action. It records the freshly built firmware as a **GitHub-sourced build**, so the
artifact shows up in the web UI's flash dropdown and can be flashed onto a pod by hand
— no device is touched here.

It deliberately reuses :func:`embeddedci.benchpod.ci.make_build_reporter`, the same code
path the pytest ``build_report`` fixture uses, so the action cannot drift away from the
tested behaviour: OIDC minting, session-token exchange, build creation, artifact upload
and wiring defaults all stay in one place.

Authentication is the job's GitHub Actions OIDC token, so the workflow needs
``permissions: id-token: write`` and the repository must be trusted in the EmbeddedCI web
app (BenchPod → GitHub Actions).

Usage::

    embeddedci-upload-build --firmware build/app.elf \\
        --build-target stm32f4 --openocd-target target/stm32f4x.cfg --swclk 11 --swdio 12
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

from .benchpod.ci import make_build_reporter

#: Sibling extensions collected alongside the named firmware, so a build carries every
#: format the web UI might want to flash.
SIBLING_EXTS = (".elf", ".bin", ".hex", ".uf2")


def firmware_artifacts(firmware: str) -> List[str]:
    """The firmware plus any sibling build outputs that exist, in a stable order."""
    paths: List[str] = []
    if os.path.exists(firmware):
        paths.append(firmware)
    stem, _ = os.path.splitext(firmware)
    for ext in SIBLING_EXTS:
        sibling = stem + ext
        if os.path.exists(sibling) and sibling not in paths:
            paths.append(sibling)
    return paths


def _optional_pin(value: Optional[str]) -> Optional[int]:
    """Parse a pin input. Empty string means "not wired" — an action input that was
    simply left unset — and must not become 0, which is a different thing."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="embeddedci-upload-build",
        description="Publish a firmware build to embeddedci.com (no BenchPod needed).",
    )
    p.add_argument("--firmware", required=True, help="path to the built firmware (.elf/.bin/.hex)")
    p.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="extra artifact to upload; repeatable. Siblings of --firmware are added automatically.",
    )
    # Two different "targets" live here and must not be conflated: the build target is
    # the platform id recorded against the build (e.g. "stm32f4"), while the OpenOCD
    # target is the config file the flash dialog pre-fills (e.g. "target/stm32f4x.cfg").
    p.add_argument("--build-target", default="", help="platform id recorded with the build, e.g. stm32f4")
    p.add_argument("--openocd-target", default="", help="OpenOCD target config, e.g. target/stm32f4x.cfg")
    p.add_argument("--swclk", default="", help="LA channel wired to SWCLK")
    p.add_argument("--swdio", default="", help="LA channel wired to SWDIO")
    p.add_argument("--nreset", default="", help="LA channel wired to NRST (omit if not wired)")
    p.add_argument("--efuse", default="", help="target-power eFuse rail: 1 internal, 2 external")
    p.add_argument("--name", default="", help="build name shown in the UI")
    p.add_argument("--api-base", default="", help="embeddedci base URL (default: the public server)")
    p.add_argument(
        "--allow-missing-token",
        action="store_true",
        help="exit 0 instead of failing when no OIDC token is available (e.g. a local dry run)",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    artifacts = firmware_artifacts(args.firmware)
    if not artifacts:
        print(f"error: firmware artifact not found: {args.firmware}", file=sys.stderr)
        return 1
    for extra in args.artifact:
        if extra and extra not in artifacts:
            if not os.path.exists(extra):
                print(f"error: artifact not found: {extra}", file=sys.stderr)
                return 1
            artifacts.append(extra)

    reporter = make_build_reporter(
        api_base=args.api_base or None,
        target=args.build_target,
        name=args.name,
    )
    if not getattr(reporter, "active", False):
        # The no-op reporter means there is nothing to publish to: either we are not in
        # GitHub Actions, or the OIDC token could not be minted. Uploading is this
        # command's whole purpose, so that is an error unless explicitly tolerated.
        msg = (
            "no embeddedci session available — this needs to run inside GitHub Actions with "
            "`permissions: id-token: write`, and the repository must be trusted in the "
            "EmbeddedCI web app under BenchPod → GitHub Actions."
        )
        if args.allow_missing_token:
            print(f"embeddedci: skipping upload: {msg}", file=sys.stderr)
            return 0
        print(f"error: {msg}", file=sys.stderr)
        return 1

    reporter.record_wiring(
        target=args.openocd_target or None,
        swclk=_optional_pin(args.swclk),
        swdio=_optional_pin(args.swdio),
        nreset=_optional_pin(args.nreset),
        efuse=_optional_pin(args.efuse),
    )
    reporter.upload_artifacts(artifacts)
    reporter.set_result(True, "artifact upload")
    reporter.finalize()

    build_id = reporter.build_id or ""
    for path in artifacts:
        print(f"embeddedci: uploaded {path}")
    print(f"embeddedci: build {build_id}")

    # Hand the id back to later workflow steps.
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"build_id={build_id}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
