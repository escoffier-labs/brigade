"""Outcome ledger core: verified, hands-off scoring and reconcile decisions.

This is the deterministic spine of Brigade's learning loop. A card or skill is
promoted only when a signal the model cannot author says it helped, and is
reverted when a real signal measures a regression. Nothing here grades itself
with an LLM and nothing here touches the filesystem; capture/persistence and the
CLI wire these pure functions to receipts and the vault.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

# Map (source, status) to a verified signal weight. Only signals the model
# cannot author earn a non-zero weight. "aboyeur ok" means the worker CLI exited
# cleanly (not that tests passed) and manual replay comparison is advisory, so
# both are deliberately neutral.
SIGNAL_RULES: dict[tuple[str, str], int] = {
    ("verify", "completed"): 1,
    ("verify", "failed"): -1,
    ("verify", "timed_out"): -1,
    ("friction", "cleared"): 1,
    ("friction", "recurred"): -1,
    ("learnings", "cleared"): 1,
    ("learnings", "recurred"): -1,
}

# Sources whose every status is advisory/neutral regardless of value.
NEUTRAL_SOURCES = frozenset({"aboyeur", "replay"})


@dataclass(frozen=True)
class OutcomeRecord:
    """One verified (or neutral) signal for an artifact on a single task."""

    artifact_id: str
    artifact_kind: str  # "card" | "skill"
    task_id: str
    source: str
    signal_value: int  # +1 | 0 | -1
    evidence_ref: str
    ts: str


@dataclass(frozen=True)
class OutcomeScore:
    artifact_id: str
    helped: int
    hurt: int
    neutral: int
    score: float
    last_signal_ts: str | None


@dataclass(frozen=True)
class ReconcileConfig:
    install_min_helped: int = 2
    revert_min_hurt: int = 1
    bump_min_helped: int = 3
    cooldown_seconds: int = 86_400
    z: float = 1.96


@dataclass(frozen=True)
class Decision:
    artifact_id: str
    action: str  # "install" | "bump" | "rollback" | "hold"
    new_status: str  # "candidate" | "promoted" | "demoted"
    reason: str


def wilson_lower_bound(helped: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for helped/total.

    Returns 0.0 with no trials so unproven artifacts never out-rank vetted ones,
    and grows toward the naive rate as confirming trials accumulate.
    """
    if total <= 0:
        return 0.0
    phat = helped / total
    denom = 1.0 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return (centre - margin) / denom


def signal_value(source: str, status: str) -> int:
    """Return the verified weight (+1/0/-1) for a (source, status) signal."""
    if source in NEUTRAL_SOURCES:
        return 0
    return SIGNAL_RULES.get((source, status), 0)


def score_records(artifact_id: str, records: list[OutcomeRecord]) -> OutcomeScore:
    """Fold an artifact's records into counts plus a Wilson lower-bound score."""
    helped = sum(1 for r in records if r.signal_value > 0)
    hurt = sum(1 for r in records if r.signal_value < 0)
    neutral = sum(1 for r in records if r.signal_value == 0)
    total = helped + hurt
    last_ts = max((r.ts for r in records), default=None)
    return OutcomeScore(
        artifact_id=artifact_id,
        helped=helped,
        hurt=hurt,
        neutral=neutral,
        score=wilson_lower_bound(helped, total),
        last_signal_ts=last_ts,
    )


def decide(
    score: OutcomeScore,
    *,
    current_status: str,
    last_action_ts: dt.datetime | None,
    now: dt.datetime,
    config: ReconcileConfig,
) -> Decision:
    """Decide the next hands-off transition for an artifact.

    Forward-only ratchet: a clean candidate installs, any verified regression on
    a promoted artifact rolls it back, and a per-artifact cooldown prevents
    thrash. No human approval is consulted anywhere.
    """
    if last_action_ts is not None and (now - last_action_ts).total_seconds() < config.cooldown_seconds:
        return Decision(score.artifact_id, "hold", current_status, "cooldown active")

    if current_status == "candidate":
        if score.hurt > 0:
            return Decision(score.artifact_id, "hold", "candidate", "withheld: verified regression present")
        if score.helped >= config.install_min_helped:
            return Decision(score.artifact_id, "install", "promoted", "verified helped, no regressions")
        return Decision(score.artifact_id, "hold", "candidate", "insufficient verified evidence")

    if current_status == "promoted":
        if score.hurt >= config.revert_min_hurt:
            return Decision(score.artifact_id, "rollback", "demoted", "verified regression measured")
        if score.helped >= config.bump_min_helped:
            return Decision(score.artifact_id, "bump", "promoted", "sustained verified helped")
        return Decision(score.artifact_id, "hold", "promoted", "no change")

    return Decision(score.artifact_id, "hold", current_status, "terminal status")


# Retrieval rank weights: confidence, verified outcome, keyword match.
RANK_WEIGHTS = (0.5, 0.4, 0.1)


def rank_score(
    *,
    confidence: float,
    outcome: float,
    keyword: float,
    weights: tuple[float, float, float] = RANK_WEIGHTS,
) -> float:
    """Blend confidence, verified outcome, and keyword match into a retrieval rank.

    The verified outcome term lets a measurably-helpful artifact rise without
    over-trusting a thin keyword match, so retrieval surfaces what worked, not
    just what matched.
    """
    w_confidence, w_outcome, w_keyword = weights
    return w_confidence * confidence + w_outcome * outcome + w_keyword * keyword
