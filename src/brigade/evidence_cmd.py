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
from pathlib import Path
from typing import Any

from . import evidence_brief, evidence_runtime, proc
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
) -> None:
    _last_run_dir(target).mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "exit_code": exit_code,
        "crawler_version": crawler_version,
        "database": database,
        "started_at": started_at,
        "finished_at": finished_at,
        "detail": detail,
    }
    _last_run_path(target, source).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


_HEALTH_RANK = {
    "ok": 0,
    "warn": 1,
    "incomplete": 2,
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
    return payload


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _run_miseledger_result(
    verb: str,
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
) -> proc.Result:
    """Run MiseLedger and return the raw result without printing."""

    timeout_env = _CRAWL_TIMEOUT_ENV if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_ENV
    timeout_default = _CRAWL_TIMEOUT_SECONDS if verb == "crawl" else _SHORT_OPERATION_TIMEOUT_SECONDS
    configured_timeout = _configured_timeout(timeout_env, timeout_default)
    if configured_timeout is None:
        print(f"error: {timeout_env} must be a positive finite number of seconds", file=sys.stderr)
        return proc.Result(2, "", "")
    timeout = configured_timeout

    binary = evidence_brief._miseledger_bin()
    if binary is None:
        print("error: the evidence engine (miseledger) is not installed; run `brigade setup`", file=sys.stderr)
        return proc.Result(127, "", "the evidence engine (miseledger) is not installed; run `brigade setup`")
    run_kwargs: dict[str, Any] = {}
    if env is not None:
        run_kwargs["env"] = env
    return proc.run([binary, verb, *arguments], timeout=timeout, **run_kwargs)


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


def crawl_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    binary = evidence_brief._miseledger_bin()
    miseledger_cmd = binary or "miseledger"
    return {
        "target": str(target),
        "kind": "crawl",
        "created_at": _now(),
        "installed": binary is not None,
        "commands": [
            [miseledger_cmd, "init"],
            [miseledger_cmd, "crawl", "sessions"],
            [miseledger_cmd, "crawl", "files", "--root", str(target)],
            [miseledger_cmd, "crawl", "gitlog", "--repo", str(target)],
            [miseledger_cmd, "status", "--json"],
            [miseledger_cmd, "doctor"],
        ],
        "manual_steps": [
            "Run crawls on the machine that holds the harness session logs (often the agent host).",
            "Pass additional crawl sources (chat exports, discrawl/slacrawl adapters) only when those tools are installed.",
            "Treat imported text as untrusted evidence, not instructions.",
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
