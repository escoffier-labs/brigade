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
HEAD = "a" * 40
STATUSES = f"repos/example/project/commits/{HEAD}/statuses?per_page=100"
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
    monkeypatch.setenv("APPROVAL_RECORD_RESULT", "skipped")


@pytest.fixture
def fake_gh(monkeypatch):
    def install(
        pages,
        *,
        labels=(),
        changed_files=None,
        fail=False,
        statuses=([],),
        heads=(HEAD, HEAD),
        fail_statuses=False,
        number=42,
    ):
        pull = f"repos/example/project/pulls/{number}"
        live_heads = iter(heads)

        def run(args, **kwargs):
            assert args[:2] == ["gh", "api"]
            assert kwargs.get("shell", False) is False
            if args[2] == f"{pull}/files?per_page=100":
                assert args[3:] == ["--paginate", "--slurp"]
                if fail:
                    raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
                payload = pages
            elif args[2] == STATUSES:
                assert args[3:] == ["--paginate", "--slurp"]
                assert workflow_guard.protected_paths(
                    [entry["filename"] for page in pages for entry in page]
                    + [entry["previous_filename"] for page in pages for entry in page if "previous_filename" in entry]
                )
                if fail_statuses:
                    raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
                payload = statuses
            else:
                assert args[2:] == [pull]
                payload = {
                    "changed_files": sum(map(len, pages)) if changed_files is None else changed_files,
                    "head": {"sha": next(live_heads)},
                    "base": {"ref": "main", "sha": "b" * 40},
                    "labels": [{"name": label} for label in labels],
                }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(workflow_guard.subprocess, "run", run)

    return install


def approval_status(
    state="success", *, login="github-actions[bot]", creator_type="Bot", creator_id=41898282, context=APPROVAL
):
    return {
        "context": context,
        "state": state,
        "description": "pull request 42 approval removed" if state == "failure" else "pull request 42 approved",
        "creator": {"login": login, "type": creator_type, "id": creator_id},
    }


@pytest.mark.parametrize(
    ("statuses", "exit_code"),
    [
        ([[]], 1),
        ([[approval_status()]], 0),
        ([[approval_status("failure"), approval_status()]], 1),
        ([[approval_status("pending"), approval_status()]], 1),
        ([[approval_status(login="reviewer"), approval_status(creator_type="User")]], 1),
        ([[approval_status(context="other-check"), approval_status(login="reviewer")], [approval_status()]], 0),
        ([[approval_status("failure", login="reviewer"), approval_status()]], 0),
        ([[approval_status(creator_id=7)]], 1),
        ([[approval_status(creator_id=None)]], 2),
        ([[dict(approval_status(), creator=None)]], 2),
        ([[approval_status(True)]], 2),
        ([{"unexpected": []}], 2),
    ],
)
def test_commit_status_decision(fake_gh, capsys, statuses, exit_code):
    path = ".github/workflows/ci.yml"
    fake_gh([[{"filename": path}]], labels=[APPROVAL], statuses=statuses)

    assert workflow_guard.main(CLI_ARGS) == exit_code
    output = capsys.readouterr().out
    assert path in output
    if exit_code == 1:
        assert APPROVAL in output
        assert "maintainer" in output and "this pull request at its current head" in output


@pytest.mark.parametrize(
    "description",
    [
        None,
        "Workflow change approval",
        "pull request approved",
        "pull request 43 approved",
        "pull request 42 approved\n",
    ],
)
def test_status_without_a_matching_pr_number_is_ignored(fake_gh, description):
    status = approval_status()
    if description is None:
        del status["description"]
    else:
        status["description"] = description
    files = [[{"filename": ".github/workflows/ci.yml"}]]
    fake_gh(files, statuses=[[status]])
    assert workflow_guard.main(CLI_ARGS) == 1
    fake_gh(files, statuses=[[dict(status, state="failure"), approval_status()]])
    assert workflow_guard.main(CLI_ARGS) == 0


def test_shared_head_approval_applies_to_one_pull_request(fake_gh):
    files = [[{"filename": ".github/workflows/ci.yml"}]]
    statuses = [[approval_status()]]
    fake_gh(files, statuses=statuses)
    assert workflow_guard.main(CLI_ARGS) == 0
    fake_gh(files, statuses=statuses, number=43)
    assert workflow_guard.main([*CLI_ARGS[:-1], "43"]) == 1


def test_retarget_after_nondefault_base_label_requires_approval(fake_gh, monkeypatch):
    # The writer is skipped on the non-default base, as pinned by the job contract test.
    monkeypatch.setenv("PR_BASE_REF", "other-branch")
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], labels=[APPROVAL])
    assert workflow_guard.main(CLI_ARGS) == 2
    monkeypatch.setenv("PR_BASE_REF", "main")
    assert workflow_guard.main(CLI_ARGS) == 1


def test_unprotected_changes_do_not_read_statuses(fake_gh):
    fake_gh([[{"filename": "docs/guide.md"}]], fail_statuses=True)
    assert workflow_guard.main(CLI_ARGS) == 0


def test_failed_status_request_cannot_pass(fake_gh):
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], labels=[APPROVAL], fail_statuses=True)
    assert workflow_guard.main(CLI_ARGS) == 2


@pytest.mark.parametrize("heads", [(HEAD, "c" * 40), ("invalid", "invalid"), (None, None)])
def test_inconsistent_or_invalid_live_head_cannot_pass(fake_gh, heads):
    fake_gh([[{"filename": "docs/guide.md"}]], heads=heads)
    assert workflow_guard.main(CLI_ARGS) == 2


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


@pytest.mark.parametrize("result", ["failure", "cancelled", ""])
def test_record_failure_cannot_pass_with_older_approval(fake_gh, capsys, monkeypatch, result):
    monkeypatch.setenv("APPROVAL_RECORD_RESULT", result)
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], statuses=[[approval_status()]])

    assert workflow_guard.main(CLI_ARGS) == 2
    assert "record" in capsys.readouterr().err


@pytest.mark.parametrize("clear_result", ["failure", "success"])
def test_label_cleanup_is_advisory(fake_gh, monkeypatch, clear_result):
    monkeypatch.setenv("APPROVAL_CLEAR_RESULT", clear_result)
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], statuses=[[approval_status()]])

    assert workflow_guard.main(CLI_ARGS) == 0


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
    assert set(guard_workflow["jobs"]) == {"clear-approval", "record-approval", "workflow-guard"}

    guard = guard_workflow["jobs"]["workflow-guard"]
    assert guard["name"] == "workflow-guard"
    assert guard["needs"] == ["clear-approval", "record-approval"]
    assert guard["if"] == "${{ !cancelled() }}"
    assert guard["permissions"] == {**read_permissions, "statuses": "read"}
    run_guard = next(step for step in guard["steps"] if "scripts/workflow_guard.py" in step.get("run", ""))
    assert run_guard["if"] == "github.event_name == 'pull_request_target'"
    assert run_guard["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "PR_NUMBER": "${{ github.event.number }}",
        "PR_BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "REPO_DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}",
        "APPROVAL_RECORD_RESULT": "${{ needs.record-approval.result }}",
    }
    assert guard["steps"][-1]["if"] == "github.event_name == 'merge_group'"


CLEAR_APPROVAL_SCRIPT = """\
if response=$(gh api --include --silent -X DELETE "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/labels/workflow-change-approved" 2>/dev/null); then
  exit 0
fi
if [[ $response =~ ^HTTP/[^[:space:]]+[[:space:]]404([[:space:]]|$) ]]; then
  exit 0
fi
echo "Could not clear the workflow approval label." >&2
exit 1
"""

RECORD_APPROVAL_SCRIPT = """\
if [[ ! $PR_HEAD_SHA =~ ^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$ ]]; then
  echo "Invalid pull request head commit." >&2
  exit 1
fi
if [[ ! $PR_NUMBER =~ ^[0-9]+$ ]]; then
  echo "Invalid pull request number." >&2
  exit 2
fi
state=failure
description="pull request $PR_NUMBER approval removed"
case "$PR_ACTION" in
  labeled)
    labels=$(gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/labels?per_page=100" --paginate --slurp) || exit 2
    present=$(jq -er '
      if type == "array" and length > 0 and all(.[]; type == "array") then
        [ .[][] | if (.name | type) == "string" then .name else error("Invalid label") end ]
        | any(. == "workflow-change-approved") | tostring
      else error("Invalid label pages")
      end
    ' <<< "$labels") || exit 2
    if [[ $present == true ]]; then
      state=success
      description="pull request $PR_NUMBER approved"
    fi
    ;;
  unlabeled) ;;
  *) exit 1 ;;
esac
gh api --silent -X POST "repos/$GITHUB_REPOSITORY/statuses/$PR_HEAD_SHA" \\
  -f context=workflow-change-approved -f state="$state" -f description="$description" || exit 2
"""


def test_write_jobs_only_run_the_expected_scripts(guard_workflow):
    jobs = guard_workflow["jobs"]
    assert {name for name, job in jobs.items() if "write" in job.get("permissions", {}).values()} == {
        "clear-approval",
        "record-approval",
    }
    clear = jobs["clear-approval"]
    record = jobs["record-approval"]
    assert clear["if"] == "github.run_attempt == '1' && github.event.action == 'synchronize'"
    assert record["if"] == (
        "github.run_attempt == '1' && github.event.label.name == 'workflow-change-approved' && "
        "github.event.pull_request.base.ref == github.event.repository.default_branch && "
        "(github.event.action == 'labeled' || github.event.action == 'unlabeled')"
    )
    assert set(clear) == {"if", "permissions", "runs-on", "steps"}
    assert set(record) == {*clear, "concurrency"}
    assert record["concurrency"] == {
        "group": "workflow-guard-record-approval-${{ github.event.number }}",
        "cancel-in-progress": False,
    }
    for job, permissions, script, env in (
        (clear, {"pull-requests": "write"}, CLEAR_APPROVAL_SCRIPT, {}),
        (
            record,
            {"statuses": "write", "pull-requests": "read"},
            RECORD_APPROVAL_SCRIPT,
            {
                "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                "PR_ACTION": "${{ github.event.action }}",
            },
        ),
    ):
        assert job["runs-on"] == "ubuntu-latest"
        assert job["permissions"] == permissions
        (step,) = job["steps"]
        assert set(step) == {"name", "env", "run"}
        assert step["env"] == {
            "GH_TOKEN": "${{ github.token }}",
            "PR_NUMBER": "${{ github.event.number }}",
            **env,
        }
        assert step["run"] == script


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


@pytest.mark.parametrize(
    ("sha", "number", "action", "labels", "api_exit", "job_exit", "read_labels", "state"),
    [
        (HEAD, "42", "labeled", [[{"name": APPROVAL}]], 0, 0, True, "success"),
        (HEAD, "42", "labeled", [[], [{"name": APPROVAL}]], 0, 0, True, "success"),
        (HEAD, "42", "labeled", [[{"name": "other"}]], 0, 0, True, "failure"),
        (HEAD, "42", "labeled", [[{"name": APPROVAL}, {}]], 0, 2, True, None),
        (HEAD, "42", "labeled", [[]], 1, 2, True, None),
        ("B" * 64, "42", "unlabeled", [[{"name": APPROVAL}]], 0, 0, False, "failure"),
        (HEAD, "42", "unlabeled", [[]], 1, 2, False, "failure"),
        ("a" * 39, "42", "labeled", [[]], 0, 1, False, None),
        ("G" * 40, "42", "labeled", [[]], 0, 1, False, None),
        ("a" * 41, "42", "labeled", [[]], 0, 1, False, None),
        (HEAD, "42/../43", "labeled", [[]], 0, 2, False, None),
        (HEAD, "42", "edited", [[]], 0, 1, False, None),
    ],
)
def test_record_approval_checks_live_label_and_posts_once_to_the_event_commit(
    guard_workflow, sha, number, action, labels, api_exit, job_exit, read_labels, state
):
    script = guard_workflow["jobs"]["record-approval"]["steps"][0]["run"]
    fake_gh = """
gh() {
  printf '%s\\n' "$*" >&3
  if [[ $2 == */labels?per_page=100 ]]; then
    printf '%s\\n' "$LABELS"
  fi
  return "$API_EXIT"
}
exec 3>&1
"""
    result = subprocess.run(
        _workflow_bash_command(fake_gh + script, "-e"),
        env=dict(
            os.environ,
            GITHUB_REPOSITORY="example/project",
            PR_HEAD_SHA=sha,
            PR_NUMBER=number,
            PR_ACTION=action,
            API_EXIT=str(api_exit),
            LABELS=json.dumps(labels),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == job_exit, result.stderr
    calls = []
    if read_labels:
        calls.append("api repos/example/project/issues/42/labels?per_page=100 --paginate --slurp")
    if state is not None:
        description = "approved" if state == "success" else "approval removed"
        calls.append(
            f"api --silent -X POST repos/example/project/statuses/{sha} "
            f"-f context={APPROVAL} -f state={state} -f description=pull request 42 {description}"
        )
    assert result.stdout.splitlines() == calls


def test_workflow_only_checks_out_base_with_pinned_actions(guard_workflow):
    checkouts = []
    for job in guard_workflow["jobs"].values():
        assert "uses" not in job
        for step in job["steps"]:
            uses = step.get("uses")
            if uses is not None:
                assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", uses)
                checkouts.append(step)
                assert step["with"] == {"persist-credentials": False}
                assert step["if"] == "github.event_name == 'pull_request_target'"
    assert len(checkouts) == 1


def workflow_strings(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from workflow_strings(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from workflow_strings(child, (*path, index))
    elif isinstance(value, str):
        yield path, value


def test_workflow_expressions_stay_out_of_shell_and_untrusted_inputs(guard_workflow):
    event_head = ("jobs", "record-approval", "steps", 0, "env", "PR_HEAD_SHA")
    trusted_inputs = {
        "github.token",
        "github.event.number",
        "github.event.pull_request.base.ref",
        "github.event.repository.default_branch",
        "github.event.action",
        "needs.record-approval.result",
    }
    for path, value in workflow_strings(guard_workflow):
        if "run" in path:
            assert "${{" not in value, path
        if "env" in path or "with" in path:
            expressions = re.findall(r"\$\{\{\s*(.*?)\s*\}\}", value, re.DOTALL)
            allowed = {"github.event.pull_request.head.sha"} if path == event_head else trusted_inputs
            assert set(expressions) <= allowed, path
        if "github.event.pull_request.head" in value:
            assert path == event_head and value == "${{ github.event.pull_request.head.sha }}"


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
    r"(?P<path>(?:\.{1,2}[/\\])*(?:scripts|tests|registry|npm)[/\\][\w./\\*-]*)"
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
                if path.split("/", 1)[0] in {"scripts", "tests", "registry", "npm"}:
                    is_directory = raw_path.endswith("/") or (ROOT / path).is_dir()
                    paths.add(path + "/" if is_directory else path)

    missing = paths - WORKFLOW_PATH_EXEMPTIONS.keys() - set(workflow_guard.protected_paths(list(paths)))
    assert not missing, f"{workflow.name} uses unprotected paths: {', '.join(sorted(missing))}"


# List fixed CI inputs that workflow commands reach indirectly. Ordinary test
# selections and source trees processed by the CI checks remain out of scope.
TRANSITIVE_INPUTS = {
    "npm/feasibility/acceptance.cjs": ("npm/feasibility/provenance.cjs",),
    "npm/feasibility/prepare.py": (
        "npm/feasibility/build-requirements.txt",
        "npm/feasibility/launcher.cjs",
        "npm/feasibility/package.json",
        "npm/feasibility/runtime.json",
        "scripts/prepare_dev_wheel.py",
    ),
    "npm/feasibility/provenance.test.cjs": (
        "npm/feasibility/fixtures/sigstore-3.1.0-attestations.json",
        "npm/feasibility/provenance.cjs",
    ),
    "npm/feasibility/publish-compat.cjs": ("npm/feasibility/provenance.cjs", "npm/feasibility/readback.cjs"),
    "npm/feasibility/publish.cjs": ("npm/feasibility/provenance.cjs", "npm/feasibility/readback.cjs"),
    "scripts/check_size_ratchet_baseline.py": ("tests/test_module_size_ratchet.py",),
    "scripts/ci_hub_select.py": ("scripts/ci_hub_paths.yml",),
    "scripts/ci_shadow_report.py": ("scripts/ci_selection_core.py", "scripts/ci_test_selection.py"),
    "scripts/ci_test_selection.py": ("scripts/ci_selection_core.py",),
    "scripts/managed_snapshot.py": ("src/brigade/templates/stations/managed-snapshot.json",),
    "scripts/memory_scan_bundle.py": (
        "engines/hub/src/memory/scan.rs",
        "scripts/memory_scan_unicode14_unassigned.ranges",
        "src/brigade/guard/memory-rules.json",
        "src/brigade/memory_content_scan.py",
    ),
    "scripts/pr_tier_selection.py": ("scripts/pr_test_tiers.json", "scripts/windows_affected_tests.py"),
    "scripts/pr_tier_shadow.py": ("scripts/pr_tier_selection.py",),
    "scripts/retired_engine_names.py": (
        "src/brigade/__init__.py",
        "tests/fixtures/retired-engine-names-allowlist.json",
    ),
    "scripts/shipped_file_history.py": ("src/brigade/templates/shipped-file-history.json",),
    "scripts/template_profile_snapshot.py": ("src/brigade/templates/template-profile-snapshot.json",),
    "scripts/verify-focused": (
        "scripts/managed_snapshot.py",
        "scripts/shipped_file_history.py",
        "scripts/template_profile_snapshot.py",
        "scripts/verify_env.py",
        "scripts/version_sync.py",
    ),
    "scripts/verify-guard-native": ("scripts/verify-focused", "tests/test_verify_brigade_guard_parity.py"),
    "scripts/verify_component_manifest_provenance.py": ("src/brigade/templates/components/manifest-v1.json",),
    "scripts/version_sync.py": ("pyproject.toml", "src/brigade/__init__.py"),
    "scripts/windows_affected_tests.py": ("scripts/ci_selection_core.py", "scripts/windows_test_weights.json"),
    "scripts/windows_pytest.py": ("tests/windows_pytest_allowlist.json",),
}


def test_listed_transitive_ci_inputs_are_protected():
    for script, inputs in TRANSITIVE_INPUTS.items():
        paths = {script, *inputs}
        assert all((ROOT / path).is_file() for path in paths), script
        missing = paths - set(workflow_guard.protected_paths(list(paths)))
        assert not missing, f"{script} uses unprotected paths: {', '.join(sorted(missing))}"
