"""Tracked workspace verify manifests must resolve and match dirty paths (#1384)."""

from __future__ import annotations

import json
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


def test_every_workspace_verify_manifest_resolves_by_id_and_path():
    paths = _workspace_manifest_paths()
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

        by_id, id_error = verify_manifest.resolve_manifest(_REPO_ROOT, manifest_id)
        assert id_error is None, (manifest_id, id_error)
        assert by_id is not None
        assert by_id.manifest_id == manifest_id
        assert by_id.subject_path == subject_path

        rel = path.relative_to(_REPO_ROOT).as_posix()
        by_path, path_error = verify_manifest.resolve_manifest(_REPO_ROOT, rel)
        assert path_error is None, (rel, path_error)
        assert by_path is not None
        assert by_path.manifest_id == manifest_id
        assert by_path.path is not None
        assert by_path.path.resolve() == path.resolve()


def test_resolve_manifest_accepts_tracked_workspace_source_path(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    rel = f"{_WORKSPACE_MANIFESTS_REL.as_posix()}/local-patch.json"
    manifest, error = verify_manifest.resolve_manifest(scoreable_target, rel)
    assert error is None
    assert manifest is not None
    assert manifest.manifest_id == "local-patch"
    assert manifest.subject_path == "skills/brigade-work/SKILL.md"


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
    assert error == "verify manifest not tracked: untracked-path"


def test_suggested_manifest_command_names_matching_tracked_manifest(scoreable_target):
    _write_patch_manifest(scoreable_target, manifest_id="local-patch")
    command = verify_manifest.suggested_manifest_command(
        scoreable_target,
        ["skills/brigade-work/SKILL.md"],
    )
    assert command == (
        'brigade work verify run --manifest "verify/manifests/local-patch.json" --capture brigade-work'
    )


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
    assert command == (
        'brigade work verify run --manifest "verify/manifests/scoped-patch.json" --capture brigade-work'
    )
    assert verify_manifest.suggested_manifest_command(scoreable_target, ["docs/readme.md"]) is None
