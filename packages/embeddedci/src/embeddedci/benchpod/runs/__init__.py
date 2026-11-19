"""Test run recording: the run folder (``run.json``, ``junit.xml``, per-case pod-call traces) the
pytest plugin writes with ``--embeddedci-run-dir`` or under ``benchpod ci``.

- :mod:`.schema`: the file formats (schema version 1), redaction, caps, case slugs.
- :mod:`.recorder`: :class:`Recorder`, which records one transport instance's calls.
- :mod:`.active`: :class:`RunRecording` and the process's active run (:func:`active_run`), which
  every :class:`~embeddedci.benchpod.BenchPod` opened while it is active attaches to.
- :mod:`.provenance`: git, tool, GitHub and pod details for ``inputs``.
"""

from .active import (
    KEEP_TRACES_ENV,
    RUN_DIR_ENV,
    RUN_ID_ENV,
    RUN_STEP_ENV,
    RUN_T0_ENV,
    RunRecording,
    active_run,
    clear_active_run,
    default_run_dir,
    new_run_id,
    set_active_run,
)
from .recorder import Recorder, RecordingLink
from .schema import (
    SCHEMA_VERSION,
    CallStats,
    Case,
    Measurement,
    case_slug,
    junit_xml,
    parse_drift,
    redact,
    run_outcome,
)

__all__ = [
    "KEEP_TRACES_ENV",
    "RUN_DIR_ENV",
    "RUN_ID_ENV",
    "RUN_STEP_ENV",
    "RUN_T0_ENV",
    "SCHEMA_VERSION",
    "CallStats",
    "Case",
    "Measurement",
    "Recorder",
    "RecordingLink",
    "RunRecording",
    "active_run",
    "case_slug",
    "clear_active_run",
    "default_run_dir",
    "junit_xml",
    "new_run_id",
    "parse_drift",
    "redact",
    "run_outcome",
    "set_active_run",
]
