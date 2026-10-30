"""Public diagnostic contracts and compatibility with reviewed reflection."""

import json

import pytest

from brigade import cli, memory_reflect
from brigade import memory_proposal_v2_reflect as reflect_proposal
from tests.test_memory_negative_outcomes import failed_card as failed_card
from tests.test_memory_outcome_evidence import verified_card as verified_card
from tests.test_memory_reflect import card, evidence, log


def prepared(target, *, anchored=True):
    path = card(target, "cache", body="Keep the cache warm. ^c-123abc" if anchored else "Keep the cache warm.")
    refs = [
        evidence(target, ref)
        for ref in (".brigade/receipts/a.json", ".brigade/receipts/b.json", "memory/evidence/c.json")
    ]
    log(target, path, *((ref, "outcome", "2026-10-20T12:00:00Z") for ref in refs))
    run = memory_reflect.run_payload(target=target)
    candidate = next(c for c in run["candidates"] if c["kind"] == "reinforce")
    return path, candidate


def policy(target, **values):
    path = target / "policy.json"
    path.write_text(
        json.dumps(dict(schema="brigade.memory-tier0-preflight-policy.v1", enabled_classes=["reinforce"], **values))
    )
    return path.name


def tree(target):
    return {
        str(p.relative_to(target)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
        for p in target.rglob("*")
    }


def test_translation_is_nonpersistent_and_matches_reviewed_ops(tmp_path):
    _, candidate = prepared(tmp_path)
    before = tree(tmp_path)
    packet = reflect_proposal.translation_payload(target=tmp_path, candidate_id=candidate["candidate_id"])
    assert tree(tmp_path) == before
    reviewed = reflect_proposal.propose_payload(
        target=tmp_path, candidate_id=candidate["candidate_id"], reason="Review evidence."
    )
    assert packet["ops"] == reviewed["ops"]
    assert packet["expected_source_sha256"] == {s["path"]: s["sha256"] for s in reviewed["sources"]}
    assert packet["run_id"] == reviewed["reflect"]["run_id"]


def test_preflight_missing_policy_defaults_deny(tmp_path, capsys):
    _, candidate = prepared(tmp_path)
    before = tree(tmp_path)
    assert (
        cli.main(["memory", "reflect", "preflight", candidate["candidate_id"], "--target", str(tmp_path), "--json"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "held"
    assert report["eligible"] is False
    assert report["diagnostic_only"] is True
    assert "policy_missing" in {c["code"] for c in report["checks"]}
    assert tree(tmp_path) == before


def evaluate(target, candidate, **kwargs):
    from brigade.memory_tier0_preflight import preflight_payload

    return preflight_payload(target=target, candidate_id=candidate["candidate_id"], **kwargs)


def codes(report):
    return {check["code"] for check in report["checks"]}


def retained_change(target, candidate, **changes):
    """Model same-UID retained-state tampering with a self-consistent digest."""
    from brigade import memory_proposal_io as io

    updated = {k: v for k, v in candidate.items() if k != "candidate_id"} | changes
    updated["candidate_id"] = "candidate-" + io.sha256(io.json_bytes(updated))[:24]
    for path in (target / memory_reflect.ROOT).glob("*/candidates.jsonl"):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        if candidate in rows:
            path.write_text("".join(json.dumps(updated if row == candidate else row) + "\n" for row in rows))
            return updated
    raise AssertionError("retained candidate not found")


@pytest.mark.parametrize(
    "changes",
    [
        dict(card_paths=[]),
        dict(card_paths="memory/cards/cache.md"),
        dict(card_paths={"cache": []}),
        dict(card_paths={"wrong": "memory/cards/cache.md"}),
        dict(base_sha256=[]),
        dict(base_sha256={"cache": "not-a-digest"}),
        dict(evidence_sources=[]),
        dict(kind=[]),
        dict(proof_count=True),
    ],
)
def test_preflight_malformed_retained_mapping_is_sanitized(tmp_path, capsys, changes):
    _, candidate = prepared(tmp_path)
    candidate = retained_change(tmp_path, candidate, **changes)
    before = tree(tmp_path)
    assert (
        cli.main(["memory", "reflect", "preflight", candidate["candidate_id"], "--target", str(tmp_path), "--json"])
        == 2
    )
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert "candidate_invalid" in codes(report)
    assert report["evaluation_sha256"] is None
    assert not output.err
    assert tree(tmp_path) == before


@pytest.mark.parametrize("kind", ["opposite-polarity", "near-match"])
def test_preflight_review_evidence_does_not_satisfy_proof_floors(tmp_path, kind):
    path = card(tmp_path, "cache", body="Keep the cache warm. ^c-123abc")
    refs = [
        evidence(tmp_path, ref)
        for ref in (".brigade/receipts/a.json", ".brigade/receipts/b.json", "memory/evidence/c.json")
    ]
    log(tmp_path, path, *((ref, kind, "2026-10-20T12:00:00Z") for ref in refs))
    candidate = next(
        c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "stale-newer-evidence"
    )
    assert candidate["proof_count"] == 0
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert {"evidence_floor_unmet", "source_group_floor_unmet"} <= codes(report)
    assert "evidence_floor_satisfied" not in codes(report)
    assert "source_group_floor_satisfied" not in codes(report)
    assert tree(tmp_path) == before


@pytest.mark.parametrize("counter", ["proof_count", "proof_sources"])
def test_preflight_inconsistent_retained_proof_is_not_satisfied(tmp_path, counter):
    _, candidate = prepared(tmp_path)
    candidate = retained_change(tmp_path, candidate, **{counter: 0})
    report = evaluate(tmp_path, candidate, policy_path=policy(tmp_path))
    assert "candidate_proof_mismatch" in codes(report)
    assert all(
        check["state"] != "satisfied"
        for check in report["checks"]
        if check["gate"] in ("evidence_floor", "source_group_floor")
    )
    assert report["evaluation_sha256"] is None


@pytest.mark.parametrize("extra", ["missing", "outside-root", "window"])
def test_preflight_incomplete_ledger_binding_preserves_diagnostics(tmp_path, extra):
    path, candidate = prepared(tmp_path)
    if extra == "window":
        refs = [evidence(tmp_path, f".brigade/receipts/new-{n}.json") for n in range(64)]
    else:
        refs = [".brigade/receipts/deleted.json" if extra == "missing" else "docs/note.json"]
    log(tmp_path, path, *((ref, "outcome", "2026-10-21T12:00:00Z") for ref in refs))
    name = policy(tmp_path)
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate, policy_path=name)
    assert report["status"] == "held"
    assert {"binding_incomplete", "delta_satisfied", "evidence_floor_satisfied"} <= codes(report)
    assert report["evaluation_sha256"] is None
    assert len(report["evidence_sha256"]) <= 64
    assert not any(c["gate"] == "snapshot" and c["state"] == "satisfied" for c in report["checks"])
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    "ref",
    [
        "../outside.json",
        "/tmp/example-evidence.json",
        r"memory\evidence\note.json",
        "https://example.invalid/note",
        "memory/evidence/" + "x" * 1025,
    ],
)
def test_preflight_unaddressable_citation_is_not_opened(tmp_path, capsys, monkeypatch, ref):
    from brigade import memory_proposal_io as io

    path, _ = prepared(tmp_path)
    path.write_text(path.read_text().replace("evidence: []", "evidence: " + ref))
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    name = policy(tmp_path)
    read = io.read_bytes

    def bounded_read(path, **kwargs):
        assert path.is_relative_to(tmp_path), "unaddressable citation must not be read"
        return read(path, **kwargs)

    monkeypatch.setattr(io, "read_bytes", bounded_read)
    before = tree(tmp_path)
    assert (
        cli.main(
            [
                "memory",
                "reflect",
                "preflight",
                candidate["candidate_id"],
                "--target",
                str(tmp_path),
                "--policy",
                name,
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "held"
    assert {"binding_incomplete", "delta_satisfied", "evidence_floor_satisfied"} <= codes(report)
    assert not any(c["gate"] == "snapshot" and c["state"] == "satisfied" for c in report["checks"])
    assert report["evaluation_sha256"] is None
    assert ref not in report["evidence_sha256"]
    assert tree(tmp_path) == before


def test_preflight_missing_support_replaces_candidate_success(tmp_path):
    _, candidate = prepared(tmp_path)
    assert "candidate_satisfied" in codes(evaluate(tmp_path, candidate))
    (tmp_path / candidate["evidence_ids"][0]).unlink()
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert "candidate_proof_mismatch" in codes(report)
    assert not any(
        c["gate"] in ("candidate", "snapshot", "evidence_floor", "source_group_floor") and c["state"] == "satisfied"
        for c in report["checks"]
    )
    assert report["evaluation_sha256"] is None
    assert tree(tmp_path) == before


@pytest.mark.parametrize("verified_card", ["anchored-linked"], indirect=True)
def test_preflight_invalid_current_receipt_replaces_candidate_success(tmp_path, verified_card, capsys):
    from tests.test_memory_outcome_evidence import capture

    path, receipt = verified_card
    assert capture(tmp_path, receipt) == 0
    capsys.readouterr()
    refs = [evidence(tmp_path, ref) for ref in (".brigade/receipts/a.json", "memory/evidence/b.json")]
    log(tmp_path, path, *((ref, "outcome", "2026-10-20T12:00:00Z") for ref in refs))
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    assert candidate["proof_count"] == 3
    assert "candidate_satisfied" in codes(evaluate(tmp_path, candidate))
    (tmp_path / f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json").write_text("{}")
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert {"delta_invalid", "candidate_proof_mismatch"} <= codes(report)
    assert not any(
        c["gate"] in ("candidate", "evidence_floor", "source_group_floor") and c["state"] == "satisfied"
        for c in report["checks"]
    )
    assert report["evaluation_sha256"] is None
    assert tree(tmp_path) == before


def test_preflight_skips_unrelated_corrupt_newer_run(tmp_path):
    _, candidate = prepared(tmp_path)
    older = memory_reflect.inspect_payload(target=tmp_path, command="status")["latest_run"]["run_id"]
    newer = memory_reflect.run_payload(target=tmp_path)["run_id"]
    assert newer > older
    path = tmp_path / memory_reflect.ROOT / newer / "candidates.jsonl"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert report["run_id"] == older
    assert "delta_satisfied" in codes(report)
    assert report["status"] == "held"
    assert tree(tmp_path) == before


@pytest.mark.parametrize("missing", ["target", "source"])
def test_preflight_missing_inputs_have_specific_reasons(tmp_path, missing):
    path, candidate = prepared(tmp_path)
    if missing == "target":
        tmp_path = tmp_path / "absent"
    else:
        path.unlink()
    report = evaluate(tmp_path, candidate)
    assert report["status"] == "refused"
    assert ("target_invalid" if missing == "target" else "source_invalid") in codes(report)
    assert "card_trust_denied" not in codes(report)


def test_preflight_counts_never_grant_authority(tmp_path):
    _, candidate = prepared(tmp_path)
    name = policy(tmp_path)
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate, policy_path=name)
    assert report["status"] == "held"
    assert {"delta_satisfied", "evidence_floor_satisfied", "source_group_floor_satisfied"} <= codes(report)
    assert {
        "evidence_authority_unbound",
        "owner_authority_unbound",
        "retrieval_eval_unbound",
        "cooldown_history_unbound",
    } <= codes(report)
    assert report["eligible"] is False
    assert report["delta_sha256"] and report["evaluation_sha256"]
    assert report["changed_path_count"] == 1
    assert evaluate(tmp_path, candidate, policy_path=name) == report
    assert tree(tmp_path) == before
    assert "Keep the cache" not in json.dumps(report)


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"schema":"wrong"}',
        '{"schema":"brigade.memory-tier0-preflight-policy.v1","schema":"brigade.memory-tier0-preflight-policy.v1"}',
        *[
            json.dumps(dict(schema="brigade.memory-tier0-preflight-policy.v1", **values))
            for values in (
                dict(owner_ok=True),
                dict(minimum_evidence_ids=True),
                dict(minimum_evidence_ids=3.5),
                dict(minimum_evidence_ids=2),
                dict(minimum_evidence_ids=65),
                dict(minimum_source_groups=1),
                dict(max_changed_paths=0),
                dict(cooldown_seconds=0),
                dict(enabled_classes=["unknown"]),
                dict(enabled_classes=["reinforce", "reinforce"]),
                dict(enabled_classes="reinforce"),
            )
        ],
        "[" * 2000,
        "x" * 17000,
    ],
)
def test_preflight_strict_policy_rejects_invalid_inputs(tmp_path, raw):
    _, candidate = prepared(tmp_path)
    (tmp_path / "policy.json").write_text(raw)
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate, policy_path="policy.json")
    assert report["status"] == "refused"
    assert "policy_invalid" in codes(report)
    assert report["evaluation_sha256"] is None
    assert tree(tmp_path) == before


def test_preflight_text_and_json_explain_holds(tmp_path, capsys):
    _, candidate = prepared(tmp_path)
    for suffix in ([], ["--json"]):
        assert (
            cli.main(["memory", "reflect", "preflight", candidate["candidate_id"], "--target", str(tmp_path), *suffix])
            == 0
        )
        text = capsys.readouterr().out
        assert "owner_authority_unbound" in text
        assert "Keep the cache" not in text
    assert cli.main(["memory", "reflect", "preflight", "bad\x1bSECRET", "--target", str(tmp_path), "--json"]) == 2
    text = capsys.readouterr().out
    assert "SECRET" not in text


@pytest.mark.parametrize("anchored", [False, True])
def test_preflight_uses_strict_live_composition(tmp_path, anchored):
    _, candidate = prepared(tmp_path, anchored=anchored)
    report = evaluate(tmp_path, candidate, policy_path=policy(tmp_path))
    assert ("delta_satisfied" in codes(report)) is anchored
    if not anchored:
        assert "anchoring_requires_review" in codes(report)


@pytest.mark.parametrize("cutoff", ["2026-10-01", "invalid", None])
@pytest.mark.parametrize("late", [False, True])
def test_preflight_retained_positive_preserves_pending_negative_fences(tmp_path, cutoff, late):
    path, candidate = prepared(tmp_path)
    if cutoff != "2026-10-01":
        text = path.read_text().replace(
            "last_reviewed: 2026-10-01\n", "" if cutoff is None else "last_reviewed: invalid\n"
        )
        path.write_text(text)
        run = memory_reflect.run_payload(target=tmp_path)
        candidate = next(c for c in run["candidates"] if c["kind"] == "reinforce")
    log(tmp_path, path, (".brigade/work/verify-runs/failed/receipt.json", "negative-outcome", "2026-10-21T12:00:00Z"))
    if late:
        log(
            tmp_path,
            path,
            *((f".brigade/receipts/new-{n}.json", "reinforced", "2026-10-22T12:00:00Z") for n in range(70)),
        )
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert "delta_satisfied" not in codes(report)
    assert not report["eligible"]
    assert tree(tmp_path) == before


@pytest.mark.parametrize("mutation", ["policy", "source", "evidence", "log", "config"])
def test_preflight_hashes_bind_exact_snapshot(tmp_path, mutation):
    path, candidate = prepared(tmp_path)
    name = policy(tmp_path)
    before = evaluate(tmp_path, candidate, policy_path=name)
    if mutation == "policy":
        path = tmp_path / name
    elif mutation == "evidence":
        path = tmp_path / ".brigade/receipts/a.json"
    elif mutation == "log":
        from brigade.memory_evidence_log import LOG_PATH

        path = tmp_path / LOG_PATH
    elif mutation == "config":
        path = tmp_path / ".brigade/config.json"
        path.write_text("{}")
    path.write_text(path.read_text() + "\n")
    after = evaluate(tmp_path, candidate, policy_path=name)
    assert after["evaluation_sha256"] != before["evaluation_sha256"]


def test_preflight_detects_final_snapshot_drift(tmp_path, monkeypatch):
    from brigade import memory_tier0_preflight as preflight

    path, candidate = prepared(tmp_path)
    original = preflight.translation_payload

    def replace(**kwargs):
        packet = original(**kwargs)
        path.write_text(path.read_text() + "Changed.\n")
        return packet

    monkeypatch.setattr(preflight, "translation_payload", replace)
    report = evaluate(tmp_path, candidate)
    assert "input_drift" in codes(report)
    assert report["evaluation_sha256"] is None
    assert "delta_satisfied" not in {c["code"] for c in report["checks"] if c["state"] == "satisfied"}


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "parent", "oversize", "traversal"])
def test_preflight_no_follow_and_resource_bounds(tmp_path, kind):
    import os

    _, candidate = prepared(tmp_path)
    name = policy(tmp_path)
    path = tmp_path / name
    if kind in ("symlink", "hardlink"):
        path.rename(tmp_path / "other.json")
        if kind == "symlink":
            path.symlink_to("other.json")
        else:
            os.link(tmp_path / "other.json", path)
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif kind == "parent":
        (tmp_path / "alias").symlink_to(".bia", target_is_directory=True)
        name = "alias/policy.json"
    elif kind == "oversize":
        path.write_bytes(b"x" * 16385)
    else:
        name = "../policy.json"
    report = evaluate(tmp_path, candidate, policy_path=name)
    assert report["status"] == "refused"
    assert "policy_invalid" in codes(report)


def test_preflight_evidence_aggregate_budget_precedes_composition(tmp_path, monkeypatch):
    from brigade import memory_tier0_preflight as preflight

    _, candidate = prepared(tmp_path)
    monkeypatch.setattr(preflight.io, "MAX_CORPUS_BYTES", 200)
    for ref in candidate["evidence_ids"]:
        (tmp_path / ref).write_bytes(b"x" * 150)

    def forbidden(**kwargs):
        pytest.fail("composition must not run above aggregate budget")

    monkeypatch.setattr(preflight, "translation_payload", forbidden)
    report = evaluate(tmp_path, candidate)
    assert "binding_incomplete" in codes(report)
    assert report["evaluation_sha256"] is None


def test_preflight_truncated_log_has_explicit_window_hold(tmp_path):
    from brigade.memory_evidence_log import LOG_PATH, MAX_SCAN_BYTES

    _, candidate = prepared(tmp_path)
    (tmp_path / LOG_PATH).write_bytes(b" " * (MAX_SCAN_BYTES + 1))
    report = evaluate(tmp_path, candidate)
    assert "evidence_window_incomplete" in codes(report)
    assert report["evaluation_sha256"] is None


def test_preflight_rejects_duplicate_ledger_fields(tmp_path):
    from brigade.memory_evidence_log import LOG_PATH

    _, candidate = prepared(tmp_path)
    path = tmp_path / LOG_PATH
    raw = path.read_text()
    path.write_text(raw.replace('"kind": "outcome"', '"kind": "near-match", "kind": "outcome"'))
    report = evaluate(tmp_path, candidate)
    assert report["status"] == "refused"
    assert "delta_satisfied" not in codes(report)


@pytest.mark.parametrize("same", [True, False])
def test_preflight_duplicate_strict_delta_differs_from_normalized_grouping(tmp_path, same):
    left = card(tmp_path, "left", body="Keep the cache warm. ^c-123abc")
    ref = evidence(tmp_path, ".brigade/receipts/duplicate.json")
    log(tmp_path, left, (ref, "outcome", "2026-10-20T12:00:00Z"))
    card(tmp_path, "right", body="Keep the cache warm. ^c-456def" if same else "KEEP the cache warm! ^c-456def")
    run = memory_reflect.run_payload(target=tmp_path)
    candidate = next(c for c in run["candidates"] if c["kind"] == "duplicate")
    report = evaluate(tmp_path, candidate)
    assert ("delta_requires_review" if same else "delta_invalid") in codes(report)
    assert "delta_satisfied" not in codes(report)


def test_preflight_refuses_dropped_generated_op(tmp_path, monkeypatch):
    from brigade import memory_tier0_preflight as preflight

    _, candidate = prepared(tmp_path)
    translate = preflight.translation_payload

    def invalid(**kwargs):
        packet = translate(**kwargs)
        packet["ops"].append(dict(type="unknown"))
        return packet

    monkeypatch.setattr(preflight, "translation_payload", invalid)
    report = evaluate(tmp_path, candidate)
    assert "delta_invalid" in codes(report)


def test_preflight_empty_policy_allowlist_holds(tmp_path):
    _, candidate = prepared(tmp_path)
    (tmp_path / "policy.json").write_text(json.dumps(dict(schema="brigade.memory-tier0-preflight-policy.v1")))
    report = evaluate(tmp_path, candidate, policy_path="policy.json")
    assert "class_disabled" in codes(report)
    assert report["eligible"] is False


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "receipt",
        "linked",
        "card",
        "outside-window",
        "missing-cutoff",
        "invalid-cutoff",
        "conflict",
        "missing",
        "invalid",
    ],
)
def test_preflight_live_negative_receipt_fences(tmp_path, failed_card, capsys, drift):
    from brigade.memory_evidence_log import LOG_PATH
    from tests.test_memory_outcome_evidence import capture, drift_support

    path, receipt, rerun = failed_card
    if drift in ("missing-cutoff", "invalid-cutoff"):
        path.write_text(
            path.read_text().replace(
                "last_reviewed: 2026-10-01\n", "" if drift == "missing-cutoff" else "last_reviewed: invalid\n"
            )
        )
        receipt = rerun()
    ref = evidence(tmp_path, ".brigade/receipts/healthy.json")
    log(tmp_path, path, (ref, "outcome", "2026-10-19T12:00:00Z"))
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    (tmp_path / LOG_PATH).chmod(0o600)
    assert capture(tmp_path, receipt) == 0
    capsys.readouterr()
    if drift in ("receipt", "linked"):
        drift_support(tmp_path, path, receipt, drift)
    elif drift == "card":
        path.write_text(path.read_text() + "Changed claim.\n")
    elif drift == "outside-window":
        log(
            tmp_path,
            path,
            *(
                (evidence(tmp_path, f".brigade/receipts/recent-{n}.json"), "outcome", "2026-10-22T12:00:00Z")
                for n in range(64)
            ),
        )
    elif drift == "conflict":
        log(
            tmp_path,
            path,
            (f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json", "negative-outcome", "2026-10-21T12:00:00Z"),
        )
    elif drift in ("missing", "invalid"):
        receipt_path = tmp_path / f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
        if drift == "missing":
            receipt_path.unlink()
        else:
            receipt_path.write_text("{}")
    before = tree(tmp_path)
    report = evaluate(tmp_path, candidate)
    assert "delta_satisfied" not in codes(report)
    assert not report["eligible"]
    assert report["evaluation_sha256"] is None
    assert tree(tmp_path) == before


@pytest.mark.parametrize("nested", [False, True])
def test_preflight_card_trust_and_host_fences(tmp_path, nested):
    from brigade import memory_cmd
    from tests.test_memory_proposals import declared_sources

    path = card(tmp_path, "current", body="Keep the cache warm. ^c-123abc")
    card(tmp_path, "previous", body="Another claim. ^c-456def")
    declared_sources((tmp_path, None), nested=nested)
    ref = evidence(tmp_path, ".brigade/receipts/current.json")
    log(tmp_path, path, (ref, "outcome", "2026-10-20T12:00:00Z"))
    candidate = next(c for c in memory_reflect.run_payload(target=tmp_path)["candidates"] if c["kind"] == "reinforce")
    name = policy(tmp_path)
    report = evaluate(tmp_path, candidate, policy_path=name)
    assert "card_trust_satisfied" in codes(report)
    assert "owner_authority_unbound" in codes(report)
    config = memory_cmd.config_path(tmp_path)
    config.write_text('read_only_paths = ["memory/cards/*.md"]\n')
    assert "path_read_only" in codes(evaluate(tmp_path, candidate, policy_path=name))


@pytest.mark.parametrize("location", ["evidence", "card"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "oversize"])
def test_preflight_unsafe_input_never_composes(tmp_path, monkeypatch, location, kind):
    import os
    from brigade import memory_tier0_preflight as preflight

    path, candidate = prepared(tmp_path)
    if location == "evidence":
        path = tmp_path / candidate["evidence_ids"][0]
    other = path.with_name("original.bin")
    path.rename(other)
    if kind == "symlink":
        path.symlink_to(other.name)
    elif kind == "hardlink":
        os.link(other, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x" * (preflight.io.MAX_INPUT_BYTES + 1))

    def forbidden(**kwargs):
        pytest.fail("unsafe input must not reach translation")

    monkeypatch.setattr(preflight, "translation_payload", forbidden)
    report = evaluate(tmp_path, candidate)
    assert report["status"] == "refused"
    assert report["evaluation_sha256"] is None
