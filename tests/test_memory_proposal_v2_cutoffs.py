"""Reviewed consolidation cutoffs follow accepted evidence, independent of op order."""

from __future__ import annotations

import hashlib
import json

import pytest

from brigade import memory_proposal_v2 as v2, memory_proposals as api
from tests.test_memory_proposal_v2 import CLAIM, PATH, apply, create, op, setup
from tests.test_memory_proposal_capabilities import SAFE_DIRECTORY_CAPABILITY
from tests.test_memory_reflect import card, evidence, log

pytestmark = pytest.mark.skipif(not SAFE_DIRECTORY_CAPABILITY, reason="safe directory descriptors unavailable")
OLD = "2026-10-20T11:00:00Z"
NEW = "2026-10-20T12:00:00Z"
EARLY = ".brigade/receipts/early.json"
LATE = ".brigade/receipts/support.json"


def test_original_date_only_cutoff_keeps_entire_day(tmp_path):
    path = setup(tmp_path)
    path.write_text(path.read_text().replace("evidence: []", "evidence: []\nlast_consolidated: 2026-10-20"))
    uncited = evidence(tmp_path, ".brigade/receipts/uncited.json")
    log(tmp_path, path, (LATE, "outcome", NEW), (uncited, "outcome", "2026-10-20T15:00:00Z"))
    proposal = create(tmp_path, [op("append_evidence", evidence_ids=[LATE], last_consolidated=NEW)])
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert api._meta(path.read_text())["last_consolidated"] == "2026-10-20"


@pytest.mark.parametrize("uncited_at", ["2026-10-21T15:00:00Z", "2026-10-22T00:30:00+01:00"])
def test_date_only_support_cannot_hide_uncited_same_day(tmp_path, uncited_at):
    path = setup(tmp_path)
    uncited = evidence(tmp_path, ".brigade/receipts/uncited.json")
    log(tmp_path, path, (LATE, "outcome", "2026-10-21"), (uncited, "outcome", uncited_at))
    before = path.read_bytes()
    with pytest.raises(api.ProposalError, match="no valid"):
        create(tmp_path, [op("append_evidence", evidence_ids=[LATE], last_consolidated="2026-10-21")])
    assert path.read_bytes() == before
    assert not list((tmp_path / ".brigade/memory/proposals").glob("*/revision-1.json"))


def test_date_only_accepted_support_covers_later_same_day_stamps(tmp_path):
    path = setup(tmp_path)
    evidence(tmp_path, EARLY)
    path.write_text(path.read_text().replace("evidence: []", f"evidence: {json.dumps([EARLY, LATE])}"))
    log(tmp_path, path, (EARLY, "outcome", "2026-10-20"), (LATE, "outcome", NEW))
    proposal = create(
        tmp_path,
        [
            op("append_evidence", evidence_ids=[EARLY], last_consolidated="2026-10-20"),
            op("append_evidence", evidence_ids=[LATE], last_consolidated=NEW),
        ],
    )
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert api._meta(path.read_text())["last_consolidated"] == "2026-10-20"


@pytest.mark.parametrize("reverse", [False, True], ids=["old-then-new", "new-then-old"])
def test_accepted_evidence_cutoff_is_order_independent(tmp_path, reverse):
    path = setup(tmp_path)
    evidence(tmp_path, EARLY)
    # The earlier row is already cited, so either individual operation is safe.
    path.write_text(path.read_text().replace("evidence: []", f"evidence: {json.dumps([EARLY])}"))
    log(tmp_path, path, (EARLY, "outcome", OLD), (LATE, "outcome", NEW))
    ops = [
        op("append_evidence", evidence_ids=[EARLY], last_consolidated=OLD),
        op("append_evidence", evidence_ids=[LATE], last_consolidated=NEW),
    ]
    if reverse:
        ops.reverse()
    before = path.read_bytes()
    proposal = create(tmp_path, ops)
    assert proposal["ops"] == ops and proposal["dropped_ops"] == []
    assert proposal["sources"][0]["sha256"] == hashlib.sha256(before).hexdigest()
    assert proposal["op_evidence"] == {ref: hashlib.sha256(b"{}\n").hexdigest() for ref in (EARLY, LATE)}
    assert path.read_bytes() == before
    with pytest.raises(api.ProposalError, match="accepted"):
        api.apply_payload(target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"])
    assert apply(tmp_path, proposal)["status"] == "committed"
    meta = api._meta(path.read_text())
    assert meta["last_consolidated"] == NEW
    assert meta["evidence"] == sorted([EARLY, LATE])
    assert meta["proof_count"] == "2"


@pytest.mark.parametrize("bad", ["hash", "timestamp", "missing"])
def test_dropped_evidence_never_advances_cutoff(tmp_path, bad):
    path = setup(tmp_path)
    future = evidence(tmp_path, ".brigade/receipts/future.json")
    log(tmp_path, path, (LATE, "outcome", NEW), (future, "outcome", "2026-10-21T12:00:00Z"))
    valid = op("append_evidence", evidence_ids=[LATE], last_consolidated=NEW)
    invalid = op("append_evidence", evidence_ids=[future], last_consolidated="2026-10-21T12:00:00Z")
    if bad == "hash":
        invalid["expected_block_hash"] = "0" * 64
    elif bad == "timestamp":
        invalid["last_consolidated"] = "2026-10-22T12:00:00Z"
    else:
        invalid["evidence_ids"] = [".brigade/receipts/missing.json"]
    with pytest.raises(api.ProposalError, match="generated ops were dropped"):
        v2.create_payload(
            target=tmp_path,
            source_paths=[PATH],
            ops=[valid, invalid],
            anchor_blocks=False,
            reason="Inspect evidence.",
            strict_ops=True,
        )
    proposal = create(tmp_path, [valid, invalid])
    assert proposal["ops"] == [valid] and len(proposal["dropped_ops"]) == 1
    assert set(proposal["op_evidence"]) == {LATE}
    assert apply(tmp_path, proposal)["status"] == "committed"
    meta = api._meta(path.read_text())
    assert meta["last_consolidated"] == NEW
    assert meta["evidence"] == [LATE]


def test_cutoff_includes_accepted_append_without_explicit_timestamp(tmp_path):
    path = setup(tmp_path)
    evidence(tmp_path, EARLY)
    log(tmp_path, path, (EARLY, "outcome", OLD), (LATE, "outcome", NEW))
    proposal = create(
        tmp_path,
        [
            op("append_evidence", evidence_ids=[EARLY], last_consolidated=OLD),
            op("append_evidence", evidence_ids=[LATE]),
        ],
    )
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert api._meta(path.read_text())["last_consolidated"] == NEW


def test_cutoffs_are_per_card_and_preserve_original_floor(tmp_path):
    path = setup(tmp_path)
    path.write_text(path.read_text().replace("evidence: []", f"evidence: []\nlast_consolidated: {NEW}"))
    other = card(tmp_path, "other", body=CLAIM)
    evidence(tmp_path, EARLY)
    log(tmp_path, path, (EARLY, "outcome", OLD))
    log(tmp_path, other, (LATE, "outcome", "2026-10-21T12:00:00Z"))
    second = op("append_evidence", evidence_ids=[LATE], last_consolidated="2026-10-21T12:00:00Z")
    second["card_path"] = "memory/cards/other.md"
    proposal = create(
        tmp_path,
        [op("append_evidence", evidence_ids=[EARLY], last_consolidated=OLD), second],
        paths=[PATH, "memory/cards/other.md"],
    )
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert api._meta(path.read_text())["last_consolidated"] == NEW
    assert api._meta(other.read_text())["last_consolidated"] == "2026-10-21T12:00:00Z"


@pytest.mark.parametrize("reviewed", ["2026-10-20", "2026-10-20T10:00:00Z", None])
def test_uncited_pending_fence_retains_review_date_fallback(tmp_path, reviewed):
    path = setup(tmp_path)
    replacement = "" if reviewed is None else f"last_reviewed_at: {reviewed}\n"
    path.write_text(path.read_text().replace("last_reviewed: 2026-10-01\n", replacement))
    log(tmp_path, path, (EARLY, "outcome", "2026-10-21T11:00:00Z"), (LATE, "outcome", "2026-10-21T12:00:00Z"))
    before = path.read_bytes()
    with pytest.raises(api.ProposalError, match="no valid"):
        create(tmp_path, [op("append_evidence", evidence_ids=[LATE], last_consolidated="2026-10-21T12:00:00Z")])
    assert path.read_bytes() == before


@pytest.mark.parametrize("drift", ["source", "receipt"])
def test_cutoff_proposal_refuses_drift_after_review(tmp_path, drift):
    path = setup(tmp_path)
    log(tmp_path, path, (LATE, "outcome", NEW))
    proposal = create(tmp_path, [op("append_evidence", evidence_ids=[LATE], last_consolidated=NEW)])
    values = dict(target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"])
    api.review_payload(**values, reason="Inspect exact cutoff.")
    changed = path if drift == "source" else tmp_path / LATE
    changed.write_text(changed.read_text() + "\n")
    before = path.read_bytes()
    with pytest.raises(api.ProposalError, match="drift"):
        api.apply_payload(**values)
    assert path.read_bytes() == before


def test_combined_support_cannot_hide_uncited_pending_evidence(tmp_path):
    path = setup(tmp_path)
    evidence(tmp_path, EARLY)
    log(
        tmp_path,
        path,
        (EARLY, "outcome", OLD),
        (".brigade/receipts/uncited.json", "outcome", "2026-10-20T11:30:00Z"),
        (LATE, "outcome", NEW),
    )
    before = path.read_bytes()
    with pytest.raises(api.ProposalError, match="hide uncited pending evidence"):
        create(
            tmp_path,
            [
                op("append_evidence", evidence_ids=[EARLY], last_consolidated=OLD),
                op("append_evidence", evidence_ids=[LATE]),
            ],
        )
    assert path.read_bytes() == before
    assert not list((tmp_path / ".brigade/memory/proposals").glob("*/revision-1.json"))


@pytest.mark.parametrize("reviewed", ["2026-10-20", "2026-10-20T12:00:00Z"])
def test_review_metadata_fallback_excludes_historical_rows(tmp_path, reviewed):
    path = setup(tmp_path)
    path.write_text(
        path.read_text().replace(
            "last_reviewed: 2026-10-01\n", f"last_consolidated: invalid\nlast_reviewed_at: {reviewed}\n"
        )
    )
    log(tmp_path, path, (EARLY, "outcome", OLD), (LATE, "outcome", "2026-10-21T12:00:00Z"))
    proposal = create(tmp_path, [op("append_evidence", evidence_ids=[LATE], last_consolidated="2026-10-21T12:00:00Z")])
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert api._meta(path.read_text())["last_consolidated"] == "2026-10-21T12:00:00Z"


def test_accepted_aliases_and_duplicates_retain_one_evidence_identity(tmp_path, monkeypatch):
    path = setup(tmp_path)
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    bundle = cache / "miseledger/evidence/example.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("{}\n")
    canonical, alias = "miseledger:example", "miseledger://evidence/example"
    log(tmp_path, path, (canonical, "outcome", OLD), (alias, "outcome", NEW))
    proposal = create(
        tmp_path,
        [
            op("append_evidence", evidence_ids=[canonical], last_consolidated=NEW),
            op("append_evidence", evidence_ids=[alias, alias], last_consolidated=NEW),
        ],
    )
    assert len(proposal["ops"]) == 2 and proposal["dropped_ops"] == []
    assert proposal["op_evidence"] == {ref: hashlib.sha256(b"{}\n").hexdigest() for ref in (canonical, alias)}
    assert apply(tmp_path, proposal)["status"] == "committed"
    meta = api._meta(path.read_text())
    assert meta["last_consolidated"] == NEW
    assert meta["evidence"] == sorted([canonical, alias])
    assert meta["proof_count"] == "1"
