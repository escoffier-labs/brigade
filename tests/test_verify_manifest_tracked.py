"""Tracked workspace verify manifests must resolve and match dirty paths (#1384)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from brigade import verify_manifest
from brigade.verify_manifest import _WORKSPACE_MANIFESTS_REL

from tests.test_verify_trial import _track_workspace_manifest, _write_patch_manifest

pytest_plugins = ["tests.test_verify_trial"]

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _workspace_manifest_paths() -> list[Path]:
    root = _REPO_ROOT / _WORKSPACE_MANIFESTS_REL
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.json") if path.is_file())


def test_every_workspace_verify_manifest_resolves_by_id_and_path(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    destination = repository / _WORKSPACE_MANIFESTS_REL
    destination.parent.mkdir(parents=True)
    shutil.copytree(_REPO_ROOT / _WORKSPACE_MANIFESTS_REL, destination)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "manifests"],
        cwd=repository,
        check=True,
    )
    paths = sorted(destination.glob("*.json"))
    assert paths, "expected tracked manifests under verify/manifests/"
    seen_ids: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        manifest_id = payload.get("manifest_id")
        assert isinstance(manifest_id, str) and manifest_id.strip()
        assert manifest_id not in seen_ids, f"duplicate manifest_id: {manifest_id}"
        seen_ids.add(manifest_id)
        assert payload.get("binding_mode") == "patch_backed"
        subject = payload.get("subject")
        assert isinstance(subject, dict)
        subject_path = subject.get("subject_path")
        assert isinstance(subject_path, str) and subject_path.strip()

        by_id, id_error = verify_manifest.resolve_manifest(repository, manifest_id)
        assert id_error is None, (manifest_id, id_error)
        assert by_id is not None
        assert by_id.manifest_id == manifest_id
        assert by_id.subject_path == subject_path

        rel = path.relative_to(repository).as_posix()
        by_path, path_error = verify_manifest.resolve_manifest(repository, rel)
        assert path_error is None, (rel, path_error)
        assert by_path is not None
        assert by_path.manifest_id == manifest_id
        assert by_path.path is not None
        assert by_path.path.resolve() == path.resolve()


def test_resolve_manifest_accepts_tracked_workspace_source_path(scoreable_target):
    assert (
        subprocess.run(
            ["git", "remote"], cwd=scoreable_target, check=True, text=True, capture_output=True
        ).stdout.strip()
        == ""
    )
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    rel = f"{_WORKSPACE_MANIFESTS_REL.as_posix()}/local-patch.json"
    manifest, error = verify_manifest.resolve_manifest(scoreable_target, rel)
    assert error is None
    assert manifest is not None
    assert manifest.manifest_id == "local-patch"
    assert manifest.subject_path == "skills/brigade-work/SKILL.md"
    assert manifest.manifest_worktree_modified is False
    assert manifest.manifest_base_commit is not None


def _set_origin_default(target: Path, branch: str, commit: str) -> None:
    remotes = subprocess.run(["git", "remote"], cwd=target, check=True, text=True, capture_output=True).stdout.split()
    if "origin" not in remotes:
        bare = target.parent / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=target, check=True)
    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{branch}", commit], cwd=target, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
        cwd=target,
        check=True,
    )


def test_resolve_manifest_uses_non_main_origin_default_branch(scoreable_target):
    payload = _write_patch_manifest(scoreable_target, manifest_id="default-branch")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=scoreable_target, check=True, text=True, capture_output=True
    ).stdout.strip()
    _set_origin_default(scoreable_target, "stable", base)
    payload["checks"][0]["command"] = f'{sys.executable} -c "raise SystemExit(9)"'
    verify_manifest.write_workspace_manifest(scoreable_target, payload)

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "default-branch")

    assert error is None
    assert manifest is not None
    assert manifest.manifest_base_commit == base
    assert manifest.manifest_worktree_modified is True
    assert "print('ok')" in str(manifest.checks[0].command)


def test_resolve_manifest_refuses_manifest_absent_from_default_branch(scoreable_target):
    default_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=scoreable_target, check=True, text=True, capture_output=True
    ).stdout.strip()
    _set_origin_default(scoreable_target, "stable", default_commit)
    _write_patch_manifest(scoreable_target, manifest_id="branch-only")

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "branch-only")

    assert manifest is None
    assert error is not None
    assert "new manifest can be used once it is on the default branch" in error


def test_resolve_manifest_rejects_untracked_workspace_source_path(scoreable_target):
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": "untracked-path",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "skills/brigade-work/SKILL.md",
        },
        "checks": [
            {
                "check_id": "verify.echo-ok",
                "check_role": "effectiveness",
                "command": "true",
            }
        ],
    }
    written = verify_manifest.write_workspace_manifest(scoreable_target, payload)
    rel = written.resolve().relative_to(scoreable_target.resolve()).as_posix()
    manifest, error = verify_manifest.resolve_manifest(scoreable_target, rel)
    assert manifest is None
    assert error is not None
    assert "not on the default branch yet" in error


def test_suggested_manifest_command_names_matching_tracked_manifest(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    command = verify_manifest.suggested_manifest_command(
        scoreable_target,
        ["skills/brigade-work/SKILL.md"],
    )
    assert command == ('brigade work verify run --manifest "verify/manifests/local-patch.json" --capture brigade-work')


def test_suggested_manifest_command_matches_scope_glob_prefix(scoreable_target):
    payload = {
        "schema": "brigade.verify_manifest.v1",
        "schema_version": 1,
        "manifest_id": "scoped-patch",
        "binding_mode": "patch_backed",
        "verifier_id": "brigade.verify.test",
        "subject": {
            "artifact_kind": "skill",
            "artifact_id": "brigade-work",
            "subject_path": "src/brigade/work_cmd/verification.py",
        },
        "scope_globs": ["src/brigade/work_cmd/**", "tests/test_work_cmd_verification.py"],
        "checks": [
            {
                "check_id": "verify.echo-ok",
                "check_role": "effectiveness",
                "command": "true",
            }
        ],
    }
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    _track_workspace_manifest(scoreable_target, "scoped-patch")
    command = verify_manifest.suggested_manifest_command(
        scoreable_target,
        ["src/brigade/work_cmd/helpers.py"],
        capture="brigade-work",
    )
    assert command == ('brigade work verify run --manifest "verify/manifests/scoped-patch.json" --capture brigade-work')
    assert verify_manifest.suggested_manifest_command(scoreable_target, ["docs/readme.md"]) is None


def test_remote_without_default_branch_ref_is_refused(scoreable_target):
    bare = scoreable_target.parent / "empty-origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=scoreable_target, check=True)

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "missing")

    assert manifest is None
    assert error is not None
    assert error.startswith("unresolved_default_branch:")
    assert "git fetch origin <branch>" in error
    assert "git remote set-head origin -a" in error


def test_no_remote_binding_records_source_and_reference(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-source")

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "local-source")

    assert error is None
    assert manifest is not None
    binding = verify_manifest.build_manifest_binding(scoreable_target, manifest)
    assert binding["base_source"] == "no_remote"
    assert binding["base_reference"] == "HEAD"
    assert binding["base_commit"]


def test_origin_master_is_used_when_origin_head_is_dangling(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="master-base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=scoreable_target, check=True, text=True, capture_output=True
    ).stdout.strip()
    _set_origin_default(scoreable_target, "master", base)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone"],
        cwd=scoreable_target,
        check=True,
    )

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "master-base")

    assert error is None
    assert manifest is not None
    assert manifest.manifest_base_reference == "refs/remotes/origin/master"


def _git(target: Path, *args: str) -> str:
    identity = ["-c", "user.name=Test User", "-c", "user.email=test@example.invalid"]
    return subprocess.run(
        ["git", *identity, *args], cwd=target, check=True, text=True, capture_output=True
    ).stdout.strip()


def _publish_to_real_origin(target: Path) -> str:
    """Push HEAD to a bare repository as ``main`` so ``origin/main`` is a real tracking ref."""
    bare = target.parent / "published.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(target, "remote", "add", "origin", str(bare))
    _git(target, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(target, "fetch", "-q", "origin")
    return _git(target, "rev-parse", "refs/remotes/origin/main")


def test_manifest_weakened_in_a_commit_ahead_of_origin_main_still_runs_the_base_version(scoreable_target):
    payload = _write_patch_manifest(scoreable_target, manifest_id="published")
    published = _publish_to_real_origin(scoreable_target)
    payload["checks"][0]["command"] = f'{sys.executable} -c "raise SystemExit(0)"'
    verify_manifest.write_workspace_manifest(scoreable_target, payload)
    _git(scoreable_target, "commit", "-qam", "weaken the check on the branch")
    assert _git(scoreable_target, "rev-parse", "HEAD") != published
    assert _git(scoreable_target, "status", "--porcelain") == ""

    manifest, error = verify_manifest.resolve_manifest(scoreable_target, "published")

    assert error is None
    assert manifest is not None
    assert "print('ok')" in str(manifest.checks[0].command)
    assert manifest.manifest_base_commit == published
    assert manifest.manifest_base_source == "default_branch"
    assert manifest.manifest_worktree_modified is True


def test_manifest_deleted_or_renamed_on_the_branch_stays_registered(scoreable_target):
    payload = _write_patch_manifest(scoreable_target, manifest_id="published")
    subject = payload["subject"]["subject_path"]
    _publish_to_real_origin(scoreable_target)
    rel = f"{_WORKSPACE_MANIFESTS_REL.as_posix()}/published.json"

    for change in (
        ["mv", rel, f"{_WORKSPACE_MANIFESTS_REL.as_posix()}/renamed.json"],
        ["rm", "-q", "--cached", "-f", "--ignore-unmatch", f"{_WORKSPACE_MANIFESTS_REL.as_posix()}/renamed.json"],
    ):
        _git(scoreable_target, *change)
        _git(scoreable_target, "commit", "-qm", "move or drop the manifest on the branch")

        assert "published" in verify_manifest.registered_manifest_ids(scoreable_target)
        assert [m.manifest_id for m in verify_manifest.manifests_matching_paths(scoreable_target, [subject])] == [
            "published"
        ]
        manifest, error = verify_manifest.resolve_manifest(scoreable_target, "published")
        assert error is None
        assert manifest is not None
        assert "print('ok')" in str(manifest.checks[0].command)
        assert "renamed" not in verify_manifest.registered_manifest_ids(scoreable_target)


def test_manifest_selector_accepts_a_dot_slash_path(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    manifest, error = verify_manifest.resolve_manifest(
        scoreable_target, f"./{_WORKSPACE_MANIFESTS_REL.as_posix()}/local-patch.json"
    )
    assert error is None
    assert manifest is not None
