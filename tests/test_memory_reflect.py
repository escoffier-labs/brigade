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


def evidence(target: Path, ref: str) -> str:
    path = target / ref.removeprefix("receipt:")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return ref


def log(target: Path, path: Path, *items: tuple[str, str, str], identity: str | None = None) -> None:
    ledger = target / LOG_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a") as handle:
        for ident, kind, ts in items:
            handle.write(
                json.dumps(
                    dict(
                        ts=ts,
                        card_id=identity or path.stem,
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
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
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
        assert candidate["evidence_ids"] == [".brigade/receipts/e-new.json"]
        assert candidate["proof_count"] == 1
        assert candidate["contrary_ids"] == []
        assert candidate["tier"] == 0
        assert "--- a/memory/cards/cache.md\n+++ b/memory/cards/cache.md\n" in candidate["diff"]
        assert '+evidence: [".brigade/receipts/e-new.json"]' in candidate["diff"]
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
    log(
        tmp_path,
        left,
        (evidence(tmp_path, ".brigade/receipts/e-against.json"), "opposite-polarity", "2026-10-21T00:00:00Z"),
    )
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    duplicates = [c for c in payload["candidates"] if c["kind"] == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0]["card_ids"] == ["left", "right"]
    assert "would merge" in duplicates[0]["summary"]
    flags = [c for c in payload["candidates"] if c["kind"] == "opposite-polarity"]
    assert len(flags) == 1
    assert flags[0]["contrary_ids"] == [".brigade/receipts/e-against.json"]
    assert flags[0]["tier"] == 2
    assert flags[0]["diff"] == ""
    assert not any(c["kind"] == "reinforce" for c in payload["candidates"])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["outcome", "reinforced", "near-match", "opposite-polarity"])
@pytest.mark.parametrize("ref", [".brigade/receipts/missing.json", "sha256:" + "0" * 64])
def test_unresolved_log_ids_never_create_candidates(tmp_path, capsys, kind, ref):
    path = card(tmp_path, "cache")
    card(tmp_path, "duplicate")
    path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{ref}"]'))
    log(tmp_path, path, (ref, kind, "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert payload["candidates"] == []
    assert snapshot(tmp_path) == before


def test_proof_and_consolidation_use_only_distinct_resolved_log_ids(tmp_path, capsys):
    path = card(tmp_path, "cache")
    ref = evidence(tmp_path, "receipt:.brigade/receipts/passed.json")
    log(
        tmp_path,
        path,
        (ref, "outcome", "2026-10-20T12:00:00Z"),
        (ref, "outcome", "2026-10-20T12:00:00Z"),
        (".brigade/receipts/missing.json", "outcome", "2026-10-21T12:00:00Z"),
    )
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    for candidate in payload["candidates"]:
        assert candidate["evidence_ids"] == [ref.removeprefix("receipt:")]
        assert candidate["proof_count"] == 1
        assert "missing.json" not in candidate["diff"]
    assert "+last_consolidated: 2026-10-20T12:00:00Z" in payload["candidates"][0]["diff"]


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
    assert candidate["evidence_ids"] == [ref.removeprefix("receipt:")]
    assert candidate["proof_count"] == 1
    assert candidate["card_ids"] == ["alpha", "beta"]
    assert cli.main(["memory", "reflect", "run", "--target", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "would merge" in output
    assert command(tmp_path, capsys, "run", "--limit", "0")[0] == 2


@pytest.mark.parametrize("failure", ["ledger", "persist", "card-race"])
def test_stage_failure_records_failed_and_preserves_cards(tmp_path, capsys, monkeypatch, failure):
    from brigade import memory_proposal_io as io

    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "near-match", "2026-10-20T12:00:00Z"))
    if failure == "ledger":
        with (tmp_path / LOG_PATH).open("a") as handle:
            handle.write("{broken\n")
    elif failure == "card-race":
        original_read = io.read_bytes

        def changed_card(candidate, **kwargs):
            if candidate == path:
                raise OSError("file changed during exact read")
            return original_read(candidate, **kwargs)

        monkeypatch.setattr(io, "read_bytes", changed_card)
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
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-cutoff.json"), "near-match", ts))
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
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    assert cli.main(["memory", "reflect", "run", "--target", str(tmp_path), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "--- a/memory/cards/cache.md\n+++ b/memory/cards/cache.md\n" in output
    assert "+proof_count: 1\r\n" in output
    assert "-Body separator" not in output
    assert snapshot(tmp_path) == before


def test_log_path_alias_survives_adoption_of_stable_identity(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-before-id.json"), "outcome", "2026-10-20T12:00:00Z"))
    stable_id = "card-00000000-0000-4000-8000-000000000001"
    path.write_text(path.read_text().replace("topic: cache", f"id: {stable_id}\ntopic: cache"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    for candidate in payload["candidates"]:
        assert candidate["card_ids"] == [stable_id]
        assert candidate["evidence_ids"] == [".brigade/receipts/e-before-id.json"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("scope", [None, "repo", "workspace"])
@pytest.mark.parametrize("collision", ["stable-id", "path-alias"])
def test_identity_collisions_are_checked_across_scopes(tmp_path, capsys, scope, collision):
    stable_id = "card-00000000-0000-4000-8000-000000000001"
    if collision == "stable-id":
        first = card(tmp_path, "first", extra=f"id: {stable_id}\n")
        second = card(tmp_path, "second", scope="workspace", extra=f"id: {stable_id}\n")
        identity = stable_id
    else:
        first = card(tmp_path, "renamed", extra=f'id: {stable_id}\naliases: ["memory/cards/second.md"]\n')
        second = card(tmp_path, "second", scope="workspace")
        identity = None
    ref = evidence(tmp_path, ".brigade/receipts/support.json")
    log(tmp_path, second, (ref, "outcome", "2026-10-20T12:00:00Z"), identity=identity)
    before = snapshot(tmp_path)
    args = ["--scope", scope] if scope else []
    code, payload = command(tmp_path, capsys, "run", "--dry-run", *args)
    assert code == 0
    assert payload["candidates"] == []
    selected = [p for p in (first, second) if scope is None or (p == first) == (scope == "repo")]
    assert payload["skipped_cards"] == sorted(
        [{"path": p.relative_to(tmp_path).as_posix(), "reason": "ambiguous canonical card identity"} for p in selected],
        key=lambda item: item["path"],
    )
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", ["json", "schema"])
def test_malformed_receipt_does_not_block_healthy_history(tmp_path, capsys, invalid):
    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/support.json"), "outcome", "2026-10-20T12:00:00Z"))
    _, healthy = command(tmp_path, capsys, "run")
    _, payload = command(tmp_path, capsys, "run")
    receipt = tmp_path / payload["receipt_path"]
    data = json.loads(receipt.read_text())
    del data["status"]
    receipt.write_text("{broken" if invalid == "json" else json.dumps(data))
    code, status = command(tmp_path, capsys, "status")
    assert code == 0
    assert status["run_count"] == 1
    assert status["invalid_runs"] == 1
    assert status["latest_run"]["run_id"] == healthy["run_id"]
    assert command(tmp_path, capsys, "candidates")[1]["candidates"] == healthy["candidates"]
    ident = healthy["candidates"][0]["candidate_id"]
    assert command(tmp_path, capsys, "show", ident)[1]["candidate_id"] == ident


def test_pending_matches_are_review_only_and_never_proof(tmp_path, capsys):
    path = card(tmp_path, "cache")
    ids = [f"memory/handoff-inbox/pending-{i}.md" for i in range(3)]
    log(tmp_path, path, *((evidence(tmp_path, ident), "near-match", "2026-10-20T12:00:00Z") for ident in ids))
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
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
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
    log(tmp_path, healthy, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
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


@pytest.mark.parametrize("bad_kind", ["encoding", "oversized"])
def test_unreadable_cards_are_skipped_without_blocking_healthy_cards(tmp_path, capsys, bad_kind):
    from brigade import memory_proposal_io as io

    healthy = card(tmp_path, "healthy")
    ref = evidence(tmp_path, ".brigade/receipts/support.json")
    log(tmp_path, healthy, (ref, "outcome", "2026-10-20T12:00:00Z"))
    broken = card(tmp_path, "broken")
    if bad_kind == "encoding":
        broken.write_bytes(b"---\ntopic: caf\xe9\n---\nClaim.\n")
        reason = "canonical card is not UTF-8"
    else:
        broken.write_bytes(b"x" * (io.MAX_CARD_BYTES + 1))
        reason = "canonical card byte limit exceeded"
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert payload["skipped_cards"] == [{"path": "memory/cards/broken.md", "reason": reason}]
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    assert all(c["card_ids"] == ["healthy"] for c in payload["candidates"])
    assert snapshot(tmp_path) == before


def test_corpus_byte_limit_still_fails_the_run(tmp_path, capsys, monkeypatch):
    from brigade import memory_proposal_io as io

    path = card(tmp_path, "cache")
    monkeypatch.setattr(io, "MAX_CORPUS_BYTES", path.stat().st_size - 1)
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 1
    assert payload["stage"] == "cards"
    assert payload["error"] == "canonical corpus byte limit exceeded"
    assert snapshot(tmp_path) == before


def test_inspection_reads_recent_history_after_directory_limit(tmp_path, capsys):
    root = tmp_path / ".brigade/memory/reflect/runs"
    root.mkdir(parents=True)
    for index in range(1025):
        (root / f"20260101-000000-{index:06d}-12345678").mkdir()
    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
    _, run = command(tmp_path, capsys, "run")
    code, status = command(tmp_path, capsys, "status")
    assert code == 0
    assert status["latest_run"]["run_id"] == run["run_id"]
    assert command(tmp_path, capsys, "candidates")[1]["candidates"] == run["candidates"]
    ident = run["candidates"][0]["candidate_id"]
    assert command(tmp_path, capsys, "show", ident)[1]["candidate_id"] == ident


@pytest.mark.parametrize(
    "block",
    [
        "evidence:\n  - old",
        "evidence :\n  - old",
        "evidence:\n- old",
        "evidence: [a.md, b.md]",
        'sources: [".brigade/receipts/old.json"]',
        "source: .brigade/receipts/old.json",
        'refs: [".brigade/receipts/old.json"]',
        'links: [".brigade/receipts/old.json"]',
    ],
)
def test_structured_or_alternate_citations_have_no_unsafe_preview(tmp_path, capsys, block):
    path = card(tmp_path, "cache")
    path.write_text(path.read_text().replace("evidence: []", block))
    evidence(tmp_path, ".brigade/receipts/old.json")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
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
    log(
        tmp_path,
        path,
        (evidence(tmp_path, ".claude/memory-handoffs/processed/old.md"), "reinforced", "2026-10-20T12:00:00Z"),
    )
    if new_outcome:
        log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-outcome.json"), "outcome", "2026-10-21T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    if new_outcome:
        candidate = next(c for c in payload["candidates"] if c["kind"] == "reinforce")
        assert candidate["proof_count"] == 2
        assert (
            '+evidence: [".brigade/receipts/e-outcome.json", ".claude/memory-handoffs/old.md", '
            '".claude/memory-handoffs/processed/old.md"]'
        ) in candidate["diff"]
        assert "+proof_count: 2" in candidate["diff"]
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
        assert payload["candidates"][0]["evidence_ids"] == [reference.removeprefix("receipt:")]
        assert payload["candidates"][0]["proof_count"] == 1


def test_limit_reports_omissions_and_prioritizes_stale_evidence(tmp_path, capsys):
    path = card(tmp_path, "cache")
    card(tmp_path, "duplicate")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run", "--limit", "1")
    assert code == 0
    assert payload["total_candidates"] == 3
    assert payload["truncated"] is True
    assert payload["candidates"][0]["kind"] == "stale-newer-evidence"


def test_trailing_slash_card_root_is_accepted(tmp_path, capsys):
    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/e-new.json"), "outcome", "2026-10-20T12:00:00Z"))
    config = tmp_path / ".brigade/memory-care.toml"
    config.write_text('card_roots = ["memory/cards/"]\n')
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert payload["candidates"]


@pytest.mark.parametrize("extra,scope", [("", "workspace"), ("trust_label: verified\n", "repo")])
def test_duplicate_cross_scope_or_mixed_trust_requires_review(tmp_path, capsys, extra, scope):
    left = card(tmp_path, "left", extra="trust_label: reviewed\n")
    card(tmp_path, "right", extra=extra, scope=scope)
    log(tmp_path, left, (evidence(tmp_path, ".brigade/receipts/e-support.json"), "reinforced", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    duplicate = next(c for c in payload["candidates"] if c["kind"] == "duplicate")
    assert duplicate["tier"] == 2
    assert duplicate["diff"] == ""


@pytest.mark.parametrize("bad_kind", ["refs", "permission", "hardlink"])
def test_ineligible_card_does_not_block_healthy_candidates(tmp_path, capsys, monkeypatch, bad_kind):
    from brigade import memory_proposal_io as io

    healthy = card(tmp_path, "healthy", body="A healthy claim.")
    log(tmp_path, healthy, (evidence(tmp_path, ".brigade/receipts/support.json"), "outcome", "2026-10-20T12:00:00Z"))
    broken = card(tmp_path, "broken")
    if bad_kind == "refs":
        refs = [f".brigade/receipts/support-{i}.json" for i in range(65)]
        broken.write_text(broken.read_text().replace("evidence: []", "evidence: " + json.dumps(refs)))
        reason = "card evidence list limit exceeded"
    elif bad_kind == "permission":
        original = io.read_bytes

        def unreadable(path, **kwargs):
            if path == broken:
                raise PermissionError("permission denied")
            return original(path, **kwargs)

        monkeypatch.setattr(io, "read_bytes", unreadable)
        reason = "canonical card is unreadable"
    else:
        os.link(broken, tmp_path / "linked.md")
        reason = "file must be regular and single-link"
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert payload["skipped_cards"] == [{"path": "memory/cards/broken.md", "reason": reason}]
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    assert all(c["card_ids"] == ["healthy"] for c in payload["candidates"])
    assert snapshot(tmp_path) == before


def test_log_evidence_is_bounded_to_newest_distinct_ids_per_card(tmp_path, capsys):
    popular = card(tmp_path, "popular")
    healthy = card(tmp_path, "healthy", body="A healthy claim.")
    refs = [evidence(tmp_path, f".brigade/receipts/support-{i:02d}.json") for i in range(65)]
    # Reverse append order and repeat an id so neither ordering nor row count
    # can stand in for the newest 64 distinct evidence ids.
    items = [(ref, "reinforced", f"2026-10-20T12:{i // 60:02d}:{i % 60:02d}Z") for i, ref in enumerate(refs)]
    log(tmp_path, popular, *reversed(items), items[1])
    log(tmp_path, healthy, (evidence(tmp_path, ".brigade/receipts/healthy.json"), "outcome", "2026-10-20T12:00:00Z"))
    card(tmp_path, "duplicate")
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert payload["skipped_cards"] == []
    duplicate = next(c for c in payload["candidates"] if c["kind"] == "duplicate")
    assert duplicate["evidence_ids"] == refs[1:]
    assert duplicate["proof_count"] == 64
    assert duplicate["tier"] == 0
    assert any(c["card_ids"] == ["healthy"] for c in payload["candidates"])
    assert snapshot(tmp_path) == before


def test_changed_evidence_log_fails_closed_and_records_receipt(tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace

    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/support.json"), "outcome", "2026-10-20T12:00:00Z"))
    original = os.stat

    def changed(name, *args, **kwargs):
        result = original(name, *args, **kwargs)
        if name == "evidence-log.jsonl" and kwargs.get("dir_fd") is not None:
            return SimpleNamespace(
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size + 1,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns,
            )
        return result

    monkeypatch.setattr(os, "stat", changed)
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 1
    assert payload["stage"] == "evidence-log"
    assert payload["error"] == "evidence log changed during reflection; retry the run"
    assert json.loads((tmp_path / payload["receipt_path"]).read_text())["status"] == "failed"
    assert snapshot(tmp_path) == before


def test_stale_opposite_polarity_evidence_has_no_metadata_diff(tmp_path, capsys):
    path = card(tmp_path, "cache")
    ref = evidence(tmp_path, ".brigade/receipts/against.json")
    log(tmp_path, path, (ref, "opposite-polarity", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    stale = next(c for c in payload["candidates"] if c["kind"] == "stale-newer-evidence")
    assert stale["contrary_ids"] == [ref]
    assert stale["proof_count"] == 0
    assert stale["tier"] == 2
    assert stale["diff"] == ""
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("surface", ["run", "show", "summary"])
def test_text_previews_escape_terminal_controls(tmp_path, capsys, surface):
    controls = "Claim with \x1b[31mESC, \x9b31mC1 and \x7fDEL."
    path = card(tmp_path, controls if surface == "summary" else "cache", body=controls)
    logged_card = card(tmp_path, "duplicate", body=controls) if surface == "summary" else path
    kind = "reinforced" if surface == "summary" else "outcome"
    log(tmp_path, logged_card, (evidence(tmp_path, ".brigade/receipts/support.json"), kind, "2026-10-20T12:00:00Z"))
    _, payload = command(tmp_path, capsys, "run")
    before = snapshot(tmp_path)
    args = ["run", "--dry-run"] if surface == "run" else ["show", payload["candidates"][0]["candidate_id"]]
    assert cli.main(["memory", "reflect", *args, "--target", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in output if c not in "\n\r\t")
    assert "\\x1b[31mESC" in output
    assert "\\x9b31mC1" in output
    assert "\\x7fDEL" in output
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("root", [".opencode/memory-handoffs", "writers/custom/inbox"])
def test_writer_and_configured_handoffs_resolve(tmp_path, capsys, root):
    path = card(tmp_path, "cache")
    if root.startswith("writers/"):
        config = tmp_path / ".brigade/handoff-sources.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"sources": [{"root": "writers", "inboxes": ["custom/inbox"]}]}))
    ref = evidence(tmp_path, f"{root}/processed/change.md")
    log(tmp_path, path, (ref, "near-match", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert [c["kind"] for c in payload["candidates"]] == ["stale-newer-evidence"]
    assert payload["candidates"][0]["review_evidence_ids"] == [ref]


@pytest.mark.parametrize("kind", ["near-match", "opposite-polarity"])
@pytest.mark.parametrize("reviewed", ["consolidated", "reviewed", "cited"])
def test_reviewed_matches_no_longer_block_support(tmp_path, capsys, kind, reviewed):
    extra = "last_consolidated: 2026-10-19T12:00:00Z\n" if reviewed == "consolidated" else ""
    path = card(tmp_path, "cache", extra=extra)
    old = evidence(tmp_path, "memory/handoff-inbox/reviewed.md")
    if reviewed == "reviewed":
        path.write_text(path.read_text().replace("last_reviewed: 2026-10-01", "last_reviewed: 2026-10-19"))
    elif reviewed == "cited":
        path.write_text(path.read_text().replace("evidence: []", f'evidence: ["{old}"]'))
    support = evidence(tmp_path, ".brigade/receipts/new.json")
    log(tmp_path, path, (old, kind, "2026-10-18T12:00:00Z"), (support, "outcome", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    for candidate in payload["candidates"]:
        assert candidate["contrary_ids"] == candidate["review_evidence_ids"] == []
        assert candidate["tier"] == 0
        assert candidate["diff"]
        assert candidate["proof_count"] == len(candidate["evidence_ids"])


def test_deep_trust_json_does_not_block_healthy_cards(tmp_path, capsys):
    healthy = card(tmp_path, "healthy", body="A healthy claim.")
    deep = '{"a":' * 12000 + '"trusted"' + "}" * 12000
    malformed = card(tmp_path, "deep", extra=f"trust: {deep}\n")
    ref = evidence(tmp_path, ".brigade/receipts/support.json")
    for path in (healthy, malformed):
        log(tmp_path, path, (ref, "outcome", "2026-10-20T12:00:00Z"))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert any(c["card_ids"] == ["healthy"] for c in payload["candidates"])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", ["json", "digest", "count"])
def test_corrupt_candidates_do_not_block_healthy_history(tmp_path, capsys, invalid):
    path = card(tmp_path, "cache")
    log(tmp_path, path, (evidence(tmp_path, ".brigade/receipts/support.json"), "outcome", "2026-10-20T12:00:00Z"))
    _, healthy = command(tmp_path, capsys, "run")
    _, newest = command(tmp_path, capsys, "run")
    retained = tmp_path / newest["candidates_path"]
    lines = retained.read_text().splitlines()
    if invalid == "json":
        retained.write_text("{broken\n")
    elif invalid == "count":
        retained.write_text(lines[0] + "\n")
    else:
        candidate = json.loads(lines[0])
        candidate["summary"] = "Corrupt digest."
        retained.write_text("\n".join([json.dumps(candidate), *lines[1:]]) + "\n")
    code, payload = command(tmp_path, capsys, "candidates")
    assert code == 0
    assert payload["run_id"] == healthy["run_id"]
    assert payload["candidates"] == healthy["candidates"]
    assert payload["invalid_runs"] == 1
    ident = healthy["candidates"][0]["candidate_id"]
    assert command(tmp_path, capsys, "show", ident)[1]["candidate_id"] == ident


def test_proof_records_sources_per_id_and_counts_distinct_roots(tmp_path, capsys):
    path = card(tmp_path, "cache")
    refs = [".brigade/receipts/one.json", ".brigade/receipts/two.json", ".codex/memory-handoffs/processed/three.md"]
    pending = evidence(tmp_path, ".opencode/memory-handoffs/processed/pending.md")
    log(tmp_path, path, *((evidence(tmp_path, ref), "outcome", "2026-10-20T12:00:00Z") for ref in refs))
    log(tmp_path, path, (refs[0], "outcome", "2026-10-20T12:00:00Z"), (pending, "near-match", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    for candidate in payload["candidates"]:
        assert candidate["proof_count"] == 3
        assert candidate["proof_sources"] == 2
        assert candidate["evidence_sources"] == {
            ref: ref.rsplit("/processed/", 1)[0] if "/processed/" in ref else ".brigade/receipts"
            for ref in candidate["evidence_ids"]
        }


@pytest.mark.parametrize("kind", ["near-match", "opposite-polarity"])
def test_new_support_for_same_id_does_not_revive_reviewed_match(tmp_path, capsys, kind):
    path = card(tmp_path, "cache", extra="last_consolidated: 2026-10-19\n")
    ref = evidence(tmp_path, "memory/handoff-inbox/reviewed.md")
    log(tmp_path, path, (ref, kind, "2026-10-18T12:00:00Z"), (ref, "outcome", "2026-10-20T12:00:00Z"))
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert {c["kind"] for c in payload["candidates"]} == {"stale-newer-evidence", "reinforce"}
    assert all(c["tier"] == 0 for c in payload["candidates"])


@pytest.mark.parametrize("origin", ["card", "log"])
@pytest.mark.parametrize("bundle", [False, True])
def test_evidence_aliases_have_one_canonical_proof_id(tmp_path, capsys, monkeypatch, origin, bundle):
    path = card(tmp_path, "left")
    card(tmp_path, "right")
    if bundle:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        evidence(tmp_path, "cache/miseledger/evidence/example.json")
        canonical = "miseledger:example"
        refs = [canonical, "miseledger://evidence/example", canonical + "#step-2"]
    else:
        canonical = evidence(tmp_path, ".brigade/receipts/a.json")
        refs = [canonical, "receipt:" + canonical, canonical + "#step-2"]
    if origin == "card":
        path.write_text(path.read_text().replace("evidence: []", "evidence: " + json.dumps(refs)))
    else:
        log(tmp_path, path, *((ref, "outcome", "2026-10-20T12:00:00Z") for ref in refs))
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert payload["candidates"]
    for candidate in payload["candidates"]:
        assert candidate["proof_count"] == 1
        assert candidate["evidence_ids"] == [canonical]
        assert set(candidate["evidence_sources"]) == {canonical}
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["near-match", "opposite-polarity"])
def test_deleted_pending_evidence_still_blocks_metadata_previews(tmp_path, capsys, kind):
    path = card(tmp_path, "left")
    card(tmp_path, "right")
    missing = evidence(tmp_path, "memory/handoff-inbox/pending.md")
    support = evidence(tmp_path, ".brigade/receipts/support.json")
    log(tmp_path, path, (missing, kind, "2026-10-19T12:00:00Z"), (support, "outcome", "2026-10-20T12:00:00Z"))
    (tmp_path / missing).unlink()
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert {"stale-newer-evidence", "reinforce", "duplicate"} <= {c["kind"] for c in payload["candidates"]}
    for candidate in payload["candidates"]:
        assert candidate["tier"] == 2
        assert candidate["diff"] == ""
        field = "contrary_ids" if kind == "opposite-polarity" else "review_evidence_ids"
        assert candidate[field] == [missing]
        assert candidate["unresolved_evidence_ids"] == [missing]
        assert candidate["evidence_ids"] == [support]
        assert candidate["proof_count"] == 1
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("failed", [False, True])
def test_run_retention_caps_history_and_preserves_latest_success(tmp_path, capsys, failed):
    from datetime import datetime, timedelta, timezone

    from brigade import memory_proposal_io as io
    from brigade import memory_reflect as reflect

    now = datetime.now(timezone.utc)
    root = tmp_path / reflect.ROOT

    def retained(age, status):
        started = now - timedelta(days=age)
        ident = started.strftime("%Y%m%d-%H%M%S-%f") + "-12345678"
        run = root / ident
        io.write_exclusive(
            run / "receipt.json",
            io.json_bytes(
                {"schema": "brigade.memory-reflect-run.v1", "run_id": ident, "status": status, "candidate_count": 0}
            ),
        )
        io.write_exclusive(run / "candidates.jsonl", b"")
        return run

    old_success = retained(40, "completed")
    old_failure = retained(45, "failed")
    recent = [retained(index / 24, "failed") for index in range(1, 56)]
    orphan = root / ((now - timedelta(days=60)).strftime("%Y%m%d-%H%M%S-%f") + "-12345678")
    orphan.mkdir()
    if failed:
        (tmp_path / LOG_PATH).write_text("{broken\n")
    before = snapshot(tmp_path)
    code, payload = command(tmp_path, capsys, "run", "--dry-run")
    assert code == int(failed)
    assert len(list(root.iterdir())) == 50
    assert not old_failure.exists()
    assert not orphan.exists()
    assert old_success.exists() == failed
    assert all(path.exists() for path in recent[: 48 if failed else 49])
    assert all(not path.exists() for path in recent[48 if failed else 49 :])
    assert (tmp_path / payload["receipt_path"]).exists()
    assert snapshot(tmp_path) == before


def test_run_retention_expires_history_with_fewer_than_fifty_runs(tmp_path, capsys):
    from datetime import datetime, timedelta, timezone

    from brigade import memory_proposal_io as io
    from brigade import memory_reflect as reflect

    root = tmp_path / reflect.ROOT
    old = datetime.now(timezone.utc) - timedelta(days=31)
    ident = old.strftime("%Y%m%d-%H%M%S-%f") + "-12345678"
    io.write_exclusive(
        root / ident / "receipt.json",
        io.json_bytes(
            {"schema": "brigade.memory-reflect-run.v1", "run_id": ident, "status": "completed", "candidate_count": 0}
        ),
    )
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert [path.name for path in root.iterdir()] == [payload["run_id"]]


def test_retention_leaves_runs_started_during_pruning(tmp_path, capsys, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from brigade import memory_reflect as reflect

    root = tmp_path / reflect.ROOT
    concurrent_id = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y%m%d-%H%M%S-%f") + "-12345678"
    concurrent = root / concurrent_id
    original = os.scandir
    scans = 0

    def arriving_run(path):
        nonlocal scans
        if isinstance(path, int):
            scans += 1
            if scans == 2:
                concurrent.mkdir()
        return original(path)

    monkeypatch.setattr(os, "scandir", arriving_run)
    code, payload = command(tmp_path, capsys, "run")
    assert code == 0
    assert concurrent.is_dir()
    assert (tmp_path / payload["receipt_path"]).exists()
