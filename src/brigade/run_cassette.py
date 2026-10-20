"""Classified worker-level recordings. Cassettes never supply lifecycle authority.

Only locally observed results are recorded. Prompts and raw notification streams
are never stored here. Deleting these sidecars cannot change offline audit state.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from . import agents, config, dirfd, run_audit, run_events, run_journal, worker_events
from . import run_cassette_store as store
from .roster import Agent

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
    return tuple(sorted({v for v in resolved.values() if v}, key=len, reverse=True))


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {_scrub(k, secrets): _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    return value


def _projection(run_dir: Path, agent: Agent) -> dict[str, Any] | None:
    dirfd.validate_component(agent.name)
    try:
        parent = dirfd.open_directory_chain(Path(os.path.abspath(run_dir / "events")))
    except FileNotFoundError:
        return None
    try:
        try:
            raw = store.read(parent, f"{agent.name}.jsonl", private=False)
        except FileNotFoundError:
            return None
    finally:
        os.close(parent)
    return worker_events.scrub_stream_text(raw.decode("utf-8"))


def _append_reference(run_dir: Path, ref: dict[str, Any]) -> None:
    from . import run_shadow

    journal = run_dir / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal_bounded(journal)
    if report.chain_errors or report.partial_tail:
        raise store.CassetteError("cassette journal is not readable")
    sequence = len(report.events)
    # Leave space for lifecycle completion. The recorder never consumes the tail.
    if sequence >= 2048 - 8 or journal.stat().st_size + 2048 > 8 * 1024 * 1024:
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
    )
    # Like live-control facts, neutral cassette facts advance shadow evidence
    # under the same pair/journal lock without changing run.json authority.
    if snapshot is not None and snapshot.get("lifecycle_journal_requested") is True:
        run_shadow.record_shadow_comparison(run_dir, snapshot)


def _stop(run_dir: Path) -> None:
    """A single bounded stop fact when storage or policy cannot admit a body."""
    from . import run_lifecycle

    print("cassette recording stopped: storage, classification, or byte cap", file=sys.stderr)
    try:
        if _finished(run_dir):
            return
        journal = run_dir / "events" / "lifecycle.jsonl"
        run_journal.ensure_journal(journal)
        with (
            run_lifecycle.checkpoint_event_pair(),
            run_journal.journal_mutation(journal),
            store.directory(run_dir, create=True) as parent,
        ):
            if _finished(run_dir):
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
            if fits:
                _append_reference(run_dir, _reference(data, "public-metadata"))
    except (OSError, ValueError, RuntimeError):
        # A full or unavailable journal cannot carry even a stop reference.
        pass


def record_result(
    *,
    run_dir: Path,
    cwd: Path,
    agent: Agent,
    task: str,
    prompt: str,
    result: agents.AgentResult,
    read_only: bool,
    sandbox: str | None,
    remote: bool = False,
) -> dict[str, Any] | None:
    """Best-effort recording. Failure stops recording, never the worker run."""
    try:
        return _record_result(
            run_dir=run_dir,
            cwd=cwd,
            agent=agent,
            task=task,
            prompt=prompt,
            result=result,
            read_only=read_only,
            sandbox=sandbox,
            remote=remote,
        )
    except (OSError, ValueError, RuntimeError, TypeError):
        _stop(run_dir)
        return None


def _record_result(
    *,
    run_dir: Path,
    cwd: Path,
    agent: Agent,
    task: str,
    prompt: str,
    result: agents.AgentResult,
    read_only: bool,
    sandbox: str | None,
    remote: bool,
) -> dict[str, Any] | None:
    from . import run_lifecycle, run_receipts

    cfg = config.load_config(cwd)
    mode = cfg.run_record if cfg is not None else "auto"
    if mode == "off":
        return None
    try:
        with store.directory(run_dir) as parent:
            if any(name == ".stopped" for name, _ in store.inventory(parent)):
                return None
    except FileNotFoundError:
        pass
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
            "transport": transport,
        },
        "coordinator_prompt_fingerprint": fingerprint,
    }
    key = hashlib.sha256(run_events.canonical_bytes(identity)).hexdigest()
    observed = not remote and transport in OBSERVED_TRANSPORTS and result.failure_phase != "preflight"
    secrets = _secret_values(agent) if observed else ()
    body = _scrub(run_receipts.agent_result_payload(result), secrets) if observed else None
    projection = (
        _scrub(_projection(run_dir, agent), secrets)
        if observed and transport in {"app-server", "codex-app-server"}
        else None
    )
    blobs: list[tuple[str, bytes]] = []
    metadata: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "privacy_class": "scrubbed",
        "key": key,
        "status": "observed" if observed else "not-observed",
        "reason": "local-result" if observed else "remote" if remote else "unsupported-or-unobserved",
    }
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
    blobs.append(("scrubbed", store.encode(metadata)))
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "privacy_class": "public-metadata",
        "key": key,
        "coordinator_prompt_fingerprint": fingerprint,
        "full_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "status": metadata["status"],
        "artifacts": [_reference(data, cls) for cls, data in blobs],
    }
    data = store.encode(manifest)
    blobs.append(("public-metadata", data))
    if sum(len(raw) for _, raw in blobs) > store.MAX_ENTRY_BYTES:
        raise store.CassetteError("cassette entry byte cap reached")
    journal = run_dir / "events" / "lifecycle.jsonl"
    run_journal.ensure_journal(journal)
    with (
        run_lifecycle.checkpoint_event_pair(),
        run_journal.journal_mutation(journal),
        store.directory(run_dir, create=True) as parent,
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
        report = run_journal.read_journal_bounded(journal)
        if (
            report.chain_errors
            or report.partial_tail
            or len(report.events) >= 2048 - 8
            or journal.stat().st_size + 2048 > 8 * 1024 * 1024
        ):
            raise store.CassetteError("cassette journal cap reached")
        for _, raw in blobs:
            store.publish(parent, raw)
        ref = _reference(data, "public-metadata")
        _append_reference(run_dir, ref)
    store.retain_private(cwd, run_dir.parent, current=run_dir)
    return ref


def record_dispatch_result(
    result: agents.AgentResult,
    *,
    run_dir: Path | None,
    cwd: Path | None,
    agent: Agent,
    task: str,
    prompt: str,
    read_only: bool,
    sandbox: str | None,
    remote: bool,
) -> None:
    if run_dir is not None:
        record_result(
            run_dir=run_dir,
            cwd=cwd or Path.cwd(),
            agent=agent,
            task=task,
            prompt=prompt,
            result=result,
            read_only=read_only,
            sandbox=sandbox,
            remote=remote,
        )


def redact_sidecars(run_dir: Path) -> None:
    """Conservative deletion before journal redaction, including retry paths."""
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


def purge_cli(run: str, *, cwd: Path, runs_dir: Path | None = None) -> int:
    from . import runs_cmd

    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error or run_dir is None:
        print(error or "run not found", file=sys.stderr)
        return 2
    try:
        count = store.purge(run_dir)
    except (OSError, ValueError, RuntimeError):
        print("error: cassette purge refused unsafe sidecars", file=sys.stderr)
        return 2
    print(f"cassette sidecars removed: {count}")
    return 0
