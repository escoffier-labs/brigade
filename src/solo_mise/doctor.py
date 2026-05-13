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
    return results


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
