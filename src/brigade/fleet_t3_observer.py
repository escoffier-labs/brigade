"""Opt-in native T3 Code thread presence, independent of managed fleet receipts.

The explicitly configured local interpreter must already contain t3-fleet.
The subprocess reads only the native shell and returns operational metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence, cast

from . import fleet_client, fleet_client_policy, proc
from .fleet_openclaw_observer import (
    OBSERVATION_BUDGET_SECONDS,
    PUBLISH_LIMIT,
    TTL_SECONDS,
    ObservationError,
    RecentSession,
    _PublicationBatch,
    _absolute_workspace,
    _bounded_json,
    _opaque_id,
    _publish_recent,
    _remaining,
    _require_node_auth,
    _select_publication_batch,
)

from .fleet_session_presence import ProviderLinkState

# The detail pass keeps this fixed cap. The first snapshot instead gets the rest of the observation
# budget minus PUBLISH_RESERVE_SECONDS, so a slow shell read under load still fits.
NATIVE_TIMEOUT_SECONDS = 8
# Held back for the session publish and the health post that follow the snapshot.
PUBLISH_RESERVE_SECONDS = 3.5
INVENTORY_TTL_SECONDS = 900
DETAIL_RESERVE_SECONDS = 1.5
MAX_THREADS = 10_000
MAX_PROJECTS = 2_000
MAX_METADATA_BYTES = 262_144
_ACTIVE = frozenset({"queued", "preparing", "starting", "running", "waiting"})
_TERMINAL = frozenset({"completed", "interrupted", "failed", "cancelled", "rolled_back"})
# Constant program, prefixed only with budgets and validated, quoted native IDs.
# The installed read client
# bounds shell and detail responses to 16 MiB. No messages, provider turns,
# target store or raw shell data are serialized here. Native auth may issue its
# own local grant; no grant values are exposed by this helper.
_NATIVE_PROGRAM = r"""
import json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor, wait
STARTED = time.monotonic()
def emit(text):
    # Raw bytes with no trailing newline: Windows text mode would write CRLF and grow the output.
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()
try:
    from t3_fleet.agent import _build_agent
    client = _build_agent(os.environ).t3
    # The optional environment id starts first and gets at most about a second, so a
    # stalled descriptor cannot use the budget. A detail pass never asks for it.
    descriptor_future = None
    if DETAIL_THREAD_IDS is None:
        descriptor_future = ThreadPoolExecutor(max_workers=1).submit(lambda: client.descriptor().get("environmentId"))
    shell = client.shell_snapshot()
    if not isinstance(shell, dict) or type(shell.get("schemaVersion")) is not int or shell["schemaVersion"] != 2:
        raise ValueError()
    projects, threads = (shell.get(k) for k in ("projects", "threads"))
    archived = shell.get("archivedThreads", [])
    if not isinstance(projects, list) or len(projects) > 2000 or not isinstance(threads, list) or not isinstance(archived, list) or len(threads) + len(archived) > 10000:
        raise ValueError()
    if any(not isinstance(row, dict) for row in projects + threads + archived):
        raise ValueError()
    active_states = {"queued", "preparing", "starting", "running", "waiting"}
    terminal_states = {"completed", "interrupted", "failed", "cancelled", "rolled_back"}
    active = []
    for row in threads:
        status = row.get("status")
        if not isinstance(status, str) or status not in active_states | terminal_states | {"idle"}:
            raise ValueError()
        if "activeRunId" not in row:
            raise ValueError()
        run_id, activity = row["activeRunId"], row.get("activityRunStatus")
        if activity is not None and (not isinstance(activity, str) or activity not in active_states):
            raise ValueError()
        pending = row.get("pendingRuntimeRequest")
        background = row.get("pendingBackgroundTasks", [])
        if pending is not None and (not isinstance(pending, dict) or any(not isinstance(pending.get(k), str) or not pending[k] for k in ("id", "kind", "createdAt"))):
            raise ValueError()
        if pending is not None and pending["kind"] not in {"command", "file-read", "file-change", "mcp-elicitation", "permission", "dynamic_tool_call", "user_input", "auth_refresh"}:
            raise ValueError()
        if not isinstance(background, list) or any(not isinstance(t, dict) or not isinstance(t.get("taskId"), str) or not t["taskId"] for t in background):
            raise ValueError()
        if row.get("archivedAt") is not None or row.get("deletedAt") is not None or status in terminal_states:
            continue
        if status in active_states or run_id is not None or activity is not None or bool(pending) or bool(background):
            active.append(row)
    # Small fleets resolve in this call. Large fleets first return metadata so
    # the parent can select the persisted publication window before details.
    if DETAIL_THREAD_IDS is not None:
        active = [row for row in active if row.get("id") in DETAIL_THREAD_IDS]
    linked = active if DETAIL_THREAD_IDS is not None or len(active) <= PUBLISH_LIMIT else []
    UNKNOWN = object()
    def provider_link(row):
        active_provider = row.get("activeProviderThreadId")
        if not active_provider:
            return None
        try:
            detail = client.thread_snapshot(row["id"])["thread"]
            if detail.get("activeProviderThreadId") != active_provider:
                return None
            provider = next((p for p in detail.get("providerThreads", []) if p.get("id") == active_provider), None)
            if provider is None:
                return None
            ref = provider.get("nativeThreadRef")
            harness = {"claudeAgent": "claude", "codex": "codex"}.get(provider.get("driver"))
            if harness and isinstance(ref, dict) and ref.get("strength") == "strong" and ref.get("driver") == provider.get("driver"):
                return {"harness": harness, "session_id": ref.get("nativeId")}
        except Exception:
            return UNKNOWN  # Failed lookups cannot invalidate cached enrichment.
        return None
    environment_id = None
    if descriptor_future is not None:
        descriptor_done, _ = wait([descriptor_future], timeout=max(0.0, min(1.0, BUDGET_SECONDS - RESERVE_SECONDS - (time.monotonic() - STARTED))))
        if descriptor_done and descriptor_future.exception() is None:
            environment_id = descriptor_future.result()
    # Optional links get at most 4s, and never the slack reserved to print the snapshot.
    detail_wait = min(4.0, BUDGET_SECONDS - RESERVE_SECONDS - (time.monotonic() - STARTED))
    pool = ThreadPoolExecutor(max_workers=8)
    window = linked[:50] if detail_wait > 0 else []
    futures = {pool.submit(provider_link, row): row for row in window}
    done, _ = wait(futures, timeout=max(detail_wait, 0)) if futures else (set(), set())
    for future in done:
        link = future.result()
        if link is not UNKNOWN:
            futures[future]["provider_link"] = link
    pool.shutdown(wait=False, cancel_futures=True)
    needed = {row.get("projectId") for row in active if isinstance(row.get("projectId"), str)}
    result = {
        "schemaVersion": 2,
        "total_threads": len(threads) + len(archived),
        "projects": [{k: row.get(k) for k in ("id", "workspaceRoot")} for row in projects if isinstance(row.get("id"), str) and row.get("id") in needed],
        "threads": [{**{k: row.get(k) for k in ("id", "projectId", "status", "activeRunId", "activityRunStatus", "worktreePath", "archivedAt", "deletedAt", "provider_link") if k in row}, "hasPendingRuntimeRequest": bool(row.get("pendingRuntimeRequest")), "hasPendingBackgroundTasks": bool(row.get("pendingBackgroundTasks"))} for row in active],
        "archived_count": len(archived),
    }
    output = json.dumps(result)
    # The exact bytes are written below with no newline, so this cap is the parent's byte cap.
    if len(output.encode("utf-8")) > 262144:
        raise ValueError()
    # Separate repository inventory: never workspace roots, titles, paths or URLs. The row
    # predicate is the Hub's, injected above. Any failure, or a result over the size cap,
    # leaves the inventory out, meaning unknown.
    try:
        def clean_id(value, limit):
            if isinstance(value, str) and 0 < len(value) <= limit and all(not c.isspace() and c.isprintable() for c in value):
                return value
            return None
        def project_id_ok(value):
            return isinstance(value, str) and len(value) <= MAX_PROJECT_ID_CHARS and re.fullmatch(PROJECT_ID_PATTERN, value) is not None
        def normal_key(value):
            if not isinstance(value, str) or len(value) > MAX_ID_CHARS:
                return None
            key = value.casefold()
            if key.endswith(".git"):
                key = key[:-4]
            if len(key) > MAX_ID_CHARS or re.fullmatch(REPO_KEY_PATTERN, key) is None:
                return None
            return key
        if clean_id(environment_id, MAX_ENVIRONMENT_ID_CHARS) is not None:
            inventory_rows, seen_ids, dropped = [], set(), 0
            for row in projects:
                identity = row.get("repositoryIdentity")
                raw_key = identity.get("canonicalKey") if isinstance(identity, dict) else None
                if raw_key is None or row.get("archivedAt") is not None or row.get("deletedAt") is not None:
                    continue
                project_id, key = row.get("id"), normal_key(raw_key)
                if not project_id_ok(project_id) or key is None or project_id in seen_ids:
                    dropped += 1
                    continue
                seen_ids.add(project_id)
                inventory_rows.append({"project_id": project_id, "repo_key": key})
            candidate = json.dumps({**result, "inventory": {"environment_id": environment_id, "projects": inventory_rows[:MAX_INVENTORY_PROJECTS], "dropped": dropped}})
            if len(candidate.encode("utf-8")) <= 262144:
                output = candidate
    except Exception:
        pass
    emit(output)
except Exception:
    emit('{"error":"native_metadata_failed"}')
# Exit without joining detail workers: a stalled optional lookup must not
# hold the subprocess past its budget after the snapshot is printed.
sys.stdout.flush()
os._exit(0)
"""


def _native_timeout(deadline: float, *, detail: bool) -> float:
    """Seconds the native subprocess may run. Raises observation_timeout when none is left."""
    if detail:
        # A failed optional phase must leave time to publish the metadata batch.
        reserve = fleet_client.REPORT_TIMEOUT_SECONDS + DETAIL_RESERVE_SECONDS
        return _remaining(deadline, cap=NATIVE_TIMEOUT_SECONDS, reserve=reserve)
    return _remaining(deadline, cap=float("inf"), reserve=PUBLISH_RESERVE_SECONDS)


def _native_metadata(interpreter: str, deadline: float, *, detail_thread_ids: list[str] | None = None) -> object:
    path = Path(interpreter)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ObservationError("t3_python_unavailable")
    if detail_thread_ids is not None:
        if len(detail_thread_ids) > PUBLISH_LIMIT:
            raise ObservationError("native_metadata_invalid")
        detail_thread_ids = [_native_id(value) for value in detail_thread_ids]
    timeout = _native_timeout(deadline, detail=detail_thread_ids is not None)
    from . import fleet_node_projects as inventory_rules

    program = (
        f"BUDGET_SECONDS = {float(timeout)!r}\nRESERVE_SECONDS = {DETAIL_RESERVE_SECONDS!r}\n"
        f"PUBLISH_LIMIT = {PUBLISH_LIMIT!r}\nDETAIL_THREAD_IDS = {detail_thread_ids!r}\n"
        f"REPO_KEY_PATTERN = {inventory_rules.REPO_KEY_PATTERN!r}\nMAX_ID_CHARS = {inventory_rules.MAX_ID_CHARS!r}\n"
        f"MAX_ENVIRONMENT_ID_CHARS = {inventory_rules.MAX_ENVIRONMENT_ID_CHARS!r}\n"
        f"PROJECT_ID_PATTERN = {inventory_rules.PROJECT_ID_PATTERN!r}\n"
        f"MAX_PROJECT_ID_CHARS = {inventory_rules.MAX_PROJECT_ID_CHARS!r}\n"
        f"MAX_INVENTORY_PROJECTS = {inventory_rules.MAX_PROJECTS!r}\n" + _NATIVE_PROGRAM
    )
    result = proc.run(
        [str(path), "-c", program],
        cwd=path.parent,
        timeout=timeout,
        supervise_group=True,
    )
    _remaining(deadline, cap=1)
    payload = _bounded_json(result, limit=MAX_METADATA_BYTES)
    if isinstance(payload, dict) and payload.get("error"):
        raise ObservationError("native_metadata_failed")
    return payload


def _native_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(c.isspace() or not c.isprintable() for c in value)
    ):
        raise ObservationError("native_metadata_invalid")
    return value


def _presence_id(native_id: str) -> str:
    native_id = _native_id(native_id)
    try:
        return _opaque_id(native_id)
    except ObservationError:
        return "t3-" + hashlib.sha256(native_id.encode("utf-8")).hexdigest()


def _optional_link(link: object) -> dict[str, str] | None | ProviderLinkState:
    """Malformed or missing enrichment is unknown. Only explicit null removes a link."""
    if link is None:
        return None
    if not isinstance(link, dict) or link.get("harness") not in ("claude", "codex"):
        return ProviderLinkState.UNKNOWN
    try:
        return {"harness": link["harness"], "session_id": _opaque_id(link.get("session_id"))}
    except ObservationError:
        return ProviderLinkState.UNKNOWN


def _active_threads(payload: object) -> tuple[list[RecentSession], int, int]:
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 2:
        raise ObservationError("native_metadata_invalid")
    projects, threads, archived = (payload.get(k) for k in ("projects", "threads", "archived_count"))
    total = payload.get("total_threads")
    if (
        not isinstance(projects, list)
        or len(projects) > MAX_PROJECTS
        or not isinstance(threads, list)
        or len(threads) > MAX_THREADS
        or type(archived) is not int
        or archived < 0
        or type(total) is not int
        or total < len(threads) + archived
        or total > MAX_THREADS
    ):
        raise ObservationError("native_metadata_invalid")
    roots: dict[str, object] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise ObservationError("native_metadata_invalid")
        try:
            project_id = _native_id(project.get("id"))
        except ObservationError:
            continue
        if project_id in roots:
            raise ObservationError("native_identity_collision")
        # Only referenced active projects need a usable path.
        roots[project_id] = project.get("workspaceRoot")
    invalid = 0
    active: list[RecentSession] = []
    seen: set[str] = set()
    now_ms = time.time_ns() // 1_000_000
    for row in threads:
        if not isinstance(row, dict):
            raise ObservationError("native_metadata_invalid")
        status, run_id, activity = (row.get(k) for k in ("status", "activeRunId", "activityRunStatus"))
        if not isinstance(status, str) or status not in _ACTIVE | _TERMINAL | {"idle"}:
            raise ObservationError("native_state_unknown")
        if "activeRunId" not in row:
            raise ObservationError("native_metadata_invalid")
        if activity is not None and (not isinstance(activity, str) or activity not in _ACTIVE):
            raise ObservationError("native_state_unknown")
        if row.get("archivedAt") is not None or row.get("deletedAt") is not None or status in _TERMINAL:
            continue
        pending = row.get("hasPendingRuntimeRequest", False)
        background = row.get("hasPendingBackgroundTasks", False)
        if type(pending) is not bool or type(background) is not bool:
            raise ObservationError("native_metadata_invalid")
        try:
            session_id = _presence_id(_native_id(row.get("id")))
        except ObservationError:
            invalid += 1
            continue
        if session_id in seen:
            raise ObservationError("native_identity_collision")
        seen.add(session_id)
        if status not in _ACTIVE and run_id is None and activity is None and not pending and not background:
            continue
        try:
            if run_id is not None:
                _native_id(run_id)
            raw = row.get("worktreePath")
            if raw is None:
                project_id = _native_id(row.get("projectId"))
                if project_id not in roots:
                    raise ObservationError("native_metadata_invalid")
                raw = roots[project_id]
            link = _optional_link(row.get("provider_link", ProviderLinkState.UNKNOWN))
            active.append(RecentSession(session_id, _absolute_workspace(raw), now_ms, link))
        except ObservationError:
            invalid += 1
    return active, total, invalid


def _inventory(payload: object) -> tuple[str, list[dict[str, str]], int] | None:
    """The optional repository inventory with its dropped-row count. Absent means unknown, never empty."""
    from . import fleet_node_projects as rules

    raw = cast(dict[str, object], payload).get("inventory")
    if not isinstance(raw, dict):
        return None
    environment_id = rules.clean_id(raw.get("environment_id"), rules.MAX_ENVIRONMENT_ID_CHARS)
    projects = raw.get("projects")
    native_dropped = raw.get("dropped", 0)
    if (
        environment_id is None
        or not isinstance(projects, list)
        or len(projects) > rules.MAX_PROJECTS
        or type(native_dropped) is not int
        or native_dropped < 0
    ):
        return None
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    dropped = native_dropped
    for item in projects:
        row = rules.project_row(item)
        if row is None or row[0] in seen:
            dropped += 1
            continue
        seen.add(row[0])
        rows.append({"project_id": row[0], "repo_key": row[1]})
    return environment_id, rows, dropped


def _publish_inventory(payload: object, *, deadline: float) -> tuple[str, int]:
    """Send the inventory once. Returns (ok|error|skipped, dropped rows) and never raises."""
    try:
        inventory = _inventory(payload)
    except Exception:
        return "skipped", 0
    if inventory is None:
        return "skipped", 0
    if not inventory[1] and inventory[2]:
        # Every reported row was invalid, so the set is unknown rather than empty.
        return "skipped", inventory[2]
    try:
        _remaining(deadline, cap=1, reserve=fleet_client_policy.POLICY_TIMEOUT_SECONDS)
        node_id = fleet_client.resolve_node_id()
        if node_id == "unknown":
            return "error", inventory[2]
        fleet_client_policy.observe_projects(
            {
                "node_id": node_id,
                "environment_id": inventory[0],
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": INVENTORY_TTL_SECONDS,
                "projects": inventory[1],
            }
        )
    except Exception:
        return "error", inventory[2]
    return "ok", inventory[2]


def _link_publication_batch(
    batch: _PublicationBatch, payload: object, *, interpreter: str, deadline: float
) -> _PublicationBatch:
    if batch.error or not batch.selected:
        return batch
    try:
        wanted = {s.session_id for s in batch.selected}
        native_ids = []
        for row in cast(dict[str, list[dict[str, object]]], payload)["threads"]:
            try:
                native_id = _native_id(row.get("id"))
            except ObservationError:
                continue
            if _presence_id(native_id) in wanted:
                native_ids.append(native_id)
        details, _, _ = _active_threads(_native_metadata(interpreter, deadline, detail_thread_ids=native_ids))
        links = {(s.session_id, s.workspace): s.provider_link for s in details}
        return replace(
            batch,
            selected=[
                replace(s, provider_link=links.get((s.session_id, s.workspace), ProviderLinkState.UNKNOWN))
                for s in batch.selected
            ],
        )
    except Exception:
        # Native details are optional. Keep the already validated shell batch.
        return batch


class _ObservationFailed(ObservationError):
    """An observation error that also carries the outcome of the best-effort failure health post."""

    def __init__(self, code: str, health: str) -> None:
        super().__init__(code)
        self.health = health


def _publish_health(ok: bool, *, deadline: float) -> str:
    """Tell the Hub whether this observation succeeded: (ok|error|skipped) is the post outcome. Never raises.

    The Hub stamps its own receipt time, so no timestamp is sent. Success means the Hub accepted
    current presence, so a caller sends ok=True only after the session publication returned cleanly.
    """
    try:
        _remaining(deadline, cap=1, reserve=fleet_client_policy.POLICY_TIMEOUT_SECONDS)
    except ObservationError:
        return "skipped"
    try:
        node_id = fleet_client.resolve_node_id()
        if node_id == "unknown":
            return "error"
        fleet_client_policy.observe_t3({"node_id": node_id, "ok": ok})
    except Exception:
        return "error"
    return "ok"


def observe(*, t3_python: str, publish: bool = False) -> dict[str, object]:
    deadline = time.monotonic() + OBSERVATION_BUDGET_SECONDS
    if publish:
        _require_node_auth()
    try:
        payload = _native_metadata(t3_python, deadline)
        recent, total, invalid = _active_threads(payload)
        _remaining(deadline, cap=1)
    except Exception as exc:
        if not publish:
            raise
        # A failing observer reports so; a missing report is just as unknown, once the TTL passes.
        code = str(exc) if isinstance(exc, ObservationError) else "observation_failed"
        raise _ObservationFailed(code, _publish_health(False, deadline=deadline)) from exc
    report: dict[str, object] = {
        "ok": not invalid,
        "mode": "publish" if publish else "inspect",
        "activity_scope": "native_t3_presence",
        "active_threads": len(recent),
        "observed": len(recent),
        "total_threads": total,
        "published": 0,
        "skipped": invalid,
        "invalid_rows": invalid,
        "truncated": False,
        "ttl_seconds": TTL_SECONDS,
        "budget_seconds": OBSERVATION_BUDGET_SECONDS,
    }
    if publish:
        batch = _select_publication_batch(recent, harness="t3", deadline=deadline)
        if len(cast(dict[str, list[object]], payload)["threads"]) > PUBLISH_LIMIT:
            batch = _link_publication_batch(batch, payload, interpreter=t3_python, deadline=deadline)
        report.update(_publish_recent(recent, harness="t3", deadline=deadline, batch=batch))
        report["skipped"] = cast(int, report["skipped"]) + invalid
        report["ok"] = not (report["truncated"] or invalid or report.get("error"))
        # Never changes ok or the exit code. Rotation truncation and the inventory do not matter:
        # only a session publication that ran without error counts as current presence.
        report["health"] = _publish_health(not report.get("error"), deadline=deadline)
        # After sessions, within the same deadline. Never changes ok or the exit code.
        report["inventory"], report["inventory_dropped"] = _publish_inventory(payload, deadline=deadline)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--t3-python", required=True, help="Absolute path to the existing trusted T3 fleet Python")
    parser.add_argument("--publish", action="store_true", help="Publish through this enrolled node")
    args = parser.parse_args(argv)
    try:
        report = observe(t3_python=args.t3_python, publish=args.publish)
    except _ObservationFailed as exc:
        report = {
            "ok": False,
            "error": str(exc),
            "observed": 0,
            "published": 0,
            "skipped": 0,
            "health": exc.health,
        }
    except ObservationError as exc:
        report = {"ok": False, "error": str(exc), "observed": 0, "published": 0, "skipped": 0}
    except Exception:
        report = {"ok": False, "error": "observation_failed", "observed": 0, "published": 0, "skipped": 0}
    print(json.dumps(report, sort_keys=True))
    if report["ok"]:
        return 0
    return (
        2
        if report.get("truncated")
        and not report.get("invalid_rows")
        and report.get("error") in (None, "observation_timeout")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
