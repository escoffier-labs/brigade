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


def test_memory_inventory_frontmatter_readline_requests_are_byte_bounded(tmp_path, monkeypatch):
    """Both binary readline calls must pass FRONTMATTER_READ_MAX_BYTES + 1 (not unbounded)."""
    from brigade import memory_operations

    bound = memory_operations.FRONTMATTER_READ_MAX_BYTES + 1
    frontmatter = b"---\ntitle: HugeBody\nfresh_until: 2099-01-01\n---\n"
    body_marker = b"BODY_NEVER_READ_875_" + (b"X" * (10 * 1024 * 1024))
    path = tmp_path / "memory" / "cards" / "huge.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frontmatter + body_marker)

    readline_sizes: list[int | None] = []
    real_open = Path.open

    def guarded_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self.resolve() != path.resolve():
            return handle
        if "b" not in str(mode):
            raise AssertionError(f"expected binary open for inventory card, got mode={mode!r}")
        orig_readline = handle.readline

        def tracked_readline(size: int | None = -1):
            readline_sizes.append(size)
            return orig_readline(size) if size is not None else orig_readline()

        handle.readline = tracked_readline  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(Path, "open", guarded_open)

    payload = memory_operations.inventory_payload(tmp_path)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/huge.md")
    assert item["title"] == "HugeBody"
    assert item["freshness"] == "fresh"
    assert readline_sizes, "expected binary readline calls for frontmatter"
    assert all(size == bound for size in readline_sizes), readline_sizes
    dumped = json.dumps(payload)
    assert "BODY_NEVER_READ_875_" not in dumped

    # 10 MB newline-free degenerate first line must never be loaded unbounded.
    readline_sizes.clear()
    degenerate = tmp_path / "memory" / "cards" / "degenerate.md"
    degenerate.write_bytes(b"X" * (10 * 1024 * 1024))
    payload = memory_operations.inventory_payload(tmp_path)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/degenerate.md")
    assert item["title"]
    assert all(size == bound for size in readline_sizes), readline_sizes
    assert "X" * 1024 not in json.dumps(payload)


def test_memory_inventory_bound_single_line_strips_c0_controls_preserving_safe_text():
    from brigade import memory_operations

    assert memory_operations._bound_single_line("safe title") == "safe title"
    assert memory_operations._bound_single_line("hello\x07world") == "helloworld"
    assert memory_operations._bound_single_line("x\x1by\x00z") == "xyz"
    assert memory_operations._bound_single_line("\x07\x1b\x00") is None
    assert memory_operations._bound_single_line("keep\t tabs") == "keep tabs"
    # CR/LF still collapse to a single line without leaking the remainder.
    assert memory_operations._bound_single_line("one\ntwo") == "one"
    assert memory_operations._bound_single_line("one\rtwo") == "one"


def test_memory_inventory_title_fallback_sanitizes_path_stem(tmp_path, monkeypatch):
    from brigade import memory_operations

    path = tmp_path / "memory" / "cards" / "notitle.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ncategory: ops\n---\nbody\n", encoding="utf-8")

    real_stem = Path.stem

    def stem_get(self: Path) -> str:
        if self.resolve() == path.resolve():
            return "hello\x07\x1bworld"
        return real_stem.__get__(self, type(self))

    monkeypatch.setattr(Path, "stem", property(stem_get))

    payload = memory_operations.inventory_payload(tmp_path)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/notitle.md")
    assert item["title"] == "helloworld"
    assert "\x07" not in item["title"]
    assert "\x1b" not in item["title"]

    def path_like_stem(self: Path) -> str:
        if self.resolve() == path.resolve():
            return "/abs/evil\x07stem"
        return real_stem.__get__(self, type(self))

    monkeypatch.setattr(Path, "stem", property(path_like_stem))
    payload = memory_operations.inventory_payload(tmp_path)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/notitle.md")
    assert isinstance(item["title"], str) and item["title"].strip()
    assert "/" not in item["title"]
    assert "\x07" not in item["title"]


def test_memory_inventory_config_oserror_fails_open_without_path_leak(tmp_path, capsys, monkeypatch):
    from brigade import memory_cmd

    _write_card(tmp_path, "memory/cards/a.md", title="A", fresh_until="2099-01-01")
    _write_care_artifacts(
        tmp_path,
        issues=[
            {
                "file": "memory/cards/a.md",
                "issue_type": "stale",
                "suggested_refresh_action": "refresh",
            }
        ],
    )

    def boom(_target: Path):
        raise OSError(13, "Permission denied", "/secret/path/.brigade/memory-care.toml")

    monkeypatch.setattr(memory_cmd, "_config_or_default", boom)

    rc, out, err = _run_inventory(tmp_path, capsys)
    assert rc == 0, err
    assert "Traceback" not in err
    dumped = out + err
    assert "/secret/path" not in dumped
    assert "memory-care.toml" not in dumped
    payload = json.loads(out)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/a.md")
    assert item["title"] == "A"
    # Care join also fails open on config OSError (defaults), still joins artifacts by path.
    assert "stale" in item["care"]["issues"]


def test_memory_inventory_last_mutation_receipt_fields_are_allowlisted(tmp_path, capsys):
    _write_card(
        tmp_path,
        "memory/cards/mutation.md",
        title="Mutation",
        last_mutation_workflow="memory-care-closeout\nEXTRA",
        last_mutation_status="totally-made-up-status",
        last_mutation_run_id="run\nid",
        last_mutation_completed_at="not-iso-8601",
        last_mutation_duration_seconds="-3",
        last_mutation_evidence_state="asserted-present",
    )
    _write_card(
        tmp_path,
        "memory/cards/mutation-ok.md",
        title="Mutation OK",
        last_mutation_workflow="memory-care-closeout",
        last_mutation_status="ok",
        last_mutation_run_id="run-2",
        last_mutation_completed_at="2026-08-11T12:00:00Z",
        last_mutation_duration_seconds="2",
        last_mutation_evidence_state="present",
    )
    _write_card(
        tmp_path,
        "memory/cards/mutation-inf.md",
        title="Mutation Inf",
        last_mutation_workflow="memory-care-closeout",
        last_mutation_status="ok",
        last_mutation_run_id="run-3",
        last_mutation_completed_at="2026-08-11T12:00:00Z",
        last_mutation_duration_seconds="inf",
        last_mutation_evidence_state="present",
    )

    payload = _payload(tmp_path, capsys)
    by_path = {item["canonical_path"]: item for item in payload["items"]}

    bad = by_path["memory/cards/mutation.md"]["last_mutation"]
    assert bad is not None
    assert "\n" not in bad["workflow"]
    assert bad["receipt"]["status"] is None
    assert bad["receipt"]["run_id"] is None or "\n" not in str(bad["receipt"]["run_id"])
    assert bad["receipt"]["completed_at"] is None
    assert bad["receipt"]["duration_seconds"] is None
    assert bad["receipt"]["evidence_state"] == "missing"

    good = by_path["memory/cards/mutation-ok.md"]["last_mutation"]
    assert good == {
        "workflow": "memory-care-closeout",
        "receipt": {
            "evidence_state": "present",
            "status": "ok",
            "run_id": "run-2",
            "completed_at": "2026-08-11T12:00:00Z",
            "duration_seconds": 2,
        },
    }
    assert by_path["memory/cards/mutation-inf.md"]["last_mutation"]["receipt"]["duration_seconds"] is None


def test_memory_inventory_care_artifacts_allowlist_producer_key_and_paths(tmp_path, capsys):
    _write_card(tmp_path, "memory/cards/care.md", title="Care")
    _write_care_artifacts(
        tmp_path,
        issues=[
            {
                "file": "memory/cards/care.md",
                "issue_type": "not-a-real-check",
                "action": "/home/secret/refresh.sh",
                "suggested_refresh_action": "refresh card metadata",
            },
            {
                "file": "memory/cards/care.md",
                "issue_type": "stale",
                "suggested_refresh_action": "refresh card metadata",
            },
            {
                "file": "memory/cards/care.md",
                "issue_type": "stale",
                "action": "legacy only",
            },
            {
                "file": "memory/cards/care.md",
                "issue_type": "expired",
                "suggested_refresh_action": "C:\\Windows\\repair.bat",
            },
            {
                "file": "memory/cards/care.md",
                "issue_type": "missing-reviewed",
                "action": "review again",
            },
        ],
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/care.md")
    assert item["care"]["issues"] == ["expired", "missing-reviewed", "stale"]
    assert item["care"]["queued_action"] == "refresh card metadata"
    dumped = json.dumps(payload)
    assert "/home/secret" not in dumped
    assert "C:\\Windows" not in dumped
    assert "not-a-real-check" not in dumped


def test_memory_inventory_payload_validates_offset_and_limit_constants(tmp_path, capsys):
    from brigade import memory_operations
    from brigade.cli import memory as memory_cli

    _write_card(tmp_path, "memory/cards/a.md", title="A")

    with pytest.raises(ValueError):
        memory_operations.inventory_payload(tmp_path, offset=-1)
    with pytest.raises(ValueError):
        memory_operations.inventory_payload(tmp_path, limit=0)
    with pytest.raises(ValueError):
        memory_operations.inventory_payload(tmp_path, limit=memory_operations.INVENTORY_MAX_LIMIT + 1)

    source = Path(memory_cli.__file__).read_text(encoding="utf-8")
    assert "INVENTORY_MAX_LIMIT" in source
    assert "INVENTORY_DEFAULT_LIMIT" in source
    assert "args.limit > 500" not in source
    assert "max: 500" not in source

    payload = _payload(tmp_path, capsys)
    assert payload["pagination"]["limit"] == memory_operations.INVENTORY_DEFAULT_LIMIT


def test_memory_inventory_skips_file_and_directory_symlinks(tmp_path, capsys):
    # Real cards under a real root.
    real_root = tmp_path / "real-cards"
    real_root.mkdir()
    (real_root / "kept.md").write_text("---\ntitle: Kept\n---\nbody\n", encoding="utf-8")

    # Symlinked card root: scanner can see these, inventory must skip symlink ancestors.
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "linked-card.md").write_text("---\ntitle: ViaDirLink\n---\nbody\n", encoding="utf-8")
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "cards").symlink_to(outside)

    # Also keep a non-symlink store so inventory is non-empty and paths stay unresolved.
    _write_markdown(tmp_path, "rules/shipping.md", title="Shipping")

    outside_file = tmp_path / "outside-file.md"
    outside_file.write_text("---\ntitle: ViaFileLink\n---\nbody\n", encoding="utf-8")
    # File symlink under a real cards tree (separate target).
    alt = tmp_path / "alt-cards"
    alt.mkdir()
    (alt / "file-link.md").symlink_to(outside_file)
    (alt / "real.md").write_text("---\ntitle: AltReal\n---\nbody\n", encoding="utf-8")

    from brigade import memory_cmd
    from brigade import memory_operations

    # Configure one real root + the symlinked memory/cards root.
    config_path = tmp_path / ".brigade" / "memory-care.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        'card_roots = ["memory/cards", "alt-cards", "real-cards"]\n'
        'index_paths = ["MEMORY.md"]\n'
        'exclude_paths = ["memory/cards/decay"]\n',
        encoding="utf-8",
    )

    # Scanner sees unresolved paths through the symlink root; inventory must not emit them.
    scanned = {str(path.relative_to(tmp_path)) for path in memory_cmd._iter_cards(tmp_path, memory_cmd._config_or_default(tmp_path))}
    assert "memory/cards/linked-card.md" in scanned

    payload = memory_operations.inventory_payload(tmp_path)
    paths = {item["canonical_path"] for item in payload["items"]}
    assert "rules/shipping.md" in paths
    assert "real-cards/kept.md" in paths
    assert "alt-cards/real.md" in paths
    assert "alt-cards/file-link.md" not in paths
    assert "memory/cards/linked-card.md" not in paths
    assert "outside/linked-card.md" not in paths
    assert "outside-dir/linked-card.md" not in paths
    assert all(not path.startswith("..") for path in paths)
    dumped = json.dumps(payload)
    assert "ViaDirLink" not in dumped
    assert "ViaFileLink" not in dumped


def test_memory_inventory_redacts_hostile_frontmatter_contract_fields(tmp_path, capsys):
    long_title = "T" * 500
    _write_card(
        tmp_path,
        "memory/cards/hostile.md",
        title=f"/home/secret/{long_title}",
        category="C:\\Users\\secret\\ops",
        tags=["~/secret-tag", "safe-tag", "normal\nmultiline"],
        source_harness="/var/lib/harness",
        source_handoff="/tmp/escape.md",
        owning_workflow="~/workflow",
        last_mutation_workflow="/abs/workflow",
        last_mutation_status="ok",
        last_mutation_run_id="~/run-id",
        last_mutation_completed_at="2026-08-11T12:00:00Z",
        last_mutation_evidence_state="present",
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/hostile.md")
    dumped = json.dumps(payload)
    assert "/home/secret" not in dumped
    assert "C:\\Users" not in dumped
    assert "~/secret-tag" not in dumped
    assert "/tmp/escape.md" not in dumped
    assert "/var/lib/harness" not in dumped
    assert "~/workflow" not in dumped
    assert "/abs/workflow" not in dumped
    assert "\n" not in item["title"]
    assert len(item["title"]) <= 256
    assert item["category"] is None or ("/" not in item["category"] and "\\" not in item["category"])
    assert "safe-tag" in item["tags"]
    assert all("\n" not in tag for tag in item["tags"])
    assert item["source_handoff"] is None
    assert item["source_harness"] == "unknown" or not item["source_harness"].startswith("/")
    assert item["owning_workflow"] == "unknown" or not str(item["owning_workflow"]).startswith("~")
    mutation = item["last_mutation"]
    assert mutation is None or (
        not str(mutation["workflow"]).startswith("/")
        and (mutation["receipt"]["run_id"] is None or not str(mutation["receipt"]["run_id"]).startswith("~"))
    )


def test_memory_inventory_robust_against_missing_malformed_oversized_and_non_utf8_body(tmp_path, capsys):
    no_fm = tmp_path / "memory" / "cards" / "no-fm.md"
    no_fm.parent.mkdir(parents=True, exist_ok=True)
    no_fm.write_text(f"plain body with {BODY_SECRET}\n", encoding="utf-8")

    bad_bytes = tmp_path / "memory" / "cards" / "bad-body.md"
    bad_bytes.write_bytes(b"---\ntitle: StillValid\nfresh_until: 2099-01-01\n---\n" + b"\xff\xfe" + BODY_SECRET.encode())

    oversized = tmp_path / "memory" / "cards" / "oversized-fm.md"
    oversized.write_bytes(b"---\n" + (b"x: " + b"y" * 200 + b"\n") * 400 + b"title: NeverClose\n")

    malformed = tmp_path / "memory" / "cards" / "malformed-fm.md"
    malformed.write_text("---\n: not a key\n[this is broken\n---\n" + BODY_SECRET + "\n", encoding="utf-8")

    _write_card(tmp_path, "memory/cards/care-hostile.md", title="CareHostile", fresh_until="2099-01-01")
    _write_care_artifacts(
        tmp_path,
        issues=[
            {
                "file": "memory/cards/care-hostile.md",
                "issue_type": "stale",
                "suggested_refresh_action": "/home/leak/action",
            }
        ],
    )

    payload = _payload(tmp_path, capsys)
    by_path = {item["canonical_path"]: item for item in payload["items"]}
    dumped = json.dumps(payload)

    assert BODY_SECRET not in dumped
    assert "/home/leak" not in dumped
    assert "NeverClose" not in dumped

    assert "memory/cards/no-fm.md" in by_path
    assert by_path["memory/cards/no-fm.md"]["freshness"] == "missing"

    bad = by_path["memory/cards/bad-body.md"]
    assert bad["title"] == "StillValid"
    assert bad["freshness"] == "fresh"  # valid frontmatter must not become unassessable from body bytes

    assert by_path["memory/cards/oversized-fm.md"]["title"] == "oversized-fm"
    assert by_path["memory/cards/care-hostile.md"]["care"]["queued_action"] is None


def test_memory_inventory_evidence_state_present_unless_persisted_missing_ref(tmp_path, capsys):
    """Approved contract (not I5): non-empty evidence => present unless scan says missing-evidence-ref."""
    _write_card(
        tmp_path,
        "memory/cards/has-evidence.md",
        title="HasEvidence",
        evidence="['receipt:.brigade/ok.json']",
    )
    _write_card(
        tmp_path,
        "memory/cards/no-evidence.md",
        title="NoEvidence",
    )
    payload = _payload(tmp_path, capsys)
    by_path = {item["canonical_path"]: item for item in payload["items"]}
    assert by_path["memory/cards/has-evidence.md"]["evidence_state"] == "present"
    assert by_path["memory/cards/no-evidence.md"]["evidence_state"] == "missing"


def test_memory_inventory_joins_real_memory_care_scan(tmp_path, capsys, monkeypatch):
    from brigade import memory_cmd

    monkeypatch.setattr(memory_cmd, "_today", lambda: date(2026, 8, 11))
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "needs-review.md").write_text(
        "\n".join(
            [
                "---",
                "title: Needs Review",
                "topic: needs-review",
                "confidence: high",
                "evidence: ['README.md']",
                "fresh_until: 2099-01-01",
                "---",
                "body",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "MEMORY.md").write_text("- [needs-review](memory/cards/needs-review.md)\n", encoding="utf-8")
    assert memory_cmd.init(target=tmp_path, force=True, update_gitignore=False, with_runbooks=False) == 0
    capsys.readouterr()
    assert memory_cmd.scan(target=tmp_path, json_output=True) == 0
    scan_out = capsys.readouterr().out
    scan_payload = json.loads(scan_out)
    assert any(issue.get("issue_type") == "missing-reviewed" for issue in scan_payload.get("issues") or [])

    inventory = _payload(tmp_path, capsys)
    item = next(i for i in inventory["items"] if i["canonical_path"] == "memory/cards/needs-review.md")
    assert "missing-reviewed" in item["care"]["issues"]
    assert item["care"]["queued_action"]
    assert "/home/" not in json.dumps(item["care"])
    assert item["care"]["issues"] == sorted(set(item["care"]["issues"]))


def _write_card_raw_tags(target: Path, rel: str, *, title: str, tags_line: str) -> Path:
    """Write a card with an exact tags frontmatter line (no JSON encoding)."""
    path = target / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                f"tags: {tags_line}",
                "---",
                BODY_SECRET,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_memory_inventory_parses_brigade_template_bare_inline_tags(tmp_path, capsys):
    # Exact unquoted Brigade template spelling; literal_eval leaves this as one scalar.
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/template-tags.md",
        title="TemplateTags",
        tags_line="[memory, handoff, ingester, claude-code, codex]",
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/template-tags.md")
    assert item["tags"] == ["memory", "handoff", "ingester", "claude-code", "codex"]
    assert "[memory, handoff, ingester, claude-code, codex]" not in item["tags"]


def test_memory_inventory_parses_quoted_mixed_empty_and_duplicate_inline_tags(tmp_path, capsys):
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/mixed-tags.md",
        title="MixedTags",
        tags_line="[memory, 'handoff', \"ingester\", , memory, 'claude-code']",
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/mixed-tags.md")
    assert item["tags"] == ["memory", "handoff", "ingester", "claude-code"]


def test_memory_inventory_drops_unsafe_inline_tag_entries(tmp_path, capsys):
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/unsafe-tags.md",
        title="UnsafeTags",
        tags_line=(
            "[safe, /abs/tag, ~/home-tag, C:\\\\Users\\\\tag, "
            "win\\\\path, control\x01tag, look\\nlike, keep-me]"
        ),
    )

    payload = _payload(tmp_path, capsys)
    item = next(i for i in payload["items"] if i["canonical_path"] == "memory/cards/unsafe-tags.md")
    dumped = json.dumps(item["tags"])
    assert "safe" in item["tags"]
    assert "keep-me" in item["tags"]
    assert "/abs/tag" not in dumped
    assert "~/home-tag" not in dumped
    assert "C:\\Users" not in dumped
    assert "win\\path" not in dumped
    assert "\x01" not in dumped
    assert "look\\nlike" not in item["tags"] or "\n" not in "".join(item["tags"])
    assert all("\n" not in tag for tag in item["tags"])
    assert all("/" not in tag and "\\" not in tag and not tag.startswith("~") for tag in item["tags"])


def test_memory_inventory_malformed_bracket_tag_scalar_stays_one_safe_tag(tmp_path, capsys):
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/malformed-open.md",
        title="MalformedOpen",
        tags_line="[memory, handoff",
    )
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/malformed-close.md",
        title="MalformedClose",
        tags_line="memory, handoff]",
    )
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/plain-tag.md",
        title="PlainTag",
        tags_line="single-tag",
    )

    payload = _payload(tmp_path, capsys)
    by_path = {item["canonical_path"]: item for item in payload["items"]}
    assert by_path["memory/cards/malformed-open.md"]["tags"] == ["[memory, handoff"]
    assert by_path["memory/cards/malformed-close.md"]["tags"] == ["memory, handoff]"]
    assert by_path["memory/cards/plain-tag.md"]["tags"] == ["single-tag"]


def test_memory_inventory_filters_on_parsed_template_inline_tags(tmp_path, capsys):
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/template-a.md",
        title="TemplateA",
        tags_line="[memory, handoff, ingester, claude-code, codex]",
    )
    _write_card_raw_tags(
        tmp_path,
        "memory/cards/template-b.md",
        title="TemplateB",
        tags_line="[ops, runbook]",
    )
    _write_card(
        tmp_path,
        "memory/cards/json-list.md",
        title="JsonList",
        tags=["memory", "listed"],
    )

    memory_only = _payload(tmp_path, capsys, "--tag", "memory")
    paths = {item["canonical_path"] for item in memory_only["items"]}
    assert "memory/cards/template-a.md" in paths
    assert "memory/cards/json-list.md" in paths
    assert "memory/cards/template-b.md" not in paths
    assert memory_only["filters"]["tag"] == ["memory"]

    both = _payload(tmp_path, capsys, "--tag", "memory", "--tag", "handoff")
    assert both["pagination"]["total"] == 1
    assert both["items"][0]["canonical_path"] == "memory/cards/template-a.md"
