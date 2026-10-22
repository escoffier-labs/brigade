"""Classified worker-level recordings. Cassettes never supply lifecycle authority.

Only locally observed results are recorded. Prompts and raw notification streams
are never stored here. Deleting these sidecars cannot change offline audit state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping
from uuid import uuid4

from . import agents, config, dirfd, run_audit, run_events, run_journal, runguard, worker_events
from . import run_cassette_store as store
from .roster import Agent
from .run_checkpoint import MAX_JOURNAL_BYTES, MAX_JOURNAL_EVENTS

if TYPE_CHECKING:
    from .roster import Roster
    from .run_transport import Assignment, WorkerResult

SCHEMA_VERSION = 1
SCHEMA = "brigade.run-cassette.v1"
EVENT_TYPE = "run.cassette.recorded"
MEDIA_TYPE = "application/vnd.brigade.run-cassette+json"
REFERENCE_KEYS = frozenset({"path", "sha256", "media_type", "byte_size", "privacy_class"})
REDACTED_REFERENCE = {key: 0 if key == "byte_size" else "[REDACTED]" for key in REFERENCE_KEYS}
OBSERVED_TRANSPORTS = frozenset({"cli", "direct", "exec", "acpx", "app-server", "codex-app-server"})
_STOP_BYTES = store.encode(
    {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "privacy_class": "public-metadata", "status": "stopped"}
)
_LIFECYCLE_EVENT_RESERVE = max(8, MAX_JOURNAL_EVENTS // 4)
_SECRET_ENV_NAME = re.compile(r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|PASS|CREDENTIALS?)(?:_|$)", re.I)
_WINDOW_LOCK = Lock()
_ACTIVE_WINDOWS: dict[tuple[str, str], list[_ProjectionWindow]] = {}


@dataclass(eq=False)
class _ProjectionWindow:
    offset: int = 0
    identity: tuple[int, int] | None = None
    unavailable: bool = False


def validate_reference(payload: Mapping[str, Any]) -> None:
    if dict(payload) == REDACTED_REFERENCE:
        return
    digest = payload.get("sha256")
    size = payload.get("byte_size")
    if not (
        set(payload) == REFERENCE_KEYS
        and isinstance(digest, str)
        and run_events._HEX64.fullmatch(digest)
        and payload.get("path") == f"events/{store.DIRECTORY}/{digest}.json"
        and payload.get("media_type") == MEDIA_TYPE
        and payload.get("privacy_class") == "public-metadata"
        and isinstance(size, int)
        and not isinstance(size, bool)
        and 0 < size <= store.MAX_ENTRY_BYTES
    ):
        raise run_events.CanonicalizationError("invalid cassette artifact reference")


def _reference(data: bytes, privacy_class: str) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    return {
        "path": f"events/{store.DIRECTORY}/{digest}.json",
        "sha256": digest,
        "byte_size": len(data),
        "media_type": MEDIA_TYPE,
        "privacy_class": privacy_class,
    }


def _read_object(path: Path) -> dict[str, Any]:
    parent = dirfd.open_directory_chain(Path(os.path.abspath(path.parent)))
    try:
        raw = store.read(parent, path.name, private=False)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise store.CassetteError("cassette metadata must be an object")
        return value
    finally:
        os.close(parent)


def _finished(run_dir: Path) -> bool:
    try:
        return bool(_read_object(run_dir / "run.json").get("finished_at"))
    except FileNotFoundError:
        return False


def _secret_values(agent: Agent) -> tuple[str, ...]:
    if not agent.env:
        return ()
    resolved, error = agents.resolve_env_overrides(agent.env)
    if error or resolved is None:
        raise store.CassetteError("cassette env resolution unavailable")
    targets = agents.secret_ref_targets(agent.env) | {name for name in resolved if _SECRET_ENV_NAME.search(name)}
    return tuple(sorted({resolved[key] for key in targets if resolved.get(key)}, key=len, reverse=True))


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        if any(secret in key for key in value for secret in secrets):
            raise store.CassetteError("cassette secret collides with a structural key")
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v, secrets) for v in value)
    return value


def _stream_name(agent: Agent) -> str:
    from .aboyeur.run_io import _slug

    return f"{_slug(agent.name)}.jsonl"


@contextmanager
def _projection_window(run_dir: Path | None, agent: Agent) -> Iterator[_ProjectionWindow]:
    window = _ProjectionWindow()
    if run_dir is None:
        yield window
        return
    key = (os.path.abspath(run_dir), _stream_name(agent))
    with _WINDOW_LOCK:
        active = _ACTIVE_WINDOWS.setdefault(key, [])
        if active:
            for other in active:
                other.unavailable = True
            window.unavailable = True
        active.append(window)
    try:
        try:
            parent = dirfd.open_directory_chain(Path(os.path.abspath(run_dir / "events")))
            try:
                info = dirfd.stat_child(parent, _stream_name(agent))
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise store.CassetteError("cassette stream is unsafe")
                window.offset = info.st_size
                window.identity = (info.st_dev, info.st_ino)
            finally:
                os.close(parent)
        except FileNotFoundError:
            pass
        except (OSError, RuntimeError):
            window.unavailable = True
        yield window
    finally:
        with _WINDOW_LOCK:
            active.remove(window)
            if not active:
                del _ACTIVE_WINDOWS[key]


def _projection(run_dir: Path, agent: Agent, window: _ProjectionWindow) -> dict[str, Any]:
    if window.unavailable:
        raise store.CassetteError("cassette stream dispatch is ambiguous")
    parent = dirfd.open_directory_chain(Path(os.path.abspath(run_dir / "events")))
    try:
        raw = store.read(
            parent,
            _stream_name(agent),
            private=False,
            limit=store.MAX_ENTRY_BYTES * 2,
            offset=window.offset,
            identity=window.identity,
        )
    finally:
        os.close(parent)
    return worker_events.scrub_stream_text(raw.decode("utf-8"))


def _journal_present(journal: Path) -> bool:
    if run_journal.bound_lstat(journal, strict=True) is None:
        return False
    _journal_size(journal)
    return True


def _journal_size(journal: Path) -> int:
    info = run_journal.bound_lstat(journal, strict=True)
    if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise store.CassetteError("cassette journal is unsafe")
    return info.st_size


def _append_reference(run_dir: Path, workspace: Path, ref: dict[str, Any]) -> None:
    from . import run_shadow

    if not runguard.is_active_run_owner(workspace, run_dir):
        raise store.CassetteError("cassette journal append requires the active run lock")
    journal = run_dir / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal_bounded(journal)
    if report.chain_errors or report.partial_tail:
        raise store.CassetteError("cassette journal is not readable")
    sequence = len(report.events)
    # Optional references, including stop facts, cannot consume the lifecycle reserve.
    if sequence >= MAX_JOURNAL_EVENTS - _LIFECYCLE_EVENT_RESERVE or _journal_size(journal) + 2048 > MAX_JOURNAL_BYTES:
        raise store.CassetteError("cassette journal cap reached")
    try:
        snapshot = _read_object(run_dir / "run.json")
    except FileNotFoundError:
        snapshot = None
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type=EVENT_TYPE,
        payload=ref,
        idempotency_key=f"cassette:{uuid4().hex}",
        expected_previous_sequence=sequence,
        workspace=workspace,
    )
    # Like live-control facts, neutral cassette facts advance shadow evidence
    # under the same pair/journal lock without changing run.json authority.
    if snapshot is not None and snapshot.get("lifecycle_journal_requested") is True:
        run_shadow.record_shadow_comparison(run_dir, snapshot)


def _stop(run_dir: Path, workspace: Path) -> None:
    """A single bounded stop fact when storage or policy cannot admit a body."""
    print("cassette recording stopped: storage, classification, or byte cap", file=sys.stderr)
    try:
        if _finished(run_dir):
            return
        journal = run_dir / "events" / "lifecycle.jsonl"
        with store.journal_scope(run_dir):
            _stop_bound(run_dir, workspace, journal)
    except Exception:
        # A full or unavailable journal cannot carry even a stop reference.
        pass


def _stop_bound(run_dir: Path, workspace: Path, journal: Path) -> None:
    from . import run_lifecycle

    journaled = _journal_present(journal)
    with (
        run_lifecycle.checkpoint_event_pair(),
        run_journal.journal_mutation(journal) if journaled else nullcontext(),
        store.directory(run_dir, create=True, create_root=False) as parent,
    ):
        if _finished(run_dir):
            return
        if journaled and not runguard.is_active_run_owner(workspace, run_dir):
            return
        files = store.inventory(parent)
        names = {name for name, _ in files}
        if ".stopped" in names:
            return
        data = _STOP_BYTES
        fits = sum(info.st_size for _, info in files) + len(data) <= store.MAX_RUN_BYTES
        if fits:
            store.publish(parent, data)
        descriptor = dirfd.open_child_file(parent, ".stopped", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        dirfd.fsync_directory(parent)
        if fits and journaled:
            _append_reference(run_dir, workspace, _reference(data, "public-metadata"))


def record_result(
    *,
    run_dir: Path,
    workspace: Path,
    agent: Agent,
    task: str,
    prompt: str,
    result: agents.AgentResult,
    read_only: bool,
    sandbox: str | None,
    remote: bool = False,
    codex_transport: str = "exec",
    projection_window: _ProjectionWindow | None = None,
    coordinator_prompt_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort recording. Failure stops recording, never the worker run."""
    try:
        cfg = config.load_config(workspace)
    except Exception:
        print("cassette recording skipped: configuration unavailable", file=sys.stderr)
        return None
    mode = cfg.run_record if cfg is not None else "metadata-only"
    if mode == "off":
        return None
    try:
        ref = _record_result(
            run_dir=run_dir,
            workspace=workspace,
            agent=agent,
            task=task,
            prompt=prompt,
            result=result,
            read_only=read_only,
            sandbox=sandbox,
            remote=remote,
            mode=mode,
            codex_transport=codex_transport,
            projection_window=projection_window or _ProjectionWindow(),
            coordinator_prompt_sha256=coordinator_prompt_sha256,
        )
    except Exception:
        _stop(run_dir, workspace)
        return None
    if ref is not None:
        store.retain_private_once(workspace, run_dir)
    return ref


def _record_result(
    *,
    run_dir: Path,
    workspace: Path,
    agent: Agent,
    task: str,
    prompt: str,
    result: agents.AgentResult,
    read_only: bool,
    sandbox: str | None,
    remote: bool,
    mode: str,
    codex_transport: str,
    projection_window: _ProjectionWindow,
    coordinator_prompt_sha256: str | None,
) -> dict[str, Any] | None:
    from . import run_receipts

    # Inventory and stop admission belong under _publish_blobs' publication fence.
    # An unfenced scan can observe the publisher's temporary two-link interval.
    try:
        run_meta = _read_object(run_dir / "run.json")
    except FileNotFoundError:
        run_meta = {}
    if run_meta.get("finished_at"):
        return None
    fingerprint = run_audit.composed_prompt_fingerprint(
        worker=agent.name,
        role=agent.role,
        task=task,
        read_only=read_only,
        skill_policy=run_meta.get("skill_route_policy"),
    )
    transport = result.transport or agent.transport
    identity = {
        "schema_version": SCHEMA_VERSION,
        "seat": {
            "name": agent.name,
            "cli": agent.cli,
            "model": agent.model,
            "reasoning": agent.reasoning,
            "sandbox": sandbox,
            "transport": agent.transport,
            **({"codex_transport": codex_transport} if agent.cli == "codex" else {}),
        },
        "coordinator_prompt_fingerprint": fingerprint,
        **({"coordinator_prompt_sha256": coordinator_prompt_sha256} if coordinator_prompt_sha256 is not None else {}),
    }
    key = hashlib.sha256(run_events.canonical_bytes(identity)).hexdigest()
    observed = not remote and transport in OBSERVED_TRANSPORTS and result.failure_phase != "preflight"
    env_unavailable = False
    try:
        secrets = _secret_values(agent) if observed else ()
    except store.CassetteError:
        observed, env_unavailable, secrets = False, True, ()
    body = None
    if observed:
        # Receipt shaping truncates text and labels, so scrub complete values first.
        scrubbed_result = agents.AgentResult(**_scrub(asdict(result), secrets))
        body = _scrub(run_receipts.agent_result_payload(scrubbed_result), secrets)
    projection = None
    projection_status = "unavailable"
    appserver = observed and transport in {"app-server", "codex-app-server"}
    if appserver:
        try:
            with _WINDOW_LOCK:
                projection = _scrub(_projection(run_dir, agent, projection_window), secrets)
            if len(store.encode(projection)) > store.MAX_ENTRY_BYTES // 2:
                projection = None
            else:
                projection_status = "available"
        except (OSError, ValueError, RuntimeError, worker_events.WorkerEventError):
            pass
    blobs: list[tuple[str, bytes]] = []
    metadata: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "privacy_class": "scrubbed",
        "key": key,
        "status": "observed" if observed else "not-observed",
        "reason": "local-result" if observed else "remote" if remote else "unsupported-or-unobserved",
        "observed_transport": _scrub(transport, secrets) if transport in OBSERVED_TRANSPORTS else "unsupported",
    }
    if env_unavailable:
        metadata["reason"] = "env-resolution-unavailable"
    if body is not None:
        metadata["result_sha256"] = hashlib.sha256(store.encode(body)).hexdigest()
        metadata["result_bytes"] = len(store.encode(body))
        metadata["ok"] = result.ok
        metadata["exit_code"] = result.exit_code
        # Arbitrary labels are digested. No model-controlled text crosses a boundary.
        metadata["labels_sha256"] = hashlib.sha256(
            store.encode(
                {k: body.get(k) for k in ("requested_model", "effective_model", "failure_kind", "stop_reason")}
            )
        ).hexdigest()
        if mode == "auto":
            blobs.append(
                (
                    "private",
                    store.encode(
                        {
                            "schema": SCHEMA,
                            "schema_version": SCHEMA_VERSION,
                            "privacy_class": "private",
                            "key": key,
                            "result": body,
                        }
                    ),
                )
            )
    if projection is not None:
        metadata["notification_projection"] = projection
        metadata["notification_projection_sha256"] = hashlib.sha256(store.encode(projection)).hexdigest()
        if sum(len(raw) for _, raw in blobs) + len(store.encode(metadata)) > store.MAX_ENTRY_BYTES - 4096:
            del metadata["notification_projection"]
            del metadata["notification_projection_sha256"]
            projection_status = "unavailable"
    if appserver:
        metadata["projection_status"] = projection_status
    blobs.append(("scrubbed", store.encode(metadata)))
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "privacy_class": "public-metadata",
        "key": key,
        "coordinator_prompt_fingerprint": fingerprint,
        "full_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        **({"coordinator_prompt_sha256": coordinator_prompt_sha256} if coordinator_prompt_sha256 is not None else {}),
        "status": metadata["status"],
        "artifacts": [_reference(data, cls) for cls, data in blobs],
    }
    data = store.encode(manifest)
    blobs.append(("public-metadata", data))
    if sum(len(raw) for _, raw in blobs) > store.MAX_ENTRY_BYTES:
        raise store.CassetteError("cassette entry byte cap reached")
    journal = run_dir / "events" / "lifecycle.jsonl"
    with store.journal_scope(run_dir):
        return _publish_blobs(run_dir, workspace, journal, blobs, data)


def _publish_blobs(
    run_dir: Path, workspace: Path, journal: Path, blobs: list[tuple[str, bytes]], data: bytes
) -> dict[str, Any] | None:
    from . import run_lifecycle

    journaled = _journal_present(journal)
    with (
        run_lifecycle.checkpoint_event_pair(),
        run_journal.journal_mutation(journal) if journaled else nullcontext(),
        store.directory(run_dir, create=True, create_root=False) as parent,
    ):
        if _finished(run_dir):
            return None
        files = store.inventory(parent)
        if any(name == ".stopped" for name, _ in files):
            return None
        if len(files) + len(blobs) + 2 > store.MAX_FILES:
            raise store.CassetteError("cassette file cap reached")
        existing = {name for name, _ in files}
        added = sum(len(raw) for _, raw in blobs if hashlib.sha256(raw).hexdigest() + ".json" not in existing)
        if sum(info.st_size for _, info in files) + added + len(_STOP_BYTES) > store.MAX_RUN_BYTES:
            raise store.CassetteError("cassette run byte cap reached")
        # Check journal admission before publishing any bodies.
        if journaled:
            if not runguard.is_active_run_owner(workspace, run_dir):
                raise store.CassetteError("cassette publication requires the active run lock")
            report = run_journal.read_journal_bounded(journal)
            if (
                report.chain_errors
                or report.partial_tail
                or len(report.events) >= MAX_JOURNAL_EVENTS - _LIFECYCLE_EVENT_RESERVE
                or _journal_size(journal) + 2048 > MAX_JOURNAL_BYTES
            ):
                raise store.CassetteError("cassette journal cap reached")
        for _, raw in blobs:
            store.publish(parent, raw)
        ref = _reference(data, "public-metadata")
        if journaled:
            _append_reference(run_dir, workspace, ref)
    return ref


def dispatch_failure(
    error: Exception,
    *,
    worker: str,
    task: str,
    transport: str = "cli",
    requested_model: str | None = None,
) -> WorkerResult:
    """Shape a closed recorder-only failure without persisting exception content."""
    from . import proc
    from .run_transport import WorkerResult

    if isinstance(error, proc.ProcessCancelledError):
        detail, failure_kind = "worker dispatch cancelled", "cancelled"
    elif isinstance(error, OSError):
        detail, failure_kind = "worker transport unavailable", "transport-error"
    else:
        detail, failure_kind = "worker dispatch failed", "internal-error"
    return WorkerResult(
        worker=worker,
        task=task,
        text="",
        ok=False,
        detail=detail,
        failure_phase="dispatch",
        failure_kind=failure_kind,
        transport=transport,
        requested_model=requested_model,
    )


@dataclass
class DispatchRecording:
    agent: Agent
    prompt: str | None = None


def dispatch_recorder(
    resolve: Callable[[Assignment, list[WorkerResult], DispatchRecording], WorkerResult],
    *,
    roster: Roster,
    run_dir: Path | None,
    workspace: Path | None,
    read_only: bool,
    sandbox: str | None,
    transport: Callable[[Agent], str],
    codex_transport: str,
    coordinator_prompt_sha256: str | None,
) -> Callable[[Assignment, list[WorkerResult]], WorkerResult]:
    """Observe the final worker result without changing retry or scheduler flow."""

    def run_one(assignment: Assignment, prior_results: list[WorkerResult]) -> WorkerResult:
        recording = DispatchRecording(roster.agents[assignment.worker])
        return record_dispatch_result(
            lambda: resolve(assignment, prior_results, recording),
            run_dir=run_dir,
            workspace=workspace,
            agent=lambda: recording.agent,
            prompt=lambda: recording.prompt,
            task=assignment.task,
            transport=lambda: transport(recording.agent),
            read_only=read_only,
            sandbox=sandbox,
            codex_transport=codex_transport,
            coordinator_prompt_sha256=coordinator_prompt_sha256,
        )

    return run_one


def record_dispatch_result(
    resolve: Callable[[], WorkerResult],
    *,
    run_dir: Path | None,
    workspace: Path | None,
    agent: Callable[[], Agent],
    prompt: Callable[[], str | None],
    task: str,
    transport: Callable[[], str],
    read_only: bool,
    sandbox: str | None,
    codex_transport: str = "exec",
    coordinator_prompt_sha256: str | None = None,
) -> WorkerResult:
    from .run_receipts import agent_result_from_worker

    def record(result: WorkerResult, window: _ProjectionWindow) -> None:
        if run_dir is not None and (sent_prompt := prompt()) is not None:
            recording_agent = agent()
            if recording_agent.name != assigned_agent.name:
                window.unavailable = True
            record_result(
                run_dir=run_dir,
                workspace=workspace or Path.cwd(),
                agent=recording_agent,
                task=result.task,
                prompt=sent_prompt,
                result=agent_result_from_worker(result),
                read_only=read_only,
                sandbox=sandbox,
                remote=result.remote is not None,
                codex_transport=codex_transport,
                projection_window=window,
                coordinator_prompt_sha256=coordinator_prompt_sha256,
            )

    assigned_agent = agent()
    with _projection_window(run_dir, assigned_agent) as window:
        try:
            result = resolve()
        except Exception as exc:
            try:
                record(
                    dispatch_failure(
                        exc, worker=assigned_agent.name, task=task, transport=transport(), requested_model=agent().model
                    ),
                    window,
                )
            except Exception:
                pass
            # Observation must never change dispatch or scheduler error semantics.
            raise
        record(result, window)
    return result


def redaction_affects(event: run_journal.RunEvent, start: int, end: int) -> bool:
    """A whole-store purge also retires references outside the selected range."""
    return start <= event.sequence <= end or (
        event.event_type == EVENT_TYPE and dict(event.payload) != REDACTED_REFERENCE
    )


def redact_sidecars(run_dir: Path) -> None:
    """Delete only after durable journal redaction and its anchor, also on retry."""
    from .run_redaction import RedactionError

    try:
        store.purge(run_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RedactionError("cassette sidecar redaction failed") from exc


def strip_for_export(run_dir: Path) -> None:
    """Strip every cassette class on a staging copy, including crash temps."""
    try:
        store.purge(run_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        raise store.CassetteError("cassette export strip refused unsafe sidecars") from exc


def assert_export_safe(root: Path) -> None:
    """Archive/evidence builders refuse bodies, even falsely labelled metadata."""
    try:
        with store.directory(root) as parent:
            if dirfd.child_names(parent, 1):
                raise store.CassetteError("cassette bodies cannot cross an export boundary")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise store.CassetteError("cassette export inventory is unsafe") from exc


def refuse_unstripped_archive(root: Path) -> None:
    """Use the verification archiver's bounded OSError failure contract."""
    try:
        assert_export_safe(root)
    except store.CassetteError as exc:
        raise OSError("cassette bodies cannot cross an archive boundary") from exc


def _path_present(path: Path) -> bool:
    path = Path(os.path.abspath(path))
    try:
        parent = dirfd.open_directory_chain(path.parent, writable=False)
        try:
            dirfd.stat_child(parent, path.name)
            return True
        finally:
            os.close(parent)
    except FileNotFoundError:
        return False


def _purge_run_dir(run: str, *, cwd: Path, runs_dir: Path | None) -> Path:
    """Resolve names without erasing links before the destructive no-follow walk."""
    from . import run_id, runs_cmd

    raw = Path(run).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        candidate = raw
    elif _path_present(cwd / raw):
        candidate = cwd / raw
    else:
        root = runs_dir.expanduser() if runs_dir is not None else cwd.expanduser() / ".brigade" / "runs"
        # Inspect only a root whose original components pass the same no-follow gate.
        os.close(dirfd.open_directory_chain(root.absolute(), writable=False))
        if run == "latest":
            runs = [(root / name, meta) for _, name, meta in store.validated_runs(root)]
            runs.sort(key=runs_cmd._run_sort_key, reverse=True)
            if not runs:
                raise FileNotFoundError("run not found")
            candidate = runs[0][0]
        else:
            candidates = [
                root / raw,
                *(
                    root / name
                    for name in run_id.lookup_names_for_workspace(run, cwd)
                    if runs_cmd._is_bare_run_id(name)
                ),
            ]
            candidate = next((p for p in candidates if _path_present(p)), root / raw)
    os.close(dirfd.open_directory_chain(candidate.absolute(), writable=False))
    _read_object(candidate / "run.json")
    return candidate


def purge_cli(run: str, *, cwd: Path, runs_dir: Path | None = None) -> int:
    try:
        run_dir = _purge_run_dir(run, cwd=cwd, runs_dir=runs_dir)
        metadata = _read_object(run_dir / "run.json")
        workspace = runguard.resolve_run_lock_workspace(metadata, run_dir) or cwd
        if workspace is None or runguard.has_active_run_owner(workspace, run_dir):
            raise store.CassetteError("cassette purge requires an inactive run")
        # Acquire before the publication locks: a newly starting owner must lose
        # the acquisition race, and dead owners use the normal recovery policy.
        with runguard.run_lock(workspace, run_dir=run_dir):
            if not runguard.is_active_run_owner(workspace, run_dir):
                raise store.CassetteError("cassette purge lost run lock ownership")
            count = store.purge_inactive(run_dir, workspace=workspace)
    except (OSError, ValueError, RuntimeError):
        print("error: cassette purge refused unsafe sidecars", file=sys.stderr)
        return 2
    print(f"cassette sidecars removed: {count}")
    return 0
