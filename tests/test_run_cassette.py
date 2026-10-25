"""Worker cassettes: identity, privacy, policy, caps, and lifecycle neutrality."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agents, config, run_journal, runguard
from brigade.selection import Selection
from brigade.roster import Agent


@pytest.fixture(autouse=True)
def auto_recording(tmp_path):
    """Body-sensitive cassette tests explicitly opt into private recording."""
    config.write_config(
        tmp_path,
        config.Config(version=1, selection=Selection(depth="repo", harnesses=["codex"]), run_record="auto"),
    )


def _record(run_dir: Path, **kwargs):
    from brigade import run_cassette

    # A live run always has its directory; publication never recreates one.
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = kwargs.pop(
        "cwd",
        next(
            (parent for parent in list(run_dir.parents)[:3] if config.config_path(parent).is_file()),
            run_dir.parent.parent.parent,
        ),
    )
    ownership = (
        nullcontext()
        if runguard.is_active_run_owner(workspace, run_dir)
        else runguard.run_lock(workspace, run_dir=run_dir)
    )
    with ownership:
        return run_cassette.record_result(
            run_dir=run_dir,
            workspace=workspace,
            agent=kwargs.pop("agent", Agent(name="worker", cli="codex", role="write", model="test-model")),
            task=kwargs.pop("task", "coordinator task"),
            prompt=kwargs.pop("prompt", "injected brief and private prompt"),
            result=kwargs.pop("result", agents.AgentResult(text="private-worker-answer", ok=True, transport="cli")),
            read_only=True,
            sandbox="read-only",
            **kwargs,
        )


def _documents(run_dir):
    return [json.loads(p.read_text()) for p in (run_dir / "events" / "cassettes").glob("*.json")]


def test_auto_records_classified_body_but_only_reference_in_journal(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_journal.ensure_journal(run_dir / "events" / "lifecycle.jsonl")
    ref = _record(run_dir)
    assert set(ref) == {"path", "sha256", "media_type", "byte_size", "privacy_class"}
    docs = _documents(run_dir)
    assert {d["privacy_class"] for d in docs} == {"private", "scrubbed", "public-metadata"}
    assert next(d for d in docs if d["privacy_class"] == "private")["result"]["text"] == "private-worker-answer"
    journal = run_dir / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    assert len(report.events) == 1
    assert report.events[0].payload == ref
    assert journal.stat().st_size < 1024
    assert "private-worker-answer" not in journal.read_text()
    assert "injected brief" not in journal.read_text()
    for path in (run_dir / "events" / "cassettes").glob("*.json"):
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem


def test_key_ignores_brief_but_tracks_coordinator_and_seat(tmp_path):
    from brigade import run_cassette

    agent = Agent(name="worker", cli="codex", role="write", model="test-model")
    keys = []
    for i, overrides in enumerate(
        ({}, {"prompt": "different brief"}, {"task": "different task"}, {"agent": replace(agent, model="other-model")})
    ):
        run_dir = tmp_path / ".brigade" / "runs" / f"20261020-12000{i}-cassette"
        ref = _record(run_dir, **overrides)
        keys.append(json.loads((run_dir / ref["path"]).read_text())["key"])
    assert keys[0] == keys[1]
    assert len(set(keys)) == 3
    assert run_cassette.SCHEMA_VERSION == 1


@pytest.mark.parametrize("mode,classes", [("off", set()), ("metadata-only", {"scrubbed", "public-metadata"})])
def test_record_policy(tmp_path, mode, classes):
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    (tmp_path / ".brigade" / "config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "depth": "repo",
                "harnesses": ["codex"],
                "run": {"record": mode},
            }
        )
    )
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run_dir, cwd=tmp_path)
    assert {d["privacy_class"] for d in _documents(run_dir)} == classes
    assert "private-worker-answer" not in json.dumps(_documents(run_dir))


def test_write_time_scrub_resolved_overrides(tmp_path, monkeypatch):
    needle = "cassette-credential-value"
    monkeypatch.setenv("CASSETTE_TEST_TOKEN", needle)
    agent = Agent(name="worker", cli="codex", role="write", env={"API_KEY_REF": "CASSETTE_TEST_TOKEN"})
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run_dir, agent=agent, result=agents.AgentResult(text=f"prefix{needle}suffix", detail=needle, ok=True))
    assert needle not in json.dumps(_documents(run_dir))


def test_literal_env_overrides_preserve_result_text(tmp_path, monkeypatch):
    needle = "cassette-credential-value"
    monkeypatch.setenv("CASSETTE_TEST_TOKEN", needle)
    agent = Agent(name="worker", cli="codex", role="write", env={"CI": "1", "API_KEY_REF": "CASSETTE_TEST_TOKEN"})
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run_dir, agent=agent, result=agents.AgentResult(text=f"1 test passed for issue 1854: {needle}", ok=True))
    body = next(d["result"] for d in _documents(run_dir) if d["privacy_class"] == "private")
    assert body["text"] == "1 test passed for issue 1854: [redacted]"


def test_recording_stop_does_not_enable_lifecycle_journaling(tmp_path, monkeypatch):
    from brigade import run_cassette_store as store

    monkeypatch.setattr(store, "MAX_ENTRY_BYTES", 128)
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    assert _record(run_dir) is None
    assert any(d.get("status") == "stopped" for d in _documents(run_dir))
    assert not (run_dir / "events" / "lifecycle.jsonl").exists()
