"""Configured external scanners cannot be replaced by workspace imports."""

import json
import subprocess
from pathlib import Path

import pytest

from brigade import scrub
from brigade.release_cmd import ci


@pytest.mark.parametrize("entry", ["scrub", "release-tip", "release-history"])
@pytest.mark.parametrize("shadow", ["brigade.py", "brigade/__init__.py", "pythonpath"])
def test_external_scanner_uses_configured_package(tmp_path, monkeypatch, entry, shadow):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    checkout = tmp_path / "scanner checkout"
    package = checkout / "src/content_guard"
    package.mkdir(parents=True)
    policies = checkout / "policies"
    policies.mkdir()
    (policies / "public-repo.json").write_text("{}")
    probe = tmp_path / "trusted-probe.json"
    imported = tmp_path / "trusted-import.json"
    (package / "__init__.py").write_text(
        "import json, os, platform, sys\nfrom pathlib import Path\n"
        f"Path({str(imported)!r}).write_text(json.dumps({{'cwd': os.getcwd(), 'file': __file__, "
        "'isolated': sys.flags.isolated, 'path': sys.path}))\n"
    )
    source = (
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        "result = {'cwd': os.getcwd(), 'file': __file__, 'args': sys.argv[1:]}\n"
        "if '--history' in sys.argv:\n"
        "    result['repo'] = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()\n"
        f"Path({str(probe)!r}).write_text(json.dumps(result))\n"
        "print('trusted scanner reported findings')\nraise SystemExit(1)\n"
    )
    for module in ("__main__.py", "git_scan.py"):
        (package / module).write_text(source)
    sentinel = tmp_path / "untrusted-executed"
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    payload = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\nraise SystemExit(0)\n"
    if shadow == "pythonpath":
        (hostile / "platform.py").write_text(payload)
        (hostile / "content_guard.py").write_text(payload)
    else:
        planted = workspace / shadow.replace("brigade", "content_guard")
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(payload)
    (checkout / "src/injected.pth").write_text(f"import pathlib; pathlib.Path({str(sentinel)!r}).touch()\n")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(checkout))
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    if entry == "scrub":
        result = scrub.run_scan(workspace, policy=str(policies / "public-repo.json"))
    else:
        result = ci._run_content_guard_check(
            workspace, name="introduced" if entry == "release-history" else "tip", policy="public-repo", base_ref="HEAD"
        )
    assert not sentinel.exists(), "external scanner executed untrusted Python"
    assert result["exit_code"] == 1, result
    loaded = json.loads(imported.read_text())
    assert Path(loaded["file"]) == package / "__init__.py"
    assert Path(loaded["cwd"]) not in (workspace, hostile)
    assert loaded["isolated"] == 1
    assert str(hostile) not in loaded["path"]
    executed = json.loads(probe.read_text())
    assert Path(executed["file"]).parent == package
    assert str(policies / "public-repo.json") in executed["args"]
    if entry == "release-history":
        assert Path(executed["repo"]) == workspace
    else:
        assert str(workspace) in executed["args"]


def test_missing_external_initializer_reports_actionable_json(tmp_path, monkeypatch, capsys):
    from brigade import cli

    checkout = tmp_path / "scanner checkout"
    checkout.mkdir()
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(checkout))
    result = cli.main(["scrub", "--target", str(tmp_path), "--json", "--no-receipt"])
    captured = capsys.readouterr()
    assert result != 0
    payload = json.loads(captured.out)
    assert "initializer not found" in payload["detail"]
    assert str(checkout / "src/content_guard/__init__.py") in payload["detail"]
    assert "Traceback" not in captured.err


def test_release_missing_external_initializer_is_actionable(tmp_path, monkeypatch):
    checkout = tmp_path / "scanner checkout"
    (checkout / "policies").mkdir(parents=True)
    (checkout / "policies/public-repo.json").write_text("{}")
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(checkout))
    argv, _, error = ci._content_guard_command(tmp_path, policy="public-repo", introduced=False, base_ref=None)
    assert argv is None
    assert "initializer not found" in error
    assert str(checkout / "src/content_guard/__init__.py") in error
    result = ci._run_content_guard_check(tmp_path, name="tip", policy="public-repo")
    assert result["status"] == "fail", "a broken configured scanner must remain a failed check"
    assert result["exit_code"] != 0
