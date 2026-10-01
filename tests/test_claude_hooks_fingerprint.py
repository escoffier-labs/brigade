"""Worktree fingerprint: untracked stat signatures, symlinks, nested repos."""

from __future__ import annotations

import os
import subprocess
import time
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


def _git_calls(monkeypatch) -> list[tuple[Path, tuple[str, ...]]]:
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real_run = fingerprint._run_snapshot_git

    def tracked(repo: Path, *git_args: str):
        calls.append((Path(repo), git_args))
        return real_run(repo, *git_args)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", tracked)
    return calls


def _stat_signature(path: Path) -> str:
    info = os.lstat(path)
    return f"stat:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}:{info.st_ino}:{info.st_mode}"


def _fail_lstat_for(monkeypatch, name: str) -> None:
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path).name == name:
            raise PermissionError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(fingerprint.os, "lstat", lstat)


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


def test_untracked_regular_files_sign_without_git_subprocess(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    for index in range(25):
        (target / f"file-{index}.txt").write_text(f"content {index}\n")
    calls = _git_calls(monkeypatch)

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    assert {args[0] for _, args in calls} == {"status", "rev-parse", "diff"}
    assert not any("file-" in arg for _, args in calls for arg in args)


def test_regular_file_lines_carry_lstat_signature(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "sub").mkdir()
    names = ["one.txt", "sub/two.bin", "with space.txt"]
    (target / "one.txt").write_text("one\n")
    (target / "sub" / "two.bin").write_bytes(b"\0\1\2")
    (target / "with space.txt").write_text("spaced\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    for name in names:
        assert f"untracked\t{name}\t{_stat_signature(target / name)}" in lines


@_POSIX_ONLY
def test_same_size_write_with_restored_mtime_changes_fingerprint(tmp_path: Path):
    # POSIX ctime is the inode change time; Windows reports creation time.
    target = _git_wired_claude(tmp_path)
    untracked = target / "notes.txt"
    untracked.write_text("aaaa\n")
    before_stat = os.stat(untracked)
    before = runtime.repo_worktree_fingerprint(target)
    time.sleep(0.01)
    untracked.write_text("bbbb\n")
    os.utime(untracked, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after_stat = os.stat(untracked)
    after = runtime.repo_worktree_fingerprint(target)

    assert (after_stat.st_size, after_stat.st_mtime_ns) == (before_stat.st_size, before_stat.st_mtime_ns)
    assert after_stat.st_ctime_ns != before_stat.st_ctime_ns
    assert before is not None and after is not None
    assert before != after


@_POSIX_ONLY
def test_newline_path_signs_and_detects_writes(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "plain.txt").write_text("plain\n")
    odd = target / "odd\nname.txt"
    odd.write_text("odd\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)
    signature = _stat_signature(odd)
    before = runtime.repo_worktree_fingerprint(target)
    odd.write_text("odd and longer\n")
    after = runtime.repo_worktree_fingerprint(target)

    assert lines is not None
    assert f"untracked\todd\nname.txt\t{signature}" in lines
    assert before is not None and after is not None
    assert before != after


def test_consecutive_fingerprints_without_writes_are_identical(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "tracked.txt").write_text("t\n")
    _git(target, "add", "tracked.txt")
    _git(target, "commit", "-m", "init")
    (target / "tracked.txt").write_text("dirty\n")
    (target / "untracked.txt").write_text("u\n")
    _nested_repo(target)
    first_lines = fingerprint._git_worktree_fingerprint_lines(target)
    first = runtime.repo_worktree_fingerprint(target)
    for path in (target / "untracked.txt", target / "nested" / "inner.txt", target / "nested" / "tracked.txt"):
        path.read_bytes()
    _git(target / "nested", "status")
    second = runtime.repo_worktree_fingerprint(target)
    second_lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert first is not None
    assert first == second
    assert first_lines == second_lines
    untracked_paths = [Path(line.split("\t")[1]) for line in first_lines or [] if line.startswith("untracked\t")]
    assert not any(".git" in path.parts or path.name.endswith(".lock") for path in untracked_paths)


def test_untracked_lstat_failure_returns_none(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "new.txt").write_text("content")
    _fail_lstat_for(monkeypatch, "new.txt")

    assert runtime.repo_worktree_fingerprint(target) is None


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
    quoted_signature = _stat_signature(target / '"foo"')
    before = runtime.repo_worktree_fingerprint(target)
    (target / '"foo"').write_text("quoted and rewritten\n")
    after = runtime.repo_worktree_fingerprint(target)

    assert lines is not None
    assert f'untracked\t"foo"\t{quoted_signature}' in lines
    assert before is not None and after is not None
    assert before != after


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
        assert signature == _stat_signature(target / relative)


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
def test_untracked_file_name_with_trailing_space_is_signed(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "trail ").write_text("spaced\n")

    lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert lines is not None
    assert f"untracked\ttrail \t{_stat_signature(target / 'trail ')}" in lines


def _nested_repos(target: Path) -> list[Path]:
    repos = [_nested_repo(target)]
    for name in ("second", "third", "unborn"):
        repo = target / name
        repo.mkdir()
        _git(repo, "init")
        (repo / "file.txt").write_text(f"{name}\n")
        if name != "unborn":
            _git(repo, "add", "file.txt")
            _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-m", "init")
            (repo / "extra.txt").write_text("extra\n")
        repos.append(repo)
    (target / "plain.txt").write_text("plain\n")
    return repos


def test_concurrent_nested_signatures_match_serial_run(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    _nested_repos(target)

    concurrent_lines = fingerprint._git_worktree_fingerprint_lines(target)
    monkeypatch.setattr(fingerprint, "_NESTED_REPO_MAX_WORKERS", 1)
    serial_lines = fingerprint._git_worktree_fingerprint_lines(target)

    assert concurrent_lines is not None
    assert concurrent_lines == serial_lines
    assert sum("\trepo:" in line for line in concurrent_lines) == 4


def test_nested_repo_probe_is_one_rev_parse_call(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    repos = _nested_repos(target)
    calls = _git_calls(monkeypatch)

    assert fingerprint._git_worktree_fingerprint_lines(target) is not None

    for repo in repos:
        rev_parses = [args for path, args in calls if path == repo and args[0] == "rev-parse"]
        assert rev_parses == [("rev-parse", "--show-toplevel", "--verify", "-q", "HEAD")]


def test_one_failing_nested_repo_among_many_fails_closed(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    failing = _nested_repos(target)[2]
    real_run = fingerprint._run_snapshot_git

    def fail_one_status(repo: Path, *args: str):
        if Path(repo) == failing and args[:1] == ("status",):
            return None
        return real_run(repo, *args)

    monkeypatch.setattr(fingerprint, "_run_snapshot_git", fail_one_status)

    assert runtime.repo_worktree_fingerprint(target) is None


def test_content_signing_catches_rewrite_that_stat_misses(tmp_path: Path, monkeypatch):
    # Windows reports creation time as st_ctime, so a same-size rewrite with
    # mtime restored leaves every stat field alone. Freeze lstat for the file
    # to model that; only the content signature can see the write.
    target = _git_wired_claude(tmp_path)
    untracked = target / "notes.txt"
    untracked.write_text("aaaa\n")
    frozen = os.lstat(untracked)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path).name == "notes.txt":
            return frozen
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(fingerprint.os, "lstat", lstat)
    monkeypatch.setattr(fingerprint, "_SIGN_CONTENT", False)
    stat_only_before = runtime.repo_worktree_fingerprint(target)
    monkeypatch.setattr(fingerprint, "_SIGN_CONTENT", True)
    content_before = runtime.repo_worktree_fingerprint(target)
    untracked.write_text("bbbb\n")
    content_after = runtime.repo_worktree_fingerprint(target)
    monkeypatch.setattr(fingerprint, "_SIGN_CONTENT", False)
    stat_only_after = runtime.repo_worktree_fingerprint(target)

    assert stat_only_before == stat_only_after
    assert content_before is not None and content_after is not None
    assert content_before != content_after


def test_nested_repos_fan_out_only_at_top_level(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    for name in ("outer-a", "outer-b"):
        outer = target / name
        outer.mkdir()
        _git(outer, "init")
        (outer / "file.txt").write_text(name)
        inner = outer / "inner"
        inner.mkdir()
        _git(inner, "init")
        (inner / "file.txt").write_text("inner")
    pools: list[int] = []
    real_pool = fingerprint.ThreadPoolExecutor

    def counting_pool(*args, **kwargs):
        pools.append(kwargs.get("max_workers", 0))
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(fingerprint, "ThreadPoolExecutor", counting_pool)

    assert runtime.repo_worktree_fingerprint(target) is not None
    assert pools == [2]
