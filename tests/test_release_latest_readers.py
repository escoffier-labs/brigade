import json
from pathlib import Path

from brigade.release_cmd import evidence


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_latest_candidate_decodes_only_newest_timestamp_prefixed_bundle(tmp_path: Path, monkeypatch):
    root = tmp_path / ".brigade" / "release" / "candidates"
    for index in range(5):
        candidate_id = f"20260805-1{index}0000-candidate-{index:03d}"
        _write(
            root / candidate_id / "EVIDENCE.json",
            {"candidate_id": candidate_id, "created_at": f"2026-08-05T1{index}:00:00+00:00"},
        )
    _write(root / "archive" / "20260901-000000-candidate-old" / "EVIDENCE.json", {"candidate_id": "archived"})
    decoded: list[str] = []
    original = evidence._read_candidate

    def counting_read(path: Path):
        decoded.append(path.name)
        return original(path)

    monkeypatch.setattr(evidence, "_read_candidate", counting_read)

    latest = evidence._latest_candidate(tmp_path)

    assert latest is not None
    assert latest["candidate_id"] == "20260805-140000-candidate-004"
    assert decoded == ["20260805-140000-candidate-004"]


def test_latest_release_receipt_decodes_only_newest_timestamp_prefixed_run(tmp_path: Path, monkeypatch):
    root = tmp_path / ".brigade" / "release" / "runs"
    for index in range(4):
        run_id = f"20260805-1{index}0000-release-{index:03d}"
        _write(root / run_id / "receipt.json", {"run_id": run_id, "started_at": f"2026-08-05T1{index}:00:00+00:00"})
    decoded: list[str] = []
    original = evidence._read_release_receipt

    def counting_read(path: Path):
        decoded.append(path.name)
        return original(path)

    monkeypatch.setattr(evidence, "_read_release_receipt", counting_read)

    latest = evidence._latest_release_receipt(tmp_path)

    assert latest is not None
    assert latest["run_id"] == "20260805-130000-release-003"
    assert decoded == ["20260805-130000-release-003"]


def test_latest_release_receipt_skips_unreadable_newest_run(tmp_path: Path):
    root = tmp_path / ".brigade" / "release" / "runs"
    _write(root / "20260805-100000-release-good" / "receipt.json", {"run_id": "good"})
    broken = root / "20260805-110000-release-broken"
    broken.mkdir(parents=True)
    (broken / "receipt.json").write_text("{not json", encoding="utf-8")

    latest = evidence._latest_release_receipt(tmp_path)

    assert latest is not None
    assert latest["run_id"] == "good"


def test_latest_release_receipt_falls_back_to_started_at_for_other_dir_names(tmp_path: Path):
    root = tmp_path / ".brigade" / "release" / "runs"
    _write(root / "zeta" / "receipt.json", {"run_id": "zeta", "started_at": "2026-01-01T00:00:00+00:00"})
    _write(root / "alpha" / "receipt.json", {"run_id": "alpha", "started_at": "2026-06-01T00:00:00+00:00"})

    latest = evidence._latest_release_receipt(tmp_path)

    assert latest is not None
    assert latest["run_id"] == "alpha"


def test_latest_release_receipt_without_runs_root(tmp_path: Path):
    assert evidence._latest_release_receipt(tmp_path) is None
