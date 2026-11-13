"""Physical frontmatter boundaries and recovery caches fail closed."""

import hashlib
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brigade import memory_care_fixes as fixes, memory_care_pending as pending
from brigade import memory_care_plan as planner, memory_care_journal as journal
from brigade import memory_care_tend as tend, memory_cmd as care
from tests.test_memory_care_fixes import card, TODAY


@pytest.mark.parametrize(
    "fields",
    [
        "",
        "# comment\n",
        "\n",
        "topic: A\n",
        "topic: \n",
        "topic:\n",
        "topic: 'A'\n",
        'topic: "A"\n',
        "topic: true\n",
        "topic: 12\n",
        "evidence: [README.md]\n",
        "tags: []\n",
        "extra: {}\n",
        'tags: ["a]: b", "c:d"]\n',
        "extra: {a: b}\n",
        "topic: A # note\n",
        "last_reviewed: 2026-11-01\n",
        "a_b-c: ok\n",
        "topic: 'it''s fine'\n",
        'topic: "a\\"b"\n',
    ],
)
def test_plain_accepted(fields):
    assert isinstance(fixes.plain_frontmatter("---\n" + fields + "---\nBody\n"), dict)


@pytest.mark.parametrize(
    "fields",
    [
        " topic: A\n",
        "\ttopic: A\n",
        "topic:\tA\n",
        "  # note\n",
        "topic: |\n",
        "topic: >-\n",
        "topic: |2+\n",
        "topic: &anchor A\n",
        "topic: *anchor\n",
        "topic: !tag A\n",
        '"topic": A\n',
        "'topic': A\n",
        "topic: A\ntopic: B\n",
        "Topic: A\ntopic: B\n",
        "last_reviewed: A\nreviewed_at: B\n",
        "expires: A\nfresh_until: B\n",
        "topic: [a\n",
        "topic: {a: b\n",
        "topic: [a}\n",
        'topic: "open\n',
        "topic: 'open\n",
        "topic: [a]\n  continuation\n",
        "topic: a: b\n",
        "topic: [*anchor]\n",
        "topic: - item\n",
        "- item\n",
        "topic:A\n",
    ],
)
def test_plain_refused(fields):
    with pytest.raises(fixes.FixRefused, match="^frontmatter-refused$"):
        fixes.plain_frontmatter("---\n" + fields + "---\nBody\n")


@pytest.mark.parametrize("text", ["\n---\n---\n", "--- \n---\n", "---\na: b\n--- \n", "---\na: b\n", "body"])
def test_exact_fences(text):
    with pytest.raises(fixes.FixRefused, match="^frontmatter-refused$"):
        fixes.plain_frontmatter(text)


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_only_added_field_bytes_and_exact_undo(tmp_path, newline, bom):
    path = card(tmp_path, "missing-freshness")
    before = bom + path.read_bytes().replace(b"\n", newline)
    path.write_bytes(before)
    config = care._config_or_default(tmp_path)
    plan = planner.plan_payload(tmp_path, config, care._scan_payload(tmp_path, config))
    preview = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan, dry_run=True)
    assert preview["applied"] == 1 and path.read_bytes() == before
    result = fixes.apply_fixes(tmp_path, config, today=TODAY, plan=plan)
    assert result["applied"] == preview["applied"] == 1
    assert path.read_bytes() == before.replace(
        newline + b"---" + newline, newline + b"fresh_until: 2027-01-30" + newline + b"---" + newline
    )
    tend.undo(tmp_path, result["decision_ids"][0])
    assert path.read_bytes() == before


@pytest.mark.parametrize("mixed", [b"---\r\ntopic: A\n---\r\n", b"---\ntopic: A\n---\nBody\r\n"])
def test_mixed_endings_refused_unchanged(tmp_path, mixed):
    path = card(tmp_path, "missing-freshness")
    path.write_bytes(mixed)
    item = {"card_file": "memory/cards/a.md", "issue_type": "missing-freshness"}
    with pytest.raises(fixes.FixRefused, match="^frontmatter-refused$"):
        fixes.prepare(tmp_path, care._config_or_default(tmp_path), item, TODAY)
    assert path.read_bytes() == mixed


def test_pending_cache_write_failure_returns_scanned_rows(tmp_path, monkeypatch):
    path = card(tmp_path, "missing-freshness")
    decision = journal.commit(
        tmp_path,
        kind="fix",
        reason="fixture",
        card_id="a",
        changes=[journal.Change("memory/cards/a.md", path.read_bytes(), b"new")],
    )
    journal.annotate(tmp_path, decision.decision_id, phase="prepared")
    expected = pending.scan(tmp_path)
    assert expected[decision.decision_id]["prepared"] is True
    monkeypatch.setattr(pending.store, "write_json", Mock(side_effect=PermissionError("cache busy")))
    assert pending.load(tmp_path) == expected


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"prepared": True},
        {"prepared": 1, "document": False, "repair": False, "append": False},
        {"prepared": False, "document": False, "repair": False, "append": False, "extra": False},
        [],
    ],
)
def test_invalid_cached_row_rebuilt(tmp_path, monkeypatch, row):
    path = card(tmp_path, "missing-freshness")
    journal.commit(
        tmp_path,
        kind="fix",
        reason="fixture",
        card_id="a",
        changes=[journal.Change("memory/cards/a.md", path.read_bytes(), b"new")],
    )
    pending.load(tmp_path)
    index = tmp_path / pending.INDEX_REL
    payload = json.loads(index.read_bytes())
    for entry in payload["records"].values():
        entry["row"] = row
    payload["sha256"] = hashlib.sha256(json.dumps(payload["records"], sort_keys=True).encode("utf-8")).hexdigest()
    index.write_bytes(json.dumps(payload).encode("utf-8"))
    scan = Mock(wraps=pending._row)
    monkeypatch.setattr(pending, "_row", scan)
    assert pending.load(tmp_path) == pending.scan(tmp_path)
    assert scan.call_count == 2


@pytest.mark.parametrize("route", ["tend", "journal"])
def test_legacy_abandoned_undo_retry(tmp_path, route):
    path = card(tmp_path, "missing-freshness")
    before = path.read_bytes()
    original = journal.commit(
        tmp_path,
        kind="fix",
        reason="fixture",
        card_id="a",
        changes=[journal.Change("memory/cards/a.md", before, b"new")],
    )
    abandoned = journal.commit(
        tmp_path,
        kind="undo",
        reason="fixture",
        card_id="a",
        changes=[journal.Change("fixture.md", None, b"fixture")],
        undoes=original.decision_id,
    )
    journal.annotate(tmp_path, abandoned.decision_id, phase="abandoned", status="abandoned")
    journal.mark_undone(tmp_path, original.decision_id, abandoned.decision_id)
    retry = (
        tend.undo(tmp_path, original.decision_id)
        if route == "tend"
        else journal.revert(tmp_path, original.decision_id, reason="undo")
    )
    assert retry.decision_id != abandoned.decision_id
    assert path.read_bytes() == before


def test_plan_two_hundred_cards_counts(tmp_path, monkeypatch):
    card(tmp_path, "missing-reviewed")
    base = tmp_path / "memory/cards/a.md"
    issues = []
    for n in range(200):
        rel = f"memory/cards/card-{n}.md"
        (tmp_path / rel).write_bytes(base.read_bytes())
        issues.append({"file": rel, "issue_type": "missing-reviewed"})
    scans = Mock(wraps=care._iter_cards)
    git = Mock(
        side_effect=lambda argv, **kwargs: SimpleNamespace(
            returncode=0, stdout="2026-11-01\n" if "log" in argv else b"true\n"
        )
    )
    monkeypatch.setattr(care, "_iter_cards", scans)
    monkeypatch.setattr(fixes.subprocess, "run", git)
    plan = planner.plan_payload(tmp_path, care._config_or_default(tmp_path), {"issues": issues})
    assert plan["blocked_count"] == 0 and plan["plan_count"] == 200
    assert scans.call_count == 1
    assert git.call_count == 201


def test_tend_uses_utc_today(tmp_path, monkeypatch):
    expected = date(2020, 1, 2)
    monkeypatch.setattr(care, "_today", lambda: expected)
    apply = Mock(return_value={})
    monkeypatch.setattr(fixes, "apply_fixes", apply)
    tend.run(tmp_path, stages=["fixes"], dry_run=True)
    assert apply.call_args.kwargs["today"] == expected


def test_cli_busy_is_one_line(tmp_path, monkeypatch, capsys):
    from brigade.cli import main
    from brigade.inbox_lock import held_file_lock

    card(tmp_path, "missing-freshness")
    care.scan(target=tmp_path, json_output=True)
    capsys.readouterr()
    lock = tmp_path / ".brigade/memory-care/care.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with held_file_lock(lock, deadline_seconds=1):
        assert main(["memory", "care", "plan-fixes", "--apply", "--target", str(tmp_path)]) != 0
    captured = capsys.readouterr()
    assert captured.out + captured.err == "memory care busy\n"


def test_existing_date_key_casing_never_adds_an_alias(tmp_path):
    path = card(tmp_path, "missing-freshness")
    before = path.read_bytes().replace(b"last_reviewed:", b"LAST_REVIEWED:")
    path.write_bytes(before)
    item = {"card_file": "memory/cards/a.md", "issue_type": "missing-reviewed"}
    with pytest.raises(fixes.FixRefused, match="^already-present$"):
        fixes.prepare(tmp_path, care._config_or_default(tmp_path), item, TODAY)
    assert path.read_bytes() == before


def test_quoted_date_with_trailing_spaces_derives_freshness(tmp_path):
    path = card(tmp_path, "missing-freshness")
    path.write_bytes(path.read_bytes().replace(b"last_reviewed: 2026-11-01", b"last_reviewed: '2026-11-01'  "))
    prepared = fixes.prepare(
        tmp_path,
        care._config_or_default(tmp_path),
        {"card_file": "memory/cards/a.md", "issue_type": "missing-freshness"},
        TODAY,
    )
    assert prepared["candidate_fields"] == {"fresh_until": "2027-01-30"}
