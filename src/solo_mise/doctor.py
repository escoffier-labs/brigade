"""`solo-mise doctor` — verify a target workspace is wired correctly."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Tuple

CheckResult = Tuple[str, str, str]  # (status, name, detail)
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
MANUAL = "MANUAL"


def run(target: Path, harness: str = "generic") -> int:
    target = target.expanduser().resolve()
    print(f"solo-mise doctor: target {target} ({harness})")
    checks: List[CheckResult] = []

    checks.extend(_check_workspace_files(target))
    checks.extend(_check_handoff_inbox(target))
    checks.extend(_check_memory_care(target))
    checks.extend(_check_publish_gate(target))

    if harness == "openclaw":
        checks.extend(_check_openclaw())
    elif harness == "hermes":
        checks.extend(_check_hermes(target))

    return _report(checks)


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
    return results


def _check_handoff_inbox(target: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    inbox = target / ".claude" / "memory-handoffs"
    template = inbox / "TEMPLATE.md"
    processed = inbox / "processed"

    if inbox.is_dir():
        results.append((OK, "handoff: inbox", str(inbox)))
    else:
        results.append((FAIL, "handoff: inbox", f"missing at {inbox}"))

    if template.is_file():
        results.append((OK, "handoff: TEMPLATE.md", str(template)))
    else:
        results.append((WARN, "handoff: TEMPLATE.md", f"missing at {template}"))

    if processed.is_dir():
        results.append((OK, "handoff: processed/", str(processed)))
    else:
        results.append((WARN, "handoff: processed/", f"missing at {processed}"))

    cards = target / "memory" / "cards"
    if cards.is_dir():
        results.append((OK, "memory: cards/", str(cards)))
    else:
        results.append(
            (WARN, "memory: cards/", f"missing at {cards}; ingester cannot promote cards")
        )
    return results


def _check_memory_care(target: Path) -> List[CheckResult]:
    results: List[CheckResult] = []
    decay_dir = target / "memory" / "cards" / "decay"
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
            scan_date = data.get("scan_date")
            counts = data.get("counts", {})
            if scan_date:
                detail = f"{scan} (scan_date={scan_date}, stale={counts.get('stale', 'unknown')})"
        except json.JSONDecodeError:
            detail = f"invalid JSON: {scan}"
            results.append((WARN, "memory-care: scan-latest", detail))
        else:
            results.append((OK, "memory-care: scan-latest", detail))
    else:
        results.append((WARN, "memory-care: scan-latest", f"missing at {scan}"))

    if queue.is_file():
        detail = str(queue)
        try:
            data = json.loads(queue.read_text())
            cards = data.get("cards", [])
            if isinstance(cards, list):
                detail = f"{queue} ({len(cards)} queued)"
        except json.JSONDecodeError:
            detail = f"invalid JSON: {queue}"
            results.append((WARN, "memory-care: refresh-queue", detail))
        else:
            results.append((OK, "memory-care: refresh-queue", detail))
    else:
        results.append((WARN, "memory-care: refresh-queue", f"missing at {queue}"))

    return results


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

    scanner_dir = Path(os.environ.get("CONTENT_GUARD_DIR", str(Path.home() / "repos" / "content-guard")))
    if scanner_dir.is_dir():
        results.append((OK, "publish: content-guard", str(scanner_dir)))
    else:
        results.append(
            (MANUAL, "publish: content-guard", f"not found at {scanner_dir}; install or set CONTENT_GUARD_DIR")
        )
    return results


def _check_openclaw() -> List[CheckResult]:
    """Inspect ~/.openclaw/openclaw.json for the wiring solo-mise expects."""
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

    primary = (
        data.get("agents", {}).get("defaults", {}).get("model", {}).get("primary")
    )
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
    results: List[CheckResult] = []
    fragments_dir = target / ".solo-mise" / "hermes"
    expected = [
        "workspace.harness.json",
        "memory-handoff.harness.json",
        "model-lanes.harness.json",
    ]
    for name in expected:
        path = fragments_dir / name
        if path.is_file():
            results.append((OK, f"hermes: {name}", str(path)))
        else:
            results.append((WARN, f"hermes: {name}", f"missing at {path}; run `solo-mise hermes-fragments`"))
    results.append(
        (MANUAL, "hermes: install validation", "Hermes adapter is experimental; validate against your install")
    )
    return results


def _report(checks: List[CheckResult]) -> int:
    width = max((len(name) for _, name, _ in checks), default=20)
    failed = 0
    manual = 0
    for status, name, detail in checks:
        marker = {
            OK: "  [ok]  ",
            WARN: "  [warn]",
            FAIL: "  [fail]",
            MANUAL: "  [todo]",
        }[status]
        print(f"{marker} {name.ljust(width)}  {detail}")
        if status == FAIL:
            failed += 1
        elif status == MANUAL:
            manual += 1
    print()
    summary = f"summary: {len(checks)} checks, {failed} failed, {manual} manual"
    print(summary)
    return 1 if failed else 0
