"""Preference replacements use the existing exact-edit review boundary."""

from datetime import date
from pathlib import Path

import pytest

from brigade import cli, ingest, memory_proposals as proposals
from brigade.handoff_cmd.drafts import build_draft
from tests.test_memory_proposal_capabilities import SAFE_DIRECTORY_CAPABILITY

pytestmark = pytest.mark.skipif(not SAFE_DIRECTORY_CAPABILITY, reason="safe directory descriptors unavailable")
OLD = "- Prefer short replies."
NEW = "- Prefer detailed replies."


def draft(target: Path, *, replacement=True):
    (target / "USER.md").write_text(f"# Preferences\n\n{OLD}\n- Use plain words.\n")
    kwargs = {"superseded_preference": OLD} if replacement else {}
    result = build_draft(
        target=target,
        title="Reply length",
        summary="The preference changed.",
        handoff_type="preference",
        target_document="USER.md",
        content=NEW,
        **kwargs,
    )
    return Path(result["path"])


def create(target, path):
    return proposals.create_payload(
        target=target,
        relation="replace-preference",
        handoff_path=path.relative_to(target).as_posix(),
        reason="User changed the preference.",
    )


def accept(target, proposal):
    return proposals.review_payload(
        target=target, proposal_id=proposal["id"], digest=proposal["digest"], reason="Reviewed both lines."
    )


def apply(target, proposal):
    return proposals.apply_payload(target=target, proposal_id=proposal["id"], digest=proposal["digest"])


def test_replace_requires_review_and_preserves_history(tmp_path):
    path = draft(tmp_path)
    before = (tmp_path / "USER.md").read_bytes()
    assert ingest.decide(ingest.parse(path), tmp_path, True, True).code == "preference-replacement-review"
    proposal = create(tmp_path, path)
    assert (tmp_path / "USER.md").read_bytes() == before
    assert proposal["preference_diff"] == {"old": OLD, "new": NEW}
    with pytest.raises(proposals.ProposalError, match="not been accepted"):
        apply(tmp_path, proposal)
    accept(tmp_path, proposal)
    assert (tmp_path / "USER.md").read_bytes() == before
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert (tmp_path / "USER.md").read_text() == before.decode().replace(OLD, NEW)
    assert OLD in (tmp_path / proposal["history_path"]).read_text()
    assert apply(tmp_path, proposal)["status"] == "already-applied"


@pytest.mark.parametrize(
    ("old", "new", "duplicate"),
    [
        ("  - Prefer short replies.", "  - Prefer detailed replies.", ""),
        ("- Prefer short replies.  ", "- Prefer detailed replies.  ", ""),
        ("  - Prefer short replies.", "  - Prefer detailed replies.", "- Prefer short replies.\n"),
    ],
    ids=["indented", "trailing-spaces", "unindented-duplicate"],
)
@pytest.mark.parametrize("from_file", [False, True], ids=["content", "content-file"])
def test_replace_preserves_exact_preference_whitespace(tmp_path, old, new, duplicate, from_file):
    before = f"# Preferences\n\n{old}\n{duplicate}- Use plain words.\n"
    (tmp_path / "USER.md").write_text(before)
    content_args = {"content": new}
    if from_file:
        content_file = tmp_path / "replacement.txt"
        content_file.write_text("\n \n" + new + "\n\t\n")
        content_args = {"content_file": content_file}
    result = build_draft(
        target=tmp_path,
        title="Reply length",
        summary="The preference changed.",
        handoff_type="preference",
        target_document="USER.md",
        superseded_preference=old,
        **content_args,
    )
    path = Path(result["path"])
    assert ingest.parse(path)["superseded preference"] == old
    assert ingest.parse(path)["suggested document content"] == new
    proposal = create(tmp_path, path)
    assert proposal["preference_diff"] == {"old": old, "new": new}
    accept(tmp_path, proposal)
    assert apply(tmp_path, proposal)["status"] == "committed"
    assert (tmp_path / "USER.md").read_text() == f"# Preferences\n\n{new}\n{duplicate}- Use plain words.\n"
    assert (tmp_path / proposal["history_path"]).read_text().endswith(old + "\n")


@pytest.mark.parametrize("heading_line", [OLD, NEW], ids=["superseded", "suggested"])
def test_padded_heading_refuses_proposal(tmp_path, heading_line):
    path = draft(tmp_path)
    path.write_text(path.read_text().replace(heading_line, "  # Heading  "))
    with pytest.raises(proposals.ProposalError, match="non-heading"):
        create(tmp_path, path)


def test_parse_removes_only_preference_section_blank_lines():
    sections = ingest.parse(
        Path("handoff.md"),
        (
            "## Suggested document content\n\n \n  - New.  \n\t\n"
            "## Superseded preference\n\n\t\n  - Old.  \n \n"
            "## Summary\n\n  Changed.  \n"
        ).encode(),
    )
    assert sections["suggested document content"] == "  - New.  "
    assert sections["superseded preference"] == "  - Old.  "
    assert sections["summary"] == "Changed."


def test_reject_changes_no_preference(tmp_path):
    path = draft(tmp_path)
    proposal = create(tmp_path, path)
    before = (tmp_path / "USER.md").read_bytes()
    proposals.reject_payload(
        target=tmp_path, proposal_id=proposal["id"], digest=proposal["digest"], reason="Keep the old preference."
    )
    with pytest.raises(proposals.ProposalError, match="rejected"):
        apply(tmp_path, proposal)
    assert (tmp_path / "USER.md").read_bytes() == before
    assert not (tmp_path / proposal["history_path"]).exists()


def test_missing_target_refuses_creation_and_stale_apply(tmp_path):
    path = draft(tmp_path)
    proposal = create(tmp_path, path)
    accept(tmp_path, proposal)
    (tmp_path / "USER.md").write_text("# Preferences\n")
    with pytest.raises(proposals.ProposalError, match="target line no longer exists"):
        create(tmp_path, path)
    with pytest.raises(proposals.ProposalError, match="target line no longer exists"):
        apply(tmp_path, proposal)
    assert (tmp_path / "USER.md").read_text() == "# Preferences\n"


def test_plain_append_unchanged(tmp_path):
    path = draft(tmp_path, replacement=False)
    outcome = ingest.decide(ingest.parse(path), tmp_path, True, True)
    assert outcome.kind == "routed"
    assert outcome.dest == tmp_path / "USER.md"


def test_cli_proposal_from_handoff(tmp_path, capsys):
    path = draft(tmp_path)
    assert (
        cli.main(
            [
                "memory",
                "proposal",
                "create",
                "--target",
                str(tmp_path),
                "--relation",
                "replace-preference",
                "--handoff",
                path.relative_to(tmp_path).as_posix(),
                "--reason",
                "Changed preference.",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"preference_diff"' in output
    assert f"| {OLD} | {NEW} |" in output


def test_ingest_holds_replacement_and_drain_cannot_apply(tmp_path):
    from brigade.memory_inbox_drain import drain

    path = draft(tmp_path)
    legacy = tmp_path / ".claude/memory-handoffs"
    legacy.mkdir(parents=True)
    path.rename(legacy / path.name)
    before = (tmp_path / "USER.md").read_bytes()
    stats = ingest.IngestStats()
    assert (
        ingest.ingest_into(source=tmp_path, owner=tmp_path, stats=stats, promote_cards=True, route_documents=True) == 0
    )
    assert stats.inboxed == 1
    drain(tmp_path, today=date.today(), deadline_days=30)
    assert (tmp_path / "USER.md").read_bytes() == before
    held = list((tmp_path / "memory/handoff-inbox").glob("*.md"))
    assert len(held) == 1
    proposal = create(tmp_path, held[0])
    assert proposal["preference_diff"] == {"old": OLD, "new": NEW}


def test_other_document_changes_invalidate_acceptance(tmp_path):
    path = draft(tmp_path)
    proposal = create(tmp_path, path)
    accept(tmp_path, proposal)
    current = (tmp_path / "USER.md").read_text() + "- Use examples.\n"
    (tmp_path / "USER.md").write_text(current)
    with pytest.raises(proposals.ProposalError, match="source or owner configuration changed"):
        apply(tmp_path, proposal)
    assert (tmp_path / "USER.md").read_text() == current


def test_duplicate_target_refuses(tmp_path):
    path = draft(tmp_path)
    with (tmp_path / "USER.md").open("a") as handle:
        handle.write(OLD + "\n")
    with pytest.raises(proposals.ProposalError, match="ambiguous"):
        create(tmp_path, path)


def test_symlink_destination_refuses(tmp_path):
    path = draft(tmp_path)
    destination = tmp_path / "USER.md"
    moved = tmp_path / "original.md"
    destination.rename(moved)
    destination.symlink_to(moved.name)
    with pytest.raises(proposals.ProposalError):
        create(tmp_path, path)
    assert NEW not in moved.read_text()


def test_history_write_failure_restores_preference(tmp_path, monkeypatch):
    from brigade.projection import kernel

    path = draft(tmp_path)
    proposal = create(tmp_path, path)
    accept(tmp_path, proposal)
    before = (tmp_path / "USER.md").read_bytes()
    original = kernel.execute
    monkeypatch.setattr(
        kernel,
        "execute",
        lambda plan, **kw: original(
            plan, **kw, inject=kernel.FailureInjector(boundary="commit:1:before", error=OSError("history write denied"))
        ),
    )
    assert apply(tmp_path, proposal)["status"] == "restored"
    assert (tmp_path / "USER.md").read_bytes() == before
    assert not (tmp_path / proposal["history_path"]).exists()
    monkeypatch.setattr(kernel, "execute", original)
    assert apply(tmp_path, proposal)["status"] == "committed"


def test_cli_draft_writes_optional_section(tmp_path, capsys):
    assert (
        cli.main(
            [
                "handoff",
                "draft",
                "--target",
                str(tmp_path),
                "--type",
                "preference",
                "--title",
                "Reply length",
                "--summary",
                "Preference changed.",
                "--target-document",
                "USER.md",
                "--superseded-preference",
                OLD,
                "--content",
                NEW,
            ]
        )
        == 0
    )
    capsys.readouterr()
    path = next((tmp_path / ".codex/memory-handoffs").glob("*.md"))
    assert ingest.parse(path)["superseded preference"] == OLD
