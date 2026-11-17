"""Closed export projection for ``brigade.work-run`` archives (#2046).

Local run files keep raw prompts, model text, and absolute paths because resume
needs them. Privacy is enforced only on the staged copy that becomes an export:
a closed file set, a closed shape per receipt kind, keyed prompt digests, and
workspace-relative paths. ``work_run_archive`` calls this module before its
privacy assertion, and import calls the same assertion before installing.

The prompt digest reuses the per-host HMAC key from :mod:`memory_recall_log`
so a short prompt cannot be reversed by guessing and the digest is not portable
across hosts. Evidence packages and remote execution transport are outside this
guarantee.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import localio, memory_recall_log, receipt_schema, run_checkpoint, run_projector, worker_events

DIGEST_PREFIX = "hmac-sha256:"
EXTERNAL_PATH_TOKEN = "<external-path>"
RUN_DIR_TOKEN = "<run-dir>"
HOME_TOKEN = "<home>"

_DIGEST_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_BATCH_RESULT_RE = re.compile(r"^batch-result-[0-9a-f]{64}\.json$")

_PROMPT_KEYS = frozenset("task text detail final error prompt stdout stderr output result content summary".split())
_PATH_KEYS = frozenset(
    "cwd lock_workspace artifacts handoff handoff_inbox control_socket worktree "
    "stdout_log stderr_log patch_ref path".split()
)
_TOP_LEVEL_JSON = frozenset(
    "run.json roster.json plan.json worker-results.json synthesis.json batch-request.json".split()
)
_TOP_LEVEL_FILES = _TOP_LEVEL_JSON | frozenset("final.txt summary.md changes.patch".split())


class RunExportProjectionError(ValueError):
    """Export projection refused an unknown kind value or unsafe content."""


@dataclass(frozen=True)
class _Object:
    fields: Mapping[str, "Node"]


@dataclass(frozen=True)
class _List:
    item: "Node"


@dataclass(frozen=True)
class _Any:
    pass


@dataclass(frozen=True)
class _Prompt:
    pass


Node = _Object | _List | _Any | _Prompt

ANY = _Any()
PROMPT = _Prompt()


def _object(keys: frozenset[str] | set[str]) -> _Object:
    return _Object({key: ANY for key in keys})


def _shape(keys: str, **overrides: Node) -> _Object:
    fields: dict[str, Node] = {key: ANY for key in keys.split()}
    fields.update(overrides)
    return _Object(fields)


_WORKER_ENTRY_KEYS = frozenset(
    "worker task ok detail text failure_phase failure_kind failure transport_warning "
    "cloud_environment metadata_provenance output_truncated output_bytes output_cap_bytes "
    "thread_id status exit_code timed_out stdout_log stderr_log duration_seconds transport "
    "requested_model effective_model reasoning stop_reason protocol_version session_id "
    "request_id acpx_version events env_overrides endpoint_host attempts provenance".split()
)
_ATTEMPT_KEYS = frozenset(
    "kind worker task transport reasoning started_at finished_at exit_code terminal_reason "
    "failure_phase failure_kind session_id selected model stdout_log stderr_log failure".split()
)
_AGENT_RESULT_KEYS = frozenset(
    "ok detail text failure_phase failure_kind failure transport_warning cloud_environment "
    "exit_code timed_out stdout_log stderr_log duration_seconds transport requested_model "
    "effective_model reasoning stop_reason protocol_version session_id request_id "
    "acpx_version metadata_provenance events".split()
)
_ROSTER_AGENT_KEYS = frozenset(
    "cli command model reasoning transport transport_version role timeout_seconds "
    "invalid_final_fallback read_only_capable cloud_safe_mode env capabilities".split()
)
_RUN_KEYS = frozenset(run_projector.OWNED_FIELDS | {"run_id"})
_PLAN_ASSIGNMENT_KEYS = frozenset(
    "stage worker task covers selected_skill_ids domain capabilities max_risk_class admissible_tool_ids".split()
)
_BATCH_TASK_KEYS = frozenset("task_id text seat depends_on isolation read_only".split())

_WORKER_ENTRY = _Object(
    {
        key: (
            PROMPT
            if key in _PROMPT_KEYS
            else _List(_Object({item: ANY for item in _ATTEMPT_KEYS}))
            if key == "attempts"
            else ANY
        )
        for key in _WORKER_ENTRY_KEYS
    }
)
_AGENT_RESULT = _Object({key: PROMPT if key in _PROMPT_KEYS else ANY for key in _AGENT_RESULT_KEYS})

_ANY_NAME = "@any-name"
_ROSTER_AGENT = _Object({key: ANY for key in _ROSTER_AGENT_KEYS})

_PLAN_ASSIGNMENT = _Object({key: PROMPT if key == "task" else ANY for key in _PLAN_ASSIGNMENT_KEYS})
_BATCH_TASK = _Object({key: PROMPT if key == "text" else ANY for key in _BATCH_TASK_KEYS})

_FILE_NODES: dict[str, Node] = {
    "run.json": _object(_RUN_KEYS),
    "roster.json": _shape(
        "schema schema_version orchestrator max_workers timeout_seconds allow_models sandbox "
        "resolution model_admission admissions runtime_admissions",
        agents=_Object({_ANY_NAME: _ROSTER_AGENT}),
    ),
    "plan.json": _shape("schema schema_version causal_receipt", assignments=_List(_PLAN_ASSIGNMENT)),
    "worker-results.json": _shape(
        "schema schema_version ground_truth producer_run_id causal_receipt", results=_List(_WORKER_ENTRY)
    ),
    "synthesis.json": _shape(
        "schema schema_version mode worker orchestrator ground_truth causal_receipt", result=_AGENT_RESULT
    ),
    "batch-request.json": _shape(
        "schema request_id base_sha fingerprint layers",
        tasks=_List(_BATCH_TASK),
        synthesis=_Object({"text": PROMPT, "seat": ANY}),
    ),
    "batch-result.json": _WORKER_ENTRY,
}

_SCHEMAS: dict[str, tuple[str, int | None]] = {
    "run.json": (receipt_schema.RUN_RECEIPT_SCHEMA, receipt_schema.RUN_RECEIPT_SCHEMA_VERSION),
    "roster.json": (receipt_schema.ROSTER_SNAPSHOT_SCHEMA, receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION),
    "plan.json": (receipt_schema.RUN_PLAN_SCHEMA, receipt_schema.RUN_PLAN_SCHEMA_VERSION),
    "worker-results.json": (receipt_schema.WORKER_RESULTS_SCHEMA, receipt_schema.WORKER_RESULTS_SCHEMA_VERSION),
    "synthesis.json": (receipt_schema.SYNTHESIS_SCHEMA, receipt_schema.SYNTHESIS_SCHEMA_VERSION),
    "batch-request.json": ("brigade.run_batch.v1", None),
}


def is_allowed_export_rel(rel: str) -> bool:
    """True when ``rel`` is in the closed export file set."""
    if rel in _TOP_LEVEL_FILES:
        return True
    if _BATCH_RESULT_RE.fullmatch(rel):
        return True
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or "\\" in rel:
        return False
    if rel == "events/lifecycle.jsonl":
        return True
    if rel.startswith(f"events/{run_checkpoint.CHECKPOINT_DIR_NAME}/") and rel.endswith(".json"):
        return len(pure.parts) == 3
    return worker_events.is_scrubbed_worker_event_stream_rel(rel)


def filter_export_files(payload_root: Path) -> list[str]:
    """Drop every staged file outside the closed export set and return its rel path."""
    payload_root = Path(payload_root)
    dropped: list[str] = []
    directories: list[Path] = []
    for path in sorted(payload_root.rglob("*")):
        if path.is_symlink():
            raise RunExportProjectionError(f"export projection refused symlink: {path.name}")
        if path.is_dir():
            directories.append(path)
            continue
        if not path.is_file():
            raise RunExportProjectionError(f"export projection refused special file: {path.name}")
        rel = path.relative_to(payload_root).as_posix()
        if is_allowed_export_rel(rel):
            continue
        path.unlink()
        dropped.append(rel)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return dropped


def require_digest_key(workspace: Path) -> bytes:
    """Return the per-host HMAC key, refusing export when it cannot be loaded."""
    key = memory_recall_log.load_or_create_key(workspace)
    if key is None:
        raise RunExportProjectionError("could not load the per-host export digest key")
    return key


def digest_prompt(key: bytes, text: str) -> dict[str, Any]:
    """Return the closed keyed-digest shape for one prompt or generated text."""
    return {
        "digest": DIGEST_PREFIX + hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest(),
        "chars": len(text),
    }


@dataclass(frozen=True)
class _Context:
    workspace: Path
    run_dir: Path
    home: Path
    key: bytes


def _map_path(value: str, ctx: _Context) -> str:
    if value.startswith("~"):
        candidate = Path(value).expanduser()
    else:
        candidate = Path(value)
    if not candidate.is_absolute():
        return value
    try:
        relative = candidate.relative_to(ctx.run_dir)
    except ValueError:
        pass
    else:
        rel = relative.as_posix()
        return RUN_DIR_TOKEN if rel in {"", "."} else f"{RUN_DIR_TOKEN}/{rel}"
    try:
        relative = candidate.relative_to(ctx.workspace)
    except ValueError:
        return EXTERNAL_PATH_TOKEN
    rel = relative.as_posix()
    return "." if rel in {"", "."} else rel


def _sanitize_string(value: str, ctx: _Context) -> str:
    if value.startswith("/") or value.startswith("~"):
        return _map_path(value, ctx)
    home = str(ctx.home)
    if home and home in value:
        return value.replace(home, HOME_TOKEN)
    return value


def _project_value(key: str, value: Any, node: Node, ctx: _Context) -> Any:
    if isinstance(value, str):
        if key in _PROMPT_KEYS:
            return digest_prompt(ctx.key, value)
        if key in _PATH_KEYS or key.endswith("_path"):
            return _map_path(value, ctx)
        return _sanitize_string(value, ctx)
    if isinstance(node, _Prompt):
        return _sanitize_any(value, ctx)
    return _project_node(value, node, ctx)


def _sanitize_any(value: Any, ctx: _Context) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value, ctx)
    if isinstance(value, list):
        return [_sanitize_any(item, ctx) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunExportProjectionError("export projection refused a non-string JSON key")
            result[key] = _project_value(key, item, ANY, ctx)
        return result
    return value


def _project_node(value: Any, node: Node, ctx: _Context) -> Any:
    if isinstance(node, _Any):
        return _sanitize_any(value, ctx)
    if isinstance(node, _List):
        if not isinstance(value, list):
            return _sanitize_any(value, ctx)
        return [_project_node(item, node.item, ctx) for item in value]
    if isinstance(node, _Object):
        if not isinstance(value, Mapping):
            return _sanitize_any(value, ctx)
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunExportProjectionError("export projection refused a non-string JSON key")
            child = node.fields.get(key)
            if child is None and _ANY_NAME in node.fields:
                child = node.fields[_ANY_NAME]
            if child is None:
                continue
            result[key] = _project_value(key, item, child, ctx)
        return result
    return _sanitize_any(value, ctx)


def _document_node(rel: str) -> Node:
    if rel in _FILE_NODES:
        return _FILE_NODES[rel]
    return _FILE_NODES["batch-result.json"]


def _check_schema(rel: str, document: Mapping[str, Any]) -> None:
    expected = _SCHEMAS.get(rel)
    if expected is None:
        return
    schema, version = expected
    if document.get("schema") != schema:
        raise RunExportProjectionError(f"{rel} has unsupported schema {document.get('schema')!r}")
    if version is not None and document.get("schema_version") != version:
        raise RunExportProjectionError(f"{rel} has unsupported schema_version {document.get('schema_version')!r}")


def project_document(rel: str, document: Any, ctx: _Context) -> Any:
    """Return a projected copy of one allowed JSON document."""
    if not isinstance(document, Mapping):
        raise RunExportProjectionError(f"{rel} must contain a JSON object")
    _check_schema(rel, document)
    return _project_node(document, _document_node(rel), ctx)


def iter_receipt_files(payload_root: Path) -> list[str]:
    """Return allowed receipt JSON paths (excluding scrubbed streams and checkpoints)."""
    payload_root = Path(payload_root)
    rels: list[str] = []
    for name in sorted(_TOP_LEVEL_JSON):
        if (payload_root / name).is_file():
            rels.append(name)
    for path in sorted(payload_root.glob("batch-result-*.json")):
        if path.is_file():
            rels.append(path.relative_to(payload_root).as_posix())
    return rels


def project_payload_tree(
    payload_root: Path,
    *,
    workspace: Path,
    run_dir: Path,
    key: bytes | None = None,
) -> int:
    """Project every allowed receipt JSON on the staged copy in place."""
    payload_root = Path(payload_root).resolve()
    ctx = _Context(
        workspace=Path(workspace).resolve(),
        run_dir=Path(run_dir).resolve(),
        home=Path.home(),
        key=key if key is not None else require_digest_key(workspace),
    )
    projected = 0
    for rel in iter_receipt_files(payload_root):
        path = payload_root / rel
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunExportProjectionError(f"{rel} is not readable JSON: {exc}") from exc
        localio.write_json(path, project_document(rel, document, ctx))
        projected += 1
    return projected


def _assert_strings(value: Any, *, label: str, home: Path) -> None:
    if isinstance(value, str):
        if value.startswith("/") or value.startswith("~") or (home and str(home) in value):
            raise RunExportProjectionError(f"{label} must not contain an absolute path")
        return
    if isinstance(value, list):
        for item in value:
            _assert_strings(item, label=label, home=home)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunExportProjectionError(f"{label} has a non-string JSON key")
            if key in _PROMPT_KEYS and isinstance(item, str):
                raise RunExportProjectionError(f"{label} carries raw prompt text in {key!r}")
            _assert_strings(item, label=label, home=home)


def _assert_node(value: Any, node: Node, *, label: str, home: Path) -> None:
    if isinstance(node, _Any):
        _assert_strings(value, label=label, home=home)
        return
    if isinstance(node, _List):
        if isinstance(value, list):
            for item in value:
                _assert_node(item, node.item, label=label, home=home)
        else:
            _assert_strings(value, label=label, home=home)
        return
    if isinstance(node, _Object):
        if not isinstance(value, Mapping):
            _assert_strings(value, label=label, home=home)
            return
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunExportProjectionError(f"{label} has a non-string JSON key")
            child = node.fields.get(key)
            if child is None and _ANY_NAME in node.fields:
                child = node.fields[_ANY_NAME]
            if child is None:
                raise RunExportProjectionError(f"{label} has a key outside its allowlist: {key!r}")
            if key in _PROMPT_KEYS and isinstance(item, str):
                raise RunExportProjectionError(f"{label} carries raw prompt text in {key!r}")
            _assert_node(item, child, label=label, home=home)
        return
    _assert_strings(value, label=label, home=home)


def assert_export_file_set(payload_root: Path) -> None:
    """Refuse a payload file outside the closed export set."""
    payload_root = Path(payload_root)
    for path in sorted(payload_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(payload_root).as_posix()
        if not is_allowed_export_rel(rel):
            raise RunExportProjectionError(f"payload file is outside the export set: {rel}")


def assert_payload_privacy(payload_root: Path, *, home: Path | None = None) -> None:
    """Fail closed on unknown receipt keys, raw prompt text, or absolute paths."""
    payload_root = Path(payload_root)
    home = home if home is not None else Path.home()
    assert_export_file_set(payload_root)
    for path in sorted(payload_root.rglob("*")):
        if path.is_dir() or not path.is_file() or path.suffix != ".json":
            continue
        rel = path.relative_to(payload_root).as_posix()
        if rel not in _TOP_LEVEL_JSON and not _BATCH_RESULT_RE.fullmatch(rel):
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunExportProjectionError(f"{rel} is not readable JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise RunExportProjectionError(f"{rel} must contain a JSON object")
        _check_schema(rel, document)
        _assert_node(document, _document_node(rel), label=rel, home=home)
