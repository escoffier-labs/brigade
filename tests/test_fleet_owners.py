"""Ownership matcher: pure attribution of live items to charters."""

from __future__ import annotations

from brigade.fleet_owners import Holding, attribute


def _match(**selectors):
    record = {
        "nodes": [],
        "harnesses": [],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": False,
    }
    record.update(selectors)
    return record


def _item(kind="claim", ref="item-1", **fields):
    return Holding(
        kind=kind,
        ref=ref,
        repo="example/repo",
        node_label="node-a",
        detail="",
        since="2026-01-01T00:00:00+00:00",
        **fields,
    )


def test_single_selector_attributes_an_item_to_one_owner():
    charters = {
        "alpha": {"match": _match(harnesses=["claude"])},
        "beta": {"match": _match(harnesses=["codex"])},
    }
    item = _item(harness="claude")
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"alpha": [item], "beta": []}
    assert unassigned == [] and overlaps == []


def test_selectors_are_ored_within_a_record():
    charters = {"alpha": {"match": _match(nodes=["node-a"], adapters=["t3-fleet"])}}
    by_node = _item(ref="by-node", nodes=("node-a",))
    by_adapter = _item(kind="delegation", ref="by-adapter", nodes=("node-z",), adapter="t3-fleet")
    neither = _item(ref="neither", nodes=("node-z",))
    by_owner, unassigned, _ = attribute(charters, [by_node, by_adapter, neither])
    assert by_owner["alpha"] == [by_node, by_adapter]
    assert unassigned == [neither]


def test_each_selector_key_matches_its_own_field():
    charters = {
        "conductors": {"match": _match(conductors=["orch"])},
        "sessions": {"match": _match(session_harnesses=["dot"])},
    }
    claim = _item(ref="c", conductor="orch")
    session = _item(kind="session", ref="s", session_harness="dot")
    other = _item(ref="o", harness="dot")
    by_owner, unassigned, _ = attribute(charters, [claim, session, other])
    assert by_owner == {"conductors": [claim], "sessions": [session]}
    assert unassigned == [other]


def test_grokbot_jobs_flag_matches_only_job_items():
    charters = {"grokbot": {"match": _match(grokbot_jobs=True)}}
    job = _item(kind="grokbot-job", ref="job-1")
    claim = _item(kind="claim", ref="claim-1")
    by_owner, unassigned, _ = attribute(charters, [job, claim])
    assert by_owner["grokbot"] == [job]
    assert unassigned == [claim]


def test_a_node_selector_matches_either_end_of_a_delegation():
    charters = {"origin": {"match": _match(nodes=["node-a"])}, "target": {"match": _match(nodes=["node-b"])}}
    delegation = _item(kind="delegation", ref="del-1", nodes=("node-a", "node-b"))
    by_owner, _, overlaps = attribute(charters, [delegation])
    assert by_owner == {"origin": [delegation], "target": [delegation]}
    assert [(overlap.item.ref, overlap.owners) for overlap in overlaps] == [("del-1", ("origin", "target"))]


def test_overlap_lists_the_item_under_both_owners_once_in_overlaps():
    charters = {
        "beta": {"match": _match(harnesses=["claude"])},
        "alpha": {"match": _match(nodes=["node-a"])},
    }
    item = _item(harness="claude", nodes=("node-a",))
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"beta": [item], "alpha": [item]}
    assert unassigned == []
    assert len(overlaps) == 1 and overlaps[0].owners == ("alpha", "beta")


def test_unmatched_items_are_unassigned_and_empty_fields_never_match():
    charters = {"alpha": {"match": _match(harnesses=["claude"])}}
    blank = _item(ref="blank")
    other = _item(ref="other", harness="codex")
    by_owner, unassigned, overlaps = attribute(charters, [blank, other])
    assert by_owner == {"alpha": []}
    assert unassigned == [blank, other]
    assert overlaps == []


def test_a_charter_without_selectors_stays_listed_and_matches_nothing():
    charters = {"quiet": {"label": "Quiet"}, "empty": {"match": _match()}}
    item = _item(harness="claude", nodes=("node-a",))
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"quiet": [], "empty": []}
    assert unassigned == [item] and overlaps == []


def test_public_shape_omits_selector_fields():
    item = _item(harness="claude", nodes=("node-a",), conductor="orch")
    assert item.public() == {
        "kind": "claim",
        "ref": "item-1",
        "repo": "example/repo",
        "node_label": "node-a",
        "detail": "",
        "since": "2026-01-01T00:00:00+00:00",
    }
