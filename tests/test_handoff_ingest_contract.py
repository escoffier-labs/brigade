"""`handoff lint` and `handoff draft` enforce the same rules the ingester enforces.

One test group per conflict class seen in ingest receipts: lint must catch it in a
hand-written file, and `draft` must refuse it (or fill it, for the expected SHA)
without writing a file.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import signal
import json
from pathlib import Path

import pytest

from brigade import budgets, handoff_cmd, ingest as ingest_mod
from brigade.handoff_cmd import drafts, linting
from brigade.handoff_ingest_rules import (
    card_frontmatter_problems,
    safe_card_name,
    safe_document_target,
)

CARD = """---
topic: contract-card
category: foundation
tags: [memory, handoff]
---

# Contract card

Body of the card.
"""

UPDATED = CARD.replace("Body of the card.", "Body of the card, revised.")


def _workspace(tmp_path: Path, *, card: str | None = "contract-card.md") -> Path:
    cards = tmp_path / "memory" / "cards"
    cards.mkdir(parents=True)
    if card:
        (cards / card).write_bytes(CARD.encode("utf-8"))
    return tmp_path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _handoff_text(
    action: str, *, card: str = "contract-card.md", content: str = UPDATED, sha: str | None = None
) -> str:
    parts = [
        "# Memory Handoff",
        "## Type\n\nworkflow",
        "## Title\n\nContract",
        "## Summary\n\nChecks the contract.",
        f"## Recommended memory action\n\n{action}",
        f"## Target card\n\n{card}",
    ]
    if sha is not None:
        parts.append(f"## Expected card SHA256\n\n{sha}")
    parts.append(f"## Suggested card content\n\n{content}")
    return "\n\n".join(parts) + "\n"


def _doc_handoff_text(document: str, content: str = "A durable note.") -> str:
    return (
        "# Memory Handoff\n\n## Type\n\nworkflow\n\n## Title\n\nDoc\n\n## Summary\n\nDoc summary.\n\n"
        f"## Recommended memory action\n\nno-card\n\n## Target document\n\n{document}\n\n"
        f"## Suggested document content\n\n{content}\n"
    )


def _draft(tmp_path: Path, **kwargs):
    kwargs.setdefault("title", "Contract draft")
    kwargs.setdefault("summary", "Checks the contract.")
    kwargs.setdefault("inbox", str(tmp_path / "inbox"))
    return drafts.build_draft(target=tmp_path, **kwargs)


def _no_files(tmp_path: Path) -> bool:
    inbox = tmp_path / "inbox"
    return not inbox.exists() or not any(inbox.iterdir())


# --- update-card without the expected SHA -------------------------------------


def test_lint_rejects_update_card_without_expected_sha(tmp_path: Path):
    ws = _workspace(tmp_path)
    result = linting.lint_text(_handoff_text("update-card"), workspace=ws)
    assert not result.valid
    assert any("Expected card SHA256" in error for error in result.errors)


def test_lint_rejects_malformed_expected_sha(tmp_path: Path):
    ws = _workspace(tmp_path)
    result = linting.lint_text(_handoff_text("update-card", sha="abc123"), workspace=ws)
    assert any("64 lowercase hex" in error for error in result.errors)


def test_lint_rejects_stale_expected_sha(tmp_path: Path):
    ws = _workspace(tmp_path)
    result = linting.lint_text(_handoff_text("update-card", sha="0" * 64), workspace=ws)
    assert any("does not match the current card" in error for error in result.errors)


def test_lint_accepts_matching_expected_sha(tmp_path: Path):
    ws = _workspace(tmp_path)
    result = linting.lint_text(_handoff_text("update-card", sha=_sha(CARD)), workspace=ws)
    assert result.valid, result.errors


def test_draft_fills_expected_sha_from_the_live_card(tmp_path: Path):
    ws = _workspace(tmp_path)
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    assert payload["valid"] is True
    assert payload["expected_sha"] == _sha(CARD)
    text = Path(payload["path"]).read_text(encoding="utf-8")
    assert f"## Expected card SHA256\n\n{_sha(CARD)}\n" in text
    assert ws == tmp_path


def test_draft_update_card_for_a_missing_card_says_what_to_do(tmp_path: Path):
    _workspace(tmp_path, card=None)
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    assert "does not exist" in refused.value.problems[0]
    assert "create-card" in refused.value.problems[0]
    assert _no_files(tmp_path)


def test_draft_names_the_flags_to_pass_when_the_workspace_is_unreachable(tmp_path: Path):
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    [problem] = refused.value.problems
    assert "--workspace" in problem and "--expected-sha" in problem
    assert _no_files(tmp_path)


def test_draft_accepts_explicit_sha_when_the_workspace_is_unreachable(tmp_path: Path):
    payload = _draft(
        tmp_path,
        action="update-card",
        target_card="contract-card.md",
        content=UPDATED,
        expected_sha=_sha(CARD).upper(),
    )
    assert payload["valid"] is True
    assert payload["expected_sha"] == _sha(CARD)
    assert any("not checked against the live card" in w for w in payload["lint"]["warnings"])


def test_draft_refuses_an_explicit_sha_that_is_stale(tmp_path: Path):
    _workspace(tmp_path)
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED, expected_sha="1" * 64)
    assert "does not match" in refused.value.problems[0]
    assert _no_files(tmp_path)


def test_draft_reads_the_card_from_a_separate_workspace(tmp_path: Path):
    ws = _workspace(tmp_path / "ws")
    target = tmp_path / "repo"
    target.mkdir()
    payload = drafts.build_draft(
        target=target,
        title="Other workspace",
        summary="Reads cards elsewhere.",
        action="update-card",
        target_card="contract-card.md",
        content=UPDATED,
        inbox=str(target / "inbox"),
        workspace=ws,
    )
    assert payload["expected_sha"] == _sha(CARD)


def test_create_card_that_already_exists_is_a_conflict(tmp_path: Path):
    ws = _workspace(tmp_path)
    result = linting.lint_text(_handoff_text("create-card"), workspace=ws)
    assert any("already exists" in error for error in result.errors)
    with pytest.raises(drafts.HandoffDraftRefused):
        _draft(tmp_path, action="create-card", target_card="contract-card.md", content=CARD)


# --- bootstrap size guard -----------------------------------------------------


def _fill_user_md(ws: Path) -> None:
    (ws / "USER.md").write_text("x" * (budgets.BOOTSTRAP_BUDGETS["USER.md"] - 10), encoding="utf-8")


def test_lint_rejects_an_append_over_the_bootstrap_budget(tmp_path: Path):
    _fill_user_md(tmp_path)
    result = linting.lint_text(_doc_handoff_text("USER.md", "y" * 200), workspace=tmp_path)
    assert any("bootstrap budget guard" in error for error in result.errors)


def test_draft_refuses_an_append_over_the_bootstrap_budget(tmp_path: Path):
    _fill_user_md(tmp_path)
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="no-card", target_document="USER.md", content="y" * 200)
    assert "bootstrap budget guard" in refused.value.problems[0]
    assert _no_files(tmp_path)


def test_draft_allows_a_small_append_to_a_bootstrap_document(tmp_path: Path):
    (tmp_path / "USER.md").write_text("short\n", encoding="utf-8")
    assert _draft(tmp_path, action="no-card", target_document="USER.md", content="A small note.")["valid"] is True


# --- unsafe target document ---------------------------------------------------


@pytest.mark.parametrize(
    "document", [".learnings/notes.md", "rules/sub/deep.md", "../outside.md", "/etc/passwd", "AGENTS.md"]
)
def test_lint_rejects_unsafe_target_documents(document: str):
    result = linting.lint_text(_doc_handoff_text(document))
    assert any("unsafe target document" in error for error in result.errors)


def test_draft_refuses_unsafe_target_documents(tmp_path: Path):
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="no-card", target_document=".learnings/notes.md", content="A note.")
    assert "unsafe target document" in refused.value.problems[0]
    assert _no_files(tmp_path)


@pytest.mark.parametrize("document", ["rules/team.md", ".learnings/LEARNINGS.md", "USER.md"])
def test_safe_target_documents_pass_lint_and_ingest_alike(document: str):
    assert safe_document_target(document)
    assert linting.lint_text(_doc_handoff_text(document)).valid


# --- missing category and frontmatter ----------------------------------------


NO_CATEGORY = CARD.replace("category: foundation\n", "")
NO_FRONTMATTER = "# Contract card\n\nBody without frontmatter.\n"
NO_TAGS = CARD.replace("tags: [memory, handoff]\n", "")


def test_lint_rejects_a_card_without_category():
    result = linting.lint_text(_handoff_text("create-card", content=NO_CATEGORY))
    assert any("`category`" in error for error in result.errors)


def test_draft_refuses_a_card_without_category(tmp_path: Path):
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="create-card", target_card="new-card.md", content=NO_CATEGORY)
    assert refused.value.problems == [
        "card frontmatter needs a nonempty `category` (for example `category: foundation`)"
    ]
    assert _no_files(tmp_path)


def test_lint_rejects_a_card_without_frontmatter():
    result = linting.lint_text(_handoff_text("create-card", content=NO_FRONTMATTER))
    assert "Suggested card content must start with YAML frontmatter" in result.errors


def test_draft_refuses_a_card_without_frontmatter(tmp_path: Path):
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="create-card", target_card="new-card.md", content=NO_FRONTMATTER)
    assert "frontmatter" in refused.value.problems[0]
    assert _no_files(tmp_path)


def test_card_frontmatter_policy_covers_tags_title_closing_delimiter_and_dates():
    assert card_frontmatter_problems(NO_TAGS) == [
        "card frontmatter needs `tags` (for example `tags: [memory, handoff]`)"
    ]
    assert card_frontmatter_problems(CARD.replace("topic: contract-card\n", "")) == [
        "card frontmatter needs a nonempty `title` or `topic`"
    ]
    assert card_frontmatter_problems("---\ntopic: x\ncategory: y\ntags: [a]\n") == [
        "card content missing YAML frontmatter (it must open and close with `---`)"
    ]
    assert card_frontmatter_problems(CARD.replace("tags:", "updated: yesterday\ntags:")) == [
        "card frontmatter `updated` must be an ISO date like 2026-11-08"
    ]
    assert card_frontmatter_problems(CARD.replace("tags: [memory, handoff]", "tags:\n  - memory\n  - handoff")) == []
    assert card_frontmatter_problems(CARD.replace("\n\n# Contract card\n\nBody of the card.\n", "\n")) == [
        "card body is empty after the frontmatter"
    ]


# --- unsafe card name ---------------------------------------------------------


@pytest.mark.parametrize("name", ["../escape.md", "sub/card.md", ".hidden.md", "card.txt", "card", "a b.md"])
def test_lint_rejects_unsafe_card_names(name: str):
    result = linting.lint_text(_handoff_text("create-card", card=name))
    assert any(error.startswith("Target card must be a filename") for error in result.errors)
    assert not safe_card_name(name)


def test_draft_refuses_unsafe_card_names(tmp_path: Path):
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="create-card", target_card="../escape.md", content=CARD)
    assert refused.value.problems[0].startswith("Target card must be a filename")
    assert _no_files(tmp_path)


# --- required sections --------------------------------------------------------


def test_lint_rejects_a_note_with_no_recognizable_sections():
    result = linting.lint_text("Just some notes about what happened today.\n")
    assert not result.valid
    assert any(error.startswith("missing required section:") for error in result.errors)


# --- ingest agrees with lint --------------------------------------------------


def test_a_valid_update_card_draft_is_promoted_by_ingest(tmp_path: Path):
    ws = _workspace(tmp_path)
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    sections = ingest_mod.parse(Path(payload["path"]))
    assert "expected card sha256" in sections
    outcome = ingest_mod.decide(sections, ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "promoted"


def test_ingest_inboxes_an_update_card_whose_expected_sha_is_stale(tmp_path: Path):
    ws = _workspace(tmp_path)
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    (ws / "memory" / "cards" / "contract-card.md").write_bytes((CARD + "\nEdited after the draft.\n").encode("utf-8"))
    outcome = ingest_mod.decide(ingest_mod.parse(Path(payload["path"])), ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "inboxed"
    assert "does not match the current card" in outcome.reason


def test_ingest_still_promotes_update_card_without_the_optional_sha_section(tmp_path: Path):
    ws = _workspace(tmp_path)
    sections = {
        "type": "workflow",
        "title": "Legacy",
        "summary": "No SHA section.",
        "recommended memory action": "update-card",
        "target card": "contract-card.md",
        "suggested card content": UPDATED,
    }
    assert ingest_mod.decide(sections, ws, promote_cards=True, route_documents=True).kind == "promoted"


# --- negative checks: valid input is never refused ----------------------------


def test_valid_drafts_for_every_action_are_written_and_lint_clean(tmp_path: Path):
    _workspace(tmp_path)
    fresh = CARD.replace("topic: contract-card", "topic: fresh-card")
    created = _draft(tmp_path, action="create-card", target_card="fresh-card.md", content=fresh, draft_id="a")
    updated = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED, draft_id="b")
    documented = _draft(tmp_path, action="no-card", target_document="rules/team.md", content="Do it.", draft_id="c")
    for payload in (created, updated, documented):
        assert payload["valid"] is True
        assert payload["lint"]["errors"] == []
        assert Path(payload["path"]).is_file()
    assert handoff_cmd.lint(target=tmp_path, paths=[Path(created["path"])], json_output=True) == 0


# --- CLI ----------------------------------------------------------------------


def test_draft_cli_prints_one_line_per_problem_and_writes_nothing(tmp_path: Path, capsys):
    rc = handoff_cmd.draft(
        target=tmp_path,
        title="Bad card",
        summary="Missing everything.",
        action="create-card",
        target_card="../bad.md",
        content=NO_CATEGORY,
        inbox=str(tmp_path / "inbox"),
    )
    assert rc == 2
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("error: draft refused, nothing written: ") for line in lines)
    assert _no_files(tmp_path)


def test_draft_cli_json_reports_the_refusal(tmp_path: Path, capsys):
    rc = handoff_cmd.draft(
        target=tmp_path,
        title="Bad doc",
        summary="Unsafe document.",
        action="no-card",
        target_document="AGENTS.md",
        content="Note.",
        inbox=str(tmp_path / "inbox"),
        json_output=True,
    )
    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["valid"] is False and payload["written"] is False
    assert "unsafe target document" in payload["errors"][0]


def test_draft_cli_fills_the_sha_and_lint_cli_accepts_the_result(tmp_path: Path, capsys):
    ws = _workspace(tmp_path)
    rc = handoff_cmd.draft(
        target=tmp_path,
        title="Update",
        summary="Updates the card.",
        action="update-card",
        target_card="contract-card.md",
        content=UPDATED,
        inbox=str(tmp_path / "inbox"),
        json_output=True,
    )
    assert rc == 0
    path = Path(json.loads(capsys.readouterr().out)["path"])
    assert handoff_cmd.lint(target=tmp_path, paths=[path], workspace=ws, json_output=True) == 0


# --- review round 1: publish race, block lists, symlinked documents, echo ------


def _execute(ws: Path, handoff: Path, outcome):
    inbox = handoff.parent
    return ingest_mod._execute(
        outcome,
        handoff,
        ws,
        ingest_mod.parse(handoff),
        ws / "memory" / "handoff-inbox",
        inbox / "processed",
        False,
    )


def _update_handoff(tmp_path: Path) -> tuple[Path, Path]:
    ws = _workspace(tmp_path)
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    return ws, Path(payload["path"])


def test_stale_card_between_decide_and_publish_is_routed_to_review(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "promoted"
    card = ws / "memory" / "cards" / "contract-card.md"
    edited = CARD + "\nA newer edit made after decide().\n"
    card.write_bytes(edited.encode("utf-8"))
    action = _execute(ws, handoff, outcome)
    assert action.kind == "inboxed"
    assert "[expected-sha-stale]" in action.summary
    assert card.read_text(encoding="utf-8") == edited
    assert list((ws / "memory" / "handoff-inbox").glob("*.md"))


def test_card_deleted_between_decide_and_publish_is_routed_to_review(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    (ws / "memory" / "cards" / "contract-card.md").unlink()
    action = _execute(ws, handoff, outcome)
    assert action.kind == "inboxed"
    assert not (ws / "memory" / "cards" / "contract-card.md").exists()


def test_unchanged_card_is_published_under_the_expected_sha(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert _execute(ws, handoff, outcome).kind == "promoted"
    assert "revised" in (ws / "memory" / "cards" / "contract-card.md").read_text(encoding="utf-8")


UNINDENTED_LIST = CARD.replace("tags: [memory, handoff]", "tags:\n- memory\n- handoff")


def test_unindented_block_list_is_accepted_by_lint_draft_and_ingest(tmp_path: Path):
    assert card_frontmatter_problems(UNINDENTED_LIST) == []
    assert linting.lint_text(_handoff_text("create-card", content=UNINDENTED_LIST)).valid
    payload = _draft(tmp_path, action="create-card", target_card="listed.md", content=UNINDENTED_LIST)
    assert payload["valid"] is True
    sections = ingest_mod.parse(Path(payload["path"]))
    assert ingest_mod.decide(sections, tmp_path, promote_cards=True, route_documents=True).kind == "promoted"


def _escape_setup(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return ws, outside


def test_lint_and_draft_reject_a_symlinked_document_destination(tmp_path: Path):
    ws, outside = _escape_setup(tmp_path)
    (outside / "target.md").write_text("outside\n", encoding="utf-8")
    (ws / "USER.md").symlink_to(outside / "target.md")
    result = linting.lint_text(_doc_handoff_text("USER.md"), workspace=ws)
    assert any("contains a symlink" in error for error in result.errors)
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        drafts.build_draft(
            target=ws,
            title="T",
            summary="S.",
            action="no-card",
            target_document="USER.md",
            content="x",
            inbox=str(ws / "in"),
        )
    assert "contains a symlink" in refused.value.problems[0]


def test_lint_rejects_a_symlinked_ancestor_and_a_dangling_link(tmp_path: Path):
    ws, outside = _escape_setup(tmp_path)
    (ws / "rules").symlink_to(outside, target_is_directory=True)
    assert any("symlink" in e for e in linting.lint_text(_doc_handoff_text("rules/x.md"), workspace=ws).errors)
    (ws / ".learnings").mkdir()
    (ws / ".learnings" / "LEARNINGS.md").symlink_to(outside / "missing.md")
    assert any(
        "symlink" in e for e in linting.lint_text(_doc_handoff_text(".learnings/LEARNINGS.md"), workspace=ws).errors
    )


def test_ingest_decide_sends_a_symlinked_document_to_review(tmp_path: Path):
    ws, outside = _escape_setup(tmp_path)
    (outside / "target.md").write_text("outside\n", encoding="utf-8")
    (ws / "USER.md").symlink_to(outside / "target.md")
    sections = {
        "type": "workflow",
        "title": "T",
        "summary": "S.",
        "recommended memory action": "no-card",
        "target document": "USER.md",
        "suggested document content": "A note.",
    }
    outcome = ingest_mod.decide(sections, ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "inboxed" and "symlink" in outcome.reason
    assert (outside / "target.md").read_text(encoding="utf-8") == "outside\n"


def test_document_swapped_for_a_symlink_after_decide_is_not_written_through(tmp_path: Path):
    ws, outside = _escape_setup(tmp_path)
    (ws / "USER.md").write_text("mine\n", encoding="utf-8")
    (outside / "target.md").write_text("outside\n", encoding="utf-8")
    payload = drafts.build_draft(
        target=ws,
        title="T",
        summary="S.",
        action="no-card",
        target_document="USER.md",
        content="A note.",
        inbox=str(ws / "in"),
    )
    handoff = Path(payload["path"])
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "routed"
    (ws / "USER.md").unlink()
    (ws / "USER.md").symlink_to(outside / "target.md")
    action = _execute(ws, handoff, outcome)
    assert action.kind == "inboxed"
    assert (outside / "target.md").read_text(encoding="utf-8") == "outside\n"


def test_document_append_creates_missing_directories_and_appends(tmp_path: Path):
    ws, _ = _escape_setup(tmp_path)
    payload = drafts.build_draft(
        target=ws,
        title="T",
        summary="S.",
        action="no-card",
        target_document="rules/team.md",
        content="Do it.",
        inbox=str(ws / "in"),
    )
    handoff = Path(payload["path"])
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert _execute(ws, handoff, outcome).kind == "routed"
    assert (ws / "rules" / "team.md").read_text(encoding="utf-8") == "\n\nDo it.\n"


# --- review round 2: raw bytes, fail-closed append, content-free refusals, comments --


def test_a_crlf_card_gets_one_sha_in_draft_lint_and_ingest(tmp_path: Path):
    card = tmp_path / "memory" / "cards" / "contract-card.md"
    card.parent.mkdir(parents=True)
    raw = CARD.replace("\n", "\r\n").encode("utf-8")
    card.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    assert payload["expected_sha"] == digest
    assert linting.lint_text(_handoff_text("update-card", sha=digest), workspace=tmp_path).valid
    handoff = Path(payload["path"])
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), tmp_path, promote_cards=True, route_documents=True)
    assert outcome.kind == "promoted"
    assert _execute(tmp_path, handoff, outcome).kind == "promoted"
    assert b"revised" in card.read_bytes()


def test_a_bytes_only_change_at_the_publish_boundary_is_routed_to_review(tmp_path: Path, monkeypatch):
    # Two different invalid bytes decode to the same replacement character, so a text
    # comparison cannot see the swap. Only an exact byte comparison can.
    card = tmp_path / "memory" / "cards" / "contract-card.md"
    card.parent.mkdir(parents=True)
    card.write_bytes(CARD.encode("utf-8") + b"\xff\n")
    payload = _draft(tmp_path, action="update-card", target_card="contract-card.md", content=UPDATED)
    handoff = Path(payload["path"])
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), tmp_path, promote_cards=True, route_documents=True)
    real = ingest_mod.dirfd.open_child_file

    def swap_after_staging(parent, name, flags, *args, **kwargs):
        descriptor = real(parent, name, flags, *args, **kwargs)
        if name.startswith(".contract-card.md."):  # the staged copy was just created
            card.write_bytes(CARD.encode("utf-8") + b"\xfe\n")
        return descriptor

    monkeypatch.setattr(ingest_mod.dirfd, "open_child_file", swap_after_staging)
    action = _execute(tmp_path, handoff, outcome)
    assert action.kind == "inboxed"
    assert card.read_bytes() == CARD.encode("utf-8") + b"\xfe\n"


def test_document_append_without_a_nofollow_backend_goes_to_review_and_writes_nothing(tmp_path: Path, monkeypatch):
    ws, _ = _escape_setup(tmp_path)
    (ws / "USER.md").write_bytes(b"mine\n")
    handoffs = []
    for document in ("USER.md", "rules/new.md"):
        payload = drafts.build_draft(
            target=ws,
            title="T",
            summary="S.",
            action="no-card",
            target_document=document,
            content="A note.",
            inbox=str(ws / "in"),
            draft_id=document.replace("/", "-"),
        )
        handoffs.append(Path(payload["path"]))
    outcomes = [ingest_mod.decide(ingest_mod.parse(h), ws, promote_cards=True, route_documents=True) for h in handoffs]
    assert [o.kind for o in outcomes] == ["routed", "routed"]
    monkeypatch.setattr(ingest_mod.dirfd, "available", lambda: False)
    for handoff, outcome in zip(handoffs, outcomes, strict=True):
        assert _execute(ws, handoff, outcome).kind == "inboxed"
    assert (ws / "USER.md").read_bytes() == b"mine\n"
    assert not (ws / "rules").exists()


def test_refusal_reasons_never_contain_handoff_text(tmp_path: Path):
    secret = "SECRET-TOKEN-9d41"
    ws = _workspace(tmp_path)
    (ws / "USER.md").write_bytes(f"{secret}-anchor line\n".encode())
    base = {"type": "workflow", "title": "T", "summary": "S."}
    cases = [
        {
            **base,
            "recommended memory action": "no-card",
            "target document": "USER.md",
            "suggested document content": f"{secret}-anchor line\nplus a second line",
        },
        {
            **base,
            "recommended memory action": "no-card",
            "target document": f"{secret}.md",
            "suggested document content": "x",
        },
        {**base, "recommended memory action": f"{secret}-action"},
        {
            **base,
            "recommended memory action": "create-card",
            "target card": f"{secret}/x.md",
            "suggested card content": CARD,
        },
        {
            **base,
            "recommended memory action": "no-card",
            "target document": "USER.md",
            "suggested document content": "x",
            f"{secret} heading": "body",
        },
    ]
    for sections in cases:
        outcome = ingest_mod.decide(sections, ws, promote_cards=True, route_documents=True)
        assert outcome.kind in {"inboxed", "skipped"}, sections
        assert secret not in outcome.reason, outcome.reason
    anchor = ingest_mod.decide(cases[0], ws, promote_cards=True, route_documents=True)
    assert anchor.reason == "document anchor already present in the target document"


def test_lint_unsafe_document_message_does_not_echo_the_target():
    result = linting.lint_text(_doc_handoff_text("SECRET-TOKEN-9d41.md"))
    assert any(e.startswith("unsafe target document: use one of") for e in result.errors)
    assert not any("SECRET-TOKEN" in e for e in result.errors)


def _agree(tmp_path: Path, content: str, *, accepted: bool) -> None:
    """Lint, draft and ingest decide give the same verdict on one card."""
    lint_ok = linting.lint_text(_handoff_text("create-card", content=content)).valid
    sections = {
        "type": "workflow",
        "title": "T",
        "summary": "S.",
        "recommended memory action": "create-card",
        "target card": "agree.md",
        "suggested card content": content,
    }
    decide_ok = ingest_mod.decide(sections, tmp_path, promote_cards=True, route_documents=True).kind == "promoted"
    try:
        _draft(tmp_path, action="create-card", target_card="agree.md", content=content, draft_id="agree")
        draft_ok = True
    except drafts.HandoffDraftRefused:
        draft_ok = False
    assert (lint_ok, draft_ok, decide_ok) == (accepted, accepted, accepted)


MULTILINE_DESCRIPTION = CARD.replace(
    "category: foundation\n",
    'category: foundation\ndescription: "first line of the description\n  second line of the description"\n',
)
MULTILINE_FLOW_LIST = CARD.replace("tags: [memory, handoff]", "tags: [\n  memory,\n  handoff,\n]")
ISO_TIMESTAMPS = CARD.replace(
    "tags:", "last_reviewed: 2026-11-08T12:00:00Z\nfresh_until: 2026-12-01T00:00:00+00:00\ntags:"
)


def test_multiline_quoted_values_are_accepted_by_lint_draft_and_ingest(tmp_path: Path):
    _agree(tmp_path, MULTILINE_DESCRIPTION, accepted=True)


def test_multiline_flow_lists_are_accepted_by_lint_draft_and_ingest(tmp_path: Path):
    _agree(tmp_path, MULTILINE_FLOW_LIST, accepted=True)


def test_iso_timestamps_and_date_only_values_are_accepted(tmp_path: Path):
    assert card_frontmatter_problems(ISO_TIMESTAMPS) == []
    dated = CARD.replace("tags:", "last_reviewed: 2026-11-08\nfresh_until: 2026-12-01\ntags:")
    assert card_frontmatter_problems(dated) == []
    _agree(tmp_path, ISO_TIMESTAMPS, accepted=True)


def test_trailing_comments_and_hash_in_values_are_accepted(tmp_path: Path):
    commented = CARD.replace("tags: [memory, handoff]", "tags: [memory, handoff] # indexing tags").replace(
        "category: foundation", "category: foundation # grouping"
    )
    assert card_frontmatter_problems(commented) == []
    assert card_frontmatter_problems(CARD.replace("category: foundation", 'category: "a # b"')) == []
    _agree(tmp_path, commented, accepted=True)


def test_a_card_the_shared_parser_rejects_is_rejected_by_all_three(tmp_path: Path):
    secret = "SECRET-TOKEN-9d41"
    for content in (
        f"# {secret}\n\nNo frontmatter at all.\n",
        CARD.replace("tags: [memory, handoff]\n---", f"tags: [memory, handoff]\n{secret}: never closed"),
    ):
        _agree(tmp_path, content, accepted=False)
    reasons = card_frontmatter_problems(f"# {secret}\n\nbody\n")
    assert reasons and not any(secret in r for r in reasons)
    sections = {
        "type": "workflow",
        "title": "T",
        "summary": "S.",
        "recommended memory action": "create-card",
        "target card": "agree.md",
        "suggested card content": f"# {secret}\n\nbody\n",
    }
    outcome = ingest_mod.decide(sections, tmp_path, promote_cards=True, route_documents=True)
    assert outcome.kind == "inboxed" and secret not in outcome.reason
    result = linting.lint_text(_handoff_text("create-card", content=f"# {secret}\n\nbody\n"))
    assert not any(secret in e for e in result.errors + result.warnings)


# --- review round 3: held parent, content-free categories ----------------------


def test_card_publication_without_a_nofollow_backend_goes_to_review_and_writes_nothing(tmp_path: Path, monkeypatch):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    card = ws / "memory" / "cards" / "contract-card.md"
    before = card.read_bytes()
    monkeypatch.setattr(ingest_mod.dirfd, "available", lambda: False)
    assert _execute(ws, handoff, outcome).kind == "inboxed"
    assert card.read_bytes() == before
    assert not list(card.parent.glob(".contract-card.md.*"))


@pytest.mark.skipif(os.name != "posix", reason="needs POSIX symlinks and stable inodes")
def test_a_parent_swapped_after_it_is_held_is_never_written_through(tmp_path: Path, monkeypatch):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    cards = ws / "memory" / "cards"
    moved = ws / "memory" / "cards-moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    real = ingest_mod.dirfd.open_child_file

    def swap_after_staging(parent, name, flags, *args, **kwargs):
        descriptor = real(parent, name, flags, *args, **kwargs)
        if name.startswith(".contract-card.md."):
            cards.rename(moved)  # the held descriptor still points at the moved directory
            cards.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(ingest_mod.dirfd, "open_child_file", swap_after_staging)
    action = _execute(ws, handoff, outcome)
    assert action.kind == "inboxed"
    assert not list(outside.iterdir())
    assert b"revised" not in (moved / "contract-card.md").read_bytes()
    assert not list(moved.glob(".contract-card.md.*"))


def test_card_publication_leaves_no_staging_file_and_keeps_the_mode(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    card = ws / "memory" / "cards" / "contract-card.md"
    if os.name == "posix":
        card.chmod(0o640)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert _execute(ws, handoff, outcome).kind == "promoted"
    assert not list(card.parent.glob(".contract-card.md.*"))
    if os.name == "posix":
        assert card.stat().st_mode & 0o777 == 0o640


def test_refusal_reasons_leave_out_fence_languages_card_names_and_targets(tmp_path: Path):
    secret = "SECRET-TOKEN-9d41"
    ws = _workspace(tmp_path, card=f"{secret}.md")
    (ws / "rules").mkdir()
    (ws / "rules" / f"{secret}.md").write_bytes(f"{secret}-anchor line\n".encode())
    (ws / "USER.md").write_bytes(b"x" * (budgets.BOOTSTRAP_BUDGETS["USER.md"] - 10))
    base = {"type": "workflow", "title": "T", "summary": "S."}
    cases = {
        "fence": {
            **base,
            "recommended memory action": "create-card",
            "target card": "fence.md",
            "suggested card content": f"```{secret}\n{CARD}\n```",
        },
        "sha": {
            **base,
            "recommended memory action": "update-card",
            "target card": f"{secret}.md",
            "expected card sha256": "0" * 64,
            "suggested card content": CARD,
        },
        "duplicate": {
            **base,
            "recommended memory action": "no-card",
            "target document": f"rules/{secret}.md",
            "suggested document content": f"{secret}-anchor line",
        },
        "anchor": {
            **base,
            "recommended memory action": "no-card",
            "target document": f"rules/{secret}.md",
            "suggested document content": f"{secret}-anchor line\nand more",
        },
        "budget": {
            **base,
            "recommended memory action": "no-card",
            "target document": "USER.md",
            "suggested document content": "y" * 200,
        },
    }
    for name, sections in cases.items():
        outcome = ingest_mod.decide(sections, ws, promote_cards=True, route_documents=True)
        assert outcome.kind == "inboxed", name
        assert secret not in outcome.reason, (name, outcome.reason)
    assert ingest_mod.decide(cases["sha"], ws, promote_cards=True, route_documents=True).reason == (
        "update-card Expected card SHA256 does not match the current card"
    )
    assert ingest_mod.decide(cases["anchor"], ws, promote_cards=True, route_documents=True).reason == (
        "document anchor already present in the target document"
    )
    # Lint and draft messages are content-free too.
    stale = linting.lint_text(_handoff_text("update-card", card=f"{secret}.md", sha="0" * 64), workspace=ws)
    assert stale.errors and not any(secret in e for e in stale.errors)
    exists = linting.lint_text(_handoff_text("create-card", card=f"{secret}.md"), workspace=ws)
    assert exists.errors and not any(secret in e for e in exists.errors)
    over = linting.lint_text(_doc_handoff_text("USER.md", "y" * 200), workspace=ws)
    assert any("bootstrap budget guard" in e for e in over.errors) and not any("USER.md" in e for e in over.errors)
    with pytest.raises(drafts.HandoffDraftRefused) as refused:
        _draft(tmp_path, action="update-card", target_card=f"{secret}-missing.md", content=UPDATED)
    assert secret not in " ".join(refused.value.problems)


# --- review round 4: parent identity on every platform --------------------------


def test_a_parent_whose_identity_changed_is_routed_to_review_without_posix_gating(tmp_path: Path, monkeypatch):
    # A Windows swap cannot run on Linux CI, so mock the directory identity: the first
    # call is the held descriptor, the second is the reopened path, and they differ.
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    card = ws / "memory" / "cards" / "contract-card.md"
    before = card.read_bytes()
    calls = []

    def identity(descriptor):
        calls.append(descriptor)
        return (1, len(calls))

    monkeypatch.setattr(ingest_mod, "_directory_identity", identity)
    action = _execute(ws, handoff, outcome)
    assert len(calls) == 2
    assert action.kind == "inboxed"
    assert card.read_bytes() == before
    assert not list(card.parent.glob(".contract-card.md.*"))


def test_a_stable_parent_identity_still_publishes(tmp_path: Path, monkeypatch):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    monkeypatch.setattr(ingest_mod, "_directory_identity", lambda descriptor: (1, 1))
    assert _execute(ws, handoff, outcome).kind == "promoted"


# --- review round 5: a FIFO where a card or document should be ------------------

posix_only = pytest.mark.skipif(
    not (hasattr(os, "mkfifo") and hasattr(signal, "SIGALRM")), reason="needs POSIX FIFOs and SIGALRM"
)


@contextlib.contextmanager
def _no_hang(seconds: int = 10):
    """Fail the test instead of hanging it if a blocking open never returns."""

    def expired(signum, frame):
        raise TimeoutError("open blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@posix_only
def test_a_card_swapped_for_a_fifo_is_routed_to_review_without_hanging(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    card = ws / "memory" / "cards" / "contract-card.md"
    card.unlink()
    os.mkfifo(card)
    with _no_hang():
        action = _execute(ws, handoff, outcome)
    assert action.kind == "inboxed"
    assert card.is_fifo()
    assert not list(card.parent.glob(".contract-card.md.*"))


@posix_only
def test_a_new_card_over_a_fifo_is_routed_to_review_without_hanging(tmp_path: Path):
    ws = _workspace(tmp_path, card=None)
    payload = _draft(tmp_path, action="create-card", target_card="fresh-card.md", content=CARD)
    handoff = Path(payload["path"])
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    assert outcome.kind == "promoted"
    os.mkfifo(ws / "memory" / "cards" / "fresh-card.md")
    with _no_hang():
        assert _execute(ws, handoff, outcome).kind == "inboxed"


@posix_only
def test_shared_card_reader_refuses_a_fifo_without_blocking(tmp_path: Path):
    from brigade.card_fingerprint import read_text_nofollow

    fifo = tmp_path / "card.md"
    os.mkfifo(fifo)
    with _no_hang(), pytest.raises(OSError, match="not a regular file"):
        read_text_nofollow(fifo)


@posix_only
def test_a_document_append_over_a_fifo_fails_without_blocking(tmp_path: Path, monkeypatch):
    ws, _ = _escape_setup(tmp_path)
    (ws / "rules").mkdir()
    payload = drafts.build_draft(
        target=ws,
        title="T",
        summary="S.",
        action="no-card",
        target_document="rules/team.md",
        content="Do it.",
        inbox=str(ws / "in"),
    )
    handoff = Path(payload["path"])
    os.mkfifo(ws / "rules" / "team.md")  # swapped in after the draft passed lint
    # The path check catches the FIFO first. Disable it to prove the open itself cannot hang.
    monkeypatch.setattr(ingest_mod, "document_destination_problem", lambda *args, **kwargs: None)
    outcome = ingest_mod.Outcome("routed", dest=ws / "rules" / "team.md")
    with _no_hang():
        assert _execute(ws, handoff, outcome).kind == "inboxed"


@posix_only
def test_regular_cards_and_documents_still_read_and_write_after_the_nonblocking_open(tmp_path: Path):
    ws, handoff = _update_handoff(tmp_path)
    outcome = ingest_mod.decide(ingest_mod.parse(handoff), ws, promote_cards=True, route_documents=True)
    with _no_hang():
        assert _execute(ws, handoff, outcome).kind == "promoted"
    card = ws / "memory" / "cards" / "contract-card.md"
    assert b"revised" in card.read_bytes()
    from brigade.card_fingerprint import read_text_nofollow

    assert "revised" in read_text_nofollow(card)
