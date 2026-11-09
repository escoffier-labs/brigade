"""A create-card must not take a card id that another card already holds (#2425).

`memory hub push` counts the id each card gets from `id`, then `topic`, then the
file stem, and refuses every card in a collision. Lint and ingest read the same
derivation, so they refuse the new card before it lands.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from brigade import ingest as ingest_mod
from brigade import memory_cmd, memory_hub_sync
from brigade.handoff_cmd import linting
from brigade.handoff_ingest_rules import CardIdIndex, card_id_collision_problem, card_state_problems
from brigade.install import install_selection
from brigade.selection import Selection

MINTED = "card-11111111-1111-4111-8111-111111111111"
OTHER_MINTED = "card-22222222-2222-4222-8222-222222222222"

EXISTING_BY_TOPIC = """---
topic: example-widget
category: fixture
tags: [fixture]
---

# Existing widget

Body of the existing card.
"""

EXISTING_BY_ID = f"""---
id: {MINTED}
topic: unrelated-topic
category: fixture
tags: [fixture]
---

# Existing by id

Body of the existing card.
"""


def _card_text(*, topic: str = "example-widget", card_id: str | None = None, body: str = "new card") -> str:
    id_line = f"id: {card_id}\n" if card_id else ""
    return f"---\n{id_line}topic: {topic}\ncategory: fixture\ntags: [fixture]\n---\n\n# {body.title()}\n\nBody of the {body}.\n"


def _handoff(action: str, *, card: str, content: str, sha: str | None = None) -> str:
    parts = [
        "# Memory Handoff",
        "## Type\n\nworkflow",
        "## Title\n\nCollision check",
        "## Summary\n\nChecks the card id guard.",
        f"## Recommended memory action\n\n{action}",
        f"## Target card\n\n{card}",
    ]
    if sha is not None:
        parts.append(f"## Expected card SHA256\n\n{sha}")
    parts.append(f"## Suggested card content\n\n{content}")
    return "\n\n".join(parts) + "\n"


def _workspace(tmp_path: Path, cards: dict[str, str]) -> Path:
    directory = tmp_path / "memory" / "cards"
    directory.mkdir(parents=True)
    for name, text in cards.items():
        (directory / name).write_text(text, encoding="utf-8")
    return tmp_path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- lint: create-card ------------------------------------------------------------


def test_create_card_colliding_by_topic_is_rejected_and_names_the_file(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})

    result = linting.lint_text(_handoff("create-card", card="example-widget.md", content=_card_text()), workspace=ws)

    assert not result.valid
    problems = [error for error in result.errors if error.startswith("card-id-collision")]
    assert len(problems) == 1
    assert "memory/cards/existing-widget.md" in problems[0]
    assert "`example-widget`" in problems[0]


def test_create_card_colliding_by_explicit_id_is_rejected(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-by-id.md": EXISTING_BY_ID})

    result = linting.lint_text(
        _handoff("create-card", card="new-card.md", content=_card_text(topic="fresh", card_id=MINTED)), workspace=ws
    )

    assert not result.valid
    assert any("card-id-collision" in error and "memory/cards/existing-by-id.md" in error for error in result.errors)


def test_create_card_with_its_own_minted_id_is_accepted(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})

    result = linting.lint_text(
        _handoff("create-card", card="example-widget.md", content=_card_text(card_id=OTHER_MINTED)), workspace=ws
    )

    assert result.valid, result.errors


def test_create_card_with_a_free_id_is_accepted(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})

    result = linting.lint_text(
        _handoff("create-card", card="gadget.md", content=_card_text(topic="gadget")), workspace=ws
    )

    assert result.valid, result.errors


def test_file_name_collision_still_reports_the_existing_target(tmp_path: Path):
    ws = _workspace(tmp_path, {"example-widget.md": EXISTING_BY_TOPIC})

    result = linting.lint_text(_handoff("create-card", card="example-widget.md", content=_card_text()), workspace=ws)

    assert any("target card already exists" in error for error in result.errors)
    assert not any(error.startswith("card-id-collision") for error in result.errors)


# --- lint: update-card ------------------------------------------------------------


def test_update_card_on_the_card_holding_the_id_stays_allowed(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})
    revised = EXISTING_BY_TOPIC.replace("Body of the existing card.", "Body, revised.")

    result = linting.lint_text(
        _handoff("update-card", card="existing-widget.md", content=revised, sha=_sha(EXISTING_BY_TOPIC)),
        workspace=ws,
    )

    assert result.valid, result.errors


def test_update_card_is_not_blocked_by_a_collision_it_did_not_create(tmp_path: Path):
    # Two cards already share an id. Updating one must stay possible so the fix can land.
    ws = _workspace(
        tmp_path,
        {"existing-widget.md": EXISTING_BY_TOPIC, "second-widget.md": EXISTING_BY_TOPIC.replace("Existing", "Second")},
    )
    revised = EXISTING_BY_TOPIC.replace("Body of the existing card.", "Body, revised.").replace(
        "topic: example-widget", f"id: {OTHER_MINTED}\ntopic: example-widget"
    )

    result = linting.lint_text(
        _handoff("update-card", card="existing-widget.md", content=revised, sha=_sha(EXISTING_BY_TOPIC)),
        workspace=ws,
    )

    assert result.valid, result.errors


def test_card_state_problems_checks_nothing_without_the_content_or_the_workspace(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})

    without_content, _ = card_state_problems(
        action="create-card", target_card="example-widget.md", expected_sha="", workspace=ws
    )
    no_workspace, _ = card_state_problems(
        action="create-card",
        target_card="example-widget.md",
        expected_sha="",
        workspace=None,
        suggested_content=_card_text(),
    )

    assert without_content == []
    assert no_workspace == []


# --- the id helper matches push --------------------------------------------------


def test_the_id_index_matches_the_ids_push_counts(tmp_path: Path):
    ws = _workspace(
        tmp_path,
        {
            "by-topic.md": EXISTING_BY_TOPIC,
            "by-id.md": EXISTING_BY_ID,
            "no-key.md": "---\ncategory: fixture\ntags: [fixture]\n---\n\n# Stem only\n\nBody.\n",
            "odd-topic.md": _card_text(topic="Has Spaces And Capitals"),
        },
    )

    pushed = {card["rel"]: card["card_id"] for card in memory_hub_sync._local_cards(ws)}
    index = CardIdIndex(ws)

    assert len(pushed) == 4
    for rel, card_id in pushed.items():
        assert index.holders(card_id) == [rel]
        text = (ws / rel).read_text(encoding="utf-8")
        assert memory_hub_sync.publication_identity(text, rel)[0] == card_id
    assert pushed["memory/cards/by-topic.md"] == "example-widget"
    assert pushed["memory/cards/by-id.md"] == MINTED


def test_a_colliding_create_card_is_what_push_would_refuse(tmp_path: Path):
    ws = _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})
    content = _card_text()

    problem = card_id_collision_problem(CardIdIndex(ws), content, "example-widget.md")
    (ws / "memory" / "cards" / "example-widget.md").write_text(content, encoding="utf-8")
    ids = [card["card_id"] for card in memory_hub_sync._local_cards(ws)]

    assert problem is not None
    assert ids.count("example-widget") == 2


# --- one index per run -----------------------------------------------------------


def test_lint_targets_reads_the_cards_once_for_many_handoffs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _workspace(tmp_path, {"existing-widget.md": EXISTING_BY_TOPIC})
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    paths = []
    for number in range(3):
        path = inbox / f"2026-11-09-100{number}-card-{number}.md"
        path.write_text(
            _handoff("create-card", card=f"card-{number}.md", content=_card_text(topic=f"topic-{number}")),
            encoding="utf-8",
        )
        paths.append(path)
    calls: list[Path] = []
    real = memory_cmd.search_card_fields
    monkeypatch.setattr(memory_cmd, "search_card_fields", lambda target, *a, **k: calls.append(target) or real(target))

    results = linting.lint_targets(tmp_path, paths=paths)

    assert len(results) == 3
    assert all(result.valid for result in results), [result.errors for result in results]
    assert len(calls) == 1


# --- ingest ----------------------------------------------------------------------


def _inbox(tmp_path: Path) -> Path:
    install_selection(tmp_path, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    return tmp_path / ".claude" / "memory-handoffs"


def _write(inbox: Path, name: str, card: str, content: str) -> Path:
    path = inbox / name
    path.write_text(_handoff("create-card", card=card, content=content), encoding="utf-8")
    return path


def _ingest(tmp_path: Path, *, dry_run: bool = False) -> ingest_mod.IngestStats:
    stats = ingest_mod.IngestStats()
    assert (
        ingest_mod.ingest_into(
            source=tmp_path, owner=tmp_path, stats=stats, dry_run=dry_run, promote_cards=True, route_documents=True
        )
        == 0
    )
    return stats


def test_ingest_rejects_an_explicit_id_collision_and_never_writes_the_card(tmp_path: Path):
    inbox = _inbox(tmp_path)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "existing-by-id.md").write_text(EXISTING_BY_ID, encoding="utf-8")
    _write(inbox, "2026-11-09-1000-clash.md", "clash.md", _card_text(topic="fresh", card_id=MINTED))

    stats = _ingest(tmp_path)

    assert not (cards / "clash.md").exists()
    assert (cards / "existing-by-id.md").read_text(encoding="utf-8") == EXISTING_BY_ID
    assert stats.inboxed == 1 and stats.promoted == 0 and stats.failed == 0
    assert any("card-id-collision" in action for action in stats.actions)
    drafts = list((tmp_path / "memory" / "handoff-inbox").glob("*"))
    assert drafts, "the rejected handoff is kept for review"
    assert "reason: card-id-collision" in drafts[0].read_text(encoding="utf-8")


def test_ingest_rejection_is_isolated_to_the_colliding_handoff(tmp_path: Path):
    inbox = _inbox(tmp_path)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "existing-by-id.md").write_text(EXISTING_BY_ID, encoding="utf-8")
    _write(inbox, "2026-11-09-1000-clash.md", "clash.md", _card_text(topic="fresh", card_id=MINTED))
    _write(inbox, "2026-11-09-1001-fine.md", "fine.md", _card_text(topic="fine-topic"))

    stats = _ingest(tmp_path)

    assert not (cards / "clash.md").exists()
    assert (cards / "fine.md").is_file()
    assert stats.inboxed == 1 and stats.promoted == 1


def test_ingest_dry_run_reports_the_rejection_and_writes_nothing(tmp_path: Path):
    inbox = _inbox(tmp_path)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "existing-by-id.md").write_text(EXISTING_BY_ID, encoding="utf-8")
    _write(inbox, "2026-11-09-1000-clash.md", "clash.md", _card_text(topic="fresh", card_id=MINTED))

    stats = _ingest(tmp_path, dry_run=True)

    assert any("card-id-collision" in action for action in stats.actions)
    assert not (cards / "clash.md").exists()
    assert not (tmp_path / "memory" / "handoff-inbox").exists() or not list(
        (tmp_path / "memory" / "handoff-inbox").glob("*")
    )


def test_two_handoffs_in_one_run_cannot_claim_the_same_id(tmp_path: Path):
    inbox = _inbox(tmp_path)
    _write(inbox, "2026-11-09-1000-first.md", "first.md", _card_text(topic="first-topic", card_id=MINTED))
    _write(
        inbox,
        "2026-11-09-1001-second.md",
        "second.md",
        _card_text(topic="second-topic", card_id=MINTED, body="second distinct card"),
    )

    stats = _ingest(tmp_path)

    cards = tmp_path / "memory" / "cards"
    assert (cards / "first.md").is_file()
    assert not (cards / "second.md").exists()
    assert stats.promoted == 1 and stats.inboxed == 1
    assert any("card-id-collision" in action for action in stats.actions)


def test_ingest_gives_an_id_less_topic_collision_a_minted_id(tmp_path: Path):
    inbox = _inbox(tmp_path)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "existing-widget.md").write_text(EXISTING_BY_TOPIC, encoding="utf-8")
    _write(inbox, "2026-11-09-1000-widget.md", "example-widget.md", _card_text())

    stats = _ingest(tmp_path)

    assert stats.promoted == 1
    ids = [card["card_id"] for card in memory_hub_sync._local_cards(tmp_path)]
    assert len(ids) == len(set(ids)), "push would count no collision"
    written = (cards / "example-widget.md").read_text(encoding="utf-8")
    assert "\nid: card-" in written


def test_a_failed_archive_still_blocks_a_second_claim_on_the_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    inbox = _inbox(tmp_path)
    _write(inbox, "2026-11-09-1000-first.md", "first.md", _card_text(topic="first-topic", card_id=MINTED))
    _write(
        inbox,
        "2026-11-09-1001-second.md",
        "second.md",
        _card_text(topic="second-topic", card_id=MINTED, body="second distinct card"),
    )
    real_archive = ingest_mod._archive

    def archive(src: Path, processed: Path, expected_sha: str | None = None) -> Path:
        if "first" in src.name:
            raise OSError("archive failed after the card was written")
        return real_archive(src, processed, expected_sha)

    monkeypatch.setattr(ingest_mod, "_archive", archive)

    stats = _ingest(tmp_path)

    cards = tmp_path / "memory" / "cards"
    assert (cards / "first.md").is_file()
    assert not (cards / "second.md").exists()
    assert stats.failed == 1
    assert stats.inboxed == 1 and stats.promoted == 0
    assert any("card-id-collision" in action for action in stats.actions)


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_update_without_an_id_keeps_the_original_id_claimed(tmp_path: Path, dry_run: bool):
    inbox = _inbox(tmp_path)
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "existing-by-id.md").write_text(EXISTING_BY_ID, encoding="utf-8")
    revised = _card_text(topic="unrelated-topic", body="revised existing card")
    (inbox / "2026-11-09-1000-update.md").write_text(
        _handoff("update-card", card="existing-by-id.md", content=revised, sha=_sha(EXISTING_BY_ID)),
        encoding="utf-8",
    )
    _write(
        inbox,
        "2026-11-09-1001-claim.md",
        "claim.md",
        _card_text(topic="claimant", card_id=MINTED, body="claiming card"),
    )

    stats = _ingest(tmp_path, dry_run=dry_run)

    # The same answer in a dry run and a real run: the update promotes, the create collides.
    assert stats.promoted == 1 and stats.inboxed == 1
    assert [action for action in stats.actions if "card-id-collision" in action]
    assert not (cards / "claim.md").exists()
