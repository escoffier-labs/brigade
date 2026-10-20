"""Worker cassettes: identity, privacy, policy, caps, and lifecycle neutrality."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import agents, run_journal
from brigade.roster import Agent


def _record(run_dir: Path, **kwargs):
    from brigade import run_cassette

    return run_cassette.record_result(
        run_dir=run_dir,
        cwd=kwargs.pop("cwd", run_dir.parent.parent.parent),
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


def test_default_records_classified_body_but_only_reference_in_journal(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
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
    (tmp_path / ".brigade").mkdir()
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
