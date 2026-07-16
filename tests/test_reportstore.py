"""Direct tests for the shared report-bundle lifecycle primitives."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import reportstore

EVIDENCE = "EVIDENCE.json"


def _bundle(tmp_path: Path, name: str, payload: dict) -> Path:
    bundle_dir = tmp_path / name
    bundle_dir.mkdir(parents=True)
    (bundle_dir / EVIDENCE).write_text(json.dumps(payload))
    return bundle_dir


def test_bundle_json_path_dir_appends_evidence_name(tmp_path: Path):
    assert reportstore.bundle_json_path(tmp_path, EVIDENCE) == tmp_path / EVIDENCE


def test_bundle_json_path_file_passes_through(tmp_path: Path):
    file_path = tmp_path / "evidence.json"
    file_path.write_text("{}")
    assert reportstore.bundle_json_path(file_path, EVIDENCE) == file_path


def test_read_bundle_defaults_path_to_bundle_dir(tmp_path: Path):
    bundle_dir = _bundle(tmp_path, "b1", {"report_id": "b1"})
    payload = reportstore.read_bundle(bundle_dir, EVIDENCE)
    assert payload is not None
    assert payload["path"] == str(bundle_dir)


def test_read_bundle_preserves_existing_path(tmp_path: Path):
    bundle_dir = _bundle(tmp_path, "b1", {"report_id": "b1", "path": "/elsewhere"})
    payload = reportstore.read_bundle(bundle_dir, EVIDENCE)
    assert payload is not None
    assert payload["path"] == "/elsewhere"


def test_read_bundle_missing_returns_none(tmp_path: Path):
    assert reportstore.read_bundle(tmp_path / "absent", EVIDENCE) is None


def _read(child: Path) -> dict | None:
    return reportstore.read_bundle(child, EVIDENCE)


def test_list_bundles_sorts_newest_first(tmp_path: Path):
    _bundle(tmp_path, "old", {"report_id": "old", "created_at": "2026-01-01"})
    _bundle(tmp_path, "new", {"report_id": "new", "created_at": "2026-06-01"})
    bundles = reportstore.list_bundles([tmp_path], _read, id_field="report_id")
    assert [b["report_id"] for b in bundles] == ["new", "old"]


def test_latest_bundles_sorts_newest_first_when_dir_names_diverge(tmp_path: Path):
    _bundle(tmp_path, "old", {"report_id": "old", "created_at": "2026-01-01"})
    _bundle(tmp_path, "new", {"report_id": "new", "created_at": "2026-06-01"})
    bundles = reportstore.latest_bundles([tmp_path], _read, id_field="report_id", limit=1)
    assert [b["report_id"] for b in bundles] == ["new"]


def test_latest_bundles_limit_preserves_newest_first_order(tmp_path: Path):
    _bundle(tmp_path, "old", {"report_id": "old", "created_at": "2026-01-01"})
    _bundle(tmp_path, "mid", {"report_id": "mid", "created_at": "2026-03-01"})
    _bundle(tmp_path, "new", {"report_id": "new", "created_at": "2026-06-01"})
    bundles = reportstore.latest_bundles([tmp_path], _read, id_field="report_id", limit=2)
    assert [b["report_id"] for b in bundles] == ["new", "mid"]


def test_latest_bundles_timestamp_prefixed_decodes_only_limit_payloads(tmp_path: Path):
    decode_count = 0

    def counting_read(child: Path) -> dict | None:
        nonlocal decode_count
        decode_count += 1
        return _read(child)

    history_count = 30
    limit = 2
    for index in range(history_count):
        minute = index % 60
        hour = 12 + index // 60
        report_id = f"20260716-{hour:02d}{minute:02d}00-operator-report-{index:03d}"
        created_at = f"2026-07-16T{hour:02d}:{minute:02d}:00+00:00"
        _bundle(tmp_path, report_id, {"report_id": report_id, "created_at": created_at})

    bundles = reportstore.latest_bundles([tmp_path], counting_read, id_field="report_id", limit=limit)

    assert decode_count == limit
    assert len(bundles) == limit
    assert bundles[0]["report_id"].endswith(f"-{history_count - 1:03d}")


def test_latest_bundles_rejects_long_timestamp_like_prefix_for_fast_path(tmp_path: Path):
    _bundle(
        tmp_path,
        "20260716-1200009-older",
        {"report_id": "older", "created_at": "2026-01-01T00:00:00+00:00"},
    )
    _bundle(
        tmp_path,
        "20260716-120000-newer",
        {"report_id": "newer", "created_at": "2026-06-01T00:00:00+00:00"},
    )

    bundles = reportstore.latest_bundles([tmp_path], _read, id_field="report_id", limit=1)

    assert [bundle["report_id"] for bundle in bundles] == ["newer"]


def test_list_bundles_skips_missing_roots_and_skip_child(tmp_path: Path):
    _bundle(tmp_path, "keep", {"report_id": "keep", "created_at": "2026-01-01"})
    _bundle(tmp_path, "drop", {"report_id": "drop", "created_at": "2026-01-02"})
    bundles = reportstore.list_bundles(
        [tmp_path, tmp_path / "absent-root"],
        _read,
        id_field="report_id",
        skip_child=lambda name: name == "drop",
    )
    assert [b["report_id"] for b in bundles] == ["keep"]


def test_resolve_bundle_latest_uses_callable():
    newest = {"report_id": "newest"}
    bundle, error = reportstore.resolve_bundle(
        [], "latest", id_field="report_id", label="report", latest=lambda: newest
    )
    assert error is None
    assert bundle is newest


def test_resolve_bundle_latest_not_found():
    bundle, error = reportstore.resolve_bundle([], "latest", id_field="report_id", label="report", latest=lambda: None)
    assert bundle is None
    assert error == "report not found: latest"


def test_resolve_bundle_unique_prefix():
    bundles = [{"report_id": "abc123"}, {"report_id": "xyz789"}]
    bundle, error = reportstore.resolve_bundle(
        bundles, "abc", id_field="report_id", label="report", latest=lambda: None
    )
    assert error is None
    assert bundle is bundles[0]


def test_resolve_bundle_not_found_message():
    bundle, error = reportstore.resolve_bundle([], "zzz", id_field="report_id", label="report", latest=lambda: None)
    assert bundle is None
    assert error == "report not found: zzz"


def test_resolve_bundle_ambiguous_message():
    bundles = [{"report_id": "abc123"}, {"report_id": "abc456"}]
    bundle, error = reportstore.resolve_bundle(
        bundles, "abc", id_field="report_id", label="report", latest=lambda: None
    )
    assert bundle is None
    assert error == "report id is ambiguous: abc"


def test_write_bundle_writes_evidence_and_documents(tmp_path: Path):
    bundle_dir = tmp_path / "b1"
    bundle_dir.mkdir()
    reportstore.write_bundle(
        bundle_dir,
        {"report_id": "b1"},
        evidence_name=EVIDENCE,
        documents={"REPORT.md": "# report\n"},
    )
    assert json.loads((bundle_dir / EVIDENCE).read_text())["report_id"] == "b1"
    assert (bundle_dir / "REPORT.md").read_text() == "# report\n"


def test_write_closeout_stamps_path(tmp_path: Path):
    closeout_path = reportstore.write_closeout(tmp_path, {"status": "reviewed"})
    assert closeout_path == tmp_path / "CLOSEOUT.json"
    payload = json.loads(closeout_path.read_text())
    assert payload["status"] == "reviewed"
    assert payload["path"] == str(closeout_path)


def test_move_bundle_moves_into_archive(tmp_path: Path):
    source = _bundle(tmp_path, "b1", {"report_id": "b1"})
    archive_root = tmp_path / "archive"
    destination, moved = reportstore.move_bundle(source, archive_root)
    assert moved is True
    assert destination == archive_root / "b1"
    assert not source.exists()
    assert (destination / EVIDENCE).is_file()


def test_move_bundle_refuses_existing_destination(tmp_path: Path):
    source = _bundle(tmp_path, "b1", {"report_id": "b1"})
    archive_root = tmp_path / "archive"
    (archive_root / "b1").mkdir(parents=True)
    destination, moved = reportstore.move_bundle(source, archive_root)
    assert moved is False
    assert destination == archive_root / "b1"
    # The source is left in place untouched.
    assert source.exists()
    assert (source / EVIDENCE).is_file()
