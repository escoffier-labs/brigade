"""Cross-platform tests for ingest's Git directory discovery."""

import subprocess
from pathlib import Path, PureWindowsPath

from brigade.ingest import _git_tracked_names


def test_configured_worktree_resolves_relative_windows_gitfile(tmp_path, monkeypatch):
    worktree = tmp_path.resolve()
    checkout = worktree / "checkout"
    inbox = checkout / "inbox"
    inbox.mkdir(parents=True)
    marker = checkout / ".git"
    metadata = worktree / "metadata"
    git = subprocess.run
    git(["git", "-C", str(checkout), "init", "-q"], check=True)
    marker.rename(metadata)
    gitfile = b"gitdir: ../metadata\r\n"
    marker.write_bytes(gitfile)
    git(["git", "-C", str(checkout), "config", "core.worktree", worktree.as_posix()], check=True)
    (inbox / "tracked.md").write_bytes(b"handoff\n")
    git(["git", "-C", str(checkout), "update-index", "--add", "--", "inbox/tracked.md"], check=True)

    class WindowsMarker(PureWindowsPath):
        def lstat(self):
            return marker.lstat()

    windows_marker = WindowsMarker("C:/example/checkout/.git")
    join = type(checkout).__truediv__

    def join_marker(path: Path, name: str):
        if path == checkout and name == ".git":
            return windows_marker
        return join(path, name)

    # Model Win32 opening a backslash path while Git's parser sees no '/'.
    # The slashless aliases let native Git exercise that parser on either OS.
    for directory in (checkout, worktree):
        (directory / "gitfile-alias").write_bytes(gitfile)

    def run_git(args, **kwargs):
        if "--git-dir" in args:
            args = args.copy()
            index = args.index("--git-dir") + 1
            if args[index] == windows_marker.as_posix():
                args[index] = marker.as_posix()
            elif args[index] == str(windows_marker):
                args[index] = "gitfile-alias"
        return git(args, **kwargs)

    monkeypatch.setattr(type(checkout), "__truediv__", join_marker)
    monkeypatch.setattr(subprocess, "run", run_git)

    assert _git_tracked_names(inbox) == {"tracked.md"}
