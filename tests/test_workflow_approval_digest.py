"""Exercise the workflow approval digest with a fake GitHub CLI."""

from datetime import datetime, timedelta, timezone
import json
import subprocess

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
    assert "Example 14" in output and "2026-11-22T13:14:15Z" in output
    assert "Label present" in output
    assert "yes" in output and "no" in output
    assert "\\u007c" in output and "\\u0060" in output
    assert "hostile|`name" not in output


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
