"""CI evidence uses GitHub's check run for the committed tree."""

from __future__ import annotations

import json
import subprocess

import pytest

from brigade import cli, localio, work_cmd
from tests.work_cmd_test_helpers import _init_git_repo

_GITHUB_HOST = "github.com"


def _git(target, *args):
    return subprocess.check_output(["git", "-C", str(target), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    real_run = subprocess.run

    def reject_gh(argv, **kwargs):
        assert argv[0] != "gh", "tests must replace the GitHub CLI"
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_gh)
    _init_git_repo(tmp_path)
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "init",
    )
    _git(tmp_path, "remote", "add", "origin", f"https://{_GITHUB_HOST}/example-org/example-repo.git")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD"), _git(tmp_path, "rev-parse", "HEAD^{tree}")


def _check(head, *, run_id=101, name="ci-gate", status="completed", conclusion="success"):
    return {
        "id": run_id,
        "name": name,
        "head_sha": head,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://example.invalid/check",
    }


def _fake_gh(monkeypatch, head, *runs, name="ci-gate", error=None, stdout=None, later_runs=()):
    real_run = subprocess.run
    calls = []

    def run(argv, **kwargs):
        if argv[0] != "gh":
            return real_run(argv, **kwargs)
        calls.append(argv)
        assert argv[:3] == ["gh", "api", f"repos/example-org/example-repo/commits/{head}/check-runs"]
        assert f"check_name={name}" in argv
        assert kwargs.get("shell", False) is False
        assert kwargs["timeout"] > 0
        if isinstance(error, Exception):
            raise error
        if error:
            return subprocess.CompletedProcess(argv, 1, "", "private diagnostic")
        payload = [{"check_runs": runs}]
        if later_runs:
            payload.append({"check_runs": later_runs})
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload) if stdout is None else stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _ci(target, *options):
    return cli.main(["work", "verify", "ci", "--target", str(target), "--json", *options])


def _record(target, head):
    return target / ".brigade" / "work" / "verify-ci" / f"{head}.json"


def test_ci_help_exposes_check_name_and_json(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["work", "verify", "ci", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--check" in out and "ci-gate" in out
    assert "--json" in out


@pytest.mark.parametrize(
    ("status", "conclusion", "code", "state"),
    [
        ("completed", "success", 0, "passed"),
        ("completed", "failure", 1, "failed"),
        ("queued", None, 2, "pending"),
        ("in_progress", None, 2, "pending"),
    ],
)
def test_ci_records_check_state(checkout, monkeypatch, capsys, status, conclusion, code, state):
    target, head, tree = checkout
    calls = _fake_gh(monkeypatch, head, _check(head, status=status, conclusion=conclusion))
    assert _ci(target) == code
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema": "brigade.verify_ci.v1",
        "head_sha": head,
        "tree_id": tree,
        "repository": "example-org/example-repo",
        "check_name": "ci-gate",
        "check_run_id": 101,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://example.invalid/check",
        "state": state,
        "fetched_at": payload["fetched_at"],
    }
    assert localio.parse_iso_datetime(payload["fetched_at"]) is not None
    assert json.loads(_record(target, head).read_text()) == payload
    assert len(calls) == 1
    # A subsequent lookup works even when the checkout does not ignore Brigade files.
    assert _ci(target) == code


def test_ci_selects_newest_named_run_across_pages(checkout, monkeypatch, capsys):
    target, head, _ = checkout
    _fake_gh(
        monkeypatch,
        head,
        _check(head, run_id=102),
        _check(head, run_id=201, name="other"),
        later_runs=[_check(head, run_id=103, status="queued", conclusion=None)],
    )
    assert _ci(target) == 2
    assert json.loads(capsys.readouterr().out)["check_run_id"] == 103


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("absent", "No check run"),
        ("different_head", "different head"),
        ("missing_gh", "gh is unavailable"),
        ("gh_error", "gh could not read"),
        ("timeout", "timed out"),
        ("invalid_json", "unreadable"),
        ("invalid_shape", "unreadable"),
        ("no_remote", "GitHub origin"),
        ("other_remote", "GitHub origin"),
    ],
)
def test_ci_records_unknown(checkout, monkeypatch, capsys, case, reason):
    target, head, _ = checkout
    if case == "no_remote":
        _git(target, "remote", "remove", "origin")
    elif case == "other_remote":
        _git(target, "remote", "set-url", "origin", "https://example.invalid/example-org/example-repo")
    elif case == "different_head":
        _fake_gh(monkeypatch, head, _check("0" * len(head), run_id=102), _check(head))
    elif case == "absent":
        _fake_gh(monkeypatch, head, _check(head, name="other"))
    else:
        error = {"missing_gh": FileNotFoundError(), "gh_error": True, "timeout": subprocess.TimeoutExpired("gh", 1)}
        output = {"invalid_json": "not JSON", "invalid_shape": "{}"}
        _fake_gh(monkeypatch, head, error=error.get(case), stdout=output.get(case))

    assert _ci(target) == 2
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["state"] == "unknown"
    assert reason in payload["reason"]
    assert "private diagnostic" not in out
    assert json.loads(_record(target, head).read_text()) == payload


def test_ci_refuses_dirty_tree_without_query_or_record(checkout, capsys):
    target, head, _ = checkout
    (target / "untracked.txt").write_text("dirty")
    assert _ci(target) == 2
    assert "commit and push" in capsys.readouterr().err.lower()
    assert not _record(target, head).exists()


@pytest.mark.parametrize(
    "remote",
    [
        f"git@{_GITHUB_HOST}:example-org/example-repo.git",
        f"ssh://git@{_GITHUB_HOST}/example-org/example-repo.git",
    ],
)
def test_ci_accepts_ssh_origin_and_check_override(checkout, monkeypatch, capsys, remote):
    target, head, _ = checkout
    _git(target, "remote", "set-url", "origin", remote)
    _fake_gh(monkeypatch, head, _check(head, name="custom gate"), name="custom gate")
    assert _ci(target, "--check", "custom gate") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_name"] == "custom gate"
    assert payload["state"] == "passed"


@pytest.mark.parametrize("record", ["matching", "previous_head", "wrong_head", "wrong_tree", "missing"])
def test_closeout_reports_current_ci_fact_without_contacting_github(checkout, monkeypatch, capsys, record):
    target, head, tree = checkout
    if record == "previous_head":
        localio.write_json(_record(target, "0" * len(head)), {"state": "passed"})
    elif record != "missing":
        calls = _fake_gh(monkeypatch, head, _check(head))
        assert _ci(target) == 0
        assert len(calls) == 1
        path = _record(target, head)
        payload = json.loads(path.read_text())
        if record == "wrong_head":
            payload["head_sha"] = "0" * len(head)
        if record == "wrong_tree":
            payload["tree_id"] = "0" * len(tree)
        localio.write_json(path, payload)
        capsys.readouterr()

    assert work_cmd.start(target=target, title="CI evidence", force=False) == 0
    assert work_cmd.end(target=target, note="done", handoff=False) == 0
    capsys.readouterr()
    # This session lacks a local receipt. Even passed CI leaves that blocker in place.
    assert work_cmd.closeout(target=target, session_id="latest", json_output=True) == 1
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["verification"] is None
    state = "passed" if record == "matching" else "unknown"
    assert closeout["ci_verification"]["state"] == state
    assert closeout["ci_verification"]["head_sha"] == head
    markdown = (target / ".brigade" / "work" / "closeouts" / closeout["closeout_id"] / "closeout.md").read_text()
    assert "Local checks" in markdown
    assert f"CI on HEAD: {state}" in markdown
    if state == "unknown":
        assert closeout["ci_verification"]["hint"] == "brigade work verify ci"
        assert "brigade work verify ci" in markdown
    if record == "matching":
        assert work_cmd.closeout(target=target, session_id="latest") == 1
        console = capsys.readouterr().out
        assert "Local checks: no receipt" in console
        assert "CI on HEAD: passed" in console
    if record not in {"previous_head", "missing"}:
        assert len(calls) == 1
