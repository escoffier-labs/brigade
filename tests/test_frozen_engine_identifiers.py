"""Freeze persisted engine contracts before the internal engine rename (R0)."""

import json
from pathlib import Path

import pytest

from brigade import evidence_cmd, receipts_cmd, trust_gate
from brigade.work_cmd import footprint, verification

ROOT = Path(__file__).resolve().parents[1]
# Exact source literals, including quotes and escaped NUL domain separators.
FROZEN_IDENTIFIERS = [
    ("engines/evidence-ledger/internal/app/evidence_cache_auth.go", '"miseledger.evidence.bundle-mac.v1\\x00"'),
    ("engines/evidence-ledger/internal/app/evidence_sanitize.go", '"miseledger.evidence.stub.id.v1\\x00"'),
    ("engines/evidence-ledger/internal/app/evidence_sanitize.go", '"miseledger://evidence/"'),
    ("engines/evidence-ledger/internal/app/app.go", '"miseledger.markdown-export-filename.v1"'),
    ("engines/evidence-ledger/internal/app/app.go", '".miseledger-export-files"'),
    ("engines/evidence-ledger/internal/app/app.go", '".miseledger-export-stage"'),
    ("engines/evidence-ledger/internal/adapter/record.go", '"miseledger.adapter.v1"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", '"miseledger.internal.source_scan.v1"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", '"__miseledger_memory_backup__:%"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", '"miseledger://%s/%s/%s"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", '"miseledger://%s/%s/%s/artifact/%s"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", '"miseledger://%s/%s/%s/link/%s"'),
    ("engines/evidence-ledger/internal/ingest/importer.go", 'SourceSystem: "miseledger"'),
    ("engines/evidence-ledger/internal/ingest/memory.go", '"__miseledger_memory_backup__:"'),
    ("engines/evidence-ledger/internal/ingest/memory.go", '"__miseledger_item_backup__:"'),
    ("engines/evidence-ledger/internal/ingest/provenance.go", '"miseledger:item:"'),
    ("engines/evidence-ledger/internal/ingest/provenance.go", '"miseledger://%s/%s/%s"'),
    ("engines/evidence-ledger/internal/ingest/provenance.go", 'SourceSystem:    "miseledger"'),
    ("src/brigade/trust_gate.py", '"miseledger:item:"'),
    ("src/brigade/receipts_cmd.py", '"miseledger.adapter.v1"'),
    ("src/brigade/receipts_cmd.py", '"miseledger-export-cursor.json"'),
    ("src/brigade/receipts_cmd.py", '"brigade.miseledger_export_result.v1"'),
    ("src/brigade/receipts_cmd.py", '"brigade.miseledger_fleet_export_result.v1"'),
    ("src/brigade/receipts_cmd.py", '"brigade.miseledger_index_result.v1"'),
    ("src/brigade/receipts_cmd.py", '"brigade.miseledger_export_cursor.v1"'),
    ("src/brigade/evidence_cmd.py", '"miseledger-export-cursor.json"'),
    ("src/brigade/memory_cmd.py", '"miseledger://evidence/"'),
    ("src/brigade/work_cmd/verification.py", 'receipt["miseledger_indexing"]'),
    ("src/brigade/work_cmd/footprint.py", '("code_graph_delta", "graphtrail_delta")'),
    ("src/brigade/code_graph_delta.py", 'payload["graphtrail_timeout_seconds"]'),
    ("src/brigade/code_graph_delta.py", 'compact["graphtrail_timeout_seconds"]'),
    ("engines/code-graph/src/query/graph.rs", '"__graphtrail_limit_truncated__"'),
    ("engines/code-graph/src/query/graph.rs", '"__graphtrail_limit_truncated_target__"'),
    ("engines/code-graph/src/query/graph.rs", '"__graphtrail_truncated_{direction_label}__"'),
]


def assert_frozen_literal(root: Path, path: str, literal: str) -> None:
    assert literal.encode() in (root / path).read_bytes(), f"{path}: missing frozen literal {literal!r}"


@pytest.mark.parametrize(("path", "literal"), FROZEN_IDENTIFIERS)
def test_frozen_source_literals(path: str, literal: str) -> None:
    assert_frozen_literal(ROOT, path, literal)


@pytest.mark.parametrize(("path", "literal"), FROZEN_IDENTIFIERS)
def test_changed_frozen_string_fails(tmp_path: Path, path: str, literal: str) -> None:
    copy = tmp_path / path
    copy.parent.mkdir(parents=True, exist_ok=True)
    original = (ROOT / path).read_bytes()
    assert literal.encode() in original
    copy.write_bytes(original.replace(literal.encode(), b"renamed-identifier"))
    with pytest.raises(AssertionError, match="missing frozen literal"):
        assert_frozen_literal(tmp_path, path, literal)


def test_python_frozen_values(tmp_path: Path) -> None:
    assert trust_gate.ITEM_REF_MISELEDGER == "miseledger:item:"
    assert receipts_cmd.MISELEDGER_SCHEMA == "miseledger.adapter.v1"
    assert receipts_cmd.MISELEDGER_EXPORT_RESULT_SCHEMA == "brigade.miseledger_export_result.v1"
    assert receipts_cmd.MISELEDGER_FLEET_EXPORT_RESULT_SCHEMA == "brigade.miseledger_fleet_export_result.v1"
    assert receipts_cmd.MISELEDGER_INDEX_RESULT_SCHEMA == "brigade.miseledger_index_result.v1"
    cursor = tmp_path / ".brigade" / "work" / "miseledger-export-cursor.json"
    assert receipts_cmd._miseledger_cursor_path(tmp_path) == cursor
    assert evidence_cmd._cursor_path(tmp_path) == cursor


def test_signed_receipt_writer_keeps_indexing_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(receipts_cmd, "index_miseledger_receipts", lambda **kwargs: {"status": "ok", "message": "done"})
    receipt: dict[str, object] = {}
    verification._attach_miseledger_indexing(receipt, target=tmp_path, json_output=True)
    assert receipt == {"miseledger_indexing": {"status": "ok"}}


def test_receipt_readers_keep_legacy_delta(tmp_path: Path) -> None:
    delta = {"status": "ok", "graphtrail_timeout_seconds": 17}
    assert footprint.receipt_graph_delta({"graphtrail_delta": delta}) == delta
    (tmp_path / "run.json").write_text(json.dumps({"graphtrail_delta": delta}))
