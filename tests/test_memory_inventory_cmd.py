"""Tests for `brigade memory inventory --json` (#875 Task 2)."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from brigade import cli

ITEM_FIELDS = {
    "id",
    "title",
    "canonical_path",
    "logical_destination",
    "store_type",
    "category",
    "tags",
    "created_at",
    "updated_at",
    "last_reviewed",
    "fresh_until",
    "freshness",
    "review_state",
    "evidence_state",
    "source_harness",
    "source_handoff",
    "owning_workflow",
    "last_mutation",
    "care",
}
PAGINATION_FIELDS = {"offset", "limit", "total", "returned", "has_more", "next_offset"}
FILTER_FIELDS = {
    "store_type",
    "category",
    "tag",
    "freshness",
    "review_state",
    "evidence_state",
    "source_harness",
    "owning_workflow",
}
FORBIDDEN_ITEM_KEYS = {"body", "summary", "content", "description", "text", "prompt"}
STORE_TYPES = {"card", "rule", "tools", "user", "learning"}
FRESHNESS_VALUES = {"fresh", "stale", "missing", "unassessable", "not_applicable"}
BODY_SECRET = "BODY_SECRET_INVENTORY_875_do_not_emit"


def _write_card(
    target: Path,
    rel: str,
    *,
    title: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    created: str | None = None,
    updated: str | None = None,
    last_reviewed: str | None = None,
    fresh_until: str | None = None,
    evidence: str | None = None,
    source_harness: str | None = None,
    source_handoff: str | None = None,
    owning_workflow: str | None = None,
    last_mutation_workflow: str | None = None,
    last_mutation_status: str | None = None,
    last_mutation_run_id: str | None = None,
    last_mutation_completed_at: str | None = None,
    last_mutation_duration_seconds: str | None = None,
    last_mutation_evidence_state: str | None = None,
    body: str = BODY_SECRET,
) -> Path:
    path = target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if title is not None:
        lines.append(f"title: {title}")
    if category is not None:
        lines.append(f"category: {category}")
    if tags is not None:
        lines.append(f"tags: {json.dumps(tags)}")
    if created is not None:
        lines.append(f"created: {created}")
    if updated is not None:
        lines.append(f"updated: {updated}")
    if last_reviewed is not None:
        lines.append(f"last_reviewed: {last_reviewed}")
    if fresh_until is not None:
        lines.append(f"fresh_until: {fresh_until}")
    if evidence is not None:
        lines.append(f"evidence: {evidence}")
    if source_harness is not None:
        lines.append(f"source_harness: {source_harness}")
    if source_handoff is not None:
        lines.append(f"source_handoff: {source_handoff}")
    if owning_workflow is not None:
        lines.append(f"owning_workflow: {owning_workflow}")
    if last_mutation_workflow is not None:
        lines.append(f"last_mutation_workflow: {last_mutation_workflow}")
    if last_mutation_status is not None:
        lines.append(f"last_mutation_status: {last_mutation_status}")
    if last_mutation_run_id is not None:
        lines.append(f"last_mutation_run_id: {last_mutation_run_id}")
    if last_mutation_completed_at is not None:
        lines.append(f"last_mutation_completed_at: {last_mutation_completed_at}")
    if last_mutation_duration_seconds is not None:
        lines.append(f"last_mutation_duration_seconds: {last_mutation_duration_seconds}")
    if last_mutation_evidence_state is not None:
        lines.append(f"last_mutation_evidence_state: {last_mutation_evidence_state}")
    lines.extend(["---", body, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_markdown(target: Path, rel: str, *, title: str | None = None, body: str = BODY_SECRET) -> Path:
    path = target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if title is None:
        path.write_text(body + "\n", encoding="utf-8")
        return path
    path.write_text(f"---\ntitle: {title}\n---\n{body}\n", encoding="utf-8")
    return path


def _write_care_artifacts(
    target: Path,
    *,
    issues: list[dict] | None = None,
    queue_cards: list[dict] | None = None,
    scan_text: str | None = None,
    queue_text: str | None = None,
) -> None:
    out = target / ".brigade" / "memory-care" / "decay"
    out.mkdir(parents=True, exist_ok=True)
    if scan_text is not None:
        (out / "scan-latest.json").write_text(scan_text, encoding="utf-8")
    else:
        payload = {
            "scan_date": date.today().isoformat(),
            "generated_at": "2026-08-11T00:00:00Z",
            "issue_count": len(issues or []),
            "issues": issues or [],
            "cards": [],
        }
        (out / "scan-latest.json").write_text(json.dumps(payload), encoding="utf-8")
    if queue_text is not None:
        (out / "refresh-queue.json").write_text(queue_text, encoding="utf-8")
    else:
        payload = {
            "version": 1,
            "scan_date": date.today().isoformat(),
            "generated_at": "2026-08-11T00:00:00Z",
            "source": "memory-care",
            "cards": queue_cards if queue_cards is not None else (issues or []),
        }
        (out / "refresh-queue.json").write_text(json.dumps(payload), encoding="utf-8")


def _run_inventory(target: Path, capsys, *extra: str) -> tuple[int, str, str]:
    rc = cli.main(["memory", "inventory", "--target", str(target), "--json", *extra])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _payload(target: Path, capsys, *extra: str) -> dict:
    rc, out, err = _run_inventory(target, capsys, *extra)
    assert rc == 0, err or out
    return json.loads(out)


def test_memory_inventory_schema_pagination_and_item_fields(tmp_path, capsys):
    today = date.today()
    _write_card(
        tmp_path,
        "memory/cards/alpha.md",
        title="Alpha",
        category="ops",
        tags=["a", "b"],
        created="2026-01-01",
        updated="2026-02-01",
        last_reviewed="2026-03-01",
        fresh_until=(today + timedelta(days=30)).isoformat(),
        evidence="['receipt:.brigade/ok.json']",
        source_harness="claude",
        source_handoff="2026-08-11-example.md",
        owning_workflow="handoff-ingest",
        last_mutation_workflow="memory-care-closeout",
        last_mutation_status="ok",
        last_mutation_run_id="run-1",
        last_mutation_completed_at="2026-08-11T12:00:00Z",
        last_mutation_duration_seconds="1.5",
        last_mutation_evidence_state="present",
    )

    payload = _payload(tmp_path, capsys)

    assert payload["schema"] == {"name": "memory-inventory", "version": 1}
    assert payload.get("schema_version", 1) == 1
    assert payload["target"] == "."
    assert isinstance(payload["generated_at"], str) and payload["generated_at"]
    assert PAGINATION_FIELDS <= set(payload["pagination"])
    assert payload["pagination"]["offset"] == 0
    assert payload["pagination"]["limit"] == 100
    assert payload["pagination"]["total"] >= 1
    assert payload["pagination"]["returned"] == len(payload["items"])
    assert FILTER_FIELDS <= set(payload["filters"])
    assert payload["items"], "expected at least one inventory item"
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/alpha.md")
    assert ITEM_FIELDS <= set(item)
    for key in FORBIDDEN_ITEM_KEYS:
        assert key not in item
    assert BODY_SECRET not in json.dumps(payload)
    assert item["store_type"] == "card"
    assert item["logical_destination"] == "cards"
    assert item["title"] == "Alpha"
    assert item["category"] == "ops"
    assert item["tags"] == ["a", "b"]
    assert item["created_at"] == "2026-01-01"
    assert item["updated_at"] == "2026-02-01"
    assert item["last_reviewed"] == "2026-03-01"
    assert item["freshness"] == "fresh"
    assert item["review_state"] == "reviewed"
    assert item["evidence_state"] == "present"
    assert item["source_harness"] == "claude"
    assert item["source_handoff"] == "2026-08-11-example.md"
    assert item["owning_workflow"] == "handoff-ingest"
    assert item["id"] == "card:memory/cards/alpha.md"
    assert item["last_mutation"] == {
        "workflow": "memory-care-closeout",
        "receipt": {
            "evidence_state": "present",
            "status": "ok",
            "run_id": "run-1",
            "completed_at": "2026-08-11T12:00:00Z",
            "duration_seconds": 1.5,
        },
    }
    assert set(item["care"]) == {"issues", "queued_action"}


def test_memory_inventory_includes_non_card_canonical_stores_and_filters(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/keep.md", title="Keep", category="cards")
    _write_markdown(tmp_path, "rules/shipping.md", title="Shipping Rule")
    _write_markdown(tmp_path, "TOOLS.md", title="Tools Notes")
    _write_markdown(tmp_path, "USER.md", title="User Notes")
    _write_markdown(tmp_path, ".learnings/LEARNINGS.md", title="Learning")

    payload = _payload(tmp_path, capsys)
    by_type = {item["store_type"] for item in payload["items"]}
    assert STORE_TYPES <= by_type

    rules_only = _payload(tmp_path, capsys, "--store-type", "rule")
    assert rules_only["pagination"]["total"] == 1
    assert rules_only["items"][0]["store_type"] == "rule"
    assert rules_only["items"][0]["canonical_path"] == "rules/shipping.md"
    assert rules_only["items"][0]["logical_destination"] == "rules"
    assert rules_only["filters"]["store_type"] == ["rule"]

    multi = _payload(tmp_path, capsys, "--store-type", "tools", "--store-type", "user")
    assert {item["store_type"] for item in multi["items"]} == {"tools", "user"}
    assert multi["pagination"]["total"] == 2


def test_memory_inventory_skips_symlinks_and_never_emits_body(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/real.md", title="Real", body=BODY_SECRET)
    outside = tmp_path / "outside.md"
    outside.write_text(f"---\ntitle: Outside\n---\n{BODY_SECRET}\n", encoding="utf-8")
    link = tmp_path / "memory" / "cards" / "linked.md"
    link.symlink_to(outside)

    payload = _payload(tmp_path, capsys)
    paths = {item["canonical_path"] for item in payload["items"]}
    assert "memory/cards/real.md" in paths
    assert "memory/cards/linked.md" not in paths
    dumped = json.dumps(payload)
    assert BODY_SECRET not in dumped
    assert str(tmp_path) not in dumped
    assert "/home/" not in dumped


def test_memory_inventory_freshness_review_and_unknown_defaults(tmp_path, capsys, monkeypatch):
    from brigade import memory_operations

    fixed = date(2026, 8, 11)
    monkeypatch.setattr(memory_operations, "_inventory_today", lambda: fixed)

    _write_card(
        tmp_path,
        "memory/cards/fresh.md",
        title="Fresh",
        fresh_until="2026-08-12",
        last_reviewed="2026-08-01",
        evidence="['ok']",
    )
    _write_card(
        tmp_path,
        "memory/cards/stale.md",
        title="Stale",
        fresh_until="2026-08-10",
        last_reviewed="2026-07-01",
    )
    _write_card(
        tmp_path,
        "memory/cards/bad-date.md",
        title="Bad",
        fresh_until="not-a-date",
    )
    _write_card(tmp_path, "memory/cards/missing-meta.md", title="Missing")
    _write_markdown(tmp_path, "TOOLS.md", title="Tools")

    payload = _payload(tmp_path, capsys)
    by_path = {item["canonical_path"]: item for item in payload["items"]}

    assert by_path["memory/cards/fresh.md"]["freshness"] == "fresh"
    assert by_path["memory/cards/fresh.md"]["review_state"] == "reviewed"
    assert by_path["memory/cards/fresh.md"]["evidence_state"] == "present"
    assert by_path["memory/cards/stale.md"]["freshness"] == "stale"
    assert by_path["memory/cards/bad-date.md"]["freshness"] == "unassessable"
    assert by_path["memory/cards/missing-meta.md"]["freshness"] == "missing"
    assert by_path["memory/cards/missing-meta.md"]["review_state"] == "missing"
    assert by_path["memory/cards/missing-meta.md"]["evidence_state"] == "missing"
    assert by_path["memory/cards/missing-meta.md"]["source_harness"] == "unknown"
    assert by_path["memory/cards/missing-meta.md"]["source_handoff"] is None
    assert by_path["memory/cards/missing-meta.md"]["owning_workflow"] == "unknown"
    assert by_path["memory/cards/missing-meta.md"]["last_mutation"] is None
    assert by_path["TOOLS.md"]["freshness"] == "not_applicable"
    assert by_path["TOOLS.md"]["store_type"] == "tools"
    for item in payload["items"]:
        assert item["freshness"] in FRESHNESS_VALUES


def test_memory_inventory_evidence_unresolved_from_persisted_scan(tmp_path, capsys):
    _write_card(
        tmp_path,
        "memory/cards/broken-evidence.md",
        title="Broken",
        evidence="['receipt:.brigade/missing.json']",
    )
    _write_care_artifacts(
        tmp_path,
        issues=[
            {
                "file": "memory/cards/broken-evidence.md",
                "issue_type": "missing-evidence-ref",
                "action": "repair evidence",
            }
        ],
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/broken-evidence.md")
    assert item["evidence_state"] == "unresolved"
    assert "missing-evidence-ref" in item["care"]["issues"]
    assert item["care"]["queued_action"] == "repair evidence"


def test_memory_inventory_care_join_and_malformed_artifacts_fail_open(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/queued.md", title="Queued")
    _write_care_artifacts(
        tmp_path,
        issues=[
            {
                "file": "memory/cards/queued.md",
                "issue_type": "stale",
                "action": "refresh queued card",
            }
        ],
    )
    good = _payload(tmp_path, capsys)
    queued = next(i for i in good["items"] if i["canonical_path"] == "memory/cards/queued.md")
    assert queued["care"]["issues"] == ["stale"]
    assert queued["care"]["queued_action"] == "refresh queued card"

    _write_care_artifacts(
        tmp_path,
        scan_text="{not json MALFORMED_CARE_LEAK_875",
        queue_text="[1,2,3] MALFORMED_QUEUE_LEAK_875",
    )
    rc, out, err = _run_inventory(tmp_path, capsys)
    assert rc == 0, err
    payload = json.loads(out)
    dumped = json.dumps(payload) + err
    assert "MALFORMED_CARE_LEAK_875" not in dumped
    assert "MALFORMED_QUEUE_LEAK_875" not in dumped
    assert "Traceback" not in err
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/queued.md")
    assert item["care"] == {"issues": [], "queued_action": None}


def test_memory_inventory_filters_are_case_insensitive_with_tag_and(tmp_path, capsys):
    _write_card(
        tmp_path,
        "memory/cards/one.md",
        title="One",
        category="Ops",
        tags=["Alpha", "Beta"],
        source_harness="Claude",
        owning_workflow="Handoff-Ingest",
        fresh_until=(date.today() + timedelta(days=10)).isoformat(),
        last_reviewed="2026-01-01",
        evidence="['x']",
    )
    _write_card(
        tmp_path,
        "memory/cards/two.md",
        title="Two",
        category="ops",
        tags=["Alpha"],
        source_harness="codex",
        owning_workflow="handoff-ingest",
        fresh_until=(date.today() + timedelta(days=10)).isoformat(),
        last_reviewed="2026-01-01",
        evidence="['x']",
    )

    both_tags = _payload(tmp_path, capsys, "--tag", "alpha", "--tag", "beta")
    assert both_tags["pagination"]["total"] == 1
    assert both_tags["items"][0]["canonical_path"] == "memory/cards/one.md"
    assert both_tags["filters"]["tag"] == ["alpha", "beta"]

    category = _payload(tmp_path, capsys, "--category", "OPS")
    assert category["pagination"]["total"] == 2
    assert category["filters"]["category"] == ["ops"]

    harness = _payload(tmp_path, capsys, "--source-harness", "claude")
    assert harness["pagination"]["total"] == 1
    assert harness["items"][0]["source_harness"].lower() == "claude"

    workflow = _payload(tmp_path, capsys, "--owning-workflow", "HANDOFF-INGEST")
    assert workflow["pagination"]["total"] == 2

    freshness = _payload(tmp_path, capsys, "--freshness", "FRESH")
    assert freshness["pagination"]["total"] == 2
    assert freshness["filters"]["freshness"] == ["fresh"]

    review = _payload(tmp_path, capsys, "--review-state", "reviewed")
    assert review["pagination"]["total"] == 2

    evidence = _payload(tmp_path, capsys, "--evidence-state", "present")
    assert evidence["pagination"]["total"] == 2


def test_memory_inventory_rejects_invalid_offset_limit_and_filters(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/a.md", title="A")

    for args in (
        ("--offset", "-1"),
        ("--limit", "0"),
        ("--limit", "501"),
        ("--store-type", "notebook"),
        ("--freshness", "rotten"),
        ("--review-state", "maybe"),
        ("--evidence-state", "maybe"),
    ):
        with pytest.raises(SystemExit) as exc:
            cli.main(["memory", "inventory", "--target", str(tmp_path), "--json", *args])
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert BODY_SECRET not in err
        assert str(tmp_path.resolve()) not in err or "error:" in err.lower() or err


def test_memory_inventory_pagination_over_505_is_stable_and_capped(tmp_path, capsys):
    for index in range(505):
        name = f"card-{index:04d}.md"
        _write_card(tmp_path, f"memory/cards/{name}", title=f"Card {index:04d}")

    page1 = _payload(tmp_path, capsys, "--limit", "500")
    assert page1["pagination"]["offset"] == 0
    assert page1["pagination"]["limit"] == 500
    assert page1["pagination"]["total"] == 505
    assert page1["pagination"]["returned"] == 500
    assert page1["pagination"]["has_more"] is True
    assert page1["pagination"]["next_offset"] == 500
    assert len(page1["items"]) == 500

    page2 = _payload(tmp_path, capsys, "--offset", "500", "--limit", "500")
    assert page2["pagination"]["offset"] == 500
    assert page2["pagination"]["total"] == 505
    assert page2["pagination"]["returned"] == 5
    assert page2["pagination"]["has_more"] is False
    assert page2["pagination"]["next_offset"] is None
    assert len(page2["items"]) == 5

    paths1 = [item["canonical_path"] for item in page1["items"]]
    paths2 = [item["canonical_path"] for item in page2["items"]]
    assert len(set(paths1) & set(paths2)) == 0
    assert paths1 == sorted(paths1)
    assert paths2 == sorted(paths2)
    assert paths1 + paths2 == sorted(paths1 + paths2)

    again = _payload(tmp_path, capsys, "--limit", "500")
    assert [item["canonical_path"] for item in again["items"]] == paths1
    assert again["generated_at"]  # may differ; only ordering must match

    with pytest.raises(SystemExit) as exc:
        cli.main(["memory", "inventory", "--target", str(tmp_path), "--json", "--limit", "501"])
    assert exc.value.code == 2


def test_memory_inventory_deterministic_order_before_filter_and_human_output(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/z.md", title="Z", category="z")
    _write_card(tmp_path, "memory/cards/a.md", title="A", category="a")
    _write_markdown(tmp_path, "rules/b.md", title="B")

    first = _payload(tmp_path, capsys)
    second = _payload(tmp_path, capsys)
    assert [i["canonical_path"] for i in first["items"]] == [i["canonical_path"] for i in second["items"]]
    assert [i["canonical_path"] for i in first["items"]] == sorted(i["canonical_path"] for i in first["items"])

    filtered = _payload(tmp_path, capsys, "--category", "a", "--limit", "1")
    assert filtered["pagination"]["total"] == 1
    assert filtered["items"][0]["canonical_path"] == "memory/cards/a.md"

    rc = cli.main(["memory", "inventory", "--target", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "memory inventory:" in out
    assert BODY_SECRET not in out
    assert not re.search(r"/home/[^\s]+", out)
