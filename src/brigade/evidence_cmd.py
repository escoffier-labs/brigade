"""Evidence station commands for MiseLedger integration.

MiseLedger stays a process-boundary Go binary. Explicit user-invoked
``brigade evidence crawl`` and ``brigade evidence search`` commands relay work to it;
status, doctor, and crawl/export plans remain local checks or review-only output.
Brigade does not start daemons or upload data.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import evidence_brief, evidence_runtime, proc
from . import memory_identity_audit
from .localio import utc_now_iso as _now


_SHORT_OPERATION_TIMEOUT_SECONDS = 30.0
_SHORT_OPERATION_TIMEOUT_ENV = "BRIGADE_EVIDENCE_TIMEOUT_SECONDS"
_CRAWL_TIMEOUT_SECONDS = 900.0
_CRAWL_TIMEOUT_ENV = "BRIGADE_EVIDENCE_CRAWL_TIMEOUT_SECONDS"
_STATUS_TIMEOUT_SECONDS = 120.0
_STATUS_TIMEOUT_ENV = "BRIGADE_EVIDENCE_STATUS_TIMEOUT_SECONDS"
_STATUS_RETRY_COMMAND = f"{_STATUS_TIMEOUT_ENV}=600 brigade evidence status"


def _configured_timeout(environment_variable: str, default: float) -> float | None:
    raw = os.environ.get(environment_variable)
    if raw is None:
        return default
    try:
        timeout = float(raw)
    except ValueError:
        return None
    return timeout if math.isfinite(timeout) and timeout > 0 else None


def _last_run_dir(target: Path) -> Path:
    return target / ".brigade" / "evidence"


def _last_run_path(target: Path, source: str) -> Path:
    return _last_run_dir(target) / f"{source}-last-run.json"


def _read_last_run(target: Path, source: str) -> dict[str, Any] | None:
    path = _last_run_path(target, source)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_last_run(
    target: Path,
    source: str,
    *,
    status: str,
    exit_code: int,
    crawler_version: str | None,
    database: str | None,
    started_at: str,
    finished_at: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> None:
    _last_run_dir(target).mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "crawler_version": crawler_version,
        "database": database,
        "started_at": started_at,
        "finished_at": finished_at,
        "detail": detail,
    }
    if extra:
        for key, value in extra.items():
            if key == "status":
                # Engine scan status is recorded separately; last-run status stays
                # the Brigade classification (ok|partial|fail).
                payload["scan_status"] = value
                continue
            payload[key] = value
    _last_run_path(target, source).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


_HEALTH_RANK = {
    "ok": 0,
    "warn": 1,
    "incomplete": 2,
    "partial": 2,
    "dry_run": 2,
    "unwired": 3,
    "timeout": 4,
    "missing": 5,
    "fail": 6,
}


def _health_rank(health: str | None) -> int:
    if health is None:
        return 0
    return _HEALTH_RANK.get(health, 0)


def _env_override_active(source: str) -> bool:
    """Return True when an explicit crawler override is present for ``source``."""
    if os.environ.get(f"{source.upper()}_CRAWLER_BIN"):
        return True
    if source == "discord" and os.environ.get("DISCRAWL_BIN"):
        return True
    return False


def _enrich_crawler_health(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    """Add per-source crawler health and upgrade the overall health if needed.

    A failed crawler compatibility check or an unhealthy last-run must be
    visible before a clean ``NO_PENDING`` queue state.  The station stays
    advisory in the workspace doctor path; only ``brigade evidence doctor``
    reflects crawler-driven failures in its own exit code.
    """

    target = target.expanduser().resolve()
    crawlers: dict[str, Any] = {}
    worst_health = payload.get("health")
    for source in evidence_runtime.known_sources():
        last_run = _read_last_run(target, source)
        if last_run is None and not _env_override_active(source):
            # No evidence the operator expects this crawler; keep the station
            # advisory and do not probe the host for optional tools.
            continue
        runtime = evidence_runtime.resolve_crawler(source)
        if runtime is None:
            continue
        compat = evidence_runtime.check_compatibility(runtime)
        block = {
            "resolved_path": runtime.resolved_path,
            "version": runtime.version,
            "compatibility": {"state": compat.state, "detail": compat.detail},
            "required_capabilities": runtime.required_capabilities,
            "config_path": compat.config_path,
            "override": runtime.override,
            "latest_run": last_run,
        }
        crawlers[source] = block
        if _health_rank(compat.state) > _health_rank(worst_health):
            worst_health = compat.state
        if last_run is not None:
            last_status = last_run.get("status")
            if _health_rank(last_status) > _health_rank(worst_health):
                worst_health = last_status
    if crawlers:
        payload["crawlers"] = crawlers
        if worst_health != payload.get("health"):
            payload["health"] = worst_health
            if worst_health == "fail":
                payload["summary"] = f"crawler unhealthy; {payload['summary']}"
    return _enrich_memory_projection_health(payload, target)


def _memory_health_from_engine_payloads(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull body-free memory health from embedded status/doctor JSON."""

    for section in ("doctor", "status"):
        block = payload.get(section) or {}
        if not isinstance(block, dict):
            continue
        stdout = block.get("stdout_json")
        if not isinstance(stdout, dict):
            continue
        for key in ("memory_health", "memory"):
            health = evidence_runtime.body_free_memory_health(
                stdout.get(key) if isinstance(stdout.get(key), dict) else None
            )
            if health:
                return health
        # Capability advertisement without a prior scan.
        capability_block = stdout.get("capability")
        if isinstance(capability_block, dict) and isinstance(capability_block.get("memory"), str):
            return evidence_runtime.body_free_memory_health(
                {
                    "capability": capability_block.get("memory"),
                    "engine_version": capability_block.get("engine_version") or stdout.get("engine_version"),
                    "status": "absent",
                }
            )
    return None


def _enrich_memory_projection_health(payload: dict[str, Any], target: Path) -> dict[str, Any]:
    """Surface body-free memory projection health for doctor/status."""

    last_run = _read_last_run(target, evidence_runtime.MEMORY_SOURCE)
    engine_health = _memory_health_from_engine_payloads(payload)
    # Prefer last-run receipt fields when present so a partial crawl cannot be
    # masked by a stale healthy engine snapshot.
    merged: dict[str, Any] = {}
    if engine_health:
        merged.update(engine_health)
    if isinstance(last_run, dict):
        last_health = evidence_runtime.body_free_memory_health(last_run)
        if last_health:
            merged.update(last_health)
        scan_status = evidence_runtime.body_free_memory_health({"status": last_run.get("scan_status")})
        if scan_status:
            merged.update(scan_status)
        merged["latest_run_status"] = last_run.get("status")

    if not merged and last_run is None:
        state = (
            "timed_out"
            if payload.get("health") == "timeout"
            else "missing"
            if payload.get("health") == "missing"
            else "unknown"
        )
        payload["memory_projection"] = {
            "capability": None,
            "engine_version": None,
            "last_completed_scan_id": None,
            "canonical_count": None,
            "live_count": None,
            "hash_divergence": None,
            "unresolved_relations": None,
            "malformed_skipped": None,
            "stale": None,
            "partial": None,
            "status": None,
            "failed": None,
            "healthy": False,
            "state": state,
            "latest_run": None,
        }
        return payload

    capability = merged.get("capability")
    if not isinstance(capability, str):
        capability = None
    status = merged.get("status") if isinstance(merged.get("status"), str) else None
    stale = merged.get("stale") if isinstance(merged.get("stale"), bool) else None
    partial = merged.get("partial") if isinstance(merged.get("partial"), bool) else None
    failed_raw = merged.get("failed")
    failed = int(failed_raw) if isinstance(failed_raw, int) else None
    latest = last_run.get("status") if isinstance(last_run, dict) else None
    engine_ok = payload.get("health") not in ("missing", "fail", "timeout", "incomplete", "unwired")
    if latest in ("fail", "partial"):
        engine_ok = False
    healthy = evidence_runtime.memory_projection_is_healthy(
        capability=capability,
        status=status,
        stale=stale,
        partial=partial,
        failed=failed if failed is not None else (0 if status == "completed" and latest == "ok" else None),
        engine_ok=engine_ok and latest != "fail",
    )
    # A failed/partial/dry-run last-run or missing required capability is never healthy.
    if latest in ("fail", "partial", "dry_run"):
        healthy = False
    if capability and capability != evidence_runtime.MEMORY_PROJECTION_CAPABILITY:
        healthy = False

    if healthy:
        state = "healthy"
    elif payload.get("health") == "timeout" or (isinstance(last_run, dict) and last_run.get("exit_code") == 124):
        state = "timed_out"
    elif latest == "fail":
        state = "failed"
    elif latest in ("partial", "dry_run") or partial is True:
        state = "partial"
    elif stale is True:
        state = "stale"
    elif status in (None, "absent") and last_run is None:
        state = "unknown"
    elif payload.get("health") == "missing":
        state = "missing"
    else:
        state = "unhealthy"

    block = {
        "capability": capability,
        "engine_version": merged.get("engine_version"),
        "last_completed_scan_id": merged.get("last_completed_scan_id"),
        "canonical_count": merged.get("canonical_count"),
        "live_count": merged.get("live_count"),
        "hash_divergence": merged.get("hash_divergence"),
        "unresolved_relations": merged.get("unresolved_relations"),
        "malformed_skipped": merged.get("malformed_skipped"),
        "stale": stale,
        "partial": partial,
        "status": status,
        "failed": failed,
        "healthy": healthy,
        "state": state,
        "latest_run": evidence_runtime.public_memory_latest_run(last_run),
    }
    payload["memory_projection"] = block

    # Absent (never crawled) is informational only. Failed/partial last-runs and
    # non-absent unhealthy engine health must surface on the station health.
    should_affect_overall = last_run is not None or (status is not None and status != "absent")
    if latest in ("fail", "partial", "dry_run"):
        should_affect_overall = True
    if not healthy and merged and should_affect_overall:
        projected = "fail" if latest == "fail" or (isinstance(failed, int) and failed > 0) else "incomplete"
        if latest in ("partial", "dry_run"):
            projected = "incomplete"
        if _health_rank(projected) > _health_rank(payload.get("health")):
            payload["health"] = projected
            payload["summary"] = f"memory projection unhealthy; {payload.get('summary')}"
    return payload


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _run_miseledger_result(
    verb: str,
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    binary: str | None = None,
) -> proc.Result:
    """Run MiseLedger and return the raw result without printing.

    When ``binary`` is provided it is used verbatim (the preflight-resolved
    path). Otherwise fall back to the configured MiseLedger resolver.
    """

    timeout_env = _CRAWL_TIMEOUT_ENV if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_ENV
    timeout_default = _CRAWL_TIMEOUT_SECONDS if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_SECONDS
    configured_timeout = _configured_timeout(timeout_env, timeout_default)
    if configured_timeout is None:
        print(f"error: {timeout_env} must be a positive finite number of seconds", file=sys.stderr)
        return proc.Result(2, "", "")
    timeout = configured_timeout

    resolved = binary if binary is not None else evidence_brief._miseledger_bin()
    if resolved is None:
        print("error: the evidence engine (miseledger) is not installed; run `brigade setup`", file=sys.stderr)
        return proc.Result(127, "", "the evidence engine (miseledger) is not installed; run `brigade setup`")
    run_kwargs: dict[str, Any] = {}
    if env is not None:
        run_kwargs["env"] = env
    return proc.run([resolved, verb, *arguments], timeout=timeout, **run_kwargs)


def _run_miseledger(verb: str, arguments: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run MiseLedger, relaying output and returning the exit code."""

    result = _run_miseledger_result(verb, arguments, env=env)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.code == 124:
        timeout_env = _CRAWL_TIMEOUT_ENV if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_ENV
        timeout_default = _CRAWL_TIMEOUT_SECONDS if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_SECONDS
        current = _configured_timeout(timeout_env, timeout_default) or timeout_default
        suggested = int(max(current * 2, 300))
        if result.stderr and not result.stderr.endswith("\n"):
            print(file=sys.stderr)
        print(
            f"hint: large archive? retry with a longer timeout: {timeout_env}={suggested} brigade evidence {verb} ...",
            file=sys.stderr,
        )
    return result.code


def _run_memory_crawl(
    arguments: list[str],
    *,
    env: dict[str, str],
    started_at: str,
) -> int:
    """Preflight MiseLedger memory projection, then delegate crawl storage."""

    if len(arguments) < 2:
        print(
            "error: usage: brigade evidence crawl memory <workspace> [--json] [--dry-run] [--limit N]",
            file=sys.stderr,
        )
        return 2

    workspace = Path(arguments[1]).expanduser().resolve()
    rest = list(arguments[2:])
    target = workspace
    option_error, want_json, dry_run, passthrough = evidence_runtime.parse_memory_crawl_options(rest)
    if option_error is not None:
        print(f"error: evidence crawl refused for memory: {option_error}", file=sys.stderr)
        _write_last_run(
            target=target,
            source=evidence_runtime.MEMORY_SOURCE,
            status="fail",
            exit_code=2,
            crawler_version=None,
            database=None,
            started_at=started_at,
            finished_at=_now(),
            detail=option_error,
            extra={
                "required_capability": evidence_runtime.MEMORY_PROJECTION_CAPABILITY,
                "preflight": "refused",
            },
        )
        return 2

    binary = evidence_brief._miseledger_bin()
    probe = evidence_runtime.probe_memory_projection(binary, env=env)
    compat = evidence_runtime.check_memory_projection_preflight(probe)
    if compat.state == "fail":
        detail = compat.detail
        print(f"error: evidence crawl refused for memory: {detail}", file=sys.stderr)
        _write_last_run(
            target=target,
            source=evidence_runtime.MEMORY_SOURCE,
            status="fail",
            exit_code=1,
            crawler_version=compat.version,
            database=compat.database,
            started_at=started_at,
            finished_at=_now(),
            detail=detail,
            extra={
                "capability": probe.capability,
                "engine_version": probe.engine_version or probe.version,
                "required_capability": evidence_runtime.MEMORY_PROJECTION_CAPABILITY,
                "preflight": "refused",
            },
        )
        return 1

    if compat.resolved_path is None:
        print("error: evidence crawl refused for memory: no executable resolved", file=sys.stderr)
        return 1

    # Invoke the exact binary that passed capability/version preflight.
    crawl_args = ["memory", str(workspace), *passthrough]
    if dry_run:
        crawl_args.append("--dry-run")
    # Always request JSON for fail-closed receipt parsing; still honor caller --json for stdout.
    if "--json" not in crawl_args:
        crawl_args.append("--json")

    result = _run_miseledger_result("crawl", crawl_args, env=env, binary=compat.resolved_path)
    receipt: dict[str, Any] | None = None
    parsed = result.json()
    if isinstance(parsed, dict):
        receipt = parsed

    if result.code != 0:
        run_status = "fail"
        counts = None
        receipt_detail = (result.stderr or "").strip()[:500] or "memory crawl engine exit nonzero"
    else:
        run_status, counts, parsed_detail = evidence_runtime.parse_memory_crawl_receipt(receipt, dry_run=dry_run)
        receipt_detail = parsed_detail or (f"memory crawl {run_status}; scan_id={(counts or {}).get('scan_id')}")

    extra: dict[str, Any] = {
        "capability": (receipt or {}).get("capability") or probe.capability,
        "engine_version": (receipt or {}).get("engine_version") or probe.engine_version or probe.version,
        "required_capability": evidence_runtime.MEMORY_PROJECTION_CAPABILITY,
        "preflight": "ok",
        "resolved_path": compat.resolved_path,
    }
    if dry_run or (isinstance(receipt, dict) and receipt.get("dry_run") is True):
        extra["dry_run"] = True
    # Persist typed counts only when the receipt validated; never fabricate zeros.
    if counts is not None:
        extra.update(counts)
        if isinstance(receipt, dict):
            safe_health = evidence_runtime.body_free_memory_health(receipt)
            if safe_health:
                extra.update(safe_health)
            if receipt.get("status") == "completed" and counts.get("scan_id"):
                extra["last_completed_scan_id"] = counts["scan_id"]
    elif isinstance(receipt, dict) and run_status == "dry_run":
        # Dry-run may expose capability/version/namespace without inventing counts.
        for key in ("capability", "engine_version", "memory_namespace", "canonical_count"):
            if key in receipt and key not in extra:
                extra[key] = receipt[key]

    exit_code = result.code if result.code != 0 else (0 if run_status == "ok" else 1)
    _write_last_run(
        target=target,
        source=evidence_runtime.MEMORY_SOURCE,
        status=run_status,
        exit_code=exit_code,
        crawler_version=extra.get("engine_version"),
        database=compat.database,
        started_at=started_at,
        finished_at=_now(),
        detail=receipt_detail,
        extra=extra,
    )

    if want_json and result.stdout:
        print(result.stdout, end="")
    elif run_status == "dry_run":
        print(f"memory crawl dry-run: non-current; status={run_status}")
    elif counts is not None:
        print(
            "scan={scan_id} created={created} updated={updated} unchanged={unchanged} "
            "removed={removed} skipped={skipped} failed={failed} status={status}".format(
                scan_id=counts.get("scan_id"),
                created=counts["created"],
                updated=counts["updated"],
                unchanged=counts["unchanged"],
                removed=counts["removed"],
                skipped=counts["skipped"],
                failed=counts["failed"],
                status=run_status,
            )
        )
    elif result.stdout and want_json:
        print(result.stdout, end="")
    else:
        print(f"memory crawl {run_status}: {receipt_detail}", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if result.code != 0:
        return result.code
    return 0 if run_status == "ok" else 1


def _run_crawl(arguments: list[str]) -> int:
    """Resolve and health-check the crawler before delegating to MiseLedger.

    The resolved crawler's directory is prepended to PATH so that MiseLedger's
    own discovery lands on the same binary.  A true miseledger ``--crawler``
    pass-through flag does not exist today and is a MiseLedger-side follow-up.
    """

    raw_source = arguments[0] if arguments else None
    # Normalize the source for gating so a differently-cased spelling (e.g.
    # "Discord") cannot bypass the compatibility gate; the original arguments are
    # still passed to MiseLedger unchanged.
    source = raw_source.lower() if raw_source is not None else None
    env = dict(os.environ)
    target = Path.cwd().expanduser().resolve()
    started_at = _now()

    if source is None:
        return _run_miseledger("crawl", arguments)

    if source == evidence_runtime.MEMORY_SOURCE:
        return _run_memory_crawl(arguments, env=env, started_at=started_at)

    runtime = evidence_runtime.resolve_crawler(source, env=env)
    if runtime is None:
        # No crawler contract for this source; delegate directly.
        return _run_miseledger("crawl", arguments)

    compat = evidence_runtime.check_compatibility(runtime, env=env)
    if compat.state == "fail":
        detail = compat.detail
        print(f"error: evidence crawl refused for {source}: {detail}", file=sys.stderr)
        _write_last_run(
            target=target,
            source=source,
            status="fail",
            exit_code=1,
            crawler_version=runtime.version,
            database=compat.database,
            started_at=started_at,
            finished_at=_now(),
            detail=detail,
        )
        return 1

    if runtime.resolved_path is None:
        print(f"error: evidence crawl refused for {source}: no executable resolved", file=sys.stderr)
        return 1

    # Prepend the resolved crawler directory so MiseLedger rediscovers the same
    # binary.  This is a Brigade-side substitute for a miseledger --crawler flag.
    crawler_dir = str(Path(runtime.resolved_path).parent)
    env["PATH"] = crawler_dir + os.pathsep + env.get("PATH", "")

    result = _run_miseledger_result("crawl", arguments, env=env)
    status = "ok" if result.code == 0 else "fail"
    _write_last_run(
        target=target,
        source=source,
        status=status,
        exit_code=result.code,
        crawler_version=runtime.version,
        database=compat.database,
        started_at=started_at,
        finished_at=_now(),
        detail=(result.stderr or "").strip()[:500],
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.code


def run_engine(verb: str, arguments: list[str]) -> int:
    """Run MiseLedger without changing its argv, output, or data contract.

    A ``--code-reference`` JSON value is relayed verbatim to MiseLedger, which
    performs exact code-reference matching before lexical fallback.
    """

    if verb == "crawl":
        return _run_crawl(arguments)
    return _run_miseledger(verb, arguments)


def memory_audit(*, workspaces: list[Path], json_output: bool = False) -> int:
    """Print the read-only memory identity audit without invoking MiseLedger."""

    payload = memory_identity_audit.audit_workspaces(workspaces)
    if json_output:
        _json_print(payload)
        return 0
    summary = payload["summary"]
    print(
        "memory identity audit: "
        f"cards={summary['cards']} explicit_ids={summary['explicit_ids']} "
        f"path_fallbacks={summary['path_fallbacks']} malformed_ids={summary['malformed_ids']} "
        f"collisions={summary['collisions']}"
    )
    readiness = payload["alias_readiness"]
    print(f"alias_readiness: {'ready' if readiness['ready'] else 'blocked'}")
    for reason in readiness["blocking_reasons"]:
        print(f"- {reason}")
    return 0


def _run_json(args: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
    result = proc.run(args, timeout=timeout)
    data = result.json()
    return {
        "command": args,
        "exit_code": result.code,
        "stdout_json": data if isinstance(data, dict) else None,
        "stdout_unparsed": None if isinstance(data, dict) else (result.stdout or "")[:500],
        "stderr": (result.stderr or "")[:500],
    }


def _print_next(payload: dict[str, Any]) -> None:
    next_commands = payload.get("next_commands") or []
    if not next_commands:
        return
    print("next:")
    for command in next_commands:
        print(f"  {command}")


def _cursor_path(target: Path) -> Path:
    return target / ".brigade" / "work" / "miseledger-export-cursor.json"


def _build_status_payload(
    target: Path,
    *,
    include_doctor: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    binary = evidence_brief._miseledger_bin()
    installed = binary is not None
    cursor = _cursor_path(target)
    payload: dict[str, Any] = {
        "target": str(target),
        "installed": installed,
        "binary": binary,
        "health": "missing",
        "summary": "evidence engine not installed; run `brigade setup`",
        "status": None,
        "doctor": None,
        "export_cursor_present": cursor.is_file(),
        "export_cursor_path": str(cursor),
        "advisory": True,
        "next_commands": [
            "brigade setup",
            "brigade evidence crawl plan",
            "brigade evidence doctor",
        ],
        "docs": {
            "product": "https://brigade.tools/evidence-memory",
            "repo": "https://github.com/escoffier-labs/brigade",
        },
        "boundaries": [
            "Explicit user-invoked `brigade evidence crawl` and `brigade evidence search` execute the evidence engine across a process boundary.",
            "Review-only `brigade evidence crawl plan` and `brigade evidence export plan` never execute the engine.",
            "Brigade does not start daemons or upload data; receipt export remains local.",
        ],
        "pipeline": [
            "evidence crawl (sessions|files|gitlog|...)",
            "miseledger.adapter.v1 JSONL",
            "evidence ledger (SQLite)",
            "brigade receipts export / run evidence briefs",
        ],
    }
    if not installed or binary is None:
        return payload

    status_result = _run_json([binary, "status", "--json"], timeout=timeout)
    payload["status"] = status_result
    status_json = status_result.get("stdout_json")
    status_data: dict[str, Any] = status_json if isinstance(status_json, dict) else {}

    if not include_doctor:
        exit_code = status_result.get("exit_code")
        if exit_code == 124:
            payload["health"] = "timeout"
            payload["summary"] = "evidence engine status timed out"
        elif exit_code == 2:
            payload["health"] = "unwired"
            payload["summary"] = "evidence engine installed but archive not initialized"
        elif exit_code == 0 and status_data:
            payload["health"] = "ok"
            item_count = next(
                (
                    status_data[key]
                    for key in ("items", "item_count", "total_items", "count")
                    if isinstance(status_data.get(key), int)
                ),
                None,
            )
            payload["summary"] = "evidence engine status ok" + (
                f", items={item_count}" if item_count is not None else ""
            )
        else:
            payload["health"] = "incomplete"
            payload["summary"] = f"evidence engine status unreadable (exit {exit_code})"
        payload["next_commands"] = [
            _STATUS_RETRY_COMMAND,
            "brigade evidence doctor",
            "brigade receipts export miseledger --target . --new-only --import",
        ]
        return payload

    doctor_result = _run_json([binary, "doctor", "--json"], timeout=timeout)
    # doctor may not support --json on older builds; fall back to plain doctor exit
    if doctor_result.get("stdout_json") is None and doctor_result.get("exit_code") not in (0, 1):
        plain = proc.run([binary, "doctor"], timeout=timeout)
        doctor_result = {
            "command": [binary, "doctor"],
            "exit_code": plain.code,
            "stdout_json": None,
            "stdout_unparsed": (plain.stdout or "")[:500],
            "stderr": (plain.stderr or "")[:500],
        }
    payload["doctor"] = doctor_result

    doctor_json = doctor_result.get("stdout_json")
    doctor_data: dict[str, Any] = doctor_json if isinstance(doctor_json, dict) else {}

    if status_result.get("exit_code") == 124 or doctor_result.get("exit_code") == 124:
        payload["health"] = "timeout"
        payload["summary"] = (
            "evidence engine status/doctor timed out (large archive); "
            f"retry with a longer timeout: {_STATUS_RETRY_COMMAND}"
        )
        payload["next_commands"] = [_STATUS_RETRY_COMMAND, "brigade evidence crawl plan"]
        return payload

    # Uninitialized archive: binary present but no archive / not configured
    if status_result.get("exit_code") not in (0, None) and not status_data:
        # exit 2 often means unwired; treat non-zero without JSON as unwired/incomplete
        if status_result.get("exit_code") == 2:
            payload["health"] = "unwired"
            payload["summary"] = "evidence engine installed but archive not initialized"
            payload["next_commands"] = [
                "brigade evidence crawl plan",
                "brigade evidence doctor",
            ]
            return payload

    item_count = None
    for key in ("items", "item_count", "total_items", "count"):
        if isinstance(status_data.get(key), int):
            item_count = status_data.get(key)
            break
    sources = status_data.get("sources") if isinstance(status_data.get("sources"), (list, int)) else None

    fail_count = int(doctor_data.get("fail_count") or 0) if doctor_data else 0
    warn_count = int(doctor_data.get("warn_count") or 0) if doctor_data else 0
    doctor_exit = int(doctor_result.get("exit_code") or 0)

    if doctor_exit not in (0, 1) and not doctor_data:
        # plain doctor nonzero without JSON
        if doctor_exit != 0:
            fail_count = max(fail_count, 1)

    if fail_count or doctor_exit not in (0, 1):
        # doctor exit 1 may mean warnings only for some tools; prefer fail_count
        if fail_count:
            payload["health"] = "fail"
        elif doctor_exit != 0 and not doctor_data:
            payload["health"] = "fail"
        else:
            payload["health"] = "warn"
    elif warn_count:
        payload["health"] = "warn"
    elif status_result.get("exit_code") == 0:
        payload["health"] = "ok"
    else:
        payload["health"] = "incomplete"

    parts = ["evidence engine installed"]
    if item_count is not None:
        parts.append(f"items={item_count}")
    if isinstance(sources, list):
        parts.append(f"sources={len(sources)}")
    elif isinstance(sources, int):
        parts.append(f"sources={sources}")
    if cursor.is_file():
        parts.append("export_cursor=yes")
    else:
        parts.append("export_cursor=no")
    parts.append(f"doctor={fail_count} fail/{warn_count} warn")
    payload["summary"] = ", ".join(parts)

    payload["next_commands"] = [
        "brigade evidence crawl plan",
        "brigade receipts export miseledger --target . --new-only --import",
        "brigade operator checkup --target .",
    ]
    if payload["health"] in ("fail", "incomplete", "unwired"):
        payload["next_commands"] = [
            "brigade evidence doctor",
            "brigade evidence crawl plan",
        ]
    elif not cursor.is_file():
        payload["next_commands"] = [
            "brigade receipts export miseledger --target . --new-only --import",
            "brigade evidence crawl plan",
            "brigade operator checkup --target .",
        ]
    return payload


def status_payload(
    target: Path,
    *,
    include_doctor: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    target = target.expanduser().resolve()
    payload = _build_status_payload(target, include_doctor=include_doctor, timeout=timeout)
    return _enrich_crawler_health(payload, target)


def _check_label(row: dict[str, Any]) -> str:
    """Human label for one engine doctor check row.

    The engine emits ``{"name", "detail", "ok": bool}`` rows; older builds used a
    ``status`` string. Support both instead of printing ``None``.
    """

    status_value = row.get("status")
    if isinstance(status_value, str) and status_value:
        return status_value
    ok = row.get("ok")
    if isinstance(ok, bool):
        return "OK" if ok else "FAIL"
    return "?"


def _status_timeout_or_error() -> float | None:
    timeout = _configured_timeout(_STATUS_TIMEOUT_ENV, _STATUS_TIMEOUT_SECONDS)
    if timeout is None:
        print(f"error: {_STATUS_TIMEOUT_ENV} must be a positive finite number of seconds", file=sys.stderr)
    return timeout


def status(*, target: Path, json_output: bool = False) -> int:
    timeout = _status_timeout_or_error()
    if timeout is None:
        return 2
    payload = status_payload(target, timeout=timeout)
    if json_output:
        _json_print(payload)
        return 0
    print(f"evidence: {payload['summary']}")
    print(f"health: {payload.get('health') or 'unknown'} (advisory; never fails workspace doctor)")
    print("pipeline: " + " -> ".join(payload.get("pipeline") or []))
    status_data = (payload.get("status") or {}).get("stdout_json") or {}
    if isinstance(status_data, dict) and status_data:
        for key in ("items", "item_count", "sources", "archive", "path", "db"):
            if key in status_data:
                print(f"{key}: {status_data.get(key)}")
    crawlers = payload.get("crawlers")
    if isinstance(crawlers, dict):
        for source, block in crawlers.items():
            compat = block.get("compatibility") or {}
            print(f"crawler/{source}: {compat.get('state')} - {compat.get('detail')}")
    memory_projection = payload.get("memory_projection")
    if isinstance(memory_projection, dict):
        print(
            "memory_projection: "
            f"state={memory_projection.get('state')} "
            f"healthy={memory_projection.get('healthy')} "
            f"capability={memory_projection.get('capability')} "
            f"engine_version={memory_projection.get('engine_version')} "
            f"last_completed={memory_projection.get('last_completed_scan_id')} "
            f"canonical={memory_projection.get('canonical_count')} "
            f"live={memory_projection.get('live_count')} "
            f"hash_divergence={memory_projection.get('hash_divergence')} "
            f"unresolved={memory_projection.get('unresolved_relations')} "
            f"malformed_skipped={memory_projection.get('malformed_skipped')} "
            f"stale={memory_projection.get('stale')} "
            f"partial={memory_projection.get('partial')} "
            f"failed={memory_projection.get('failed')}"
        )
    doctor_data = (payload.get("doctor") or {}).get("stdout_json") or {}
    if isinstance(doctor_data, dict) and doctor_data.get("checks"):
        print("checks:")
        for row in doctor_data.get("checks") or []:
            if isinstance(row, dict):
                print(f"- {_check_label(row)}: {row.get('name')} - {row.get('detail')}")
    elif (payload.get("doctor") or {}).get("stdout_unparsed"):
        text = str((payload.get("doctor") or {}).get("stdout_unparsed") or "").strip()
        if text:
            print("doctor_output:")
            for line in text.splitlines()[:12]:
                print(f"  {line}")
    _print_next(payload)
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    timeout = _status_timeout_or_error()
    if timeout is None:
        return 2
    payload = status_payload(target, timeout=timeout)
    payload["command"] = "evidence doctor"
    if json_output:
        _json_print(payload)
    else:
        print(f"evidence doctor: {payload['summary']}")
        print(f"health: {payload.get('health') or 'unknown'}")
        memory_projection = payload.get("memory_projection")
        if isinstance(memory_projection, dict):
            print(
                "memory_projection: "
                f"healthy={memory_projection.get('healthy')} "
                f"capability={memory_projection.get('capability')} "
                f"engine_version={memory_projection.get('engine_version')} "
                f"last_completed={memory_projection.get('last_completed_scan_id')} "
                f"canonical={memory_projection.get('canonical_count')} "
                f"live={memory_projection.get('live_count')} "
                f"hash_divergence={memory_projection.get('hash_divergence')} "
                f"unresolved={memory_projection.get('unresolved_relations')} "
                f"malformed_skipped={memory_projection.get('malformed_skipped')} "
                f"stale={memory_projection.get('stale')} "
                f"partial={memory_projection.get('partial')} "
                f"failed={memory_projection.get('failed')}"
            )
        doctor_data = (payload.get("doctor") or {}).get("stdout_json") or {}
        if isinstance(doctor_data, dict) and doctor_data.get("checks"):
            print("checks:")
            for row in doctor_data.get("checks") or []:
                if isinstance(row, dict):
                    print(f"- {_check_label(row)}: {row.get('name')} - {row.get('detail')}")
        _print_next(payload)
        print(
            "note: evidence checks are advisory for workspace doctor; "
            "this command exits 1 on engine fail/incomplete/timeout or crawler fail"
        )
    health = payload.get("health")
    if health in ("fail", "incomplete", "timeout"):
        return 1
    return 0


# In-tree miseledger 0.6.0 CLI surface (engines/evidence-ledger/internal/app).
# commandTable names from app.go. Crawl kinds from cmdCrawl in crawl.go.
_MISELEDGER_TOP_LEVEL = frozenset(
    {
        "version",
        "init",
        "status",
        "sources",
        "scans",
        "sessions",
        "serve",
        "mcp",
        "watch",
        "schedule",
        "crawl",
        "adapter",
        "import",
        "search",
        "show",
        "evidence",
        "explain",
        "export",
        "relations",
        "stats",
        "fork",
        "diff",
        "compact",
        "prune",
        "sql",
        "doctor",
        "trust",
    }
)

# Built-in crawl kinds that dispatch without the retired sourceharvest helper.
_MISELEDGER_BUILTIN_CRAWL = frozenset(
    {
        "sessions",
        "memory",
        "adapter",
        "cursor",
        "discord",
        "slack",
        "granola",
        "notion",
        "gmail",
        "github",
        "telegram",
        "chatgpt-export",
        "claude-export",
    }
)

# cmdCrawlSourceHarvest kinds. They still parse, then fail without sourceharvest.
_MISELEDGER_SOURCEHARVEST_CRAWL = frozenset(
    {
        "docs",
        "files",
        "repo",
        "markdown",
        "html",
        "gitlog",
        "json",
        "jsonl",
    }
)

_MISELEDGER_CRAWL_KINDS = _MISELEDGER_BUILTIN_CRAWL | _MISELEDGER_SOURCEHARVEST_CRAWL

_SOURCEHARVEST_OMIT_REASON = "requires the retired sourceharvest helper; omitted from the printed plan"


def _split_miseledger_flags(
    args: Sequence[str],
    *,
    value_flags: frozenset[str],
    bool_flags: frozenset[str],
) -> tuple[dict[str, str], dict[str, bool], list[str]]:
    """Mirror engines/evidence-ledger/internal/app/flags.go splitFlags."""
    values: dict[str, str] = {}
    bools: dict[str, bool] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("--") or arg == "--":
            rest.append(arg)
            i += 1
            continue
        name_val = arg[2:]
        name = name_val
        val = ""
        if "=" in name_val:
            name, val = name_val.split("=", 1)
        if name in value_flags:
            if val == "":
                i += 1
                if i >= len(args):
                    raise ValueError(f"--{name} requires a value")
                val = args[i]
            values[name] = val
            i += 1
            continue
        if name in bool_flags:
            if val != "":
                raise ValueError(f"--{name} does not take a value")
            bools[name] = True
            i += 1
            continue
        raise ValueError(f"unknown flag --{name}")
    return values, bools, rest


def _first_positional(args: Sequence[str]) -> str:
    """Mirror engines/evidence-ledger/internal/app/flags.go firstPositional."""
    known_bool = frozenset({"json", "dry-run", "help", "h"})
    i = 0
    while i < len(args):
        arg = args[i]
        if not arg.startswith("--") or arg == "--":
            return arg
        name_val = arg[2:]
        name = name_val
        has_inline = False
        if "=" in name_val:
            name = name_val.split("=", 1)[0]
            has_inline = True
        if has_inline or name in known_bool:
            i += 1
            continue
        i += 2
    return ""


def miseledger_argv_dispatches(argv: Sequence[str]) -> None:
    """Raise ValueError if argv would not parse/dispatch on in-tree miseledger.

    Dispatch means ``commandTable`` plus ``cmdCrawl`` accept the argv. Kinds
    that shell out to sourceharvest still parse (then fail at runtime).
    Unknown commands, unknown crawl kinds, and rejected flag forms raise.
    """
    if len(argv) < 2:
        raise ValueError("usage: miseledger <command>")
    command = argv[1]
    if command not in _MISELEDGER_TOP_LEVEL:
        raise ValueError(f"unknown command: {command}")
    rest = list(argv[2:])
    if command == "init":
        return
    if command == "status":
        _, _, leftover = _split_miseledger_flags(rest, value_flags=frozenset(), bool_flags=frozenset({"json"}))
        if leftover:
            raise ValueError("usage: miseledger status [--json]")
        return
    if command == "doctor":
        if rest == ["--help"] or rest == ["-h"]:
            return
        if rest and rest[0] == "provenance":
            return
        _, _, leftover = _split_miseledger_flags(
            rest, value_flags=frozenset(), bool_flags=frozenset({"json", "mcp", "archive"})
        )
        if leftover:
            raise ValueError("usage: miseledger doctor [--json] [--mcp] [--archive]")
        return
    if command != "crawl":
        return
    if not rest:
        raise ValueError(
            "usage: miseledger crawl sessions|memory|docs|files|repo|markdown|"
            "html|gitlog|json|jsonl|adapter|cursor|discord|github|slack|granola|"
            "notion|gmail|telegram|chatgpt-export|claude-export <path> [options]"
        )
    kind = rest[0]
    kind_args = rest[1:]
    if kind not in _MISELEDGER_CRAWL_KINDS:
        raise ValueError(f"unknown crawl kind: {kind}")
    if kind in _MISELEDGER_SOURCEHARVEST_CRAWL:
        if _first_positional(kind_args) == "":
            raise ValueError(f"usage: miseledger crawl {kind} <path> [options]")
        return
    if kind == "sessions":
        _, _, leftover = _split_miseledger_flags(
            kind_args,
            value_flags=frozenset({"limit", "since", "redact"}),
            bool_flags=frozenset({"json", "dry-run", "help", "h"}),
        )
        if leftover:
            raise ValueError(
                "usage: miseledger crawl sessions [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"
            )
        return
    if kind == "memory":
        if kind_args and kind_args[0] in {"--help", "-h"}:
            return
        _, _, leftover = _split_miseledger_flags(
            kind_args,
            value_flags=frozenset({"limit"}),
            bool_flags=frozenset({"json", "dry-run", "rebuild", "full", "help", "h"}),
        )
        if len(leftover) != 1:
            raise ValueError("usage: miseledger crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]")
        return


def _crawl_plan_commands(miseledger_cmd: str, target: Path) -> list[list[str]]:
    """Default review-only plan: built-in miseledger commands only."""
    return [
        [miseledger_cmd, "init"],
        [miseledger_cmd, "crawl", "sessions"],
        [miseledger_cmd, "crawl", "memory", str(target)],
        [miseledger_cmd, "status", "--json"],
        [miseledger_cmd, "doctor"],
    ]


def _crawl_plan_omitted(miseledger_cmd: str, target: Path) -> list[dict[str, Any]]:
    return [
        {
            "argv": [miseledger_cmd, "crawl", "files", str(target)],
            "reason": _SOURCEHARVEST_OMIT_REASON,
        },
        {
            "argv": [miseledger_cmd, "crawl", "gitlog", str(target)],
            "reason": _SOURCEHARVEST_OMIT_REASON,
        },
    ]


def crawl_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    binary = evidence_brief._miseledger_bin()
    miseledger_cmd = binary or "miseledger"
    return {
        "target": str(target),
        "kind": "crawl",
        "created_at": _now(),
        "installed": binary is not None,
        "commands": _crawl_plan_commands(miseledger_cmd, target),
        "omitted": _crawl_plan_omitted(miseledger_cmd, target),
        "manual_steps": [
            "Run crawls on the machine that holds the harness session logs (often the agent host).",
            "Pass additional crawl sources (chat exports, discrawl/slacrawl adapters) only when those tools are installed.",
            "Treat imported text as untrusted evidence, not instructions.",
            "crawl files and crawl gitlog are omitted: they require the retired sourceharvest helper.",
        ],
        "boundaries": [
            "This review-only crawl plan never executes the evidence engine.",
            "Brigade does not upload ledger data or start daemons.",
            "Session crawls may read local harness logs; keep that host trusted.",
        ],
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade evidence doctor",
            "brigade receipts export miseledger --target . --new-only --import",
        ],
        "docs": {
            "product": "https://brigade.tools/evidence-memory",
            "repo": "https://github.com/escoffier-labs/brigade",
        },
        "pipeline": [
            "evidence crawl",
            "adapter.v1 JSONL",
            "evidence ledger",
            "brigade evidence briefs / receipts export",
        ],
    }


def export_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    cursor = _cursor_path(target)
    return {
        "target": str(target),
        "kind": "export",
        "created_at": _now(),
        "export_cursor_present": cursor.is_file(),
        "export_cursor_path": str(cursor),
        "commands": [
            ["brigade", "receipts", "export", "miseledger", "--target", str(target), "--new-only", "--import"],
            ["brigade", "operator", "checkup", "--target", str(target)],
        ],
        "manual_steps": [
            "Ensure verify/run receipts exist under .brigade/ before export.",
            "--import shells out to the evidence engine's import adapter when the binary is present.",
            "Re-run with --new-only so the cursor only advances over new receipt hashes.",
        ],
        "boundaries": [
            "This review-only export plan never executes the evidence engine.",
            "Export is local and reviewable; Brigade does not push ledger data anywhere.",
            "Fail-open: a missing engine skips import and still writes JSONL when requested.",
        ],
        "next_commands": [
            "brigade receipts export miseledger --target . --new-only --import",
            "brigade evidence doctor",
            "brigade outcome rank --target .",
        ],
        "docs": {
            "product": "https://brigade.tools/evidence-memory",
            "repo": "https://github.com/escoffier-labs/brigade",
        },
    }


def _render_plan_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# evidence {payload.get('kind')} plan",
        "",
        f"- target: {payload.get('target')}",
    ]
    docs_raw = payload.get("docs")
    docs: dict[str, Any] = docs_raw if isinstance(docs_raw, dict) else {}
    if docs.get("product"):
        lines.append(f"- product: {docs['product']}")
    if docs.get("repo"):
        lines.append(f"- repo: {docs['repo']}")
    if payload.get("pipeline"):
        lines.append(f"- pipeline: {' -> '.join(payload['pipeline'])}")
    lines.extend(["", "## Commands", ""])
    for command in payload.get("commands") or []:
        lines.append("```sh")
        lines.append(" ".join(str(part) for part in command))
        lines.append("```")
        lines.append("")
    omitted = payload.get("omitted") or []
    if omitted:
        lines.extend(["## Omitted", ""])
        for row in omitted:
            if not isinstance(row, dict):
                continue
            argv = row.get("argv") or []
            reason = row.get("reason") or ""
            printed = " ".join(str(part) for part in argv) if argv else "command"
            lines.append(f"- `{printed}` — {reason}")
        lines.append("")
    lines.extend(["## Manual Steps", ""])
    for step in payload.get("manual_steps") or []:
        lines.append(f"- {step}")
    lines.extend(["", "## Boundaries", ""])
    for boundary in payload.get("boundaries") or []:
        lines.append(f"- {boundary}")
    next_commands = payload.get("next_commands") or []
    if next_commands:
        lines.extend(["", "## Next", ""])
        for command in next_commands:
            lines.append(f"- {command}")
    return "\n".join(lines).rstrip() + "\n"


def _write_plan(target: Path, payload: dict[str, Any]) -> dict[str, Any]:
    created = str(payload.get("created_at") or _now())
    stamp = created.replace(":", "").replace("+", "Z").replace(".", "-")
    kind = str(payload.get("kind") or "plan")
    plan_dir = target / ".brigade" / "evidence" / "plans" / f"{stamp}-{kind}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    json_path = plan_dir / "plan.json"
    md_path = plan_dir / "PLAN.md"
    out = dict(payload)
    out["plan_id"] = plan_dir.name
    out["plan_path"] = str(md_path)
    out["receipt_path"] = str(json_path)
    json_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    md_path.write_text(_render_plan_md(out))
    return out


def crawl_plan(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    payload = crawl_plan_payload(target=target)
    if write:
        payload = _write_plan(target, payload)
    if json_output:
        _json_print(payload)
        return 0
    if write:
        print(f"wrote evidence crawl plan: {payload['plan_path']}")
    else:
        print(_render_plan_md(payload), end="")
    return 0


def export_plan(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    payload = export_plan_payload(target=target)
    if write:
        payload = _write_plan(target, payload)
    if json_output:
        _json_print(payload)
        return 0
    if write:
        print(f"wrote evidence export plan: {payload['plan_path']}")
    else:
        print(_render_plan_md(payload), end="")
    return 0


def trust_review(
    *,
    target: Path,
    item_ref: str,
    content_hash: str,
    json_output: bool = False,
) -> int:
    from . import trust_gate

    target = target.expanduser().resolve()
    try:
        payload = trust_gate.review_item_ref(target, item_ref, content_hash)
    except trust_gate.TrustReviewError as exc:
        if json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"item_ref: {payload.get('item_ref')}")
    print(f"status: {payload.get('status')}")
    event = payload.get("event")
    if isinstance(event, dict):
        print(f"to_label: {event.get('to_label')}")
        print(f"envelope_content_hash: {event.get('envelope_content_hash')}")
    return 0
