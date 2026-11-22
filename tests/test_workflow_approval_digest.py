"""Exercise the workflow approval digest with a fake GitHub CLI."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import workflow_approval_digest, workflow_guard

APPROVAL = workflow_guard.APPROVAL_LABEL
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MERGED = NOW.isoformat().replace("+00:00", "Z")
HEADS = {11: "a" * 40, 12: "b" * 40, 13: "c" * 40, 14: "d" * 40}
FILES = {
    11: ".github/workflows/ci.yml",
    12: "scripts/verify-focused",
    13: "docs/readme.md",
    14: ".github/workflows/hostile|`name.yml",
}
HOSTILE_TITLE = "evil </table><h1>x</h1> [click](https://example.com) | a ` b"
PULLS = [
    {
        "number": number,
        "title": f"Example {number}",
        "merged_at": MERGED,
        "base": {"ref": "main"},
        "labels": ([{"name": APPROVAL}] if number == 11 else []),
    }
    for number in (11, 12, 13, 14)
]
PULLS[-1]["title"] = HOSTILE_TITLE

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "workflow_approval_digest.py"


def approval_status(number: int) -> dict[str, object]:
    return {
        "context": APPROVAL,
        "state": "success",
        "description": f"pull request {number} approved",
        "creator": {"login": "github-actions[bot]", "type": "Bot", "id": workflow_guard.GITHUB_ACTIONS_BOT_ID},
        "created_at": "2026-11-22T13:14:15Z",
    }


def revoked_status(number: int) -> dict[str, object]:
    return {
        "context": APPROVAL,
        "state": "failure",
        "description": f"pull request {number} approval removed",
        "creator": {"login": "github-actions[bot]", "type": "Bot", "id": workflow_guard.GITHUB_ACTIONS_BOT_ID},
        "created_at": "2026-11-22T13:14:15Z",
    }


def unescaped_pipe_count(line: str) -> int:
    return sum(char == "|" and (index == 0 or line[index - 1] != "\\") for index, char in enumerate(line))


def fake_gh(monkeypatch, *, fail_endpoint: str | None = None):
    def run(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert kwargs.get("shell", False) is False
        endpoint = args[2]
        if endpoint == fail_endpoint:
            raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
        if endpoint == "repos/example/project":
            payload = {"default_branch": "main"}
        elif endpoint.startswith("repos/example/project/pulls?"):
            assert args[3:] == ["--paginate", "--slurp"]
            payload = [PULLS]
        elif endpoint.endswith("/files?per_page=100"):
            assert args[3:] == ["--paginate", "--slurp"]
            number = int(endpoint.split("/pulls/")[1].split("/")[0])
            payload = [[{"filename": FILES[number], "status": "modified"}]]
        elif endpoint.endswith("/statuses?per_page=100"):
            assert args[3:] == ["--paginate", "--slurp"]
            number = next(number for number, sha in HEADS.items() if sha in endpoint)
            payload = (
                [[approval_status(number)]] if number == 11 else [[revoked_status(number)]] if number == 14 else [[]]
            )
        elif "/pulls/" in endpoint:
            number = int(endpoint.rsplit("/", 1)[1])
            payload = {
                "changed_files": 1,
                "head": {"sha": HEADS[number]},
                "base": {"ref": "main", "sha": "f" * 40},
            }
        else:
            raise AssertionError(f"Unexpected API endpoint: {endpoint}")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

    monkeypatch.setattr(workflow_guard.subprocess, "run", run)


def test_digest_includes_approved_and_missing_and_omits_unprotected(monkeypatch, capsys):
    fake_gh(monkeypatch)

    assert workflow_approval_digest.main(["--repo", "example/project"]) == 0
    output = capsys.readouterr().out
    assert "1 with a success approval status; 1 with a non-success status; 1 with no approval status." in output
    assert "Example 11" in output and "yes" in output and "2026-11-22T13:14:15Z" in output
    assert "Example 12" in output and "no" in output
    assert "Example 13" not in output
    assert "2026-11-22T13:14:15Z" in output
    rows = {
        number: next(line for line in output.splitlines() if line.startswith(f"| {number} |"))
        for number in (11, 12, 14)
    }
    assert rows[11].endswith("| yes |")
    assert rows[12].endswith("| no |")
    assert rows[14].endswith("| no |")
    assert "Label present" in output
    assert "hostile|`name" not in output
    title_cell = workflow_approval_digest.code_span(HOSTILE_TITLE)
    assert f"| 14 | {title_cell} |" in rows[14]
    assert HOSTILE_TITLE.replace("|", r"\|") in title_cell
    for fragment in ("</table>", "<h1>x</h1>", "[click]", "https://example.com"):
        assert rows[14].count(fragment) == title_cell.count(fragment) == 1
    header = next(line for line in output.splitlines() if line.startswith("| # |"))
    assert unescaped_pipe_count(rows[14]) == unescaped_pipe_count(header)


def test_api_failure_exits_two_without_partial_digest(monkeypatch, capsys):
    fake_gh(
        monkeypatch,
        fail_endpoint="repos/example/project/pulls?state=closed&base=main&sort=updated&direction=desc&per_page=100",
    )

    assert workflow_approval_digest.main(["--repo", "example/project"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Could not build a complete workflow approval digest.\n"


def test_days_excludes_older_merged_pull_requests(monkeypatch, capsys):
    fake_gh(monkeypatch)
    original = PULLS[0]["merged_at"]
    PULLS[0]["merged_at"] = (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    try:
        assert workflow_approval_digest.main(["--repo", "example/project", "--days", "7"]) == 0
        assert "Example 11" not in capsys.readouterr().out
    finally:
        PULLS[0]["merged_at"] = original


def test_cutoff_excludes_pre_guard_pull_requests_and_counts_them(monkeypatch, capsys):
    fake_gh(monkeypatch)
    old_dates = [PULLS[number - 11]["merged_at"] for number in (11, 12)]
    PULLS[0]["merged_at"] = workflow_approval_digest.GUARD_START.isoformat().replace("+00:00", "Z")
    PULLS[1]["merged_at"] = (
        (workflow_approval_digest.GUARD_START + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    )
    try:
        assert workflow_approval_digest.main(["--repo", "example/project"]) == 0
        output = capsys.readouterr().out
        assert "Excluded 1 merged protected pull request(s)" in output
        assert "at or before the cutoff" in output
        assert "Example 11" not in output
        assert "Example 12" in output
    finally:
        PULLS[0]["merged_at"], PULLS[1]["merged_at"] = old_dates


def test_title_with_three_backticks_uses_a_longer_fence(monkeypatch, capsys):
    fake_gh(monkeypatch)
    original = PULLS[-1]["title"]
    PULLS[-1]["title"] = "value with ``` run"
    try:
        assert workflow_approval_digest.main(["--repo", "example/project"]) == 0
        row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("| 14 |"))
        assert "| 14 | ```` value with ``` run ```` |" in row
    finally:
        PULLS[-1]["title"] = original


def test_script_path_help_works_from_another_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "--since" in result.stdout


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Without a shell, Windows resolves gh to an executable only, so a script shim on PATH cannot stand in for it.",
)
def test_script_path_runs_with_fake_gh_from_another_directory(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh_script = fake_bin / "fake_gh.py"
    fake_gh_script.write_text(
        "import json\n"
        "import sys\n"
        "endpoint = sys.argv[2]\n"
        "if endpoint == 'repos/example/project':\n"
        "    print(json.dumps({'default_branch': 'main'}))\n"
        "elif endpoint.startswith('repos/example/project/pulls?'):\n"
        "    print(json.dumps([[]]))\n"
        "else:\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake_gh_script}" "$@"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "example/project"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert "# Workflow approval digest:" in result.stdout
    assert "0 with a success approval status" in result.stdout
