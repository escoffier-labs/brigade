"""Deterministic reflect previews never write canonical cards."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from brigade import cli
from brigade.memory_evidence_log import LOG_PATH

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


def card(target: Path, name: str, *, body="Keep the cache warm.", extra="", scope="repo") -> Path:
    path = target / f"memory/cards/{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntopic: {name}\nscope: {scope}\nlast_reviewed: 2026-10-01\nevidence: []\n{extra}---\n{body}\n"
    )
    return path


def log(target: Path, path: Path, *items: tuple[str, str, str]) -> None:
    ledger = target / LOG_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as handle:
        for ident, kind, ts in items:
            handle.write(
                json.dumps(
                    dict(
                        ts=ts,
                        card_id=path.stem,
                        card_path=path.relative_to(target).as_posix(),
                        evidence_id=ident,
                        kind=kind,
                        similarity=0.95,
                    )
                )
                + "\n"
            )


def command(target, capsys, *args):
    code = cli.main(["memory", "reflect", *args, "--target", str(target), "--json"])
    output = capsys.readouterr()
    return code, json.loads(output.out)


def snapshot(target):
    return {p.relative_to(target).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns) for p in target.rglob("*.md")}


def test_run_persists_deterministic_candidates_and_metadata_diffs(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "near-match", "2026-10-20T12:00:00Z"))
    log(tmp_path, path, ("e-new", "near-match", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    code, first = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert first["status"] == "completed"
    candidates = {c["kind"]: c for c in first["candidates"]}
    assert set(candidates) == {"stale-newer-evidence", "reinforce"}
    for candidate in candidates.values():
        assert candidate["schema"] == "brigade.memory-reflect-candidate.v1"
        assert candidate["card_ids"] == ["cache"]
        assert candidate["base_sha256"] == {"cache": hashlib.sha256(path.read_bytes()).hexdigest()}
        assert candidate["evidence_ids"] == ["e-new"]
        assert candidate["proof_count"] == 1
        assert candidate["contrary_ids"] == []
        assert candidate["tier"] == 0
        assert "--- a/memory/cards/cache.md\n+++ b/memory/cards/cache.md\n" in candidate["diff"]
        assert '+evidence: ["e-new"]' in candidate["diff"]
        assert "+proof_count: 1" in candidate["diff"]
    assert "+last_consolidated: 2026-10-20T12:00:00Z" in candidates["stale-newer-evidence"]["diff"]
    code, second = command(tmp_path, capsys, "run")
    assert code == 0
    assert second["candidates"] == first["candidates"]
    assert snapshot(tmp_path) == before
    stored = tmp_path / second["candidates_path"]
    assert [json.loads(line) for line in stored.read_text().splitlines()] == second["candidates"]
    assert stored.stat().st_mode & 0o777 == 0o600
    assert command(tmp_path, capsys, "status")[1]["latest_run"]["run_id"] == second["run_id"]
    assert command(tmp_path, capsys, "candidates")[1]["candidates"] == second["candidates"]
    ident = second["candidates"][0]["candidate_id"]
    assert command(tmp_path, capsys, "show", ident)[1]["candidate_id"] == ident
    assert command(tmp_path, capsys, "show", "../escape")[0] == 2


def test_resolution_duplicates_and_polarity_flags(tmp_path, capsys):
    left = card(tmp_path, "left")
    card(tmp_path, "right", body="KEEP the cache warm!")
    card(tmp_path, "uncited-a", body="Another claim.")
    card(tmp_path, "uncited-b", body="Another claim.")
    log(tmp_path, left, ("e-against", "opposite-polarity", "2026-10-21T00:00:00Z"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    duplicates = [c for c in payload["candidates"] if c["kind"] == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["card_ids"] == ["left", "right"]
    assert "would merge" in duplicates[0]["summary"]
    flags = [c for c in payload["candidates"] if c["kind"] == "opposite-polarity"]
    assert len(flags) == 1
    assert flags[0]["contrary_ids"] == ["e-against"]
    assert flags[0]["tier"] == 2
    assert flags[0]["diff"] == ""
    assert not any(c["kind"] == "reinforce" for c in payload["candidates"])
    assert snapshot(tmp_path) == before


def test_receipt_evidence_scope_limit_and_human_output(tmp_path, capsys):
    receipt = tmp_path / ".brigade/work/verify-runs/example/receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"passed"}\n')
    ref = "receipt:.brigade/work/verify-runs/example/receipt.json"
    for name in ("alpha", "beta"):
        path = card(tmp_path, name, scope="workspace")
        path.write_text(
            path.read_text().replace(
                "evidence: []", f'evidence: ["{ref}", "missing", "https://invalid.example/source"]'
            )
        )
    card(tmp_path, "repo-only")
    code, payload = command(tmp_path, capsys, "run", "--scope", "workspace", "--limit", "1")
    assert code == 0
    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert candidate["kind"] == "duplicate"
    assert candidate["evidence_ids"] == [ref]
    assert candidate["proof_count"] == 1
    assert candidate["card_ids"] == ["alpha", "beta"]
    assert cli.main(["memory", "reflect", "run", "--target", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "would merge" in output
    assert command(tmp_path, capsys, "run", "--limit", "0")[0] == 2


@pytest.mark.parametrize("failure", ["malformed-card", "ledger", "persist"])
def test_stage_failure_records_failed_and_preserves_cards(tmp_path, capsys, monkeypatch, failure):
    from brigade import memory_proposal_io as io

    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "near-match", "2026-10-20T12:00:00Z"))
    if failure == "malformed-card":
        path.write_text("---\nbroken frontmatter\n")
    elif failure == "ledger":
        with (tmp_path / LOG_PATH).open("a") as handle:
            handle.write("{broken\n")
    else:
        original = io.write_exclusive

        def fail_candidates(path, data):
            if path.name == "candidates.jsonl":
                raise OSError("candidate write failed")
            original(path, data)

        monkeypatch.setattr(io, "write_exclusive", fail_candidates)
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 1
    assert payload["status"] == "failed"
    assert payload["error"]
    assert snapshot(tmp_path) == before
    receipt = json.loads((tmp_path / payload["receipt_path"]).read_text())
    assert receipt["status"] == "failed"
    assert receipt["stage"]
    assert command(tmp_path, capsys, "candidates")[1]["candidates"] == []


@pytest.mark.parametrize(
    "extra,ts,stale",
    [
        ("", "2026-10-01T23:59:00Z", False),
        ("", "2026-09-30T23:59:00Z", False),
        ("last_consolidated: 2026-10-20\n", "2026-10-20T23:59:00Z", False),
        ("last_consolidated: 2026-10-20T12:00:00Z\n", "2026-10-20T12:00:01Z", True),
    ],
)
def test_staleness_uses_consolidation_cutoff_not_card_age(tmp_path, capsys, extra, ts, stale):
    path = card(tmp_path, "cache", extra=extra)
    log(tmp_path, path, ("e-cutoff", "near-match", ts))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert any(c["kind"] == "stale-newer-evidence" for c in payload["candidates"]) == stale


def test_status_ignores_unfinished_run_without_receipt(tmp_path, capsys):
    unfinished = tmp_path / ".brigade/memory/reflect/runs/20261001-000000-000000-12345678"
    unfinished.mkdir(parents=True)
    code, payload = command(tmp_path, capsys, "status")
    assert code == 0
    assert payload["latest_run"] is None


@pytest.mark.parametrize("root", [".brigade", "."])
def test_overlapping_card_root_refuses_without_writing_under_it(tmp_path, capsys, root):
    config = tmp_path / ".brigade/memory-care.toml"
    config.parent.mkdir()
    config.write_text(f'card_roots = ["{root}"]\n')
    before = sorted(tmp_path.rglob("*"))
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 2
    assert payload["status"] == "refused"
    assert "overlaps" in payload["error"]
    assert sorted(tmp_path.rglob("*")) == before


def test_human_dry_run_prints_diff_and_preserves_crlf_body(tmp_path, capsys):
    path = card(tmp_path, "cache", body="Keep the cache warm.\n\n---\nBody separator.")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    log(tmp_path, path, ("e-new", "near-match", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    assert cli.main(["memory", "reflect", "run", "--target", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "--- a/memory/cards/cache.md\n+++ b/memory/cards/cache.md\n" in output
    assert "+proof_count: 1\r\n" in output
    assert "-Body separator" not in output
    assert snapshot(tmp_path) == before


def test_log_path_alias_survives_adoption_of_stable_identity(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-before-id", "near-match", "2026-10-20T12:00:00Z"))
    stable_id = "card-00000000-0000-4000-8000-000000000001"
    path.write_text(path.read_text().replace("topic: cache", f"id: {stable_id}\ntopic: cache"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    for candidate in payload["candidates"]:
        assert candidate["card_ids"] == [stable_id]
        assert candidate["evidence_ids"] == ["e-before-id"]
    assert snapshot(tmp_path) == before


def test_malformed_receipt_refuses_inspection_without_crashing(tmp_path, capsys):
    _, payload = command(tmp_path, capsys, "run")
    receipt = tmp_path / payload["receipt_path"]
    data = json.loads(receipt.read_text())
    del data["status"]
    receipt.write_text(json.dumps(data))
    code, result = command(tmp_path, capsys, "candidates")
    assert code == 2
    assert result["status"] == "refused"
