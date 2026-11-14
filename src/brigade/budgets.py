"""Canonical size/staleness budgets for the brigade operator system.

This is the single source of truth for the numbers that govern how much content
may live in bootstrap files and memory cards, how long handoffs may sit before
they count as a stalled backlog, and how stale a memory-care scan may get.

brigade's own `doctor`, `ingest`, `handoff`, and `repos` stations all import
from here so the preventive guards and the post-hoc warnings can never disagree.
Satellite tools (bootstrap-doctor, memory-doctor) are intended to depend on
brigade and consume these definitions rather than redeclaring them, so updating
a budget here updates every downstream consumer.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

# --- Bootstrap files -------------------------------------------------------
# Brigade's own per-name target budget for an always-loaded instruction file.
# No harness enforces these numbers. The caps a harness does enforce come from
# `instruction_loaders` (OpenClaw configuration, the Codex project cap), and
# `limit_for` takes the smaller of the two. Per-file targets stay below the
# empirical 12,000 char truncation point with headroom.
BOOTSTRAP_BUDGETS: dict[str, int] = {
    "AGENTS.md": 12_000,
    "CLAUDE.md": 6_000,
    "MEMORY.md": 7_000,
    "TOOLS.md": 10_000,
    "USER.md": 8_000,
    "SAFETY_RULES.md": 10_000,
    "INSTALL_FOR_AGENTS.md": 8_000,
    "SOUL.md": 8_000,
    "IDENTITY.md": 4_000,
    "HEARTBEAT.md": 5_000,
}

# Flat whole-file thresholds for the simpler bootstrap auditor model (one soft
# warning level and one hard limit applied across tracked files), as opposed to
# the per-file BOOTSTRAP_BUDGETS above. Canonical here so the bootstrap-doctor
# satellite sources them instead of redeclaring its own (which had drifted).
# Invariant: soft < hard < ceiling. The ceiling is the empirical truncation point.
# `budgets show` (budgets_cmd.py), tests/test_budgets.py and the external size
# checker the instruction-budget design retires still read these. No limit in
# the instruction inventory uses them.
DEFAULT_BOOTSTRAP_SOFT_LIMIT = 10_000
DEFAULT_BOOTSTRAP_HARD_LIMIT = 11_500
BOOTSTRAP_HARD_LIMIT_CEILING = 12_000

# --- Instruction file thresholds -------------------------------------------
# A file's limit is the smallest of its loader caps and its target budget.
# Brigade acts at ACT_AT_PERCENT of the limit and trims down to TRIM_TO_PERCENT.
ACT_AT_PERCENT = 85
TRIM_TO_PERCENT = 70
# Target budget for a file the per-name table does not list.
DEFAULT_LIMIT_BYTES = 20_000

# --- Memory cards ----------------------------------------------------------
# Default card size budget. Per-workspace override lives in
# .brigade/memory-care.toml (max_card_bytes); MemoryCareConfig and `doctor`
# both read that, so this is only the fallback default when no config is set.
MEMORY_CARD_BUDGET_BYTES = 12_000

# --- MEMORY.md index -------------------------------------------------------
# The flat index should stay short; detail belongs in topic cards.
MEMORY_INDEX_MAX_LINES = 180

# --- Staleness thresholds --------------------------------------------------
# A memory-care decay scan older than this is considered stale.
MEMORY_CARE_SCAN_STALE_DAYS = 7
# A handoff inbox with pending files older than this is a stalled backlog:
# handoffs are being written but nothing is ingesting them.
HANDOFF_BACKLOG_STALE_DAYS = 3
HANDOFF_BACKLOG_STALE_SECONDS = HANDOFF_BACKLOG_STALE_DAYS * 24 * 60 * 60


def bootstrap_budget(name: str) -> int | None:
    """Byte budget for a bootstrap file basename, or None if it is not tracked."""
    return BOOTSTRAP_BUDGETS.get(name)


def is_bootstrap_target(rel_path: str) -> bool:
    """True if a routing target basename is a budgeted bootstrap file."""
    return Path(rel_path).name in BOOTSTRAP_BUDGETS


def route_would_exceed_budget(dest: Path, addition: str) -> tuple[bool, int | None]:
    """Whether appending `addition` to a bootstrap file would exceed its budget.

    Returns (would_exceed, budget). Non-bootstrap targets (e.g. .learnings/*)
    are never guarded: (False, None).
    """
    budget = BOOTSTRAP_BUDGETS.get(dest.name)
    if budget is None:
        return False, None
    try:
        existing = dest.stat().st_size if dest.is_file() else 0
    except OSError:
        existing = 0
    projected = existing + len(addition.encode("utf-8"))
    return projected > budget, budget


def threshold(limit: int, percent: int) -> int:
    """`percent` of `limit` in whole bytes, rounded down."""
    return limit * percent // 100


def limit_for(target_budget: int, target_source: str, caps: Sequence[tuple[str, int]]) -> tuple[int, str]:
    """The one limit for a file and the label that names where it came from.

    The smallest of the target budget and every loader cap wins. A tie with the
    target goes to the loader, since a harness enforces it, and a tie between
    loader caps goes to the lexicographically smallest label.
    """
    if not caps:
        return target_budget, target_source
    label, cap = min(caps, key=lambda item: (item[1], item[0]))
    if cap <= target_budget:
        return cap, label
    return target_budget, target_source
