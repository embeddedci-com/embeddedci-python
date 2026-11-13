"""User-defined sensors: definitions, the public sensor library and the pod's sensor emulation.

``bp.sensors`` (a :class:`SensorEmulation`) drives the pod; :func:`sensor_library` gives the
library of ready definitions (version :data:`SENSOR_LIBRARY_VERSION` ships with the SDK);
:func:`load_definition` and :func:`load_stimulus` read YAML or JSON with the YAML 1.2 rules.
"""

from .definition import (
    MAX_DOCUMENT_BYTES,
    SENSOR_FORMAT,
    SENSOR_KINDS,
    DefinitionError,
    check_definition,
    check_stimulus,
    definition_errors,
    load_definition,
    load_document,
    load_stimulus,
    parse_text,
    stimulus_errors,
    to_json_bytes,
)
from .emulation import (
    INSTANCE_RE,
    MAX_CHUNK_BYTES,
    MAX_COMMAND_BYTES,
    MAX_LA_BYTES,
    PIN_ROLES,
    TOO_OLD,
    Sensor,
    SensorEmulation,
    decode_regs,
    upload_requests,
)
from .library import (
    SENSOR_CACHE_ENV,
    SENSOR_LIBRARY_ENV,
    SENSOR_LIBRARY_VERSION,
    LibraryEntry,
    LibraryError,
    SensorLibrary,
    bundled_library,
    cache_root,
    cached_versions,
    latest_version,
    requested_version,
    sensor_library,
)
from .yaml12 import YamlError, load_yaml

__all__ = [
    "INSTANCE_RE", "MAX_CHUNK_BYTES", "MAX_COMMAND_BYTES", "MAX_LA_BYTES", "MAX_DOCUMENT_BYTES", "PIN_ROLES", "SENSOR_CACHE_ENV", "SENSOR_FORMAT",
    "SENSOR_KINDS", "SENSOR_LIBRARY_ENV", "SENSOR_LIBRARY_VERSION", "TOO_OLD",
    "DefinitionError", "LibraryEntry", "LibraryError", "Sensor", "SensorEmulation",
    "SensorLibrary", "YamlError",
    "bundled_library", "cache_root", "cached_versions", "check_definition", "check_stimulus",
    "decode_regs", "definition_errors", "latest_version", "load_definition", "load_document", "load_stimulus",
    "load_yaml", "parse_text", "requested_version", "sensor_library", "stimulus_errors",
    "to_json_bytes", "upload_requests",
]
