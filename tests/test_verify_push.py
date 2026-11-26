"""Check pushed branch tips against worktree-local verification receipts."""

from __future__ import annotations

import io
import json
import shlex
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


def _receipt(
    target,
    tree,
    *,
    second=1,
    status="completed",
    plan=None,
    argv=None,
    passed=1,
    command_status=None,
    failed=0,
    errors=0,
    started_at=None,
    extra_commands=(),
    tree_stable=True,
):
    receipt = {
        "status": status,
        "started_at": started_at if started_at is not None else f"2026-01-01T12:00:{second:02d}+00:00",
        "tree_stable": tree_stable,
        "tree_fingerprint": tree,
        "commands": [
            {
                "argv": [*PREFIX, SELECTOR] if argv is None else argv,
                "test_outcomes": {
                    "passed": passed,
                    "failed": failed,
                    "errors": errors,
                    "no_tests_ran": passed == failed == errors == 0,
                },
                **({"status": command_status} if command_status else {}),
            },
            *extra_commands,
        ],
    }
    if plan is None:
        verify_gate.stamp_gating(target, receipt)
    else:
        policy = verify_gate._load_policy(target)
        eligible = not plan["missing_files"]
        receipt["gating"] = policy.stamp(eligible, "passed_required_check" if eligible else "plan_files_missing")
        receipt["gating"]["plan"] = plan
    path = target / ".brigade" / "work" / "verify-runs" / f"20260101-1200{second:02d}-work-verify-ffff00"
    path.mkdir(parents=True)
    receipt_path = path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path


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


@pytest.mark.parametrize("status", ["busy", "running", "canceled"])
def test_ignores_unfinished_receipt(push_repo, capsys, status):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, status=status)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "missing_receipt")
    _receipt(push_repo, tree, second=2)
    assert _single(push_repo, capsys)[0] == 0


@pytest.mark.parametrize("passing_second", [1, 2])
def test_accepts_required_check_with_an_unrelated_run(push_repo, capsys, passing_second):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, second=passing_second)
    _receipt(push_repo, tree, second=3 - passing_second, argv=["git", "status"])
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (0, "ok")


@pytest.mark.parametrize(("failure_second", "expected"), [(1, (0, "ok")), (2, (1, "rejected"))])
def test_latest_required_failure_or_pass_governs(push_repo, capsys, failure_second, expected):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, second=3 - failure_second)
    _receipt(push_repo, tree, second=failure_second, status="failed", command_status="failed")
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == expected
    if code:
        assert result["reason"] == "The most recent run of the required check failed. An earlier run had passed."


@pytest.mark.parametrize("command_status", ["failed", "timed_out"])
@pytest.mark.parametrize(("failure_second", "expected"), [(1, (0, "ok")), (2, (1, "rejected"))])
def test_unstable_required_failure_governs_when_newer(push_repo, capsys, command_status, failure_second, expected):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, second=3 - failure_second)
    _receipt(push_repo, tree, second=failure_second, status="failed", command_status=command_status, tree_stable=False)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == expected
    if code:
        assert "An earlier run had passed" in result["reason"]


@pytest.mark.parametrize("status", ["failed", "timed_out"])
def test_legacy_multi_command_failure_masks_an_older_pass(push_repo, capsys, status):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, status=status, extra_commands=[{"argv": ["git", "status"]}])
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert "An earlier run had passed" in result["reason"]


@pytest.mark.parametrize("different_policy", [False, True], ids=["lint-fail-pass", "policy-changed"])
def test_newest_lint_does_not_hide_failure_before_pass(push_repo, capsys, different_policy):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    failure_path = _receipt(push_repo, tree, second=2, status="failed", command_status="failed")
    if different_policy:
        receipt = json.loads(failure_path.read_text(encoding="utf-8"))
        receipt["gating"]["declaration_sha256"] = "f" * 64
        failure_path.write_text(json.dumps(receipt), encoding="utf-8")
    _receipt(push_repo, tree, second=3, argv=["git", "status"])
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert "An earlier run had passed" in result["reason"]


@pytest.mark.parametrize(
    "later",
    [
        {"status": "failed", "command_status": "failed", "passed": 0},
        {"status": "timed_out", "command_status": "timed_out", "passed": 0},
        {"status": "rejected", "command_status": "rejected"},
        {"status": "canceled", "command_status": "failed"},
        {"status": "failed", "command_status": "failed", "argv": PREFIX},
        {
            "status": "failed",
            "command_status": "completed",
            "extra_commands": [{"argv": ["git", "status"], "status": "failed"}],
        },
    ],
    ids=["no-tests", "timed-out-no-tests", "rejected", "canceled", "no-selector", "unrelated-command-failed"],
)
def test_ineligible_run_without_required_failure_keeps_pass(push_repo, capsys, later):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, **later)
    assert _single(push_repo, capsys) == (0, {"ref": "HEAD", "status": "ok", "reason": None, "remedy": None})


@pytest.mark.parametrize("status", ["failed", "rejected"])
def test_sole_unsuccessful_receipt_reports_not_completed(push_repo, capsys, status):
    _policy(push_repo)
    _receipt(push_repo, _tree(push_repo), status=status, command_status=status)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert result["reason"].startswith(("The selected run", "The most recent run"))
    assert result["reason"].endswith("of the required check did not complete successfully.")


def test_eligible_receipt_for_another_tree_is_missing(push_repo, capsys):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, "f" * len(tree))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "missing_receipt")
    assert result["reason"] == "No local verification receipt matches this tree."


def test_empty_receipt_directory_is_missing(push_repo, capsys):
    _policy(push_repo)
    (push_repo / ".brigade" / "work" / "verify-runs").mkdir(parents=True)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "missing_receipt")


@pytest.mark.parametrize("outcome", [{"failed": 1}, {"errors": 1}, {"command_status": "timed_out"}])
def test_required_failure_uses_recorded_outcomes_and_timeout(push_repo, capsys, outcome):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    status = "failed" if outcome.get("command_status") else "completed"
    _receipt(push_repo, tree, second=2, status=status, passed=int("command_status" in outcome), **outcome)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert "An earlier run had passed" in result["reason"]


@pytest.mark.parametrize("failure_is_newer", [False, True])
def test_same_second_pass_and_failure_follow_start_time(push_repo, capsys, failure_is_newer):
    _policy(push_repo)
    tree = _tree(push_repo)
    passed = "2026-01-01T12:00:01Z"
    fractional = "2026-01-01T12:00:01.500000+00:00"
    _receipt(push_repo, tree, started_at=passed if failure_is_newer else fractional)
    _receipt(
        push_repo,
        tree,
        second=2,
        started_at=fractional if failure_is_newer else passed,
        status="failed",
        command_status="failed",
    )
    root = push_repo / ".brigade" / "work" / "verify-runs"
    failed_run = root / "20260101-120002-work-verify-ffff00"
    failed_run.rename(root / "20260101-120001-work-verify-aaaa00")
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((1, "rejected") if failure_is_newer else (0, "ok"))


@pytest.mark.parametrize("timestamp", [None, "invalid", "2026-01-01T12:00:01.000000+00:00"])
def test_unordered_same_second_pass_and_failure_are_indeterminate(push_repo, capsys, timestamp):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, started_at="2026-01-01T12:00:01Z")
    failed = _receipt(push_repo, tree, second=2, status="failed", command_status="failed")
    receipt = json.loads(failed.read_text(encoding="utf-8"))
    if timestamp is None:
        del receipt["started_at"]
    else:
        receipt["started_at"] = timestamp
    failed.write_text(json.dumps(receipt), encoding="utf-8")
    failed.parent.rename(failed.parent.with_name("20260101-120001-work-verify-aaaa00"))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "order" in result["reason"] or "timestamp" in result["reason"]


def test_audit_policy_keeps_any_eligible_receipt(push_repo, monkeypatch, capsys):
    policy = verify_gate._GatePolicy("audit_only", "audit_only")
    monkeypatch.setattr(verify_gate, "_committed_policy", lambda *args, **kwargs: policy)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, status="failed", command_status="failed")
    assert _single(push_repo, capsys)[0] == 0


@pytest.mark.parametrize(
    ("argv", "passed", "reason"),
    [
        (["git", "status"], 1, "did not run the required check"),
        ([*PREFIX, SELECTOR], 0, "passed no tests"),
    ],
)
def test_rejects_ineligible_command(push_repo, capsys, argv, passed, reason):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, argv=argv, passed=passed)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert reason in result["reason"]
    assert json.dumps([*PREFIX, "tests/test_example.py"]) in result["remedy"]


def test_rejection_prefers_latest_required_check(push_repo, capsys):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, status="rejected")
    _receipt(push_repo, tree, second=2, passed=0)
    _receipt(push_repo, tree, second=3, argv=["git", "status"])
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "rejected")
    assert result["reason"].startswith("The most recent run of the required check passed no tests")


@pytest.mark.parametrize(
    ("older", "expected"),
    [
        ("2026-01-01T12:00:01Z", (1, "rejected")),
        ("invalid", (3, "indeterminate")),
        ("0001-01-01T00:00:00+01:00", (3, "indeterminate")),
    ],
)
def test_rejection_checks_parsed_start_times(push_repo, capsys, older, expected):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, started_at=older, argv=PREFIX)
    _receipt(push_repo, tree, second=2, passed=0)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == expected
    if expected[1] == "rejected":
        assert "passed no tests" in result["reason"]


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


@pytest.mark.parametrize(("missing", "prior_pass"), [([], False), ([EXTRA_SELECTOR], False), ([EXTRA_SELECTOR], True)])
def test_plan_snapshot_and_remedy(push_repo, capsys, missing, prior_pass):
    base = _policy(push_repo, coverage=True)
    files = [SELECTOR, EXTRA_SELECTOR]
    plan = {
        "schema_version": 1,
        "snapshot_tree": _tree(push_repo),
        "base_commit": base,
        "mode": "selective",
        "missing_files": missing,
        "required_files": files,
        "run_argv": [*PREFIX, *files],
    }
    if prior_pass:
        _receipt(push_repo, _tree(push_repo), plan={**plan, "missing_files": []})
    _receipt(
        push_repo,
        _tree(push_repo),
        second=2,
        plan=plan,
    )
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((1, "rejected") if missing and not prior_pass else (0, "ok"))
    if missing and not prior_pass:
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


@pytest.mark.parametrize(("matches_tree", "passing"), [(False, False), (True, False), (True, True)])
def test_bounded_scan_reports_indeterminate(push_repo, capsys, monkeypatch, matches_tree, passing):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(
        push_repo, tree if matches_tree else "f" * len(tree), second=2, argv=None if passing else ["git", "status"]
    )
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "scan reached its limit" in result["reason"]


def test_scan_cap_does_not_decide_using_partial_second(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2, status="failed")
    root = push_repo / ".brigade" / "work" / "verify-runs"
    (root / "20260101-120002-work-verify-ffff00").rename(root / "20260101-120001-work-verify-aaaa00")
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")


@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize("content", [b"{", b"[]", b'{"commands": [null]}', b"[" * 2000])
def test_malformed_receipt_only_blocks_when_it_could_decide(push_repo, capsys, newer, content):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree, second=1 if newer else 2)
    broken = _receipt(push_repo, tree, second=2 if newer else 1)
    broken.write_bytes(content)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == ((3, "indeterminate") if newer else (0, "ok"))
    if newer:
        assert "malformed" in result["reason"] and result["remedy"].startswith("mv -- ")


def test_run_directory_without_receipt_is_not_evidence(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2).unlink()
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    assert _single(push_repo, capsys)[0] == 0


@pytest.mark.parametrize(
    "name",
    ["000-old-name", "20260101-120000-work-verify-abcdef", "20990101-120002-work-verify-abcdef"],
)
def test_invalid_name_of_matching_receipt_is_indeterminate(push_repo, capsys, name):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    suspect = _receipt(push_repo, tree, second=2)
    suspect.parent.rename(suspect.parent.with_name(name))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "timestamp" in result["reason"]


def test_future_timestamp_agreeing_with_name_is_indeterminate(push_repo, capsys):
    _policy(push_repo)
    future = _receipt(push_repo, _tree(push_repo), started_at="2099-01-01T12:00:01Z")
    future.parent.rename(future.parent.with_name("20990101-120001-work-verify-abcdef"))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "future" in result["reason"]


def test_symlinked_run_directory_is_indeterminate(push_repo, capsys):
    _policy(push_repo)
    run = _receipt(push_repo, _tree(push_repo)).parent
    run.with_name("20260101-120002-work-verify-abcdef").symlink_to(run, target_is_directory=True)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "symbolic link" in result["reason"]


@pytest.mark.parametrize("snapshot_matches", [False, True])
def test_tree_and_plan_snapshot_must_both_match(push_repo, capsys, snapshot_matches):
    base = _policy(push_repo, coverage=True)
    tree = _tree(push_repo)
    other = "f" * len(tree)
    _receipt(
        push_repo,
        other if snapshot_matches else tree,
        plan={
            "schema_version": 1,
            "snapshot_tree": tree if snapshot_matches else other,
            "base_commit": base,
            "mode": "selective",
            "missing_files": [],
            "required_files": [SELECTOR],
            "run_argv": [*PREFIX, SELECTOR],
        },
    )
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (1, "missing_receipt")


@pytest.mark.parametrize("count", [True, False, -1, 1.5, "1", None])
def test_invalid_outcome_count_is_malformed(push_repo, capsys, count):
    _policy(push_repo)
    tree = _tree(push_repo)
    _receipt(push_repo, tree)
    path = _receipt(push_repo, tree, second=2)
    receipt = json.loads(path.read_text())
    receipt["commands"][0]["test_outcomes"]["deselected"] = count
    path.write_text(json.dumps(receipt), encoding="utf-8")
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert "malformed" in result["reason"]


def test_receipt_read_checks_deadline_after_open(push_repo, monkeypatch):
    path = _receipt(push_repo, _tree(push_repo))
    receipts = verify_push._Receipts(push_repo, time.monotonic() + 2)

    class UnreadStream(io.BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail("read started after the deadline")

    def slow_open(*args, **kwargs):
        monkeypatch.setattr(verify_push, "monotonic", lambda: receipts.deadline)
        return UnreadStream()

    monkeypatch.setattr(type(path), "open", slow_open)
    with pytest.raises(verify_push._Indeterminate, match="two-second time limit"):
        receipts._load(path.parent.name)


def test_invalid_input_is_usage_error(push_repo, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("invalid pre-push input"))
    assert main(["work", "verify", "push-check", "--target", str(push_repo)]) == 2
    assert "four fields" in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["symlink", "malformed", "bad-name", "mismatch", "future"])
def test_exact_store_remedy_restores_verdict(push_repo, capsys, monkeypatch, problem):
    _policy(push_repo)
    tree = _tree(push_repo)
    good = _receipt(push_repo, tree)
    if problem == "symlink":
        offending = good.parent.with_name("20260101-120002-work-verify-abc123")
        offending.symlink_to(good.parent, target_is_directory=True)
    else:
        bad = _receipt(push_repo, tree, second=2, started_at="2099-01-01T12:00:02Z" if problem == "future" else None)
        if problem == "malformed":
            bad.write_text("{", encoding="utf-8")
            offending = bad
        else:
            name = {
                "bad-name": "backup-old",
                "mismatch": "20260101-120000-work-verify-abc123",
                "future": "20990101-120002-work-verify-abc123",
            }[problem]
            offending = bad.parent.with_name(name)
            bad.parent.rename(offending)
    sandbox = push_repo / "scratch folder"
    sandbox.mkdir()
    monkeypatch.setenv("TMPDIR", str(sandbox))
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert str(offending) in result["reason"] and result["remedy"].startswith("mv -- ")
    subprocess.run(["bash", "-c", result["remedy"]], cwd=push_repo, check=True)
    assert not offending.exists()
    assert _single(push_repo, capsys)[0] == 0


def test_cap_remedy_uses_configured_archive_and_restores_verdict(push_repo, capsys, monkeypatch):
    _policy(push_repo)
    tree = _tree(push_repo)
    old = _receipt(push_repo, tree)
    _receipt(push_repo, tree, second=2)
    gate_config = push_repo / ".brigade" / "config.json"
    gate_config.write_text(
        '{"version": 1, "depth": "workspace", "harnesses": ["codex"], '
        '"verify_runs_keep": 1, "verify_archive_dir": ".brigade/custom-archive"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_push, "_RECEIPT_LIMIT", 1)
    code, result = _single(push_repo, capsys)
    assert (code, result["status"]) == (3, "indeterminate")
    assert all(
        value in result["reason"]
        for value in ("verify_runs_keep", str(gate_config), ".brigade/custom-archive", str(old.parent.parent))
    )
    archive = push_repo / ".brigade" / "custom-archive"
    archive.write_text("fixture archive path conflict", encoding="utf-8")
    assert main(shlex.split(result["remedy"])[1:]) == 1
    assert old.is_file()
    archive.unlink()
    assert main(shlex.split(result["remedy"])[1:]) == 0
    assert (archive / old.parent.name / "receipt.json").is_file()
    assert "Pruned 1 local verify run(s)." in capsys.readouterr().out
    assert _single(push_repo, capsys)[0] == 0


def test_stray_malformed_receipt_warns_without_masking_pass(push_repo, capsys):
    _policy(push_repo)
    receipt = _receipt(push_repo, _tree(push_repo))
    stray = receipt.parent.with_name("backup-old")
    stray.mkdir()
    (stray / "receipt.json").write_text("{", encoding="utf-8")
    code = main(["work", "verify", "push-check", "--target", str(push_repo), "--ref", "HEAD", "--json"])
    output = capsys.readouterr()
    assert (code, json.loads(output.out)["refs"][0]["status"]) == (0, "ok")
    assert "ignoring malformed receipt in a non-run directory" in output.err and str(stray) in output.err
    assert stray.is_dir()
