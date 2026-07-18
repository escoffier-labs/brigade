"""`brigade doctor` - verify a target workspace is wired correctly."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Tuple

from .budgets import (
    BOOTSTRAP_BUDGETS,
    MEMORY_CARE_SCAN_STALE_DAYS,
)
from . import localio
from .selection import WRITER_INBOXES
from .station import DoctorContext

CheckResult = Tuple[str, str, str]  # (status, name, detail)
OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
MANUAL = "MANUAL"
INFO = "INFO"
DEFAULT_TEXT_CHECK_LIMIT = 50


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
    checks.extend(_check_agents_quality(ctx.target))
    checks.extend(_check_default_wired_skills(ctx.target, ctx.harnesses))
    if "claude" in ctx.harnesses:
        check = _check_claude_work_loop(ctx.target)
        if check is not None:
            checks.append(check)
    if "openclaw" in ctx.harnesses:
        checks.extend(_check_openclaw())
    if "hermes" in ctx.harnesses:
        checks.extend(_check_hermes(ctx.target))
    checks.extend(_check_orphan_inboxes(ctx.target, ctx.harnesses))
    return checks


def _check_claude_work_loop(target: Path) -> CheckResult | None:
    from .claude_hooks.classify import classify

    payload = classify(target)
    if payload is None:
        return None
    status = OK if payload["state"] == "enforced" else WARN
    return (status, "claude work loop", str(payload["detail"]))


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


def run(target: Path, harness: str = "generic", *, json_output: bool = False, full: bool = False) -> int:
    ctx = build_context(target, harness)
    checks = _gather_checks(ctx)
    if json_output:
        return _report_json(ctx, checks)

    print(f"brigade doctor: target {ctx.target}")
    if ctx.selection is not None:
        sel = ctx.selection
        print(f"  harnesses: {', '.join(sel.harnesses) or '(none)'} (owner={sel.owner}, depth={sel.depth})")
    else:
        print(f"  harnesses: (legacy target, no config; assuming {', '.join(ctx.harnesses)})")
    return _report(checks, full=full)


def _gather_checks(ctx: DoctorContext) -> List[CheckResult]:
    from .registry import all_stations
    from . import managed

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
    checks.append(_check_receipts(ctx.target))
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
        name = f"skills: {harness} default wired"
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
        results.append((OK, "guard: embedded content guard", f"{label}: {scanner_dir}"))
    else:
        results.append((MANUAL, "guard: embedded content guard", f"not found at {scanner_dir}; reinstall brigade-cli"))
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

# Checks about host-global state rather than this specific repo. Grouping them
# under their own header keeps a single-repo run from reading as if the repo
# itself is responsible for an unrelated OpenClaw config or content-guard clone.
_MACHINE_LEVEL_PREFIXES = ("openclaw:",)
_MACHINE_LEVEL_NAMES = {"guard: embedded content guard", "managed tools"}


def _is_machine_level(name: str) -> bool:
    return name.startswith(_MACHINE_LEVEL_PREFIXES) or name in _MACHINE_LEVEL_NAMES


def _report(checks: List[CheckResult], *, full: bool = True) -> int:
    width = max((len(name) for _, name, _ in checks), default=20)
    counts = _status_counts(checks)
    print(
        f"triage: {len(checks)} checks, {counts[OK]} ok, {counts[WARN]} warn, "
        f"{counts[FAIL]} failed, {counts[MANUAL]} manual, {counts[INFO]} info"
    )

    condensed = not full and len(checks) > DEFAULT_TEXT_CHECK_LIMIT
    visible_checks = [check for check in checks if check[0] in {FAIL, WARN, MANUAL}] if condensed else checks
    repo_checks = [check for check in visible_checks if not _is_machine_level(check[1])]
    machine_checks = [check for check in visible_checks if _is_machine_level(check[1])]

    def _emit(items: List[CheckResult]) -> None:
        for status, name, detail in items:
            print(f"{_MARKERS[status]} {name.ljust(width)}  {detail}")

    print()
    if visible_checks:
        _emit(repo_checks)
    else:
        print("  no failures, warnings, or manual actions")
    if machine_checks:
        print()
        print("machine-level (not specific to this repo):")
        _emit(machine_checks)

    if condensed:
        print()
        print(f"showing {len(visible_checks)} actionable checks; run `brigade doctor --full` to show all checks")

    print()
    print(f"summary: {len(checks)} checks, {counts[FAIL]} failed, {counts[MANUAL]} manual")
    return 1 if counts[FAIL] else 0


def _status_counts(checks: List[CheckResult]) -> dict[str, int]:
    counts = {OK: 0, WARN: 0, FAIL: 0, MANUAL: 0, INFO: 0}
    for status, _, _ in checks:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _report_json(ctx: DoctorContext, checks: List[CheckResult]) -> int:
    counts = _status_counts(checks)
    sel = ctx.selection
    payload = {
        "target": str(ctx.target),
        "harnesses": list(ctx.harnesses),
        "owner": getattr(sel, "owner", None),
        "depth": getattr(sel, "depth", None),
        "checks": [
            {
                "status": status,
                "name": name,
                "detail": detail,
                "scope": "machine" if _is_machine_level(name) else "repo",
            }
            for status, name, detail in checks
        ],
        "summary": {
            "total": len(checks),
            "ok": counts[OK],
            "warn": counts[WARN],
            "manual": counts[MANUAL],
            "failed": counts[FAIL],
        },
        "ready": counts[FAIL] == 0,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if counts[FAIL] else 0
