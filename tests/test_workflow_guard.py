"""Exercise the workflow guard against pull request API responses."""

import json
import os
from pathlib import Path
from posixpath import normpath
import re
import subprocess

import pytest

from scripts import workflow_guard
from tests.test_ci_workflow import _workflow_bash_command
from tests.test_publish_workflow import _load_workflow_yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
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
        "verify/manifests/example.toml",
        "pyproject.toml",
        ".content-guard-baseline.json",
        "conftest.py",
        "tests/conftest.py",
        "examples/nested/conftest.py",
        "tests/_coverage_sidecar_guard.py",
        "tests/support.py",
        "tests/test_ci_workflow.py",
        "tests/test_publish_workflow.py",
        "tests/test_workflow_guard.py",
        "tests/windows_pytest_allowlist.json",
        "tests/fleet_hub_container_smoke.py",
        "scripts/windows_job.py",
        "scripts/windows_test_weights.json",
    ],
)
def test_protected_path(path):
    assert workflow_guard.protected_paths([path]) == [path]


def test_unprotected_paths():
    assert (
        workflow_guard.protected_paths(
            [
                "src/brigade/cli.py",
                "tests/test_proc.py",
                ".github/workflows-backup/ci.yml",
                "verify-backup/required-tests.json",
            ]
        )
        == []
    )


@pytest.fixture(autouse=True)
def pull_request_event(monkeypatch):
    monkeypatch.setenv("PR_BASE_REF", "main")
    monkeypatch.setenv("REPO_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("PR_ACTION", "labeled")
    monkeypatch.setenv("APPROVAL_CLEAR_RESULT", "skipped")


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
    fake_gh(
        [
            [{"filename": f"docs/guide-{i}.md", "status": "added"} for i in range(start, start + 100)]
            for start in range(0, 3000, 100)
        ],
        changed_files=3001,
        labels=[APPROVAL],
    )
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


@pytest.mark.parametrize("base", ["other-branch", ""])
def test_rejects_nondefault_or_missing_base(fake_gh, capsys, monkeypatch, base):
    monkeypatch.setenv("PR_BASE_REF", base)
    fake_gh([[{"filename": "docs/guide.md"}]], labels=[APPROVAL])

    assert workflow_guard.main(CLI_ARGS) == 2
    assert "default branch" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("action", "clear_result", "path", "exit_code"),
    [
        ("synchronize", "success", ".github/workflows/ci.yml", 1),
        ("synchronize", "success", "docs/guide.md", 0),
        ("synchronize", "failure", "docs/guide.md", 2),
        ("synchronize", "skipped", "docs/guide.md", 2),
        ("labeled", "skipped", "pyproject.toml", 0),
    ],
)
def test_approval_after_push(fake_gh, capsys, monkeypatch, action, clear_result, path, exit_code):
    monkeypatch.setenv("PR_ACTION", action)
    monkeypatch.setenv("APPROVAL_CLEAR_RESULT", clear_result)
    fake_gh([[{"filename": path}]], labels=[APPROVAL])

    assert workflow_guard.main(CLI_ARGS) == exit_code
    if exit_code == 2:
        assert "clear" in capsys.readouterr().err


@pytest.fixture
def guard_workflow():
    return _load_workflow_yaml(WORKFLOWS / "workflow-guard.yml")


def test_workflow_triggers(guard_workflow):
    # PyYAML's YAML 1.1 loader parses the unquoted key "on" as True.
    assert guard_workflow.get("on", guard_workflow.get(True)) == {
        "pull_request_target": {
            "types": ["opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "ready_for_review"],
        },
        "merge_group": None,
    }


def test_workflow_job_contract(guard_workflow):
    read_permissions = {"contents": "read", "pull-requests": "read"}
    assert guard_workflow["permissions"] == read_permissions
    assert set(guard_workflow["jobs"]) == {"clear-approval", "workflow-guard"}

    clear = guard_workflow["jobs"]["clear-approval"]
    assert clear["if"] == "github.event.action == 'synchronize'"
    assert clear["permissions"] == {"pull-requests": "write"}
    assert all("uses" not in step for step in clear["steps"])
    assert clear["steps"][0]["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "PR_NUMBER": "${{ github.event.number }}",
    }

    guard = guard_workflow["jobs"]["workflow-guard"]
    assert guard["name"] == "workflow-guard"
    assert guard["needs"] == "clear-approval"
    assert guard["if"] == "${{ !cancelled() }}"
    assert guard["permissions"] == read_permissions
    run_guard = next(step for step in guard["steps"] if "scripts/workflow_guard.py" in step.get("run", ""))
    assert run_guard["if"] == "github.event_name == 'pull_request_target'"
    assert run_guard["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "PR_NUMBER": "${{ github.event.number }}",
        "PR_BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "REPO_DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "PR_ACTION": "${{ github.event.action }}",
        "APPROVAL_CLEAR_RESULT": "${{ needs.clear-approval.result }}",
    }
    assert guard["steps"][-1]["if"] == "github.event_name == 'merge_group'"


@pytest.mark.parametrize(
    ("status_line", "api_exit", "job_exit"),
    [
        ("HTTP/2.0 204 No Content", 0, 0),
        ("HTTP/2.0 404 Not Found", 1, 0),
        ("HTTP/2.0 403 Forbidden", 1, 1),
        ("", 1, 1),
    ],
)
def test_clear_approval_uses_one_delete_and_only_accepts_success_or_404(
    guard_workflow, status_line, api_exit, job_exit
):
    script = guard_workflow["jobs"]["clear-approval"]["steps"][0]["run"]
    fake_gh = """
gh() {
  printf '%s\\n' "$*" >&3
  printf '%s\\r\\n' "$STATUS_LINE"
  return "$API_EXIT"
}
exec 3>&1
"""
    result = subprocess.run(
        _workflow_bash_command(fake_gh + script, "-e"),
        env=dict(
            os.environ,
            GITHUB_REPOSITORY="example/project",
            PR_NUMBER="42",
            STATUS_LINE=status_line,
            API_EXIT=str(api_exit),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == job_exit, result.stderr
    assert result.stdout.splitlines() == [
        "api --include --silent -X DELETE repos/example/project/issues/42/labels/workflow-change-approved"
    ]


def test_workflow_only_checks_out_base_with_pinned_actions(guard_workflow):
    checkouts = []
    for job in guard_workflow["jobs"].values():
        assert "uses" not in job
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is not None:
                assert re.search(r"@[0-9a-f]{40}$", uses)
                if uses.startswith("actions/checkout@"):
                    checkouts.append(step)
                    assert step["with"] == {"persist-credentials": False}
                    assert step["if"].startswith("github.event_name == 'pull_request_target'")
            assert not re.search(r"\$\{\{\s*github\.(?:event\.pull_request\.|head_ref\b)", step.get("run", ""))
    assert len(checkouts) == 1


# These are ordinary tests explicitly selected by workflow commands. Requiring a
# label for edits to tests selected this way would make the approval rule depend
# on how each test is invoked.
WORKFLOW_PATH_EXEMPTIONS = {
    "tests/": "Directory selector for ordinary tests, whose individual files cannot be label-gated.",
    "tests/test_claude_hooks_state_containment.py": "Windows containment pytest selector.",
    "tests/test_cloud_agent_parity.py": "Connector and cloud parity pytest selector.",
    "tests/test_cloud_agent_profile.py": "Connector and cloud parity pytest selector.",
    "tests/test_component_manifest_provenance.py": "Manifest provenance pytest selector.",
    "tests/test_connector_fix3.py": "Connector and cloud parity pytest selector.",
    "tests/test_connector_registry.py": "Connector and cloud parity pytest selector.",
    "tests/test_connector_review_fixes.py": "Connector and cloud parity pytest selector.",
    "tests/test_cosign_attestation.py": "Attestation pytest selector.",
    "tests/test_dirfd.py": "Windows containment pytest selector.",
    "tests/test_dot_access_auth.py": "Connector and cloud parity pytest selector.",
    "tests/test_dot_compat.py": "Connector and cloud parity pytest selector.",
    "tests/test_dot_events.py": "Connector and cloud parity pytest selector.",
    "tests/test_dot_inbox_argument_errors.py": "Connector and cloud parity pytest selector.",
    "tests/test_dot_lifecycle.py": "Connector and cloud parity pytest selector.",
    "tests/test_dot_transport.py": "Connector and cloud parity pytest selector.",
    "tests/test_duet_dispatch.py": "Connector and cloud parity pytest selector.",
    "tests/test_duet_profile.py": "Connector and cloud parity pytest selector.",
    "tests/test_fleet_claim_loss_cancellation.py": "Windows containment pytest selector.",
    "tests/test_nt_private_storage.py": "Windows containment pytest selector.",
    "tests/test_proc.py": "Windows containment pytest selector.",
    "tests/test_readme_brand_header.py": "Repository metadata pytest selector.",
    "tests/test_retired_connector_review.py": "Repository metadata pytest selector.",
    "tests/test_run_journal.py": "Windows journal locking pytest selector.",
    "tests/test_skills_state_windows.py": "Windows containment pytest selector.",
    "tests/test_steward_connector_parity.py": "Connector and cloud parity pytest selector.",
    "tests/test_windows_job.py": "Windows containment pytest selector.",
}

WORKFLOW_PATH = re.compile(
    r"(?:(?P<workspace>\$\{\{\s*github\.workspace\s*\}\}|\$GITHUB_WORKSPACE|\$\{GITHUB_WORKSPACE\})[/\\]"
    r"|(?<![\w./\\-]))"
    r"(?P<path>(?:\.{1,2}[/\\])*(?:scripts|tests|registry)[/\\][\w./\\*-]*)"
)


@pytest.mark.parametrize(
    "workflow", sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml"))), ids=lambda p: p.name
)
def test_workflow_referenced_repository_paths_are_protected(workflow):
    data = _load_workflow_yaml(workflow)
    workflow_directory = data.get("defaults", {}).get("run", {}).get("working-directory", ".")
    paths = set()
    for job in data["jobs"].values():
        job_directory = job.get("defaults", {}).get("run", {}).get("working-directory", workflow_directory)
        inputs = [(str(value), ".") for value in job.get("with", {}).values()]
        for step in job.get("steps", []):
            inputs.extend((str(value), ".") for value in step.get("with", {}).values())
            inputs.append((step.get("run", ""), step.get("working-directory", job_directory)))
        for value, directory in inputs:
            for match in WORKFLOW_PATH.finditer(value):
                raw_path = match["path"].replace("\\", "/")
                root = "." if match["workspace"] or directory == "${{ github.workspace }}" else directory
                path = normpath(f"{root}/{raw_path}")
                if path.split("/", 1)[0] in {"scripts", "tests", "registry"}:
                    paths.add(path + "/" if raw_path.endswith("/") else path)

    missing = paths - WORKFLOW_PATH_EXEMPTIONS.keys() - set(workflow_guard.protected_paths(list(paths)))
    assert not missing, f"{workflow.name} uses unprotected paths: {', '.join(sorted(missing))}"
