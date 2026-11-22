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


def _bootstrap_step_result(tmp_path: Path, *, main_files: dict[str, str], proposal_files: dict[str, str]):
    """Run the workflow step against a main without the script, and a proposal that adds ``proposal_files``."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    for name, text in {"README.md": "x\n", **main_files}.items():
        (origin / name).parent.mkdir(parents=True, exist_ok=True)
        (origin / name).write_text(text)
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "switch", "-qc", "change")
    for name, text in proposal_files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    git(work, "add", ".")
    git(work, "commit", "-qm", "change")
    step = _workflow_step_section(CI, "ledger-recompute", "Recompute the outcome ledger from trusted main")
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_WORKSPACE": str(work)}
    return subprocess.run(
        _workflow_bash_command(run_text(step), "-e"), cwd=work, env=env, capture_output=True, text=True
    )


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_the_bootstrap_pass_lets_the_change_that_introduces_the_script_through_when_it_carries_no_ledger(tmp_path):
    result = _bootstrap_step_result(
        tmp_path, main_files={}, proposal_files={"scripts/ledger_recompute.py": "# new\n", "docs/a.md": "x\n"}
    )
    assert result.returncode == 0, result.stderr
    assert "nothing to recompute against" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_the_bootstrap_pass_never_waves_through_a_proposal_that_carries_a_ledger(tmp_path):
    result = _bootstrap_step_result(
        tmp_path,
        main_files={},
        proposal_files={"scripts/ledger_recompute.py": "# new\n", "outcome-ledger/skills.json": "{}\n"},
    )
    assert result.returncode != 0
    assert "carries an outcome ledger" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_a_ledger_alone_cannot_use_the_bootstrap_pass_after_the_script_was_removed_from_main(tmp_path):
    result = _bootstrap_step_result(tmp_path, main_files={}, proposal_files={"outcome-ledger/skills.json": "{}\n"})
    assert result.returncode != 0
    assert "carries an outcome ledger" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_a_ledger_on_trusted_main_without_the_script_is_a_removed_check(tmp_path):
    result = _bootstrap_step_result(
        tmp_path, main_files={"outcome-ledger/skills.json": "{}\n"}, proposal_files={"docs/a.md": "x\n"}
    )
    assert result.returncode != 0
    assert "check was removed" in result.stderr


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


SCRIPT_RUNS_ON_UBUNTU = pytest.mark.skipif(os.name == "nt", reason="the recompute job runs on Ubuntu")


@SCRIPT_RUNS_ON_UBUNTU
@pytest.mark.parametrize("event", ["pull_request", "merge_group", "workflow_dispatch"])
def test_a_change_that_leaves_the_ledger_alone_passes_at_once(recompute_repo, tmp_path, event):
    (recompute_repo / "README.md").write_text("changed\n")
    git(recompute_repo, "commit", "-qam", "docs")
    result = run_recompute(recompute_repo, tmp_path, event)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ledger is untouched" in result.stdout


def push_event(repo: Path, before: str, after: str) -> dict:
    return {"before": before, "after": after, "ref": "refs/heads/main"}


@SCRIPT_RUNS_ON_UBUNTU
def test_a_push_that_leaves_the_ledger_alone_passes(recompute_repo, tmp_path):
    before = git(recompute_repo, "rev-parse", "HEAD")
    (recompute_repo / "README.md").write_text("changed\n")
    git(recompute_repo, "commit", "-qam", "docs")
    after = git(recompute_repo, "rev-parse", "HEAD")
    result = run_recompute(recompute_repo, tmp_path, "push", payload=push_event(recompute_repo, before, after))
    assert result.returncode == 0 and "ledger is untouched" in result.stdout, result.stdout


def first_ledger_push(repo: Path, *, with_script: bool) -> tuple[str, str]:
    """before: a main with no ledger, and the recompute script when ``with_script``. after: ``{}`` as the first ledger."""
    git(repo, "switch", "-q", "main")
    git(repo, "rm", "-q", "outcome-ledger/skills.json")
    if with_script:
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts/ledger_recompute.py").write_text("# trusted\n")
        git(repo, "add", ".")
    git(repo, "commit", "-qm", "no ledger")
    before = git(repo, "rev-parse", "HEAD")
    (repo / "outcome-ledger").mkdir(exist_ok=True)
    (repo / "outcome-ledger/skills.json").write_text("{}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "first ledger")
    return before, git(repo, "rev-parse", "HEAD")


@SCRIPT_RUNS_ON_UBUNTU
def test_the_push_that_introduces_the_script_passes_because_nothing_could_check_it(recompute_repo, tmp_path):
    before, after = first_ledger_push(recompute_repo, with_script=False)
    result = run_recompute(recompute_repo, tmp_path, "push", payload=push_event(recompute_repo, before, after))
    assert result.returncode == 0 and "nothing could check" in result.stdout, result.stdout


@SCRIPT_RUNS_ON_UBUNTU
def test_a_first_ledger_pushed_after_the_script_is_on_main_is_not_waved_through(recompute_repo, tmp_path):
    before, after = first_ledger_push(recompute_repo, with_script=True)
    result = run_recompute(recompute_repo, tmp_path, "push", payload=push_event(recompute_repo, before, after))
    assert result.returncode == 1, result.stdout
    assert "nothing could check" not in result.stdout


class RecomputeRun:
    """The script's push check run in this process, with the Actions API and the clock replaced."""

    def __init__(self, world, monkeypatch, tmp_path, capsys) -> None:
        from tests.test_outcome_ledger import NOW, SLUG

        self.world, self.capsys, self.monkeypatch, self.tmp_path = world, capsys, monkeypatch, tmp_path
        self.module = load_script()
        real_verify = self.module.ledger.verify_ledger
        monkeypatch.setattr(
            self.module.ledger, "verify_ledger", lambda ev, **kwargs: real_verify(ev, **{**kwargs, "now": NOW})
        )
        monkeypatch.setattr(
            self.module.ci_attestation,
            "default_gh_api",
            lambda endpoint: [] if "/commits/" in endpoint else world.gh(endpoint),
        )
        monkeypatch.setenv("GITHUB_REPOSITORY", SLUG)

    def push(self, before: str, after: str) -> tuple[int, str]:
        import argparse

        event = self.tmp_path / "push.json"
        event.write_text(json.dumps({"before": before, "after": after}))
        self.monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
        args = argparse.Namespace(repo=str(self.world.root))
        code = self.module._check_push(args)
        return code, self.capsys.readouterr().out


@pytest.fixture()
def recompute_run(tmp_path, monkeypatch, capsys):
    from tests.test_outcome_ledger import World

    world = World(tmp_path / "world")
    return RecomputeRun(world, monkeypatch, tmp_path, capsys)


def genesis_push(run: RecomputeRun, ledger_bytes: bytes | None = None) -> tuple[str, str]:
    """main gets the recompute script, then a first ledger. Returns (before, after) for the second push."""
    from tests.test_outcome_ledger import T

    world = run.world
    world.commit({"scripts/ledger_recompute.py": "# trusted\n"}, T % (21, "00:01:00Z"), branch="main")
    before = world.git("rev-parse", "HEAD")
    raw = ledger_bytes or world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:00:00Z")
    )
    after = world.commit({"outcome-ledger/skills.json": raw.decode()}, T % (21, "00:05:00Z"), branch="main")
    return before, after


def test_a_correct_first_ledger_is_accepted_once_the_script_is_on_main(recompute_run):
    before, after = genesis_push(recompute_run)
    code, out = recompute_run.push(before, after)
    assert code == 0 and "matches the recomputed ledger" in out, out


def test_an_empty_object_is_not_accepted_as_the_first_ledger_once_the_script_is_on_main(recompute_run):
    before, after = genesis_push(recompute_run, b"{}\n")
    code, out = recompute_run.push(before, after)
    assert code == 1 and "schema must be" in out, out


def test_a_hand_made_promoted_first_ledger_is_rejected_once_the_script_is_on_main(recompute_run):
    from brigade import outcome_ledger as L
    from tests.test_outcome_ledger import T

    world = recompute_run.world
    honest = world.build(
        prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),), cutoff=T % (21, "00:00:00Z")
    )
    forged = json.loads(honest)
    subject = forged["subjects"]["skill:note"]
    subject["status"], subject["band"] = "promoted", "promoted"
    subject["decision"] = {**subject["decision"], "action": "install", "code": "attested"}
    before, after = genesis_push(recompute_run, L.canonical_bytes(forged))
    code, out = recompute_run.push(before, after)
    assert code == 1 and "differs from the recomputed ledger" in out, out


def test_a_push_without_a_ledger_on_either_side_passes_once_the_script_is_on_main(recompute_run):
    from tests.test_outcome_ledger import T

    world = recompute_run.world
    world.commit({"scripts/ledger_recompute.py": "# trusted\n"}, T % (21, "00:01:00Z"), branch="main")
    before = world.git("rev-parse", "HEAD")
    after = world.commit({"README.md": "changed\n"}, T % (21, "00:05:00Z"), branch="main")
    code, out = recompute_run.push(before, after)
    assert code == 0 and "untouched" in out, out


@SCRIPT_RUNS_ON_UBUNTU
def test_a_push_that_changes_the_ledger_is_verified_and_fails_closed(recompute_repo, tmp_path):
    """A ledger that lands on main without a passing check is not waved through."""
    before = git(recompute_repo, "rev-parse", "HEAD")
    (recompute_repo / "outcome-ledger/skills.json").write_text('{"edited": true}\n')
    git(recompute_repo, "commit", "-qam", "edit ledger")
    after = git(recompute_repo, "rev-parse", "HEAD")
    result = run_recompute(recompute_repo, tmp_path, "push", payload=push_event(recompute_repo, before, after))
    assert result.returncode == 1, result.stdout
    assert "cannot read trusted evidence" in result.stdout


@SCRIPT_RUNS_ON_UBUNTU
def test_a_push_that_deletes_the_ledger_fails(recompute_repo, tmp_path):
    before = git(recompute_repo, "rev-parse", "HEAD")
    git(recompute_repo, "rm", "-q", "outcome-ledger/skills.json")
    git(recompute_repo, "commit", "-qm", "delete ledger")
    after = git(recompute_repo, "rev-parse", "HEAD")
    result = run_recompute(recompute_repo, tmp_path, "push", payload=push_event(recompute_repo, before, after))
    assert result.returncode == 1 and "was removed" in result.stdout


@SCRIPT_RUNS_ON_UBUNTU
@pytest.mark.parametrize("payload", [{}, {"before": "x", "after": "y"}, {"before": "0" * 40}])
def test_a_push_event_without_readable_commits_fails_closed(recompute_repo, tmp_path, payload):
    result = run_recompute(recompute_repo, tmp_path, "push", payload=payload)
    assert result.returncode == 1, result.stdout


@SCRIPT_RUNS_ON_UBUNTU
def test_an_unknown_event_fails(recompute_repo, tmp_path):
    result = run_recompute(recompute_repo, tmp_path, "schedule")
    assert result.returncode == 1 and "unsupported event" in result.stdout


@SCRIPT_RUNS_ON_UBUNTU
def test_a_ledger_change_fails_closed_when_the_evidence_cannot_be_read(recompute_repo, tmp_path):
    (recompute_repo / "outcome-ledger/skills.json").write_text('{"edited": true}\n')
    git(recompute_repo, "commit", "-qam", "edit ledger")
    result = run_recompute(recompute_repo, tmp_path, "pull_request", payload={"pull_request": {"number": 5}})
    assert result.returncode == 1
    assert "cannot read trusted evidence" in result.stdout


@SCRIPT_RUNS_ON_UBUNTU
def test_a_deleted_ledger_is_a_ledger_change(recompute_repo, tmp_path):
    git(recompute_repo, "rm", "-q", "outcome-ledger/skills.json")
    git(recompute_repo, "commit", "-qm", "delete ledger")
    result = run_recompute(recompute_repo, tmp_path, "pull_request")
    assert result.returncode == 1
    assert "ledger is untouched" not in result.stdout


@SCRIPT_RUNS_ON_UBUNTU
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


def test_a_dispatched_run_ignores_a_pull_list_entry_that_is_not_an_object(monkeypatch):
    script = load_script()
    monkeypatch.setenv("GITHUB_REF_NAME", "ledger/auto")
    assert script.event_context("workflow_dispatch", {}, lambda _e: ["oops"], "o/r").pr_number is None
    assert script.event_context("workflow_dispatch", {}, lambda _e: [{"number": "7"}], "o/r").pr_number is None


def test_a_pushed_ledger_finds_the_pull_request_that_merged_it():
    script = load_script()
    after = "c" * 40
    seen: list[str] = []

    def gh(endpoint):
        seen.append(endpoint)
        return [{"number": 9, "merge_commit_sha": "d" * 40}, {"number": 12, "merge_commit_sha": after}, "oops"]

    assert script.event_context("push", {"after": after}, gh, "o/r").pr_number == 12
    assert seen == [f"repos/o/r/commits/{after}/pulls"]
    both = lambda _e: [{"number": 1, "merge_commit_sha": after}, {"number": 2, "merge_commit_sha": after}]  # noqa: E731
    assert script.event_context("push", {"after": after}, both, "o/r").pr_number is None
    assert script.event_context("push", {"after": "nope"}, None, "o/r").pr_number is None


def test_messages_cannot_smuggle_workflow_commands(capsys):
    load_script().say("::error::forged\nline two")
    out = capsys.readouterr().out
    assert out == "ledger-recompute: :error:forged?line two\n"


# ---------------------------------------------------------------------------
# The trusted-tree path, end to end through the job's own shell step
# ---------------------------------------------------------------------------

STEP = "Recompute the outcome ledger from trusted main"
FAKE_GH = """#!/usr/bin/env python3
import json, sys
endpoint = sys.argv[-1]
if endpoint == "repos/example-org/example-repo":
    print(json.dumps({"id": 4242}))
elif "/actions/workflows/" in endpoint and "/runs?" in endpoint:
    print(json.dumps({"total_count": 0, "workflow_runs": []}))
else:
    sys.stderr.write("fake gh: unexpected endpoint " + endpoint + "\\n")
    sys.exit(1)
"""


def run_step(work: Path, tmp_path: Path, *, event: dict, bin_dir: Path | None = None):
    step = _workflow_step_section(CI, "ledger-recompute", STEP)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event))
    path = os.environ["PATH"] if bin_dir is None else f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    env = {
        **os.environ,
        "PATH": path,
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_WORKSPACE": str(work),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": "example-org/example-repo",
        "GH_TOKEN": "not-a-real-token",
    }
    (tmp_path / "runner").mkdir(exist_ok=True)
    return subprocess.run(
        _workflow_bash_command(run_text(step), "-e"), cwd=work, env=env, capture_output=True, text=True
    )


@pytest.fixture()
def trusted_tree_world(tmp_path):
    """main has the recompute logic and a genesis ledger. The fake ``gh`` lives only on a test-local PATH."""
    from brigade import outcome_ledger as ledger, shipped_history
    from brigade.outcome_ledger_git import Repo, bundle_fingerprint

    skill_dir = "src/brigade/templates/skills/note"
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    shutil.copytree(
        ROOT / "src/brigade",
        origin / "src/brigade",
        ignore=lambda _dir, names: [n for n in names if n == "__pycache__" or n.endswith(".pyc")],
    )
    for relative in (
        "scripts/ledger_recompute.py",
        "verify/manifests/skill-note.json",
        "src/brigade/templates/shipped-file-history.json",
    ):
        (origin / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, origin / relative)
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    c0 = git(origin, "rev-parse", "HEAD")
    repo = Repo(origin)
    files = repo.snapshot(c0, skill_dir)
    row = {
        "basis": "release",
        "commit": c0,
        "fingerprint": bundle_fingerprint(files),
        "skill_md_sha256": shipped_history.skill_digest(files["SKILL.md"]),
    }
    definitions = {"skill:note": ledger.SubjectDef("skill:note", "skill-note", f"{skill_dir}/SKILL.md", c0, (row,))}
    cutoff = repo.commit_time(c0)
    evidence = ledger.Evidence(
        repo=repo, gh=lambda _endpoint: {"id": 4242}, slug="example-org/example-repo", repo_id=4242
    )

    def build(prev_raw: bytes | None) -> bytes:
        prev = ledger.parse_ledger(prev_raw) if prev_raw else None
        return ledger.canonical_bytes(
            ledger.build_ledger(
                evidence,
                prev=prev,
                prev_sha256=ledger.sha256_hex(prev_raw) if prev_raw else ledger.GENESIS_PREV,
                definitions=definitions,
                covers_sha=c0,
                cutoff_at=cutoff,
            )
        )

    genesis = build(None)
    (origin / "outcome-ledger").mkdir()
    (origin / "outcome-ledger/skills.json").write_bytes(genesis)
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "genesis ledger")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "switch", "-qc", "proposal")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(0o755)
    return {"work": work, "bin": bin_dir, "next_ledger": build(genesis), "genesis": genesis}


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_a_correct_ledger_change_passes_through_the_trusted_tree_path(trusted_tree_world, tmp_path):
    work = trusted_tree_world["work"]
    (work / "outcome-ledger/skills.json").write_bytes(trusted_tree_world["next_ledger"])
    git(work, "commit", "-qam", "rebuilt ledger")
    result = run_step(work, tmp_path, event={"pull_request": {"number": 7}}, bin_dir=trusted_tree_world["bin"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "the ledger matches the recomputed ledger" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_a_hand_edited_ledger_fails_through_the_trusted_tree_path_even_when_the_proposal_edits_the_script(
    trusted_tree_world, tmp_path
):
    work = trusted_tree_world["work"]
    edited = json.loads(trusted_tree_world["next_ledger"])
    edited["subjects"]["skill:note"]["decision"]["reason"] = "forced"
    (work / "outcome-ledger/skills.json").write_text(json.dumps(edited, sort_keys=True, indent=2) + "\n")
    (work / "scripts/ledger_recompute.py").write_text("print('the proposal ran')\n")
    git(work, "commit", "-qam", "forged ledger and a script that says yes")
    result = run_step(work, tmp_path, event={"pull_request": {"number": 7}}, bin_dir=trusted_tree_world["bin"])
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "the proposal ran" not in output
    assert "differs from the recomputed ledger" in output
    assert "may not touch scripts/ledger_recompute.py" in output


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_the_job_without_a_gh_binary_fails_closed(trusted_tree_world, tmp_path):
    work = trusted_tree_world["work"]
    (work / "outcome-ledger/skills.json").write_bytes(trusted_tree_world["next_ledger"])
    git(work, "commit", "-qam", "rebuilt ledger")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    # A PATH holding only git, python3 and the shell, so no real gh is found.
    for name in ("git", "python3", "bash", "tar", "rm", "mkdir", "cat", "env"):
        found = shutil.which(name)
        if found and not (empty / name).exists():
            (empty / name).symlink_to(found)
    step = _workflow_step_section(CI, "ledger-recompute", STEP)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"pull_request": {"number": 7}}))
    env = {
        "PATH": str(empty),
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_WORKSPACE": str(work),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_REPOSITORY": "example-org/example-repo",
    }
    result = subprocess.run(
        _workflow_bash_command(run_text(step), "-e"), cwd=work, env=env, capture_output=True, text=True
    )
    assert result.returncode == 1 and "cannot read trusted evidence" in result.stdout, result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="workflow shell steps run on Ubuntu")
def test_ledger_recompute_fails_when_main_has_a_ledger_but_lost_the_recompute_logic(tmp_path):
    """The bootstrap pass covers the change that adds the logic. It is not a permanent off switch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "outcome-ledger").mkdir()
    (origin / "outcome-ledger/skills.json").write_text("{}\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "ledger without logic")
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    result = run_step(work, tmp_path, event={})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "no scripts/ledger_recompute.py" in result.stderr
    assert "nothing to recompute against" not in result.stdout


def test_the_job_names_the_bootstrap_exception_only_for_a_main_without_a_ledger():
    script = run_text(_workflow_step_section(CI, "ledger-recompute", STEP))
    gate = script.index('git cat-file -e "$trusted:scripts/ledger_recompute.py"')
    assert script.index('git cat-file -e "$trusted:outcome-ledger/skills.json"') > gate
    assert script.index("exit 1") < script.index("nothing to recompute against")


def test_a_merge_group_checks_the_ledger_commit_with_its_own_pr_number(recompute_run):
    run = recompute_run
    from tests.test_outcome_ledger import T

    world = run.world
    raw = world.build(prev_raw=None, covers=world.c0, approved=(world.approved_row(world.c0),))
    before, ledger_commit = genesis_push(run, raw)
    after = world.commit({"README.md": "unrelated queued change\n"}, T % (24, "00:20:00Z"))
    ledger_ref = f"refs/remotes/origin/gh-readonly-queue/main/pr-23-{before}"
    world.git("update-ref", ledger_ref, ledger_commit)
    world.git("update-ref", "refs/ci-trusted/main", before)
    payload = {
        "merge_group": {"head_sha": after, "head_ref": f"refs/heads/gh-readonly-queue/main/pr-24-{ledger_commit}"}
    }
    event = run.tmp_path / "queue.json"
    event.write_text(json.dumps(payload))
    run.monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    run.monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    result = run.module.main(["--repo", str(world.root)])
    assert result == 0, run.capsys.readouterr().out
    assert run.module.queue_ledger_pr(world.repo, payload, ledger_commit, after) == 23


def test_queue_pr_mapping_requires_the_ledger_commit_and_its_actual_parent(recompute_run):
    run = recompute_run
    from tests.test_outcome_ledger import T

    before, ledger_commit = genesis_push(run)
    world = run.world
    after = world.commit({"README.md": "unrelated queued change\n"}, T % (21, "01:00:00Z"))
    wrong = "b" * 40
    world.git("update-ref", f"refs/remotes/origin/gh-readonly-queue/main/pr-23-{wrong}", ledger_commit)
    world.git("update-ref", f"refs/remotes/origin/gh-readonly-queue/main/pr-25-{before}", after)
    payload = {
        "merge_group": {"head_sha": after, "head_ref": f"refs/heads/gh-readonly-queue/main/pr-24-{ledger_commit}"}
    }
    assert run.module.queue_ledger_pr(world.repo, payload, ledger_commit, after) is None


def test_batched_push_looks_up_the_ledger_commit_instead_of_the_tip(recompute_run, monkeypatch):
    run = recompute_run
    from tests.test_outcome_ledger import T

    before, ledger_commit = genesis_push(run)
    after = run.world.commit({"README.md": "other landed pull request\n"}, T % (21, "01:00:00Z"))
    seen = []

    def context(event_name, payload, gh, slug):
        seen.append(payload["after"])
        return run.module.ledger.EventContext(event_name, 7)

    monkeypatch.setattr(run.module, "event_context", context)
    code, out = run.push(before, after)
    assert code == 0, out
    assert seen == [ledger_commit]
