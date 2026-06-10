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

from pathlib import Path

# --- Bootstrap files -------------------------------------------------------
# OpenClaw loads these into the session prefix every turn. There is an empirical
# soft ceiling around 12,000 chars per file before content is silently truncated
# mid-session. Per-file budgets stay below that with headroom.
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
DEFAULT_BOOTSTRAP_SOFT_LIMIT = 10_000
DEFAULT_BOOTSTRAP_HARD_LIMIT = 11_500
BOOTSTRAP_HARD_LIMIT_CEILING = 12_000

# --- Memory cards ----------------------------------------------------------
MEMORY_CARD_BUDGET_BYTES = 8_000

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
