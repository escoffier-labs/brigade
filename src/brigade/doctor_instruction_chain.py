"""Doctor estimate of the Codex project instruction chain (#1564)."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from . import extras
from .codex_user_config import read_user_base_cap
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


def _bytes(value: int) -> str:
    whole, remainder = divmod(value, 1024)
    fraction = f"{remainder / 1024:.10f}"[1:].rstrip("0") if remainder else ""
    return f"{value:,} bytes ({whole:,}{fraction} KiB)"


def check_codex_instruction_chain(target: Path) -> CheckResult:
    """Estimate the chain under a discovered user-base cap and stated assumptions.

    Configuration provenance does not establish effective settings or observed
    session consumption. The preview itself remains caller-supplied only.
    """
    from . import context_preview

    cap = read_user_base_cap()
    over_ceiling = cap.value is not None and cap.value > context_preview.RESOURCE_MAX_BYTES
    assumptions = (
        f"estimate, not observed (codex {_CODEX_CHAIN_VERSION} contract; assumed trust=trusted, "
        "read_access=full, cwd=target, fallbacks=none, root markers=.git, no global scope); "
        "Fresh-disk chain estimate. Active session not observed. actual_session_observed: false; "
        "profile, project, system, managed, and launch-time overrides unresolved; "
        "config discovery uses the doctor's environment and launch directory, which may differ from a Codex session; "
        f"config source {cap.config_label}; cap source {cap.source}"
    )
    if cap.value is not None:
        label = "Configured user-base cap" if cap.source == "user-base-config" else "Assumed default cap"
        # Keep ordinary over-ceiling values exact, but bound decimal rendering
        # independently of the interpreter's configurable integer digit limit.
        rendered = _bytes(cap.value) if cap.value.bit_length() <= 64 else "above the Brigade resource ceiling"
        assumptions += f"; {label}: {rendered}"
    if over_ceiling:
        return (
            MANUAL,
            _CODEX_CHAIN_NAME,
            f"not evaluated (project_resource_limit_not_evaluated); {assumptions}; "
            f"above the Brigade resource ceiling of {_bytes(context_preview.RESOURCE_MAX_BYTES)} "
            "for instruction inspection",
        )
    flags = f"--codex-version {_CODEX_CHAIN_VERSION} --trust trusted --read-access full --assume-codex-defaults"
    if cap.value is not None:
        flags += f" --project-doc-max-bytes {cap.value}"
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
    if not context_preview._POSIX_NOFOLLOW_SUPPORTED:
        remedy = "preview is unsupported on this platform (requires POSIX nofollow capabilities)"
    elif cap.value is None:
        remedy = (
            "supply a known effective cap with --project-doc-max-bytes and effective settings "
            "to brigade context preview"
        )
    if cap.value is None:
        return (MANUAL, _CODEX_CHAIN_NAME, f"not evaluated ({cap.reason}); {assumptions}; {remedy}")
    if cap.value == 0:
        return (
            WARN,
            _CODEX_CHAIN_NAME,
            f"project instruction loading disabled under this cap; instruction files were not inspected; "
            f"{assumptions}; {remedy}",
        )
    try:
        result = context_preview.preview(
            target=target,
            cwd=target,
            codex_version=_CODEX_CHAIN_VERSION,
            trust="trusted",
            read_access="full",
            assume_codex_defaults=True,
            project_doc_max_bytes=cap.value,
            codex_home=None,
        )
    except Exception as exc:  # noqa: BLE001 - doctor must stay advisory
        return (MANUAL, _CODEX_CHAIN_NAME, f"not evaluated ({type(exc).__name__}); {assumptions}; {remedy}")
    reasons = [item for item in result["limitations"] if item not in _CODEX_CHAIN_BASELINE_LIMITATIONS]
    reason_text = ", ".join(reasons) or result["status"]
    if "posix_nofollow_required" in reasons:
        return (
            MANUAL,
            _CODEX_CHAIN_NAME,
            f"not evaluated (reasons: {reason_text}); {assumptions}; "
            "preview is unsupported on this platform (requires POSIX nofollow capabilities)",
        )
    if result["status"] != "complete" or result["matches_codex"] is not True:
        return (MANUAL, _CODEX_CHAIN_NAME, f"not evaluated (reasons: {reason_text}); {assumptions}; {remedy}")
    totals = result["totals"]["project"]
    rows = [row for row in result["files"] if row["scope"] == "project"]
    parts = [
        f"{assumptions}; selected {_bytes(totals['selected_bytes'])} / cap {_bytes(cap.value)}, "
        f"consumed {_bytes(totals['consumed_raw_bytes'])}"
    ]
    for row in rows:
        advisory = row["brigade_advisory_budget_bytes"]
        advisory_text = f" (Brigade advisory budget {_bytes(advisory)})" if advisory is not None else ""
        label = json.dumps(row["path"], ensure_ascii=True)
        parts.append(f"{label} {_bytes(row['size_bytes'])} {row['contribution']}{advisory_text}")
    if not rows:
        parts.append("no project instruction files selected")
    if reasons:
        parts.append(f"notes: {', '.join(reasons)}")
    parts.append("consumer cap is separate from Brigade advisory budgets")
    truncated = any(row["contribution"] in {"truncated", "cap_exhausted"} for row in rows)
    if truncated:
        parts.append(
            "reduce startup instructions or increase project_doc_max_bytes if the full chain is necessary; "
            "configuration changes require a new run or restarted session"
        )
        parts.append(remedy)
        return (WARN, _CODEX_CHAIN_NAME, "; ".join(parts))
    return (OK, _CODEX_CHAIN_NAME, "; ".join(parts))
