"""Issue #1756: a cleanup error racing a retained run lock must not terminalize run.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import runguard
from brigade.aboyeur import orchestrator, planning
from brigade.roster import Agent, Roster
from tests.run_test_helpers import run_aboyeur_guarded


def _cleanup_error_over_retention(error: Exception):
    @orchestrator._terminalize_run_lifecycle
    def run(*, output_dir: Path, worker: str) -> int:
        try:
            raise runguard.RetainRunLockError("failed to write terminal run receipt: disk full")
        except runguard.RetainRunLockError:
            # Implicit __context__ chaining, as when a cleanup step raises mid-retention.
            raise error  # noqa: B904

    return run


@pytest.mark.parametrize(
    "error",
    [ValueError("cleanup failed"), TimeoutError("cleanup timed out")],
    ids=["exception", "timeout"],
)
def test_cleanup_error_over_retention_leaves_run_json_untouched(tmp_path, monkeypatch, error):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    original = json.dumps({"status": "dispatching", "started_at": "2026-07-19T12:00:00Z"}) + "\n"
    (output_dir / "run.json").write_text(original)
    terminations: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        _cleanup_error_over_retention(error)(output_dir=output_dir, worker="coder")

    assert runguard.retained_run_lock_error(excinfo.value) is not None
    assert terminations == []
    assert (output_dir / "run.json").read_text() == original


@pytest.mark.parametrize("error_type", [ValueError, TimeoutError], ids=["exception", "timeout"])
def test_dispatch_cleanup_error_over_retention_does_not_terminalize(tmp_path, monkeypatch, error_type):
    retained = runguard.RetainRunLockError("failed to write terminal run receipt: disk full")
    terminations: list[dict[str, object]] = []

    def dispatch(*args, **kwargs):
        try:
            raise retained
        except runguard.RetainRunLockError:
            raise error_type("dispatch cleanup failed")  # noqa: B904

    monkeypatch.setattr(planning, "dispatch", dispatch)
    # Deliver the cleanup error to run's dispatch handlers, without the inner
    # cancellation boundary promoting its retention context first.
    monkeypatch.setattr(
        planning, "_call_with_process_registry", lambda function, *args, **kwargs: function(*args, **kwargs)
    )
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
    )

    with pytest.raises(runguard.RetainRunLockError) as excinfo:
        run_aboyeur_guarded(
            "build feature",
            roster,
            worker="coder",
            output_dir=tmp_path / "run",
            route_enabled=False,
        )

    assert excinfo.value is retained
    assert terminations == []


@pytest.mark.parametrize(
    ("error", "status"),
    [(ValueError("boom"), "failed"), (TimeoutError("slow"), "timeout")],
    ids=["exception", "timeout"],
)
def test_error_without_retention_still_terminalizes(tmp_path, monkeypatch, error, status):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(json.dumps({"status": "dispatching"}) + "\n")
    terminations: list[dict[str, object]] = []
    monkeypatch.setattr(
        orchestrator.artifacts,
        "record_run_termination",
        lambda *args, **kwargs: terminations.append(kwargs),
    )

    @orchestrator._terminalize_run_lifecycle
    def run(*, output_dir: Path, worker: str) -> int:
        raise error

    with pytest.raises(type(error)):
        run(output_dir=output_dir, worker="coder")

    assert [entry["status"] for entry in terminations] == [status]
