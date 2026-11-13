"""Load sensor definitions and stimuli (YAML or JSON) into the JSON document a pod receives.

The format is the sensor library's ``docs/format.md`` (format 1). The pod compiles a definition
and reports what is wrong with it; the checks here are only the header (``format``, ``kind``,
``name``, ``params``) and the size, so a file that is plainly not a definition fails before
anything is sent.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Union

from .yaml12 import YamlError, load_yaml

if TYPE_CHECKING:  # pragma: no cover
    from .library import SensorLibrary

#: The definition format this SDK writes and checks (``sensors_format`` in a pod's status).
SENSOR_FORMAT = 1
#: Definition kinds a pod plays.
SENSOR_KINDS = ("i2c", "spi", "uart", "analog")
#: Largest definition or stimulus JSON a pod accepts, in bytes.
MAX_DOCUMENT_BYTES = 32768
#: Instance and definition names: ``[a-z0-9_]{1,32}``.
NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")

DefinitionSource = Union[str, "os.PathLike[str]", Mapping[str, Any]]


class DefinitionError(ValueError):
    """A definition or stimulus that cannot be read, or whose header is wrong. ``path`` is the
    file when it came from one."""

    def __init__(self, message: str, *, path: Optional[str] = None) -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def parse_text(text: str, *, path: Optional[str] = None) -> Dict[str, Any]:
    """A definition or stimulus from YAML or JSON text (JSON is read as JSON, so its numbers and
    strings stay exactly as written)."""
    stripped = text.lstrip()
    try:
        doc = json.loads(text) if stripped.startswith("{") else load_yaml(text)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"invalid JSON: {exc}", path=path) from None
    except YamlError as exc:
        raise DefinitionError(f"invalid YAML: {exc}", path=path) from None
    if not isinstance(doc, dict):
        raise DefinitionError("a definition is a mapping (format, kind, name, ...)", path=path)
    return doc


def read_file(path: Union[str, "os.PathLike[str]"]) -> Dict[str, Any]:
    """A definition or stimulus from a ``.yaml``/``.yml``/``.json`` file; a stimulus's ``data``
    segments with ``file:`` (CSV ``t,value``, relative to the file) become ``points``."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DefinitionError(f"cannot read: {exc.strerror or exc}", path=str(p)) from None
    doc = parse_text(text, path=str(p))
    return inline_data_files(doc, p.parent)


def _looks_like_text(source: str) -> bool:
    return "\n" in source or source.lstrip().startswith("{")


def load_document(source: DefinitionSource, *, library: Optional["SensorLibrary"] = None,
                  base_dir: Union[str, "os.PathLike[str]", None] = None) -> Dict[str, Any]:
    """A definition or stimulus as a dict, from any of:

    * a mapping (copied),
    * a path to a YAML or JSON file,
    * YAML or JSON text (anything with a newline, or starting with ``{``),
    * the name of a library entry (``"bme280"``) or its path in the library
      (``"sensors/i2c/bme280.yaml"``), looked up in ``library`` (default: the active library).

    ``data`` segments with ``file:`` are inlined relative to the file, or to ``base_dir``
    (default: the working directory) for a mapping or text.
    """
    if isinstance(source, Mapping):
        doc = json.loads(json.dumps(dict(source)))
        return inline_data_files(doc, Path(base_dir or "."))
    if isinstance(source, os.PathLike):
        return read_file(source)
    if not isinstance(source, str):
        raise TypeError(f"a definition is a dict, a path, YAML/JSON text or a library name, "
                        f"got {type(source).__name__}")
    if _looks_like_text(source):
        return inline_data_files(parse_text(source), Path(base_dir or "."))
    if os.path.isfile(source):
        return read_file(source)
    if library is None:
        from .library import sensor_library

        library = sensor_library()
    entry = library.find(source)
    if entry is None:
        names = ", ".join(e.name for e in library.entries) or "none"
        raise DefinitionError(
            f"{source!r} is neither a file nor an entry of the sensor library {library.version} "
            f"(entries: {names})")
    return library.load(entry.name)


def load_definition(source: DefinitionSource, *, library: Optional["SensorLibrary"] = None,
                    base_dir: Union[str, "os.PathLike[str]", None] = None) -> Dict[str, Any]:
    """A sensor definition from a path, a dict, YAML/JSON text or a library name (see
    :func:`load_document`), with its header checked (:func:`check_definition`)."""
    doc = load_document(source, library=library, base_dir=base_dir)
    check_definition(doc)
    return doc


def load_stimulus(source: DefinitionSource, *, library: Optional["SensorLibrary"] = None,
                  base_dir: Union[str, "os.PathLike[str]", None] = None) -> Dict[str, Any]:
    """A stimulus from a path, a dict, YAML/JSON text or a library name, with ``data`` segment
    files inlined and its header checked (:func:`check_stimulus`)."""
    doc = load_document(source, library=library, base_dir=base_dir)
    check_stimulus(doc)
    return doc


def _check_common(doc: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    fmt = doc.get("format")
    if fmt is None:
        errors.append("format: missing (format: 1)")
    elif isinstance(fmt, bool) or fmt != SENSOR_FORMAT:
        errors.append(f"format: {fmt!r} is not supported (this SDK reads format {SENSOR_FORMAT})")
    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"name: {name!r} must match [a-z0-9_]{{1,32}}")
    return errors


def definition_errors(doc: Mapping[str, Any]) -> List[str]:
    """What is wrong with a definition's header and size, ``[]`` when nothing (the pod checks
    the rest)."""
    errors = _check_common(doc)
    kind = doc.get("kind")
    if kind == "stimulus":
        errors.append("kind: this is a stimulus, not a sensor definition")
    elif kind not in SENSOR_KINDS:
        errors.append(f"kind: {kind!r} must be one of {', '.join(SENSOR_KINDS)}")
    label = doc.get("label")
    if label is not None and not isinstance(label, str):
        errors.append("label: must be text")
    params = doc.get("params", [])
    if not isinstance(params, list):
        errors.append("params: must be a list")
    else:
        for i, p in enumerate(params):
            if not isinstance(p, Mapping) or not isinstance(p.get("key"), str) or not p.get("key"):
                errors.append(f"params[{i}]: needs a key")
    errors += _size_errors(doc)
    return errors


def stimulus_errors(doc: Mapping[str, Any]) -> List[str]:
    """What is wrong with a stimulus's header and size, ``[]`` when nothing."""
    errors = _check_common(doc)
    if doc.get("kind") != "stimulus":
        errors.append(f"kind: {doc.get('kind')!r} must be stimulus")
    signals = doc.get("signals")
    if signals is not None and not isinstance(signals, Mapping):
        errors.append("signals: must be a mapping of param to segments")
    faults = doc.get("faults")
    if faults is not None and not isinstance(faults, list):
        errors.append("faults: must be a list")
    errors += _size_errors(doc)
    return errors


def _size_errors(doc: Mapping[str, Any]) -> List[str]:
    size = len(to_json_bytes(doc))
    if size > MAX_DOCUMENT_BYTES:
        return [f"{size} bytes of JSON, more than the pod's {MAX_DOCUMENT_BYTES}"]
    return []


def check_definition(doc: Mapping[str, Any]) -> None:
    """Raise :class:`DefinitionError` listing every header problem of a definition."""
    errors = definition_errors(doc)
    if errors:
        raise DefinitionError("not a valid sensor definition: " + "; ".join(errors))


def check_stimulus(doc: Mapping[str, Any]) -> None:
    """Raise :class:`DefinitionError` listing every header problem of a stimulus."""
    errors = stimulus_errors(doc)
    if errors:
        raise DefinitionError("not a valid stimulus: " + "; ".join(errors))


def to_json_bytes(doc: Mapping[str, Any]) -> bytes:
    """The compact UTF-8 JSON the pod receives."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _read_csv_points(path: Path) -> List[List[float]]:
    points: List[List[float]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DefinitionError(f"cannot read the data file: {exc.strerror or exc}",
                              path=str(path)) from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cells = line.split(",")
        if len(cells) < 2:
            raise DefinitionError(f"expected 't,value', got {line!r}", path=str(path))
        try:
            points.append([float(cells[0]), float(cells[1])])
        except ValueError:
            if points:
                raise DefinitionError(f"not a number: {line!r}", path=str(path)) from None
            continue  # a header line
    return points


def inline_data_files(doc: Dict[str, Any], base_dir: Union[str, "os.PathLike[str]"]) -> Dict[str, Any]:
    """Replace each stimulus ``data`` segment's ``file:`` (CSV ``t,value``) with ``points``: the
    pod receives one JSON document. Other documents are returned unchanged."""
    base = Path(base_dir)

    def walk(segments: Any) -> None:
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("shape") == "repeat":
                walk(seg.get("segments"))
            if seg.get("shape") == "data" and "file" in seg:
                seg["points"] = _read_csv_points(base / str(seg.pop("file")))

    if doc.get("kind") == "stimulus" and isinstance(doc.get("signals"), Mapping):
        for segments in doc["signals"].values():
            walk(segments)
    return doc
