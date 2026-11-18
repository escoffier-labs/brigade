"""Closed privacy projection for served run views and the run handoff note (#2046).

Local run files keep raw prompts, worker text, and absolute paths because resume
needs them. ``brigade runs list --json``, ``brigade runs show --json``, the
loopback Run View, its watch stream, and the memory handoff note are readable
surfaces, so they reuse the export rules from :mod:`run_export_projection`:
each payload is projected through the same closed node types the export uses.
Prompt and generated text becomes the keyed ``{digest, chars}`` object, a
mapping key such as an agent name becomes a keyed digest, and every other
string stays readable only when it is a known enum value, an ISO timestamp,
pure hex, a run or event id, a numeric version, or a fixed path token.

The digest key is the same per-host HMAC key the export uses, so a digest is not
portable across hosts and a short prompt cannot be reversed by guessing.

The closed field helpers at the bottom (``_clean_str``, ``_allowlisted`` and
friends) used to live in ``runs_cmd``. They live here so the frozen legacy
module does not grow: every string in a list, detail, or watch contract passes
through ``_clean_str``, and every fixed key set passes through ``_allowlisted``.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from . import run_export_projection

TEXT_PREVIEW_CHARS = 80

RUNS_LIST_SCHEMA = "brigade.runs-list.v2"
RUN_DETAIL_SCHEMA = "brigade.run-detail.v2"
RUN_WATCH_SCHEMA = "brigade.run-watch.v2"

# The serve layer resolves run directory names with this same form. A run id is
# a navigation identifier, so it stays readable. Anything outside it is digested.
RUN_ID = run_export_projection.pattern(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_ID = run_export_projection.pattern(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_RUN_STATUS = run_export_projection.enum(*run_export_projection.RUN_STATUSES, "timeout", "incomplete", "canceled")
# The export set plus the failure phases the run lifecycle records today. An
# unknown phase still becomes "<other>" rather than raw text.
_VIEW_FAILURE_PHASES = (
    *run_export_projection.FAILURE_PHASES,
    "output-validation",
    "harness",
    "admission",
    "startup",
    "handoff",
    "run-isolation",
    "provider-preflight",
    "launch",
    "launch-argument",
    "inference",
    "configured-request",
    "artifact-validation",
    "stale-lock-recovery",
    "worker",
    "workers",
    "result",
    "routing",
)
_FAILURE_PHASE = run_export_projection.enum(*_VIEW_FAILURE_PHASES)
_TRANSPORT = run_export_projection.enum(*run_export_projection.TRANSPORTS)
_WORKER_STATUS = run_export_projection.enum(*run_export_projection.WORKER_STATUSES)
_RUN_MODE = run_export_projection.enum("normal", "read-only", "normal, dry-run", "read-only, dry-run")
_TERMINAL = run_export_projection.enum("terminal", "nonterminal")
_LINEAGE_KIND = run_export_projection.enum("branch", "child", "parent", "root", "unknown")
_SYNTHESIS_MODE = run_export_projection.enum("direct-worker", "orchestrator", "single-seat")
_VERIFY_STATUS = run_export_projection.enum("ok", "completed", "failed", "rejected", "timeout", "running", "unknown")
_LOCAL_STATE = run_export_projection.enum("cleared", "required", "unknown")
_BRIEF_NAME = run_export_projection.enum("code-graph", "drift-impact", "evidence")
_WATCH_TYPE = run_export_projection.enum(
    "watch", "run", "plan", "event", "workers", "synthesis", "final", "summary", "error"
)
_DIAGNOSTIC_CODE = run_export_projection.enum("digest-key-unavailable", "run-view-unavailable")

PROMPT = run_export_projection.PROMPT
NUMBER = run_export_projection.NUMBER
INT = run_export_projection.INT
BOOL = run_export_projection.BOOL

_SHARED_PREFIX = run_export_projection.object_node(
    {
        "event_sequence": INT,
        "event_digest": run_export_projection.SHA_OR_HEX_ID,
        "previous_digest": run_export_projection.SHA_OR_HEX_ID,
    }
)
_CHILD = run_export_projection.object_node(
    {
        "run_id": RUN_ID,
        "status": _RUN_STATUS,
        "branch_point_event_id": EVENT_ID,
    }
)
_LINEAGE = run_export_projection.object_node(
    {
        "kind": _LINEAGE_KIND,
        "parent_run_id": RUN_ID,
        "branch_point_event_id": EVENT_ID,
        "shared_prefix": _SHARED_PREFIX,
        "children": run_export_projection.list_node(_CHILD),
    }
)
_RUN_FAILURE = run_export_projection.object_node({"phase": _FAILURE_PHASE, "kind": PROMPT, "detail": PROMPT})
_RUN_SECTION = run_export_projection.object_node(
    {
        "run_id": RUN_ID,
        "status": _RUN_STATUS,
        "task": PROMPT,
        "mode": _RUN_MODE,
        "started_at": run_export_projection.ISO_TIMESTAMP,
        "finished_at": run_export_projection.ISO_TIMESTAMP,
        "duration_seconds": NUMBER,
        "failure": _RUN_FAILURE,
        "error": PROMPT,
        "suspected_noop": BOOL,
        "resume_available": BOOL,
        "terminal": _TERMINAL,
        "lineage": _LINEAGE,
    }
)
_RUN_SUMMARY = run_export_projection.object_node(
    {
        "run_id": RUN_ID,
        "status": _RUN_STATUS,
        "task": PROMPT,
        "started_at": run_export_projection.ISO_TIMESTAMP,
        "finished_at": run_export_projection.ISO_TIMESTAMP,
        "duration_seconds": NUMBER,
        "failure_phase": _FAILURE_PHASE,
        "mode": _RUN_MODE,
        "resume_available": BOOL,
        "parent_run_id": RUN_ID,
    }
)
_ROSTER_AGENT = run_export_projection.object_node(
    {
        "cli": PROMPT,
        "model": PROMPT,
        "reasoning": PROMPT,
        "role": PROMPT,
        "timeout_seconds": NUMBER,
    }
)
_ROSTER = run_export_projection.object_node(
    {
        "orchestrator": PROMPT,
        "max_workers": INT,
        "timeout_seconds": NUMBER,
        "allow_models": run_export_projection.list_node(PROMPT),
        "agents": run_export_projection.object_node({}, any_name=_ROSTER_AGENT, digest_keys=True),
    }
)
_ASSIGNMENT = run_export_projection.object_node({"stage": INT, "worker": PROMPT, "task": PROMPT})
_PLAN = run_export_projection.object_node({"assignments": run_export_projection.list_node(_ASSIGNMENT)})
_WORKER_FAILURE = run_export_projection.object_node({"class": PROMPT, "detail": PROMPT})
_WORKER_RESULT = run_export_projection.object_node(
    {
        "worker": PROMPT,
        "ok": BOOL,
        "status": _WORKER_STATUS,
        "task": PROMPT,
        "detail": PROMPT,
        "duration_seconds": NUMBER,
        "exit_code": INT,
        "timed_out": BOOL,
        "requested_model": PROMPT,
        "transport": _TRANSPORT,
        "failure": _WORKER_FAILURE,
    }
)
_WORKERS = run_export_projection.object_node({"results": run_export_projection.list_node(_WORKER_RESULT)})
_AGENT_RESULT = run_export_projection.object_node(
    {
        "ok": BOOL,
        "detail": PROMPT,
        "duration_seconds": NUMBER,
        "exit_code": INT,
        "requested_model": PROMPT,
    }
)
_SYNTHESIS = run_export_projection.object_node(
    {"orchestrator": PROMPT, "mode": _SYNTHESIS_MODE, "result": _AGENT_RESULT}
)
_VERIFY_COMMAND = run_export_projection.object_node({"command": PROMPT, "exit_code": INT, "duration_seconds": NUMBER})
_VERIFY = run_export_projection.object_node(
    {
        "run_id": RUN_ID,
        "status": _VERIFY_STATUS,
        "started_at": run_export_projection.ISO_TIMESTAMP,
        "duration_seconds": NUMBER,
        "commands": run_export_projection.list_node(_VERIFY_COMMAND),
    }
)
_BRIEF = run_export_projection.object_node({"name": _BRIEF_NAME, "attached": BOOL, "bytes": INT, "pending_count": INT})
_DETAIL = run_export_projection.object_node(
    {
        "schema": run_export_projection.enum(RUN_DETAIL_SCHEMA),
        "run": _RUN_SECTION,
        "roster": _ROSTER,
        "plan": _PLAN,
        "workers": _WORKERS,
        "synthesis": _SYNTHESIS,
        "verification": run_export_projection.list_node(_VERIFY),
        "briefs": run_export_projection.list_node(_BRIEF),
    }
)
_WATCH_EVENT = run_export_projection.object_node({"method": PROMPT, "item_type": PROMPT})
_WATCH = run_export_projection.object_node(
    {
        "schema": run_export_projection.enum(RUN_WATCH_SCHEMA),
        "type": _WATCH_TYPE,
        "run_id": RUN_ID,
        "status": _RUN_STATUS,
        "task": PROMPT,
        "started_at": run_export_projection.ISO_TIMESTAMP,
        "finished_at": run_export_projection.ISO_TIMESTAMP,
        "duration_seconds": NUMBER,
        "failure_phase": _FAILURE_PHASE,
        "failure_kind": PROMPT,
        "failure_detail": PROMPT,
        "assignments": run_export_projection.list_node(_ASSIGNMENT),
        "worker": PROMPT,
        "event": _WATCH_EVENT,
        "results": run_export_projection.list_node(_WORKER_RESULT),
        "orchestrator": PROMPT,
        "mode": _SYNTHESIS_MODE,
        "result": _AGENT_RESULT,
        "text": PROMPT,
        "inspect_command": PROMPT,
        "recover_status": _LOCAL_STATE,
        "resume_available": BOOL,
        "error": _DIAGNOSTIC_CODE,
    }
)


DIGEST_KEY_UNAVAILABLE = "error: run digest key unavailable"
VIEW_UNAVAILABLE = "error: run view unavailable"


class DigestKeyUnavailable(RuntimeError):
    """The per-host digest key is refused or cannot be created."""


@lru_cache(maxsize=64)
def _key_for_target(target: Path) -> bytes:
    return run_export_projection.require_digest_key(target)


def digest_key(run_dir: Path) -> bytes:
    """Return the per-host keyed digest key for the workspace that owns *run_dir*."""
    try:
        return _key_for_target(run_export_projection.digest_key_target(run_dir))
    except run_export_projection.RunExportProjectionError as exc:
        raise DigestKeyUnavailable("run digest key unavailable") from exc


def task_preview(value: object) -> str | None:
    """Return a bounded, home-redacted, one-line task preview for ``--show-task``."""
    return _clean_str(value, TEXT_PREVIEW_CHARS)


def text_value(value: object) -> str | None:
    """Return a raw string for the projection node, or None when absent."""
    return value if isinstance(value, str) and value else None


_ERROR_CODES = frozenset({"digest-key-unavailable", "run-view-unavailable"})


def error_record(code: str) -> dict[str, Any]:
    """Return the one closed SSE record sent when the view cannot be projected."""
    closed = code if code in _ERROR_CODES else "run-view-unavailable"
    return {"schema": RUN_WATCH_SCHEMA, "type": "error", "error": closed}


def _project(value: Any, node: Any, run_dir: Path, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise run_export_projection.RunExportProjectionError(f"{label} must be an object")
    projected = run_export_projection.project_node(
        dict(value),
        node,
        workspace=run_export_projection.workspace_for_run(run_dir),
        run_dir=run_dir,
        key=digest_key(run_dir),
        label=label,
    )
    run_export_projection.assert_node(projected, node, label=label)
    return projected


def project_summary(
    payload: Mapping[str, Any], run_dir: Path, *, task: object = None, show_task: bool = False
) -> dict[str, Any]:
    """Project one ``brigade.runs-list.v2`` run summary."""
    projected = _project(payload, _RUN_SUMMARY, run_dir, label="brigade.runs-list.v2")
    if show_task:
        preview = task_preview(task)
        if preview:
            projected["task_preview"] = preview
    return projected


def project_detail(
    payload: Mapping[str, Any], run_dir: Path, *, task: object = None, show_task: bool = False
) -> dict[str, Any]:
    """Project one ``brigade.run-detail.v2`` payload."""
    projected = _project(payload, _DETAIL, run_dir, label="brigade.run-detail.v2")
    if show_task:
        preview = task_preview(task)
        run_section = projected.get("run")
        if preview and isinstance(run_section, dict):
            run_section["task_preview"] = preview
    return projected


def watch_record_projector(run_dir: Path) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Return a projection callable for ``brigade.run-watch.v2`` records."""
    key = digest_key(run_dir)
    workspace = run_export_projection.workspace_for_run(run_dir)

    def project(record: Mapping[str, Any]) -> dict[str, Any]:
        projected = run_export_projection.project_node(
            dict(record),
            _WATCH,
            workspace=workspace,
            run_dir=run_dir,
            key=key,
            label="brigade.run-watch.v2",
        )
        run_export_projection.assert_node(projected, _WATCH, label="brigade.run-watch.v2")
        return projected

    return project


# --- closed field helpers (#631) moved out of the frozen runs_cmd module ---


# Windows alternative is first so `C:/Users/<name>` is not left as `C:~`.
# `/+` and `[/\\]+` accept doubled separators; `[^/\\]+` keeps spaces and apostrophes.
_HOME_PREFIX_RE = re.compile(
    r"(?:[A-Za-z]:[/\\]+(?i:Users)[/\\]+|/(?:home|Users)/+)[^/\\]+",
)


def _redact_home_prefixes(text: str) -> str:
    """Rewrite POSIX and Windows home prefixes to `~` for JSON contracts."""
    return _HOME_PREFIX_RE.sub("~", text)


def _short(text: object, limit: int = 72) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _clean_str(value: object, limit: int | None = None) -> str | None:
    """Bound, then home-prefix-redact, every string that enters a JSON contract."""
    if not isinstance(value, str) or not value:
        return None
    cleaned = _short(value, limit) if limit is not None else value
    return _redact_home_prefixes(cleaned)


def _clean_str_list(value: object) -> list[str] | None:
    """Clean every string in a JSON-contract list; drop non-strings and empties."""
    if not isinstance(value, list):
        return None
    return [item for raw in value if (item := _clean_str(raw)) is not None]


def _clean_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _compact(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value is not None}


class _MergedSource(dict[str, object]):
    """Source overlay that records which keys passed through the cleaner."""

    cleaned_keys: frozenset[str]

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(data or {})
        self.cleaned_keys = frozenset()


def _merge_source(source: Mapping[str, Any] | None, cleaned: Mapping[str, object]) -> dict[str, object]:
    """Overlay cleaned fields on the source mapping so `_allowlisted` must drop extras."""
    payload = _MergedSource(source)
    payload.update(cleaned)
    payload.cleaned_keys = frozenset(cleaned)
    return payload


def _allowlisted(payload: Mapping[str, object], allowed: frozenset[str]) -> dict[str, object]:
    """Keep only explicitly allowlisted keys; drop None the same way as `_compact`."""
    if not isinstance(payload, _MergedSource):
        raise AssertionError("JSON contract allowlist requires _merge_source overlay")
    missing = allowed - payload.cleaned_keys
    if missing:
        raise AssertionError("allowlist keys have no cleaned counterpart: " + ", ".join(sorted(missing)))
    return {key: value for key, value in payload.items() if key in allowed and value is not None}
