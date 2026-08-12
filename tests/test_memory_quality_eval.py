"""Read-only memory quality evaluation for issue #845."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from brigade.memory_retrieval_eval.quality import DEFAULT_QUALITY_ROOT, evaluate_memory_quality


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_quality_fixture_report_is_deterministic_and_read_only():
    before = _tree_digest(DEFAULT_QUALITY_ROOT)
    first = evaluate_memory_quality(DEFAULT_QUALITY_ROOT)
    second = evaluate_memory_quality(DEFAULT_QUALITY_ROOT)

    assert before == _tree_digest(DEFAULT_QUALITY_ROOT)
    assert first == second
    assert first["schema"] == "brigade.memory-quality-eval.v1"
    assert first["issue"] == 845
    assert first["as_of"] == "2026-08-12"
    assert first["read_only"] is True
    assert first["stale_recall"]["violation_count"] == 1
    assert first["stale_recall"]["rate"] == 0.25
    assert first["projection_drift"]["divergent_count"] == 1
    assert first["projection_drift"]["rate"] == 0.5
    assert first["scope_leakage"]["violation_count"] == 1
    assert first["scope_leakage"]["rate"] == 0.25
    assert first["contract_wishes"]["retrieval_explanations"]["available"] is False
    json.dumps(first, sort_keys=True)


def test_quality_report_emits_diffable_violation_evidence():
    report = evaluate_memory_quality(DEFAULT_QUALITY_ROOT)
    stale = report["stale_recall"]["violations"][0]
    assert stale == {
        "card_id": "retired-endpoint",
        "fresh_until": "2026-01-31",
        "query_id": "q-stale",
        "rank": 1,
    }
    drift = report["projection_drift"]["violations"][0]
    assert drift["card_id"] == "deployment-policy"
    assert drift["canonical_hash"].startswith("sha256:")
    assert drift["projection_hash"] == "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    leak = report["scope_leakage"]["violations"][0]
    assert leak["card_id"] == "retired-endpoint"
    assert leak["dimension"] == "repository"
    assert leak["declared"] == "repo-alpha"
    assert leak["requested"] == "repo-beta"


def test_missing_external_contracts_are_wishes_not_fabricated_metrics(tmp_path: Path):
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "one.md").write_text("---\nid: one\nfresh_until: 2027-01-01\n---\nOne\n", encoding="utf-8")
    (tmp_path / "eval.json").write_text(
        json.dumps({"schema": "brigade.memory-quality-input.v1", "as_of": "2026-08-12", "retrievals": []}),
        encoding="utf-8",
    )
    report = evaluate_memory_quality(tmp_path)
    assert report["projection_drift"]["available"] is False
    assert report["scope_leakage"]["available"] is False
    assert report["contract_wishes"]["projection_items"]["available"] is False
