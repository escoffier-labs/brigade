"""Ingest sends an append past a known instruction file's act-at to a private overflow card."""

from __future__ import annotations

import base64
import os
from datetime import date

import pytest

from brigade import ingest, memory_cmd
from brigade import memory_care_journal as journal
from brigade.handoff_cmd import linting
from brigade.instruction_overflow import CARDS_REL, handoff_type, route
from tests._home import set_home
from tests._instruction_fixtures import (
    INJECTION,
    field_workspace,
    handoff_text,
    refuse_subprocess,
    sized,
    write_handoff,
)

TODAY = date(2026, 11, 13)
NOTE = "Prefer one-line status updates."  # append_block(NOTE) is 34 bytes


def _sections(document, content, type_="preference"):
    return {
        "type": type_,
        "title": "T",
        "summary": "S.",
        "recommended memory action": "no-card",
        "target document": document,
        "suggested document content": content,
    }


def _decide(ws, sections):
    return ingest.decide(sections, ws, promote_cards=True, route_documents=True)


def _ingest(ws, name="2026-11-13-pref.md", content=NOTE):
    write_handoff(ws, name, handoff_text("USER.md", content, type_="preference"))
    return ingest.run(target=ws, promote_cards=True, route_documents=True)


def _cap_cards(ws, max_card_bytes=600):
    (ws / ".brigade").mkdir(exist_ok=True)
    (ws / ".brigade" / "memory-care.toml").write_text(f"max_card_bytes = {max_card_bytes}\n")


def test_finding_9_preference_past_act_at_lands_in_the_preference_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 3390)  # 3390 + 34 passes act-at 3400
    assert _ingest(ws) == 0
    assert (ws / "USER.md").stat().st_size == 3390
    card = (ws / CARDS_REL / "user-md--overflow-preference.md").read_text()
    assert "visibility: private" in card and "archive: never" in card and "moved-instructions" in card
    assert card.endswith("\nPrefer one-line status updates.\n")
    (decision,) = [d for d in journal.entries(ws) if d.reason == "ingest:document-routed-to-card"]
    assert journal._payload(ws, decision.decision_id)["appends"][0]["writer"] == "card-append"


def test_overflow_card_frontmatter_is_private_and_pinned_byte_for_byte(tmp_path, monkeypatch):
    # A card the hub could publish would leak handoff content: pin the exact frontmatter fields.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    assert _ingest(ws) == 0  # USER.md is 8241 bytes, past act-at
    raw = (ws / CARDS_REL / "user-md--overflow-preference.md").read_bytes()
    head = raw.split(b"\n---\n", 1)[0].decode("utf-8").splitlines()
    fields = dict(line.split(": ", 1) for line in head[1:])
    assert head[0] == "---"
    assert fields.pop("id")
    assert fields == {
        "title": "USER.md overflow: preference",
        "category": "instructions",
        "tags": "[moved-instructions, user-md, overflow]",
        "visibility": "private",
        "source_file": "USER.md",
        "archive": "never",
        "last_reviewed": date.today().isoformat(),
        "fresh_until": fields["fresh_until"],
    }
    assert raw.count(b"visibility: private\n") == 1 and raw.count(b"archive: never\n") == 1


def test_append_within_act_at_still_goes_to_the_file(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    outcome = _decide(ws, _sections("USER.md", NOTE))
    assert (outcome.kind, outcome.code) == ("routed", "document-appended")
    assert _ingest(ws) == 0
    assert (ws / "USER.md").read_text().endswith("\n\nPrefer one-line status updates.\n")
    assert not (ws / CARDS_REL).exists()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Preference", "preference"),
        (" research \n", "research"),
        ("preferences", "project-context"),
        ("", "project-context"),
        (None, "project-context"),
        ("bugfix", "bugfix"),
    ],
)
def test_handoff_type_maps_to_the_template_values(raw, expected):
    assert handoff_type({} if raw is None else {"type": raw}) == expected


def test_injection_signal_still_goes_to_a_person(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)  # USER.md 8241, past act-at
    outcome = _decide(ws, _sections("USER.md", INJECTION))
    assert outcome.code == "document-injection-signal" and not (ws / CARDS_REL).exists()
    write_handoff(ws, "2026-11-13-inj.md", handoff_text("USER.md", INJECTION, type_="preference"))
    assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    assert not (ws / CARDS_REL).exists() and not journal.entries(ws)
    assert list((ws / "memory" / "handoff-inbox").glob("*.md"))


def test_unsafe_name_never_reaches_routing(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    outcome = _decide(ws, _sections("../USER.md", NOTE))
    assert outcome.code == "unsafe-target-document" and not (ws / CARDS_REL).exists()


def test_overflow_card_rolls_at_max_card_bytes(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    sized(ws / CARDS_REL / "user-md--overflow-preference.md", 590, head="---\ntitle: t\n---\n")
    outcome = _decide(ws, _sections("USER.md", NOTE))
    assert outcome.dest.name == "user-md--overflow-preference-2.md"
    assert outcome.overflow.cards == (f"{CARDS_REL}/user-md--overflow-preference.md",)


@pytest.mark.parametrize("archive_fails", [False, True])
def test_replay_across_the_rollover_never_writes_the_block_twice(
    tmp_path, monkeypatch, archive_fails
):  # Review Focus 6
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    card = sized(ws / CARDS_REL / "user-md--overflow-preference.md", 540, head="---\ntitle: t\n---\n")
    if archive_fails:  # publication succeeds, archiving the source fails once
        real, calls = ingest._archive, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("archive failed")
            return real(*args, **kwargs)

        monkeypatch.setattr(ingest, "_archive", flaky)
    _ingest(ws)
    assert card.stat().st_size == 574  # 540 + 34: card 1 cannot take the block again
    if archive_fails:
        assert (ws / ".claude/memory-handoffs/2026-11-13-pref.md").exists()  # left in place for the replay
        ingest.run(target=ws, promote_cards=True, route_documents=True)
    else:
        _ingest(ws, name="2026-11-13-pref-copy.md")  # a second copy of the same handoff
    assert sorted((ws / CARDS_REL).glob("user-md--overflow-preference*.md")) == [card]
    assert card.read_text().count(NOTE) == 1


def test_replay_finds_the_block_in_an_earlier_card_after_a_rollover(tmp_path, monkeypatch):
    # The block went to card 1 when it had room; card 1 then grew. A replay must not open card 2.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    _cap_cards(ws)
    assert _ingest(ws) == 0
    card = ws / CARDS_REL / "user-md--overflow-preference.md"
    with card.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + "filler line. " * 40 + "\n")  # card 1 is now past max_card_bytes
    assert card.stat().st_size > 600
    _ingest(ws, name="2026-11-13-replay.md")
    assert [p.name for p in (ws / CARDS_REL).iterdir()] == [card.name]
    assert card.read_text().count(NOTE) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_user_md_routes_to_card_without_writing_through(tmp_path, monkeypatch):  # Review Focus 3
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    shared = sized(tmp_path / "shared" / "USER.md", 100)
    (ws / "USER.md").unlink()
    (ws / "USER.md").symlink_to(shared)
    outcome = _decide(ws, _sections("USER.md", NOTE))
    assert (outcome.kind, outcome.overflow.reason) == ("routed-card", "symlinked")
    assert _ingest(ws) == 0
    assert shared.stat().st_size == 100
    assert (ws / "USER.md").is_symlink()
    card = ws / CARDS_REL / "user-md--overflow-preference.md"
    assert card.is_file() and "source_file: USER.md\n" in card.read_text()  # the card names the workspace path


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_user_md_with_an_invalid_config_still_routes_to_the_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    shared = sized(tmp_path / "shared" / "USER.md", 100)
    (ws / "USER.md").unlink()
    (ws / "USER.md").symlink_to(shared)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    outcome = _decide(ws, _sections("USER.md", NOTE))
    assert outcome.kind == "routed-card"
    assert _ingest(ws) == 0
    assert shared.stat().st_size == 100


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_dangling_link_and_linked_card_folder_stay_unsafe(tmp_path, monkeypatch):  # Review Focus 3
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (ws / "memory" / "cards").mkdir(parents=True)
    (ws / CARDS_REL).symlink_to(elsewhere, target_is_directory=True)
    linked = _decide(ws, _sections("USER.md", NOTE))  # USER.md 8241, past act-at
    (ws / "USER.md").unlink()
    (ws / "USER.md").symlink_to(tmp_path / "missing.md")
    dangling = _decide(ws, _sections("USER.md", NOTE))
    assert {linked.code, dangling.code} == {"document-destination-unsafe"}
    assert not any(elsewhere.iterdir())
    assert _ingest(ws) == 0
    assert not any(elsewhere.iterdir())


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_symlinked_card_file_is_never_written_through(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    outside = sized(tmp_path / "outside.md", 50)
    (ws / CARDS_REL).mkdir(parents=True)
    (ws / CARDS_REL / "user-md--overflow-preference.md").symlink_to(outside)
    assert _decide(ws, _sections("USER.md", NOTE)).code == "document-destination-unsafe"
    assert _ingest(ws) == 0
    assert outside.stat().st_size == 50


def test_invalid_instructions_table_routes_every_known_file(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "USER.md", 100)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nact_at_percent = "x"\n')
    assert route(ws, "USER.md", "note", "setup", home=home, environ=env).reason == "config-invalid"


def test_invalid_instructions_table_never_loses_a_handoff(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 100)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nenabled = "yes"\n')
    assert _ingest(ws) == 0
    inbox = ws / "memory" / "handoff-inbox"
    assert not inbox.exists() or not list(inbox.glob("*.md"))
    assert (ws / CARDS_REL / "user-md--overflow-preference.md").read_text().endswith("\n" + NOTE + "\n")
    assert (ws / "USER.md").stat().st_size == 100


def test_unparseable_care_configuration_keeps_the_handoff_for_a_person(tmp_path, monkeypatch):
    # Protected paths cannot be read from it, so nothing is written: the handoff is inboxed, not lost.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text("[instructions\nnot toml")
    assert _ingest(ws) == 0
    assert not (ws / CARDS_REL).exists() and (ws / "USER.md").stat().st_size == 8241
    assert list((ws / "memory" / "handoff-inbox").glob("*.md"))


def test_disabled_stage_and_exclude_keep_routing_on(tmp_path):  # decision 14
    ws, home, env = field_workspace(tmp_path)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions]\nenabled = false\nexclude = ["USER.md"]\n')
    assert route(ws, "USER.md", "note", "preference", home=home, environ=env).reason == "over-act-at"


def test_a_requested_larger_limit_does_not_loosen_routing(tmp_path):
    # A configured limit can only lower a limit: USER.md keeps its 4,000 loader cap.
    ws, home, env = field_workspace(tmp_path)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('[instructions.files."USER.md"]\nlimit_bytes = 90000\n')
    assert route(ws, "USER.md", "note", "preference", home=home, environ=env).reason == "over-act-at"


def test_a_rejected_override_is_not_a_way_to_a_direct_append(tmp_path):
    # An absolute path that discovery did not find is rejected: its requested limit applies nothing.
    ws, home, env = field_workspace(tmp_path)
    notes = sized(ws / ".learnings" / "LEARNINGS.md", 19_990)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text(
        f'[instructions.files."{notes.as_posix()}"]\nlimit_bytes = 999999\nclass = "append"\n'
    )
    routed = route(ws, ".learnings/LEARNINGS.md", "x" * 4000, "workflow", home=home, environ=env)
    assert routed is not None and routed.reason == "over-act-at"
    assert routed.file.override_reason == "absolute-path-not-discovered" and routed.file.file_class == "report"


def test_a_document_outside_the_inventory_is_not_routed(tmp_path):
    ws, home, env = field_workspace(tmp_path)
    assert route(ws, "rules/notes.md", "x" * 90_000, "workflow", home=home, environ=env) is None
    assert route(ws, "USER.md", "note", "workflow", home=home, environ=env).reason == "over-act-at"  # control


def test_routing_reads_the_given_home_not_the_real_one(tmp_path):
    # With no OpenClaw configuration the 4,000 cap is unknown, so a small USER.md stays a direct append.
    ws = tmp_path / "ws"
    sized(ws / "USER.md", 100)
    home = tmp_path / "home"
    home.mkdir()
    assert route(ws, "USER.md", NOTE, "setup", home=home, environ={}) is None


def test_lint_and_draft_accept_an_append_past_act_at(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "USER.md", 3900)
    assert not linting.lint_text(handoff_text("USER.md", "y" * 200), workspace=ws).errors


def test_overflow_cards_skip_orphan_and_undersourced_checks(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    sized(ws / "memory/cards/plain.md", 60, head="---\ntitle: plain\n---\n")
    sized(ws / "memory/cards/other.md", 60, head="---\ntitle: other\n---\n")
    (ws / "MEMORY.md").write_text("- [other](memory/cards/other.md)\n")
    assert _ingest(ws) == 0
    issues = memory_cmd._scan_payload(ws, memory_cmd._config_or_default(ws))["issues"]
    assert not [
        i
        for i in issues
        if i["file"].startswith(CARDS_REL + "/") and i["issue_type"] in ("orphaned-card", "undersourced")
    ]
    assert any(i["file"] == "memory/cards/plain.md" and i["issue_type"] == "orphaned-card" for i in issues)


def test_routing_starts_no_subprocess(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    refuse_subprocess(monkeypatch)
    assert _decide(ws, _sections("USER.md", NOTE)).kind == "routed-card"


def test_discovery_runs_once_per_ingest_run_not_once_per_handoff(tmp_path, monkeypatch):
    from brigade import instruction_overflow

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    calls = []
    real = instruction_overflow.discover

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(instruction_overflow, "discover", counting)
    for number in range(3):
        write_handoff(ws, f"2026-11-13-{number}.md", handoff_text("USER.md", f"Note number {number}.", type_="setup"))
    assert ingest.run(target=ws, promote_cards=True, route_documents=True) == 0
    assert len(calls) == 1
    card = (ws / CARDS_REL / "user-md--overflow-setup.md").read_text()
    assert all(f"Note number {number}." in card for number in range(3))


def test_two_files_that_collide_on_a_key_never_share_an_overflow_card(tmp_path):
    # PR 1 gives colliding rows a `-<hex>` key suffix; the card name is built from that key.
    ws, home, env = field_workspace(tmp_path)
    sized(ws / "a" / "b.md", 100)
    sized(ws / "a-b.md", 100)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text(
        '[instructions.files."a/b.md"]\nclass = "append"\n[instructions.files."a-b.md"]\nclass = "append"\n'
    )
    first = route(ws, "a/b.md", "x" * 20_000, "setup", home=home, environ=env)
    second = route(ws, "a-b.md", "x" * 20_000, "setup", home=home, environ=env)
    assert first is not None and second is not None
    assert first.file.key != second.file.key and first.card_rel != second.card_rel
    assert first.card_rel == f"{CARDS_REL}/{first.file.key}--overflow-setup.md"


def test_ingest_decide_on_a_missing_user_md_with_a_big_note_routes_to_a_card(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / "USER.md").unlink()
    outcome = _decide(ws, _sections("USER.md", "z" * 5000))
    assert (outcome.kind, outcome.code) == ("routed-card", "document-routed-to-card")


def test_journal_decision_undoes_the_card_append_and_keeps_later_ones(tmp_path, monkeypatch):
    from brigade import memory_care_tend

    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    assert _ingest(ws, name="2026-11-13-a.md", content="First note.") == 0
    assert _ingest(ws, name="2026-11-13-b.md", content="Second note.") == 0
    decisions = [d for d in journal.entries(ws) if d.reason == "ingest:document-routed-to-card"]
    by_text = {
        "first"
        if b"First note." in base64.b64decode(journal._payload(ws, d.decision_id)["appends"][0]["data"])
        else "second": d
        for d in decisions
    }
    first, second = by_text["first"], by_text["second"]
    card = ws / CARDS_REL / "user-md--overflow-preference.md"
    memory_care_tend.undo(ws, first.decision_id)
    text = card.read_text()
    assert "First note." not in text and text.endswith("\n\nSecond note.\n") and text.startswith("---\n")
    memory_care_tend.undo(ws, second.decision_id)
    # The creating append was undone first, so its frontmatter stays for later appends: a bare header remains.
    assert card.read_text().startswith("---\n") and "note." not in card.read_text()


def test_read_only_card_path_keeps_the_handoff_for_a_person(tmp_path, monkeypatch):
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    (ws / ".brigade").mkdir()
    (ws / ".brigade" / "memory-care.toml").write_text('read_only_paths = ["memory/cards/instructions/**"]\n')
    assert _ingest(ws) == 0
    assert not (ws / CARDS_REL).exists()
    assert list((ws / "memory" / "handoff-inbox").glob("*.md"))


def test_document_lock_is_taken_before_the_journal(tmp_path, monkeypatch):
    # Retry and revert take the document lock and then the journal; the direct path must match.
    ws, home, env = field_workspace(tmp_path)
    set_home(monkeypatch, home)
    order: list[str] = []
    real_lock, real_serialized = ingest._document_lock, journal.commit_phased

    class Spy:
        def __init__(self, target):
            self.inner = real_lock(target)

        def __enter__(self):
            order.append("document-lock")
            return self.inner.__enter__()

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    def commit(*args, **kwargs):
        order.append("journal")
        return real_serialized(*args, **kwargs)

    monkeypatch.setattr(ingest, "_document_lock", lambda target: Spy(target))
    monkeypatch.setattr(journal, "commit_phased", commit)
    assert _ingest(ws) == 0
    assert order.index("document-lock") < order.index("journal")
