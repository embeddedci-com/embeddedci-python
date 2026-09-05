"""Tests for the firmware upload command (embeddedci.upload_build)."""

from __future__ import annotations

import pytest

from embeddedci import upload_build


def _write(path, data=b"\x7fELF"):
    path.write_bytes(data)
    return str(path)


# -- firmware_artifacts ----------------------------------------------------------


def test_firmware_artifacts_collects_siblings_in_stable_order(tmp_path):
    _write(tmp_path / "app.elf")
    _write(tmp_path / "app.bin")
    _write(tmp_path / "app.hex")
    _write(tmp_path / "other.bin")  # different stem, must not be picked up

    found = upload_build.firmware_artifacts(str(tmp_path / "app.elf"))

    assert [p.rsplit("/", 1)[-1] for p in found] == ["app.elf", "app.bin", "app.hex"]


def test_firmware_artifacts_does_not_duplicate_the_named_firmware(tmp_path):
    fw = _write(tmp_path / "app.elf")
    assert upload_build.firmware_artifacts(fw) == [fw]


def test_firmware_artifacts_empty_when_nothing_exists(tmp_path):
    assert upload_build.firmware_artifacts(str(tmp_path / "missing.elf")) == []


def test_firmware_artifacts_finds_siblings_when_named_file_is_absent(tmp_path):
    # A build that emits only app.bin is still publishable when the workflow names app.elf.
    _write(tmp_path / "app.bin")
    found = upload_build.firmware_artifacts(str(tmp_path / "app.elf"))
    assert [p.rsplit("/", 1)[-1] for p in found] == ["app.bin"]


# -- _optional_pin ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_optional_pin_unset_is_none_not_zero(value):
    # An action input left unset must stay "not wired"; 0 is a real channel number.
    assert upload_build._optional_pin(value) is None


def test_optional_pin_parses_numbers_including_zero():
    assert upload_build._optional_pin("0") == 0
    assert upload_build._optional_pin(" 11 ") == 11


# -- main ------------------------------------------------------------------------


class _FakeReporter:
    active = True

    def __init__(self, build_id="build-123"):
        self.build_id = build_id
        self.wiring = None
        self.uploaded = []
        self.result = None
        self.finalized = 0

    def record_wiring(self, **kwargs):
        self.wiring = kwargs

    def upload_artifacts(self, paths):
        self.uploaded.extend(paths)

    def set_result(self, success, reason=""):
        self.result = (success, reason)

    def finalize(self):
        self.finalized += 1


@pytest.fixture
def fake_reporter(monkeypatch):
    reporter = _FakeReporter()
    monkeypatch.setattr(upload_build, "make_build_reporter", lambda **_kw: reporter)
    return reporter


def test_main_uploads_and_records_wiring(tmp_path, monkeypatch, fake_reporter):
    fw = _write(tmp_path / "app.elf")
    _write(tmp_path / "app.bin")
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    rc = upload_build.main(
        [
            "--firmware", fw,
            "--build-target", "stm32f4",
            "--openocd-target", "target/stm32f4x.cfg",
            "--swclk", "11",
            "--swdio", "12",
        ]
    )

    assert rc == 0
    assert [p.rsplit("/", 1)[-1] for p in fake_reporter.uploaded] == ["app.elf", "app.bin"]
    assert fake_reporter.wiring == {
        "target": "target/stm32f4x.cfg",
        "swclk": 11,
        "swdio": 12,
        # Not passed on the command line, so they must be absent, not 0.
        "nreset": None,
        "efuse": None,
    }
    assert fake_reporter.result == (True, "artifact upload")
    assert fake_reporter.finalized == 1
    assert out.read_text() == "build_id=build-123\n"


def test_main_passes_build_target_and_name_to_the_reporter(tmp_path, monkeypatch):
    fw = _write(tmp_path / "app.elf")
    seen = {}

    def _factory(**kwargs):
        seen.update(kwargs)
        return _FakeReporter()

    monkeypatch.setattr(upload_build, "make_build_reporter", _factory)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert upload_build.main(
        ["--firmware", fw, "--build-target", "stm32f4", "--name", "nightly"]
    ) == 0
    assert seen == {"api_base": None, "target": "stm32f4", "name": "nightly"}


def test_main_extra_artifacts_are_appended(tmp_path, monkeypatch, fake_reporter):
    fw = _write(tmp_path / "app.elf")
    extra = _write(tmp_path / "app.map", b"map")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert upload_build.main(["--firmware", fw, "--artifact", extra]) == 0
    assert [p.rsplit("/", 1)[-1] for p in fake_reporter.uploaded] == ["app.elf", "app.map"]


def test_main_fails_when_firmware_missing(tmp_path, capsys):
    rc = upload_build.main(["--firmware", str(tmp_path / "nope.elf")])
    assert rc == 1
    assert "firmware artifact not found" in capsys.readouterr().err


def test_main_fails_when_extra_artifact_missing(tmp_path, capsys, fake_reporter):
    fw = _write(tmp_path / "app.elf")
    rc = upload_build.main(["--firmware", fw, "--artifact", str(tmp_path / "nope.map")])
    assert rc == 1
    assert "artifact not found" in capsys.readouterr().err
    assert fake_reporter.uploaded == []


def test_main_fails_without_a_session(tmp_path, monkeypatch, capsys):
    fw = _write(tmp_path / "app.elf")
    monkeypatch.setattr(upload_build, "make_build_reporter", lambda **_kw: _NoopReporter())

    rc = upload_build.main(["--firmware", fw])

    assert rc == 1
    assert "no embeddedci session available" in capsys.readouterr().err


def test_main_allow_missing_token_succeeds_without_a_session(tmp_path, monkeypatch, capsys):
    fw = _write(tmp_path / "app.elf")
    monkeypatch.setattr(upload_build, "make_build_reporter", lambda **_kw: _NoopReporter())

    rc = upload_build.main(["--firmware", fw, "--allow-missing-token"])

    assert rc == 0
    assert "skipping upload" in capsys.readouterr().err


class _NoopReporter:
    active = False
    build_id = None
