"""Read-only stale-recall, projection-drift, and scope-leakage eval (#845)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from brigade.memory_cmd import MemoryCareConfig, _iter_cards, _parse_frontmatter

from .corpus import repo_root

REPORT_SCHEMA = "brigade.memory-quality-eval.v1"
INPUT_SCHEMA = "brigade.memory-quality-input.v1"
DEFAULT_QUALITY_ROOT = repo_root() / "evals" / "memory-quality"
SCOPE_DIMENSIONS = ("repository", "task", "operator", "branch", "worktree")


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _cards(root: Path) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    for path in _iter_cards(root, MemoryCareConfig()):
        raw = path.read_bytes()
        metadata, _ = _parse_frontmatter(raw.decode("utf-8"))
        card_id = str(metadata.get("id") or path.stem)
        scope = metadata.get("scope")
        if not isinstance(scope, dict):
            scope = {key: metadata.get(key) for key in SCOPE_DIMENSIONS if metadata.get(key) is not None}
        cards[card_id] = {
            "fresh_until": metadata.get("fresh_until"),
            "hash": f"sha256:{hashlib.sha256(raw).hexdigest()}",
            "path": path.relative_to(root).as_posix(),
            "scope": scope,
        }
    return cards


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "sample_count": 0,
        "violation_count": 0,
        "rate": None,
        "violations": [],
    }


def evaluate_memory_quality(root: Path = DEFAULT_QUALITY_ROOT) -> dict[str, Any]:
    """Evaluate existing cards and captured evidence contracts without writing them."""
    root = root.expanduser().resolve()
    payload = json.loads((root / "eval.json").read_text(encoding="utf-8"))
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"eval input schema must be {INPUT_SCHEMA!r}")
    as_of = date.fromisoformat(str(payload["as_of"]))
    cards = _cards(root)
    retrievals = payload.get("retrievals")
    if not isinstance(retrievals, list):
        raise ValueError("retrievals must be a list")

    stale_violations: list[dict[str, Any]] = []
    scope_violations: list[dict[str, Any]] = []
    retrieval_count = 0
    scoped_count = 0
    for query in retrievals:
        if not isinstance(query, dict) or not isinstance(query.get("results"), list):
            raise ValueError("each retrieval must contain a results list")
        requested = query.get("scope") if isinstance(query.get("scope"), dict) else None
        for rank, card_id_raw in enumerate(query["results"], 1):
            card_id = str(card_id_raw)
            card = cards.get(card_id)
            if card is None:
                continue
            retrieval_count += 1
            fresh_until = card["fresh_until"]
            if isinstance(fresh_until, str) and date.fromisoformat(fresh_until) < as_of:
                stale_violations.append(
                    {
                        "card_id": card_id,
                        "fresh_until": fresh_until,
                        "query_id": str(query.get("id") or ""),
                        "rank": rank,
                    }
                )
            if requested is not None:
                scoped_count += 1
                declared = card["scope"]
                if isinstance(declared, dict):
                    for dimension in SCOPE_DIMENSIONS:
                        wanted, actual = requested.get(dimension), declared.get(dimension)
                        if actual is not None and wanted != actual:
                            scope_violations.append(
                                {
                                    "card_id": card_id,
                                    "declared": actual,
                                    "dimension": dimension,
                                    "query_id": str(query.get("id") or ""),
                                    "rank": rank,
                                    "requested": wanted,
                                }
                            )
                            break

    projections = payload.get("projections")
    if isinstance(projections, list):
        drift: list[dict[str, Any]] = []
        compared = 0
        for row in projections:
            if not isinstance(row, dict):
                continue
            card_id = str(row.get("card_id") or "")
            card = cards.get(card_id)
            projection_hash = row.get("content_hash")
            if card is None or not isinstance(projection_hash, str):
                continue
            compared += 1
            if projection_hash != card["hash"]:
                drift.append({"card_id": card_id, "canonical_hash": card["hash"], "projection_hash": projection_hash})
        projection = {
            "available": True,
            "sample_count": compared,
            "divergent_count": len(drift),
            "rate": _ratio(len(drift), compared),
            "violations": drift,
        }
    else:
        projection = _unavailable("evidence contract has no projection_items field")
        projection["divergent_count"] = 0

    scope = (
        {
            "available": True,
            "sample_count": scoped_count,
            "violation_count": len(scope_violations),
            "rate": _ratio(len(scope_violations), scoped_count),
            "violations": scope_violations,
        }
        if any(isinstance(q, dict) and isinstance(q.get("scope"), dict) for q in retrievals)
        else _unavailable("retrieval contract has no requested scope field")
    )
    return {
        "schema": REPORT_SCHEMA,
        "issue": 845,
        "as_of": as_of.isoformat(),
        "read_only": True,
        "source": "existing-memory-and-evidence-contracts",
        "card_count": len(cards),
        "stale_recall": {
            "available": True,
            "sample_count": retrieval_count,
            "violation_count": len(stale_violations),
            "rate": _ratio(len(stale_violations), retrieval_count),
            "violations": stale_violations,
        },
        "projection_drift": projection,
        "scope_leakage": scope,
        "contract_wishes": {
            "projection_items": {
                "available": isinstance(projections, list),
                "wish": "evidence reports should expose card_id and content_hash per derived item",
            },
            "retrieval_explanations": {
                "available": False,
                "wish": "retrieval reports should expose exclusion reasons and freshness decisions",
            },
        },
    }
