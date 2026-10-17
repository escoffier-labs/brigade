"""Doctor estimate of the Codex project instruction chain (#1564)."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from . import extras
from .doctor import MANUAL, OK, WARN, CheckResult

_CODEX_CHAIN_NAME = "instruction-chain: codex"
_CODEX_CHAIN_VERSION = "0.160.0"
# Preview limitations present on every result; only the rest explain an outcome.
_CODEX_CHAIN_BASELINE_LIMITATIONS = frozenset(
    {
        "fresh_disk_only",
        "no_atomic_snapshot",
        "timestamp_granularity_limits_change_detection",
        "access_times_may_change",
        "thread_and_session_state_unknown",
        "global_not_requested",
    }
)


def check_codex_instruction_chain(target: Path) -> CheckResult:
    """Estimate the Codex project instruction chain at the target (#1564).

    Doctor supplies stated assumptions to the pinned preview contract: cwd is
    the target, trust is trusted, read access is full, omitted settings use
    Codex defaults, and no global scope is inspected. The result is a fresh-disk
    estimate, never observed session consumption.
    """
    from . import context_preview

    assumptions = (
        f"estimate, not observed (codex {_CODEX_CHAIN_VERSION} contract; assumed trust=trusted, "
        "read_access=full, cwd=target, no global scope"
    )
    flags = f"--codex-version {_CODEX_CHAIN_VERSION} --trust trusted --read-access full --assume-codex-defaults"
    target_text = str(target)
    # Controls would reach the terminal and backticks would split agent command
    # extraction, so only display-safe targets are spelled out.
    # `context` is an extras-only command, so the remedy must enable extras first.
    # Only the persistent marker survives this process; a one-shot BRIGADE_EXTRAS=1
    # on the doctor invocation does not carry over to the emitted command.
    # An explicit BRIGADE_EXTRAS=0 in the shell overrides the marker for later commands too.
    explicitly_off = os.environ.get("BRIGADE_EXTRAS", "").strip().lower() in {"0", "false", "no", "off"}
    persistent = extras.marker_path().is_file() and not explicitly_off
    enable = "" if persistent else "run `brigade extras on` (or set BRIGADE_EXTRAS=1), then "
    if target_text.isprintable() and "`" not in target_text:
        quoted = shlex.quote(target_text)
        remedy = (
            f"{enable}replace assumptions with effective settings and run "
            f"`brigade context preview --target {quoted} --cwd {quoted} {flags}`"
        )
    else:
        remedy = (
            f"{enable}replace assumptions with effective settings and run brigade context preview "
            f"with --target and --cwd set to this target and {flags}"
        )
    try:
        result = context_preview.preview(
            target=target,
            cwd=target,
            codex_version=_CODEX_CHAIN_VERSION,
            trust="trusted",
            read_access="full",
            assume_codex_defaults=True,
            codex_home=None,
        )
    except Exception as exc:  # noqa: BLE001 - doctor must stay advisory
        return (MANUAL, _CODEX_CHAIN_NAME, f"not evaluated ({type(exc).__name__}); {assumptions}); {remedy}")
    reasons = [item for item in result["limitations"] if item not in _CODEX_CHAIN_BASELINE_LIMITATIONS]
    reason_text = ", ".join(reasons) or result["status"]
    if "posix_nofollow_required" in reasons:
        return (
            MANUAL,
            _CODEX_CHAIN_NAME,
            f"not evaluated (reasons: {reason_text}); {assumptions}); "
            "preview is unsupported on this platform (requires POSIX nofollow capabilities)",
        )
    if result["status"] != "complete" or result["matches_codex"] is not True:
        return (MANUAL, _CODEX_CHAIN_NAME, f"not evaluated (reasons: {reason_text}); {assumptions}); {remedy}")
    cap = result["settings"]["project_doc_max_bytes"]
    totals = result["totals"]["project"]
    rows = [row for row in result["files"] if row["scope"] == "project"]
    parts = [
        f"{assumptions}; cap source {cap['source']}): selected {totals['selected_bytes']}/{cap['value']} bytes, "
        f"consumed {totals['consumed_raw_bytes']}"
    ]
    for row in rows:
        advisory = row["brigade_advisory_budget_bytes"]
        advisory_text = f" (advisory budget {advisory})" if advisory is not None else ""
        label = json.dumps(row["path"], ensure_ascii=True)
        parts.append(f"{label} {row['size_bytes']} bytes {row['contribution']}{advisory_text}")
    if not rows:
        parts.append("no project instruction files selected")
    if reasons:
        parts.append(f"notes: {', '.join(reasons)}")
    parts.append("consumer cap is separate from Brigade advisory budgets")
    truncated = any(row["contribution"] in {"truncated", "cap_exhausted"} for row in rows)
    if truncated:
        parts.append(remedy)
        return (WARN, _CODEX_CHAIN_NAME, "; ".join(parts))
    return (OK, _CODEX_CHAIN_NAME, "; ".join(parts))
