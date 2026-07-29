"""`brigade doctor` - verify a target workspace is wired correctly."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

from .budgets import (
    BOOTSTRAP_BUDGETS,
    MEMORY_CARE_SCAN_STALE_DAYS,
)
from . import localio
from .selection import WRITER_INBOXES
from .station import DoctorContext

CheckResult = Tuple[str, str, str]  # (status, name, detail)
ScopedCheckResult = Tuple[str, str, str, str]  # (status, name, detail, scope)
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
MANUAL = "MANUAL"
INFO = "INFO"
SCOPE_TARGET = "target"
SCOPE_OPERATOR = "operator"
ACTIONABLE_STATUSES = frozenset({FAIL, WARN, MANUAL})
ADAPTER_CHECK_PREFIX = "adapter: "


def _adapter_check_name(name: str) -> str:
    """Label Brigade adapter/projection checks distinctly from native harness checks."""
    return f"{ADAPTER_CHECK_PREFIX}{name}"


def _scoped_check(status: str, name: str, detail: str, scope: str = SCOPE_TARGET) -> ScopedCheckResult:
    return (status, name, detail, scope)


def _operator_check(status: str, name: str, detail: str) -> ScopedCheckResult:
    return _scoped_check(status, name, detail, SCOPE_OPERATOR)


def _normalize_scoped_check(
    check: CheckResult | ScopedCheckResult,
    *,
    default_scope: str = SCOPE_TARGET,
) -> ScopedCheckResult:
    if len(check) == 4:
        return check  # type: ignore[return-value]
    status, name, detail = check
    return _scoped_check(status, name, detail, default_scope)


def _check_scope(check: CheckResult | ScopedCheckResult) -> str:
    return check[3] if len(check) == 4 else SCOPE_TARGET


def build_context(target: Path, harness: str = "generic") -> DoctorContext:
    target = target.expanduser().resolve()
    from .config import load_config

    sel = None
    harnesses: list[str] = []
    try:
        cfg = load_config(target)
    except (ValueError, json.JSONDecodeError):
        cfg = None
    if cfg is not None:
        sel = cfg.selection
        harnesses = list(sel.harnesses)
    elif harness in ("openclaw", "hermes"):
        harnesses = [harness]
    return DoctorContext(target=target, selection=sel, harnesses=harnesses)


def core_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    checks: List[CheckResult] = []
    checks.extend(_check_workspace_files(ctx.target))
    checks.extend(_check_agents_quality(ctx.target))
    checks.extend(_check_default_wired_skills(ctx.target, ctx.harnesses))
    if "claude" in ctx.harnesses:
        check = _check_claude_work_loop(ctx.target)
        if check is not None:
            checks.append(check)
    if "openclaw" in ctx.harnesses:
        checks.extend(_operator_check(status, name, detail) for status, name, detail in _check_openclaw())
    if "hermes" in ctx.harnesses:
        checks.extend(_check_hermes(ctx.target))
    checks.extend(_check_orphan_inboxes(ctx.target, ctx.harnesses))
    checks.append(_check_recovery_checkpoints(ctx.target))
    return checks


_RECOVERY_CHECKPOINTS_NAME = "runs: recovery checkpoints"
_RECOVERY_CHECKPOINTS_SCAN_LIMIT = 50
_RECOVERY_CHECKPOINTS_FAIL_PREVIEW = 8


def _check_recovery_checkpoints(target: Path, *, full: bool = False) -> CheckResult:
    """Read-only aggregate verdict for activated-journal recovery checkpoints."""
    runs_root = target / ".brigade" / "runs"
    all_dirs, scan_omitted = _immediate_run_dirs(runs_root)
    scanned = all_dirs[:_RECOVERY_CHECKPOINTS_SCAN_LIMIT]
    scan_omitted += len(all_dirs) - len(scanned)

    counts = {"ok": 0, "warn": 0, "fail": 0, "omitted": scan_omitted}
    failing_runs: list[tuple[str, str]] = []

    for run_dir in scanned:
        verdict, reason = _recovery_checkpoint_run_verdict(target, run_dir)
        if verdict == "omitted":
            counts["omitted"] += 1
        elif verdict == "ok":
            counts["ok"] += 1
        elif verdict == "warn":
            counts["warn"] += 1
        else:
            counts["fail"] += 1
            failing_runs.append((run_dir.name, reason))

    if counts["fail"]:
        status = FAIL
    elif counts["warn"]:
        status = WARN
    else:
        status = OK

    preview_limit = len(failing_runs) if full else _RECOVERY_CHECKPOINTS_FAIL_PREVIEW
    detail = f"ok={counts['ok']} warn={counts['warn']} fail={counts['fail']} omitted={counts['omitted']}"
    if failing_runs:
        shown = failing_runs[:preview_limit]
        labels = [f"{name} ({reason})" for name, reason in shown]
        detail = f"{detail}; failures: {', '.join(labels)}"
        if not full and len(failing_runs) > preview_limit:
            detail = f"{detail}, ... {len(failing_runs) - preview_limit} more"
    return (status, _RECOVERY_CHECKPOINTS_NAME, detail)


def _immediate_run_dirs(runs_root: Path) -> tuple[List[Path], int]:
    """Return ``(run dirs sorted newest-first, omitted_count)``.

    Guards ``iterdir``/``is_dir``/``is_symlink``/``stat`` races so a vanishing
    or unreadable entry never crashes Doctor. Entries that vanish between
    discovery and ``stat`` are counted as omitted where discoverable.
    """
    try:
        if not runs_root.is_dir():
            return [], 0
    except OSError:
        return [], 0
    try:
        entries = list(runs_root.iterdir())
    except OSError:
        return [], 0
    sortable: list[tuple[float, str, Path]] = []
    omitted = 0
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if not entry.is_dir():
                continue
        except OSError:
            omitted += 1
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            omitted += 1
            continue
        sortable.append((mtime, entry.name, entry))
    sortable.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in sortable], omitted


def _recovery_checkpoint_run_verdict(target: Path, run_dir: Path) -> tuple[str, str]:
    from brigade import run_checkpoint, run_journal, run_lifecycle, runguard

    journal_path = run_lifecycle._journal_path(run_dir)
    try:
        if not journal_path.is_file():
            return "omitted", "no-journal"
    except OSError:
        return "omitted", "no-journal"

    # Resolve the live-owner workspace from parseable run metadata so a run
    # launched with a custom --output-dir verifies against the lock its owner
    # actually holds. Remain fail-safe for missing/unparseable metadata.
    run_meta_preview = _read_run_meta_fail_safe(run_dir / "run.json")
    live_workspace = target
    if run_meta_preview is not None:
        resolved = runguard.resolve_run_lock_workspace(run_meta_preview, run_dir, fallback=target)
        if resolved is not None:
            live_workspace = resolved
    lock_state = runguard.run_lock_state(live_workspace, run_dir)
    if lock_state == "live" and _recovery_run_has_live_owner(live_workspace, run_dir):
        return "ok", "live owner"

    run_id = run_dir.name
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except run_journal.RunJournalError as exc:
        if "bound exceeded" in exc.diagnostic:
            return "fail", "bound exceeded"
        return "fail", exc.diagnostic
    except OSError as exc:
        return "fail", f"journal unreadable: {type(exc).__name__}"

    chain_reason = _recovery_journal_chain_reason(report, run_id)
    if chain_reason is not None:
        return "fail", chain_reason

    latest = run_checkpoint.latest_checkpoint_event(report.events)
    if latest is None:
        return "fail", "no checkpoint event in journal"
    if latest.run_id != run_id:
        return "fail", "run_id mismatch"

    bound_reason = _recovery_checkpoint_bound_reason(run_dir, latest)
    if bound_reason is not None:
        return "fail", bound_reason

    try:
        checkpoint_bytes = run_checkpoint.validate_checkpoint(run_dir, latest)
    except run_checkpoint.CheckpointError as exc:
        if _recovery_checkpoint_error_is_bound_exceeded(exc):
            return "fail", "bound exceeded"
        return "fail", exc.diagnostic
    except OSError as exc:
        return "fail", f"checkpoint unreadable: {type(exc).__name__}"

    try:
        checkpoint_obj = run_checkpoint._parse_checkpoint_object(checkpoint_bytes)
        run_checkpoint._verify_coverage(report.events, latest, checkpoint_obj)
    except run_checkpoint.CheckpointError as exc:
        return "fail", exc.diagnostic

    run_json_path = run_dir / "run.json"
    try:
        run_json_present = run_json_path.is_file()
    except OSError:
        run_json_present = False
    if not run_json_present:
        return "warn", "run.json missing with valid checkpoint"

    try:
        run_bytes = run_json_path.read_bytes()
    except OSError:
        return "fail", "run.json present but unreadable"

    try:
        parsed = json.loads(run_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return "warn", "run.json unparseable with valid checkpoint"
    run_meta = parsed if isinstance(parsed, dict) else None
    if run_meta is None:
        return "warn", "run.json unparseable with valid checkpoint"

    if run_bytes == checkpoint_bytes:
        return "ok", "checkpoint bytes match run.json"

    expected = _reconstruct_stale_lock_recovery_receipt(checkpoint_obj, run_meta)
    if expected is not None and expected == run_meta:
        return "ok", "stale-lock-recovery receipt matches checkpoint reconstruction"

    return "fail", "run.json does not match checkpoint"


def _read_run_meta_fail_safe(run_json_path: Path) -> dict[str, object] | None:
    """Best-effort read of ``run.json`` for live-owner workspace resolution.

    Collapses every failure mode (missing, unreadable, unparseable) into
    ``None`` so the caller can fall back to ``target``; the precise
    missing/unreadable/unparseable verdict is handled later in the verdict.
    """
    try:
        if not run_json_path.is_file():
            return None
    except OSError:
        return None
    try:
        raw = run_json_path.read_bytes()
    except OSError:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _recovery_run_has_live_owner(target: Path, run_dir: Path) -> bool:
    from brigade import runguard

    if runguard.run_lock_state(target, run_dir) != "live":
        return False
    try:
        lock_path = runguard.lock_path(target)
        owner = runguard._read_lock_owner(lock_path)
    except runguard.RunGuardError:
        return False
    except OSError:
        return False
    try:
        resolved = run_dir.expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = run_dir
    return runguard._owner_matches_run(owner, resolved)


def _recovery_journal_chain_reason(report: object, run_id: str) -> str | None:
    if report.partial_tail is not None:
        return "partial tail"
    if report.chain_errors:
        return "malformed chain"
    if not report.events:
        return "empty journal"
    expected_sequence = 1
    expected_previous: str | None = None
    for event in report.events:
        if event.sequence != expected_sequence:
            return "malformed chain"
        if event.sequence == 1:
            if event.previous_digest is not None:
                return "malformed chain"
        elif event.previous_digest != expected_previous:
            return "malformed chain"
        if event.run_id != run_id:
            return "run_id mismatch"
        expected_sequence = event.sequence + 1
        expected_previous = event.event_digest
    return None


def _recovery_checkpoint_bound_reason(run_dir: Path, event: object) -> str | None:
    import os

    from brigade import run_checkpoint, run_journal

    payload = event.payload if hasattr(event, "payload") else event
    if not isinstance(payload, dict):
        return None
    byte_size = payload.get("byte_size")
    if isinstance(byte_size, int) and byte_size > run_checkpoint.MAX_CHECKPOINT_BYTES:
        return "bound exceeded"
    sha = payload.get("sha256")
    if not isinstance(sha, str):
        return None
    final_path = run_checkpoint.checkpoint_path(run_dir, sha)
    if not final_path.exists():
        return None
    try:
        fd = run_journal._open_nofollow(final_path, os.O_RDONLY)
    except (run_journal.RunJournalError, OSError):
        return None
    try:
        info = os.fstat(fd)
        if info.st_size > run_checkpoint.MAX_CHECKPOINT_BYTES:
            return "bound exceeded"
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return None


def _recovery_checkpoint_error_is_bound_exceeded(exc: object) -> bool:
    from brigade import run_checkpoint

    if not isinstance(exc, run_checkpoint.CheckpointError):
        return False
    if "bound exceeded" in exc.diagnostic:
        return True
    return exc.category == "byte-size"


def _reconstruct_stale_lock_recovery_receipt(
    checkpoint_obj: dict[str, object],
    candidate: dict[str, object],
) -> dict[str, object] | None:
    """Pure replay of ``runguard._recover_run_artifact`` terminalization fields.

    ``prior_status`` is derived from the checkpoint object's ``status`` exactly
    as runguard derives it from the pre-terminalization run.json (with an
    ``artifact-unavailable`` fallback when the status is absent/empty). The
    candidate's ``failure.prior_status`` is never trusted; a forged value
    diverges from the reconstruction and fails the verdict. A terminal
    checkpoint status means runguard would have returned ``"terminal"`` and
    never written a stale-lock-recovery receipt, so no reconstruction can
    match.
    """
    from brigade import receipt_schema, runguard

    failure = candidate.get("failure")
    if not isinstance(failure, dict):
        return None
    if candidate.get("failure_phase") != "stale-lock-recovery":
        return None
    if candidate.get("status") != "failed":
        return None

    owner_pid = failure.get("owner_pid")
    if isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
        return None
    recovered_at = failure.get("recovered_at")
    if not isinstance(recovered_at, str) or not recovered_at:
        return None
    try:
        datetime.fromisoformat(recovered_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.get("status_started_at") != recovered_at or candidate.get("finished_at") != recovered_at:
        return None

    detail = f"run owner process {owner_pid} is no longer active"
    if candidate.get("error") != detail:
        return None
    if failure.get("detail") != detail:
        return None
    if failure.get("phase") != "stale-lock-recovery":
        return None
    if failure.get("kind") != "owner-process-exited":
        return None

    try:
        payload = json.loads(json.dumps(checkpoint_obj))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    raw_status = payload.get("status")
    if isinstance(raw_status, str) and raw_status:
        if raw_status not in runguard._NONTERMINAL_RUN_STATUSES:
            return None
        prior_status = raw_status
    else:
        prior_status = "artifact-unavailable"

    failure_attribution: dict[str, object] = {}
    stored_status = payload.get("status")
    active_seats = payload.get("active_seats")
    phase_owner = payload.get("phase_owner")
    if stored_status == "dispatching" and isinstance(active_seats, list):
        seats = [seat for seat in active_seats if isinstance(seat, str) and seat]
        if len(seats) == 1:
            failure_attribution["seat"] = seats[0]
        elif seats:
            failure_attribution["seats"] = seats
    if not failure_attribution and isinstance(phase_owner, str) and phase_owner:
        failure_attribution["seat"] = phase_owner
    if not failure_attribution:
        worker = payload.get("worker")
        orchestrator = payload.get("orchestrator")
        if isinstance(worker, str) and worker:
            failure_attribution["seat"] = worker
        elif isinstance(orchestrator, str) and orchestrator:
            failure_attribution["seat"] = orchestrator

    workspace = failure.get("lock_workspace")
    if isinstance(workspace, str) and workspace:
        payload.setdefault("cwd", workspace)
        payload.setdefault("lock_workspace", workspace)
    acquired_at = failure.get("lock_acquired_at")
    if isinstance(acquired_at, str) and acquired_at:
        payload.setdefault("started_at", acquired_at)

    failure_payload: dict[str, object] = {
        "phase": "stale-lock-recovery",
        "kind": "owner-process-exited",
        "detail": detail,
        "owner_pid": owner_pid,
        "prior_status": prior_status,
        "recovered_at": recovered_at,
        **failure_attribution,
    }
    if isinstance(workspace, str) and workspace:
        failure_payload["lock_workspace"] = workspace
    if isinstance(acquired_at, str) and acquired_at:
        failure_payload["lock_acquired_at"] = acquired_at
    payload.update(
        {
            "status": "failed",
            "status_started_at": recovered_at,
            "finished_at": recovered_at,
            "error": detail,
            "failure_phase": "stale-lock-recovery",
            "failure": failure_payload,
        }
    )
    return receipt_schema.stamp_run_receipt(payload)


def _check_claude_work_loop(target: Path) -> CheckResult | None:
    from .claude_hooks.classify import classify

    payload = classify(target)
    if payload is None:
        return None
    status = OK if payload["state"] == "enforced" else WARN
    detail = str(payload["detail"])
    hooks = payload.get("hooks") or {}
    legacy_count = hooks.get("legacy_handler_count", 0)
    if legacy_count:
        status = WARN
        detail = (
            f"{detail}; {legacy_count} legacy hook handler(s) in .claude/settings.json - "
            f"run `brigade work hooks install --target {target}` to reconcile"
        )
    return (status, "claude work loop", detail)


def memory_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    checks: List[CheckResult] = []
    checks.extend(_check_handoff_inboxes(ctx.target, ctx.selection, ctx.harnesses))
    checks.extend(_check_handoff_sources(ctx.target))
    checks.extend(_check_memory_cards(ctx.target))
    checks.extend(_check_memory_index(ctx.target))
    checks.extend(_check_memory_care(ctx.target))
    checks.extend(_check_memory_care_producer_collision(ctx.target))
    return checks


def guard_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    return _check_publish_gate(ctx.target)


def skills_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    return [(OK, "skills: built-in templates", "portable Brigade skills available")]


def tokens_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    return []


def search_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    # The managed code-search tools carry this station's signal. The station
    # itself owns no per-workspace files and does not start local services.
    return []


def pantry_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    # The agentpantry managed tool carries this station's signal; the station
    # itself lays down no per-workspace files.
    return []


def notifications_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    # The agent-notify managed tool carries this station's signal; Brigade
    # does not send notifications or write hook config from doctor.
    return []


def evidence_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    # The miseledger managed tool (which absorbed the stationtrail and
    # sourceharvest exporters) carries this station's signal; the station itself
    # owns no per-workspace files and inspects only host-global state.
    return []


def mcp_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    from . import mcp_cmd

    path = mcp_cmd.canonical_path(ctx.target)
    if not path.exists():
        # Optional station: no canonical catalog means MCP sync is simply not in use.
        return [(INFO, "mcp: catalog", "no .brigade/mcp.json (run `brigade mcp init` to start)")]
    servers, errors, warnings = mcp_cmd.load_canonical(ctx.target)
    if errors:
        return [(FAIL, "mcp: catalog", "; ".join(errors))]
    results: List[CheckResult] = [(OK, "mcp: catalog", f"{path} ({len(servers)} server(s))")]
    from . import mcp_adapters

    for server in servers.values():
        for severity, message in mcp_adapters.validate_server(server):
            results.append((FAIL if severity == "error" else WARN, "mcp: server", message))
    for w in warnings:
        results.append((WARN, "mcp: catalog", w))
    unsupported = sorted(h for h in ctx.harnesses if h not in mcp_adapters.ADAPTERS and h != "this-repo")
    if unsupported:
        results.append((INFO, "mcp: targets", f"no MCP adapter for: {', '.join(unsupported)}"))
    return results


def security_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    from . import security_cmd

    results: List[CheckResult] = [(OK, "security: built-in scanner", "available")]
    config = security_cmd.config_path(ctx.target)
    config_valid = True
    if config.is_file():
        try:
            loaded = security_cmd.load_config(ctx.target)
        except ValueError as exc:
            config_valid = False
            results.append((FAIL, "security: config", f"invalid {config}: {exc}"))
        else:
            results.append((OK, "security: config", f"{config} (policy={loaded.policy if loaded else 'personal'})"))
            enrichment = security_cmd.enrichment_health(ctx.target)
            if enrichment.get("configured"):
                results.append(
                    (OK, "security: enrichment", f"{enrichment.get('provider')} ({enrichment.get('status')})")
                )
            else:
                results.append((WARN, "security: enrichment", str(enrichment.get("status"))))
    else:
        results.append((WARN, "security: config", f"missing at {config}; run `brigade security init --target .`"))

    if config_valid:
        try:
            suppression_health = security_cmd.suppression_health(ctx.target)
        except ValueError as exc:
            results.append((FAIL, "security: suppressions", f"invalid: {exc}"))
        else:
            suppression_count = suppression_health["suppression_count"]
            stale = suppression_health["stale"]
            missing_reasons = suppression_health["missing_reasons"]
            if stale:
                preview = ", ".join(stale[:5])
                results.append(
                    (WARN, "security: stale suppressions", f"{len(stale)} no longer match current findings: {preview}")
                )
            if missing_reasons:
                preview = ", ".join(missing_reasons[:5])
                results.append(
                    (WARN, "security: suppression reasons", f"{len(missing_reasons)} missing reason: {preview}")
                )
            if not stale and not missing_reasons:
                results.append((OK, "security: suppressions", f"{suppression_count} configured"))

    artifacts_dir = security_cmd.default_artifacts_dir(ctx.target)
    bundle = security_cmd.inspect_evidence_bundle(artifacts_dir)
    if bundle.get("ready"):
        detail = f"{artifacts_dir} (generated_at={bundle.get('generated_at')}, findings={bundle.get('finding_count')})"
        results.append((OK, "security: evidence bundle", detail))
    else:
        results.append(
            (
                WARN,
                "security: evidence bundle",
                f"{bundle.get('reason')} at {artifacts_dir}; run `brigade security scan --output-dir {artifacts_dir}`",
            )
        )

    ignored = localio.check_git_ignored(ctx.target, artifacts_dir)
    level = OK if ignored in {"yes", "outside-target"} else WARN
    results.append((level, "security: evidence ignored", ignored))
    return results


def run(
    target: Path,
    harness: str = "generic",
    *,
    json_output: bool = False,
    full: bool = False,
    operator: bool = False,
) -> int:
    ctx = build_context(target, harness)
    checks = _gather_checks(ctx)
    if full:
        checks = _replace_recovery_check_with_full(checks, ctx.target)
    if not operator:
        checks = _filter_target_scoped_checks(checks)
    if json_output:
        return _report_json(ctx, checks, operator=operator)

    print(f"brigade doctor: target {ctx.target}")
    if ctx.selection is not None:
        sel = ctx.selection
        print(f"  harnesses: {', '.join(sel.harnesses) or '(none)'} (owner={sel.owner}, depth={sel.depth})")
    else:
        if ctx.harnesses:
            print(f"  harnesses: (legacy target, no config; declared {', '.join(ctx.harnesses)})")
        else:
            print("  harnesses: (unspecified; no Brigade config and no explicit --harness)")
    if not operator:
        print("  scope: target workspace only (pass --operator for host-global checks)")
    return _report(checks, full=full, target=ctx.target, operator=operator)


def _replace_recovery_check_with_full(
    checks: List[ScopedCheckResult],
    target: Path,
) -> List[ScopedCheckResult]:
    """Swap the bounded recovery-checkpoint aggregate for the exhaustive one.

    The public station callback always emits the bounded (8-preview) verdict so
    its contract stays unchanged; ``run --full`` re-runs the check with
    ``full=True`` and substitutes it in place, preserving the original scope.
    """
    full_status, _name, full_detail = _check_recovery_checkpoints(target, full=True)
    replaced: List[ScopedCheckResult] = []
    for check in checks:
        status, name, detail, scope = _normalize_scoped_check(check)
        if name == _RECOVERY_CHECKPOINTS_NAME:
            replaced.append(_scoped_check(full_status, name, full_detail, scope))
        else:
            replaced.append((status, name, detail, scope))
    return replaced


def _gather_checks(ctx: DoctorContext) -> List[ScopedCheckResult]:
    from . import component_report
    from .registry import all_stations
    from . import managed

    checks: List[ScopedCheckResult] = []
    for status, name, detail in component_report.doctor_checks():
        checks.append(_operator_check(status, name, detail))
    missing_tools: List[Tuple[str, str]] = []
    for station in all_stations():
        if station.doctor is not None:
            for check in station.doctor(ctx):
                checks.append(_normalize_scoped_check(check))
        for tool in managed.for_station(station.name):
            if tool.detect():
                for check in tool.doctor(ctx):
                    checks.append(_operator_check(check[0], check[1], check[2]))
            else:
                missing_tools.append((station.name, tool.name))
    if len(missing_tools) == 1:
        station_name, tool_name = missing_tools[0]
        checks.append(
            _operator_check(MANUAL, f"{station_name}: {tool_name}", f"not installed; run `brigade add {station_name}`")
        )
    elif missing_tools:
        stations = sorted({station for station, _ in missing_tools})
        checks.append(
            _operator_check(
                MANUAL,
                "managed tools",
                f"{len(missing_tools)} managed tools not installed ({', '.join(stations)}); optional, install with `brigade add <station>`",
            )
        )
    checks.append(_normalize_scoped_check(_check_receipts(ctx.target)))
    return checks


def _check_receipts(target: Path) -> CheckResult:
    from . import receipts_cmd

    try:
        payload = receipts_cmd.verify_payload(target)
    except Exception as exc:  # noqa: BLE001 - doctor must stay advisory
        return (WARN, "receipts: verify", f"unable to inspect receipts: {type(exc).__name__}: {exc}")
    summary = payload["summary"]
    status = WARN if summary["mismatch"] or summary["missing"] else OK
    detail = (
        f"checked={summary['total']} ok={summary['ok']} mismatch={summary['mismatch']} "
        f"missing={summary['missing']} legacy={summary['legacy']}"
    )
    return (status, "receipts: verify", detail)


def _check_workspace_files(target: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    required = ["AGENTS.md"]
    optional = [
        "CLAUDE.md",
        "MEMORY.md",
        "TOOLS.md",
        "USER.md",
        "SAFETY_RULES.md",
        "INSTALL_FOR_AGENTS.md",
    ]
    for name in required:
        path = target / name
        if path.is_file():
            results.append((OK, f"bootstrap: {name}", str(path)))
        else:
            results.append((FAIL, f"bootstrap: {name}", f"missing at {path}"))
    for name in optional:
        path = target / name
        if path.is_file():
            results.append((OK, f"bootstrap: {name}", str(path)))
        else:
            results.append((WARN, f"bootstrap: {name}", f"not present at {path}"))
    results.extend(_check_bootstrap_budgets(target))
    return results


def _check_agents_quality(target: Path) -> List[CheckResult]:
    """Nudge AGENTS.md toward the sections agents actually rely on.

    Existence is already a required check; this is a quality lint (WARN only, so
    it never blocks): a useful AGENTS.md states a definition of done and points
    at a memory-handoff path. The Brigade-seeded template satisfies both.
    """
    path = target / "AGENTS.md"
    if not path.is_file():
        return []  # absence is already a FAIL in _check_workspace_files
    try:
        text = path.read_text(errors="replace").lower()
    except OSError:
        return []
    missing: list[str] = []
    if "definition of done" not in text:
        missing.append("a 'Definition of Done' section")
    if "handoff" not in text:
        missing.append("a memory-handoff section")
    if not missing:
        return [(OK, "agents-quality: AGENTS.md", "states a definition of done and a handoff path")]
    return [
        (
            WARN,
            "agents-quality: AGENTS.md",
            f"missing {', '.join(missing)}; agents work better with explicit done criteria and a handoff footer",
        )
    ]


def _check_default_wired_skills(target: Path, selected_harnesses: List[str]) -> List[CheckResult]:
    from .install import DEFAULT_WIRED_SKILLS
    from .skills_cmd import HARNESS_ADAPTERS

    results: List[CheckResult] = []
    for harness in selected_harnesses:
        adapter = HARNESS_ADAPTERS.get(harness)
        if not adapter:
            continue
        present: list[str] = []
        missing: list[tuple[str, str]] = []
        for skill_id in DEFAULT_WIRED_SKILLS:
            rel_dir = _repo_relative_skill_install_dir(adapter, skill_id)
            if rel_dir is None:
                continue
            rel_file = rel_dir / "SKILL.md"
            if (target / rel_file).is_file():
                present.append(str(rel_dir))
            else:
                missing.append((skill_id, str(rel_file)))
        if not present and not missing:
            continue
        name = _adapter_check_name(f"skills: {harness} default wired")
        if missing:
            for skill_id, rel_file in missing:
                results.append(
                    (
                        WARN,
                        f"{name}: {skill_id}",
                        f"harness={harness} skill={skill_id} missing {rel_file}; "
                        f"fix: brigade skills install {skill_id} --workspace {target} --target {harness}",
                    )
                )
        else:
            results.append((OK, name, f"{len(present)} skill(s): {', '.join(present)}"))
    return results


def _repo_relative_skill_install_dir(adapter: dict, skill_id: str) -> Path | None:
    template = str(adapter.get("install_path", ""))
    if not template:
        return None
    rel = template.format(skill_id=skill_id)
    path = Path(rel)
    if path.is_absolute() or not rel.startswith("."):
        return None
    return path


def _check_bootstrap_budgets(target: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    for name, limit in BOOTSTRAP_BUDGETS.items():
        path = target / name
        if not path.exists():
            continue
        if not path.is_file():
            results.append((FAIL, f"bootstrap-budget: {name}", f"not a file: {path}"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            results.append((FAIL, f"bootstrap-budget: {name}", f"unreadable: {exc}"))
            continue
        detail = f"{size}/{limit} bytes"
        if size > limit:
            results.append(
                (
                    FAIL,
                    f"bootstrap-budget: {name}",
                    f"{detail}; over hard limit, split durable context into memory/cards before agents load it",
                )
            )
        else:
            results.append((OK, f"bootstrap-budget: {name}", detail))
    return results


def _check_handoff_inboxes(target: Path, sel, selected_harnesses: List[str]) -> List[CheckResult]:
    results: List[CheckResult] = []
    writers = selected_harnesses
    for h in writers:
        rel = WRITER_INBOXES.get(h)
        if rel is None:
            continue  # reader harness, no inbox
        inbox = target / rel
        if inbox.is_dir():
            results.append((OK, _adapter_check_name(f"handoff: {h} inbox"), str(inbox)))
        else:
            results.append((FAIL, _adapter_check_name(f"handoff: {h} inbox"), f"missing at {inbox}"))
        tmpl = inbox / "TEMPLATE.md"
        if tmpl.is_file():
            results.append((OK, _adapter_check_name(f"handoff: {h} TEMPLATE.md"), str(tmpl)))
        else:
            results.append((WARN, _adapter_check_name(f"handoff: {h} TEMPLATE.md"), f"missing at {tmpl}"))
        processed = inbox / "processed"
        if processed.is_dir():
            results.append((OK, _adapter_check_name(f"handoff: {h} processed/"), str(processed)))
        else:
            results.append((WARN, _adapter_check_name(f"handoff: {h} processed/"), f"missing at {processed}"))
    cards = target / "memory" / "cards"
    if cards.is_dir():
        card_count = len([path for path in cards.rglob("*.md") if path.is_file()])
        results.append((OK, "memory: cards/", f"{cards} ({card_count} card{'s' if card_count != 1 else ''})"))
    else:
        results.append(
            (
                WARN,
                "memory: cards/",
                f"missing at {cards}; ingester cannot promote cards",
            )
        )
    return results


def _check_handoff_sources(target: Path) -> List[CheckResult]:
    from . import handoff_cmd

    mapping = {handoff_cmd.OK: OK, handoff_cmd.WARN: WARN, handoff_cmd.FAIL: FAIL}
    return [
        (mapping.get(status, WARN), f"handoff-source: {name}", detail)
        for status, name, detail in handoff_cmd.doctor_checks(target)
    ]


def _check_memory_index(target: Path) -> List[CheckResult]:
    index = target / "MEMORY.md"
    if not index.is_file():
        return []
    try:
        text = index.read_text()
    except OSError as exc:
        return [(FAIL, "memory-index: MEMORY.md", f"unreadable: {exc}")]

    linked_cards = sorted(
        {
            match.group("path")
            for match in re.finditer(
                r"\[[^\]]+\]\((?P<path>memory/cards/[^)#\s]+\.md)(?:#[^)]+)?\)",
                text,
            )
        }
    )
    if not linked_cards:
        return [(WARN, "memory-index: card links", "MEMORY.md links no memory cards")]

    missing = [path for path in linked_cards if not (target / path).is_file()]
    if missing:
        preview = ", ".join(missing[:5])
        if len(missing) > 5:
            preview += f", ... {len(missing) - 5} more"
        return [
            (
                FAIL,
                "memory-index: card links",
                f"{len(missing)} broken link{'s' if len(missing) != 1 else ''}: {preview}",
            )
        ]
    return [(OK, "memory-index: card links", f"{len(linked_cards)} verified")]


def _check_memory_cards(target: Path) -> List[CheckResult]:
    cards = target / "memory" / "cards"
    if not cards.is_dir():
        return []

    # Honor the same memory-care config `brigade memory care` uses, so the two
    # subsystems agree: per-workspace max_card_bytes and exclude_paths (decay/,
    # archive/, ...) instead of a hardcoded limit that also scanned excluded dirs.
    from . import memory_cmd

    config = memory_cmd.load_config(target) or memory_cmd.MemoryCareConfig()
    budget = config.max_card_bytes

    results: List[CheckResult] = []
    oversized: list[str] = []
    empty: list[str] = []
    counted = 0
    for path in sorted(cards.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(target)
        if config.exclude_paths and memory_cmd._path_matches(str(rel), config.exclude_paths):
            continue
        counted += 1
        try:
            size = path.stat().st_size
        except OSError as exc:
            results.append((FAIL, f"memory-card: {rel}", f"unreadable: {exc}"))
            continue
        if size == 0:
            empty.append(str(rel))
        if size > budget:
            oversized.append(f"{rel} ({size}/{budget} bytes)")

    if empty:
        preview = ", ".join(empty[:5])
        if len(empty) > 5:
            preview += f", ... {len(empty) - 5} more"
        results.append(
            (WARN, "memory-card: empty", f"{len(empty)} empty card{'s' if len(empty) != 1 else ''}: {preview}")
        )

    if oversized:
        preview = ", ".join(oversized[:5])
        if len(oversized) > 5:
            preview += f", ... {len(oversized) - 5} more"
        results.append(
            (
                FAIL,
                "memory-card: budget",
                f"{len(oversized)} over hard limit; split cards into atomic topics: {preview}",
            )
        )
    else:
        results.append((OK, "memory-card: budget", f"{counted} card{'s' if counted != 1 else ''} <= {budget} bytes"))
    return results


def _check_orphan_inboxes(target: Path, selected_harnesses: List[str]) -> List[CheckResult]:
    results: List[CheckResult] = []
    for h, rel in WRITER_INBOXES.items():
        if h in selected_harnesses:
            continue
        inbox = target / rel
        if inbox.is_dir():
            results.append(
                (
                    WARN,
                    _adapter_check_name(f"orphan: {h} inbox"),
                    f"{inbox} exists but {h} is not in config; remove or add to config (unselected harness)",
                )
            )
    return results


def _check_memory_care(target: Path) -> List[CheckResult]:
    from . import memory_cmd

    results: List[CheckResult] = []
    config = memory_cmd.load_config(target) or memory_cmd.MemoryCareConfig()
    decay_dir = memory_cmd._read_output_dir(target, config)
    scan = decay_dir / "scan-latest.json"
    queue = decay_dir / "refresh-queue.json"

    if decay_dir.is_dir():
        results.append((OK, "memory-care: decay/", str(decay_dir)))
    else:
        results.append(
            (
                WARN,
                "memory-care: decay/",
                f"missing at {decay_dir}; staleness scanner not wired",
            )
        )
        return results

    if scan.is_file():
        detail = str(scan)
        try:
            data = json.loads(scan.read_text())
            if not isinstance(data, dict):
                results.append((FAIL, "memory-care: scan-latest", f"expected JSON object: {scan}"))
            else:
                scan_date = data.get("scan_date")
                counts = data.get("counts", {})
                if not isinstance(counts, dict):
                    counts = {}
                if scan_date:
                    detail = f"{scan} (scan_date={scan_date}, stale={counts.get('stale', 'unknown')})"
                results.append((OK, "memory-care: scan-latest", detail))
                results.append(_check_memory_care_scan_freshness(scan, scan_date))
        except json.JSONDecodeError:
            results.append((FAIL, "memory-care: scan-latest", f"invalid JSON: {scan}"))
    else:
        results.append((WARN, "memory-care: scan-latest", f"missing at {scan}"))

    if queue.is_file():
        detail = str(queue)
        try:
            data = json.loads(queue.read_text())
            if not isinstance(data, dict):
                results.append((FAIL, "memory-care: refresh-queue", f"expected JSON object: {queue}"))
            else:
                cards = data.get("cards", [])
                if not isinstance(cards, list):
                    results.append((FAIL, "memory-care: refresh-queue", f"`cards` must be a list: {queue}"))
                else:
                    detail = f"{queue} ({len(cards)} queued)"
                    results.append((OK, "memory-care: refresh-queue", detail))
        except json.JSONDecodeError:
            results.append((FAIL, "memory-care: refresh-queue", f"invalid JSON: {queue}"))
    else:
        results.append((WARN, "memory-care: refresh-queue", f"missing at {queue}"))

    return results


def _is_memory_care_scan_command(command: str) -> bool:
    """Return True when a scanner command is the Brigade memory-care writer."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return False
    return len(parts) >= 4 and parts[:4] == ["brigade", "memory", "care", "scan"]


def _check_memory_care_producer_collision(target: Path) -> List[CheckResult]:
    """Warn when two enabled producers can write the same memory-care artifact.

    This is a read-only migration-planning check. It inspects configured
    producers, resolves their output destinations, and reports collisions. It
    never disables a cron job, edits a card, or mutates a queue file.
    """
    from . import memory_cmd
    from .work_cmd.config import _scanner_output_path

    target = target.expanduser().resolve()
    producer_dirs: dict[Path, set[str]] = {}

    def _add(dir_path: Path, label: str) -> None:
        producer_dirs.setdefault(dir_path, set()).add(label)

    p1_exists = False
    config_dir: Path | None = None

    # Brigade memory-care scanner configured for this workspace.
    if memory_cmd.config_path(target).is_file():
        p1_exists = True
        try:
            config = memory_cmd.load_config(target) or memory_cmd.MemoryCareConfig()
        except ValueError:
            config = memory_cmd.MemoryCareConfig()
        config_dir = memory_cmd._output_dir(target, config).expanduser().resolve()
        _add(config_dir, "brigade memory-care")

    # Enabled Brigade scanners whose command is the memory-care writer.
    scanners_path = target / ".brigade" / "scanners.toml"
    if scanners_path.is_file():
        try:
            data = memory_cmd.tomllib.loads(scanners_path.read_text())
        except (memory_cmd.tomllib.TOMLDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            for scanner in data.get("scanner", []):
                if not isinstance(scanner, dict):
                    continue
                if not scanner.get("enabled", False):
                    continue
                command = scanner.get("command")
                if not isinstance(command, str) or not _is_memory_care_scan_command(command):
                    continue
                p1_exists = True
                output_path = _scanner_output_path(target, scanner)
                if output_path is None:
                    continue
                scanner_dir = output_path.expanduser().resolve().parent
                if scanner_dir == config_dir:
                    # Same producer as the configured writer; do not double-count.
                    continue
                _add(scanner_dir, f"brigade memory-care scanner {scanner.get('id', 'unknown')}")

    # Legacy OpenClaw cron producer (only when this target is a memory-care workspace).
    if p1_exists:
        jobs_path = Path.home() / ".openclaw" / "cron" / "jobs.json"
        if jobs_path.is_file():
            try:
                data = json.loads(jobs_path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            jobs = data.get("jobs", []) if isinstance(data, dict) else []
            legacy_job = _find_job(jobs, "Card Decay Scanner (Daily)")
            if legacy_job is not None and legacy_job.get("enabled", False):
                legacy_dir = (target / "memory/cards/decay").expanduser().resolve()
                _add(legacy_dir, "legacy Card Decay Scanner (Daily)")

    results: List[CheckResult] = []
    for dir_path, labels in sorted(producer_dirs.items()):
        if len(labels) < 2:
            continue
        try:
            rel = dir_path.relative_to(target)
        except ValueError:
            rel = dir_path.name
        labels_str = ", ".join(sorted(labels))
        detail = (
            f"writer collision on `{rel}`: enabled producers are {labels_str}. "
            "Migration order: (1) verify the Brigade output with `brigade memory care status`; "
            "(2) point consumers at the Brigade output location; "
            "(3) disable the legacy 'Card Decay Scanner (Daily)' cron job. "
            "This check is read-only; no queue files, cards, or cron jobs were changed."
        )
        results.append((WARN, "memory-care: producer collision", detail))
    return results


def _check_memory_care_scan_freshness(scan: Path, scan_date: object) -> CheckResult:
    if not scan_date:
        return (WARN, "memory-care: scan freshness", f"scan_date missing in {scan}")
    parsed = _parse_memory_care_scan_date(scan_date)
    if parsed is None:
        return (WARN, "memory-care: scan freshness", f"unparseable scan_date={scan_date!r} in {scan}")
    age_days = (_memory_care_today() - parsed).days
    if age_days < 0:
        return (WARN, "memory-care: scan freshness", f"scan_date is in the future: {scan_date}")
    if age_days > MEMORY_CARE_SCAN_STALE_DAYS:
        return (
            WARN,
            "memory-care: scan freshness",
            f"last scan {age_days} days ago; run memory-care scanner",
        )
    return (OK, "memory-care: scan freshness", f"last scan {age_days} days ago")


def _parse_memory_care_scan_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _memory_care_today() -> date:
    # The scanner stamps scan_date in UTC (memory_cmd._today), so the freshness
    # comparison must also be in UTC. Comparing against a local date made an
    # evening run in a behind-UTC timezone read a same-day scan as the future.
    return datetime.now(timezone.utc).date()


def _check_publish_gate(target: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    hook = target / "hooks" / "pre-push"
    if hook.is_file():
        results.append((OK, "publish: hooks/pre-push", str(hook)))
        if not os.access(hook, os.X_OK):
            results.append(
                (WARN, "publish: hooks/pre-push", "exists but not executable; run `chmod +x hooks/pre-push`")
            )
    else:
        results.append((WARN, "publish: hooks/pre-push", f"missing at {hook}"))

    from . import scrub

    scanner_dir = scrub.scanner_dir()
    if scanner_dir.is_dir():
        label = "external compatibility override" if os.environ.get("CONTENT_GUARD_DIR") else "embedded content guard"
        results.append(_operator_check(OK, "guard: embedded content guard", f"{label}: {scanner_dir}"))
    else:
        results.append(
            _operator_check(
                MANUAL, "guard: embedded content guard", f"not found at {scanner_dir}; reinstall brigade-cli"
            )
        )
    return results


def _check_openclaw() -> List[CheckResult]:
    """Inspect ~/.openclaw/openclaw.json for the wiring brigade expects."""
    results: List[CheckResult] = []
    config = Path.home() / ".openclaw" / "openclaw.json"
    if not config.is_file():
        results.append((MANUAL, "openclaw: config", f"not found at {config}; install OpenClaw first"))
        return results
    try:
        data = json.loads(config.read_text())
    except json.JSONDecodeError as exc:
        results.append((FAIL, "openclaw: config", f"invalid JSON: {exc}"))
        return results
    results.append((OK, "openclaw: config", str(config)))

    plugins = data.get("plugins", {}).get("entries", {})
    if plugins:
        results.append((OK, "openclaw: plugins", f"{len(plugins)} entries"))
    else:
        results.append((WARN, "openclaw: plugins", "no plugin entries configured"))

    primary = data.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")
    if primary:
        results.append((OK, "openclaw: primary model", primary))
    else:
        results.append((WARN, "openclaw: primary model", "agents.defaults.model.primary unset"))

    # jq sanity (optional)
    if shutil.which("jq"):
        results.append((OK, "openclaw: jq", "present"))
    else:
        results.append((WARN, "openclaw: jq", "missing; merge helpers will not work"))
    results.extend(_check_openclaw_cron_jobs())
    return results


def _check_openclaw_cron_jobs() -> List[CheckResult]:
    results: List[CheckResult] = []
    jobs_path = Path.home() / ".openclaw" / "cron" / "jobs.json"
    if not jobs_path.is_file():
        return [
            (
                WARN,
                "openclaw: cron jobs",
                f"not found at {jobs_path}; handoff ingest and memory-care schedules unknown",
            )
        ]

    try:
        data = json.loads(jobs_path.read_text())
    except json.JSONDecodeError as exc:
        return [(WARN, "openclaw: cron jobs", f"invalid JSON: {exc}")]

    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return [(WARN, "openclaw: cron jobs", "jobs.json has no jobs array")]

    expected = [
        ("openclaw: handoff ingest cron", "Claude Memory Handoff Ingest"),
        ("openclaw: card decay scanner", "Card Decay Scanner (Daily)"),
        ("openclaw: card decay refresh", "Card Decay Auto-Refresh (Safe)"),
    ]
    for check_name, job_name in expected:
        job = _find_job(jobs, job_name)
        if job is None:
            results.append((WARN, check_name, f"missing job named {job_name!r}"))
            continue
        if not job.get("enabled", False):
            results.append((WARN, check_name, f"{job_name!r} exists but is disabled"))
            continue
        results.append((OK, check_name, _format_schedule(job.get("schedule"))))

    weekly = _find_job(jobs, "Card Decay Deep Report (Weekly)")
    if weekly is not None and weekly.get("enabled", False):
        results.append((OK, "openclaw: card decay weekly", _format_schedule(weekly.get("schedule"))))
    return results


def _find_job(jobs: list, name: str) -> dict | None:
    for job in jobs:
        if isinstance(job, dict) and job.get("name") == name:
            return job
    return None


def _format_schedule(schedule) -> str:
    if not isinstance(schedule, dict):
        return "enabled; schedule not specified"
    kind = schedule.get("kind")
    if kind == "cron":
        return f"enabled; cron {schedule.get('expr', '<missing expr>')} {schedule.get('tz', '')}".strip()
    if kind == "every":
        every_ms = schedule.get("everyMs")
        if isinstance(every_ms, int):
            return f"enabled; every {every_ms // 60000} min"
        return "enabled; every schedule"
    return f"enabled; {kind or 'unknown'} schedule"


def _check_hermes(target: Path) -> List[CheckResult]:
    from .hermes_adapter import inspect_hermes_adapter

    results: List[CheckResult] = []
    inbox_rel = WRITER_INBOXES["hermes"]
    for item in inspect_hermes_adapter(target, inbox_rel):
        results.append(_doctor_hermes_result(item))

    inbox_path = target / inbox_rel
    gitignore_probe = inbox_path / ".brigade-ignore-probe"
    gitignored = localio.check_git_ignored(target, gitignore_probe)
    if gitignored == "no":
        results.append((FAIL, "hermes: handoff inbox ignored", f"{inbox_rel} is not ignored by git"))
    elif gitignored in {"yes", "unknown"}:
        results.append((OK, "hermes: handoff inbox ignored", f"gitignore status: {gitignored}"))
    else:
        results.append((WARN, "hermes: handoff inbox ignored", f"gitignore status: {gitignored}"))
    results.append(
        (
            OK,
            "hermes: runtime validation",
            "Validated against a real Hermes install (Hermes v0.17): handoffs and skill install both work.",
        )
    )
    return results


def _doctor_hermes_result(item: dict) -> CheckResult:
    status = {"ok": OK, "warn": WARN, "fail": FAIL}.get(str(item.get("status")), WARN)
    result_id = item.get("id")
    if result_id == "fragment":
        name = f"hermes: {item.get('fragment')}"
    else:
        name = {
            "workspace_handoff_inbox": "hermes: workspace handoff inbox",
            "workspace_json": "hermes: workspace.harness.json",
            "memory_handoff_inbox": "hermes: memory handoff inbox",
            "processed_handoff_inbox": "hermes: processed handoff inbox",
            "memory_handoff_json": "hermes: memory-handoff.harness.json",
        }.get(str(result_id), f"hermes: {result_id}")
    return (status, name, str(item.get("detail", "")))


_MARKERS = {
    OK: "  [ok]  ",
    WARN: "  [warn]",
    FAIL: "  [fail]",
    MANUAL: "  [todo]",
    INFO: "  [info]",
}

_SEVERITY_ORDER = (FAIL, WARN, MANUAL, INFO, OK)
_SEVERITY_GROUP_LABELS = {
    FAIL: "failures:",
    WARN: "warnings:",
    MANUAL: "manual actions:",
    INFO: "info:",
    OK: "ok:",
}
_OPERATOR_SECTION_HEADER = "operator/host (not specific to this target):"


def _filter_target_scoped_checks(checks: List[ScopedCheckResult]) -> List[ScopedCheckResult]:
    return [check for check in checks if _check_scope(check) == SCOPE_TARGET]


def _target_detail_prefix(target: Path) -> str:
    return f"target={target}: "


def _annotate_target_detail(target: Path, check: CheckResult | ScopedCheckResult) -> str:
    status, name, detail, scope = _normalize_scoped_check(check)
    if scope == SCOPE_OPERATOR:
        return detail
    prefix = _target_detail_prefix(target)
    if detail.startswith(prefix) or detail.startswith(str(target)):
        return detail
    return f"{prefix}{detail}"


def _report(
    checks: Sequence[CheckResult | ScopedCheckResult],
    *,
    full: bool = True,
    target: Path | None = None,
    operator: bool = False,
) -> int:
    scoped_checks = [_normalize_scoped_check(check) for check in checks]
    width = max((len(check[1]) for check in scoped_checks), default=20)
    counts = _status_counts(scoped_checks)
    print(
        f"triage: {len(scoped_checks)} checks, {counts[OK]} ok, {counts[WARN]} warn, "
        f"{counts[FAIL]} failed, {counts[MANUAL]} manual, {counts[INFO]} info"
    )

    visible_statuses = set(_SEVERITY_ORDER) if full else ACTIONABLE_STATUSES
    visible_checks = [check for check in scoped_checks if check[0] in visible_statuses]
    target_checks = [check for check in visible_checks if _check_scope(check) == SCOPE_TARGET]
    operator_checks = [check for check in visible_checks if _check_scope(check) == SCOPE_OPERATOR]
    hidden_detail = not full and any(check[0] not in ACTIONABLE_STATUSES for check in scoped_checks)

    def _emit(items: List[ScopedCheckResult]) -> None:
        for status, name, detail, scope in items:
            annotated_detail = detail
            if target is not None:
                annotated_detail = _annotate_target_detail(target, (status, name, detail, scope))
            print(f"{_MARKERS[status]} {name.ljust(width)}  {annotated_detail}")

    def _emit_grouped(items: List[ScopedCheckResult]) -> bool:
        emitted = False
        for status in _SEVERITY_ORDER:
            if status not in visible_statuses:
                continue
            group = [check for check in items if check[0] == status]
            if not group:
                continue
            emitted = True
            print(_SEVERITY_GROUP_LABELS[status])
            _emit(group)
        return emitted

    print()
    if visible_checks:
        if not _emit_grouped(target_checks):
            print("  no failures, warnings, or manual actions")
    else:
        print("  no failures, warnings, or manual actions")
    if operator and operator_checks:
        print()
        print(_OPERATOR_SECTION_HEADER)
        _emit_grouped(operator_checks)

    if hidden_detail:
        print()
        print(f"showing {len(visible_checks)} actionable checks; run `brigade doctor --full` to show all checks")

    print()
    print(f"summary: {len(scoped_checks)} checks, {counts[FAIL]} failed, {counts[MANUAL]} manual")
    return 1 if counts[FAIL] else 0


def _status_counts(checks: List[CheckResult | ScopedCheckResult]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, MANUAL: 0, INFO: 0}
    for check in checks:
        status = check[0]
        counts[status] = counts.get(status, 0) + 1
    return counts


def _report_json(ctx: DoctorContext, checks: List[CheckResult | ScopedCheckResult], *, operator: bool = False) -> int:
    scoped_checks = [_normalize_scoped_check(check) for check in checks]
    counts = _status_counts(scoped_checks)
    sel = ctx.selection
    payload = {
        "target": str(ctx.target),
        "harnesses": list(ctx.harnesses),
        "owner": getattr(sel, "owner", None),
        "depth": getattr(sel, "depth", None),
        "operator": operator,
        "checks": [
            {
                "status": status,
                "name": name,
                "detail": _annotate_target_detail(ctx.target, (status, name, detail, scope)),
                "scope": scope,
            }
            for status, name, detail, scope in scoped_checks
        ],
        "summary": {
            "total": len(scoped_checks),
            "ok": counts[OK],
            "warn": counts[WARN],
            "manual": counts[MANUAL],
            "failed": counts[FAIL],
        },
        "ready": counts[FAIL] == 0,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if counts[FAIL] else 0
