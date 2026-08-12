"""Read-only stale-recall, projection-drift, and scope-leakage metrics (#845).

Builds on the #722 retrieval envelope and #891 card-identity dual-read. Does not
invent a parallel harness CLI or ``projection_items`` vocabulary.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from brigade.memory_cmd import _parse_frontmatter

from .corpus import load_cards, repo_root
from .projection import SCOPE_DIMENSIONS, content_hash

QUALITY_SCHEMA = "memory-retrieval-quality.v1"
INPUT_SCHEMA = "memory-retrieval-quality-input.v1"
DEFAULT_QUALITY_ROOT = repo_root() / "evals" / "memory-retrieval" / "quality"
PROJECTION_UNAVAILABLE = "eval input has no projections list with external_id/content_hash rows"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _scope_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    scope = metadata.get("scope")
    if isinstance(scope, dict):
        return {key: scope.get(key) for key in SCOPE_DIMENSIONS}
    return {key: metadata.get(key) for key in SCOPE_DIMENSIONS}


def _card_index(root: Path) -> dict[str, dict[str, Any]]:
    """Index cards by canonical id and every alias for dual-read resolution."""
    root = root.expanduser().resolve()
    index: dict[str, dict[str, Any]] = {}
    for card in load_cards(root):
        path = root / card.path
        text = path.read_text(encoding="utf-8")
        metadata, _ = _parse_frontmatter(text)
        record = {
            "external_id": card.card_id,
            "fresh_until": metadata.get("fresh_until"),
            "hash": content_hash(text),
            "path": card.path,
            "scope": _scope_from_metadata(metadata if isinstance(metadata, dict) else {}),
        }
        for key in (card.card_id, *card.aliases):
            index[key] = record
    return index


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "sample_count": 0,
        "violation_count": 0,
        "rate": None,
        "violations": [],
    }


def _resolve(cards: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    return cards.get(key)


def evaluate_memory_quality(root: Path = DEFAULT_QUALITY_ROOT) -> dict[str, Any]:
    """Evaluate existing cards and captured retrieval/projection rows without writing them."""
    root = root.expanduser().resolve()
    payload = json.loads((root / "eval.json").read_text(encoding="utf-8"))
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"eval input schema must be {INPUT_SCHEMA!r}")
    as_of = date.fromisoformat(str(payload["as_of"]))
    cards = _card_index(root)
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
        for rank, key_raw in enumerate(query["results"], 1):
            key = str(key_raw)
            card = _resolve(cards, key)
            if card is None:
                raise ValueError(f"retrieval result {key!r} did not resolve via card_id or alias")
            retrieval_count += 1
            external_id = str(card["external_id"])
            fresh_until = card["fresh_until"]
            if isinstance(fresh_until, str) and date.fromisoformat(fresh_until) < as_of:
                stale_violations.append(
                    {
                        "external_id": external_id,
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
                                    "external_id": external_id,
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
            key = str(row.get("external_id") or "")
            card = _resolve(cards, key)
            projection_hash = row.get("content_hash")
            if card is None:
                raise ValueError(f"projection external_id {key!r} did not resolve via card_id or alias")
            if not isinstance(projection_hash, str):
                continue
            compared += 1
            if projection_hash != card["hash"]:
                drift.append(
                    {
                        "external_id": str(card["external_id"]),
                        "canonical_hash": card["hash"],
                        "projection_hash": projection_hash,
                    }
                )
        projection = {
            "available": True,
            "sample_count": compared,
            "divergent_count": len(drift),
            "rate": _ratio(len(drift), compared),
            "violations": drift,
        }
    else:
        projection = _unavailable(PROJECTION_UNAVAILABLE)
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
    canonical_ids = {record["external_id"] for record in cards.values()}
    return {
        "schema": QUALITY_SCHEMA,
        "issue": 845,
        "as_of": as_of.isoformat(),
        "read_only": True,
        "source": "existing-memory-and-evidence-contracts",
        "card_count": len(canonical_ids),
        "stale_recall": {
            "available": True,
            "sample_count": retrieval_count,
            "violation_count": len(stale_violations),
            "rate": _ratio(len(stale_violations), retrieval_count),
            "violations": stale_violations,
        },
        "projection_drift": projection,
        "scope_leakage": scope,
    }


def attach_quality_to_report(report: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    """Nest quality metrics under the #722 report's projection section."""
    attached = dict(report)
    projection = dict(attached.get("projection") or {})
    projection["quality"] = quality
    attached["projection"] = projection
    return attached
