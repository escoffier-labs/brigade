"""Memory Handoff delivery across worktrees and per-item ingest failures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from brigade import handoff_worktrees, ingest as ingest_mod, runguard
from brigade.claude_hooks import runtime as hook_runtime
from brigade.install import install_selection
from brigade.selection import Selection

_HANDOFF = """\
# Memory Handoff

## Recommended memory action
no-card

## Summary
{summary}
"""

needs_posix_perms = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions enforced for a non-root user",
)
needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="creates POSIX symlinks")


@pytest.fixture(autouse=True)
def _no_quiescence_wait(monkeypatch):
    """Fresh test files count as quiet unless a test sets the window."""
    monkeypatch.setattr(handoff_worktrees, "QUIESCENCE_SECONDS", 0.0, raising=False)


def _age(path: Path, seconds: float = 60.0) -> None:
    past = path.stat().st_mtime - seconds
    os.utime(path, (past, past))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _seed(target: Path) -> Path:
    install_selection(target, Selection(depth="workspace", harnesses=["claude"], owner="claude", includes=[]))
    return target / ".claude" / "memory-handoffs"


def _handoff(inbox: Path, name: str, summary: str) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(_HANDOFF.format(summary=summary))
    return path


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    _seed(main)
    _git(main, "init", "-q")
    _git(main, "commit", "-q", "--allow-empty", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    return main, linked


def _plain_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _on_first_fsync(monkeypatch, action) -> None:
    """Run `action` the first time rescue fsyncs its copy (after the claim)."""
    real = os.fsync
    state = {"done": False}

    def fsync(fd):
        if not state["done"]:
            state["done"] = True
            action()
        return real(fd)

    monkeypatch.setattr(handoff_worktrees.os, "fsync", fsync)


def _claims(inbox: Path) -> list[Path]:
    return list(inbox.glob(".brigade-claim-*"))


# A. One bad item is recorded and skipped; the rest of the batch still runs.


@needs_posix_perms
def test_unreadable_handoff_is_recorded_and_the_rest_ingest(tmp_target: Path, capsys):
    inbox = _seed(tmp_target)
    bad = _handoff(inbox, "2026-11-08-0900-unreadable.md", "bad")
    good = _handoff(inbox, "2026-11-08-0901-good.md", "good")
    bad.chmod(0)
    try:
        rc = ingest_mod.run(tmp_target)
    finally:
        bad.chmod(0o644)

    out = capsys.readouterr().out
    assert rc == 0
    assert not good.exists()
    assert (inbox / "processed" / good.name).is_file()
    assert bad.is_file()
    assert "Failed 1" in out
    assert "[io-error]" in out
    assert "PARTIAL" in out
    assert "Status partial" in out


@needs_posix_perms
def test_read_only_inbox_fails_its_items_without_aborting_other_inboxes(tmp_path: Path):
    main = tmp_path / "ws"
    _seed(main)
    other = tmp_path / "other"
    locked_inbox = other / ".claude" / "memory-handoffs"
    locked = _handoff(locked_inbox, "2026-11-08-0900-locked.md", "locked")
    good = _handoff(main / ".claude" / "memory-handoffs", "2026-11-08-0901-good.md", "good")
    (main / ".brigade" / "handoff-sources.json").write_text(
        '{"sources": [{"root": "%s", "inboxes": [".claude/memory-handoffs"]}]}' % other
    )
    locked_inbox.chmod(0o555)
    try:
        stats = ingest_mod.IngestStats()
        rc = ingest_mod.ingest_into(source=main, owner=main, stats=stats)
    finally:
        locked_inbox.chmod(0o755)

    assert rc == 0
    assert stats.failed == 1
    assert stats.status == "partial"
    assert locked.is_file()
    assert not good.exists()
    assert any(line.startswith("fail") and locked.name in line for line in stats.actions)


def test_clean_run_reports_ok(tmp_target: Path, capsys):
    inbox = _seed(tmp_target)
    _handoff(inbox, "2026-11-08-0901-good.md", "good")
    assert ingest_mod.run(tmp_target) == 0
    out = capsys.readouterr().out
    assert "Status ok" in out
    assert "PARTIAL" not in out


# Daybreak 3: malformed text is a per-item failure; guard failures stay fatal.


def test_invalid_utf8_is_a_per_item_failure(tmp_target: Path, capsys):
    inbox = _seed(tmp_target)
    bad = inbox / "2026-11-08-0900-bad-bytes.md"
    bad.write_bytes(b"# Memory Handoff\n\n## Summary\n\xff\xfe broken\n")
    good = _handoff(inbox, "2026-11-08-0901-good.md", "good")

    assert ingest_mod.run(tmp_target) == 0
    out = capsys.readouterr().out
    assert bad.is_file()
    assert not good.exists()
    assert "[invalid-encoding]" in out
    assert "Status partial" in out


def test_parse_value_error_is_a_per_item_failure(tmp_target: Path, monkeypatch, capsys):
    inbox = _seed(tmp_target)
    bad = _handoff(inbox, "2026-11-08-0900-bad.md", "bad")
    good = _handoff(inbox, "2026-11-08-0901-good.md", "good")
    real_parse = ingest_mod.parse

    def parse(path):
        if path.name == bad.name:
            raise ValueError("malformed fence")
        return real_parse(path)

    monkeypatch.setattr(ingest_mod, "parse", parse)
    assert ingest_mod.run(tmp_target) == 0
    out = capsys.readouterr().out
    assert bad.is_file()
    assert not good.exists()
    assert "[invalid-content]" in out
    assert "malformed fence" not in out


def test_guard_failure_in_decide_stays_fatal(tmp_target: Path, monkeypatch):
    inbox = _seed(tmp_target)
    _handoff(inbox, "2026-11-08-0900-a.md", "a")

    def decide(*args, **kwargs):
        raise RuntimeError("guard invariant broken")

    monkeypatch.setattr(ingest_mod, "decide", decide)
    with pytest.raises(RuntimeError):
        ingest_mod.run(tmp_target)


# Daybreak 4: logs and review receipts carry codes, never handoff fields.


def test_untrusted_fields_stay_out_of_logs_and_receipts(tmp_target: Path, capsys):
    inbox = _seed(tmp_target)
    name = "2026-11-08-0900-evil\x1b[31m.md"
    (inbox / name).write_text(
        "# Memory Handoff\n\n## Recommended memory action\ncreate-card\n\n"
        "## Target card\nSENTINEL-TARGET\x07/../x.md\n\n"
        "## Suggested card content\n---\ntopic: t\n---\nSENTINEL-CONTENT\n"
    )

    assert ingest_mod.run(tmp_target, promote_cards=True, route_documents=True) == 0
    out = capsys.readouterr().out
    assert "[unsafe-card-name]" in out
    assert "SENTINEL-TARGET" not in out
    assert "SENTINEL-CONTENT" not in out
    assert "\x1b" not in out and "\x07" not in out
    assert "\\x1b" in out
    reviews = list((tmp_target / "memory" / "handoff-inbox").glob("*.md"))
    assert len(reviews) == 1
    header = reviews[0].read_text().split("-->", 1)[0]
    assert "reason: unsafe-card-name" in header
    assert "SENTINEL-TARGET" not in header
    assert "\x1b" not in header
    assert "\x1b" not in reviews[0].name


# Codex 4507466523: review drafts never overwrite each other.


def _freeze_ingest_clock(monkeypatch) -> None:
    from datetime import datetime as real_datetime, timezone

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(ingest_mod, "datetime", Frozen)


@pytest.mark.skipif(os.name == "nt", reason="'?' is not a valid Windows filename character")
def test_sanitized_name_collision_keeps_both_drafts(tmp_target: Path, monkeypatch):
    inbox = _seed(tmp_target)
    _handoff(inbox, "a!.md", "first")
    _handoff(inbox, "a?.md", "second")
    _freeze_ingest_clock(monkeypatch)

    assert ingest_mod.run(tmp_target) == 0
    drafts = sorted((tmp_target / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    bodies = [d.read_text() for d in drafts]
    assert any("first" in b for b in bodies) and any("second" in b for b in bodies)


def test_truncated_long_name_collision_keeps_both_drafts(tmp_target: Path, monkeypatch):
    inbox = _seed(tmp_target)
    prefix = "2026-11-08-0900-" + "x" * 150
    _handoff(inbox, prefix + "-one.md", "first")
    _handoff(inbox, prefix + "-two.md", "second")
    _freeze_ingest_clock(monkeypatch)

    assert ingest_mod.run(tmp_target) == 0
    drafts = sorted((tmp_target / "memory" / "handoff-inbox").glob("*.md"))
    assert len(drafts) == 2
    bodies = [d.read_text() for d in drafts]
    assert any("first" in b for b in bodies) and any("second" in b for b in bodies)


# B. Linked worktree inboxes are found through git and deduped by content.


def test_ingest_discovers_linked_worktree_inbox(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    stranded = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-stranded.md", "stranded")

    paths = ingest_mod._resolve_inbox_paths(main)
    assert (linked / ".claude" / "memory-handoffs").resolve() in paths

    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=main, owner=main, stats=stats) == 0
    assert stats.processed == 1
    assert not stranded.exists()
    assert (stranded.parent / "processed" / stranded.name).is_file()


def test_same_handoff_in_two_worktrees_ingests_once(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    _handoff(main / ".claude" / "memory-handoffs", "2026-11-08-0900-same.md", "same")
    copy = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-same.md", "same")

    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=main, owner=main, stats=stats) == 0
    assert stats.processed == 1
    assert stats.skipped == 1
    assert any("[duplicate-content]" in line for line in stats.actions)
    assert (copy.parent / "processed" / copy.name).is_file()


# Codex 4507705655: a retryable skip is not handled content.

_CARD = """\
# Memory Handoff

## Type
decision

## Title
Lock busy

## Recommended memory action
create-card

## Target card
lock-busy.md

## Suggested card content
---
topic: lock-busy
category: test
tags: [test]
---

# Lock busy

Retry fact.
"""


def test_retryable_skip_does_not_mark_identical_handoffs_handled(tmp_path: Path):
    from brigade import inbox_lock

    main, linked = _repo_with_worktree(tmp_path)
    in_main = main / ".claude" / "memory-handoffs" / "2026-11-08-0900-card.md"
    in_main.write_text(_CARD)
    in_linked = linked / ".claude" / "memory-handoffs" / "2026-11-08-0900-card-copy.md"
    in_linked.parent.mkdir(parents=True)
    in_linked.write_text(_CARD)
    lock = main / ".brigade/memory/proposals/proposal.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    with inbox_lock.held_file_lock(lock, deadline_seconds=5):
        stats = ingest_mod.IngestStats()
        assert ingest_mod.ingest_into(source=main, owner=main, stats=stats, promote_cards=True) == 0

    card = main / "memory" / "cards" / "lock-busy.md"
    assert not card.exists()
    assert in_main.is_file() and in_linked.is_file()
    assert not list((main / ".claude" / "memory-handoffs" / "processed").glob("*.md"))
    assert not list((linked / ".claude" / "memory-handoffs" / "processed").glob("*.md"))
    # The pending main copy is content ingest will read, so the linked copy
    # may count as its duplicate. Either way the bytes stay recoverable.
    rescued = handoff_worktrees.rescue_pending_handoffs(linked, main)
    assert [r.state for r in rescued] in (["delivered"], ["duplicate"])
    assert in_main.is_file()

    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=main, owner=main, stats=stats, promote_cards=True) == 0
    assert stats.promoted == 1
    assert "Retry fact." in card.read_text()


# Codex 4507705659: a subdirectory root keeps its subdirectory in siblings.


def test_subdirectory_root_maps_into_linked_checkouts(tmp_path: Path):
    main = tmp_path / "main"
    project = main / "project"
    project.mkdir(parents=True)
    (project / "README.md").write_text("project\n")
    _git(main, "init", "-q")
    _git(main, "add", "project/README.md")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    _seed(project)
    intended = _handoff(linked / "project" / ".claude" / "memory-handoffs", "2026-11-08-0900-intended.md", "intended")
    unrelated = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-unrelated.md", "unrelated")

    paths = ingest_mod._resolve_inbox_paths(project)
    assert intended.parent.resolve() in paths
    assert unrelated.parent.resolve() not in paths

    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=project, owner=project, stats=stats) == 0
    assert not intended.exists()
    assert unrelated.is_file()
    assert handoff_worktrees.main_checkout_for(linked / "project", repo=main) == project.resolve()
    assert handoff_worktrees.main_checkout_for(linked / "project", repo=main) != main.resolve()


@needs_symlinks
def test_symlinked_prefix_component_in_main_checkout_is_refused(tmp_path: Path):
    main = tmp_path / "main"
    (main / "a" / "b").mkdir(parents=True)
    (main / "a" / "b" / "README.md").write_text("b\n")
    _git(main, "init", "-q")
    _git(main, "add", "a/b/README.md")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    outside = tmp_path / "outside"
    (main / "a").rename(outside)  # outside/b is a real directory
    (main / "a").symlink_to(outside, target_is_directory=True)
    session = linked / "a" / "b"
    pending = _handoff(session / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")

    assert handoff_worktrees.main_checkout_for(session, repo=main) is None
    hint = handoff_worktrees.stop_hint(session)
    assert "could not be verified" in hint and str(outside) not in hint
    assert handoff_worktrees.handoff_inbox(session) == session / ".claude" / "memory-handoffs"

    runguard.remove_worktree(main, session, force=True)

    assert pending.is_file()
    assert not (outside / "b" / ".claude").exists()


# Codex 4507748094: rescue covers project-subdirectory inboxes.


def _repo_with_project(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    (main / "project").mkdir(parents=True)
    (main / "project" / "README.md").write_text("project\n")
    _git(main, "init", "-q")
    _git(main, "add", "project/README.md")
    _git(main, "commit", "-q", "-m", "init")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    return main, linked


def test_project_subdirectory_handoff_is_rescued_to_matching_main_directory(tmp_path: Path):
    main, linked = _repo_with_project(tmp_path)
    pending = _handoff(linked / "project" / ".claude" / "memory-handoffs", "pending.md", "project handoff")

    runguard.remove_worktree(main, linked, force=True)

    assert not linked.exists()
    delivered = main / "project" / ".claude" / "memory-handoffs" / "pending.md"
    assert delivered.read_text() == _HANDOFF.format(summary="project handoff")
    assert not (main / ".claude" / "memory-handoffs" / "pending.md").exists()
    assert not pending.exists()


def test_untracked_listing_over_budget_keeps_worktree(tmp_path: Path, monkeypatch, capsys):
    main, linked = _repo_with_project(tmp_path)
    pending = _handoff(linked / "project" / ".claude" / "memory-handoffs", "pending.md", "project handoff")
    monkeypatch.setattr(handoff_worktrees, "MAX_UNTRACKED_LIST_BYTES", 8)

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert pending.is_file()
    assert "inbox-scan-failed" in capsys.readouterr().err


def test_dot_prefixed_handoff_is_rescued_like_ingest_accepts_it(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    dotted = _handoff(inbox, ".pending.md", "dot-prefixed handoff")

    runguard.remove_worktree(main, linked, force=True)

    delivered = main / ".claude" / "memory-handoffs" / ".pending.md"
    assert delivered.read_text() == _HANDOFF.format(summary="dot-prefixed handoff")
    assert not dotted.exists()
    assert not linked.exists()
    handoffs, _ = ingest_mod._list_handoffs(delivered.parent)
    assert delivered in handoffs
    assert handoff_worktrees.is_handoff_filename(".pending.md")
    assert not handoff_worktrees.is_handoff_filename(".brigade-claim-x.md.claimed")
    assert not handoff_worktrees.is_handoff_filename(".late-oversize-x.md.recovered")


def test_hidden_ignored_and_deep_inboxes_are_never_deleted(tmp_path: Path, capsys):
    main, linked = _repo_with_project(tmp_path)
    deep = tuple(f"level{i}" for i in range(12))
    hidden = _handoff(linked / ".hidden" / "project" / ".claude" / "memory-handoffs", "hidden.md", "hidden")
    modules = _handoff(linked / "node_modules" / "x" / ".claude" / "memory-handoffs", "modules.md", "modules")
    deep_one = _handoff(linked.joinpath(*deep) / ".claude" / "memory-handoffs", "deep.md", "deep")
    # The main checkout has matching directories for two of the three.
    (main / ".hidden" / "project").mkdir(parents=True)
    main.joinpath(*deep).mkdir(parents=True)

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()  # node_modules/x has no matching main directory
    assert (main / ".hidden" / "project" / ".claude" / "memory-handoffs" / "hidden.md").is_file()
    assert (main.joinpath(*deep) / ".claude" / "memory-handoffs" / "deep.md").is_file()
    assert not hidden.exists() and not deep_one.exists()
    assert modules.is_file()
    assert "destination-unavailable" in capsys.readouterr().err


@needs_symlinks
def test_destination_prefix_swapped_after_validation_keeps_worktree(tmp_path: Path, monkeypatch, capsys):
    main, linked = _repo_with_project(tmp_path)
    pending = _handoff(linked / "project" / ".claude" / "memory-handoffs", "pending.md", "project handoff")
    outside = tmp_path / "outside"
    outside.mkdir()
    real = handoff_worktrees._publish_bytes

    def publish(*args, **kwargs):
        # After the destination was validated and opened, swap the prefix
        # for a link that points outside the repository.
        (main / "project").rename(main / "moved-project")
        (main / "project").symlink_to(outside, target_is_directory=True)
        return real(*args, **kwargs)

    monkeypatch.setattr(handoff_worktrees, "_publish_bytes", publish)
    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert pending.read_text() == _HANDOFF.format(summary="project handoff")
    assert list(outside.iterdir()) == []
    assert "destination-moved" in capsys.readouterr().err


@needs_symlinks
def test_project_handoff_with_unwalkable_destination_keeps_worktree(tmp_path: Path, capsys):
    main, linked = _repo_with_project(tmp_path)
    pending = _handoff(linked / "project" / ".claude" / "memory-handoffs", "pending.md", "project handoff")
    outside = tmp_path / "outside"
    (main / "project").rename(outside)
    (main / "project").symlink_to(outside, target_is_directory=True)

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert pending.is_file()
    assert not (outside / ".claude").exists()
    assert "destination-unavailable" in capsys.readouterr().err


# Daybreak 1: linked inboxes must stay inside the configured repository.


def test_worktree_metadata_pointing_at_another_repo_is_ignored(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    foreign = _plain_repo(tmp_path / "foreign")
    victim = _handoff(foreign / ".claude" / "memory-handoffs", "2026-11-08-0900-victim.md", "victim")
    admin = main / ".git" / "worktrees" / "evil"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(str(foreign / ".git") + "\n")
    (admin / "HEAD").write_text((main / ".git" / "HEAD").read_text())
    (admin / "commondir").write_text("../..\n")
    assert str(foreign) in _git(main, "worktree", "list", "--porcelain")

    found = handoff_worktrees.linked_worktrees(main)
    assert linked.resolve() in found
    assert foreign.resolve() not in found
    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=main, owner=main, stats=stats) == 0
    assert victim.is_file()


@needs_symlinks
def test_symlinked_inbox_component_is_refused(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    outside = tmp_path / "outside"
    victim = _handoff(outside / "memory-handoffs", "2026-11-08-0900-victim.md", "victim")
    (linked / ".claude").symlink_to(outside, target_is_directory=True)

    assert handoff_worktrees.contained_inbox(linked, ".claude/memory-handoffs") is None
    stats = ingest_mod.IngestStats()
    assert ingest_mod.ingest_into(source=main, owner=main, stats=stats) == 0
    assert victim.is_file()

    # Git lists the link itself and never descends through it, so rescue
    # touches nothing behind it, and removing the worktree removes the link
    # only.
    assert handoff_worktrees.rescue_pending_handoffs(linked, main) == []
    runguard.remove_worktree(main, linked, force=True)
    assert victim.is_file()
    assert not (main / ".claude" / "memory-handoffs" / victim.name).exists()


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/memory-handoffs",
        "C:/x/memory-handoffs",
        "//server/share",
        "a/../memory-handoffs",
        "./memory-handoffs",
        "a\\b",
        "\uff41/memory-handoffs",  # fullwidth "a", changes under NFKC
        "e\u0301/memory-handoffs",  # decomposed accent, changes under NFC
        "a\x1b/memory-handoffs",
        "",
    ],
)
def test_inbox_components_reject_unsafe_paths(rel: str):
    with pytest.raises(ValueError):
        handoff_worktrees.inbox_components(rel)


def test_inbox_components_accept_plain_relative_path():
    assert handoff_worktrees.inbox_components(".claude/memory-handoffs") == (".claude", "memory-handoffs")


def test_main_checkout_requires_the_configured_repo(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    other = _plain_repo(tmp_path / "other")
    assert handoff_worktrees.main_checkout(linked, repo=main) == main.resolve()
    assert handoff_worktrees.main_checkout(linked, repo=other) is None
    assert handoff_worktrees.main_checkout(main) is None


# Daybreak 5: `git worktree list` is read with byte and record bounds.


def test_worktree_list_over_byte_limit_fails_closed(tmp_path: Path, capsys):
    main, linked = _repo_with_worktree(tmp_path)
    assert handoff_worktrees.linked_worktrees(main, max_bytes=16) == []
    assert "exceeded" in capsys.readouterr().err
    assert handoff_worktrees.linked_worktrees(main) == [linked.resolve()]


def test_worktree_records_stop_at_the_limit():
    raw = b"".join(b"worktree /tmp/wt%d\0HEAD 0\0detached\0\0" % i for i in range(300))
    assert len(handoff_worktrees._worktree_records(raw, 256)) == 256


def test_worktree_discovery_is_bounded(tmp_path: Path):
    main = _plain_repo(tmp_path / "main")
    for index in range(3):
        _git(main, "worktree", "add", "-q", "--detach", str(tmp_path / f"wt{index}"))
    # The main checkout is the first record, so two records hold one linked tree.
    assert len(handoff_worktrees.linked_worktrees(main, limit=2)) == 1
    assert len(handoff_worktrees.linked_worktrees(main)) == 3
    assert handoff_worktrees.linked_worktrees(tmp_path / "not-a-repo") == []


# Worktree removal delivers pending handoffs first.


def test_remove_worktree_delivers_pending_handoffs_first(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    fresh = _handoff(inbox, "2026-11-08-0900-fresh.md", "fresh")
    _handoff(main / ".claude" / "memory-handoffs" / "processed", "older-name.md", "already ingested")
    _handoff(inbox, "2026-11-08-0901-dup.md", "already ingested")
    clash = _handoff(main / ".claude" / "memory-handoffs", "2026-11-08-0902-clash.md", "main copy")
    _handoff(inbox, "2026-11-08-0902-clash.md", "worktree copy")
    fresh_text = fresh.read_text()

    runguard.remove_worktree(main, linked, force=True)

    assert not linked.exists()
    dest = main / ".claude" / "memory-handoffs"
    assert (dest / fresh.name).read_text() == fresh_text
    assert not (dest / "2026-11-08-0901-dup.md").exists()
    assert clash.read_text() == _HANDOFF.format(summary="main copy")
    renamed = list(dest.glob("2026-11-08-0902-clash-*.md"))
    assert len(renamed) == 1
    assert renamed[0].read_text() == _HANDOFF.format(summary="worktree copy")
    assert not list(dest.glob(".brigade-rescue-*"))


@needs_posix_perms
def test_remove_worktree_keeps_worktree_when_delivery_fails(tmp_path: Path, capsys):
    main, linked = _repo_with_worktree(tmp_path)
    pending = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    dest = main / ".claude" / "memory-handoffs"
    dest.chmod(0o555)
    try:
        runguard.remove_worktree(main, linked, force=True)
    finally:
        dest.chmod(0o755)

    assert pending.is_file()
    assert "kept worktree" in capsys.readouterr().err


def test_remove_worktree_leaves_tracked_handoffs_to_git(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    tracked = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-tracked.md", "tracked")
    _git(linked, "add", "-f", "--", str(tracked.relative_to(linked)))
    _git(linked, "commit", "-q", "-m", "track")

    assert handoff_worktrees.rescue_pending_handoffs(linked, main) == []
    assert not (main / ".claude" / "memory-handoffs" / tracked.name).exists()


def test_rescue_without_verified_destination_keeps_the_handoff(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    pending = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    results = handoff_worktrees.rescue_pending_handoffs(linked, None)
    assert [r.state for r in results] == ["failed"]
    assert pending.is_file()


# Daybreak 2: the source is claimed atomically before it is copied.


def test_clean_rescue_leaves_no_claim_file(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-clean.md", "clean")
    text = source.read_text()

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["delivered"]
    assert not source.exists()
    assert _claims(inbox) == []
    assert (main / ".claude" / "memory-handoffs" / source.name).read_text() == text


def test_writer_returning_to_claimed_path_keeps_worktree(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-raced.md", "original")
    original = source.read_text()
    _on_first_fsync(monkeypatch, lambda: source.write_text("written after the claim"))

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert source.read_text() == "written after the claim"
    assert (main / ".claude" / "memory-handoffs" / source.name).read_text() == original
    assert _claims(inbox) == []


def test_file_swapped_in_just_before_unlink_survives(tmp_path: Path, monkeypatch):
    """The race Daybreak named: a new file lands at the original path right
    before the source unlink. It must survive as pending."""
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-raced.md", "original")
    original = source.read_text()
    real_unlink = handoff_worktrees.dirfd.unlink_child
    state = {"done": False}

    def unlink_child(parent, name, **kwargs):
        if not state["done"]:
            state["done"] = True
            if source.exists():
                source.unlink()
            source.write_text("swapped in before the unlink")
        return real_unlink(parent, name, **kwargs)

    monkeypatch.setattr(handoff_worktrees.dirfd, "unlink_child", unlink_child)
    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert source.read_text() == "swapped in before the unlink"
    assert (main / ".claude" / "memory-handoffs" / source.name).read_text() == original
    assert _claims(inbox) == []


def test_claimed_file_changed_after_copy_is_restored(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-raced.md", "original")

    def rewrite_claim():
        (claim,) = _claims(inbox)
        claim.write_text("changed after the copy")

    _on_first_fsync(monkeypatch, rewrite_claim)
    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [(r.state, r.reason) for r in results] == [("failed", "late-write-kept")]
    assert source.read_text() == "changed after the copy"
    assert _claims(inbox) == []


def test_claimed_file_changed_during_removal_keeps_worktree(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-raced.md", "original")

    def rewrite_claim():
        (claim,) = _claims(inbox)
        claim.write_text("changed after the copy")

    _on_first_fsync(monkeypatch, rewrite_claim)
    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert source.read_text() == "changed after the copy"


def test_stale_claim_keeps_worktree(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / ".brigade-claim-0123.md.claimed").write_text("interrupted rescue")

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert (inbox / ".brigade-claim-0123.md.claimed").is_file()


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX rename path")
def test_rename_noreplace_never_replaces(tmp_path: Path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(FileExistsError):
            handoff_worktrees._rename_noreplace(fd, "a.md", "b.md")
        handoff_worktrees._rename_noreplace(fd, "a.md", "c.md")
        if os.uname().sysname == "Linux":
            assert handoff_worktrees._renameat2_noreplace(fd, "c.md", "d.md")
    finally:
        os.close(fd)
    assert (tmp_path / "b.md").read_text() == "b"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.md", "d.md"] if os.uname().sysname == "Linux" else True


def test_recent_source_is_not_claimed_and_worktree_kept(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-fresh.md", "still being written")
    monkeypatch.setattr(handoff_worktrees, "QUIESCENCE_SECONDS", 5.0)

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)
    runguard.remove_worktree(main, linked, force=True)

    assert [(r.state, r.reason) for r in results] == [("failed", "source-too-recent")]
    assert linked.exists()
    assert source.read_text() == _HANDOFF.format(summary="still being written")
    assert _claims(inbox) == []
    assert not (main / ".claude" / "memory-handoffs" / source.name).exists()


def test_source_modified_after_claim_is_rechecked_and_restored(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-old.md", "old")
    _age(source)
    monkeypatch.setattr(handoff_worktrees, "QUIESCENCE_SECONDS", 5.0)
    real = handoff_worktrees._rename_noreplace

    def rename(parent, src, dst):
        real(parent, src, dst)
        if dst.startswith(".brigade-claim-"):
            os.utime(inbox / dst)  # a writer touches the claimed inode

    monkeypatch.setattr(handoff_worktrees, "_rename_noreplace", rename)
    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [(r.state, r.reason) for r in results] == [("failed", "source-too-recent")]
    assert source.is_file()
    assert _claims(inbox) == []


def test_tracked_but_deleted_destination_name_is_not_reused(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    dest = main / ".claude" / "memory-handoffs"
    tracked = _handoff(dest, "pending.md", "older committed handoff")
    _git(main, "add", "-f", "--", str(tracked.relative_to(main)))
    _git(main, "commit", "-q", "-m", "track")
    tracked.unlink()  # still in the index, gone from the working tree
    pending = _handoff(linked / ".claude" / "memory-handoffs", "pending.md", "fresh worktree handoff")

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["delivered"]
    assert not pending.exists()
    assert not (dest / "pending.md").exists()
    (published,) = list(dest.glob("pending-*.md"))
    assert published.read_text() == _HANDOFF.format(summary="fresh worktree handoff")
    assert ingest_mod.run(main) == 0
    assert len(list((main / "memory" / "handoff-inbox").glob("*pending-*.md"))) == 1


def test_unknown_destination_tracking_fails_the_rescue(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    pending = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    real = handoff_worktrees._tracked
    dest = (main / ".claude" / "memory-handoffs").resolve()
    monkeypatch.setattr(handoff_worktrees, "_tracked", lambda inbox: None if inbox == dest else real(inbox))

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [(r.state, r.reason) for r in results] == [("failed", "destination-unavailable")]
    assert pending.is_file()


def _append_after_publish(monkeypatch, writer: int, data: bytes) -> None:
    """Append through a pre-claim descriptor right after the copy is published."""
    real = handoff_worktrees._publish_bytes

    def publish(*args, **kwargs):
        published = real(*args, **kwargs)
        os.write(writer, data)
        return published

    monkeypatch.setattr(handoff_worktrees, "_publish_bytes", publish)


def _inode_in(inbox: Path, fd: int) -> list[Path]:
    info = os.fstat(fd)
    return [p for p in inbox.iterdir() if (p.stat().st_dev, p.stat().st_ino) == (info.st_dev, info.st_ino)]


def test_late_append_before_unlink_is_kept_in_source(tmp_path: Path, monkeypatch, capsys):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-slow.md", "first part")
    writer = os.open(source, os.O_WRONLY | os.O_APPEND)
    _append_after_publish(monkeypatch, writer, b"late tail\n")
    try:
        runguard.remove_worktree(main, linked, force=True)
    finally:
        os.close(writer)

    assert linked.exists()
    assert "late-write-kept" in capsys.readouterr().err
    assert source.read_text() == _HANDOFF.format(summary="first part") + "late tail\n"
    assert (main / ".claude" / "memory-handoffs" / source.name).read_text() == _HANDOFF.format(summary="first part")
    assert _claims(inbox) == []


def test_oversize_late_append_is_kept_under_a_skipped_name(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-slow.md", "first part")
    monkeypatch.setattr(handoff_worktrees, "MAX_HANDOFF_BYTES", 200)
    tail = b"x" * 1000 + b"\n"
    writer = os.open(source, os.O_WRONLY | os.O_APPEND)
    _append_after_publish(monkeypatch, writer, tail)
    try:
        results = handoff_worktrees.rescue_pending_handoffs(linked, main)
    finally:
        os.close(writer)

    assert [(r.state, r.reason) for r in results] == [("failed", "late-write-kept")]
    (kept,) = list(inbox.glob(".late-oversize-*.md.recovered"))
    assert kept.read_bytes().endswith(tail)
    assert not source.exists()
    assert _claims(inbox) == []


def _long_base(tmp_path: Path, length: int = 200) -> Path:
    """A directory whose absolute path is about `length` characters, like CI's tmp paths."""
    base = tmp_path
    while len(str(base)) < length:
        base = base / ("d" * min(60, length - len(str(base))))
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_oversize_recovery_file_keeps_worktree_on_later_forced_removal(tmp_path: Path, monkeypatch, capsys):
    main, linked = _repo_with_worktree(_long_base(tmp_path))
    assert len(str(linked)) >= 200
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-slow.md", "first part")
    monkeypatch.setattr(handoff_worktrees, "MAX_HANDOFF_BYTES", 200)
    writer = os.open(source, os.O_WRONLY | os.O_APPEND)
    _append_after_publish(monkeypatch, writer, b"x" * 1000 + b"\n")
    try:
        runguard.remove_worktree(main, linked, force=True)
    finally:
        os.close(writer)
    (kept,) = list(inbox.glob(".late-oversize-*.md.recovered"))
    first = capsys.readouterr().err
    assert "late-write-kept" in first and kept.name in first

    runguard.remove_worktree(main, linked, force=True)

    assert linked.exists()
    assert kept.is_file()
    second = capsys.readouterr().err
    assert "recovery-file-pending" in second and kept.name in second


def test_safe_path_keeps_the_final_name_whole():
    name = ".late-oversize-" + "a" * 32 + ".md.recovered"
    path = "/tmp/" + "x" * 300 + "/linked/.claude/memory-handoffs/" + name
    shown = handoff_worktrees.safe_path(path)
    assert shown.endswith("/" + name)
    assert "..." in shown and shown.startswith("/tmp/")
    assert len(shown) <= 160 + len(name)
    assert handoff_worktrees.safe_path("/short/" + name) == "/short/" + name
    assert "\\x1b" in handoff_worktrees.safe_path("/tmp/a\x1b/" + name)


def _grow_on_every_read(monkeypatch, writer: int, chunk: int, state: dict) -> None:
    """Simulate a writer faster than any reader: each module read of the
    writer's inode appends `chunk` bytes once armed (capped at 200 appends)."""
    real_read = handoff_worktrees.os.read
    target = os.fstat(writer)

    def read(fd, size):
        if state.get("armed") and state["appends"] < 200:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                os.write(writer, b"y" * chunk)
                state["appends"] += 1
        return real_read(fd, size)

    monkeypatch.setattr(handoff_worktrees.os, "read", read)


def test_continuously_growing_source_does_not_hang_rescue(tmp_path: Path, monkeypatch):
    import time as _time

    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-firehose.md", "start")
    writer = os.open(source, os.O_WRONLY | os.O_APPEND)
    state = {"armed": False, "appends": 0}
    _grow_on_every_read(monkeypatch, writer, 128 * 1024, state)
    real = handoff_worktrees._publish_bytes

    def publish(*args, **kwargs):
        published = real(*args, **kwargs)
        state["armed"] = True
        return published

    monkeypatch.setattr(handoff_worktrees, "_publish_bytes", publish)
    started = _time.monotonic()
    try:
        results = handoff_worktrees.rescue_pending_handoffs(linked, main)
        state["armed"] = False
        runguard.remove_worktree(main, linked, force=True)
        kept = _inode_in(inbox, writer)
    finally:
        os.close(writer)

    assert state["appends"] < 10
    assert _time.monotonic() - started < 15
    assert [(r.state, r.reason) for r in results] == [("failed", "late-write-kept")]
    assert len(kept) == 1
    assert linked.exists()


def test_no_read_of_the_source_after_unlink(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-firehose.md", "start")
    writer = os.open(source, os.O_WRONLY | os.O_APPEND)
    state = {"armed": False, "appends": 0}
    _grow_on_every_read(monkeypatch, writer, 128 * 1024, state)
    real_unlink = handoff_worktrees.dirfd.unlink_child

    def unlink_child(parent, name, **kwargs):
        if name.startswith(".brigade-claim-"):
            state["armed"] = True
        return real_unlink(parent, name, **kwargs)

    monkeypatch.setattr(handoff_worktrees.dirfd, "unlink_child", unlink_child)
    try:
        results = handoff_worktrees.rescue_pending_handoffs(linked, main)
    finally:
        os.close(writer)

    assert state["appends"] == 0
    assert [r.state for r in results] == ["delivered"]


def test_final_check_deadline_keeps_the_claim(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    inbox = linked / ".claude" / "memory-handoffs"
    source = _handoff(inbox, "2026-11-08-0900-slow-disk.md", "body")
    monkeypatch.setattr(handoff_worktrees, "FINAL_CHECK_SECONDS", -1.0)

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [(r.state, r.reason) for r in results] == [("failed", "late-write-kept")]
    assert source.read_text() == _HANDOFF.format(summary="body")
    assert _claims(inbox) == []


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX link fallback")
def test_link_fallback_never_overwrites_a_racing_file(tmp_path: Path, monkeypatch):
    (tmp_path / "a.md").write_text("claimed")
    monkeypatch.setattr(handoff_worktrees, "_renameat2_noreplace", lambda *args: False)
    real_link, real_rename = os.link, os.rename

    def plant(destination, dir_fd):
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dir_fd)
        os.write(fd, b"racer")
        os.close(fd)

    def link(src, dst, *args, **kwargs):
        plant(dst, kwargs["dst_dir_fd"])
        return real_link(src, dst, *args, **kwargs)

    def rename(src, dst, *args, **kwargs):
        plant(dst, kwargs["dst_dir_fd"])
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(handoff_worktrees.os, "link", link)
    monkeypatch.setattr(handoff_worktrees.os, "rename", rename)
    # Keep dirfd's capability probe true with the wrappers installed.
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {link, rename})
    fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(FileExistsError):
            handoff_worktrees._rename_noreplace(fd, "a.md", "b.md")
    finally:
        os.close(fd)
    assert (tmp_path / "b.md").read_text() == "racer"
    assert (tmp_path / "a.md").read_text() == "claimed"


@pytest.mark.skipif(os.name == "nt", reason="exercises the POSIX link fallback")
def test_link_fallback_moves_the_source(tmp_path: Path, monkeypatch):
    (tmp_path / "a.md").write_text("claimed")
    monkeypatch.setattr(handoff_worktrees, "_renameat2_noreplace", lambda *args: False)
    fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        handoff_worktrees._rename_noreplace(fd, "a.md", "b.md")
    finally:
        os.close(fd)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.md"]


def test_tracked_destination_copy_is_not_a_duplicate(tmp_path: Path, capsys):
    main, linked = _repo_with_worktree(tmp_path)
    dest = main / ".claude" / "memory-handoffs"
    committed = _handoff(dest, "committed.md", "same bytes")
    _git(main, "add", "-f", "--", str(committed.relative_to(main)))
    _git(main, "commit", "-q", "-m", "track")
    pending = _handoff(linked / ".claude" / "memory-handoffs", "pending.md", "same bytes")

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["delivered"]
    assert not pending.exists()
    assert (dest / "pending.md").is_file()
    assert ingest_mod.run(main) == 0
    drafts = list((main / "memory" / "handoff-inbox").glob("*pending*.md"))
    assert len(drafts) == 1
    assert committed.is_file()


def test_preplanted_temp_name_is_never_deleted(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    source = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    fixed = handoff_worktrees.uuid.UUID(int=7)
    monkeypatch.setattr(handoff_worktrees.uuid, "uuid4", lambda: fixed)
    planted = main / ".claude" / "memory-handoffs" / f".brigade-rescue-{fixed.hex}.tmp"
    planted.write_text("someone else's file")

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["failed"]
    assert planted.read_text() == "someone else's file"
    assert source.read_text() == _HANDOFF.format(summary="pending")


# Codex 6: broken git metadata is not "nothing tracked".


def test_broken_git_metadata_does_not_move_a_committed_handoff(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    committed = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-committed.md", "committed")
    _git(linked, "add", "-f", "--", str(committed.relative_to(linked)))
    _git(linked, "commit", "-q", "-m", "track")
    (linked / ".git").write_text("gitdir: " + str(tmp_path / "missing-admin-dir") + "\n")

    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["failed"]
    assert committed.is_file()
    assert not (main / ".claude" / "memory-handoffs" / committed.name).exists()


# Codex 7: an unreadable destination index is a failed rescue, not a raise.


@needs_posix_perms
def test_unlistable_processed_archive_is_a_failed_rescue(tmp_path: Path, capsys):
    main, linked = _repo_with_worktree(tmp_path)
    pending = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    processed = main / ".claude" / "memory-handoffs" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    processed.chmod(0)
    try:
        results = handoff_worktrees.rescue_pending_handoffs(linked, main)
        runguard.remove_worktree(main, linked, force=True)
    finally:
        processed.chmod(0o755)

    assert [r.state for r in results] == ["failed"]
    assert pending.is_file()
    assert "kept worktree" in capsys.readouterr().err


# Codex 8: a rescued handoff appears in the inbox only when complete.


def test_failed_copy_leaves_no_partial_handoff(tmp_path: Path, monkeypatch):
    main, linked = _repo_with_worktree(tmp_path)
    pending = _handoff(linked / ".claude" / "memory-handoffs", "2026-11-08-0900-pending.md", "pending")
    dest = main / ".claude" / "memory-handoffs"
    before = {p.name for p in dest.iterdir()}

    def fsync(fd):
        raise OSError("disk full")

    monkeypatch.setattr(handoff_worktrees.os, "fsync", fsync)
    results = handoff_worktrees.rescue_pending_handoffs(linked, main)

    assert [r.state for r in results] == ["failed"]
    assert pending.is_file()
    assert {p.name for p in dest.iterdir()} == before


# C. A session in a linked worktree is pointed at the main checkout's inbox.


def test_stop_hint_points_linked_worktree_at_main_checkout(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    main_inbox = (main / ".claude" / "memory-handoffs").resolve()

    assert handoff_worktrees.handoff_inbox(linked).resolve() == main_inbox
    assert str(main_inbox) in handoff_worktrees.stop_hint(linked)
    assert handoff_worktrees.stop_hint(main) == "`.claude/memory-handoffs/`"
    assert hook_runtime._is_handoff_path(linked, str(main_inbox / "note.md"))
    assert hook_runtime._is_handoff_path(linked, str(linked / ".claude" / "memory-handoffs" / "note.md"))
    assert not hook_runtime._is_handoff_path(linked, str(linked / "src" / "note.md"))


def test_stop_note_keeps_salience_rule_and_names_main_inbox(tmp_path: Path):
    from brigade.claude_hooks.handoff_close import HANDOFF_STOP_NOTE, handoff_stop_note

    main, linked = _repo_with_worktree(tmp_path)
    assert handoff_stop_note(main) == HANDOFF_STOP_NOTE
    note = handoff_stop_note(linked)
    assert str((main / ".claude" / "memory-handoffs").resolve()) in note
    assert "Nothing durable: <reason>" in note
    assert "brigade handoff draft" in note


def test_handoff_since_sees_main_checkout_inbox(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    started = "2000-01-01T00:00:00+00:00"
    assert not hook_runtime._handoff_since(linked, started)
    _handoff(main / ".claude" / "memory-handoffs", "2026-11-08-0900-from-worktree.md", "x")
    assert hook_runtime._handoff_since(linked, started)
