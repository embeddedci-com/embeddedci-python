"""The sensor library client: pinned bundled version, overrides, download + cache, offline
fallback, all against a fake GitHub (no network)."""

from __future__ import annotations

import io
import json
import logging
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional

import pytest

from embeddedci.benchpod import SENSOR_LIBRARY_VERSION, sensor_library
from embeddedci.benchpod.sensors import library

BME = "format: 1\nkind: i2c\nname: {name}\nlabel: \"{label}\"\nparams: []\n"


def archive(version: str, files: Dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path if path.startswith(("/", "..")) else
                                   f"benchpod-sensors-{version}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    def __init__(self) -> None:
        self.archives: Dict[str, bytes] = {}
        self.latest: Optional[str] = None
        self.tags: List[str] = []
        self.requests: List[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                body, code = fake.route(self.path)
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, path: str):
        repo = f"/repos/{library.LIBRARY_REPO}"
        if path == f"{repo}/releases/latest":
            if self.latest is None:
                return b'{"message":"Not Found"}', 404
            return json.dumps({"tag_name": f"v{self.latest}"}).encode(), 200
        if path.startswith(f"{repo}/tags"):
            return json.dumps([{"name": t} for t in self.tags]).encode(), 200
        prefix = f"/{library.LIBRARY_REPO}/archive/refs/tags/v"
        if path.startswith(prefix) and path.endswith(".tar.gz"):
            version = path[len(prefix):-len(".tar.gz")]
            if version in self.archives:
                return self.archives[version], 200
        return b"not found", 404

    def close(self):
        self.server.shutdown()


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    monkeypatch.setattr(library, "_GITHUB", fake.url)
    monkeypatch.setattr(library, "_GITHUB_API", fake.url)
    monkeypatch.setenv(library.SENSOR_CACHE_ENV, str(tmp_path / "cache"))
    monkeypatch.delenv(library.SENSOR_LIBRARY_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(library, "_loaded", {})
    yield fake
    fake.close()


def test_the_pinned_version_is_bundled_and_never_downloaded(github):
    lib = sensor_library()
    assert lib.version == SENSOR_LIBRARY_VERSION == "0.1.0" and lib.source == "bundled"
    assert {e.name for e in lib.entries} >= {"bme280", "bmp280", "sht4x", "mpu6050", "gps_nmea"}
    assert lib.get("gps_nmea").kind == "uart" and lib.get("bme280").path == "sensors/i2c/bme280.yaml"
    assert lib.format_doc().startswith("# BenchPod sensor definition format, version 1")
    assert sensor_library("0.1.0").source == "bundled" and sensor_library("v0.1.0").source == "bundled"
    assert github.requests == []
    index = json.loads((lib.root / "library.json").read_text())
    assert index["format"] == 1 and index["version"] == "0.1.0"
    assert index["entries"][0].keys() == {"path", "name", "kind", "label"}


def test_a_version_is_downloaded_once_then_served_from_the_cache(github):
    github.archives["0.2.0"] = archive("0.2.0", {
        "sensors/i2c/new.yaml": BME.format(name="newsensor", label="New"),
        "stimuli/warm.yaml": "format: 1\nkind: stimulus\nname: warm\nsignals: {}\n",
        "docs/format.md": "# format\n",
        "tools/evil.py": "print('x')",
        "../escape.yaml": BME.format(name="escape", label="x"),
        "README.md": "top-level readme is skipped",
    })
    lib = sensor_library("0.2.0")
    assert lib.source == "download" and lib.version == "0.2.0"
    assert [e.to_dict() for e in lib.entries] == [
        {"path": "sensors/i2c/new.yaml", "name": "newsensor", "kind": "i2c", "label": "New"},
        {"path": "stimuli/warm.yaml", "name": "warm", "kind": "stimulus", "label": ""}]
    assert not (lib.root / "tools").exists() and lib.format_doc() == "# format\n"
    assert lib.load("newsensor")["label"] == "New"
    library._loaded.clear()
    again = sensor_library("0.2.0")
    assert again.source == "cache" and len(github.requests) == 1


def test_latest_resolves_the_release_then_the_highest_tag(github):
    github.archives["0.3.0"] = archive("0.3.0", {"sensors/a.yaml": BME.format(name="a", label="A")})
    github.latest = "0.3.0"
    assert sensor_library("latest").version == "0.3.0"
    library._loaded.clear()
    github.latest = None
    github.tags = ["v0.2.0", "v0.10.0", "v0.9.1", "junk", "v1.0.0-rc1"]
    assert library.latest_version() == "0.10.0"   # semver order, no pre-releases


def test_env_and_cli_config_select_the_version(github, monkeypatch, tmp_path):
    github.archives["0.2.0"] = archive("0.2.0", {"sensors/a.yaml": BME.format(name="a", label="A")})
    cfg = tmp_path / "config" / "benchpod-cli"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps({"sensor_library": "0.2.0"}))
    assert library.requested_version() == "0.2.0"
    assert sensor_library().version == "0.2.0"
    monkeypatch.setenv(library.SENSOR_LIBRARY_ENV, "default")
    assert library.requested_version() == SENSOR_LIBRARY_VERSION
    assert library.requested_version("latest") == "latest"
    with pytest.raises(ValueError, match="a sensor library version"):
        library.requested_version("../../etc")


def test_offline_falls_back_to_the_cache_then_the_bundled_copy(github, monkeypatch, caplog):
    github.archives["0.2.0"] = archive("0.2.0", {"sensors/a.yaml": BME.format(name="a", label="A")})
    sensor_library("0.2.0")
    library._loaded.clear()
    monkeypatch.setattr(library, "_GITHUB_API", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(library, "_GITHUB", "http://127.0.0.1:9")
    with caplog.at_level(logging.WARNING, logger="embeddedci.benchpod"):
        lib = sensor_library("latest", timeout=2)
    assert lib.version == "0.2.0" and lib.source == "cache" and "newest cached" in lib.warning
    lib = sensor_library("0.4.0", timeout=2)
    assert lib.source == "bundled" and "using the bundled version 0.1.0" in lib.warning
    assert sensor_library("0.2.0", offline=True).source == "cache"
    assert "bundled" in caplog.text


def test_a_version_that_does_not_exist_is_an_error(github):
    with pytest.raises(library.LibraryError, match="no release 9.9.9"):
        sensor_library("9.9.9")
