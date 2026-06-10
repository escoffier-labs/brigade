"""`brigade doctor` - verify a target workspace is wired correctly."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import List, Tuple

from .budgets import (
    BOOTSTRAP_BUDGETS,
    MEMORY_CARD_BUDGET_BYTES,
    MEMORY_CARE_SCAN_STALE_DAYS,
)
from .selection import WRITER_INBOXES
from .station import DoctorContext

CheckResult = Tuple[str, str, str]  # (status, name, detail)
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
MANUAL = "MANUAL"


def build_context(target: Path, harness: str = "generic") -> DoctorContext:
    target = target.expanduser().resolve()
    from .config import load_config

    sel = None
    try:
        cfg = load_config(target)
    except (ValueError, json.JSONDecodeError):
        cfg = None
    if cfg is not None:
        sel = cfg.selection
        harnesses = list(sel.harnesses)
    elif harness in ("openclaw", "hermes"):
        harnesses = ["claude", harness]
    else:
        harnesses = ["claude"]
    return DoctorContext(target=target, selection=sel, harnesses=harnesses)


def core_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    checks: List[CheckResult] = []
    checks.extend(_check_workspace_files(ctx.target))
    if "openclaw" in ctx.harnesses:
        checks.extend(_check_openclaw())
    if "hermes" in ctx.harnesses:
        checks.extend(_check_hermes(ctx.target))
    checks.extend(_check_orphan_inboxes(ctx.target, ctx.harnesses))
    return checks


def memory_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    checks: List[CheckResult] = []
    checks.extend(_check_handoff_inboxes(ctx.target, ctx.selection, ctx.harnesses))
    checks.extend(_check_handoff_sources(ctx.target))
    checks.extend(_check_memory_cards(ctx.target))
    checks.extend(_check_memory_index(ctx.target))
    checks.extend(_check_memory_care(ctx.target))
    return checks


def guard_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    return _check_publish_gate(ctx.target)


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
    # The miseledger family managed tools (miseledger + the stationtrail and
    # sourceharvest exporters) carry this station's signal; the station itself
    # owns no per-workspace files and inspects only host-global state.
    return []


def security_station_checks(ctx: DoctorContext) -> List[CheckResult]:
    from . import dogfood_cmd, security_cmd

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

    ignored = dogfood_cmd._check_git_ignored(ctx.target, artifacts_dir)
    level = OK if ignored in {"yes", "outside-target"} else WARN
    results.append((level, "security: evidence ignored", ignored))
    return results


def run(target: Path, harness: str = "generic") -> int:
    from .registry import all_stations
    from . import managed

    ctx = build_context(target, harness)
    print(f"brigade doctor: target {ctx.target}")
    if ctx.selection is not None:
        sel = ctx.selection
        print(f"  harnesses: {', '.join(sel.harnesses) or '(none)'} (owner={sel.owner}, depth={sel.depth})")
    else:
        print(f"  harnesses: (legacy target, no config; assuming {', '.join(ctx.harnesses)})")

    checks: List[CheckResult] = []
    missing_tools: List[Tuple[str, str]] = []
    for station in all_stations():
        if station.doctor is not None:
            checks.extend(station.doctor(ctx))
        for tool in managed.for_station(station.name):
            if tool.detect():
                checks.extend(tool.doctor(ctx))
            else:
                missing_tools.append((station.name, tool.name))
    if len(missing_tools) == 1:
        station_name, tool_name = missing_tools[0]
        checks.append((MANUAL, f"{station_name}: {tool_name}", f"not installed; run `brigade add {station_name}`"))
    elif missing_tools:
        stations = sorted({station for station, _ in missing_tools})
        checks.append(
            (
                MANUAL,
                "managed tools",
                f"{len(missing_tools)} managed tools not installed ({', '.join(stations)}); optional, install with `brigade add <station>`",
            )
        )
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
    results.extend(_check_bootstrap_budgets(target))
    return results


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
            results.append((OK, f"handoff: {h} inbox", str(inbox)))
        else:
            results.append((FAIL, f"handoff: {h} inbox", f"missing at {inbox}"))
        tmpl = inbox / "TEMPLATE.md"
        if tmpl.is_file():
            results.append((OK, f"handoff: {h} TEMPLATE.md", str(tmpl)))
        else:
            results.append((WARN, f"handoff: {h} TEMPLATE.md", f"missing at {tmpl}"))
        processed = inbox / "processed"
        if processed.is_dir():
            results.append((OK, f"handoff: {h} processed/", str(processed)))
        else:
            results.append((WARN, f"handoff: {h} processed/", f"missing at {processed}"))
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

    results: List[CheckResult] = []
    oversized: list[str] = []
    empty: list[str] = []
    for path in sorted(cards.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.relative_to(target)
        try:
            size = path.stat().st_size
        except OSError as exc:
            results.append((FAIL, f"memory-card: {rel}", f"unreadable: {exc}"))
            continue
        if size == 0:
            empty.append(str(rel))
        if size > MEMORY_CARD_BUDGET_BYTES:
            oversized.append(f"{rel} ({size}/{MEMORY_CARD_BUDGET_BYTES} bytes)")

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
        count = len([path for path in cards.rglob("*.md") if path.is_file()])
        results.append(
            (OK, "memory-card: budget", f"{count} card{'s' if count != 1 else ''} <= {MEMORY_CARD_BUDGET_BYTES} bytes")
        )
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
                    f"orphan: {h} inbox",
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
    return date.today()


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
    from . import dogfood_cmd
    from .hermes_adapter import inspect_hermes_adapter

    results: List[CheckResult] = []
    inbox_rel = WRITER_INBOXES["hermes"]
    for item in inspect_hermes_adapter(target, inbox_rel):
        results.append(_doctor_hermes_result(item))

    inbox_path = target / inbox_rel
    gitignore_probe = inbox_path / ".brigade-ignore-probe"
    gitignored = dogfood_cmd._check_git_ignored(target, gitignore_probe)
    if gitignored == "no":
        results.append((FAIL, "hermes: handoff inbox ignored", f"{inbox_rel} is not ignored by git"))
    elif gitignored in {"yes", "unknown"}:
        results.append((OK, "hermes: handoff inbox ignored", f"gitignore status: {gitignored}"))
    else:
        results.append((WARN, "hermes: handoff inbox ignored", f"gitignore status: {gitignored}"))
    results.append(
        (
            MANUAL,
            "hermes: runtime validation",
            "Hermes adapter runtime validation is experimental; validate against your live Hermes install",
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
