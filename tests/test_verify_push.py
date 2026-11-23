"""Check pushed branch tips against worktree-local verification receipts."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest

from brigade import localio
from brigade.cli import main
from brigade.work_cmd import verify_gate, verify_push, verify_required, verify_runtime

from tests.test_verify_gate import DECLARATION, PREFIX, SELECTOR, _declaration, _git

EXTRA_SELECTOR = "tests/test_other.py"


def _commit(target, message="fixture change"):
    _git(target, "add", ".")
    _git(target, "commit", "--allow-empty", "-m", message)
    return _git(target, "rev-parse", "HEAD")


def _declare(target, *, coverage=False, malformed=False):
    path = target / DECLARATION
    path.parent.mkdir(exist_ok=True)
    data = json.loads(_declaration())
    data["needs_plan_coverage"] = coverage
    path.write_text("{" if malformed else json.dumps(data), encoding="utf-8")


@pytest.fixture
def push_repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "remote", "add", "origin", str(tmp_path / "unused-remote"))
    (tmp_path / ".gitignore").write_text(".brigade/\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("value = 1\n", encoding="utf-8")
    base = _commit(tmp_path)
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", base)
    return tmp_path


def _policy(target, **kwargs):
    _declare(target, **kwargs)
    base = _commit(target, "add required check")
    _git(target, "update-ref", "refs/remotes/origin/main", base)
    return base


def _tree(target):
    return _git(target, "rev-parse", "HEAD^{tree}")


def _receipt(target, tree, *, second=1, status="completed", plan=None):
    receipt = {
        "status": status,
        "started_at": f"2026-01-01T12:00:{second:02d}+00:00",
        "tree_stable": True,
        "tree_fingerprint": tree,
        "commands": [
            {
                "argv": [*PREFIX, SELECTOR],
                "test_outcomes": {"passed": 1, "failed": 0, "errors": 0, "no_tests_ran": False},
            }
        ],
    }
    if plan is None:
        verify_gate.stamp_gating(target, receipt)
    else:
        policy = verify_gate._load_policy(target)
        eligible = not plan["missing_files"]
        receipt["gating"] = policy.stamp(eligible, "passed_required_check" if eligible else "plan_files_missing")
        receipt["gating"]["plan"] = plan
    path = target / ".brigade" / "work" / "verify-runs" / f"20260101-1200{second:02d}-work-verify-fixture"
    path.mkdir(parents=True)
    (path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


def _check(target, capsys, *options):
    code = main(["work", "verify", "push-check", "--target", str(target), "--json", *options])
    output = capsys.readouterr()
    return code, json.loads(output.out)["refs"]


def _single(target, capsys):
    code, [result] = _check(target, capsys, "--ref", "HEAD")
    return code, result


def _line(commit, ref, remote=None):
    return f"refs/heads/local {commit} {ref} {remote or '0' * len(commit)}"


def test_verify_then_commit(push_repo, capsys):
    _policy(push_repo)
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    verified_tree = localio.tree_fingerprint(push_repo)
    _receipt(push_repo, verified_tree)
    _commit(push_repo)
    assert _tree(push_repo) == verified_tree
    assert _single(push_repo, capsys) == (0, {"ref": "HEAD", "status": "ok", "reason": None, "remedy": None})


def test_commit_changes_verified_tree(push_repo, capsys):
    _policy(push_repo)
    _receipt(push_repo, _tree(push_repo))
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    _commit(push_repo)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "missing_receipt")
    assert "tests/test_example.py" in result["remedy"]
    assert "brigade work verify required --worktree" in result["remedy"]


@pytest.mark.parametrize("committed_receipt", [True, False])
def test_dirty_worktree(push_repo, capsys, committed_receipt):
    _policy(push_repo)
    committed = _tree(push_repo)
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    _receipt(push_repo, committed if committed_receipt else localio.tree_fingerprint(push_repo))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((0, "ok") if committed_receipt else (1, "missing_receipt"))


@pytest.mark.parametrize("status", ["failed", "busy", "running", "canceled"])
def test_latest_tested_receipt(push_repo, capsys, status):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, status=status)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((1, "rejected") if status == "failed" else (0, "ok"))
    if status == "failed":
        assert "did not complete" in result["reason"]


def test_updates_check_only_distinct_tip_trees(push_repo, capsys, monkeypatch):
    base = _policy(push_repo)
    (push_repo / "src.py").write_text("value = 2\n", encoding="utf-8")
    _commit(push_repo, "unverified intermediate")
    (push_repo / "src.py").write_text("value = 3\n", encoding="utf-8")
    tip = _commit(push_repo, "verified tip")
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    same_tree = _commit(push_repo, "same tree, different commit")
    (push_repo / "src.py").write_text("value = 4\n", encoding="utf-8")
    other = _commit(push_repo, "unverified tip")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "\n".join(
                [
                    _line(tip, "refs/heads/first", base),
                    _line(same_tree, "refs/heads/second"),
                    _line(other, "refs/heads/other"),
                    _line("0" * len(tip), "refs/heads/deleted", tip),
                    _line(tip, "refs/tags/release"),
                    _line(tip, "refs/heads/no-op", tip),
                ]
            )
        ),
    )
    checked = []
    real = verify_push._tree_verdict

    def record(target, policy, tree, receipts, deadline):
        checked.append(tree)
        return real(target, policy, tree, receipts, deadline)

    monkeypatch.setattr(verify_push, "_tree_verdict", record)
    code, results = _check(push_repo, capsys)
    assert code == 1
    assert [(item["ref"], item["status"]) for item in results] == [
        ("refs/heads/first", "ok"),
        ("refs/heads/second", "ok"),
        ("refs/heads/other", "missing_receipt"),
    ]
    assert checked == [tree, _tree(push_repo)]


def test_no_declaration(push_repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid pre-push input"))
    assert main(["work", "verify", "push-check", "--ref", "main", "--target", str(push_repo)]) == 0
    assert capsys.readouterr().out == ""
    assert _single(push_repo, capsys)[1]["status"] == "not_applicable"


def test_base_predates_policy(push_repo, monkeypatch, capsys):
    old = _git(push_repo, "rev-parse", "HEAD")
    _policy(push_repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(_line(old, "refs/heads/behind")))
    code, [result] = _check(push_repo, capsys)
    assert (code, result["status"]) == (3, "base_predates_policy")
    assert "Update the branch" in result["reason"]


def test_malformed_declaration(push_repo, capsys):
    _policy(push_repo, malformed=True)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert DECLARATION in result["reason"]


def test_slow_git_shares_the_deadline(push_repo, capsys, monkeypatch):
    real = subprocess.run
    deadlines = []

    def slow_git(argv, *args, **kwargs):
        deadlines.append(kwargs["timeout"])
        if argv[1] == "merge-base":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_git)
    started = time.monotonic()
    code, result = _single(push_repo, capsys)
    assert time.monotonic() - started < 2
    assert (code, result["status"]) == (3, "indeterminate")
    assert "timed out" in result["reason"]
    assert len(deadlines) > 2 and 0 < deadlines[-1] < deadlines[0] <= 2


@pytest.mark.parametrize("missing", [[], [EXTRA_SELECTOR]])
def test_plan_snapshot_and_remedy(push_repo, capsys, missing):
    base = _policy(push_repo, coverage=True)
    files = [SELECTOR, EXTRA_SELECTOR]
    _receipt(
        push_repo,
        "f" * len(_tree(push_repo)),
        plan={
            "schema_version": 1,
            "snapshot_tree": _tree(push_repo),
            "base_commit": base,
            "mode": "selective",
            "missing_files": missing,
            "required_files": files,
            "run_argv": [*PREFIX, *files],
        },
    )
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((1, "rejected") if missing else (0, "ok"))
    if missing:
        assert EXTRA_SELECTOR in result["reason"]
        assert json.dumps([*PREFIX, *files]) in result["remedy"]
        assert "--worktree" not in result["remedy"]


def test_only_offline_git_is_called(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    _receipt(push_repo, _tree(push_repo))
    real = subprocess.Popen
    commands = []

    def process(argv, *args, **kwargs):
        assert argv[0] == "git"
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        commands.append(argv[1])
        return real(argv, *args, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("push-check attempted to snapshot, plan or run a test")

    monkeypatch.setattr(subprocess, "Popen", process)
    monkeypatch.setattr(localio, "tree_fingerprint", unexpected)
    monkeypatch.setattr(verify_required, "plan_snapshot", unexpected)
    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", unexpected)
    assert _single(push_repo, capsys)[0] == 0
    assert set(commands) == {"rev-parse", "remote", "symbolic-ref", "merge-base", "ls-tree", "cat-file"}


def test_bounded_scan_reports_indeterminate(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    _receipt(push_repo, _tree(push_repo))
    _receipt(push_repo, "f" * len(_tree(push_repo)), second=2)
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "scan reached its limit" in result["reason"]


def test_invalid_input_is_usage_error(push_repo, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid pre-push input"))
    assert main(["work", "verify", "push-check", "--target", str(push_repo)]) == 2
    assert "four fields" in capsys.readouterr().err
