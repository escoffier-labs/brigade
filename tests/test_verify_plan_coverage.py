"""Opt-in coverage through verify runs, Stop, and closeout."""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from brigade import cli, localio
from brigade.claude_hooks import runtime
from brigade.work_cmd import closeout_receipt, verification, verify_gate, verify_required, verify_runtime

from tests.test_verify_gate import DECLARATION, PREFIX, _declaration, _start
from tests.test_verify_gate import repo as gate_repo
from tests.test_verify_required import commit, configure, git, pin_base, plan, planner, write

A, B, OTHER = "tests/test_a.py", "tests/test_b.py", "tests/test_other.py"
PASS = "1 passed in 0.01s\n"
repo = gate_repo


def _setup(target, *, flag=True, planner_body=None, selected=(A, B), mode="selective", **extras):
    declaration = json.loads(_declaration())
    if flag is not None:
        declaration["needs_plan_coverage"] = flag
    write(target, DECLARATION, json.dumps(declaration))
    for path in sorted({A, B, OTHER, *selected}):
        write(target, path, "")
    # The shared fixture helper also checks this path from the planner's repo.
    write(target, "tests/a.py", "")
    if mode != "unconfigured":
        configure(target)
        if planner_body is None:
            plan(target, list(selected), mode=mode, **extras)
        else:
            planner(target, planner_body)
    base = commit(target, "configure check and planner")
    pin_base(target, base)
    return base


def _run(target, monkeypatch, capsys, selectors, *, reuse=False, status="completed", code=0, stdout=PASS):
    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", lambda *a, **k: (status, code, stdout, ""))
    capsys.readouterr()
    commands = [selector if isinstance(selector, str) else [*PREFIX, *selector] for selector in selectors]
    assert verification.verify_run(target=target, commands=commands, reuse=reuse, json_output=True) == code
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    return receipt


@pytest.fixture(autouse=True)
def clean_test_environment(monkeypatch):
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PY"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("flag", [None, False])
def test_disabled_keeps_legacy_stamp(repo, monkeypatch, capsys, flag):
    _setup(repo, flag=flag, planner_body="raise AssertionError")
    monkeypatch.setenv("PY", "")
    receipt = _run(repo, monkeypatch, capsys, [[OTHER]])
    stamp = receipt["gating"]
    assert stamp["eligible"] and stamp["plan_coverage"] == "not_checked"
    assert "plan" not in stamp
    assert "py_set" not in receipt["inherited_test_env"]


@pytest.mark.parametrize("value, arguments, passed", [("true", True, True), (True, False, True), (True, True, False)])
def test_invalid_declaration(value, arguments, passed):
    declaration = json.loads(_declaration(needs_arguments=arguments, needs_passed_tests=passed))
    declaration["needs_plan_coverage"] = value
    assert verify_gate._parse_declaration(json.dumps(declaration).encode()).reason == "declaration_invalid"


@pytest.mark.parametrize(
    "commands,missing",
    [
        ([[A, B]], []),
        ([[B]], [A]),
        ([[OTHER]], [A, B]),
        ([[A], [B]], []),
        (
            [
                [
                    "--",
                    "-p",
                    "xdist",
                    "-n",
                    "4",
                    "-q",
                    "-v",
                    "-vv",
                    "--timeout",
                    "5",
                    "--durations=3",
                    "--tb",
                    "short",
                    f"./{A}",
                    B,
                ]
            ],
            [],
        ),
        ([["-n=4", "--timeout=5.5", "--durations", "0", "--tb=no", A, B]], []),
    ],
)
def test_selective_coverage_and_shared_guidance(repo, monkeypatch, capsys, commands, missing):
    base = _setup(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, commands)
    stamp = receipt["gating"]
    assert stamp["eligible"] is (not missing)
    assert stamp["plan"] == {
        "schema_version": 1,
        "mode": "selective",
        "base_commit": base,
        "snapshot_tree": receipt["tree_fingerprint"],
        "required_files": [A, B],
        "missing_files": missing,
        "run_argv": [*PREFIX, A, B],
    }

    receipt["producer_work_session_id"] = "fixture-session"
    selected, explanation = closeout_receipt.select(repo, repo, {"id": "fixture-session"}, [receipt])
    assert selected is receipt
    stop_result = runtime.handle_payload("Stop", stop)
    if missing:
        assert stop_result["decision"] == "block"
        for message in (stop_result["reason"], explanation):
            assert "missed required test files" in message
            assert all(path in message for path in missing)
            assert message.count("--argv-json") == 1
            words = shlex.split(message.split("`", 2)[1])
            assert json.loads(words[words.index("--argv-json") + 1]) == [*PREFIX, A, B]
    else:
        assert explanation is None
        assert stop_result is None or stop_result.get("decision") != "block"


@pytest.mark.parametrize(
    "extra",
    [
        ["-k", "test"],
        ["-m", "unit"],
        ["--deselect", A],
        ["--ignore", A],
        ["--lf"],
        ["--ff"],
        ["-x"],
        ["--maxfail=1"],
        ["--co"],
        ["--collect-only"],
        ["-o", "addopts=-k test"],
        ["-c", "alt.ini"],
        ["--rootdir", "tests"],
        ["--pyargs"],
        ["-p", "other_plugin"],
        ["-n", "--lf"],
        ["--tb=other"],
        ["-qq"],
        ["--", "--"],
        [A + "::test_case"],
        ["tests/test_*.py"],
        ["tests/test_?.py"],
        ["tests/test_[a].py"],
        ["tests/../" + A],
        ["tests"],
        ["@" + A],
        ["./@" + A],
        ["/" + A],
        ["C:/" + A],
        ["././" + A],
        ["tests\\test_a.py"],
        ["tests/test_absent.py"],
    ],
)
def test_noncontributing_argv(repo, monkeypatch, capsys, extra):
    _setup(repo)
    receipt = _run(repo, monkeypatch, capsys, [[A, B, *extra]])
    assert not receipt["gating"]["eligible"]


@pytest.mark.parametrize("mode", [b"120000", b""])
def test_nonregular_files_cannot_contribute(mode):
    entries = {A: (b"100644", b""), B: (mode, b"")}
    assert verify_gate._command_files([A, B], entries) == set()


@pytest.mark.parametrize("variable", ["PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PY"])
@pytest.mark.parametrize("inline", [True, False])
def test_test_environment_cannot_contribute(repo, monkeypatch, capsys, variable, inline):
    _setup(repo)
    if inline:
        commands = [shlex.join([f"{variable}=value", *PREFIX, A, B])]
    else:
        monkeypatch.setenv(variable, "")
        commands = [[A, B]]
    assert not _run(repo, monkeypatch, capsys, commands)["gating"]["eligible"]


@pytest.mark.parametrize(
    "stdout",
    [
        "1 passed, 1 deselected in 0.01s\n",
        "1 failed in 0.01s\n",
        "1 passed, 1 error in 0.01s\n",
        "no tests ran in 0.01s\n",
    ],
)
def test_command_outcome_must_support_coverage(repo, monkeypatch, capsys, stdout):
    _setup(repo)
    assert not _run(repo, monkeypatch, capsys, [[A, B]], stdout=stdout)["gating"]["eligible"]


def test_cannot_combine_receipts_or_hide_newest_miss(repo, monkeypatch, capsys):
    _setup(repo)
    stop, _ = _start(repo, monkeypatch)
    _run(repo, monkeypatch, capsys, [[A, B]])
    _run(repo, monkeypatch, capsys, [[A]])
    latest = _run(repo, monkeypatch, capsys, [[B]])
    assert not latest["gating"]["eligible"]
    result = runtime.handle_payload("Stop", stop)
    assert result["decision"] == "block"
    assert f"missed required test files {A}" in result["reason"]


def test_guidance_bounds_missing_list_and_keeps_complete_argv(repo, monkeypatch, capsys):
    required = [f"tests/test_{i:02}.py" for i in range(12)]
    _setup(repo, selected=required)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, [[OTHER]])
    assert not receipt["gating"]["eligible"]
    stopped = runtime.handle_payload("Stop", stop)["reason"]
    _, closeout = verify_gate.receipt_counts_for_policy(repo, receipt)
    for guidance in (stopped, closeout):
        assert guidance.count("--argv-json") == 1
        command = shlex.split(guidance.split("`", 2)[1])
        assert json.loads(command[command.index("--argv-json") + 1]) == [*PREFIX, *required]
        assert f"{required[9]} and 2 more." in guidance


def test_overlapping_runs_use_newest_start_for_stop_and_closeout(repo, monkeypatch, capsys):
    _setup(repo)
    first = _run(repo, monkeypatch, capsys, [[A, B]])
    newest = _run(repo, monkeypatch, capsys, [[A]])
    first["completed_at"] = (datetime.fromisoformat(newest["completed_at"]) + timedelta(seconds=1)).isoformat()
    first["producer_work_session_id"] = newest["producer_work_session_id"] = "fixture-session"
    gate = verify_gate.StopGate(repo)
    threshold = datetime.fromisoformat(first["started_at"])
    assert list(gate.latest_completion([first, newest], threshold, None)) == []
    assert gate.command() == [*PREFIX, A, B]
    selected, blocker = closeout_receipt.select(repo, repo, {"id": "fixture-session"}, [newest, first])
    assert selected is newest
    assert f"missed required test files {B}" in blocker


@pytest.mark.parametrize(
    "mode, selected, extras, expected",
    [
        ("selective", tuple(f"tests/test_{i}.py" for i in range(21)), {}, "ci_required"),
        ("selective", (A, B, OTHER), {"test_files_total": 3}, "ci_required"),
        ("full", (A, B), {}, "ci_required"),
        ("unconfigured", (), {}, "unconfigured"),
    ],
)
def test_existing_rule_when_plan_is_broad_or_unconfigured(repo, monkeypatch, capsys, mode, selected, extras, expected):
    _setup(repo, selected=selected, mode=mode, **extras)
    stamp = _run(repo, monkeypatch, capsys, [[OTHER]])["gating"]
    assert stamp["eligible"] and stamp["plan"]["mode"] == expected
    assert ("Required CI" if expected == "ci_required" else "not configured") in stamp["plan"]["reason"]


@pytest.mark.parametrize(
    "body,reason",
    [
        ("raise SystemExit(7)", "status 7"),
        ("out.write_text('invalid JSON')", "valid UTF-8 JSON"),
        ("out.write_text(json.dumps(dict(mode='full', selected=[], error='fixture failure')))", "fixture failure"),
        ("time.sleep(10)", "timed out"),
    ],
)
def test_planner_error_preserves_status_and_retries(repo, monkeypatch, capsys, body, reason):
    _setup(repo, planner_body=body)
    stop, _ = _start(repo, monkeypatch)
    snapshot = verify_required.plan_snapshot
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(verify_required, "plan_snapshot", record)
    if body.startswith("time.sleep"):
        monkeypatch.setattr(verify_required, "PLANNER_TIMEOUT_SECONDS", 1)
    first = _run(repo, monkeypatch, capsys, [[A, B]])
    assert first["status"] == "completed"
    assert first["gating"]["plan"]["mode"] == "error"
    assert not first["gating"]["eligible"]
    counts, explanation = verify_gate.receipt_counts_for_policy(repo, first)
    assert not counts
    stopped = runtime.handle_payload("Stop", stop)
    for message in (explanation, stopped["reason"]):
        assert "planning failed" in message
        assert reason in message
        assert "plans again" in message
    assert len(calls) == 1
    second = _run(repo, monkeypatch, capsys, [[A, B]], reuse=True)
    assert not second.get("reused_from")
    assert second["gating"]["plan"]["mode"] == "error"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "status, stable, reused",
    [
        ("failed", True, False),
        ("canceled", True, False),
        ("busy", True, False),
        ("completed", False, False),
        ("completed", True, True),
    ],
)
def test_nonfinal_and_unstable_receipts_skip_planning(repo, monkeypatch, status, stable, reused):
    _setup(repo)
    monkeypatch.setattr(verify_required, "plan_snapshot", lambda *a, **kw: pytest.fail("planning should be skipped"))
    receipt = {
        "status": status,
        "tree_stable": stable,
        "tree_fingerprint": localio.tree_fingerprint(repo),
    }
    if reused:
        receipt["reused_from"] = "source-run"
    verify_gate.stamp_gating(repo, receipt)
    assert receipt["gating"]["plan"]["mode"] == "not_checked"
    assert not receipt["gating"]["eligible"]


def test_fallback_declaration_does_not_run_planner(repo, monkeypatch, capsys):
    _setup(repo)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    monkeypatch.setattr(verify_required, "plan_snapshot", lambda *a, **kw: pytest.fail("planning should be skipped"))
    stamp = _run(repo, monkeypatch, capsys, [[A, B]])["gating"]
    assert stamp["fallback_source"] == "head"
    assert stamp["plan"]["mode"] == "not_checked"
    assert not stamp["eligible"]


def test_git_and_planner_share_deadline(repo, monkeypatch):
    base = _setup(repo)
    tree = localio.tree_fingerprint(repo)
    run = subprocess.run
    now = 100.0
    timeouts = []

    def advance(*args, **kwargs):
        nonlocal now
        timeouts.append(kwargs["timeout"])
        now += 5
        return run(*args, **kwargs)

    monkeypatch.setattr(verify_required, "monotonic", lambda: now)
    monkeypatch.setattr(subprocess, "run", advance)
    fields, _, _ = verify_required.plan_snapshot(repo, base, tree, deadline=160.0)
    assert fields["mode"] == "selective"
    assert timeouts[:-1] == [min(30, 60 - i * 5) for i in range(len(timeouts) - 1)]
    assert timeouts[-1] == 60 - (len(timeouts) - 1) * 5


def test_worktree_cli_and_receipt_share_base_pinned_snapshot(repo, monkeypatch, capsys):
    source = (
        "repo = pathlib.Path(arg('repo'))\n"
        "assert (repo / 'src.py').read_text() == 'value = 2\\n'\n"
        "assert (repo / 'tests/test_new.py').is_file()\n"
        "assert not (repo / 'tests/test_ignored.py').exists()\n"
        "import subprocess\n"
        "changed = subprocess.check_output(['git', '-C', str(repo), 'diff', '--name-only',"
        " arg('base'), arg('head')], text=True).splitlines()\n"
        "assert set(changed) >= {'src.py', 'tests/test_new.py', 'committed.py'}\n"
        "out.write_text(json.dumps(dict(mode='selective', selected=['tests/test_new.py'])))"
    )
    base = _setup(repo, planner_body=source)
    write(repo, "committed.py", "")
    write(repo, ".gitignore", ".brigade/\n.claude/\n/tests/test_ignored.py\n")
    commit(repo, "branch change")
    _start(repo, monkeypatch)
    write(repo, "tests/test_new.py", "")
    write(repo, "tests/test_ignored.py", "")
    write(repo, verify_required.CONFIG_PATH, "broken config")
    write(repo, "planner.py", "raise AssertionError")
    capsys.readouterr()
    assert cli.main(["work", "verify", "required", "--target", str(repo), "--worktree", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["base"] == base and result["covers_worktree"]
    assert result["tests"] == ["tests/test_new.py"]
    assert git(repo, "cat-file", "-t", result["tree_id"]) == "tree"
    receipt = _run(repo, monkeypatch, capsys, [["tests/test_new.py"]])
    assert receipt["gating"]["eligible"]
    assert receipt["gating"]["plan"]["snapshot_tree"] == result["tree_id"]


def test_stamp_staleness_and_reuse(repo, monkeypatch, capsys):
    _setup(repo)
    receipt = _run(repo, monkeypatch, capsys, [[A, B]])
    now = datetime.now(timezone.utc) - timedelta(minutes=1)

    def counts(expected):
        assert verify_gate.receipt_counts_for_policy(repo, receipt)[0] is expected
        assert (verify_gate.StopGate(repo).completion_in_window(receipt, now, None) is not None) is expected
        assert (verify_gate.reuse_gating(repo, receipt) is not None) is expected

    counts(True)
    git(repo, "commit", "--allow-empty", "-qm", "same tree on branch")
    counts(True)
    current = _run(repo, monkeypatch, capsys, [[A, B]], reuse=True)
    assert current["reused_from"] == receipt["run_id"]
    assert current["gating"] == receipt["gating"]
    old_stamp = {**receipt, "gating": {**receipt["gating"]}}
    del old_stamp["gating"]["plan"]
    assert not verify_gate.receipt_counts_for_policy(repo, old_stamp)[0]
    write(repo, "src.py", "changed = True\n")
    counts(False)
    git(repo, "restore", "src.py")
    counts(True)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    counts(False)
