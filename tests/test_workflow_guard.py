"""Exercise the workflow guard against pull request API responses."""

import json
from pathlib import Path
from posixpath import normpath
import re
import subprocess

import pytest

from scripts import workflow_guard
from tests.test_publish_workflow import _load_workflow_yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
APPROVAL = "workflow-change-approved"
HEAD = "a" * 40
RELEASE_SCOPE = "7abc3f44de68421afbb710eea4640dfbd236f5679add92851980ea2dcd1ad1e4"
STATUSES = f"repos/example/project/commits/{HEAD}/statuses?per_page=100"
CLI_ARGS = ["--repo", "example/project", "--pr", "42"]


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
        "scripts/ci_docs_only.py",
        "scripts/ledger_recompute.py",
        "scripts/pr_test_tiers.json",
        "scripts/pr_tier_selection.py",
        "scripts/workflow_guard.py",
        "scripts/workflow_approval_digest.py",
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
def pull_request_event(monkeypatch, tmp_path):
    event_path = tmp_path / "event.json"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))

    def write(*, base="main", head=HEAD, event_repo="example/project", base_repo="example/project", number=42, **extra):
        event_path.write_text(
            json.dumps(
                {
                    "number": number,
                    "action": "opened",
                    "label": {"name": APPROVAL},
                    "repository": {"full_name": event_repo, "default_branch": "main"},
                    "pull_request": {
                        "number": number,
                        "state": "open",
                        "updated_at": "2026-01-01T00:00:02Z",
                        "head": {"sha": head},
                        "base": {"ref": base, "sha": "b" * 40, "repo": {"full_name": base_repo}},
                    },
                    **extra,
                }
            )
        )

    write()
    monkeypatch.setenv("APPROVAL_RECORD_RESULT", "skipped")
    return write


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
        bases=("main", "main", "main"),
        base_shas=("b" * 40, "b" * 40, "b" * 40),
        history=None,
        history_after=None,
        live_repo="example/project",
        delete=(0, "HTTP/2.0 204 No Content"),
    ):
        pull = f"repos/example/project/pulls/{number}"
        calls = []
        label_history = [[approval_event()]] if history is None else history
        history_pages = iter((label_history, label_history if history_after is None else history_after))
        live_heads = iter((*heads, heads[-1]))
        live_bases = iter(bases)
        live_base_shas = iter(base_shas)

        def run(args, **kwargs):
            assert args[:2] == ["gh", "api"]
            assert kwargs.get("shell", False) is False
            calls.append(args[2:])
            if args[2] == "--include":
                return subprocess.CompletedProcess(args, delete[0], stdout=delete[1])
            if args[2] == f"{pull}/files?per_page=100":
                assert args[3:] == ["--paginate", "--slurp"]
                if fail:
                    raise subprocess.CalledProcessError(1, args, stderr="API unavailable")
                payload = pages
            elif args[2] == f"repos/example/project/issues/{number}/events?per_page=100":
                assert args[3:] == ["--paginate", "--slurp"]
                payload = next(history_pages)
            elif args[2] == f"repos/example/project/issues/{number}/labels?per_page=100":
                assert args[3:] == ["--paginate", "--slurp"]
                payload = [[], [{"name": label} for label in labels]]
            elif args[2].startswith("repos/example/project/statuses/"):
                assert args[3:5] == ["-X", "POST"]
                payload = {}
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
                    "number": number,
                    "state": "open",
                    "base": {"ref": next(live_bases), "sha": next(live_base_shas), "repo": {"full_name": live_repo}},
                    "labels": [{"name": label} for label in labels],
                }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload))

        monkeypatch.setattr(workflow_guard.subprocess, "run", run)
        return calls

    return install


def approval_status(
    state="success", *, login="github-actions[bot]", creator_type="Bot", creator_id=41898282, context=APPROVAL
):
    return {
        "context": context,
        "state": state,
        "description": "pull request 42 approval removed" if state == "failure" else "pull request 42 approved",
        "created_at": "2026-01-01T00:00:02Z",
        "creator": {"login": login, "type": creator_type, "id": creator_id},
    }


def approval_event(action="labeled", *, event_id=100, second=1):
    return {
        "id": event_id,
        "event": action,
        "created_at": f"2026-01-01T00:00:{second:02}Z",
        "label": {"name": APPROVAL},
    }


@pytest.mark.parametrize("base", ["release/0.32", "release/0.32-extra", "Release/0.32", "refs/heads/release/0.32"])
def test_release_base_is_admitted_exactly(fake_gh, pull_request_event, base):
    pull_request_event(base=base)
    fake_gh([[{"filename": "docs/guide.md"}]], bases=(base,) * 3)
    assert workflow_guard.main(CLI_ARGS) == (0 if base == "release/0.32" else 2)


@pytest.mark.parametrize("field", ["head", "event_repo", "base_repo"])
@pytest.mark.parametrize("value", [None, "wrong"])
def test_rejects_missing_or_mismatched_event_identity(fake_gh, pull_request_event, field, value):
    pull_request_event(**{field: value})
    fake_gh([[{"filename": "docs/guide.md"}]])
    assert workflow_guard.main(CLI_ARGS) == 2


def test_rejects_live_base_change_during_pagination(fake_gh):
    fake_gh(
        [[{"filename": "docs/guide.md"}]],
        base_shas=("b" * 40, "c" * 40, "c" * 40),
    )
    assert workflow_guard.main(CLI_ARGS) == 2


@pytest.mark.parametrize("action", ["base_ref_changed", "reopened", "unlabeled"])
def test_legacy_approval_cannot_survive_invalidation(fake_gh, action):
    fake_gh(
        [[{"filename": ".github/workflows/ci.yml"}]],
        statuses=[[approval_status()]],
        history=[[approval_event()], [approval_event(action, event_id=101, second=3)]],
    )
    assert workflow_guard.main(CLI_ARGS) == 1


@pytest.mark.parametrize(
    "payload", [{"action": "reopened"}, {"action": "edited", "changes": {"base": {"ref": {"from": "release/0.32"}}}}]
)
def test_lifecycle_event_rejects_prior_label_before_history_catches_up(fake_gh, pull_request_event, payload):
    pull_request_event(**payload)
    scope = workflow_guard.base_scope("main")
    status = dict(approval_status(), description=f"pull request 42 approved [{scope}:100]")
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], statuses=[[status]])
    assert workflow_guard.main(CLI_ARGS) == 1


@pytest.mark.parametrize(
    ("label_id", "include_release_scope", "exit_code"), [(102, True, 0), (100, True, 1), (102, False, 1)]
)
def test_new_status_requires_current_base_and_label_transition(
    fake_gh, pull_request_event, label_id, include_release_scope, exit_code
):
    base = "release/0.32"
    pull_request_event(base=base, action="edited", changes={"base": {"ref": {"from": "main"}}})
    scope = RELEASE_SCOPE if include_release_scope else workflow_guard.base_scope("main")
    status = dict(approval_status(), description=f"pull request 42 approved [{scope}:{label_id}]")
    fake_gh(
        [[{"filename": ".github/workflows/ci.yml"}]],
        bases=(base,) * 3,
        statuses=[[status, approval_status()]],
        history=[
            [approval_event(), approval_event("base_ref_changed", event_id=101, second=2)],
            [approval_event(event_id=102, second=3)],
        ],
    )
    assert workflow_guard.main(CLI_ARGS) == exit_code


def test_stable_live_base_advance_before_first_read_is_allowed(fake_gh):
    fake_gh([[{"filename": "docs/guide.md"}]], base_shas=("c" * 40,) * 3)
    assert workflow_guard.main(CLI_ARGS) == 0


@pytest.mark.parametrize("flag", [[], ["--record-approval"]])
def test_label_transition_during_status_read_or_write_cannot_pass(fake_gh, pull_request_event, flag):
    pull_request_event(action="labeled")
    calls = fake_gh(
        [[{"filename": ".github/workflows/ci.yml"}]],
        labels=[APPROVAL],
        statuses=[[approval_status()]],
        history_after=[[approval_event("unlabeled", event_id=101, second=3)]],
    )
    expected = 2 if flag else 1
    assert workflow_guard.main([*CLI_ARGS, *flag]) == expected
    assert all("POST" not in call for call in calls)


def test_audit_reader_recognizes_scoped_approval_and_skips_other_base(fake_gh):
    from scripts import workflow_approval_digest

    wrong_scope = workflow_guard.base_scope("main")
    statuses = [
        dict(approval_status("failure"), description=f"pull request 42 approval removed [{wrong_scope}:101]"),
        dict(approval_status(), description=f"pull request 42 approved [{RELEASE_SCOPE}:100]"),
    ]
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], statuses=[statuses])

    assert workflow_approval_digest.approval_record(
        "example/project", HEAD, 42, "release/0.32", allow_legacy=False
    ) == (
        "yes",
        "2026-01-01T00:00:02Z",
    )


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
        "pull request 042 approved",
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


def test_shared_head_approval_applies_to_one_pull_request(fake_gh, pull_request_event):
    files = [[{"filename": ".github/workflows/ci.yml"}]]
    statuses = [[approval_status()]]
    fake_gh(files, statuses=statuses)
    assert workflow_guard.main(CLI_ARGS) == 0
    pull_request_event(number=43)
    fake_gh(files, statuses=statuses, number=43)
    assert workflow_guard.main([*CLI_ARGS[:-1], "43"]) == 1


def test_retarget_after_nondefault_base_label_requires_approval(fake_gh, pull_request_event):
    pull_request_event(base="other-branch")
    fake_gh([[{"filename": ".github/workflows/ci.yml"}]], labels=[APPROVAL])
    assert workflow_guard.main(CLI_ARGS) == 2
    pull_request_event()
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
def test_rejects_nondefault_or_missing_base(fake_gh, capsys, pull_request_event, base):
    pull_request_event(base=base)
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
        "APPROVAL_RECORD_RESULT": "${{ needs.record-approval.result }}",
    }
    assert guard["steps"][-1]["if"] == "github.event_name == 'merge_group'"


def test_write_jobs_only_run_the_expected_scripts(guard_workflow):
    jobs = guard_workflow["jobs"]
    assert {name for name, job in jobs.items() if "write" in job.get("permissions", {}).values()} == {
        "clear-approval",
        "record-approval",
    }
    clear = jobs["clear-approval"]
    record = jobs["record-approval"]
    assert clear["if"] == (
        "github.event_name == 'pull_request_target' && github.run_attempt == '1' && github.event.action == 'synchronize'"
    )
    assert record["if"] == (
        "github.event_name == 'pull_request_target' && github.run_attempt == '1' && "
        "github.event.label.name == 'workflow-change-approved' && "
        "(github.event.action == 'labeled' || github.event.action == 'unlabeled')"
    )
    assert record["concurrency"] == {
        "group": "workflow-guard-record-approval-${{ github.event.number }}",
        "cancel-in-progress": False,
    }
    for job, permissions, mode in (
        (clear, {"contents": "read", "pull-requests": "write"}, "--clear-approval"),
        (record, {"contents": "read", "statuses": "write", "pull-requests": "read"}, "--record-approval"),
    ):
        assert job["runs-on"] == "ubuntu-latest"
        assert job["permissions"] == permissions
        checkout, step = job["steps"]
        assert set(checkout) == {"name", "uses", "with"}
        assert set(step) == {"name", "env", "run"}
        assert step["env"] == {
            "GH_TOKEN": "${{ github.token }}",
            "PR_NUMBER": "${{ github.event.number }}",
        }
        assert step["run"] == f'python3 scripts/workflow_guard.py --repo "$GITHUB_REPOSITORY" --pr "$PR_NUMBER" {mode}'


@pytest.mark.parametrize(
    ("status_line", "api_exit", "job_exit"),
    [
        ("HTTP/2.0 204 No Content", 0, 0),
        ("HTTP/2.0 404 Not Found", 1, 0),
        ("HTTP/2.0 403 Forbidden", 1, 2),
        ("", 1, 2),
    ],
)
def test_clear_approval_uses_one_delete_and_only_accepts_success_or_404(
    fake_gh, pull_request_event, status_line, api_exit, job_exit
):
    pull_request_event(action="synchronize", base="release/0.32")
    calls = fake_gh([[]], bases=("release/0.32",) * 3, delete=(api_exit, status_line))

    assert workflow_guard.main([*CLI_ARGS, "--clear-approval"]) == job_exit
    assert calls == [
        ["repos/example/project/pulls/42"],
        ["--include", "--silent", "-X", "DELETE", "repos/example/project/issues/42/labels/workflow-change-approved"],
    ]


@pytest.mark.parametrize("flag", ["--record-approval", "--clear-approval"])
@pytest.mark.parametrize("live", [{"heads": ("c" * 40,)}, {"bases": ("release/0.32",)}, {"live_repo": "example/other"}])
def test_write_jobs_reject_stale_or_mismatched_live_snapshot(fake_gh, pull_request_event, flag, live):
    pull_request_event(action="labeled" if flag == "--record-approval" else "synchronize")
    calls = fake_gh([[]], **live)

    assert workflow_guard.main([*CLI_ARGS, flag]) == 2
    assert calls == [["repos/example/project/pulls/42"]]


@pytest.mark.parametrize(
    ("action", "latest", "present", "exit_code", "state"),
    [
        ("labeled", "labeled", True, 0, "success"),
        ("unlabeled", "labeled", True, 0, None),
        ("labeled", "unlabeled", False, 0, "failure"),
        ("unlabeled", "unlabeled", False, 0, "failure"),
        ("labeled", "labeled", False, 2, None),
    ],
)
def test_record_approval_keeps_latest_label_state(
    fake_gh, pull_request_event, action, latest, present, exit_code, state
):
    base = "release/0.32"
    pull_request_event(base=base, action=action)
    calls = fake_gh(
        [[]],
        bases=(base,) * 3,
        labels=[APPROVAL] if present else [],
        history=[[approval_event()], [approval_event(latest, event_id=101, second=3)]],
    )

    assert workflow_guard.main([*CLI_ARGS, "--record-approval"]) == exit_code
    posts = [call for call in calls if "POST" in call]
    if state is None:
        assert posts == []
    else:
        description = "approved" if state == "success" else "approval removed"
        assert posts == [
            [
                f"repos/example/project/statuses/{HEAD}",
                "-X",
                "POST",
                "-f",
                f"context={APPROVAL}",
                "-f",
                f"state={state}",
                "-f",
                f"description=pull request 42 {description} [{RELEASE_SCOPE}:101]",
            ]
        ]


def test_workflow_only_checks_out_base_with_pinned_actions(guard_workflow):
    for name, job in guard_workflow["jobs"].items():
        assert "uses" not in job
        (checkout,) = [step for step in job["steps"] if "uses" in step]
        assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
        assert checkout["with"] == {"persist-credentials": False}
        if name == "workflow-guard":
            assert checkout["if"] == "github.event_name == 'pull_request_target'"
        else:
            assert "github.event_name == 'pull_request_target'" in job["if"]


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
    trusted_inputs = {"github.token", "github.event.number", "needs.record-approval.result"}
    for path, value in workflow_strings(guard_workflow):
        if "run" in path:
            assert "${{" not in value, path
        if "env" in path or "with" in path:
            expressions = re.findall(r"\$\{\{\s*(.*?)\s*\}\}", value, re.DOTALL)
            assert set(expressions) <= trusted_inputs, path
        assert "github.event.pull_request.head" not in value, path


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


def _extract_workflow_paths(value, directory="."):
    paths = []
    for match in WORKFLOW_PATH.finditer(value):
        raw_path = match["path"].replace("\\", "/").rstrip(".,;:)'\"")
        root = "." if match["workspace"] or directory == "${{ github.workspace }}" else directory
        path = normpath(f"{root}/{raw_path}")
        if path.split("/", 1)[0] in {"scripts", "tests", "registry", "npm"}:
            is_directory = raw_path.endswith("/") or (ROOT / path).is_dir()
            paths.append(path + "/" if is_directory else path)
    return paths


def test_workflow_path_extraction_strips_trailing_sentence_punctuation():
    assert _extract_workflow_paths("no scripts/ledger_recompute.py. The check") == ["scripts/ledger_recompute.py"]


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
            paths.update(_extract_workflow_paths(value, directory))

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
