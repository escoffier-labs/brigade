"""Pin agent-facing refusal copy for the highest-traffic CLI errors (#739).

These strings are an API surface: agents pattern-match remediations out of them.
Bypass / overwrite flags must not appear in the refusal text; structured fields
carry machine-readable detail instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import center_cmd, cli, mcp_cmd, outcome_cmd, runguard, scrub, work_cmd
from brigade.work_cmd import edges as edges_mod
from brigade.work_cmd.session import briefing as briefing_mod

from tests.work_cmd_test_helpers import _init_git_repo


_BYPASS_TOKENS = (
    "--force",
    "--allow-dirty",
    "--allow-unreviewed",
    "--allow-global-stdio",
    "--operator-confirm",
    "--no-verify",
    "chmod +x",
)


def _assert_no_bypass_tokens(text: str) -> None:
    for token in _BYPASS_TOKENS:
        assert token not in text, f"refusal copy must not name {token!r}: {text!r}"


def _add(target: Path, text: str) -> dict:
    task, created = work_cmd._add_task(target, text)
    assert created
    return task


def _seed_mcp(target: Path) -> None:
    mcp_cmd.init(target=target, json_output=True)
    mcp_cmd.add(
        target=target,
        name="github",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env=["GITHUB_TOKEN=ref:GITHUB_TOKEN"],
        timeout=60,
        json_output=True,
    )


def test_task_done_open_children_refusal_is_structured_without_force(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone.utc),
    )
    parent = _add(tmp_path, "Parent epic")
    child = _add(tmp_path, "Open child")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    capsys.readouterr()

    assert work_cmd.task_done(target=tmp_path, task_id=parent["id"], json_output=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "parent has open children"
    assert payload["reason"] == edges_mod.REASON_OPEN_CHILDREN
    assert payload["task_id"] == parent["id"]
    assert any(item["id"] == child["id"] for item in payload["open_children"])
    _assert_no_bypass_tokens(json.dumps(payload))

    assert work_cmd.task_done(target=tmp_path, task_id=parent["id"], json_output=False) == 2
    err = capsys.readouterr().err
    assert "parent has open children" in err
    _assert_no_bypass_tokens(err)


def test_workflow_rules_missing_templates_remediation_omits_force(tmp_path):
    detail = briefing_mod._workflow_rule_health(tmp_path)["detail"]
    assert "brigade init --target" in detail
    assert "--depth repo" in detail
    assert "missing" in detail
    _assert_no_bypass_tokens(detail)


def test_mcp_user_scope_stdio_gate_omits_allow_flag(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_mcp(repo)
    capsys.readouterr()

    rc = mcp_cmd.sync(target=repo, harness="cursor", user_scope=True, write=True, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    joined = "\n".join(payload.get("errors") or [])
    assert "user-scoped sync would write stdio MCP servers" in joined
    _assert_no_bypass_tokens(joined)
    _assert_no_bypass_tokens(json.dumps(payload.get("errors") or []))


def test_center_actions_build_refuses_unreviewed_without_bypass_flag(tmp_path, capsys):
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps(
            {
                "id": "import-high",
                "text": "Fix high risk finding",
                "kind": "task",
                "source": "security-scan",
                "status": "pending",
                "priority": "high",
                "metadata": {"source_fingerprint": "fp-high"},
                "created_at": "2026-05-29T12:01:00+00:00",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 2
    err = capsys.readouterr().err
    assert "must be closed out as reviewed or deferred" in err
    _assert_no_bypass_tokens(err)


def test_outcome_ledger_corrupt_points_at_doctor_not_confirm_flag(tmp_path):
    path = tmp_path / "records.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    message = outcome_cmd._ledger_corrupt_message(1, "invalid_json", path)
    assert "brigade outcome doctor" in message
    _assert_no_bypass_tokens(message)


def test_dirty_worktree_refusal_omits_allow_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(runguard.DirtyWorktreeError) as exc:
        runguard.require_clean_worktree(repo)
    text = str(exc.value)
    assert "dirty worktree" in text
    assert "Commit, stash, or clean the tree" in text
    _assert_no_bypass_tokens(text)


def test_publish_hook_doctor_advice_is_platform_portable(tmp_path, monkeypatch):
    from brigade import doctor as doctor_mod

    hook = tmp_path / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o644)
    assert not (hook.stat().st_mode & 0o111)
    monkeypatch.setattr(scrub, "scanner_dir", lambda: tmp_path / "missing-guard")

    results = doctor_mod._check_publish_gate(tmp_path)
    details = [item[2] for item in results]
    joined = "\n".join(details)
    assert any("not executable" in detail for detail in details)
    _assert_no_bypass_tokens(joined)


def test_cli_open_children_and_mcp_init_contracts_via_main(tmp_path, monkeypatch, capsys):
    """Smoke the argv path for two of the highest-traffic refusals."""
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 16, 0, 0, tzinfo=timezone.utc),
    )
    parent = _add(tmp_path, "Epic")
    child = _add(tmp_path, "Child")
    assert (
        work_cmd.task_edge_add(target=tmp_path, edge_type="parent-child", source=parent["id"], target_id=child["id"])
        == 0
    )
    capsys.readouterr()
    assert cli.main(["work", "task", "done", parent["id"], "--target", str(tmp_path), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "parent has open children"
    _assert_no_bypass_tokens(json.dumps(payload))

    assert mcp_cmd.init(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert cli.main(["mcp", "init", "--target", str(tmp_path), "--json"]) == 3
    mcp_payload = json.loads(capsys.readouterr().out)
    assert mcp_payload["reason"] == "already_exists"
    assert "leaving it unchanged" in mcp_payload["error"]
    _assert_no_bypass_tokens(json.dumps(mcp_payload))
