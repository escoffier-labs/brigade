"""Receipt verdicts and their use at the Claude Stop boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from brigade import code_graph_delta, localio, verify_manifest_git
from brigade.claude_hooks import runtime
from brigade.work_cmd import verification, verify_gate, verify_runtime

from tests.test_claude_hooks_runtime import _git_wired_claude, _unverified_write_stop, _write_session_receipt

DECLARATION = "verify/required-check.json"
PREFIX = ["python", "focused.py"]
SELECTOR = "tests/test_sample.py"
AUDIT_NOTICE = "This repository declares no required check."


def _git(target, *args):
    return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True).stdout.strip()


def _declaration(argv_prefix=PREFIX, *, needs_arguments=True, needs_passed_tests=True):
    return json.dumps(
        {
            "schema": "brigade.verify_required_check.v1",
            "argv_prefix": argv_prefix,
            "needs_arguments": needs_arguments,
            "needs_passed_tests": needs_passed_tests,
        }
    ).encode()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / ".gitignore").write_text(".brigade/\n.claude/\n", encoding="utf-8")
    (target / "src.py").write_text("value = 1\n", encoding="utf-8")
    (target / "focused.py").write_text("# fixture verifier\n", encoding="utf-8")
    (target / "verify").mkdir()
    monkeypatch.setattr(runtime, "_run_brief", lambda _target: "brief")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(code_graph_delta, "capture_before", lambda *args, **kwargs: None)
    monkeypatch.setattr(code_graph_delta, "capture_after_and_diff", lambda *args, **kwargs: {})
    # The child is stubbed below. Keep a bare true executable resolvable on Windows too.
    real_which = verification.shutil.which
    monkeypatch.setattr(
        verification.shutil,
        "which",
        lambda name, **kwargs: sys.executable if name == "true" else real_which(name, **kwargs),
    )
    return target


def _commit(target):
    _git(target, "add", ".")
    return _git(target, "commit", "-m", "fixture base")


def _start(target, monkeypatch):
    (target / "src.py").write_text("value = 2\n", encoding="utf-8")
    stop = _unverified_write_stop(target)
    state = runtime.read_session_state(target, stop["session_id"])
    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, state["session_fingerprint"])
    return stop, state


def _run(target, monkeypatch, capsys, argv=None, *, stdout="", exit_code=0, change_tree=False, manifest=None):
    def child(*_args, **_kwargs):
        if change_tree:
            (target / "src.py").write_text("value = 3\n", encoding="utf-8")
        return ("failed" if exit_code else "completed", exit_code, stdout, "")

    monkeypatch.setattr(verify_runtime, "_run_verify_child_process", child)
    capsys.readouterr()
    assert (
        verification.verify_run(
            target=target, commands=[argv] if argv else None, manifest_id=manifest, json_output=True
        )
        == exit_code
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["digests"]["receipt_sha256"] == localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    return receipt


def _verdict(receipt, mode, eligible, reason):
    stamp = receipt["gating"]
    from brigade.work_cmd.verify_gate import receipt_is_gating

    assert (stamp["mode"], stamp["eligible"], stamp["reason"]) == (mode, eligible, reason)
    assert stamp["plan_coverage"] == "not_checked"
    assert receipt_is_gating(receipt) == (eligible, reason)


@pytest.mark.parametrize(
    ("argv", "stdout", "exit_code", "change_tree", "reason"),
    [
        (["true"], "", 0, False, "not_the_required_check"),
        (["git", "status"], "", 0, False, "not_the_required_check"),
        (PREFIX, "1 passed in 0.01s\n", 0, False, "no_selector"),
        (PREFIX + [SELECTOR], "no tests ran in 0.01s\n", 0, False, "no_tests_passed"),
        (PREFIX + [SELECTOR], "3 skipped in 0.01s\n", 0, False, "no_tests_passed"),
        (PREFIX + [SELECTOR], "1 passed, 1 failed in 0.01s\n", 0, False, "no_tests_passed"),
        (PREFIX + [SELECTOR], "1 passed, 1 error in 0.01s\n", 0, False, "no_tests_passed"),
        (PREFIX + [SELECTOR], "1 failed in 0.01s\n", 1, False, "not_completed"),
        (PREFIX + [SELECTOR], "1 passed in 0.01s\n", 0, True, "tree_changed_during_run"),
        (PREFIX + [SELECTOR], "1 passed in 0.01s\n", 0, False, "passed_required_check"),
    ],
)
def test_required_check_stop(repo, monkeypatch, capsys, argv, stdout, exit_code, change_tree, reason):
    raw = _declaration()
    (repo / DECLARATION).write_bytes(raw)
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, argv, stdout=stdout, exit_code=exit_code, change_tree=change_tree)

    eligible = reason == "passed_required_check"
    result = runtime.handle_payload("Stop", stop)
    assert (result is None or result.get("decision") != "block") == eligible
    if not eligible:
        assert "--argv-json" in result["reason"]
    _verdict(receipt, "required_check", eligible, reason)
    assert receipt["gating"]["base_commit"] == _git(repo, "rev-parse", "HEAD")
    assert receipt["gating"]["declaration_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("committed_edit", [False, True])
def test_branch_cannot_replace_the_base_declaration(repo, monkeypatch, capsys, committed_edit):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", "../origin.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    (repo / DECLARATION).write_bytes(_declaration(["true"]))
    if committed_edit:
        _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, ["true", SELECTOR], stdout="1 passed in 0.01s\n")

    assert runtime.handle_payload("Stop", stop)["decision"] == "block"
    _verdict(receipt, "required_check", False, "not_the_required_check")
    assert receipt["gating"]["base_commit"] == base


def test_missing_declaration_audits_and_explains(repo, monkeypatch, capsys):
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)

    assert AUDIT_NOTICE in runtime.handle_payload("Stop", stop)["reason"]
    receipt = _run(repo, monkeypatch, capsys, ["true"])
    result = runtime.handle_payload("Stop", stop)
    assert result is None or result.get("decision") != "block"
    _verdict(receipt, "audit_only", True, "audit_only")


@pytest.mark.parametrize("topology", ["unborn", "upstream", "custom_default", "no_merge_base"])
def test_unresolved_base_without_declaration_audits(repo, monkeypatch, capsys, topology):
    if topology != "unborn":
        _commit(repo)
        remote = "upstream" if topology == "upstream" else "origin"
        _git(repo, "remote", "add", remote, "../remote.git")
        if topology == "custom_default":
            _git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        if topology == "no_merge_base":
            unrelated = _git(repo, "commit-tree", "HEAD^{tree}", "-m", "unrelated")
            _git(repo, "update-ref", "refs/remotes/origin/main", unrelated)
    stop, _ = _start(repo, monkeypatch)
    block = runtime.handle_payload("Stop", stop)
    receipt = _run(repo, monkeypatch, capsys, ["true"])

    _verdict(receipt, "audit_only", True, "audit_only")
    assert receipt["gating"]["fallback_source"] == "none"
    result = runtime.handle_payload("Stop", stop)
    assert result is None or result.get("decision") is None
    assert "Git base was unresolved" not in json.dumps(result)
    assert block["decision"] == "block"
    notice = block["reason"]
    assert "Git base was unresolved" in notice
    assert "either local HEAD or the working tree" in notice
    if topology in {"upstream", "custom_default"}:
        assert "git remote set-head origin -a" in notice
    elif topology == "no_merge_base":
        assert "no merge base with HEAD" in notice
    else:
        assert "no commit at HEAD" in notice


@pytest.mark.parametrize("source", ["head", "worktree"])
def test_unresolved_base_enforces_local_declaration(repo, monkeypatch, capsys, source):
    raw = _declaration()
    if source == "head":
        (repo / DECLARATION).write_bytes(raw)
    _commit(repo)
    (repo / DECLARATION).write_bytes(_declaration(["true"]) if source == "head" else raw)
    _git(repo, "remote", "add", "origin", "../origin.git")
    stop, _ = _start(repo, monkeypatch)

    wrong = _run(repo, monkeypatch, capsys, ["true", SELECTOR], stdout="1 passed in 0.01s")
    _verdict(wrong, "required_check", False, "not_the_required_check")
    assert wrong["gating"]["fallback_source"] == source
    result = runtime.handle_payload("Stop", stop)
    assert result["decision"] == "block"
    assert "Git base was unresolved" in result["reason"]
    assert ("HEAD" if source == "head" else "working tree") in result["reason"]

    right = _run(repo, monkeypatch, capsys, PREFIX + [SELECTOR], stdout="1 passed in 0.01s")
    _verdict(right, "required_check", True, "passed_required_check")
    assert right["gating"]["declaration_sha256"] == hashlib.sha256(raw).hexdigest()
    assert runtime.handle_payload("Stop", stop).get("decision") != "block"


@pytest.mark.parametrize("valid", [True, False])
def test_shallow_clone_uses_default_branch_tip(repo, valid):
    _git(repo, "branch", "-m", "main")
    (repo / DECLARATION).write_bytes(_declaration() if valid else b"{")
    _commit(repo)
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "feature")
    (repo / DECLARATION).unlink()
    _commit(repo)
    _git(repo, "switch", "main")

    clone = repo.parent / "shallow"
    _git(repo, "clone", "--depth=1", "--branch=feature", repo.as_uri(), str(clone))
    _git(clone, "fetch", "--depth=1", "origin", "main:refs/remotes/origin/main")
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    assert verify_manifest_git.resolve_base(clone).status == "no_merge_base"
    assert not (clone / DECLARATION).exists()

    receipt = {"status": "completed", "commands": [{"argv": ["true"]}]}
    verify_gate.stamp_gating(clone, receipt)
    assert receipt["gating"]["fallback_source"] == "default_branch_tip"
    gate = verify_gate.StopGate(clone)
    assert "default branch tip" in gate.notice()
    if valid:
        _verdict(receipt, "required_check", False, "not_the_required_check")
        assert receipt["gating"]["base_commit"] == tip
        assert gate.command() == PREFIX + ["tests/test_example.py"]
    else:
        _verdict(receipt, "unresolved", False, "declaration_invalid")
        assert DECLARATION in gate.block_guidance("", None)


@pytest.mark.parametrize("source", ["base", "head", "worktree"])
def test_invalid_declaration_is_unresolved(repo, monkeypatch, capsys, source):
    if source == "worktree":
        _commit(repo)
    (repo / DECLARATION).write_bytes(b'{"schema":')
    if source != "worktree":
        _commit(repo)
    if source != "base":
        _git(repo, "remote", "add", "origin", "../origin.git")
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, ["true"])

    result = runtime.handle_payload("Stop", stop)
    assert result["decision"] == "block"
    assert DECLARATION in result["reason"]
    assert "Expecting value" in result["reason"]
    _verdict(receipt, "unresolved", False, "declaration_invalid")
    if source != "base":
        assert "Git base was unresolved" in result["reason"]
        assert receipt["gating"]["fallback_source"] == source


@pytest.mark.parametrize("character", ["`", "\n", "\r", "\t", "\b", "\0", "\x1b", "\x7f", "\x85", "\u2028", "\u2029"])
def test_unsafe_argv_prefix_blocks_stop(repo, monkeypatch, character):
    (repo / DECLARATION).write_bytes(_declaration([*PREFIX, f"arg{character}value"]))
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)

    result = runtime.handle_payload("Stop", stop)
    assert result["decision"] == "block"
    assert DECLARATION in result["reason"]
    assert "argv_prefix" in result["reason"]


@pytest.mark.parametrize("declared", [False, True])
def test_legacy_receipt_only_counts_in_audit_mode(repo, monkeypatch, declared):
    if declared:
        (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    _write_session_receipt(repo, stop["session_id"], tree_fingerprint=localio.tree_fingerprint(repo))
    receipt = next(runtime._iter_verify_receipts(repo))
    result = runtime.handle_payload("Stop", stop)
    assert (result is not None and result.get("decision") == "block") == declared

    from brigade.work_cmd.verify_gate import receipt_is_gating

    assert receipt_is_gating(receipt) == (False, "missing_gating")


def test_manifest_receipt_can_satisfy_required_check(repo, monkeypatch, capsys):
    (repo / DECLARATION).write_bytes(_declaration())
    manifests = repo / "verify" / "manifests"
    manifests.mkdir()
    (manifests / "sample.json").write_text(
        json.dumps(
            {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": "sample",
                "binding_mode": "patch_backed",
                "verifier_id": "test",
                "subject": {"artifact_kind": "skill", "artifact_id": "sample", "subject_path": "other.py"},
                "checks": [{"check_id": "sample", "check_role": "effectiveness", "command": "true"}],
            }
        ),
        encoding="utf-8",
    )
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, manifest="sample")

    result = runtime.handle_payload("Stop", stop)
    assert result is None or result.get("decision") != "block"
    _verdict(receipt, "required_check", True, "manifest_run")


@pytest.mark.parametrize("eligible", [True, False])
def test_reused_receipt_inherits_source_verdict(repo, monkeypatch, capsys, eligible):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    _start(repo, monkeypatch)
    argv = PREFIX + [SELECTOR] if eligible else ["true"]
    first = _run(repo, monkeypatch, capsys, argv, stdout="1 passed in 0.01s\n")
    reused = _run(repo, monkeypatch, capsys, argv)

    assert reused["reused_from"] == first["run_id"]
    assert reused["gating"] == first["gating"]
    _verdict(reused, "required_check", eligible, "passed_required_check" if eligible else "not_the_required_check")


@pytest.mark.parametrize("declared", [False, True])
def test_legacy_receipt_reuses_only_in_audit_mode(repo, monkeypatch, capsys, declared):
    if declared:
        (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    _start(repo, monkeypatch)
    argv = PREFIX + [SELECTOR]
    first = _run(repo, monkeypatch, capsys, argv, stdout="1 passed in 0.01s\n")
    path = Path(first["path"]) / "receipt.json"
    first.pop("gating", None)
    first["digests"]["receipt_sha256"] = localio.canonical_json_digest(first, exclude_keys={"digests"})
    path.write_text(json.dumps(first), encoding="utf-8")

    result = _run(repo, monkeypatch, capsys, argv)

    if declared:
        assert "reused_from" not in result
        _verdict(result, "required_check", False, "no_tests_passed")
    else:
        assert result["reused_from"] == first["run_id"]
        _verdict(result, "audit_only", True, "audit_only")


@pytest.mark.parametrize("was_declared", [False, True])
@pytest.mark.parametrize("transition", ["stop", "reuse"])
def test_policy_change_invalidates_receipt(repo, monkeypatch, capsys, was_declared, transition):
    if was_declared:
        (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    old_base = _git(repo, "rev-parse", "HEAD")
    (repo / DECLARATION).write_bytes(_declaration(PREFIX + ["--updated"]))
    _commit(repo)
    _git(repo, "remote", "add", "origin", "../origin.git")
    _git(repo, "update-ref", "refs/remotes/origin/main", old_base)
    stop, _ = _start(repo, monkeypatch)
    argv = PREFIX + [SELECTOR]
    first = _run(repo, monkeypatch, capsys, argv, stdout="1 passed in 0.01s")
    assert first["gating"]["eligible"] is True

    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert localio.tree_fingerprint(repo) == first["tree_fingerprint"]
    if transition == "stop":
        assert runtime.handle_payload("Stop", stop)["decision"] == "block"
    else:
        rerun = _run(repo, monkeypatch, capsys, argv, stdout="2 passed in 0.01s")
        assert "reused_from" not in rerun
        assert rerun["commands"][0]["test_outcomes"]["passed"] == 2
        _verdict(rerun, "required_check", False, "not_the_required_check")


@pytest.mark.parametrize("head_available", [True, False])
def test_stop_policy_git_timeout_is_bounded(repo, monkeypatch, head_available):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    real_run = subprocess.run
    timeouts = []
    monkeypatch.setattr(verify_gate, "STOP_POLICY_BUDGET_SECONDS", 0.4)
    monkeypatch.setattr(verify_gate, "_HEAD_FALLBACK_BUDGET_SECONDS", 0.3)

    def slow_git(args, **kwargs):
        policy_probe = args[:2] == ["git", "remote"] or (
            not head_available and args[:4] == ["git", "rev-parse", "--verify", "--quiet"]
        )
        if not policy_probe:
            return real_run(args, **kwargs)
        timeout = kwargs["timeout"]
        timeouts.append(timeout)
        time.sleep(min(timeout, 0.4))
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(verify_manifest_git.subprocess, "run", slow_git)
    result = runtime.handle_payload("Stop", stop)

    assert timeouts and max(timeouts) <= 0.4
    if head_available:
        assert result["decision"] == "block"
        assert "Git base was unresolved" in result["reason"]
        assert "HEAD" in result["reason"]
    else:
        assert result is None


def test_stop_guidance_round_trips_argv(repo, monkeypatch):
    prefix = ["python", "folder with spaces/check.py", "a'b", 'a"b', r"a\b"]
    (repo / DECLARATION).write_bytes(_declaration(prefix))
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)

    reason = runtime.handle_payload("Stop", stop)["reason"]
    command = reason.split("`")[1]
    words = shlex.split(command)
    assert "POSIX shell" in reason
    assert json.loads(words[words.index("--argv-json") + 1]) == prefix + ["tests/test_example.py"]


@pytest.mark.parametrize(
    ("flags", "argv", "stdout"),
    [
        ({"needs_arguments": False}, PREFIX, "1 passed in 0.01s"),
        ({"needs_passed_tests": False}, PREFIX + [SELECTOR], ""),
    ],
)
def test_optional_required_check_constraints(repo, monkeypatch, capsys, flags, argv, stdout):
    (repo / DECLARATION).write_bytes(_declaration(**flags))
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, argv, stdout=stdout)

    _verdict(receipt, "required_check", True, "passed_required_check")
    assert runtime.handle_payload("Stop", stop).get("decision") != "block"


def test_safe_finalize_stamps_fallback_verdict(repo, monkeypatch):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    _git(repo, "remote", "add", "origin", "../origin.git")
    now = localio.utc_now()
    receipt = {"status": "completed", "commands": []}

    def broken_finalize(*args, **kwargs):
        raise OSError("fixture finalization failure")

    monkeypatch.setattr(verification, "_finalize_verify_receipt", broken_finalize)
    finalized, rc = verification._safe_finalize_verify_receipt(repo, repo, receipt, started=now, rc=0, canceled=False)

    assert rc != 0
    _verdict(finalized, "required_check", False, "not_completed")
    assert finalized["gating"]["fallback_source"] == "head"
    assert json.loads((repo / "receipt.json").read_text())["gating"] == finalized["gating"]


@pytest.mark.parametrize("valid", [True, False])
def test_non_git_target_uses_worktree_declaration(repo, monkeypatch, capsys, valid):
    (repo / DECLARATION).write_bytes(_declaration() if valid else b"{")
    (repo / ".git").rename(repo.parent / "saved-git")
    stop, _ = _start(repo, monkeypatch)

    receipt = _run(repo, monkeypatch, capsys, ["true"])
    _verdict(
        receipt,
        "required_check" if valid else "unresolved",
        False,
        "not_the_required_check" if valid else "declaration_invalid",
    )
    assert receipt["gating"]["fallback_source"] == "worktree"
    assert runtime.handle_payload("Stop", stop)["decision"] == "block"


def test_stop_explains_latest_ineligible_run(repo, monkeypatch, capsys):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    _run(repo, monkeypatch, capsys, PREFIX)
    _run(repo, monkeypatch, capsys, PREFIX + [SELECTOR])

    reason = runtime.handle_payload("Stop", stop)["reason"]
    assert "The last run passed no tests or reported test failures or errors." in reason
    assert "The last run had no test selector." not in reason


def test_policy_source_change_invalidates_same_declaration(repo, monkeypatch, capsys):
    (repo / DECLARATION).write_bytes(_declaration())
    _commit(repo)
    _git(repo, "remote", "add", "origin", "../origin.git")
    stop, _ = _start(repo, monkeypatch)
    argv = PREFIX + [SELECTOR]
    first = _run(repo, monkeypatch, capsys, argv, stdout="1 passed in 0.01s")
    assert first["gating"]["fallback_source"] == "head"
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    assert runtime.handle_payload("Stop", stop)["decision"] == "block"
    rerun = _run(repo, monkeypatch, capsys, argv, stdout="1 passed in 0.01s")
    assert "reused_from" not in rerun
    assert rerun["gating"]["declaration_sha256"] == first["gating"]["declaration_sha256"]
    assert "fallback_source" not in rerun["gating"]
