"""Exercise the workflow guard against pull request API responses."""

import json
from pathlib import Path
import subprocess

import pytest

from scripts import workflow_guard

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = "workflow-change-approved"
PULL = "repos/example/project/pulls/42"
CLI_ARGS = ["--repo", "example/project", "--pr", "42"]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
        "scripts/ci_docs_only.py",
        "scripts/pr_test_tiers.json",
        "scripts/pr_tier_selection.py",
        "scripts/workflow_guard.py",
        "scripts/windows_affected_tests.py",
        "verify/required-check.json",
        "verify/required-tests.json",
    ],
)
def test_protected_path(path):
    assert workflow_guard.protected_paths([path]) == [path]


def test_unprotected_paths():
    assert (
        workflow_guard.protected_paths(
            ["src/brigade/cli.py", ".github/workflows-backup/ci.yml", "verify/required-tests.json.bak"]
        )
        == []
    )


@pytest.fixture
def fake_gh(monkeypatch):
    def install(pages, *, labels=(), changed_files=None, fail=False):
        def run(args, **kwargs):
            assert args[:2] == ["gh", "api"]
            assert kwargs.get("shell", False) is False
            if args[2] == f"{PULL}/files?per_page=100":
                assert args[3:] == ["--paginate", "--slurp"]
                if fail:
                    raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
                payload = pages
            else:
                assert args[2:] == [PULL]
                payload = {
                    "changed_files": sum(map(len, pages)) if changed_files is None else changed_files,
                    "labels": [{"name": label} for label in labels],
                }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(workflow_guard.subprocess, "run", run)

    return install


@pytest.mark.parametrize(
    ("path", "labels", "exit_code"),
    [
        ("docs/guide.md", [], 0),
        (".github/workflows/ci.yml", [APPROVAL], 0),
        (".github/workflows/ci.yml", ["unrelated"], 1),
    ],
)
def test_label_decision(fake_gh, capsys, path, labels, exit_code):
    fake_gh([[{"filename": path, "status": "modified"}]], labels=labels)

    assert workflow_guard.main(CLI_ARGS) == exit_code

    if path.startswith(".github/"):
        output = capsys.readouterr().out
        assert path in output
        assert APPROVAL in output
        assert "maintainer" in output and "after reading" in output


def test_custom_label(fake_gh):
    fake_gh([[{"filename": "scripts/ci_docs_only.py", "status": "modified"}]], labels=["ci-reviewed"])
    assert workflow_guard.main([*CLI_ARGS, "--label", "ci-reviewed"]) == 0


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ("docs/guide.md", ".github/workflows/ci.yml"),
        (".github/workflows/ci.yml", "docs/guide.md"),
    ],
)
def test_rename_checks_both_paths(fake_gh, capsys, previous, current):
    fake_gh([[{"filename": current, "previous_filename": previous, "status": "renamed"}]])

    assert workflow_guard.main(CLI_ARGS) == 1
    assert ".github/workflows/ci.yml" in capsys.readouterr().out


def test_reads_later_pages(fake_gh):
    fake_gh(
        [
            [{"filename": f"docs/guide-{i}.md", "status": "added"} for i in range(100)],
            [{"filename": "verify/required-tests.json", "status": "modified"}],
        ]
    )
    assert workflow_guard.main(CLI_ARGS) == 1


def test_failed_file_request_cannot_pass(fake_gh, capsys):
    fake_gh([], labels=[APPROVAL], fail=True)

    assert workflow_guard.main(CLI_ARGS) == 2
    assert "Could not read" in capsys.readouterr().err


def test_incomplete_file_list_cannot_pass(fake_gh):
    fake_gh([[{"filename": "docs/guide.md", "status": "added"}]], changed_files=2)
    assert workflow_guard.main(CLI_ARGS) == 2


def test_rename_missing_previous_path_cannot_pass(fake_gh):
    fake_gh([[{"filename": "docs/guide.md", "status": "renamed"}]])
    assert workflow_guard.main(CLI_ARGS) == 2


def test_protected_filename_stays_on_one_log_line(fake_gh, capsys):
    path = ".github/actions/example/\n::error::example"
    fake_gh([[{"filename": path, "status": "added"}]])

    assert workflow_guard.main(CLI_ARGS) == 1
    output = capsys.readouterr().out
    assert json.dumps(path) in output
    assert "\n::error::" not in output


def test_workflow_runs_the_pull_request_check_from_the_base():
    text = (ROOT / ".github/workflows/workflow-guard.yml").read_text(encoding="utf-8")
    assert "\n  pull_request_target:\n" in text
    assert "\n  merge_group:\n" in text
    assert "github.event.pull_request.head" not in text
    assert "\n  workflow-guard:\n" in text
    assert "persist-credentials: false" in text
