"""Regressions for cassette purge, partial redaction, and fallback failures."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace

import pytest

from brigade import acpx_adapter, agents, cli, run_audit, run_cassette, run_journal, run_redaction, run_transport
from brigade.roster import Agent, Roster
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_cassette_privacy import _finish_recording
from tests.test_run_redaction import _authority_run
from tests.test_run_transport_recovery import _invalid_final, _recovery_roster


@pytest.mark.skipif(
    os.name != "posix",
    reason="Safe redaction transactions require POSIX dir_fd and flock. Tracking #1894 (cassette hardening)",
)
def test_partial_redaction_tombstones_every_purged_cassette_reference(tmp_path):
    run, snapshot, _ = _authority_run(tmp_path)
    refs = [
        _record(run, task=task, result=agents.AgentResult(text="exposed-cassette-value", ok=True))
        for task in ("first task", "second task")
    ]
    _finish_recording(run, snapshot)
    before = run_journal.read_journal(run / "events" / "lifecycle.jsonl").events
    first = next(e for e in before if e.event_type == run_cassette.EVENT_TYPE)
    run_redaction.redact_journal(
        run,
        sequence_start=first.sequence,
        sequence_end=first.sequence,
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert not list((run / "events" / "cassettes").iterdir())
    journal = run_journal.read_journal(run / "events" / "lifecycle.jsonl")
    assert not journal.chain_errors
    references = [e.payload for e in journal.events if e.event_type == run_cassette.EVENT_TYPE]
    assert references == [run_cassette.REDACTED_REFERENCE] * 2
    assert all(ref["sha256"] not in str(journal.events) for ref in refs)
    assert journal.events[-1].event_type == "run.redaction.recorded"
    assert run_audit.audit_run(run).result == "match"


@pytest.mark.parametrize("component", ["run", "runs-root", "workspace"])
def test_purge_cli_refuses_symlink_components(tmp_path, component, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    run = workspace / ".brigade" / "runs" / "current"
    _record(run)
    sidecars = {p.name: p.read_bytes() for p in (run / "events" / "cassettes").iterdir()}
    if component == "run":
        link = run.parent / "linked"
        target = run
        requested = "linked"
    elif component == "runs-root":
        link = tmp_path / "linked-runs"
        target = run.parent
        requested = str(link / run.name)
    else:
        link = tmp_path / "linked-workspace"
        target = workspace
        requested = "current"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.chdir(link if component == "workspace" else workspace)
    # An explicit cwd preserves the original path on hosts that canonicalize chdir.
    args = ["runs", "cassette", "purge", requested]
    if component == "workspace":
        args += ["--cwd", str(link)]
    assert cli.main(args) == 2
    assert "cassette purge refused unsafe sidecars" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in (run / "events" / "cassettes").iterdir()} == sidecars


@pytest.mark.parametrize("transport", ["cli", "acpx", "codex-app-server"])
def test_fallback_adapter_exception_records_active_seat_and_transport(tmp_path, monkeypatch, transport):
    roster = _recovery_roster()
    fallback = Agent(
        name="fallback",
        cli="cursor" if transport == "acpx" else "codex",
        role="review",
        model="test-model",
        transport="acpx" if transport == "acpx" else "direct",
    )
    roster = replace(
        roster,
        agents={
            "grok-review": replace(roster.agents["grok-review"], invalid_final_fallback=fallback.name),
            fallback.name: fallback,
        },
    )
    calls = []
    sent_prompts = []

    def invoke(cli_ref, prompt, **kwargs):
        calls.append(cli_ref)
        if cli_ref == "grok":
            return _invalid_final()
        sent_prompts.append(prompt)
        raise OSError("fallback pipe failed")

    def fail(*args, **kwargs):
        calls.append(fallback.cli)
        sent_prompts.append(args[3] if transport == "codex-app-server" else args[0])
        raise OSError("fallback pipe failed")

    monkeypatch.setattr(agents, "run_agent", invoke)
    monkeypatch.setattr(acpx_adapter, "run_cursor", fail)
    run = tmp_path / ".brigade" / "runs" / "current"
    result = run_transport.dispatch(
        [run_transport.Assignment(worker="grok-review", task="Review the diff.")],
        Roster(orchestrator="chef", agents=roster.agents),
        build_prompt=lambda agent, assignment, **kw: f"prompt for {agent.name}",
        run_appserver_worker=fail,
        event_writer=lambda *a, **kw: None,
        appserver=object() if transport == "codex-app-server" else None,
        codex_transport="app-server" if transport == "codex-app-server" else "exec",
        cwd=tmp_path,
        output_dir=run,
        read_only=True,
        direct=True,
    )[0]
    assert calls == ["grok", "grok", fallback.cli]
    assert not result.ok and result.detail == "fallback pipe failed"
    assert result.worker == "grok-review"
    docs = _documents(run)
    manifest = next(d for d in docs if d["privacy_class"] == "public-metadata")
    identity = {
        "schema_version": 1,
        "seat": {
            "name": fallback.name,
            "cli": fallback.cli,
            "model": fallback.model,
            "reasoning": None,
            "sandbox": "read-only",
            "transport": fallback.transport,
            **(
                {"codex_transport": "app-server" if transport == "codex-app-server" else "exec"}
                if fallback.cli == "codex"
                else {}
            ),
        },
        "coordinator_prompt_fingerprint": run_audit.composed_prompt_fingerprint(
            worker=fallback.name,
            role=fallback.role,
            task="Review the diff.",
            read_only=True,
            skill_policy=None,
        ),
    }
    assert (
        manifest["key"]
        == hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    body = next(d["result"] for d in docs if d["privacy_class"] == "private")
    assert body["transport"] == transport
    assert body["requested_model"] == fallback.model
    assert len(sent_prompts) == 1 and sent_prompts[0].startswith("prompt for fallback")
    assert manifest["full_prompt_sha256"] == hashlib.sha256(sent_prompts[0].encode()).hexdigest()
