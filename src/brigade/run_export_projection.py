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
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from . import localio, memory_recall_log, receipt_schema, run_checkpoint, worker_events

DIGEST_PREFIX = "hmac-sha256:"
EXTERNAL_PATH_TOKEN = "<external-path>"
RUN_DIR_TOKEN = "<run-dir>"
HOME_TOKEN = "<home>"
BATCH_RESULT_ENTRY_SCHEMA = "brigade.run_batch_result_entry.v1"

_DIGEST_RE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_BATCH_RESULT_RE = re.compile(r"^batch-result-[0-9a-f]{64}\.json$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'`;,)]*")
_UNC_RE = re.compile(r"\\\\[^\s\"'`;,)]*")
_ABS_SEGMENT_RE = re.compile(r"/(?:home|Users|root|tmp)(?:/[^\s\"'`;,)]*)?")
_FILE_URI_RE = re.compile(r"file://[^\s\"']*")
_SEAT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_PROMPT_KEYS = frozenset("task text detail final error prompt stdout stderr output result content summary".split())
_PATH_KEYS = frozenset(
    "cwd lock_workspace artifacts handoff handoff_inbox control_socket worktree "
    "stdout_log stderr_log patch_ref path".split()
)
_TOP_LEVEL_JSON = frozenset(
    "run.json roster.json plan.json worker-results.json synthesis.json batch-request.json".split()
)
_BATCH_RESULT_FIELDS = (
    "schema schema_version worker task ok detail text failure_phase failure_kind failure "
    "output_truncated output_bytes output_cap_bytes thread_id status exit_code timed_out "
    "stdout_log stderr_log duration_seconds transport requested_model effective_model reasoning "
    "stop_reason protocol_version session_id request_id acpx_version attempts"
)


class RunExportProjectionError(ValueError):
    """Export projection refused an unknown kind value or unsafe content."""


@dataclass(frozen=True)
class _Object:
    fields: Mapping[str, "Node"]
    any_name: "Node | None" = None
    key_pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class _List:
    item: "Node"


@dataclass(frozen=True)
class _Scalar:
    pass


@dataclass(frozen=True)
class _Prompt:
    pass


Node = _Object | _List | _Scalar | _Prompt

SCALAR = _Scalar()
PROMPT = _Prompt()
_OMIT = object()


def _shape(keys: str, **overrides: Node) -> _Object:
    fields: dict[str, Node] = {key: SCALAR for key in keys.split()}
    fields.update(overrides)
    return _Object(fields)


_STRING_LIST = _List(SCALAR)


_FAILURE = _shape(
    "schema class domain phase retry detected_by cause_code attempt seat message",
    detail=PROMPT,
    message=PROMPT,
)
_ARTIFACT_COLLECTION = _shape(
    "status changed tracked_count untracked_count", patch_ref=SCALAR, worktree=SCALAR, failure=_FAILURE
)
_GIT = _shape("head branch dirty_files")
_SCHEDULER = _shape("requested used fallback_reason")
_ATTEMPT = _shape(
    "kind worker transport reasoning started_at finished_at exit_code terminal_reason "
    "failure_phase failure_kind session_id selected model",
    task=PROMPT,
    stdout_log=SCALAR,
    stderr_log=SCALAR,
    failure=_FAILURE,
)
_WORKER_ENTRY = _shape(
    "worker ok failure_phase failure_kind output_truncated output_bytes output_cap_bytes "
    "thread_id status exit_code timed_out duration_seconds transport requested_model "
    "effective_model reasoning stop_reason protocol_version session_id request_id acpx_version",
    task=PROMPT,
    detail=PROMPT,
    text=PROMPT,
    failure=_FAILURE,
    stdout_log=SCALAR,
    stderr_log=SCALAR,
    attempts=_List(_ATTEMPT),
)
_AGENT_RESULT = _shape(
    "ok failure_phase failure_kind exit_code timed_out duration_seconds transport "
    "requested_model effective_model reasoning stop_reason protocol_version session_id "
    "request_id acpx_version",
    detail=PROMPT,
    text=PROMPT,
    failure=_FAILURE,
    stdout_log=SCALAR,
    stderr_log=SCALAR,
)
_ROSTER_AGENT = _shape(
    "cli model reasoning transport transport_version role timeout_seconds "
    "invalid_final_fallback read_only_capable cloud_safe_mode",
    command=_STRING_LIST,
    capabilities=_STRING_LIST,
)
_PLAN_ASSIGNMENT = _shape(
    "stage worker domain max_risk_class",
    task=PROMPT,
    covers=_STRING_LIST,
    selected_skill_ids=_STRING_LIST,
    capabilities=_STRING_LIST,
    admissible_tool_ids=_STRING_LIST,
)
_BATCH_TASK = _shape(
    "task_id seat isolation read_only",
    text=PROMPT,
    depends_on=_STRING_LIST,
)
_BATCH_RESULT = _shape(_BATCH_RESULT_FIELDS, task=PROMPT, detail=PROMPT, text=PROMPT, failure=_FAILURE)

_FILE_NODES: dict[str, Node] = {
    "run.json": _shape(
        "schema schema_version kind orchestrator worker dry_run read_only codex_transport "
        "lifecycle_journal_requested run_journal_authority_requested requester_principal "
        "requester_keyid tree_fingerprint tree_fingerprint_head category started_at "
        "status_started_at finished_at duration_seconds resumed_at suspected_noop status "
        "projector_version journal_present journal_last_sequence journal_last_event_digest "
        "failure_phase failure_kind orphaned_at last_observed_status uncommitted_change_count",
        task=PROMPT,
        error=PROMPT,
        cwd=SCALAR,
        lock_workspace=SCALAR,
        artifacts=SCALAR,
        handoff=SCALAR,
        handoff_inbox=SCALAR,
        control_socket=SCALAR,
        recovery_preserved_artifact=SCALAR,
        scheduler=_SCHEDULER,
        artifact_collection=_ARTIFACT_COLLECTION,
        failure=_FAILURE,
        git=_GIT,
    ),
    "roster.json": _shape(
        "schema schema_version orchestrator max_workers timeout_seconds sandbox",
        allow_models=_STRING_LIST,
        agents=_Object({}, any_name=_ROSTER_AGENT, key_pattern=_SEAT_NAME_RE),
    ),
    "plan.json": _shape("schema schema_version", assignments=_List(_PLAN_ASSIGNMENT)),
    "worker-results.json": _shape("schema schema_version producer_run_id", results=_List(_WORKER_ENTRY)),
    "synthesis.json": _shape("schema schema_version mode worker orchestrator", result=_AGENT_RESULT),
    "batch-request.json": _shape(
        "schema request_id base_sha fingerprint",
        layers=_List(_List(SCALAR)),
        tasks=_List(_BATCH_TASK),
        synthesis=_shape("seat", text=PROMPT),
    ),
    "batch-result.json": _BATCH_RESULT,
}

_SCHEMAS: dict[str, tuple[str, int | None, bool]] = {
    "run.json": (receipt_schema.RUN_RECEIPT_SCHEMA, receipt_schema.RUN_RECEIPT_SCHEMA_VERSION, False),
    "roster.json": (receipt_schema.ROSTER_SNAPSHOT_SCHEMA, receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION, False),
    "plan.json": (receipt_schema.RUN_PLAN_SCHEMA, receipt_schema.RUN_PLAN_SCHEMA_VERSION, False),
    "worker-results.json": (
        receipt_schema.WORKER_RESULTS_SCHEMA,
        receipt_schema.WORKER_RESULTS_SCHEMA_VERSION,
        False,
    ),
    "synthesis.json": (receipt_schema.SYNTHESIS_SCHEMA, receipt_schema.SYNTHESIS_SCHEMA_VERSION, False),
    "batch-request.json": ("brigade.run_batch.v1", None, False),
    "batch-result.json": (BATCH_RESULT_ENTRY_SCHEMA, 1, True),
}


def is_valid_digest(value: Any) -> bool:
    """True for the closed keyed-digest string form."""
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def is_allowed_export_rel(rel: str) -> bool:
    """True when ``rel`` is in the closed export file set."""
    if rel in _TOP_LEVEL_JSON:
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


def workspace_for_run(run_dir: Path) -> Path:
    """Return the owning workspace without ever returning the filesystem root."""
    resolved = Path(run_dir).expanduser().resolve()
    for ancestor in (resolved.parent, *resolved.parent.parents):
        if ancestor == Path(ancestor.anchor):
            break
        if (ancestor / ".brigade").is_dir():
            return ancestor
    return resolved if resolved.parent == Path(resolved.anchor) else resolved.parent


def digest_key_target(run_dir: Path) -> Path:
    """Return a writable per-host key target, never the filesystem root."""
    resolved = Path(run_dir).expanduser().resolve()
    for ancestor in (resolved.parent, *resolved.parent.parents):
        if ancestor == Path(ancestor.anchor):
            break
        if (ancestor / ".brigade").is_dir():
            return ancestor
    home = Path.home()
    if home != Path(home.anchor):
        return home
    raise RunExportProjectionError("no .brigade workspace or user state directory exists for the digest key")


def require_digest_key(target: Path) -> bytes:
    """Return the per-host HMAC key, refusing export when it cannot be loaded."""
    key = memory_recall_log.load_or_create_key(target)
    if key is None:
        raise RunExportProjectionError(f"could not load the per-host export digest key from {target}")
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


def _sanitize_embedded(value: str, ctx: _Context) -> str:
    home = str(ctx.home)
    if home and home in value:
        value = value.replace(home, HOME_TOKEN)
    value = _FILE_URI_RE.sub(EXTERNAL_PATH_TOKEN, value)
    value = _WIN_ABS_RE.sub(EXTERNAL_PATH_TOKEN, value)
    value = _UNC_RE.sub(EXTERNAL_PATH_TOKEN, value)
    return _ABS_SEGMENT_RE.sub(EXTERNAL_PATH_TOKEN, value)


def _file_uri_candidate(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        return Path(f"//{parsed.netloc}{path}")
    return Path(url2pathname(path))


def _relative_path(candidate: Path, root: Path) -> str | None:
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix()


def _map_path(value: str, ctx: _Context) -> str:
    path_like = False
    candidate: Path | None = None
    if value.startswith("file://"):
        candidate = _file_uri_candidate(value)
        path_like = True
    elif _DRIVE_RE.match(value) or value.startswith("\\\\"):
        candidate = Path(value)
        path_like = True
    elif value.startswith("~"):
        try:
            expanded = Path(value).expanduser()
        except RuntimeError:
            return EXTERNAL_PATH_TOKEN
        if str(expanded).startswith("~"):
            return EXTERNAL_PATH_TOKEN
        candidate = expanded
        path_like = True
    else:
        candidate = Path(value)
        path_like = candidate.is_absolute()

    if candidate is not None:
        rel = _relative_path(candidate, ctx.run_dir)
        if rel is not None:
            return RUN_DIR_TOKEN if rel in {"", "."} else f"{RUN_DIR_TOKEN}/{rel}"
        rel = _relative_path(candidate, ctx.workspace)
        if rel is not None:
            return "." if rel in {"", "."} else rel
    if path_like:
        return EXTERNAL_PATH_TOKEN
    return _sanitize_embedded(value, ctx)


def _sanitize_string(value: str, ctx: _Context) -> str:
    if value.startswith(("file://", "/", "~")) or _DRIVE_RE.match(value) or value.startswith("\\\\"):
        return _map_path(value, ctx)
    return _sanitize_embedded(value, ctx)


def _assert_prompt(value: Any, *, label: str, key: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"digest", "chars"}:
        raise RunExportProjectionError(f"{label}.{key} must be a closed prompt digest object")
    if not is_valid_digest(value.get("digest")):
        raise RunExportProjectionError(f"{label}.{key}.digest is not a keyed digest")
    chars = value.get("chars")
    if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
        raise RunExportProjectionError(f"{label}.{key}.chars must be a non-negative integer")


def _child_for(node: _Object, key: str, *, label: str) -> Node | None:
    child = node.fields.get(key)
    if child is not None:
        return child
    if node.any_name is None:
        return None
    if node.key_pattern is not None and node.key_pattern.fullmatch(key) is None:
        raise RunExportProjectionError(f"{label} has invalid key {key!r}")
    return node.any_name


def _project_prompt(value: Any, ctx: _Context, *, label: str, key: str) -> Any:
    if value is None:
        return _OMIT
    if isinstance(value, str):
        return digest_prompt(ctx.key, value)
    _assert_prompt(value, label=label, key=key)
    return dict(value)


def _project_string(key: str, value: str, ctx: _Context) -> Any:
    if key in _PROMPT_KEYS:
        return digest_prompt(ctx.key, value)
    if key in _PATH_KEYS or key.endswith("_path"):
        return _map_path(value, ctx)
    return _sanitize_string(value, ctx)


def _project_node(value: Any, node: Node, ctx: _Context, *, label: str, key: str = "") -> Any:
    if isinstance(node, _Prompt):
        return _project_prompt(value, ctx, label=label, key=key)
    if value is None:
        return None
    if isinstance(node, _Scalar):
        if isinstance(value, str):
            return _project_string(key, value, ctx)
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            return value
        raise RunExportProjectionError(f"{label}.{key} must be a scalar")
    if isinstance(node, _List):
        if not isinstance(value, list):
            raise RunExportProjectionError(f"{label}.{key} must be a list")
        return [
            _project_node(item, node.item, ctx, label=f"{label}[{index}]", key=key) for index, item in enumerate(value)
        ]
    if isinstance(node, _Object):
        if not isinstance(value, Mapping):
            raise RunExportProjectionError(f"{label}.{key} must be an object")
        result: dict[str, Any] = {}
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise RunExportProjectionError(f"{label} has a non-string JSON key")
            child = _child_for(node, item_key, label=f"{label}.{item_key}")
            if child is None:
                continue
            projected = _project_node(item, child, ctx, label=label, key=item_key)
            if projected is not _OMIT:
                result[item_key] = projected
        return result
    raise RunExportProjectionError(f"{label} has an unsupported schema node")


def _spec_rel(rel: str) -> str:
    return "batch-result.json" if _BATCH_RESULT_RE.fullmatch(rel) else rel


def _document_node(rel: str) -> Node:
    return _FILE_NODES.get(_spec_rel(rel), _FILE_NODES["batch-result.json"])


def _check_schema(rel: str, document: Mapping[str, Any], *, allow_legacy: bool) -> None:
    expected = _SCHEMAS.get(_spec_rel(rel))
    if expected is None:
        return
    schema, version, absent_ok = expected
    actual_schema = document.get("schema")
    if actual_schema is None:
        if not (allow_legacy or absent_ok):
            raise RunExportProjectionError(f"{rel} is missing schema")
    elif actual_schema != schema:
        raise RunExportProjectionError(f"{rel} has unsupported schema {actual_schema!r}")
    if version is None:
        return
    actual_version = document.get("schema_version")
    if actual_version is None:
        if not (allow_legacy or absent_ok):
            raise RunExportProjectionError(f"{rel} is missing schema_version")
    elif actual_version != version:
        raise RunExportProjectionError(f"{rel} has unsupported schema_version {actual_version!r}")


def project_document(rel: str, document: Any, ctx: _Context, *, allow_legacy: bool = False) -> Any:
    """Return a projected copy of one allowed JSON document."""
    if not isinstance(document, Mapping):
        raise RunExportProjectionError(f"{rel} must contain a JSON object")
    _check_schema(rel, document, allow_legacy=allow_legacy)
    if allow_legacy:
        expected = _SCHEMAS.get(_spec_rel(rel))
        if expected is not None:
            schema, version, _absent_ok = expected
            document = dict(document)
            document.setdefault("schema", schema)
            if version is not None:
                document.setdefault("schema_version", version)
    return _project_node(document, _document_node(rel), ctx, label=rel)


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
    allow_legacy: bool = False,
) -> int:
    """Project every allowed receipt JSON on the staged copy in place."""
    payload_root = Path(payload_root).resolve()
    ctx = _Context(
        workspace=Path(workspace).resolve(),
        run_dir=Path(run_dir).resolve(),
        home=Path.home(),
        key=key if key is not None else require_digest_key(digest_key_target(run_dir)),
    )
    projected = 0
    for rel in iter_receipt_files(payload_root):
        path = payload_root / rel
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunExportProjectionError(f"{rel} is not readable JSON: {exc}") from exc
        localio.write_json(path, project_document(rel, document, ctx, allow_legacy=allow_legacy))
        projected += 1
    return projected


def _assert_safe_string(value: str, *, label: str, home: Path) -> None:
    unsafe = (
        value.startswith(("/", "~", "file://", "\\\\"))
        or _DRIVE_RE.match(value) is not None
        or (bool(home) and str(home) in value)
        or _ABS_SEGMENT_RE.search(value) is not None
        or _FILE_URI_RE.search(value) is not None
        or _WIN_ABS_RE.search(value) is not None
        or _UNC_RE.search(value) is not None
    )
    if unsafe:
        raise RunExportProjectionError(f"{label} must not contain an absolute path")


def _assert_value(value: Any, node: Node, *, label: str, home: Path, key: str = "") -> None:
    if isinstance(node, _Prompt):
        _assert_prompt(value, label=label, key=key)
        return
    if value is None:
        return
    if isinstance(node, _Scalar):
        if isinstance(value, str):
            _assert_safe_string(value, label=f"{label}.{key}", home=home)
            return
        if isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
            return
        raise RunExportProjectionError(f"{label}.{key} must be a scalar")
    if isinstance(node, _List):
        if not isinstance(value, list):
            raise RunExportProjectionError(f"{label}.{key} must be a list")
        for index, item in enumerate(value):
            _assert_value(item, node.item, label=f"{label}[{index}]", home=home, key=key)
        return
    if isinstance(node, _Object):
        if not isinstance(value, Mapping):
            raise RunExportProjectionError(f"{label}.{key} must be an object")
        for item_key, item in value.items():
            if not isinstance(item_key, str):
                raise RunExportProjectionError(f"{label} has a non-string JSON key")
            child = _child_for(node, item_key, label=f"{label}.{item_key}")
            if child is None:
                raise RunExportProjectionError(f"{label} has a key outside its allowlist: {item_key!r}")
            _assert_value(item, child, label=label, home=home, key=item_key)
        return
    raise RunExportProjectionError(f"{label} has an unsupported schema node")


def _assert_generic(value: Any, *, label: str, home: Path) -> None:
    """Walk an already-closed document and refuse unsafe strings and raw prompts."""
    if isinstance(value, str):
        _assert_safe_string(value, label=label, home=home)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_generic(item, label=f"{label}[{index}]", home=home)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RunExportProjectionError(f"{label} has a non-string JSON key")
            if key in _PROMPT_KEYS:
                _assert_prompt(item, label=label, key=key)
                continue
            _assert_generic(item, label=f"{label}.{key}", home=home)
        return
    if value is None or isinstance(value, bool) or isinstance(value, int) or isinstance(value, float):
        return
    raise RunExportProjectionError(f"{label} has an unsupported JSON value")


def assert_manifest_privacy(manifest: Mapping[str, Any], *, home: Path | None = None) -> None:
    """Refuse absolute paths or prompt-shaped text anywhere in a v2 manifest."""
    home = home if home is not None else Path.home()
    _assert_generic(manifest, label="work-run.json", home=home)


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
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunExportProjectionError(f"{rel} is not readable JSON: {exc}") from exc
        if not isinstance(document, Mapping):
            raise RunExportProjectionError(f"{rel} must contain a JSON object")
        if rel in _TOP_LEVEL_JSON or _BATCH_RESULT_RE.fullmatch(rel):
            _check_schema(rel, document, allow_legacy=False)
            _assert_value(document, _document_node(rel), label=rel, home=home)
        else:
            _assert_generic(document, label=rel, home=home)
