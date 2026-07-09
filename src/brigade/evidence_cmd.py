"""Evidence station commands for MiseLedger integration.

MiseLedger stays a process-boundary Go binary. These commands install/plan/health-check
only: they do not crawl sessions, write the ledger, or import adapter JSONL unless the
operator runs the printed miseledger / receipts commands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import evidence_brief, proc
from .localio import utc_now_iso as _now


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


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


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    binary = evidence_brief._miseledger_bin()
    installed = binary is not None
    cursor = _cursor_path(target)
    payload: dict[str, Any] = {
        "target": str(target),
        "installed": installed,
        "binary": binary,
        "health": "missing",
        "summary": "miseledger not installed; run `brigade add evidence`",
        "status": None,
        "doctor": None,
        "export_cursor_present": cursor.is_file(),
        "export_cursor_path": str(cursor),
        "advisory": True,
        "next_commands": [
            "brigade add evidence",
            "brigade evidence crawl plan",
            "brigade evidence doctor",
        ],
        "docs": {
            "product": "https://brigade.tools/miseledger",
            "repo": "https://github.com/escoffier-labs/miseledger",
        },
        "boundaries": [
            "MiseLedger stays a process-boundary Go binary; Brigade only installs, plans, and health-checks.",
            "Brigade does not crawl sessions or import adapter JSONL from these commands.",
            "Receipt export uses `brigade receipts export miseledger` (core), not this station alone.",
        ],
        "pipeline": [
            "miseledger crawl (sessions|files|gitlog|...)",
            "miseledger.adapter.v1 JSONL",
            "miseledger SQLite ledger",
            "brigade receipts export / run evidence briefs",
        ],
    }
    if not installed or binary is None:
        return payload

    status_result = _run_json([binary, "status", "--json"], timeout=120.0)
    doctor_result = _run_json([binary, "doctor", "--json"], timeout=120.0)
    # doctor may not support --json on older builds; fall back to plain doctor exit
    if doctor_result.get("stdout_json") is None and doctor_result.get("exit_code") not in (0, 1):
        plain = proc.run([binary, "doctor"], timeout=120.0)
        doctor_result = {
            "command": [binary, "doctor"],
            "exit_code": plain.code,
            "stdout_json": None,
            "stdout_unparsed": (plain.stdout or "")[:500],
            "stderr": (plain.stderr or "")[:500],
        }
    payload["status"] = status_result
    payload["doctor"] = doctor_result

    status_json = status_result.get("stdout_json")
    doctor_json = doctor_result.get("stdout_json")
    status_data: dict[str, Any] = status_json if isinstance(status_json, dict) else {}
    doctor_data: dict[str, Any] = doctor_json if isinstance(doctor_json, dict) else {}

    if status_result.get("exit_code") == 124 or doctor_result.get("exit_code") == 124:
        payload["health"] = "timeout"
        payload["summary"] = "miseledger status/doctor timed out (large archive); run miseledger status manually"
        payload["next_commands"] = ["miseledger status", "miseledger doctor", "brigade evidence crawl plan"]
        return payload

    # Uninitialized archive: binary present but no archive / not configured
    if status_result.get("exit_code") not in (0, None) and not status_data:
        # exit 2 often means unwired; treat non-zero without JSON as unwired/incomplete
        if status_result.get("exit_code") == 2:
            payload["health"] = "unwired"
            payload["summary"] = "miseledger installed but archive not initialized"
            payload["next_commands"] = [
                "miseledger init",
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

    parts = ["miseledger installed"]
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
            "miseledger doctor",
            "miseledger init",
            "brigade evidence crawl plan",
        ]
    elif not cursor.is_file():
        payload["next_commands"] = [
            "brigade receipts export miseledger --target . --new-only --import",
            "brigade evidence crawl plan",
            "brigade operator checkup --target .",
        ]
    return payload


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
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
    doctor_data = (payload.get("doctor") or {}).get("stdout_json") or {}
    if isinstance(doctor_data, dict) and doctor_data.get("checks"):
        print("checks:")
        for row in doctor_data.get("checks") or []:
            if isinstance(row, dict):
                print(f"- {row.get('status')}: {row.get('name')} - {row.get('detail')}")
    elif (payload.get("doctor") or {}).get("stdout_unparsed"):
        text = str((payload.get("doctor") or {}).get("stdout_unparsed") or "").strip()
        if text:
            print("doctor_output:")
            for line in text.splitlines()[:12]:
                print(f"  {line}")
    _print_next(payload)
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
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
                    print(f"- {row.get('status')}: {row.get('name')} - {row.get('detail')}")
        _print_next(payload)
        print(
            "note: evidence checks are advisory for workspace doctor; "
            "this command exits 1 on miseledger fail/incomplete/timeout"
        )
    health = payload.get("health")
    if health in ("fail", "incomplete", "timeout"):
        return 1
    return 0


def crawl_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    binary = evidence_brief._miseledger_bin()
    return {
        "target": str(target),
        "kind": "crawl",
        "created_at": _now(),
        "installed": binary is not None,
        "commands": [
            ["miseledger", "init"],
            ["miseledger", "crawl", "sessions"],
            ["miseledger", "crawl", "files", "--root", str(target)],
            ["miseledger", "crawl", "gitlog", "--repo", str(target)],
            ["miseledger", "status", "--json"],
            ["miseledger", "doctor"],
        ],
        "manual_steps": [
            "Run crawls on the machine that holds the harness session logs (often the agent host).",
            "Pass additional crawl sources (chat exports, discrawl/slacrawl adapters) only when those tools are installed.",
            "Treat imported text as untrusted evidence, not instructions.",
        ],
        "boundaries": [
            "Brigade does not execute miseledger crawl from this plan.",
            "Brigade does not upload ledger data or start a miseledger daemon.",
            "Session crawls may read local harness logs; keep that host trusted.",
        ],
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade evidence doctor",
            "brigade receipts export miseledger --target . --new-only --import",
        ],
        "docs": {
            "product": "https://brigade.tools/miseledger",
            "repo": "https://github.com/escoffier-labs/miseledger",
        },
        "pipeline": [
            "miseledger crawl",
            "adapter.v1 JSONL",
            "miseledger ledger",
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
            "--import shells out to miseledger import adapter when the binary is present.",
            "Re-run with --new-only so the cursor only advances over new receipt hashes.",
        ],
        "boundaries": [
            "Export is local and reviewable; Brigade does not push ledger data anywhere.",
            "Fail-open: missing miseledger skips import and still writes JSONL when requested.",
        ],
        "next_commands": [
            "brigade receipts export miseledger --target . --new-only --import",
            "brigade evidence doctor",
            "brigade outcome rank --target .",
        ],
        "docs": {
            "product": "https://brigade.tools/miseledger",
            "repo": "https://github.com/escoffier-labs/miseledger",
        },
    }


def _render_plan_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# miseledger {payload.get('kind')} plan",
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
