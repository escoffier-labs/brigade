"""Worktree fingerprint: batched untracked hashing, symlinks, nested repos."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from brigade.claude_hooks import fingerprint, runtime
from brigade.install import install_selection
from brigade.selection import Selection


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="needs POSIX symlinks and newline filenames")


def _git(target: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True)
    return result.stdout


def _git_wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    _git(target, "init")
    _git(target, "config", "user.email", "test@example.com")
    _git(target, "config", "user.name", "Test User")
    return target


def _hash_calls(monkeypatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    real_run = fingerprint._run_snapshot_git

    def tracked(repo: Path, *git_args: str, **kwargs):
        if git_args[:1] == ("hash-object",):
            calls.append(git_args)
        return real_run(repo, *git_args, **kwargs)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", tracked)
    return calls


def test_runtime_keeps_repo_worktree_fingerprint_name():
    assert runtime.repo_worktree_fingerprint is fingerprint.repo_worktree_fingerprint


@_POSIX_ONLY
def test_untracked_symlink_to_directory_signs_without_following(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "real-dir").mkdir()
    (target / "real-dir" / "inside.txt").write_text("x")
    os.symlink("real-dir", target / "link-to-dir")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    link_lines = [line for line in lines if line.startswith("untracked\tlink-to-dir\t")]
    assert len(link_lines) == 1
    assert link_lines[0].split("\t")[2].startswith("symlink:")
    assert runtime.repo_worktree_fingerprint(target) is not None


@_POSIX_ONLY
def test_untracked_symlink_retarget_changes_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "a").mkdir()
    (target / "b").mkdir()
    os.symlink("a", target / "link")
    before = runtime.repo_worktree_fingerprint(target)
    (target / "link").unlink()
    os.symlink("b", target / "link")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


@_POSIX_ONLY
def test_dangling_untracked_symlink_signs(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    os.symlink("does/not/exist", target / "dangling")

    assert runtime.repo_worktree_fingerprint(target) is not None


def test_untracked_regular_files_hash_in_one_batch(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    for index in range(25):
        (target / f"file-{index}.txt").write_text(f"content {index}\n")
    calls = _hash_calls(monkeypatch)

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    assert len(calls) == 1
    assert "--stdin-paths" in calls[0]


def test_regular_file_lines_match_per_file_hash_object(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "sub").mkdir()
    names = ["one.txt", "sub/two.bin", "with space.txt"]
    (target / "one.txt").write_text("one\n")
    (target / "sub" / "two.bin").write_bytes(b"\0\1\2")
    (target / "with space.txt").write_text("spaced\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    for name in names:
        digest = _git(target, "hash-object", "--", name).strip()
        assert f"untracked\t{name}\t{digest}" in lines


@_POSIX_ONLY
def test_newline_path_falls_back_to_per_file_hash(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "plain.txt").write_text("plain\n")
    (target / "odd\nname.txt").write_text("odd\n")
    calls = _hash_calls(monkeypatch)

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    digest = _git(target, "hash-object", "--", "odd\nname.txt").strip()
    assert f"untracked\todd\nname.txt\t{digest}" in lines
    assert ("hash-object", "--", "odd\nname.txt") in calls
    assert any("--stdin-paths" in call for call in calls)


def test_batch_count_mismatch_falls_back_to_per_file(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    for name in ("a.txt", "b.txt", "c.txt"):
        (target / name).write_text(name)
    real_run = fingerprint._run_snapshot_git
    per_file: list[str] = []

    def short_batch(repo: Path, *git_args: str, **kwargs):
        result = real_run(repo, *git_args, **kwargs)
        if git_args[:2] == ("hash-object", "--stdin-paths") and result is not None:
            result.stdout = result.stdout.splitlines()[0] + "\n"
        elif git_args[:2] == ("hash-object", "--"):
            per_file.append(git_args[-1])
        return result

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", short_batch)
    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    assert {"a.txt", "b.txt", "c.txt"} <= set(per_file)
    for name in ("a.txt", "b.txt", "c.txt"):
        digest = _git(target, "hash-object", "--", name).strip()
        assert f"untracked\t{name}\t{digest}" in lines


def _nested_repo(target: Path) -> Path:
    nested = target / "nested"
    nested.mkdir()
    _git(nested, "init")
    _git(nested, "config", "user.email", "test@example.com")
    _git(nested, "config", "user.name", "Test User")
    (nested / "tracked.txt").write_text("tracked\n")
    _git(nested, "add", "tracked.txt")
    _git(nested, "commit", "-m", "init")
    (nested / "inner.txt").write_text("inner\n")
    return nested


def test_nested_untracked_repo_is_signed_from_its_own_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    _nested_repo(target)
    status = _git(target, "status", "--porcelain", "--untracked-files=all")
    assert "?? nested/" in status

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    nested_lines = [line for line in lines if line.startswith("untracked\tnested\t")]
    assert len(nested_lines) == 1
    assert nested_lines[0].split("\t")[2].startswith("repo:")
    assert runtime.repo_worktree_fingerprint(target) is not None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda nested: (nested / "tracked.txt").write_text("edited\n"), id="tracked-edit"),
        pytest.param(lambda nested: (nested / "brand-new.txt").write_text("new\n"), id="new-file"),
        pytest.param(lambda nested: (nested / "inner.txt").write_text("changed\n"), id="untracked-edit"),
        pytest.param(lambda nested: (nested / "deep" / "x").mkdir(parents=True), id="empty-dir-noop"),
    ],
)
def test_write_inside_nested_untracked_repo_changes_parent_fingerprint(tmp_path: Path, mutate, request):
    target = _git_wired_claude(tmp_path)
    nested = _nested_repo(target)
    before = runtime.repo_worktree_fingerprint(target)
    mutate(nested)
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    if request.node.callspec.id == "empty-dir-noop":
        # Git does not track empty directories; neither does the fingerprint.
        assert before == after
    else:
        assert before != after


def test_commit_inside_nested_untracked_repo_changes_parent_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    nested = _nested_repo(target)
    before = runtime.repo_worktree_fingerprint(target)
    (nested / "tracked.txt").write_text("edited\n")
    _git(nested, "commit", "-am", "edit")
    (nested / "tracked.txt").write_text("tracked\n")
    _git(nested, "commit", "-am", "revert content")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


def test_nested_repo_recursion_depth_guard_fails_closed(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    _nested_repo(target)
    monkeypatch.setattr(fingerprint, "_NESTED_REPO_MAX_DEPTH", 0)

    assert runtime.repo_worktree_fingerprint(target) is None


def test_nested_repo_fingerprint_failure_fails_closed(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    nested = _nested_repo(target)
    real_run = fingerprint._run_snapshot_git

    def fail_nested_status(repo: Path, *args: str, **kwargs):
        if Path(repo) == nested and args[:1] == ("status",):
            return None
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", fail_nested_status)

    assert runtime.repo_worktree_fingerprint(target) is None


@_POSIX_ONLY
def test_symlink_to_nested_repo_is_not_recursed(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _git(outside, "init")
    os.symlink(str(outside), target / "linked-repo")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    linked = [line for line in lines if line.startswith("untracked\tlinked-repo\t")]
    assert len(linked) == 1
    assert linked[0].split("\t")[2].startswith("symlink:")


@_POSIX_ONLY
def test_leading_quote_filename_is_not_unquoted_by_batch(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "foo").write_text("plain\n")
    (target / '"foo"').write_text("quoted\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)
    quoted_digest = _git(target, "hash-object", "--", '"foo"').strip()
    before = runtime.repo_worktree_fingerprint(target)
    (target / '"foo"').write_text("quoted and rewritten\n")
    after = runtime.repo_worktree_fingerprint(target)

    assert lines is not None
    assert f'untracked\t"foo"\t{quoted_digest}' in lines
    assert before is not None and after is not None
    assert before != after


def test_batch_hash_failure_still_returns_none(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "new.txt").write_text("content")
    real_run = fingerprint._run_snapshot_git

    def fail_hash(repo: Path, *git_args: str, **kwargs):
        if git_args[:1] == ("hash-object",):
            return None
        return real_run(repo, *git_args, **kwargs)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", fail_hash)
    assert runtime.repo_worktree_fingerprint(target) is None


def test_batch_unavailable_skips_per_file_fallback(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "a.txt").write_text("a")
    real_run = fingerprint._run_snapshot_git
    per_file: list[str] = []

    def timed_out_batch(repo: Path, *args: str, **kwargs):
        if args[:2] == ("hash-object", "--stdin-paths"):
            return None
        if args[:2] == ("hash-object", "--"):
            per_file.append(args[-1])
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", timed_out_batch)

    assert fingerprint._git_worktree_fingerprint_lines(target) is None
    assert per_file == []


def test_batch_nonzero_exit_falls_back_to_per_file(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "a.txt").write_text("a")
    real_run = fingerprint._run_snapshot_git

    def failing_batch(repo: Path, *args: str, **kwargs):
        if args[:2] == ("hash-object", "--stdin-paths"):
            return subprocess.CompletedProcess(args=list(args), returncode=128, stdout="")
        return real_run(repo, *args, **kwargs)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", failing_batch)
    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    digest = _git(target, "hash-object", "--", "a.txt").strip()
    assert f"untracked\ta.txt\t{digest}" in lines


def test_fingerprint_unchanged_for_plain_repo_shape(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "tracked.txt").write_text("t\n")
    _git(target, "add", "tracked.txt")
    _git(target, "commit", "-m", "init")
    (target / "tracked.txt").write_text("changed\n")
    (target / "untracked.txt").write_text("u\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    untracked = [line for line in lines if line.startswith("untracked\t")]
    assert any(line.startswith("untracked\tuntracked.txt\t") for line in untracked)
    for line in untracked:
        _, relative, signature = line.split("\t")
        assert signature == _git(target, "hash-object", "--", relative).strip()


def _unborn_repo_with_staged_file(repo: Path) -> None:
    (repo / "a.txt").write_text("first\n")
    _git(repo, "add", "a.txt")


def test_unborn_repo_staged_rewrite_changes_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    _unborn_repo_with_staged_file(target)
    before = runtime.repo_worktree_fingerprint(target)
    (target / "a.txt").write_text("second\n")
    _git(target, "add", "a.txt")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


def test_unborn_repo_unstaged_rewrite_changes_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    _unborn_repo_with_staged_file(target)
    before = runtime.repo_worktree_fingerprint(target)
    (target / "a.txt").write_text("second\n")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


def test_unborn_nested_repo_staged_rewrite_changes_parent_fingerprint(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    nested = target / "nested"
    nested.mkdir()
    _git(nested, "init")
    _unborn_repo_with_staged_file(nested)
    before = runtime.repo_worktree_fingerprint(target)
    (nested / "a.txt").write_text("second\n")
    _git(nested, "add", "a.txt")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


def test_diff_head_output_unchanged_for_repo_with_head(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "tracked.txt").write_text("t\n")
    _git(target, "add", "tracked.txt")
    _git(target, "commit", "-m", "init")
    (target / "tracked.txt").write_text("changed\n")
    expected = _git(target, "diff", "HEAD", "--no-renames", "--", ".", *fingerprint._snapshot_pathspec_excludes())

    assert fingerprint._git_diff_head(target) == expected
    assert expected


@_POSIX_ONLY
def test_nested_repo_name_with_trailing_space_is_signed(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    nested = target / "nested "
    nested.mkdir()
    _git(nested, "init")
    (nested / "inner.txt").write_text("inner\n")
    before = runtime.repo_worktree_fingerprint(target)
    (nested / "inner.txt").write_text("changed\n")
    after = runtime.repo_worktree_fingerprint(target)

    assert before is not None and after is not None
    assert before != after


@_POSIX_ONLY
def test_untracked_file_name_with_trailing_space_is_hashed(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "trail ").write_text("spaced\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    digest = _git(target, "hash-object", "--", "trail ").strip()
    assert f"untracked\ttrail \t{digest}" in lines
