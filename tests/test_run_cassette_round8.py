"""Regressions for secret boundaries and effective sandbox identity."""

import pytest

from brigade import agents, message_envelope, proc
from brigade.roster import Agent, Roster
from brigade.run_seat import SeatInvoker
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording


@pytest.mark.parametrize(
    "field,limit,fill,transport",
    [
        ("text", message_envelope.MESSAGE_WRAP_MAX_BYTES, "x", "cli"),
        ("text", message_envelope.MESSAGE_WRAP_MAX_BYTES, "é", "cli"),
        ("detail", proc.MAX_CAPTURE_BYTES, "x", "cli"),
        ("stop_reason", 128, "x", "acpx"),
    ],
)
def test_resolved_secret_is_scrubbed_before_receipt_truncation(tmp_path, monkeypatch, field, limit, fill, transport):
    needle = "cassette-boundary-value-0123456789"
    monkeypatch.setenv("CASSETTE_TEST_TOKEN", needle)
    agent = Agent(name="worker", cli="codex", role="write", env={"API_KEY_REF": "CASSETTE_TEST_TOKEN"})
    prefix = fill * ((limit - 16) // len(fill.encode("utf-8")))
    result = agents.AgentResult(**{"text": "answer", "ok": True, "transport": transport, field: prefix + needle})
    run = tmp_path / ".brigade" / "runs" / "current"

    assert _record(run, agent=agent, result=result) is not None

    docs = _documents(run)
    body = next(d["result"] for d in docs if d["privacy_class"] == "private")
    assert body[field] == prefix + "[redacted]"
    assert len(body[field].encode("utf-8")) <= limit
    assert all(needle[:16] not in path.read_text() for path in (run / "events" / "cassettes").glob("*.json"))
    assert getattr(result, field) == prefix + needle


def test_seat_invoker_key_tracks_execution_instead_of_roster_sandbox(tmp_path, monkeypatch):
    launch_modes = []

    def worker(cli, prompt, **kwargs):
        argv = agents.build_argv(cli, prompt, read_only=kwargs["read_only"], sandbox=kwargs.get("sandbox"))
        launch_modes.append(argv[argv.index("--sandbox") + 1])
        return agents.AgentResult(text="answer", ok=True)

    monkeypatch.setattr(agents, "run_agent", worker)
    keys = []
    for i, sandbox in enumerate((None, "danger-full-access", "workspace-write")):
        run = tmp_path / ".brigade" / "runs" / f"run-{i}"
        invoker = SeatInvoker(
            roster=Roster(
                orchestrator="chef",
                agents={"worker": Agent(name="worker", cli="codex", role="write")},
                sandbox=sandbox,
            ),
            cwd=tmp_path,
            run_id=run.name,
            process_registry=proc.ProcessRegistry(),
            output_dir=run,
        )
        assert invoker.invoke(seat="worker", phase="research.plan", prompt="task", timeout=10).ok
        manifest = next(d for d in _documents(run) if d["privacy_class"] == "public-metadata")
        keys.append(manifest["key"])

    assert launch_modes == ["read-only"] * 3
    assert keys[0] == keys[1] == keys[2]
