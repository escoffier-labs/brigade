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
from typing import Any

# Map (source, status) to a verified signal weight. Only signals the model
# cannot author earn a non-zero weight. "aboyeur ok" means the worker CLI exited
# cleanly (not that tests passed) and manual replay comparison is advisory, so
# both are deliberately neutral.
SIGNAL_RULES: dict[tuple[str, str], int] = {
    ("verify", "completed"): 1,
    ("verify", "failed"): -1,
    ("verify", "timed_out"): -1,
    ("run", "ok"): 1,
    ("run", "error"): -1,
    ("run", "failed"): -1,
    ("friction", "cleared"): 1,
    ("friction", "recurred"): -1,
    ("learnings", "cleared"): 1,
    ("learnings", "recurred"): -1,
}

# Sources whose every status is advisory/neutral regardless of value.
NEUTRAL_SOURCES = frozenset({"aboyeur", "replay"})


@dataclass(frozen=True)
class OutcomeRecord:
    """One verified (or neutral) signal for an artifact on a single task.

    ``content_fingerprint`` is the sha256 of the artifact's content at capture
    time (CocoIndex's memo-key idea applied to the ratchet: a signal vouches for
    the exact text that earned it, not the name). ``None`` on records captured
    before fingerprints existed - the legacy cohort.

    ``context`` is a coarse manifest of the runtime harness the signal was earned
    under (Brigade version, interpreter, platform, best-effort harness and model),
    and ``capability_fingerprint`` is the sha256 of its low-cardinality capability
    vector. Both are ``None`` on records captured before context tracking existed.
    Phase 1 records them but does not score on them; see
    docs/design/context-blind-spot.md.
    """

    artifact_id: str
    artifact_kind: str  # "card" | "skill"
    task_id: str
    source: str
    signal_value: int  # +1 | 0 | -1
    evidence_ref: str
    ts: str
    code_graph_delta: dict[str, Any] | None = None
    context_eval: dict[str, Any] | None = None
    content_fingerprint: str | None = None
    context: dict[str, Any] | None = None
    capability_fingerprint: str | None = None


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


def scored_records(records: list[OutcomeRecord]) -> list[OutcomeRecord]:
    """Return the de-duplicated records that contribute to scoring.

    Counts DISTINCT verified evidence, not raw rows. A re-captured or retried run
    yields a byte-identical record (same source, evidence_ref, task_id), and the
    same physical receipt must contribute at most one signal, or a single trial
    could cross install_min_helped and auto-install with no independent evidence.

    Dedup keys on ``evidence_ref`` (the physical proof, e.g. a receipt path). A
    record with NO evidence_ref - an explicit ``outcome record`` written without
    ``--evidence`` - cannot be proven a duplicate of another, so it is kept as a
    distinct signal (keyed by row position). Otherwise N genuinely-distinct manual
    signals for one artifact would all collapse to the empty key and the artifact
    could never reach install_min_helped no matter how many real clears occurred.
    """
    seen: set[tuple] = set()
    deduped: list[OutcomeRecord] = []
    for idx, r in enumerate(records):
        key: tuple = (r.source, r.evidence_ref, r.task_id) if r.evidence_ref else ("__unkeyed__", idx)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def score_records(artifact_id: str, records: list[OutcomeRecord]) -> OutcomeScore:
    """Fold an artifact's records into counts plus a Wilson lower-bound score."""
    deduped = scored_records(records)
    helped = sum(1 for r in deduped if r.signal_value > 0)
    hurt = sum(1 for r in deduped if r.signal_value < 0)
    neutral = sum(1 for r in deduped if r.signal_value == 0)
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


@dataclass(frozen=True)
class FingerprintCohorts:
    """An artifact's score split against its current content fingerprint.

    ``current`` drops records that are PROVEN stale: fingerprinted against a
    different revision of the artifact's text. An edited skill therefore earns
    its score back instead of inheriting signals earned by text that no longer
    exists. Records captured before fingerprints existed (``content_fingerprint``
    is None) cannot be proven stale either way, so they are grandfathered into
    ``current``: a never-edited skill keeps its score across the rollout instead
    of collapsing to zero. ``lifetime`` is the fold over every record, unchanged.
    When the artifact's content cannot be resolved (``current_fingerprint`` is
    None) the split is unavailable and ``current`` equals ``lifetime``.

    ``stale_records`` counts scored records fingerprinted against a different
    revision; ``legacy_records`` counts scored pre-fingerprint records. Legacy
    is surfaced as a count but never rewritten.
    """

    current_fingerprint: str | None
    current: OutcomeScore
    lifetime: OutcomeScore
    stale_records: int
    legacy_records: int

    @property
    def pinned(self) -> bool:
        return self.current_fingerprint is not None


def split_by_fingerprint(
    artifact_id: str,
    records: list[OutcomeRecord],
    current_fingerprint: str | None,
) -> FingerprintCohorts:
    """Split an artifact's records into current/stale/legacy cohorts and score them.

    Pure fold, same dedup rules as ``score_records``: cohort counts are over
    scored (deduped) records so a re-captured receipt cannot inflate any cohort.
    """
    lifetime = score_records(artifact_id, records)
    if not current_fingerprint:
        return FingerprintCohorts(None, lifetime, lifetime, 0, 0)
    deduped = scored_records(records)
    current = score_records(
        artifact_id,
        [r for r in deduped if not r.content_fingerprint or r.content_fingerprint == current_fingerprint],
    )
    stale = sum(1 for r in deduped if r.content_fingerprint and r.content_fingerprint != current_fingerprint)
    legacy = sum(1 for r in deduped if not r.content_fingerprint)
    return FingerprintCohorts(current_fingerprint, current, lifetime, stale, legacy)


def fingerprint_cohort(record: OutcomeRecord, current_fingerprint: str | None) -> str:
    """Name the cohort one record belongs to: "current", "stale", or "legacy"."""
    if not record.content_fingerprint:
        return "legacy"
    if current_fingerprint is None or record.content_fingerprint == current_fingerprint:
        return "current"
    return "stale"


def current_content_records(records: list[OutcomeRecord], current_fingerprint: str | None) -> list[OutcomeRecord]:
    """Deduped records whose content is current (proven-stale dropped, legacy kept).

    The same predicate ``split_by_fingerprint`` scores its ``current`` cohort over,
    exposed as the record subset so the capability split can compose on top of it.
    """
    deduped = scored_records(records)
    if not current_fingerprint:
        return deduped
    return [r for r in deduped if not r.content_fingerprint or r.content_fingerprint == current_fingerprint]


# Shrinkage strength: a capability cohort thinner than ~kappa trials is pulled
# toward the pooled rate, so a single run under a novel harness cannot swing the
# estimate. One global constant, documented, never tuned per artifact.
DEFAULT_SHRINK_KAPPA = 4.0


def shrink_rate(helped: int, total: int, prior_rate: float, kappa: float = DEFAULT_SHRINK_KAPPA) -> float:
    """Deterministic shrinkage toward a prior: (helped + kappa*prior) / (total + kappa).

    With no trials the result is the prior; as trials accumulate it converges on
    the cohort's own rate. This is the graceful-degradation lever for thin
    per-capability cohorts, not a confidence bound.
    """
    denom = total + kappa
    if denom <= 0:
        return prior_rate
    return (helped + kappa * prior_rate) / denom


@dataclass(frozen=True)
class CapabilityCohorts:
    """A content-current score split by the CURRENT runtime capability context.

    ``pooled`` is the content-current cohort (the score the ratchet and default
    rank use, across all capabilities). ``capability`` scores only the
    content-current records earned under ``current_capability``, with pre-context
    records (no ``capability_fingerprint``) grandfathered in so the split is
    non-disruptive at rollout, exactly like content legacy records. ``shrunk_rate``
    pulls a thin capability cohort toward the pooled rate. When no capability is
    resolvable ``capability`` equals ``pooled``.

    ``off_capability_records`` counts content-current records earned under a
    different capability; ``capability_legacy_records`` counts content-current
    records with no capability fingerprint yet. Phase 2 surfaces this in retrieval
    (rank/explain); the ratchet still scores ``pooled`` only.
    """

    current_capability: str | None
    pooled: OutcomeScore
    capability: OutcomeScore
    shrunk_rate: float
    off_capability_records: int
    capability_legacy_records: int

    @property
    def pinned(self) -> bool:
        return self.current_capability is not None


def split_by_capability(
    artifact_id: str,
    content_current: list[OutcomeRecord],
    current_capability: str | None,
    *,
    kappa: float = DEFAULT_SHRINK_KAPPA,
) -> CapabilityCohorts:
    """Split content-current records by the current capability and shrink the thin cohort.

    ``content_current`` is expected to be the deduped content-current subset
    (from ``current_content_records``). Pass the current capability fingerprint to
    score the "will this help under my harness" cohort; pass None (unresolvable)
    to get the pooled score in both slots.
    """
    pooled = score_records(artifact_id, content_current)
    pooled_total = pooled.helped + pooled.hurt
    pooled_rate = pooled.helped / pooled_total if pooled_total else 0.0
    if not current_capability:
        return CapabilityCohorts(None, pooled, pooled, pooled_rate, 0, 0)
    on = [r for r in content_current if not r.capability_fingerprint or r.capability_fingerprint == current_capability]
    capability = score_records(artifact_id, on)
    off_capability = sum(
        1 for r in content_current if r.capability_fingerprint and r.capability_fingerprint != current_capability
    )
    capability_legacy = sum(1 for r in content_current if not r.capability_fingerprint)
    shrunk = shrink_rate(capability.helped, capability.helped + capability.hurt, pooled_rate, kappa)
    return CapabilityCohorts(current_capability, pooled, capability, shrunk, off_capability, capability_legacy)


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


@dataclass(frozen=True)
class StatusTransition:
    """One persisted status transition, read back from a decision receipt.

    The decision receipts under ``memory/outcome/decisions/`` are the transition
    log; ``status.json`` is the cache they fold into. Keeping this pure lets the
    rebuild check prove the cache is reproducible from the log.
    """

    artifact_id: str
    new_status: str
    created_at: str  # ISO 8601


def fold_status(transitions: list[StatusTransition]) -> dict[str, dict]:
    """Fold decision transitions into the status map they produce.

    Later transitions win per artifact, ordered by ``created_at``. This is the
    projection ``status.json`` caches: reconcile writes a decision receipt and a
    status entry together on every transition, so replaying the receipts must
    reproduce the persisted status exactly. Inspired by ActiveGraph's
    ``apply_event`` projection (state is a fold of the append-only log).
    """
    ordered = sorted(transitions, key=lambda t: (t.created_at, t.artifact_id))
    status: dict[str, dict] = {}
    for t in ordered:
        status[t.artifact_id] = {"status": t.new_status, "last_action_ts": t.created_at}
    return status


def project_statuses(
    scores: dict[str, OutcomeScore],
    *,
    config: ReconcileConfig,
    now: dt.datetime,
) -> dict[str, Decision]:
    """Project each artifact's ratchet decision from a clean candidate baseline.

    A pure "what-if" over the signal log: given the scores derived from
    ``records.jsonl`` and a hypothetical config, what would the ratchet decide
    for each artifact starting fresh? Because the baseline has no prior action,
    the cooldown never fires and each artifact gets one decision from its score.
    This is the fork primitive: replay the log under different rules without
    touching live state, so two configs can be diffed.
    """
    return {
        artifact_id: decide(
            score_obj,
            current_status="candidate",
            last_action_ts=None,
            now=now,
            config=config,
        )
        for artifact_id, score_obj in scores.items()
    }


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
