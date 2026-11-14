"""An overflow card is only ever written when this feature created it, within a size that can be searched.

The card path is predictable and the handoff is untrusted: a planted card must never receive handoff
bytes, a card larger than the duplicate scan reads must never be written, and a replay must find
content wherever it was published.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date

import pytest

from brigade import ingest, instruction_overflow, memory_cmd, memory_inbox_drain as drain
from brigade import memory_care_journal as journal
from brigade.instruction_overflow import CARDS_REL, RouteContext, route
from brigade.memory_hub_sync import VISIBILITY_PRIVATE, withheld
from tests._home import set_home
from tests._instruction_fixtures import CARD_HEAD, field_workspace, handoff_text, sized, write_handoff

NOTE = "Prefer one-line status updates."  # append_block(NOTE) is 34 bytes
CARD = f"{CARDS_REL}/user-md--overflow-preference.md"
CARD_2 = f"{CARDS_REL}/user-md--overflow-preference-2.md"
TODAY = date(2026, 11, 13)
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX hard links and symlinks")

PUBLIC = CARD_HEAD.replace("visibility: private", "visibility: public")
PLANTS = {
    "public": PUBLIC + "\nplanted\n",
    "no-visibility": CARD_HEAD.replace("visibility: private\n", "") + "\nplanted\n",
    "no-frontmatter": "planted, no frontmatter at all\n",
    "two-blocks": CARD_HEAD + "---\nvisibility: public\ncategory: x\n---\nplanted\n",
    "two-visibility-lines": CARD_HEAD.replace("visibility: private\n", "visibility: private\nvisibility: public\n")
    + "\nplanted\n",
    "not-archive-never": CARD_HEAD.replace("archive: never", "archive: yes") + "\nplanted\n",
    "no-marker-tag": CARD_HEAD.replace("moved-instructions, ", "") + "\nplanted\n",
    "no-source-file": CARD_HEAD.replace("source_file: USER.md\n", "") + "\nplanted\n",
}


def _cap_cards(ws, max_card_bytes=600):
    (ws / ".brigade").mkdir(exist_ok=True)
    (ws / ".brigade" / "memory-care.toml").write_text(f"max_card_bytes = {max_card_bytes}\n")


def _sections(content=NOTE):
    return {
        "type": "preference",
        "title": "T",
        "summary": "S.",
        "recommended memory action": "no-card",
        "target document": "USER.md",
        "suggested document content": content,
    }


def _ingest(ws, name="2026-11-13-pref.md", content=NOTE):
    write_handoff(ws, name, handoff_text("USER.md", content, type_="preference"))
    return ingest.run(target=ws, promote_cards=True, route_documents=True)


def _drafts(ws):
    inbox = ws / "memory" / "handoff-inbox"
    return sorted(inbox.glob("*.md")) if inbox.exists() else []


def _held(ws, content=NOTE, number=0):
    """A draft an earlier release held for a full instruction file, so the drain retries it."""
    source = write_handoff(ws, f"2026-10-01-{number}.md", handoff_text("USER.md", content, type_="preference"))
    ingest._execute(
        ingest.Outcome("inboxed", code="bootstrap-budget-exceeded"),
        source,
        ws,
        {},
        ws / drain.INBOX_REL,
        source.parent / "processed",
        False,
        snapshot=source.read_bytes(),
    )
    return sorted((ws / drain.INBOX_REL).glob("*.md"))[-1]


def _plant(ws, text, rel=CARD):
    path = ws / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _no_note_in_cards(ws):
    cards = ws / CARDS_REL
    return not cards.exists() or all(NOTE not in p.read_text() for p in cards.iterdir() if p.is_file())


# --- A1: a planted card never receives handoff bytes ---------------------------------------------


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_planted_card_gets_no_handoff_bytes_through_ingest(tmp_path, monkeypatch, plant):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    planted = _plant(ws, PLANTS[plant])
    before = planted.read_bytes()
    outcome = ingest.decide(_sections(), ws, promote_cards=True, route_documents=True)
    assert outcome.code == "overflow-card-untrusted" and planted.name in outcome.reason
    assert _ingest(ws) == 0
    assert planted.read_bytes() == before
    assert not [d for d in journal.entries(ws) if d.reason == "ingest:document-routed-to-card"]
    (draft,) = _drafts(ws)
    assert "overflow-card-untrusted" in draft.read_text()


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_planted_card_gets_no_handoff_bytes_through_retry(tmp_path, monkeypatch, plant):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    draft = _held(ws)
    planted = _plant(ws, PLANTS[plant])
    before = planted.read_bytes()
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 0, result
    assert planted.read_bytes() == before and draft.exists()
    assert not [d for d in journal.entries(ws) if d.reason.startswith("retry:document-routed-to-card")]


def test_a_planted_rollover_card_gets_no_handoff_bytes(tmp_path, monkeypatch):
    # Card 1 is a real, full card. The card the append would roll into is planted and public.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    sized(ws / CARD, 590, head=CARD_HEAD)
    planted = _plant(ws, PUBLIC + "\nplanted\n", CARD_2)
    before = planted.read_bytes()
    assert _ingest(ws) == 0
    assert planted.read_bytes() == before and _no_note_in_cards(ws)
    # and the same through retry
    draft = _held(ws, number=5)
    assert drain.drain(ws, today=TODAY, deadline_days=14)["retried"] == 0
    assert planted.read_bytes() == before and draft.exists()


def test_a_card_planted_after_decide_is_caught_under_the_lock(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    handoff = write_handoff(ws, "2026-11-13-pref.md", handoff_text("USER.md", NOTE, type_="preference"))
    sections = ingest.parse(handoff)
    outcome = ingest.decide(sections, ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "routed-card"
    planted = _plant(ws, PLANTS["public"])
    before = planted.read_bytes()
    action = ingest._execute(
        outcome,
        handoff,
        ws,
        sections,
        ws / "memory" / "handoff-inbox",
        handoff.parent / "processed",
        False,
        snapshot=handoff.read_bytes(),
    )
    assert action.kind == "inboxed" and "overflow-card-untrusted" in action.summary
    assert planted.read_bytes() == before


def test_every_card_this_feature_writes_is_private_to_the_hub_and_passes_its_own_check(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    for index in range(4):
        assert _ingest(ws, name=f"2026-11-13-{index}.md", content=f"{NOTE} {index} " + "pad " * 40) == 0
    cards = sorted((ws / CARDS_REL).iterdir())
    assert len(cards) >= 2, "the cap must have rolled the chain"
    for card in cards:
        text = card.read_text()
        assert withheld(text) == VISIBILITY_PRIVATE, card.name
        assert instruction_overflow.card_trust_problem(text) is None, card.name


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_the_trust_check_refuses_what_the_hub_would_publish_or_cannot_vouch_for(plant):
    assert instruction_overflow.card_trust_problem(PLANTS[plant]) is not None
    if plant in ("public", "no-visibility"):
        assert withheld(PLANTS[plant]) is None  # the hub would publish it: the two parsers agree


def test_content_that_would_form_a_second_frontmatter_block_is_held_before_a_card_exists(tmp_path, monkeypatch):
    # Every card written must pass the check that guards the next append, or it would hold its own feed.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    assert _ingest(ws, content="---\nvisibility: public\ncategory: x\n---\nrest") == 0
    assert not (ws / CARDS_REL).exists()
    (draft,) = _drafts(ws)
    assert "overflow-block-too-large" in draft.read_text()


# --- A2: the card limit bounds a new card and the duplicate scan can read every card -------------


def _first_card_size(tmp_path, monkeypatch, content=NOTE):
    ws, home, env = field_workspace(tmp_path / "probe")
    set_home(monkeypatch, home)
    assert _ingest(ws, content=content) == 0
    return (ws / CARD).stat().st_size


def test_a_first_block_at_the_card_limit_is_written_and_one_byte_over_is_held(tmp_path, monkeypatch):
    size = _first_card_size(tmp_path, monkeypatch)
    ws, home, env = field_workspace(tmp_path / "at-limit")
    set_home(monkeypatch, home)
    _cap_cards(ws, size)
    assert _ingest(ws) == 0
    assert (ws / CARD).stat().st_size == size  # frontmatter is counted, and it fits exactly

    ws2, home2, env2 = field_workspace(tmp_path / "over")
    set_home(monkeypatch, home2)
    _cap_cards(ws2, size - 1)
    assert _ingest(ws2) == 0
    assert not (ws2 / CARDS_REL).exists() and (ws2 / "USER.md").stat().st_size == 8241
    (draft,) = _drafts(ws2)
    assert "overflow-block-too-large" in draft.read_text()
    outcome = ingest.decide(_sections(), ws2, promote_cards=True, route_documents=True)
    assert outcome.code == "overflow-block-too-large" and str(size - 1) in outcome.reason


def test_a_block_larger_than_the_whole_limit_is_never_written_oversize(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws, 300)
    assert _ingest(ws, content="long line " * 60) == 0
    assert not (ws / CARDS_REL).exists()


def test_an_append_to_an_existing_card_may_reach_the_limit_exactly(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    card = sized(ws / CARD, 600 - 34, head=CARD_HEAD)  # + the 34 byte block is exactly 600
    assert _ingest(ws) == 0
    assert card.stat().st_size == 600
    card2 = sized(ws / CARD_2, 600 - 35, head=CARD_HEAD)
    assert _ingest(ws, name="2026-11-13-two.md", content=NOTE + "!") == 0  # 35 bytes: card 1 is full, card 2 fits
    assert card2.stat().st_size == 600 and card.stat().st_size == 600


def test_the_limit_is_clamped_to_what_the_duplicate_scan_reads_and_the_request_is_recorded(tmp_path, monkeypatch):
    size = _first_card_size(tmp_path, monkeypatch)
    ws, home, env = field_workspace(tmp_path / "clamped")
    set_home(monkeypatch, home)
    _cap_cards(ws, 5000)
    monkeypatch.setattr(instruction_overflow, "INSPECTION_CEILING", size + 100)
    found = route(ws, "USER.md", NOTE, "preference", home=home, environ=env)
    assert (found.card_limit, found.limit_requested) == (size + 100, 5000)
    assert _ingest(ws) == 0
    (decision,) = [d for d in journal.entries(ws) if d.reason == "ingest:document-routed-to-card"]
    payload = journal._payload(ws, decision.decision_id)
    assert (payload["card_limit"], payload["card_limit_requested"]) == (size + 100, 5000)
    unclamped = route(ws, "USER.md", NOTE, "preference", home=home, environ=env)
    assert unclamped.cards == (CARD,)


@pytest.mark.parametrize("archive_fails", [False, True])
def test_a_card_the_scan_cannot_read_is_never_written_and_a_replay_never_duplicates(
    tmp_path, monkeypatch, archive_fails
):
    size = _first_card_size(tmp_path, monkeypatch)
    ws, home, env = field_workspace(tmp_path / "ceiling")
    set_home(monkeypatch, home)
    _cap_cards(ws, 5000)  # far above what the scan may read
    monkeypatch.setattr(instruction_overflow, "INSPECTION_CEILING", size - 1)
    if archive_fails:
        real, calls = ingest._archive, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("archive failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(ingest, "_archive", flaky)
    _ingest(ws)
    ingest.run(target=ws, promote_cards=True, route_documents=True)  # the replay
    cards = list((ws / CARDS_REL).iterdir()) if (ws / CARDS_REL).exists() else []
    assert all(card.stat().st_size <= size - 1 for card in cards)
    assert sum(card.read_text().count(NOTE) for card in cards) <= 1


def test_a_replay_with_a_card_exactly_at_the_ceiling_finds_the_block(tmp_path, monkeypatch):
    size = _first_card_size(tmp_path, monkeypatch)
    ws, home, env = field_workspace(tmp_path / "at-ceiling")
    set_home(monkeypatch, home)
    _cap_cards(ws, 5000)
    monkeypatch.setattr(instruction_overflow, "INSPECTION_CEILING", size)
    real, calls = ingest._archive, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("archive failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "_archive", flaky)
    _ingest(ws)
    assert (ws / CARD).stat().st_size == size
    ingest.run(target=ws, promote_cards=True, route_documents=True)
    assert (ws / CARD).read_text().count(NOTE) == 1 and not (ws / CARD_2).exists()


def test_an_existing_card_over_the_ceiling_holds_the_handoff_instead_of_hiding_a_replay(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    monkeypatch.setattr(instruction_overflow, "INSPECTION_CEILING", 300)
    big = sized(ws / CARD, 400, head=CARD_HEAD)
    assert _ingest(ws) == 0
    assert big.stat().st_size == 400 and not (ws / CARD_2).exists()
    (draft,) = _drafts(ws)
    assert "overflow-card-untrusted" in draft.read_text()


# --- A3: a hard link swapped in before the write cannot redirect it ------------------------------


@POSIX_ONLY
@pytest.mark.parametrize("existing", [False, True])
def test_a_hard_link_swapped_in_before_the_open_never_redirects_a_card_append(tmp_path, monkeypatch, existing):
    from brigade import dirfd

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside file, not a card\n")
    card = ws / CARD
    if existing:
        sized(card, 200, head=CARD_HEAD)
    real_open = dirfd.open_child_file

    def swapped(parent, name, flags, mode=0o777):
        if name == card.name:
            card.unlink(missing_ok=True)
            os.link(outside, card)
        return real_open(parent, name, flags, mode)

    monkeypatch.setattr(dirfd, "open_child_file", swapped)
    assert _ingest(ws) == 0
    assert outside.read_bytes() == b"outside file, not a card\n"
    (draft,) = _drafts(ws)
    assert "document-destination-unwritable" in draft.read_text()


@POSIX_ONLY
def test_a_hard_link_swapped_in_before_the_open_never_redirects_a_direct_append(tmp_path, monkeypatch):
    from brigade import dirfd

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside file\n")
    real_open = dirfd.open_child_file

    def swapped(parent, name, flags, mode=0o777):
        if name == "USER.md":
            (ws / "USER.md").unlink()
            os.link(outside, ws / "USER.md")
        return real_open(parent, name, flags, mode)

    monkeypatch.setattr(dirfd, "open_child_file", swapped)
    with pytest.raises(OSError):
        ingest._append_document_nofollow(ws, ws / "USER.md", "\n\nnote\n")
    assert outside.read_bytes() == b"outside file\n"
    assert _ingest(ws) == 0  # the same swap through a full run
    assert outside.read_bytes() == b"outside file\n"


# --- B: Codex review threads ---------------------------------------------------------------------


def test_a_file_missing_at_discovery_is_measured_again_after_an_earlier_handoff_creates_it(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / "USER.md").unlink()
    act_at = instruction_overflow.find(RouteContext(ws, home, env).load()[0], "USER.md").act_at
    chunk = int(act_at * 0.6)  # one fits, the second would pass act-at
    write_handoff(ws, "2026-11-13-1.md", handoff_text("USER.md", "a" * chunk, type_="preference"))
    write_handoff(ws, "2026-11-13-2.md", handoff_text("USER.md", "b" * chunk, type_="preference"))
    assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    assert (ws / "USER.md").stat().st_size <= act_at
    assert ("b" * chunk) in (ws / CARD).read_text() and ("b" * chunk) not in (ws / "USER.md").read_text()


def test_cards_beyond_a_gap_in_the_chain_still_protect_a_replay(tmp_path, monkeypatch):
    # An undo deleted card 1 and left card 2. The content in card 2 must not be written again into a new card 1.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    card_2 = _plant(ws, CARD_HEAD + "\n" + NOTE + "\n", CARD_2)
    before = card_2.read_bytes()
    found = route(ws, "USER.md", NOTE, "preference", home=home, environ=env)
    assert found.cards == (CARD_2,)
    assert _ingest(ws) == 0
    assert not (ws / CARD).exists() and card_2.read_bytes() == before
    (draft,) = _drafts(ws)
    assert "document-content-present" in draft.read_text()


def test_new_content_fills_the_first_gap_in_the_chain(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _plant(ws, CARD_HEAD + "\nOther note.\n", CARD_2)
    assert _ingest(ws) == 0
    assert (ws / CARD).read_text().endswith("\n" + NOTE + "\n")


def test_a_chain_of_full_cards_that_is_too_long_holds_the_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    monkeypatch.setattr(instruction_overflow, "_MAX_CARDS", 2)
    sized(ws / CARD, 590, head=CARD_HEAD)
    sized(ws / CARD_2, 590, head=CARD_HEAD)
    outcome = ingest.decide(_sections(), ws, promote_cards=True, route_documents=True)
    assert outcome.code == "document-destination-unsafe" and "too long" in outcome.reason
    assert _ingest(ws) == 0
    assert _no_note_in_cards(ws)


# --- C: review notes -----------------------------------------------------------------------------


def test_the_direct_path_finds_content_already_published_to_a_card(tmp_path, monkeypatch):
    # A card took the block, archiving failed, then a person trimmed USER.md below act-at before the replay.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    _plant(ws, CARD_HEAD + "\n" + NOTE + "\n")
    assert _ingest(ws) == 0
    assert (ws / "USER.md").stat().st_size == 100
    (draft,) = _drafts(ws)
    assert "document-content-present" in draft.read_text()


def test_a_line_of_an_unrelated_card_does_not_hold_a_new_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _plant(ws, CARD_HEAD + "\n" + NOTE + "\nA second line that is not in the handoff.\n")
    outcome = ingest.decide(
        _sections(NOTE + "\nA different second line."), ws, promote_cards=True, route_documents=True
    )
    assert outcome.kind == "routed-card", outcome


@POSIX_ONLY
def test_a_card_problem_found_under_the_lock_holds_with_the_same_code_as_decide(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    handoff = write_handoff(ws, "2026-11-13-pref.md", handoff_text("USER.md", NOTE, type_="preference"))
    sections = ingest.parse(handoff)
    outcome = ingest.decide(sections, ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "routed-card"
    outside = sized(tmp_path / "outside.md", 50)
    (ws / CARDS_REL).mkdir(parents=True)
    (ws / CARD).symlink_to(outside)  # a link appeared after decide
    action = ingest._execute(
        outcome,
        handoff,
        ws,
        sections,
        ws / "memory" / "handoff-inbox",
        handoff.parent / "processed",
        False,
        snapshot=handoff.read_bytes(),
    )
    assert "document-destination-unsafe" in action.summary and outside.stat().st_size == 50


def test_a_bug_in_building_the_block_is_not_swallowed_as_a_held_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    handoff = write_handoff(ws, "2026-11-13-pref.md", handoff_text("USER.md", NOTE, type_="preference"))
    sections = ingest.parse(handoff)
    outcome = ingest.decide(sections, ws, promote_cards=True, route_documents=True)

    def broken(*args, **kwargs):
        raise ValueError("bug in card_block")

    monkeypatch.setattr(instruction_overflow, "card_block", broken)
    with pytest.raises(ValueError, match="bug in card_block"):
        ingest._execute(
            outcome,
            handoff,
            ws,
            sections,
            ws / "memory" / "handoff-inbox",
            handoff.parent / "processed",
            False,
            snapshot=handoff.read_bytes(),
        )


def test_an_unparseable_care_configuration_is_retried_automatically_once_it_parses(tmp_path, monkeypatch):
    # Nothing is lost and no person is involved: ingest keeps the handoff under a retry code, and the drain
    # delivers it as soon as the configuration parses.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    config = ws / ".brigade" / "memory-care.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text("[instructions\nnot toml")
    assert _ingest(ws) == 0
    assert not (ws / CARDS_REL).exists()
    (draft,) = _drafts(ws)
    text = draft.read_text()
    assert "document-destination-unwritable" in text
    assert "document-destination-unwritable" in drain.RETRY_CODES
    assert drain.classify("document-destination-unwritable", text) == "retry"
    still_broken = drain.drain(ws, today=TODAY, deadline_days=14)
    assert still_broken["retried"] == 0 and draft.exists()  # kept, not lost, not given to a person
    config.write_text("max_card_bytes = 12000\n")
    result = drain.drain(ws, today=TODAY, deadline_days=14)
    assert result["retried"] == 1 and result["kept_for_person"] == 0, result
    assert (ws / CARD).read_text().endswith("\n" + NOTE + "\n") and not draft.exists()


def test_a_card_title_and_source_name_prefer_the_workspace_path(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    file = instruction_overflow.find(RouteContext(ws, home, env).load()[0], "USER.md")
    for paths in (("~/shared/USER.md", "USER.md", "<cap>"), ("USER.md", "~/shared/USER.md"), ("<x>", "~/y")):
        picked = instruction_overflow._source_name(replace(file, paths=paths))
        assert picked == ("USER.md" if "USER.md" in paths else file.display)


def test_codes_for_the_new_holds_are_classified_for_a_person():
    for code in ("overflow-card-untrusted", "overflow-block-too-large"):
        assert code in drain.HUMAN_CODES and drain.NEEDS_EVIDENCE_PROOF[code] is False
        assert (
            drain.classify(code, "<!-- routed from a.md\n     reason: x\n     routed-at: 20261101T000000Z -->\n")
            == "human"
        )


def test_config_is_read_through_the_care_configuration_the_ingest_uses(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    _cap_cards(ws, 777)
    assert memory_cmd._config_or_default(ws).max_card_bytes == 777
    assert route(ws, "USER.md", NOTE, "preference", home=home, environ=env).card_limit == 777
