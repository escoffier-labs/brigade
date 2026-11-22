"""Receipt verdicts and their use at the Claude Stop boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import code_graph_delta, localio
from brigade.claude_hooks import runtime
from brigade.work_cmd import verification, verify_runtime

from tests.test_claude_hooks_runtime import _git_wired_claude, _unverified_write_stop, _write_session_receipt

DECLARATION = "verify/required-check.json"
PREFIX = ["python", "focused.py"]
SELECTOR = "tests/test_sample.py"
AUDIT_NOTICE = "This repository declares no required check."


def _git(target, *args):
    return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True).stdout.strip()


def _declaration(argv_prefix=PREFIX):
    return json.dumps(
        {
            "schema": "brigade.verify_required_check.v1",
            "argv_prefix": argv_prefix,
            "needs_arguments": True,
            "needs_passed_tests": True,
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
        assert "python focused.py <selector>" in result["reason"]
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


def test_unresolved_default_branch_blocks(repo, monkeypatch, capsys):
    _commit(repo)
    _git(repo, "remote", "add", "origin", "../origin.git")
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, ["true"])

    assert runtime.handle_payload("Stop", stop)["decision"] == "block"
    _verdict(receipt, "unresolved", False, "base_unresolved")


def test_invalid_declaration_is_unresolved(repo, monkeypatch, capsys):
    (repo / DECLARATION).write_bytes(b'{"schema": "brigade.verify_required_check.v1", "argv_prefix": []}')
    _commit(repo)
    stop, _ = _start(repo, monkeypatch)
    receipt = _run(repo, monkeypatch, capsys, ["true"])

    assert runtime.handle_payload("Stop", stop)["decision"] == "block"
    _verdict(receipt, "unresolved", False, "declaration_invalid")


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
def test_reused_legacy_receipt_does_not_invent_a_required_check_pass(repo, monkeypatch, capsys, declared):
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

    reused = _run(repo, monkeypatch, capsys, argv)

    assert reused["reused_from"] == first["run_id"]
    if declared:
        _verdict(reused, "required_check", False, "missing_gating")
    else:
        _verdict(reused, "audit_only", True, "audit_only")
