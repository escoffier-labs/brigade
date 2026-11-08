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
    assert any(line.startswith("fail") and str(locked) in line for line in stats.actions)


def test_clean_run_reports_ok(tmp_target: Path, capsys):
    inbox = _seed(tmp_target)
    _handoff(inbox, "2026-11-08-0901-good.md", "good")
    assert ingest_mod.run(tmp_target) == 0
    out = capsys.readouterr().out
    assert "Status ok" in out
    assert "PARTIAL" not in out


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
    assert any("duplicate content" in line for line in stats.actions)
    assert (copy.parent / "processed" / copy.name).is_file()


def test_worktree_discovery_is_bounded(tmp_path: Path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "commit", "-q", "--allow-empty", "-m", "init")
    for index in range(3):
        _git(main, "worktree", "add", "-q", "--detach", str(tmp_path / f"wt{index}"))
    assert len(handoff_worktrees.linked_worktrees(main, limit=2)) == 2
    assert len(handoff_worktrees.linked_worktrees(main)) == 3
    assert handoff_worktrees.linked_worktrees(tmp_path / "not-a-repo") == []


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
    renamed = [p for p in dest.glob("2026-11-08-0902-clash-*.md")]
    assert len(renamed) == 1
    assert renamed[0].read_text() == _HANDOFF.format(summary="worktree copy")


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


def test_handoff_since_sees_main_checkout_inbox(tmp_path: Path):
    main, linked = _repo_with_worktree(tmp_path)
    started = "2000-01-01T00:00:00+00:00"
    assert not hook_runtime._handoff_since(linked, started)
    _handoff(main / ".claude" / "memory-handoffs", "2026-11-08-0900-from-worktree.md", "x")
    assert hook_runtime._handoff_since(linked, started)
