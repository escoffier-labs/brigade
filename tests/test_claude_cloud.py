"""Tests for the Claude Code Cloud disabled inventory contract (#Task3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import claude_cloud, cloud_tracker, proc


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


CLAUDE_AGENTS_JSON = """[
  {
    "id": "agent-claude-1",
    "name": "brigade-worker",
    "status": "active",
    "workspace": "/home/user/repo"
  },
  {
    "id": "agent-claude-2",
    "name": "reviewer",
    "status": "idle",
    "workspace": "/home/user/other"
  }
]"""


CLAUDE_21246_AGENTS_JSON = """[
  {
    "sessionId": "sess-claude-1",
    "cwd": "/home/user/repo",
    "kind": "chat",
    "name": "brigade-worker",
    "pid": 1234,
    "startedAt": "2026-08-26T12:00:00Z"
  },
  {
    "sessionId": "sess-claude-2",
    "cwd": "/home/user/other",
    "status": "idle",
    "name": "reviewer"
  }
]"""


class TestParse:
    def test_parse_agents_reads_json_array(self):
        agents = claude_cloud.parse_agents(CLAUDE_AGENTS_JSON)
        assert len(agents) == 2
        assert agents[0]["id"] == "agent-claude-1"
        assert agents[0]["state"] == "active"
        assert agents[1]["state"] == "idle"

    def test_parse_agents_returns_empty_on_malformed_json(self):
        assert claude_cloud.parse_agents("not json") == []

    def test_parse_agents_returns_empty_on_non_array(self):
        assert claude_cloud.parse_agents('{"agents": []}') == []

    def test_parse_agents_prefers_session_id_and_cwd(self):
        agents = claude_cloud.parse_agents(CLAUDE_21246_AGENTS_JSON)
        assert len(agents) == 2
        assert agents[0]["id"] == "sess-claude-1"
        assert agents[0]["workspace"] == "/home/user/repo"
        assert agents[1]["id"] == "sess-claude-2"
        assert agents[1]["workspace"] == "/home/user/other"
        assert agents[1]["state"] == "idle"

    def test_parse_agents_falls_back_to_legacy_id_and_workspace(self):
        legacy = """[{"id": "legacy-1", "status": "active", "workspace": "/legacy"}]"""
        agents = claude_cloud.parse_agents(legacy)
        assert agents[0]["id"] == "legacy-1"
        assert agents[0]["workspace"] == "/legacy"
        assert agents[0]["state"] == "active"


class TestInventory:
    def test_list_agents_runs_claude_agents_json_all(self, monkeypatch):
        runs = []

        def fake_run(command, *args, **kwargs):
            runs.append(command)
            return proc.Result(code=0, stdout=CLAUDE_AGENTS_JSON, stderr="")

        monkeypatch.setattr(claude_cloud.subprocess, "run", fake_run)
        agents = claude_cloud.list_agents()
        assert len(agents) == 2
        assert runs[0][:4] == ["claude", "agents", "--json", "--all"]

    def test_list_agents_returns_empty_when_claude_missing(self, monkeypatch):
        def fake_run(command, *args, **kwargs):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(claude_cloud.subprocess, "run", fake_run)
        assert claude_cloud.list_agents() == []

    def test_launch_is_disabled_by_policy(self):
        result = claude_cloud.launch_agent("repo", "prompt")
        assert not result.ok
        assert result.reason == "disabled-by-policy"
        assert "claude --cloud" not in str(result)


class TestTrackerContract:
    def test_claude_cloud_register_and_adopt_accepted(self, tmp_path: Path):
        entry = cloud_tracker.register(
            tmp_path,
            provider="claude-cloud",
            task_id="agent-claude-1",
            label="claude-task",
            prompt_hash="sha256:aa",
        )
        assert entry["provider"] == "claude-cloud"
        adopted = cloud_tracker.adopt(
            tmp_path,
            provider="claude-cloud",
            branch="claude/adopted",
            label="adopted",
        )
        assert adopted["provider"] == "claude-cloud"
        assert adopted["source"] == "adopt-branch"

    def test_status_reports_claude_cloud_disabled_by_policy(self, tmp_path: Path, capsys, monkeypatch):
        cloud_tracker.register(
            tmp_path,
            provider="claude-cloud",
            task_id="agent-claude-1",
            label="claude-task",
            prompt_hash="sha256:aa",
            dispatched_at=_iso(),
        )
        monkeypatch.setattr(
            cloud_tracker,
            "observe_providers",
            lambda target, **k: ({}, {"branches": [], "prs": []}, False),
        )
        from brigade import cli

        rc = cli.main(["run", "cloud", "status", "--target", str(tmp_path), "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sources"]["claude-cloud"]["authority"] == "disabled-by-policy"
        assert payload["sources"]["claude-cloud"]["wired"] is False


class TestNoLiveInvocation:
    def test_list_agents_returns_empty_when_claude_not_on_path(self, monkeypatch):
        """A missing executable returns empty rows without raising."""

        def raise_missing(*args, **kwargs):
            raise FileNotFoundError("claude not found")

        monkeypatch.setattr(claude_cloud.subprocess, "run", raise_missing)
        assert claude_cloud.list_agents() == []
