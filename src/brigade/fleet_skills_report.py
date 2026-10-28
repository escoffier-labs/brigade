"""Node side of the Fleet Hub skills review: a bounded summary of local skill state.

The summary reuses ``brigade skills fleet status`` for copy status, the install
receipts for fingerprints and rollback snapshots, and ``outcome`` for every
score and ratchet suggestion. It carries no skill contents, prompts,
credentials, or absolute paths, only harness names, ids, and hashes.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from . import __version__

SCHEMA_VERSION = 1
MAX_ROWS = 500
MAX_TEXT = 120
SHA256 = re.compile(r"[0-9a-f]{64}")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return "".join(ch for ch in str(value) if ch.isprintable())[:MAX_TEXT]


def _hash(value: Any) -> str | None:
    return value if isinstance(value, str) and SHA256.fullmatch(value) else None


def _update_selector(skill_id: str, source: Any) -> str | None:
    """A registry-resolvable selector, never a filesystem path."""
    kind = source.get("kind") if isinstance(source, dict) else None
    if kind == "brigade-bundle":
        return f"bundled:{skill_id}"
    if kind == "registry":
        return f"registry:{skill_id}"
    return None


def _rollback_available(receipt: dict[str, Any]) -> bool:
    snapshot = receipt.get("rollback_snapshot")
    return isinstance(snapshot, str) and bool(snapshot) and Path(snapshot).is_dir()


def build_copies(target: Path) -> list[dict[str, Any]]:
    from . import skills_cmd

    payload = skills_cmd._fleet_status_payload(target)
    receipts = skills_cmd._fleet_receipts(target)
    rows: list[dict[str, Any]] = []
    for copy in payload["copies"]:
        skill_id, harness = str(copy["skill_id"]), str(copy["harness"])
        receipt = receipts.get((skill_id, harness), {})
        installed = _mapping(receipt.get("installed"))
        source = _mapping(copy.get("source"))
        receipt_source = _mapping(receipt.get("source"))
        rows.append(
            {
                "skill_id": _text(skill_id),
                "harness": _text(harness),
                "status": _text(copy["status"]),
                "installed_sha256": _hash(installed.get("skill_fingerprint")),
                "registry_sha256": _hash(source.get("fingerprint")),
                "rollback_available": _rollback_available(receipt),
                "source_identity_sha256": _hash(_identity_hash(receipt_source)),
                "update_selector": _update_selector(skill_id, source or receipt_source),
            }
        )
    return rows


def _identity_hash(source: dict[str, Any]) -> str | None:
    """Hash of the source identity so a path or URL never leaves the machine."""
    import hashlib

    identity = source.get("identity")
    if not isinstance(identity, str) or not identity:
        return None
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def find_conflicts(copies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same skill id from different sources, or harness copies that disagree."""
    by_skill: dict[str, list[dict[str, Any]]] = {}
    for copy in copies:
        by_skill.setdefault(copy["skill_id"], []).append(copy)
    conflicts: list[dict[str, Any]] = []
    for skill_id, rows in sorted(by_skill.items()):
        if len(rows) < 2:
            continue
        sources = {row["source_identity_sha256"] for row in rows if row["source_identity_sha256"]}
        if len(sources) > 1:
            conflicts.append(
                {"skill_id": skill_id, "kind": "duplicate-source", "locations": sorted({r["harness"] for r in rows})}
            )
        present = [row for row in rows if row["status"] != "missing" and row["installed_sha256"]]
        # Each harness renders the same source differently (Codex adds frontmatter), so
        # differing hashes only mean divergence when some copy is not current for its harness.
        all_current = all(row["status"] == "current" for row in present)
        if len({row["installed_sha256"] for row in present}) > 1 and not all_current:
            conflicts.append(
                {
                    "skill_id": skill_id,
                    "kind": "divergent-copies",
                    "locations": sorted({row["harness"] for row in present}),
                }
            )
    return conflicts


def build_scores(target: Path, now: dt.datetime) -> list[dict[str, Any]]:
    """Per-skill outcome numbers from ``outcome`` (the same path as ``brigade outcome reconcile``, dry-run)."""
    from . import localio, outcome as core, outcome_cmd, scorecard as scorecard_mod

    records = outcome_cmd.load_scoring_records(target)
    cohorts = outcome_cmd._fingerprint_cohorts_by_artifact(target, records)
    by_artifact = outcome_cmd._records_by_artifact(records)
    scorecards = {card.subject.artifact_id: card for card in scorecard_mod.build_scorecards(target)}
    status_map = outcome_cmd.load_status(target)
    kinds = outcome_cmd._artifact_kinds(records, status_map, scorecards)
    config = core.ReconcileConfig()
    half_life = core.DEFAULT_RECENCY_HALF_LIFE_DAYS * 86400.0
    rows: list[dict[str, Any]] = []
    for artifact_id in outcome_cmd._reconcile_artifact_ids(kinds, status_map, scorecards, cohorts):
        if kinds.get(artifact_id, "skill") != "skill":
            continue
        entry = status_map.get(artifact_id) or {}
        prior = str(entry.get("status", "candidate"))
        decision = outcome_cmd._decide_reconcile_item(
            artifact_id,
            "skill",
            cohorts=cohorts.get(artifact_id),
            card=scorecards.get(artifact_id),
            prior_status=prior,
            last_action_ts=localio.parse_iso_datetime(entry.get("last_action_ts")),
            now=now,
            config=config,
        )
        cohort = cohorts.get(artifact_id)
        current = cohort.current if cohort is not None else core.score_records(artifact_id, [])
        scored = (
            core.current_content_records(by_artifact.get(artifact_id, []), cohort.current_fingerprint)
            if cohort is not None
            else []
        )
        rows.append(
            {
                "artifact_id": _text(artifact_id),
                "helped": current.helped,
                "hurt": current.hurt,
                "neutral": current.neutral,
                "total": current.helped + current.hurt,
                "wilson_lower_bound": round(current.score, 4),
                "recency_wilson": round(core.recency_weighted_wilson(scored, now, half_life), 4),
                "status": _text(prior),
                "suggestion": _text(decision.action),
                "suggestion_reason": _text(decision.reason),
                "last_signal_at": current.last_signal_ts,
            }
        )
    return rows[:MAX_ROWS]


def build_report(target: Path, *, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    copies = build_copies(target)
    conflicts = find_conflicts(copies)
    try:
        scores = build_scores(target, now)
    except Exception:  # an unreadable ledger must read as "not tracked", never as zero usage
        scores = []
    truncated = len(copies) > MAX_ROWS
    public_copies = [{k: v for k, v in copy.items() if k != "source_identity_sha256"} for copy in copies[:MAX_ROWS]]
    return {
        "schema_version": SCHEMA_VERSION,
        "brigade_version": _text(__version__),
        "observed_at": now.replace(microsecond=0).isoformat(),
        "registry_revision": None,
        "usage_tracked": bool(copies) and any(row["helped"] + row["hurt"] + row["neutral"] > 0 for row in scores),
        "truncated": truncated,
        "copies": public_copies,
        "conflicts": conflicts[:MAX_ROWS],
        "scores": scores,
    }
