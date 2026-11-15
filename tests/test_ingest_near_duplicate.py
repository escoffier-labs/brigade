"""Ingest uses reflect's deterministic near-duplicate policy."""

from pathlib import Path

from brigade import ingest
from brigade.install import install_selection
from brigade.selection import Selection

BODY = " ".join(f"word{i}" for i in range(100))


def card(body: str, topic: str = "original") -> str:
    return f"---\ntopic: {topic}\ncategory: test\ntags: [test]\n---\n\n{body}\n"


def seed(target: Path) -> Path:
    install_selection(target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    return target / ".claude/memory-handoffs"


def handoff(inbox: Path, name: str, body: str) -> None:
    (inbox / f"{name}.md").write_text(
        f"## Recommended memory action\ncreate-card\n\n## Target card\n{name}.md\n\n"
        f"## Suggested card content\n{card(body, name)}"
    )


def test_exact_duplicate_leaves_inbox_receipt(tmp_target: Path):
    inbox = seed(tmp_target)
    existing = tmp_target / "memory/cards/original.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(card(BODY))
    handoff(inbox, "copy", BODY)
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    assert not (existing.parent / "copy.md").exists()
    drafts = list((tmp_target / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert "card-id=original" in drafts[0].read_text()
    assert "similarity=1.00" in drafts[0].read_text()


def test_reflect_threshold_and_claim_anchors(tmp_target: Path):
    inbox = seed(tmp_target)
    existing = tmp_target / "memory/cards/original.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(card(BODY + " ^c-aaaaaa"))
    config = tmp_target / ".brigade/memory-care.toml"
    config.write_text("[reflect]\nnear_duplicate_threshold = 0.98\n")
    handoff(inbox, "copy", BODY + " extra ^c-bbbbbb")
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    drafts = list((tmp_target / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert "card-id=original" in drafts[0].read_text()
    assert "similarity=0.99" in drafts[0].read_text()
    assert not (existing.parent / "copy.md").exists()


def test_same_session_duplicates_merge_pending(tmp_target: Path):
    inbox = seed(tmp_target)
    existing = tmp_target / "memory/cards/original.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(card(BODY))
    before = set(existing.parent.glob("*.md"))
    handoff(inbox, "copy-a", BODY + " extra")
    handoff(inbox, "copy-b", BODY + " extra")
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    drafts = list((tmp_target / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    text = drafts[0].read_text()
    assert "copy-a.md" in text and "copy-b.md" in text
    assert set(existing.parent.glob("*.md")) == before


def test_distinct_handoff_creates_card(tmp_target: Path):
    inbox = seed(tmp_target)
    handoff(inbox, "distinct", "A separate fact about preparing a fresh testing database.")
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    assert (tmp_target / "memory/cards/distinct.md").is_file()


def test_lower_reflect_threshold_catches_match_below_legacy_overlap(tmp_target: Path):
    inbox = seed(tmp_target)
    existing = tmp_target / "memory/cards/original.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(card(BODY))
    (tmp_target / ".brigade/memory-care.toml").write_text("[reflect]\nnear_duplicate_threshold = 0.85\n")
    handoff(inbox, "copy", BODY + " " + " ".join(f"extra{i}" for i in range(15)))
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    assert not (existing.parent / "copy.md").exists()
    drafts = list((tmp_target / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert "similarity=0.86" in drafts[0].read_text()


def test_explicit_scopes_do_not_match(tmp_target: Path):
    inbox = seed(tmp_target)
    existing = tmp_target / "memory/cards/original.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(card(BODY).replace("topic:", "scope: other\ntopic:"))
    handoff(inbox, "copy", BODY)
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    assert (existing.parent / "copy.md").is_file()


def test_duplicate_of_card_created_earlier_in_run(tmp_target: Path):
    inbox = seed(tmp_target)
    handoff(inbox, "a-first", BODY)
    handoff(inbox, "b-copy", BODY)
    assert ingest.run(target=tmp_target, promote_cards=True) == 0
    assert (tmp_target / "memory/cards/a-first.md").exists()
    assert not (tmp_target / "memory/cards/b-copy.md").exists()
    drafts = list((tmp_target / "memory/handoff-inbox").glob("*.md"))
    assert len(drafts) == 1
    assert "card-id=card-" in drafts[0].read_text()
