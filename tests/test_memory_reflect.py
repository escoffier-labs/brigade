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
    return {
        p.relative_to(target).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (target / "memory/cards").rglob("*")
        if p.is_file()
    }


def test_run_persists_deterministic_candidates_and_metadata_diffs(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    (path.parent / "attachment.bin").write_bytes(b"\x00\xff")
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


@pytest.mark.parametrize("failure", ["ledger", "persist"])
def test_stage_failure_records_failed_and_preserves_cards(tmp_path, capsys, monkeypatch, failure):
    from brigade import memory_proposal_io as io

    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "near-match", "2026-10-20T12:00:00Z"))
    if failure == "ledger":
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
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    assert cli.main(["memory", "reflect", "run", "--target", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "--- a/memory/cards/cache.md\n+++ b/memory/cards/cache.md\n" in output
    assert "+proof_count: 1\r\n" in output
    assert "-Body separator" not in output
    assert snapshot(tmp_path) == before


def test_log_path_alias_survives_adoption_of_stable_identity(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-before-id", "outcome", "2026-10-20T12:00:00Z"))
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


def test_pending_matches_are_review_only_and_never_proof(tmp_path, capsys):
    path = card(tmp_path, "cache")
    ids = [f"memory/handoff-inbox/pending-{i}.md" for i in range(3)]
    log(tmp_path, path, *((ident, "near-match", "2026-10-20T12:00:00Z") for ident in ids))
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert [c["kind"] for c in payload["candidates"]] == ["stale-newer-evidence"]
    candidate = payload["candidates"][0]
    assert candidate["tier"] == 2
    assert candidate["proof_count"] == 0
    assert candidate["review_evidence_ids"] == ids
    assert candidate["diff"] == ""


@pytest.mark.parametrize("malformed", [False, True])
def test_log_scan_window_is_informational_but_malformed_rows_fail(tmp_path, capsys, malformed):
    from brigade.memory_evidence_log import MAX_SCAN_BYTES

    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    ledger = tmp_path / LOG_PATH
    tail = ledger.read_bytes() + (b"{broken\n" if malformed else b"")
    ledger.write_bytes(b"x" * (MAX_SCAN_BYTES + 10) + b"\n" + tail)
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert payload["evidence_log"]["scan_window_truncated"] is True
    assert code == (1 if malformed else 0)
    assert payload["status"] == ("failed" if malformed else "completed")
    assert snapshot(tmp_path) == before


def test_bad_cards_are_reported_without_blocking_healthy_cards(tmp_path, capsys):
    healthy = card(tmp_path, "healthy")
    log(tmp_path, healthy, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    broken = card(tmp_path, "notes")
    broken.write_text("Notes without frontmatter.\n")
    unclosed = card(tmp_path, "unclosed")
    unclosed.write_text("---\nbroken frontmatter\n")
    for name in ("duplicate-a", "duplicate-b"):
        path = card(tmp_path, name)
        path.write_text(path.read_text().replace(f"topic: {name}", "topic: shared"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert {item["path"] for item in payload["skipped_cards"]} == {
        "memory/cards/notes.md",
        "memory/cards/unclosed.md",
        "memory/cards/duplicate-a.md",
        "memory/cards/duplicate-b.md",
    }
    assert all(c["card_ids"] == ["healthy"] for c in payload["candidates"])
    assert payload["candidates"]
    assert snapshot(tmp_path) == before
    code, scoped = command(tmp_path, capsys, "run", "--scope", "workspace")
    assert code == 0
    assert scoped["skipped_cards"] == []


def test_inspection_reads_recent_history_after_directory_limit(tmp_path, capsys):
    root = tmp_path / ".brigade/memory/reflect/runs"
    root.mkdir(parents=True)
    for index in range(1025):
        (root / f"20260101-000000-{index:06d}-12345678").mkdir()
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    _, run = command(tmp_path, capsys, "run")
    code, status = command(tmp_path, capsys, "status")
    assert code == 0
    assert status["latest_run"]["run_id"] == run["run_id"]
    assert command(tmp_path, capsys, "candidates")[1]["candidates"] == run["candidates"]
    ident = run["candidates"][0]["candidate_id"]
    assert command(tmp_path, capsys, "show", ident)[1]["candidate_id"] == ident


@pytest.mark.parametrize("block", ["evidence:\n  - old", "evidence :\n  - old", "evidence:\n- old"])
def test_block_list_metadata_has_no_unsafe_preview(tmp_path, capsys, block):
    path = card(tmp_path, "cache")
    path.write_text(path.read_text().replace("evidence: []", block))
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert payload["candidates"]
    assert all(c["tier"] == 2 and c["diff"] == "" for c in payload["candidates"])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("new_outcome", [False, True])
def test_exact_reinforcement_is_already_cited(tmp_path, capsys, new_outcome):
    path = card(tmp_path, "cache")
    path.write_text(path.read_text().replace("evidence: []", 'evidence: [".claude/memory-handoffs/old.md"]'))
    log(tmp_path, path, (".claude/memory-handoffs/processed/old.md", "reinforced", "2026-10-20T12:00:00Z"))
    if new_outcome:
        log(tmp_path, path, ("e-outcome", "outcome", "2026-10-21T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    if new_outcome:
        candidate = next(c for c in payload["candidates"] if c["kind"] == "reinforce")
        assert candidate["proof_count"] == 2
        assert '+evidence: [".claude/memory-handoffs/old.md", "e-outcome"]' in candidate["diff"]
    else:
        assert payload["candidates"] == []


@pytest.mark.parametrize(
    "reference,valid",
    [
        ("README.md", False),
        ("receipt:.brigade/work/verify-runs/example/receipt.json", True),
        (".claude/memory-handoffs/example.md", True),
        ("miseledger:example", True),
    ],
)
def test_only_evidence_items_resolve_without_size_limit(tmp_path, capsys, monkeypatch, reference, valid):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if reference.startswith("miseledger:"):
        evidence = tmp_path / "cache/miseledger/evidence/example.json"
    else:
        evidence = tmp_path / reference.removeprefix("receipt:")
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(b"example" if not valid else b"x" * (4 * 1024 * 1024 + 1))
    for name in ("left", "right"):
        path = card(tmp_path, name)
        path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{reference}"]'))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert bool(payload["candidates"]) == valid
    if valid:
        assert payload["candidates"][0]["evidence_ids"] == [reference]
        assert payload["candidates"][0]["proof_count"] == 1


def test_limit_reports_omissions_and_prioritizes_stale_evidence(tmp_path, capsys):
    path = card(tmp_path, "cache")
    card(tmp_path, "duplicate")
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run", "--limit", "1")
    assert code == 0
    assert payload["total_candidates"] == 3
    assert payload["truncated"] is True
    assert payload["candidates"][0]["kind"] == "stale-newer-evidence"


def test_trailing_slash_card_root_is_accepted(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, ("e-new", "outcome", "2026-10-20T12:00:00Z"))
    config = tmp_path / ".brigade/memory-care.toml"
    config.write_text('card_roots = ["memory/cards/"]\n')
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert payload["candidates"]


@pytest.mark.parametrize("extra,scope", [("", "workspace"), ("trust_label: verified\n", "repo")])
def test_duplicate_cross_scope_or_mixed_trust_requires_review(tmp_path, capsys, extra, scope):
    left = card(tmp_path, "left", extra="trust_label: reviewed\n")
    card(tmp_path, "right", extra=extra, scope=scope)
    log(tmp_path, left, ("e-support", "reinforced", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    duplicate = next(c for c in payload["candidates"] if c["kind"] == "duplicate")
    assert duplicate["tier"] == 2
    assert duplicate["diff"] == ""
