"""Local repository identity, dirty-path snapshots, and overlap projection."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from types import SimpleNamespace

from brigade import fleet_session_presence as presence


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Example Dev")
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _set_origin(repo: Path, remote: str) -> None:
    existing = subprocess.run(
        ["git", "remote"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    if "origin" in existing.stdout.split():
        _git(repo, "remote", "remove", "origin")
    _git(repo, "remote", "add", "origin", remote)


def _seed_dirty_paths(repo: Path, *, count: int, include_rename: bool = False) -> None:
    brigade_dir = repo / ".brigade"
    brigade_dir.mkdir(exist_ok=True)
    (brigade_dir / "run.lock").write_text("local-only\n", encoding="utf-8")
    if include_rename:
        source = repo / "to-rename.txt"
        source.write_text("rename-me\n", encoding="utf-8")
        _git(repo, "add", "to-rename.txt")
        _git(repo, "commit", "-m", "seed rename source")
        _git(repo, "mv", "to-rename.txt", "renamed file.txt")
    for index in range(count):
        (repo / f"z-dirty-{index:03d}.txt").write_text(f"{index}\n", encoding="utf-8")


def test_remote_forms_have_one_credential_free_identity(git_repo, monkeypatch):
    identities = []
    for remote in (
        "https://github.com/example/project.git",
        "https://user:secret@github.com/example/project.git",  # content-guard: allow email
        "ssh://git@github.com/example/project.git",  # content-guard: allow email
        "git@github.com:example/project.git",  # content-guard: allow email
    ):
        _set_origin(git_repo, remote)
        identities.append(presence.repository_identity(git_repo))
    assert {item.value for item in identities} == {"github.com/example/project"}
    assert all(item.scope == "fleet" for item in identities)
    assert "secret" not in repr(identities)


def test_dirty_paths_are_bounded_relative_and_rename_safe(git_repo):
    _seed_dirty_paths(git_repo, count=70, include_rename=True)
    snapshot = presence.collect_dirty_paths(git_repo)
    assert len(snapshot.paths) == 64
    assert snapshot.truncated is True
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in snapshot.paths)
    assert "renamed file.txt" in snapshot.paths
    assert all(not path.startswith(".brigade/") for path in snapshot.paths)


def test_missing_remote_is_node_scoped_and_uses_local_digest(git_repo):
    identity = presence.repository_identity(git_repo)
    common = _git(git_repo, "rev-parse", "--git-common-dir").stdout.strip()
    common_path = Path(common) if Path(common).is_absolute() else git_repo / common
    digest = hashlib.sha256(str(common_path.resolve()).encode()).hexdigest()
    assert identity.scope == "node"
    assert identity.value == f"local:{digest}"


def test_overlap_warns_once_and_skips_same_session_or_disjoint_paths():
    current = presence.SessionSnapshot(
        harness="claude",
        session_id="sess-a",
        repo_identity="github.com/example/project",
        identity_scope="fleet",
        repo_label="project",
        checkout_path="/tmp/project",
        branch="topic",
        dirty_paths=("src/a.py", "src/b.py"),
        dirty_truncated=False,
    )
    other = {
        "node_id": "node-b",
        "harness": "cursor",
        "session_id": "sess-b",
        "repo_identity": "github.com/example/project",
        "identity_scope": "fleet",
        "checkout_path": "/other/project",
        "branch": "main",
        "dirty_paths": ["src/b.py", "src/c.py"],
        "dirty_truncated": False,
        "state": "active",
        "started_at": "2026-01-01T00:00:00+00:00",
        "expires_at": 9_999_999_999.0,
    }
    same_session = {
        **other,
        "node_id": "node-a",
        "harness": "claude",
        "session_id": "sess-a",
        "dirty_paths": ["src/a.py"],
    }
    disjoint = {**other, "session_id": "sess-c", "dirty_paths": ["docs/readme.md"]}
    warnings = presence.overlap_warnings(
        current,
        [other, same_session, disjoint],
        current_node="node-a",
    )
    assert len(warnings) == 1
    assert warnings[0]["node_id"] == "node-b"
    assert warnings[0]["paths"] == ["src/b.py"]
    assert warnings[0]["partial"] is False


def test_overlap_marks_truncated_observations_partial():
    current = presence.SessionSnapshot(
        harness="claude",
        session_id="sess-a",
        repo_identity="github.com/example/project",
        identity_scope="fleet",
        repo_label="project",
        checkout_path="/tmp/project",
        branch="topic",
        dirty_paths=("src/a.py",),
        dirty_truncated=True,
    )
    other = {
        "node_id": "node-b",
        "harness": "cursor",
        "session_id": "sess-b",
        "repo_identity": "github.com/example/project",
        "identity_scope": "fleet",
        "checkout_path": "/other/project",
        "branch": "main",
        "dirty_paths": ["src/a.py"],
        "dirty_truncated": True,
        "state": "active",
        "started_at": "2026-01-01T00:00:00+00:00",
        "expires_at": 9_999_999_999.0,
    }
    warnings = presence.overlap_warnings(current, [other], current_node="node-a")
    assert warnings[0]["partial"] is True
    assert warnings[0]["paths"] == ["src/a.py"]


def test_git_status_timeout_is_partial_never_clean(git_repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=5)

    monkeypatch.setattr(subprocess, "run", boom)
    snapshot = presence.collect_dirty_paths(git_repo)
    assert snapshot.paths == ()
    assert snapshot.truncated is True


def test_git_status_missing_binary_is_partial_never_clean(git_repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    snapshot = presence.collect_dirty_paths(git_repo)
    assert snapshot.paths == ()
    assert snapshot.truncated is True


def test_git_status_nonzero_is_partial_never_clean(git_repo, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal"),
    )
    snapshot = presence.collect_dirty_paths(git_repo)
    assert snapshot.paths == ()
    assert snapshot.truncated is True


def test_normalized_brigade_skips_are_not_partial(git_repo, monkeypatch):
    real_run = subprocess.run

    def fake(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="?? ./.brigade/local.lock\0?? .brigade/other\0")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake)
    snapshot = presence.collect_dirty_paths(git_repo)
    assert snapshot.paths == ()
    assert snapshot.truncated is False


def test_encoded_path_separators_reject_before_and_after_one_decode(git_repo):
    presence.clear_repository_identity_cache()
    decoded = presence._parse_remote("https://github.com/example/project/extra.git")
    assert decoded == "github.com/example/project/extra"
    for remote in (
        "https://github.com/example/project%2Fextra.git",
        "https://github.com/example/project%252Fextra.git",
        "https://github.com/example/project%2fextra.git",
        "git@github.com:example/project%2Fextra.git",
    ):
        assert presence._parse_remote(remote) is None
        _set_origin(git_repo, remote)
        presence.clear_repository_identity_cache()
        identity = presence.repository_identity(git_repo)
        assert identity.scope == "node"
        assert identity.value.startswith("local:")
        assert "%2F" not in identity.value
        assert "%252F" not in identity.value
    presence.clear_repository_identity_cache()


def test_decoded_remote_rejects_controls_and_secret_like_ambiguity():
    assert presence._parse_remote("https://github.com/example/pro%00ject.git") is None
    assert presence._parse_remote("https://github.com/example/token%3Dabc.git") is None
    assert presence._parse_remote(  # content-guard: allow email
        "https://user:secret@github.com/example/project.git"
    ) == ("github.com/example/project")


def test_repository_identity_is_cached_and_invalidates_when_remote_changes(git_repo):
    presence.clear_repository_identity_cache()
    _set_origin(git_repo, "https://github.com/example/project.git")
    first = presence.repository_identity(git_repo)
    assert first.value == "github.com/example/project"
    second = presence.repository_identity(git_repo)
    assert second == first
    _set_origin(git_repo, "https://github.com/example/other.git")
    third = presence.repository_identity(git_repo)
    assert third.value == "github.com/example/other"
    presence.clear_repository_identity_cache()


def test_publish_presence_skips_git_when_hub_unconfigured(git_repo, monkeypatch):
    git_calls = []
    real_run = subprocess.run

    def counted(cmd, *args, **kwargs):
        git_calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counted)
    calls = []
    monkeypatch.setattr("brigade.fleet_client.upsert_session", lambda snap: calls.append(snap) or True)
    presence._publish_presence("SessionStart", git_repo, "s1")
    assert calls == []
    assert git_calls == []


def test_worktree_identity_cache_follows_common_config(git_repo, tmp_path):
    presence.clear_repository_identity_cache()
    _set_origin(git_repo, "https://github.com/example/project.git")
    worktree = tmp_path / "linked-worktree"
    _git(git_repo, "worktree", "add", str(worktree), "HEAD")
    first = presence.repository_identity(worktree)
    assert first.value == "github.com/example/project"
    _set_origin(git_repo, "https://github.com/example/other.git")
    second = presence.repository_identity(worktree)
    assert second.value == "github.com/example/other"
    presence.clear_repository_identity_cache()


def test_git_metadata_read_rejects_oversized_and_nonregular_paths(tmp_path):
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (presence._GIT_DIR_FILE_LIMIT + 1))
    assert presence._read_bounded_text(oversized) is None

    directory = tmp_path / "directory"
    directory.mkdir()
    assert presence._read_bounded_text(directory) is None


def test_publish_presence_maps_events_and_swallows_errors(git_repo, monkeypatch):
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:3774")
    calls = []
    monkeypatch.setattr(
        "brigade.fleet_client.upsert_session",
        lambda snap: calls.append(("upsert", snap)) or True,
    )
    monkeypatch.setattr(
        "brigade.fleet_client.end_session",
        lambda snap: calls.append(("end", snap)) or True,
    )
    presence._publish_presence("SessionStart", git_repo, "s1")
    presence._publish_presence("PostToolUse", git_repo, "s1")
    presence._publish_presence("Stop", git_repo, "s1")
    assert [item[0] for item in calls] == ["upsert", "upsert", "end"]
    assert all(item[1].harness == "claude" and item[1].session_id == "s1" for item in calls)

    monkeypatch.setattr(
        "brigade.fleet_client.upsert_session",
        lambda snap: (_ for _ in ()).throw(RuntimeError("hub down")),
    )
    presence._publish_presence("SessionStart", git_repo, "s2")
