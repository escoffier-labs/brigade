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

_PROMPT_KEYS = frozenset("task text detail final error prompt stdout stderr output result content summary".split())
_TOP_LEVEL_JSON = frozenset(
    "run.json roster.json plan.json worker-results.json synthesis.json batch-request.json".split()
)


class RunExportProjectionError(ValueError):
    """Export projection refused an unknown kind value or unsafe content."""


@dataclass(frozen=True)
class _Object:
    fields: Mapping[str, "Node"]
    any_name: "Node | None" = None
    digest_keys: bool = False


@dataclass(frozen=True)
class _List:
    item: "Node"


@dataclass(frozen=True)
class _CheckedString:
    values: frozenset[str] | None = None
    pattern: re.Pattern[str] | None = None
    fallback: str = "<other>"
    digest_ok: bool = True

    def matches(self, value: str) -> bool:
        if value == self.fallback:
            return True
        if self.values is not None and value not in self.values:
            return False
        if self.pattern is not None and self.pattern.fullmatch(value) is None:
            return False
        return True


@dataclass(frozen=True)
class _Int:
    pass


@dataclass(frozen=True)
class _Number:
    pass


@dataclass(frozen=True)
class _Bool:
    pass


@dataclass(frozen=True)
class _Path:
    pass


@dataclass(frozen=True)
class _DigestValue:
    pass


Node = _Object | _List | _CheckedString | _Int | _Number | _Bool | _Path | _DigestValue

INT = _Int()
NUMBER = _Number()
BOOL = _Bool()
PATH = _Path()
PROMPT = _DigestValue()
DIGEST = _DigestValue()
_OMIT = object()


def _enum(*values: str, fallback: str = "<other>") -> _CheckedString:
    return _CheckedString(values=frozenset(values), fallback=fallback, digest_ok=False)


def _pat(pattern: str, *, digest_ok: bool = True) -> _CheckedString:
    return _CheckedString(pattern=re.compile(pattern), digest_ok=digest_ok)


def _tags() -> _List:
    return _List(DIGEST)


_ISO = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
_HEX = r"^[0-9a-f]{7,64}$"
_SHA_OR_HEX = r"^(?:[0-9a-f]{40,64}|sha256:[0-9a-f]{64})$"
_RUN_ID = r"^\d{8}-\d{6}-[0-9a-f]{8}$"
_VERSION = r"^\d+(?:\.\d+){0,3}$"
_STREAM_EXPORT_RE = re.compile(r"^events/[0-9a-f]{64}\.scrubbed\.json$")
_CHECKPOINT_EXPORT_RE = re.compile(r"^events/recovery-checkpoints/[0-9a-f]{64}\.json$")
# Every exported payload path is a fixed name or hex-named; no path segment carries free text.
_MANIFEST_PATH = (
    r"^(?:run|roster|plan|worker-results|synthesis|batch-request)\.json"
    r"|batch-result-[0-9a-f]{64}\.json"
    r"|events/recovery-checkpoints/[0-9a-f]{64}\.json"
    r"|events/[0-9a-f]{64}\.scrubbed\.json$"
)
_FIXED_PATH_TOKENS = frozenset({".", RUN_DIR_TOKEN, EXTERNAL_PATH_TOKEN, HOME_TOKEN})
_STRICT = "\x00"
_MEDIA_TYPES = (
    "application/json",
    "application/x-ndjson",
    "text/plain",
    "text/x-diff",
    "application/octet-stream",
    "application/vnd.brigade.worker-events.appserver.scrubbed+jsonl",
)
_RUN_STATUSES = (
    "started",
    "planning",
    "dispatching",
    "result-processing",
    "synthesizing",
    "handoff",
    "artifact-collection",
    "running",
    "ok",
    "dry-run",
    "completed",
    "failed",
    "interrupted",
    "stopped",
    "orphaned",
    "cancelled",
    "unknown",
    "paused",
)
_FAILURE_PHASES = (
    "preflight",
    "dispatch",
    "postflight",
    "validation",
    "artifact-collection",
    "result-processing",
    "synthesis",
    "unknown",
)
_TRANSPORTS = ("direct", "acpx", "exec", "app-server", "codex-app-server", "cli")
_ATTEMPT_KINDS = ("initial", "same-seat-once", "continuation", "fallback", "retry", "worker-request")
_WORKER_STATUSES = (
    "complete",
    "completed",
    "failed",
    "interrupted",
    "skipped",
    "held",
    "unknown",
    "stopped",
    "cancelled",
    "ok",
    "running",
)
_SANDBOXES = ("none", "read-only", "workspace-write", "danger-full-access", "isolated", "shared")

_FAILURE = _Object(
    {
        "schema": _enum("brigade.worker_failure.v1"),
        "class": DIGEST,
        "domain": _enum("infrastructure", "model-output"),
        "phase": _enum(*_FAILURE_PHASES),
        "retry": _enum("never", "after-remediation", "fallback", "same-seat-once"),
        "detected_by": DIGEST,
        "cause_code": DIGEST,
        "attempt": INT,
        "seat": DIGEST,
        "kind": DIGEST,
        "detail": PROMPT,
        "message": PROMPT,
    }
)
_ARTIFACT_COLLECTION = _Object(
    {
        "status": _enum("ok", "failed", "started", "skipped"),
        "changed": BOOL,
        "tracked_count": INT,
        "untracked_count": INT,
        "patch_ref": PATH,
        "worktree": PATH,
        "failure": _FAILURE,
    }
)
_GIT = _Object({"head": _pat(_HEX), "branch": DIGEST, "dirty_files": INT})
_SCHEDULER = _Object(
    {
        "requested": _enum("waves", "dag", "single-node", "auto"),
        "used": _enum("waves", "dag", "single-node", "auto"),
        "fallback_reason": PROMPT,
    }
)
_ATTEMPT = _Object(
    {
        "kind": _enum(*_ATTEMPT_KINDS),
        "worker": DIGEST,
        "task": PROMPT,
        "transport": _enum(*_TRANSPORTS),
        "reasoning": DIGEST,
        "started_at": _pat(_ISO),
        "finished_at": _pat(_ISO),
        "exit_code": INT,
        "terminal_reason": DIGEST,
        "failure_phase": _enum(*_FAILURE_PHASES),
        "failure_kind": DIGEST,
        "session_id": DIGEST,
        "selected": BOOL,
        "model": DIGEST,
        "stdout_log": PATH,
        "stderr_log": PATH,
        "failure": _FAILURE,
    }
)
_WORKER_ENTRY = _Object(
    {
        "worker": DIGEST,
        "task": PROMPT,
        "ok": BOOL,
        "detail": PROMPT,
        "text": PROMPT,
        "failure_phase": _enum(*_FAILURE_PHASES),
        "failure_kind": DIGEST,
        "failure": _FAILURE,
        "output_truncated": BOOL,
        "output_bytes": INT,
        "output_cap_bytes": INT,
        "thread_id": DIGEST,
        "status": _enum(*_WORKER_STATUSES),
        "exit_code": INT,
        "timed_out": BOOL,
        "stdout_log": PATH,
        "stderr_log": PATH,
        "duration_seconds": NUMBER,
        "transport": _enum(*_TRANSPORTS),
        "requested_model": DIGEST,
        "effective_model": DIGEST,
        "reasoning": DIGEST,
        "stop_reason": DIGEST,
        "protocol_version": _pat(_VERSION),
        "session_id": DIGEST,
        "request_id": DIGEST,
        "acpx_version": _pat(_VERSION),
        "attempts": _List(_ATTEMPT),
    }
)
_AGENT_RESULT = _Object(
    {
        "ok": BOOL,
        "detail": PROMPT,
        "text": PROMPT,
        "failure_phase": _enum(*_FAILURE_PHASES),
        "failure_kind": DIGEST,
        "failure": _FAILURE,
        "exit_code": INT,
        "timed_out": BOOL,
        "stdout_log": PATH,
        "stderr_log": PATH,
        "duration_seconds": NUMBER,
        "transport": _enum(*_TRANSPORTS),
        "requested_model": DIGEST,
        "effective_model": DIGEST,
        "reasoning": DIGEST,
        "stop_reason": DIGEST,
        "protocol_version": _pat(_VERSION),
        "session_id": DIGEST,
        "request_id": DIGEST,
        "acpx_version": _pat(_VERSION),
    }
)
_ROSTER_AGENT = _Object(
    {
        "cli": DIGEST,
        "model": DIGEST,
        "reasoning": DIGEST,
        "transport": _enum(*_TRANSPORTS),
        "transport_version": _pat(_VERSION),
        "role": PROMPT,
        "timeout_seconds": NUMBER,
        "invalid_final_fallback": DIGEST,
        "read_only_capable": BOOL,
        "cloud_safe_mode": BOOL,
        "capabilities": _tags(),
    }
)
_PLAN_ASSIGNMENT = _Object(
    {
        "stage": INT,
        "worker": DIGEST,
        "task": PROMPT,
        "covers": _tags(),
        "selected_skill_ids": _tags(),
        "domain": DIGEST,
        "capabilities": _tags(),
        "max_risk_class": DIGEST,
        "admissible_tool_ids": _tags(),
    }
)
_BATCH_TASK = _Object(
    {
        "task_id": DIGEST,
        "text": PROMPT,
        "seat": DIGEST,
        "depends_on": _tags(),
        "isolation": _enum("shared", "worktree"),
        "read_only": BOOL,
    }
)
_BATCH_RESULT = _Object(
    {
        "schema": _enum(BATCH_RESULT_ENTRY_SCHEMA),
        "schema_version": INT,
        "worker": DIGEST,
        "task": PROMPT,
        "ok": BOOL,
        "detail": PROMPT,
        "text": PROMPT,
        "failure_phase": _enum(*_FAILURE_PHASES),
        "failure_kind": DIGEST,
        "failure": _FAILURE,
        "output_truncated": BOOL,
        "output_bytes": INT,
        "output_cap_bytes": INT,
        "thread_id": DIGEST,
        "status": _enum(*_WORKER_STATUSES),
        "exit_code": INT,
        "timed_out": BOOL,
        "stdout_log": PATH,
        "stderr_log": PATH,
        "duration_seconds": NUMBER,
        "transport": _enum(*_TRANSPORTS),
        "requested_model": DIGEST,
        "effective_model": DIGEST,
        "reasoning": DIGEST,
        "stop_reason": DIGEST,
        "protocol_version": _pat(_VERSION),
        "session_id": DIGEST,
        "request_id": DIGEST,
        "acpx_version": _pat(_VERSION),
        "attempts": _List(_ATTEMPT),
    }
)

_FILE_NODES: dict[str, Node] = {
    "run.json": _Object(
        {
            "schema": _enum(receipt_schema.RUN_RECEIPT_SCHEMA),
            "schema_version": INT,
            "kind": _enum("work", "research", "batch", "verify"),
            "orchestrator": DIGEST,
            "worker": DIGEST,
            "dry_run": BOOL,
            "read_only": BOOL,
            "codex_transport": _enum("exec", "app-server"),
            "lifecycle_journal_requested": BOOL,
            "run_journal_authority_requested": BOOL,
            "requester_principal": DIGEST,
            "requester_keyid": DIGEST,
            "tree_fingerprint": _pat(_HEX),
            "tree_fingerprint_head": _pat(_HEX),
            "category": DIGEST,
            "started_at": _pat(_ISO),
            "status_started_at": _pat(_ISO),
            "finished_at": _pat(_ISO),
            "duration_seconds": NUMBER,
            "resumed_at": _pat(_ISO),
            "suspected_noop": BOOL,
            "status": _enum(*_RUN_STATUSES),
            "projector_version": INT,
            "journal_present": BOOL,
            "journal_last_sequence": INT,
            "journal_last_event_digest": _pat(_SHA_OR_HEX),
            "failure_phase": _enum(*_FAILURE_PHASES),
            "failure_kind": DIGEST,
            "orphaned_at": _pat(_ISO),
            "last_observed_status": _enum(*_RUN_STATUSES),
            "uncommitted_change_count": INT,
            "task": PROMPT,
            "error": PROMPT,
            "cwd": PATH,
            "lock_workspace": PATH,
            "artifacts": PATH,
            "handoff": PATH,
            "handoff_inbox": PATH,
            "control_socket": PATH,
            "recovery_preserved_artifact": PATH,
            "scheduler": _SCHEDULER,
            "artifact_collection": _ARTIFACT_COLLECTION,
            "failure": _FAILURE,
            "git": _GIT,
            "journal_withheld": _Object({"chain_head": _pat(_SHA_OR_HEX), "event_count": INT}),
        }
    ),
    "roster.json": _Object(
        {
            "schema": _enum(receipt_schema.ROSTER_SNAPSHOT_SCHEMA),
            "schema_version": INT,
            "orchestrator": DIGEST,
            "max_workers": INT,
            "timeout_seconds": NUMBER,
            "sandbox": _enum(*_SANDBOXES),
            "allow_models": _tags(),
            "agents": _Object({}, any_name=_ROSTER_AGENT, digest_keys=True),
        }
    ),
    "plan.json": _Object(
        {
            "schema": _enum(receipt_schema.RUN_PLAN_SCHEMA),
            "schema_version": INT,
            "assignments": _List(_PLAN_ASSIGNMENT),
        }
    ),
    "worker-results.json": _Object(
        {
            "schema": _enum(receipt_schema.WORKER_RESULTS_SCHEMA),
            "schema_version": INT,
            "producer_run_id": _pat(_RUN_ID),
            "results": _List(_WORKER_ENTRY),
        }
    ),
    "synthesis.json": _Object(
        {
            "schema": _enum(receipt_schema.SYNTHESIS_SCHEMA),
            "schema_version": INT,
            "mode": _enum("direct-worker", "orchestrator", "single-seat"),
            "worker": DIGEST,
            "orchestrator": DIGEST,
            "result": _AGENT_RESULT,
        }
    ),
    "batch-request.json": _Object(
        {
            "schema": _enum("brigade.run_batch.v1"),
            "request_id": DIGEST,
            "base_sha": _pat(_HEX),
            "fingerprint": _pat(_HEX),
            "layers": _List(_List(DIGEST)),
            "tasks": _List(_BATCH_TASK),
            "synthesis": _Object({"seat": DIGEST, "text": PROMPT}),
        }
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


_MANIFEST_RUN = _Object(
    {
        "schema": _enum("brigade.run.v1", fallback=_STRICT),
        "schema_version": INT,
        "status": _enum(*_RUN_STATUSES),
        "started_at": _pat(_ISO),
        "finished_at": _pat(_ISO),
        "task_digest": _pat(r"^hmac-sha256:[0-9a-f]{64}$", digest_ok=False),
    }
)
_MANIFEST_COMPAT = _Object(
    {
        "min_reader_schema_version": INT,
        "max_reader_schema_version": INT,
        "journal_authority": _enum("none", "present", "authoritative", "withheld", fallback=_STRICT),
        "resume_supported": BOOL,
        "private_checkpoint_bodies": _enum("strip_to_artifact_reference", fallback=_STRICT),
        "unsupported_archive_version": _enum("refuse", fallback=_STRICT),
        "nested_receipt_unknown_keys": _enum("ignore", fallback=_STRICT),
        "symlinks_and_special_files": _enum("refuse", fallback=_STRICT),
    }
)
_MANIFEST_FILE = _Object(
    {
        "path": _pat(_MANIFEST_PATH, digest_ok=False),
        "sha256": _pat(r"^[0-9a-f]{64}$", digest_ok=False),
        "byte_size": INT,
        "role": _enum("receipt", "journal", "checkpoint-reference", "artifact", "support", "other", fallback=_STRICT),
        "media_type": _enum(*_MEDIA_TYPES, fallback=_STRICT),
        "nested_schema": _enum(
            receipt_schema.RUN_RECEIPT_SCHEMA,
            receipt_schema.ROSTER_SNAPSHOT_SCHEMA,
            receipt_schema.RUN_PLAN_SCHEMA,
            receipt_schema.WORKER_RESULTS_SCHEMA,
            receipt_schema.SYNTHESIS_SCHEMA,
            receipt_schema.RUN_EVENT_SCHEMA,
            "brigade.run_batch.v1",
            BATCH_RESULT_ENTRY_SCHEMA,
            worker_events.SCHEMA,
            worker_events.STREAM_SCHEMA,
            fallback=_STRICT,
        ),
        "nested_schema_version": INT,
        "privacy_class": _enum("public", "private", "redacted", fallback=_STRICT),
    }
)
_MANIFEST_NODE = _Object(
    {
        "schema": _enum("brigade.work-run", fallback=_STRICT),
        "schema_version": INT,
        "run_id": _pat(_RUN_ID, digest_ok=False),
        "exported_at": _pat(_ISO, digest_ok=False),
        "exporter_brigade_version": _pat(_VERSION, digest_ok=False),
        "format": _enum("directory", fallback=_STRICT),
        "payload_dir": _enum("payload", fallback=_STRICT),
        "compatibility": _MANIFEST_COMPAT,
        "files": _List(_MANIFEST_FILE),
        "run": _MANIFEST_RUN,
        "journal_withheld": _Object({"chain_head": _pat(_SHA_OR_HEX, digest_ok=False), "event_count": INT}),
        "source_run_dir": _enum(RUN_DIR_TOKEN, fallback=_STRICT),
        "schema_artifact": _enum("schemas/work-run.v3.schema.json", fallback=_STRICT),
    }
)


def is_valid_digest(value: Any) -> bool:
    """True for the closed keyed-digest string form."""
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def is_allowed_export_rel(rel: str) -> bool:
    """True when ``rel`` is in the closed export file set, after stream renaming."""
    if rel in _TOP_LEVEL_JSON or _BATCH_RESULT_RE.fullmatch(rel):
        return True
    return bool(_CHECKPOINT_EXPORT_RE.fullmatch(rel) or _STREAM_EXPORT_RE.fullmatch(rel))


def _is_source_exportable_rel(rel: str) -> bool:
    """True for a staged run file that may enter the export before stream renaming."""
    if rel in _TOP_LEVEL_JSON:
        return True
    if _BATCH_RESULT_RE.fullmatch(rel):
        return True
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or "\\" in rel:
        return False
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
        if _is_source_exportable_rel(rel):
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
    return node.any_name


def _digest_key(key: bytes, value: str) -> str:
    return DIGEST_PREFIX + hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _project_prompt(value: Any, ctx: _Context, *, label: str, key: str) -> Any:
    if value is None:
        return _OMIT
    if isinstance(value, str):
        return digest_prompt(ctx.key, value)
    _assert_prompt(value, label=label, key=key)
    return dict(value)


def _project_node(value: Any, node: Node, ctx: _Context, *, label: str, key: str = "") -> Any:
    if isinstance(node, _DigestValue):
        return _project_prompt(value, ctx, label=label, key=key)
    if value is None:
        return None
    if isinstance(node, _Int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RunExportProjectionError(f"{label}.{key} must be an integer")
        return value
    if isinstance(node, _Number):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RunExportProjectionError(f"{label}.{key} must be a number")
        return value
    if isinstance(node, _Bool):
        if not isinstance(value, bool):
            raise RunExportProjectionError(f"{label}.{key} must be a boolean")
        return value
    if isinstance(node, _CheckedString):
        if isinstance(value, Mapping) and node.digest_ok:
            _assert_prompt(value, label=label, key=key)
            return dict(value)
        if not isinstance(value, str):
            raise RunExportProjectionError(f"{label}.{key} must be a string")
        if node.matches(value):
            return value
        if node.values is not None:
            return node.fallback
        if node.digest_ok:
            return digest_prompt(ctx.key, value)
        raise RunExportProjectionError(f"{label}.{key} is not an allowed value")
    if isinstance(node, _Path):
        if isinstance(value, Mapping):
            _assert_prompt(value, label=label, key=key)
            return dict(value)
        if not isinstance(value, str):
            raise RunExportProjectionError(f"{label}.{key} must be a path string")
        mapped = _map_path(value, ctx)
        return mapped if mapped in _FIXED_PATH_TOKENS else digest_prompt(ctx.key, value)
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
                output_key = _digest_key(ctx.key, item_key) if node.digest_keys else item_key
                if output_key in result:
                    raise RunExportProjectionError(f"{label} has a duplicate projected key")
                result[output_key] = projected
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


def rename_worker_streams(payload_root: Path, key: bytes) -> int:
    """Give each scrubbed worker stream a keyed-digest name; seat names never become paths."""
    payload_root = Path(payload_root)
    renamed = 0
    for path in sorted(payload_root.rglob("*.scrubbed.json")):
        rel = path.relative_to(payload_root).as_posix()
        # Rename every stream, including one already hex-named: a legacy archive must not
        # choose a name that survives import.
        if not worker_events.is_scrubbed_worker_event_stream_rel(rel):
            continue
        digest = hmac.new(key, rel.encode("utf-8"), hashlib.sha256).hexdigest()
        target = payload_root / "events" / f"{digest}.scrubbed.json"
        if target.exists():
            raise RunExportProjectionError("worker stream name collision during export projection")
        path.rename(target)
        renamed += 1
    for directory in sorted((d for d in (payload_root / "events").rglob("*") if d.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return renamed


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
    if (payload_root / "events").is_dir():
        rename_worker_streams(payload_root, ctx.key)
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
    if isinstance(node, _DigestValue):
        _assert_prompt(value, label=label, key=key)
        return
    if value is None:
        return
    if isinstance(node, _Int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RunExportProjectionError(f"{label}.{key} must be an integer")
        return
    if isinstance(node, _Number):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RunExportProjectionError(f"{label}.{key} must be a number")
        return
    if isinstance(node, _Bool):
        if not isinstance(value, bool):
            raise RunExportProjectionError(f"{label}.{key} must be a boolean")
        return
    if isinstance(node, _CheckedString):
        if isinstance(value, Mapping) and node.digest_ok:
            _assert_prompt(value, label=label, key=key)
            return
        if not isinstance(value, str) or not node.matches(value):
            raise RunExportProjectionError(f"{label}.{key} is not an allowed value")
        return
    if isinstance(node, _Path):
        if isinstance(value, Mapping):
            _assert_prompt(value, label=label, key=key)
            return
        if isinstance(value, str) and value in _FIXED_PATH_TOKENS:
            return
        raise RunExportProjectionError(f"{label}.{key} must be a fixed path token or digest")
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
            if node.digest_keys and not _DIGEST_RE.fullmatch(item_key):
                raise RunExportProjectionError(f"{label} key is not a digest")
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
    """Validate every v3 manifest string against its closed type."""
    home = home if home is not None else Path.home()
    _assert_value(manifest, _MANIFEST_NODE, label="work-run.json", home=home)


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
