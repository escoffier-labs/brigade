import json
import subprocess
from pathlib import Path


from brigade import release_cmd
from brigade.release_cmd import version_guard


def _write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo"',
                f'version = "{version}"',
            ]
        )
        + "\n"
    )


def test_version_tag_check_ok_when_matching_tag_exists(tmp_path):
    _write_pyproject(tmp_path, "1.2.3")

    check = version_guard.version_tag_check(
        tmp_path,
        tag_exists=lambda _target, tag: tag == "v1.2.3",
    )

    assert check["name"] == version_guard.VERSION_TAG_CHECK_NAME
    assert check["status"] == version_guard.OK
    assert check["description"] == version_guard.VERSION_TAG_CHECK_DESCRIPTION
    assert check["detail"] == "v1.2.3 exists"


def test_version_tag_check_warns_when_declared_version_has_no_tag(tmp_path):
    _write_pyproject(tmp_path, "0.25.1")

    check = version_guard.version_tag_check(tmp_path, tag_exists=lambda _target, _tag: False)

    assert check["status"] == version_guard.WARN
    assert "0.25.1" in check["detail"]
    assert "v0.25.1 is missing" in check["detail"]
    assert check["description"] == version_guard.VERSION_TAG_CHECK_DESCRIPTION


def test_version_tag_check_warns_when_pyproject_version_missing(tmp_path):
    check = version_guard.version_tag_check(tmp_path, tag_exists=lambda _target, _tag: True)

    assert check["status"] == version_guard.WARN
    assert "could not read project.version" in check["detail"]


def test_release_doctor_surfaces_version_tag_warning(tmp_path, monkeypatch, capsys):
    _write_pyproject(tmp_path, "9.9.9")
    monkeypatch.setattr(version_guard, "git_tag_exists", lambda _target, _tag: False)
    monkeypatch.setattr(release_cmd, "version_tag_check", version_guard.version_tag_check)
    monkeypatch.setattr(
        release_cmd,
        "_run_content_guard_check",
        lambda *_args, **_kwargs: {
            "name": "content_guard_tip",
            "status": release_cmd.OK,
            "detail": "clean",
            "available": True,
        },
    )
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda _target: True)

    rc = release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    matching = [item for item in payload["checks"] if item["name"] == version_guard.VERSION_TAG_CHECK_NAME]
    assert len(matching) == 1
    assert matching[0]["status"] == version_guard.WARN
    assert any(version_guard.VERSION_TAG_CHECK_NAME in warning for warning in payload["warnings"])


def test_git_tag_exists_uses_git_rev_parse(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_git(target, *args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(version_guard, "_git", fake_git)
    assert version_guard.git_tag_exists(tmp_path, "v1.0.0") is True
    assert calls == [["rev-parse", "-q", "--verify", "refs/tags/v1.0.0^{commit}"]]
