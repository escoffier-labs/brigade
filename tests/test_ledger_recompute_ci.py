"""ci.yml wiring for the outcome ledger check and the dispatch trigger (#2589).

The workflow's own shell and Python steps run for real against temporary repositories.
The first live dispatch happens only after the trigger is on the default branch, so
these tests are the evidence for the dispatch branches in this change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.test_ci_workflow import ROOT, _workflow_bash_command, _workflow_job_section, _workflow_step_section

CI = (ROOT / ".github/workflows/ci.yml").read_text()
SCRIPT = ROOT / "scripts/ledger_recompute.py"
CHANGES_OUTPUTS = ("code_graph", "evidence_ledger", "notify", "fleet_hub_container", "hub")


def run_text(step: str) -> str:
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


# ---------------------------------------------------------------------------
# The ledger-recompute job
# ---------------------------------------------------------------------------


def test_ledger_recompute_is_a_mandatory_never_skipped_member_of_the_gate():
    gate = _workflow_job_section(CI, "ci-gate")
    needs = gate.split("    needs:\n", 1)[1].split("    runs-on:", 1)[0]
    assert "      - ledger-recompute\n" in needs
    mandatory = re.search(r"mandatory = \{(.*?)\n          \}", gate, re.DOTALL)
    docs_skips = re.search(r"docs_skips = \{(.*?)\n          \}", gate, re.DOTALL)
    assert mandatory and '"ledger-recompute"' in mandatory[1]
    assert docs_skips and "ledger-recompute" not in docs_skips[1]
    job = _workflow_job_section(CI, "ledger-recompute")
    assert "\n    if:" not in job and "\n    needs:" not in job


def test_ledger_recompute_asks_for_read_only_permissions():
    job = _workflow_job_section(CI, "ledger-recompute")
    permissions = job.split("    permissions:\n", 1)[1].split("    steps:", 1)[0]
    assert set(re.findall(r"^      ([a-z-]+): (\w+)$", permissions, re.MULTILINE)) == {
        ("contents", "read"),
        ("actions", "read"),
        ("pull-requests", "read"),
    }


def test_ledger_recompute_pins_its_actions_and_keeps_no_credentials():
    job = _workflow_job_section(CI, "ledger-recompute")
    uses = re.findall(r"uses: (\S+) # (v\d+)", job)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref) for ref, _tag in uses)
    assert len(re.findall(r"uses: ", job)) == len(uses)
    assert "persist-credentials: false" in job and "fetch-depth: 0" in job


def test_ledger_recompute_runs_main_s_logic_in_isolated_mode_and_never_the_proposals():
    step = _workflow_step_section(CI, "ledger-recompute", "Recompute the outcome ledger from trusted main")
    script = run_text(step)
    assert "'+refs/heads/main:refs/ci-trusted/main'" in script
    assert 'git archive "$trusted" scripts/ledger_recompute.py src/brigade' in script
    assert 'python3 -I -S "$tree/scripts/ledger_recompute.py"' in script
    assert "--main-ref refs/ci-trusted/main" in script
    # Nothing from the event is spliced into the shell, and the proposal's tree is never run.
    assert "${{" not in script
    invocations = [line for line in script.splitlines() if "python3" in line]
    assert invocations == ['python3 -I -S "$tree/scripts/ledger_recompute.py" \\']
    assert "pip install" not in script and "python3 scripts/" not in script


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_ledger_recompute_passes_while_trusted_main_has_no_recompute_logic(tmp_path):
    """The change that introduces the logic cannot be checked by logic main does not have yet."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "README.md").write_text("no ledger logic here\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    step = _workflow_step_section(CI, "ledger-recompute", "Recompute the outcome ledger from trusted main")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_WORKSPACE": str(work)}
    result = subprocess.run(
        _workflow_bash_command(run_text(step), "-e"), cwd=work, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "nothing to recompute against" in result.stdout


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return done.stdout.strip()


# ---------------------------------------------------------------------------
# workflow_dispatch: the scheduled writer dispatches CI on its own branch
# ---------------------------------------------------------------------------


def test_ci_accepts_workflow_dispatch_next_to_the_existing_triggers():
    triggers = CI.split("jobs:", 1)[0]
    assert re.search(r"^  workflow_dispatch:\n", triggers, re.MULTILINE)
    for existing in ("push:", "pull_request:", "merge_group:"):
        assert existing in triggers


def test_the_path_filters_skip_dispatch_and_every_changes_output_fails_closed():
    for step_id in ("filter", "hub_filter"):
        match = re.search(rf"if: (\$\{{\{{ .*? \}}\}})\n\s+id: {step_id}\b", CI)
        assert match, step_id
        assert "github.event_name != 'workflow_dispatch'" in match[1]
    changes = _workflow_job_section(CI, "changes")
    for name in CHANGES_OUTPUTS:
        line = re.search(rf"^      {name}: (.+)$", changes, re.MULTILINE)
        assert line and line[1].startswith("${{ steps.docs.outputs.uncertain != 'false' && 'true' ||"), name


@pytest.mark.skipif(os.name == "nt", reason="workflow steps run on Ubuntu")
def test_a_dispatched_run_classifies_as_uncertain_so_full_ci_runs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    head = git(repo, "rev-parse", "HEAD")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"ref": "refs/heads/ledger/auto"}))
    output = tmp_path / "output"
    step = _workflow_step_section(CI, "changes", "Classify authoritative event diff")
    program = step.split("python3 -I -S - <<'PY'\n", 1)[1].rsplit("\n          PY", 1)[0]
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_SHA": head,
        "GITHUB_OUTPUT": str(output),
        "RUNNER_TEMP": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-"],
        input=textwrap.dedent(program),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        "docs_only=false",
        "uncertain=true",
        "reason=trusted-classifier-unavailable",
    ]


@pytest.fixture()
def branch_clone(tmp_path):
    """A shallow checkout of a ledger branch cut from main, after main moved on twice."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "uploadpack.allowFilter", "true")
    shutil.copy(ROOT / "scripts/changelog_fragments.py", origin / "changelog_fragments.py")
    (origin / "scripts").mkdir()
    shutil.move(origin / "changelog_fragments.py", origin / "scripts/changelog_fragments.py")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    fork_point = git(origin, "rev-parse", "HEAD")
    git(origin, "switch", "-qc", "ledger/auto")
    (origin / "ledger.txt").write_text("ledger\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "ledger")
    git(origin, "switch", "-q", "main")
    for number in (2, 3):
        (origin / "README.md").write_text(f"{number}\n")
        git(origin, "commit", "-qam", f"main {number}")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", "--depth", "2", "--branch", "ledger/auto", f"file://{origin}", str(work))
    assert (work / ".git/shallow").exists()
    return work, fork_point


@pytest.mark.skipif(os.name == "nt", reason="lint shell step runs on Ubuntu")
def test_lint_allowlist_base_on_dispatch_is_the_merge_base_with_main(branch_clone, tmp_path):
    work, fork_point = branch_clone
    step = _workflow_step_section(CI, "lint", "Resolve allowlist base")
    output = tmp_path / "github_output"
    env = {
        **os.environ,
        "EVENT_NAME": "workflow_dispatch",
        "PR_HEAD_SHA": "",
        "EVENT_BASE_SHA": "",
        "GITHUB_OUTPUT": str(output),
    }
    result = subprocess.run(
        _workflow_bash_command(run_text(step), "-e", "-o", "pipefail"),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"sha={fork_point}\n"


@pytest.mark.skipif(os.name == "nt", reason="changelog shell step runs on Ubuntu")
def test_changelog_guard_on_dispatch_uses_the_merge_base_with_main(branch_clone):
    work, _fork_point = branch_clone
    step = _workflow_step_section(CI, "changelog-fragments", "Reject CHANGELOG.md edits outside a release commit")
    assert "if: ${{ github.event_name != 'push' }}" in step
    env = {**os.environ, "EVENT_NAME": "workflow_dispatch", "PR_HEAD_SHA": "", "EVENT_BASE_SHA": ""}
    result = subprocess.run(
        _workflow_bash_command(run_text(step), "-e", "-o", "pipefail"),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # The guard still fails a dispatched branch that edits the changelog outside a release commit.
    (work / "CHANGELOG.md").write_text("## [Unreleased]\n- edited\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "edit changelog")
    result = subprocess.run(
        _workflow_bash_command(run_text(step), "-e", "-o", "pipefail"),
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "do not edit the changelog" in result.stdout + result.stderr


def test_dispatch_does_not_change_which_jobs_the_pull_request_only_guards_admit():
    from tests.test_ci_workflow import _job_admitted

    for name in ("windows-affected-plan", "pr-tier-shadow", "shadow-plan", "shadow-selection"):
        assert not _job_admitted(CI, name, event="workflow_dispatch"), name
    for name in ("lint", "test-shards", "ci-gate", "ledger-recompute"):
        assert _job_admitted(CI, name, event="workflow_dispatch"), name


# ---------------------------------------------------------------------------
# scripts/ledger_recompute.py
# ---------------------------------------------------------------------------


@pytest.fixture()
def recompute_repo(tmp_path):
    """main with a ledger file, and HEAD on a branch whose change is chosen by the test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "outcome-ledger").mkdir()
    (repo / "outcome-ledger/skills.json").write_text("{}\n")
    (repo / "README.md").write_text("x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "update-ref", "refs/ci-trusted/main", "HEAD")
    git(repo, "switch", "-qc", "change")
    return repo


def run_recompute(
    repo: Path, tmp_path: Path, event: str, *, payload: dict | None = None, extra_env: dict | None = None
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    git_link = bin_dir / "git"
    if not git_link.exists():
        git_link.symlink_to(shutil.which("git"))
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(payload or {}))
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": "example-org/example-repo",
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "--repo", str(repo)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("event", ["pull_request", "merge_group", "workflow_dispatch"])
def test_a_change_that_leaves_the_ledger_alone_passes_at_once(recompute_repo, tmp_path, event):
    (recompute_repo / "README.md").write_text("changed\n")
    git(recompute_repo, "commit", "-qam", "docs")
    result = run_recompute(recompute_repo, tmp_path, event)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ledger is untouched" in result.stdout


def test_a_push_to_main_is_not_recomputed(recompute_repo, tmp_path):
    result = run_recompute(recompute_repo, tmp_path, "push")
    assert result.returncode == 0 and "merge group" in result.stdout


def test_an_unknown_event_fails(recompute_repo, tmp_path):
    result = run_recompute(recompute_repo, tmp_path, "schedule")
    assert result.returncode == 1 and "unsupported event" in result.stdout


def test_a_ledger_change_fails_closed_when_the_evidence_cannot_be_read(recompute_repo, tmp_path):
    (recompute_repo / "outcome-ledger/skills.json").write_text('{"edited": true}\n')
    git(recompute_repo, "commit", "-qam", "edit ledger")
    result = run_recompute(recompute_repo, tmp_path, "pull_request", payload={"pull_request": {"number": 5}})
    assert result.returncode == 1
    assert "cannot read trusted evidence" in result.stdout


def test_a_deleted_ledger_is_a_ledger_change(recompute_repo, tmp_path):
    git(recompute_repo, "rm", "-q", "outcome-ledger/skills.json")
    git(recompute_repo, "commit", "-qm", "delete ledger")
    result = run_recompute(recompute_repo, tmp_path, "pull_request")
    assert result.returncode == 1
    assert "ledger is untouched" not in result.stdout


def test_the_script_imports_only_the_tree_it_runs_from(recompute_repo, tmp_path):
    """A planted package in the checkout under test is never imported, even from the working directory."""
    decoy = recompute_repo / "brigade"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise SystemExit('the proposal ran')\n")
    (recompute_repo / "src/brigade").mkdir(parents=True)
    (recompute_repo / "src/brigade/__init__.py").write_text("raise SystemExit('the proposal ran')\n")
    (recompute_repo / "README.md").write_text("changed\n")
    git(recompute_repo, "add", ".")
    git(recompute_repo, "commit", "-qm", "planted")
    result = run_recompute(recompute_repo, tmp_path, "pull_request")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the proposal ran" not in result.stdout + result.stderr


def load_script():
    spec = importlib.util.spec_from_file_location("ledger_recompute_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_pull_request_number_comes_from_the_event_or_the_queue_ref():
    script = load_script()
    ref = "refs/heads/gh-readonly-queue/main/pr-123-" + "a" * 40
    assert script.event_context("pull_request", {"pull_request": {"number": 7}}, None, "o/r").pr_number == 7
    assert script.event_context("merge_group", {"merge_group": {"head_ref": ref}}, None, "o/r").pr_number == 123
    assert (
        script.event_context("merge_group", {"merge_group": {"head_ref": "refs/heads/main"}}, None, "o/r").pr_number
        is None
    )


def test_a_dispatched_run_finds_its_pull_request_by_branch(monkeypatch):
    script = load_script()
    monkeypatch.setenv("GITHUB_REF_NAME", "ledger/auto")
    seen: list[str] = []

    def gh(endpoint):
        seen.append(endpoint)
        return [{"number": 41}]

    assert script.event_context("workflow_dispatch", {}, gh, "o/r").pr_number == 41
    assert "head=o:ledger/auto" in seen[0]
    assert (
        script.event_context("workflow_dispatch", {}, lambda _e: [{"number": 1}, {"number": 2}], "o/r").pr_number
        is None
    )


def test_messages_cannot_smuggle_workflow_commands(capsys):
    load_script().say("::error::forged\nline two")
    out = capsys.readouterr().out
    assert out == "ledger-recompute: :error:forged?line two\n"
