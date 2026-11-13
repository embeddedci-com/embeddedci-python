"""The public sensor library (github.com/embeddedci-com/benchpod-sensors): versions, cache, lookup.

The SDK pins a library version, :data:`SENSOR_LIBRARY_VERSION`, and ships that version's files in
the package, so the default works offline and never changes under a test suite. Ask for another
version, or ``"latest"``, with an argument, the ``BENCHPOD_SENSOR_LIBRARY`` environment variable or
``sensor_library`` in benchpod-cli's ``config.json`` (in that order). Other versions are downloaded
from the library's GitHub release archive once and cached per version under the user cache
directory (the CLI uses the same cache). Without a network the cache serves, then the bundled
copy, with a warning.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple, Union

from .. import cli_config
from .definition import DefinitionError, parse_text, read_file

#: The library version this SDK release pins and bundles.
SENSOR_LIBRARY_VERSION = "0.1.0"
#: Environment variable that selects another library version (``latest``, ``0.2.0``, ...).
SENSOR_LIBRARY_ENV = "BENCHPOD_SENSOR_LIBRARY"
#: Environment variable that moves the download cache (default: the user cache directory).
SENSOR_CACHE_ENV = "BENCHPOD_SENSOR_CACHE"
#: The benchpod-cli ``config.json`` key that selects a library version.
CONFIG_KEY = "sensor_library"
#: The library's GitHub repository.
LIBRARY_REPO = "embeddedci-com/benchpod-sensors"

#: Overridable in tests (a fake server); not part of the API.
_GITHUB = "https://github.com"
_GITHUB_API = "https://api.github.com"
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_FILE_SUFFIXES = (".yaml", ".yml", ".json", ".csv", ".md")
_ENTRY_DIRS = ("sensors", "stimuli")
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")
_BUNDLED = Path(__file__).with_name("bundled")
_USER_AGENT = "embeddedci-python (sensor library)"

_log = logging.getLogger("embeddedci.benchpod")
_lock = threading.Lock()
_loaded: Dict[Tuple[str, str], "SensorLibrary"] = {}


class LibraryError(DefinitionError):
    """The library version asked for cannot be found or read."""


@dataclass(frozen=True)
class LibraryEntry:
    """One file of the library: its ``path`` in the library, ``name``, ``kind`` and ``label``."""

    path: str
    name: str
    kind: str
    label: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"path": self.path, "name": self.name, "kind": self.kind, "label": self.label}


class SensorLibrary:
    """One version of the sensor library on disk.

    ``source`` is ``"bundled"`` (shipped with the SDK), ``"cache"`` or ``"download"``;
    ``warning`` says why a fallback was used, ``""`` otherwise.
    """

    def __init__(self, version: str, root: Union[str, "os.PathLike[str]"], *,
                 source: str = "bundled", warning: str = "") -> None:
        self.version = version
        self.root = Path(root)
        self.source = source
        self.warning = warning
        index = self.root / "library.json"
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = build_index(self.root, version)
        self.entries: Tuple[LibraryEntry, ...] = tuple(
            LibraryEntry(path=str(e.get("path", "")), name=str(e.get("name", "")),
                         kind=str(e.get("kind", "")), label=str(e.get("label", "") or ""))
            for e in data.get("entries", []) if isinstance(e, dict))

    def __repr__(self) -> str:
        return f"SensorLibrary(version={self.version!r}, source={self.source!r}, entries={len(self.entries)})"

    def find(self, name: str) -> Optional[LibraryEntry]:
        """The entry called ``name``, or at that library path (``sensors/i2c/bme280.yaml``)."""
        key = str(name).strip()
        for e in self.entries:
            if e.name == key or e.path == key:
                return e
        return None

    def get(self, name: str) -> LibraryEntry:
        """Like :meth:`find`, raising :class:`LibraryError` when the library has no such entry."""
        entry = self.find(name)
        if entry is None:
            names = ", ".join(e.name for e in self.entries) or "none"
            raise LibraryError(f"the sensor library {self.version} has no entry {name!r} "
                               f"(entries: {names})")
        return entry

    def path(self, name: str) -> Path:
        """The file of entry ``name`` on disk."""
        return self.root / PurePosixPath(self.get(name).path)

    def text(self, name: str) -> str:
        """The file of entry ``name`` as it is in the library (YAML)."""
        return self.path(name).read_text(encoding="utf-8")

    def load(self, name: str) -> Dict[str, Any]:
        """Entry ``name`` as a dict (``data`` segment files inlined)."""
        return read_file(self.path(name))

    def format_doc(self) -> str:
        """The library's format reference (``docs/format.md``), ``""`` when this copy lacks it."""
        try:
            return (self.root / "docs" / "format.md").read_text(encoding="utf-8")
        except OSError:
            return ""


def build_index(root: Union[str, "os.PathLike[str]"], version: str) -> Dict[str, Any]:
    """The ``library.json`` of a library tree: every definition and stimulus under ``sensors/``
    and ``stimuli/`` with its name, kind and label."""
    base = Path(root)
    entries: List[Dict[str, str]] = []
    for top in _ENTRY_DIRS:
        folder = base / top
        if not folder.is_dir():
            continue
        for file in sorted(folder.rglob("*")):
            if file.suffix not in (".yaml", ".yml", ".json") or not file.is_file():
                continue
            try:
                doc = parse_text(file.read_text(encoding="utf-8"), path=str(file))
            except (OSError, DefinitionError) as exc:
                _log.warning("sensor library %s: skipping %s: %s", version, file, exc)
                continue
            entries.append({"path": file.relative_to(base).as_posix(),
                            "name": str(doc.get("name", file.stem)),
                            "kind": str(doc.get("kind", "")),
                            "label": str(doc.get("label", "") or "")})
    return {"format": 1, "version": version, "entries": entries}


def normalize_version(version: str) -> str:
    """``"v0.2.0"`` -> ``"0.2.0"``; ``"latest"`` stays. Raises ``ValueError`` on anything else."""
    v = str(version).strip()
    if v.lower() == "latest":
        return "latest"
    if v.lower() in ("default", "pinned", ""):
        return SENSOR_LIBRARY_VERSION
    m = _VERSION_RE.match(v)
    if not m:
        raise ValueError(f"a sensor library version is 'latest' or a release such as "
                         f"{SENSOR_LIBRARY_VERSION!r}, got {version!r}")
    return m.group(1)


def requested_version(version: Optional[str] = None) -> str:
    """The version to use: ``version``, else ``BENCHPOD_SENSOR_LIBRARY``, else benchpod-cli's
    ``sensor_library`` setting, else :data:`SENSOR_LIBRARY_VERSION`."""
    for candidate in (version, os.environ.get(SENSOR_LIBRARY_ENV), cli_config.saved_setting(CONFIG_KEY)):
        if candidate is not None and str(candidate).strip():
            return normalize_version(str(candidate))
    return SENSOR_LIBRARY_VERSION


def user_cache_dir() -> Path:
    """The per-user cache root, as Go's ``os.UserCacheDir`` (so the CLI shares it)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def cache_root(cache_dir: Union[str, "os.PathLike[str]", None] = None) -> Path:
    """Where downloaded versions live: ``cache_dir``, else ``BENCHPOD_SENSOR_CACHE``, else
    ``<user cache>/embeddedci/benchpod-sensors``."""
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get(SENSOR_CACHE_ENV)
    if env:
        return Path(env)
    return user_cache_dir() / "embeddedci" / "benchpod-sensors"


def bundled_library() -> SensorLibrary:
    """The library version shipped inside the SDK (:data:`SENSOR_LIBRARY_VERSION`)."""
    return SensorLibrary(SENSOR_LIBRARY_VERSION, _BUNDLED, source="bundled")


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept": "application/vnd.github+json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
        data = resp.read(_MAX_ARCHIVE_BYTES + 1)
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise LibraryError(f"{url}: larger than {_MAX_ARCHIVE_BYTES} bytes")
    return data


def _version_key(v: str) -> Tuple[Tuple[int, ...], int, str]:
    core, _, pre = v.partition("-")
    nums = tuple(int(x) for x in core.split("+")[0].split("."))
    return nums, 0 if pre else 1, pre


def latest_version(timeout: float = 10.0) -> str:
    """The newest released library version on GitHub (the latest release, else the highest
    ``v*`` tag that is not a pre-release)."""
    try:
        data = json.loads(_http_get(f"{_GITHUB_API}/repos/{LIBRARY_REPO}/releases/latest", timeout))
        tag = data.get("tag_name") if isinstance(data, dict) else None
        if isinstance(tag, str) and _VERSION_RE.match(tag):
            return normalize_version(tag)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    tags = json.loads(_http_get(f"{_GITHUB_API}/repos/{LIBRARY_REPO}/tags?per_page=100", timeout))
    versions = [normalize_version(t["name"]) for t in tags if isinstance(t, dict)
                and isinstance(t.get("name"), str) and _VERSION_RE.match(t["name"])
                and "-" not in t["name"]]   # pre-releases are never "latest"
    if not versions:
        raise LibraryError(f"{LIBRARY_REPO} has no released versions yet")
    return max(versions, key=_version_key)


def _safe_member(name: str) -> Optional[PurePosixPath]:
    """The path below the archive's top folder, or None for anything outside the library files."""
    parts = PurePosixPath(name).parts
    if len(parts) < 2 or any(p in ("..", "") for p in parts) or name.startswith("/"):
        return None
    rel = PurePosixPath(*parts[1:])
    if rel.parts[0] not in _ENTRY_DIRS + ("docs",) or rel.suffix not in _FILE_SUFFIXES:
        return None
    return rel


def download(version: str, *, cache_dir: Union[str, "os.PathLike[str]", None] = None,
             timeout: float = 30.0) -> Path:
    """Fetch release ``version`` into the cache and return its folder (replaces a partial one)."""
    root = cache_root(cache_dir)
    url = f"{_GITHUB}/{LIBRARY_REPO}/archive/refs/tags/v{version}.tar.gz"
    data = _http_get(url, timeout)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                rel = _safe_member(member.name)
                if rel is None or not member.isfile():
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read())
        index = build_index(tmp, version)
        if not index["entries"]:
            raise LibraryError(f"{url}: the archive has no sensor definitions")
        (tmp / "library.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        final = root / version
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
        return final
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def cached_versions(cache_dir: Union[str, "os.PathLike[str]", None] = None) -> List[str]:
    """Versions in the download cache, oldest first."""
    root = cache_root(cache_dir)
    if not root.is_dir():
        return []
    found = [p.name for p in root.iterdir()
             if p.is_dir() and _VERSION_RE.match(p.name) and (p / "library.json").is_file()]
    return sorted(found, key=_version_key)


def _fallback(version: str, reason: str, cache_dir: Union[str, "os.PathLike[str]", None]) -> SensorLibrary:
    cached = cached_versions(cache_dir)
    if version != "latest" and version in cached:
        lib = SensorLibrary(version, cache_root(cache_dir) / version, source="cache")
        return lib
    if version == "latest" and cached:
        newest = cached[-1]
        msg = (f"sensor library 'latest': {reason}; using the newest cached version {newest}")
        _log.warning("%s", msg)
        return SensorLibrary(newest, cache_root(cache_dir) / newest, source="cache", warning=msg)
    msg = (f"sensor library {version}: {reason}; using the bundled version "
           f"{SENSOR_LIBRARY_VERSION}")
    _log.warning("%s", msg)
    lib = bundled_library()
    lib.warning = msg
    return lib


def sensor_library(version: Optional[str] = None, *,
                   cache_dir: Union[str, "os.PathLike[str]", None] = None,
                   offline: bool = False, refresh: bool = False,
                   timeout: float = 30.0) -> SensorLibrary:
    """The sensor library to use (see :func:`requested_version` for which version).

    The pinned version comes from the package itself. Another version comes from the cache, or is
    downloaded into it; ``"latest"`` asks GitHub for the newest release each time a process first
    uses it. ``offline=True`` never touches the network. ``refresh=True`` downloads again even
    when the version is cached. When a download fails the cache, then the bundled copy, is used
    and :attr:`SensorLibrary.warning` says so.
    """
    want = requested_version(version)
    key = (want, str(cache_root(cache_dir)))
    with _lock:
        if not refresh and key in _loaded:
            return _loaded[key]
        lib = _resolve(want, cache_dir=cache_dir, offline=offline, refresh=refresh, timeout=timeout)
        if not lib.warning:
            _loaded[key] = lib
        return lib


def _resolve(want: str, *, cache_dir: Union[str, "os.PathLike[str]", None], offline: bool,
             refresh: bool, timeout: float) -> SensorLibrary:
    if want == SENSOR_LIBRARY_VERSION:
        return bundled_library()
    root = cache_root(cache_dir)
    if want != "latest" and not refresh and (root / want / "library.json").is_file():
        return SensorLibrary(want, root / want, source="cache")
    if offline:
        return _fallback(want, "offline", cache_dir)
    try:
        version = latest_version(timeout) if want == "latest" else want
        if version == SENSOR_LIBRARY_VERSION and not refresh:
            return bundled_library()
        if not refresh and (root / version / "library.json").is_file():
            return SensorLibrary(version, root / version, source="cache")
        return SensorLibrary(version, download(version, cache_dir=cache_dir, timeout=timeout),
                             source="download")
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and want != "latest":
            cached = cached_versions(cache_dir)
            if want not in cached:
                raise LibraryError(f"the sensor library has no release {want} "
                                   f"(github.com/{LIBRARY_REPO}/releases)") from None
        return _fallback(want, f"download failed (HTTP {exc.code})", cache_dir)
    except (urllib.error.URLError, OSError, tarfile.TarError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return _fallback(want, f"download failed ({reason})", cache_dir)
