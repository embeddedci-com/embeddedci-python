"""Tests for the CI build-reporting module (embeddedci.benchpod.ci)."""

from __future__ import annotations

import os

import pytest

from embeddedci.benchpod import ci


def test_noop_when_not_in_github_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    reporter = ci.make_build_reporter()
    assert isinstance(reporter, ci.NoopBuildReporter)
    assert reporter.active is False
    # All operations are safe no-ops.
    reporter.record_wiring(target="x", swclk=1)
    reporter.upload_artifacts(["/does/not/exist"])
    reporter.set_result(True)
    reporter.finalize()


def test_github_metadata_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widget")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "acme")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.setenv("GITHUB_RUN_ID", "99")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    meta = ci.github_build_metadata()
    assert meta["repo"] == "widget"
    assert meta["repo_owner"] == "acme"
    assert meta["sha"] == "deadbeef"
    assert meta["run_id"] == "99"
    assert meta["run_attempt"] == "2"


class _FakeReporter(ci.BuildReporter):
    """A BuildReporter whose HTTP layer is replaced with an in-memory recorder."""

    def __init__(self):
        super().__init__(api_base="https://x", session_token="t", metadata={"repo": "widget"})
        self.calls = []

    def _request(self, method, url, *, body, content_type):
        self.calls.append((method, url, content_type))
        if url.endswith("/cloud/builds"):
            return {"build_id": "build-123"}
        return {}


def test_reporter_lazy_create_and_finalize(tmp_path):
    r = _FakeReporter()
    # No build yet.
    assert r.build_id is None
    # Uploading an artifact creates the build lazily, then uploads.
    fw = tmp_path / "fw.elf"
    fw.write_bytes(b"\x7fELF")
    r.record_wiring(target="target/stm32f4x.cfg", swclk=11, swdio=12, nreset=3, efuse=1)
    r.upload_artifacts([str(fw)])
    assert r.build_id == "build-123"
    # set_result + finalize posts status exactly once.
    r.set_result(True)
    r.finalize()
    r.finalize()  # idempotent

    methods_urls = [(m, u.split("/api/")[-1]) for (m, u, _ct) in r.calls]
    assert ("POST", "cloud/builds") in methods_urls
    assert any(u.startswith("cloud/builds/build-123/artifacts") for (_m, u) in methods_urls)
    assert methods_urls.count(("POST", "cloud/builds/build-123/status")) == 1


def test_reporter_finalize_without_artifacts_still_creates_build():
    r = _FakeReporter()
    r.set_result(False, "boom")
    r.finalize()
    assert r.build_id == "build-123"
    urls = [u.split("/api/")[-1] for (_m, u, _ct) in r.calls]
    assert "cloud/builds" in urls
    assert "cloud/builds/build-123/status" in urls
