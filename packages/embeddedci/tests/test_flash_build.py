"""``BenchPod.flash(build=...)``: flash a build uploaded to embeddedci.com (no hardware, no network).

A cloud pod (``embeddedci:<device>``) has the server flash it; a LAN pod downloads the file and runs
the local OpenOCD path. ``server_side`` overrides the choice.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

from embeddedci.benchpod import BenchPod, FlashError, FlashResult, TargetUnreachableError
from embeddedci.benchpod import flash as flash_mod

ART = {"job_id": "job-1", "pack_id": "pack-1", "name": "build/fw.elf", "repo": "acme/fw",
       "ref": "main", "sha": "abc123", "wiring_defaults": {"target": "target/stm32f4x.cfg"}}


class Pod:
    def status(self) -> Dict[str, Any]:
        return {"board": "stm32h563", "caps": []}

    def ping(self) -> Any:
        return "pong"

    def command(self, req: dict) -> Any:
        return {}

    def close(self) -> None:
        pass


class FakeServer:
    def __init__(self, flash_reply: Dict[str, Any] | None = None) -> None:
        self.lookups: List[dict] = []
        self.downloads: List[dict] = []
        self.flashes: List[tuple] = []
        self.flash_reply = flash_reply or {"ok": True, "output": "** Verified OK **"}

    def build_artifact(self, **sel: str) -> Dict[str, Any]:
        self.lookups.append(sel)
        return dict(ART)

    def download_build_artifact(self, art: Dict[str, Any]) -> bytes:
        self.downloads.append(art)
        return b"\x7fELF-image"

    def resolve_device_id(self, name: str) -> str:
        return "dev-" + name

    def flash_build(self, device_id: str, body: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        self.flashes.append((device_id, body, timeout))
        return dict(self.flash_reply)


def _bp(monkeypatch, server: FakeServer, *, cloud: bool) -> BenchPod:
    bp = BenchPod(transport=Pod(), lease=False)
    bp._device_name = "bench-pod" if cloud else ""
    monkeypatch.setattr(bp, "_require_server_api", lambda: server)
    return bp


@pytest.fixture
def local_flash(monkeypatch):
    """Replace the OpenOCD run with a recorder that checks the downloaded file exists."""
    calls: List[dict] = []

    def fake(transport, **kw):
        with open(kw["file"], "rb") as fh:
            kw["content"] = fh.read()
        calls.append(kw)
        return FlashResult(ok=True, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(flash_mod, "flash", fake)
    return calls


def test_cloud_pod_has_the_server_flash_the_build(monkeypatch, local_flash):
    server = FakeServer()
    bp = _bp(monkeypatch, server, cloud=True)
    res = bp.flash(swclk=11, swdio=12, nreset=True, repo="acme/fw", ref="main", target_power=1)
    assert res.ok and res.artifact == ART and "Verified" in res.stdout
    assert server.lookups == [{"build": "", "repo": "acme/fw", "ref": "main", "artifact": ""}]
    device_id, body, _ = server.flashes[0]
    assert device_id == "dev-bench-pod"
    assert body == {"job_id": "job-1", "pack_id": "pack-1", "artifact_name": "build/fw.elf",
                    "target": "target/stm32f4x.cfg", "load_addr": "", "swclk": 11, "swdio": 12,
                    "nreset": True, "efuse": 1}
    assert local_flash == []  # nothing ran here


def test_lan_pod_downloads_and_flashes_locally(monkeypatch, local_flash):
    server = FakeServer()
    bp = _bp(monkeypatch, server, cloud=False)
    res = bp.flash(swclk=11, swdio=12, build="job-1", target="target/stm32f1x.cfg")
    assert res.ok and res.artifact == ART
    assert server.flashes == [] and len(server.downloads) == 1
    call = local_flash[0]
    assert os.path.basename(call["file"]) == "fw.elf" and call["content"] == b"\x7fELF-image"
    assert call["target"] == "target/stm32f1x.cfg"  # an explicit target beats the build's
    assert not os.path.exists(call["file"])  # temp file cleaned up


def test_server_side_false_flashes_a_cloud_pod_locally(monkeypatch, local_flash):
    server = FakeServer()
    bp = _bp(monkeypatch, server, cloud=True)
    bp.flash(swclk=1, swdio=2, build="latest", server_side=False, extra_args=["-d2"])
    assert server.flashes == [] and local_flash[0]["extra_args"] == ["-d2"]


def test_server_side_refuses_local_only_options(monkeypatch):
    bp = _bp(monkeypatch, FakeServer(), cloud=True)
    with pytest.raises(ValueError, match="verify=False, extra_configs only apply to the local OpenOCD"):
        bp.flash(swclk=1, swdio=2, build="job-1", verify=False, extra_configs=["adapter speed 100"])


def test_server_side_on_a_lan_pod_is_refused(monkeypatch):
    bp = _bp(monkeypatch, FakeServer(), cloud=False)
    with pytest.raises(Exception, match="registered on embeddedci.com"):
        bp.flash(swclk=1, swdio=2, build="job-1", server_side=True)


def test_file_and_build_are_exclusive(monkeypatch):
    bp = _bp(monkeypatch, FakeServer(), cloud=True)
    with pytest.raises(ValueError, match="either file or a build"):
        bp.flash(swclk=1, swdio=2, file="fw.elf", build="job-1")


def test_server_failure_raises_like_a_local_one(monkeypatch):
    server = FakeServer({"ok": False, "output": "Error: no device found", "error": "openocd exited 1",
                         "target_unreachable": True})
    bp = _bp(monkeypatch, server, cloud=True)
    with pytest.raises(TargetUnreachableError):
        bp.flash(swclk=1, swdio=2, build="job-1")
    res = bp.flash(swclk=1, swdio=2, build="job-1", check=False)
    assert not res.ok and res.target_unreachable and res.stderr == "openocd exited 1"
    server.flash_reply = {"ok": False, "output": "", "error": "verify failed"}
    with pytest.raises(FlashError, match="verify failed"):
        bp.flash(swclk=1, swdio=2, build="job-1")


def test_server_api_build_calls():
    from embeddedci.benchpod.server_api import ServerApi

    api = ServerApi(api_key="eci_test")
    seen: List[tuple] = []

    def fake_request(method, path, **kw):
        seen.append((method, path, kw))
        if path == "/benchpod/builds/artifact":
            return 200, {"artifact": ART}
        if method == "GET":
            return 200, b"bytes"
        return 200, {"ok": True}

    api.request = fake_request  # type: ignore[assignment]
    assert api.build_artifact(repo="acme/fw", ref="main") == ART
    assert seen[0][2]["query"] == {"build": None, "repo": "acme/fw", "ref": "main", "artifact": None}
    assert api.download_build_artifact({"job_id": "j 1", "pack_id": "p", "name": "out/fw v2.elf"}) == b"bytes"
    assert seen[1][1] == "/benchpod/builds/j%201/packs/p/artifacts/out/fw%20v2.elf"
    assert seen[1][2]["parse_json"] is False
    assert api.flash_build("dev-1", {"build": "latest"}, timeout=99) == {"ok": True}
    assert seen[2][:2] == ("POST", "/benchpod/devices/dev-1/flash") and seen[2][2]["timeout"] == 99
