"""TDD tests for `brigade model scorecard` (read-only run-artifact aggregation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli
from brigade import model_scorecard


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_run(
    run_dir: Path,
    *,
    task: str = "build feature",
    status: str = "ok",
    started_at: str = "2026-05-26T14:00:00Z",
    duration_seconds: float = 2.0,
    orchestrator: str = "chef",
    agents: dict | None = None,
    results: list | None = None,
    ground_truth: dict | None = None,
    include_worker_results: bool = True,
) -> Path:
    """Write a realistic run artifact tree under run_dir."""
    run_dir.mkdir(parents=True, exist_ok=True)
    if agents is None:
        agents = {
            "chef": {"cli": "codex", "model": None, "role": "plan and synthesize"},
            "coder": {"cli": "claude", "model": "claude-fable-5", "role": "write code"},
        }
    _write_json(
        run_dir / "run.json",
        {
            "task": task,
            "status": status,
            "started_at": started_at,
            "duration_seconds": duration_seconds,
            "orchestrator": orchestrator,
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": orchestrator,
            "max_workers": 2,
            "timeout_seconds": 600.0,
            "allow_models": [],
            "agents": agents,
        },
    )
    if include_worker_results:
        payload: dict = {
            "results": results
            if results is not None
            else [
                {
                    "worker": "coder",
                    "task": "implement it",
                    "ok": True,
                    "detail": "",
                    "text": "done",
                }
            ],
        }
        if ground_truth is not None:
            payload["ground_truth"] = ground_truth
        else:
            payload["ground_truth"] = {
                "available": True,
                "cwd": "/repo",
                "diffstat": " tracked.txt | 1 +\n 1 file changed, 1 insertion(+)",
                "changed_files": ["tracked.txt"],
                "untracked_files": [],
                "patch_ref": "changes.patch",
            }
        _write_json(run_dir / "worker-results.json", payload)
    return run_dir


def _runs_root(target: Path) -> Path:
    root = target / ".brigade" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def test_aggregates_by_cli_and_model_with_missing_model_as_cli_alone(tmp_path):
    runs = _runs_root(tmp_path)
    _write_run(
        runs / "run-a",
        agents={
            "chef": {"cli": "codex", "model": None, "role": "plan"},
            "coder": {"cli": "claude", "model": "claude-fable-5", "role": "code"},
            "helper": {"cli": "ollama:llama3.3", "role": "code"},  # model omitted
        },
        results=[
            {"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "x"},
            {"worker": "helper", "task": "b", "ok": False, "detail": "fail", "text": ""},
        ],
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    by_label = {row.label: row for row in card.models}

    assert "codex" in by_label  # missing model → cli alone
    assert "claude/claude-fable-5" in by_label
    assert "ollama:llama3.3" in by_label
    assert by_label["codex"].model is None
    assert by_label["claude/claude-fable-5"].model == "claude-fable-5"
    assert by_label["claude/claude-fable-5"].cli == "claude"


def test_runs_participated_worker_and_orchestrator_seats(tmp_path):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": "gpt-5", "role": "plan"},
        "coder": {"cli": "claude", "model": "claude-fable-5", "role": "code"},
    }
    _write_run(runs / "run-1", started_at="2026-05-26T14:00:00Z", duration_seconds=10.0, agents=agents)
    _write_run(runs / "run-2", started_at="2026-05-27T14:00:00Z", duration_seconds=20.0, agents=agents)
    # Second run reuses same models; chef is orchestrator, coder is worker once each.

    card = model_scorecard.build_scorecard(target=tmp_path)
    by_label = {row.label: row for row in card.models}

    chef = by_label["codex/gpt-5"]
    assert chef.runs == 2
    assert chef.orchestrator_seats == 2
    assert chef.worker_seats == 0

    coder = by_label["claude/claude-fable-5"]
    assert coder.runs == 2
    assert coder.orchestrator_seats == 0
    assert coder.worker_seats == 2
    assert coder.seats == 2


def test_worker_ok_count_and_ok_rate(tmp_path):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
    }
    _write_run(
        runs / "ok-run",
        agents=agents,
        results=[{"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "ok"}],
    )
    _write_run(
        runs / "fail-run",
        started_at="2026-05-27T14:00:00Z",
        agents=agents,
        results=[{"worker": "coder", "task": "b", "ok": False, "detail": "err", "text": ""}],
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    coder = next(r for r in card.models if r.label == "claude/m1")
    assert coder.worker_seats == 2
    assert coder.worker_ok == 1
    assert coder.ok_rate == pytest.approx(0.5)


def test_suspected_no_op_flag_per_run_count_per_model(tmp_path):
    """suspected no-op = worker ok AND ground_truth.available AND empty changed_files."""
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
        "helper": {"cli": "claude", "model": "m1", "role": "code"},
    }
    # No-op run: two ok workers same model, empty changed_files → count once per model per run
    _write_run(
        runs / "noop",
        agents=agents,
        results=[
            {"worker": "coder", "task": "a", "ok": True, "detail": "", "text": "x"},
            {"worker": "helper", "task": "b", "ok": True, "detail": "", "text": "y"},
        ],
        ground_truth={
            "available": True,
            "cwd": "/repo",
            "diffstat": "",
            "changed_files": [],
            "untracked_files": [],
            "patch_ref": None,
        },
    )
    # Real work: ok worker but files changed → not a no-op
    _write_run(
        runs / "real",
        started_at="2026-05-27T14:00:00Z",
        agents=agents,
        results=[{"worker": "coder", "task": "c", "ok": True, "detail": "", "text": "z"}],
        ground_truth={
            "available": True,
            "cwd": "/repo",
            "diffstat": "f | 1 +",
            "changed_files": ["f"],
            "untracked_files": [],
        },
    )
    # Ok but GT unavailable → not a no-op
    _write_run(
        runs / "no-gt",
        started_at="2026-05-28T14:00:00Z",
        agents=agents,
        results=[{"worker": "coder", "task": "d", "ok": True, "detail": "", "text": "z"}],
        ground_truth={
            "available": False,
            "cwd": "/tmp",
            "diffstat": "",
            "changed_files": [],
            "reason": "not a git worktree",
        },
    )
    # Failed worker with empty changes → not a no-op (needs worker ok)
    _write_run(
        runs / "fail",
        started_at="2026-05-29T14:00:00Z",
        agents=agents,
        results=[{"worker": "coder", "task": "e", "ok": False, "detail": "x", "text": ""}],
        ground_truth={
            "available": True,
            "cwd": "/repo",
            "diffstat": "",
            "changed_files": [],
        },
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    coder = next(r for r in card.models if r.label == "claude/m1")
    assert coder.suspected_no_op == 1


def test_duration_total_and_mean_for_participated_runs(tmp_path):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
    }
    _write_run(runs / "r1", duration_seconds=10.0, agents=agents)
    _write_run(
        runs / "r2",
        started_at="2026-05-27T14:00:00Z",
        duration_seconds=30.0,
        agents=agents,
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    coder = next(r for r in card.models if r.label == "claude/m1")
    assert coder.total_duration_seconds == pytest.approx(40.0)
    assert coder.mean_duration_seconds == pytest.approx(20.0)
    assert coder.runs == 2


def test_first_and_last_seen_timestamps(tmp_path):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
    }
    _write_run(runs / "early", started_at="2026-05-20T10:00:00Z", agents=agents)
    _write_run(runs / "mid", started_at="2026-05-25T12:00:00Z", agents=agents)
    _write_run(runs / "late", started_at="2026-05-30T18:00:00Z", agents=agents)

    card = model_scorecard.build_scorecard(target=tmp_path)
    coder = next(r for r in card.models if r.label == "claude/m1")
    assert coder.first_seen == "2026-05-20T10:00:00Z"
    assert coder.last_seen == "2026-05-30T18:00:00Z"


def test_malformed_and_partial_dirs_skipped_never_crash(tmp_path):
    runs = _runs_root(tmp_path)
    _write_run(runs / "good", started_at="2026-05-26T14:00:00Z")

    # empty dir
    (runs / "empty").mkdir()
    # missing roster
    bad_run = runs / "no-roster"
    bad_run.mkdir()
    _write_json(bad_run / "run.json", {"task": "x", "status": "ok", "started_at": "2026-05-26T14:00:00Z"})
    # invalid run.json
    bad_json = runs / "bad-json"
    bad_json.mkdir()
    (bad_json / "run.json").write_text("not json\n")
    (bad_json / "roster.json").write_text("{}\n")
    # file instead of dir (ignored, not a run dir)
    (runs / "not-a-dir.txt").write_text("x\n")
    # partial: run.json not an object
    partial = runs / "partial"
    partial.mkdir()
    _write_json(partial / "run.json", ["not", "an", "object"])
    _write_json(partial / "roster.json", {"orchestrator": "chef", "agents": {}})

    card = model_scorecard.build_scorecard(target=tmp_path)
    assert card.scanned == 1
    assert card.skipped >= 3
    assert len(card.models) >= 1
    assert all(isinstance(s.path, str) and s.reason for s in card.skipped_dirs)


def test_since_filter_yyyy_mm_dd(tmp_path):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
    }
    _write_run(runs / "old", started_at="2026-05-01T12:00:00Z", agents=agents)
    _write_run(runs / "on-day", started_at="2026-05-15T00:00:00Z", agents=agents)
    _write_run(runs / "new", started_at="2026-05-20T12:00:00Z", agents=agents)

    card = model_scorecard.build_scorecard(target=tmp_path, since="2026-05-15")
    coder = next(r for r in card.models if r.label == "claude/m1")
    assert coder.runs == 2
    assert coder.first_seen == "2026-05-15T00:00:00Z"


def test_multi_runs_dir(tmp_path):
    a = tmp_path / "a" / "runs"
    b = tmp_path / "b" / "runs"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    agents_a = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m-a", "role": "code"},
    }
    agents_b = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m-b", "role": "code"},
    }
    _write_run(a / "r1", agents=agents_a)
    _write_run(b / "r2", agents=agents_b, started_at="2026-05-27T14:00:00Z")

    card = model_scorecard.build_scorecard(runs_dirs=[a, b])
    labels = {row.label for row in card.models}
    assert "claude/m-a" in labels
    assert "claude/m-b" in labels
    assert card.scanned == 2


def test_json_stable_shape(tmp_path):
    runs = _runs_root(tmp_path)
    _write_run(runs / "r1")

    card = model_scorecard.build_scorecard(target=tmp_path)
    payload = model_scorecard.scorecard_to_dict(card)

    assert set(payload.keys()) == {"models", "scanned", "skipped", "skipped_dirs"}
    assert isinstance(payload["models"], list)
    assert payload["scanned"] == 1
    assert payload["skipped"] == 0
    row = payload["models"][0]
    expected_keys = {
        "cli",
        "model",
        "label",
        "runs",
        "worker_seats",
        "orchestrator_seats",
        "seats",
        "worker_ok",
        "ok_rate",
        "suspected_no_op",
        "total_duration_seconds",
        "mean_duration_seconds",
        "first_seen",
        "last_seen",
    }
    assert set(row.keys()) == expected_keys
    # sort_keys stability: dumps with sort_keys matches key order when re-parsed fields present
    dumped = json.dumps(payload, sort_keys=True)
    assert json.loads(dumped) == payload


def test_models_sorted_ok_rate_desc_then_seats_desc(tmp_path):
    runs = _runs_root(tmp_path)
    # model A: 100% ok, 1 seat
    _write_run(
        runs / "a1",
        agents={
            "chef": {"cli": "codex", "model": None, "role": "plan"},
            "coder": {"cli": "claude", "model": "high-rate", "role": "code"},
        },
        results=[{"worker": "coder", "task": "t", "ok": True, "detail": "", "text": "x"}],
    )
    # model B: 50% ok, 2 seats (lower rate, more seats)
    agents_b = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "w1": {"cli": "claude", "model": "mid-rate", "role": "code"},
        "w2": {"cli": "claude", "model": "mid-rate", "role": "code"},
    }
    _write_run(
        runs / "b1",
        started_at="2026-05-27T14:00:00Z",
        agents=agents_b,
        results=[
            {"worker": "w1", "task": "t", "ok": True, "detail": "", "text": "x"},
            {"worker": "w2", "task": "t", "ok": False, "detail": "e", "text": ""},
        ],
    )
    # model C: 100% ok, 2 seats — should rank above A (same rate, more seats)
    agents_c = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "w1": {"cli": "claude", "model": "high-seats", "role": "code"},
        "w2": {"cli": "claude", "model": "high-seats", "role": "code"},
    }
    _write_run(
        runs / "c1",
        started_at="2026-05-28T14:00:00Z",
        agents=agents_c,
        results=[
            {"worker": "w1", "task": "t", "ok": True, "detail": "", "text": "x"},
            {"worker": "w2", "task": "t", "ok": True, "detail": "", "text": "y"},
        ],
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    claude_rows = [r for r in card.models if r.cli == "claude"]
    labels = [r.label for r in claude_rows]
    assert labels == ["claude/high-seats", "claude/high-rate", "claude/mid-rate"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_scorecard_text_table_and_footer(tmp_path, capsys):
    runs = _runs_root(tmp_path)
    _write_run(runs / "r1")
    (runs / "empty").mkdir()

    assert cli.main(["model", "scorecard", "--target", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "claude/claude-fable-5" in out or "claude" in out
    assert "codex" in out
    assert "scanned" in out.lower()
    assert "skipped" in out.lower()


def test_cli_scorecard_json(tmp_path, capsys):
    runs = _runs_root(tmp_path)
    _write_run(runs / "r1")

    assert cli.main(["model", "scorecard", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "models" in payload
    assert payload["scanned"] == 1


def test_cli_scorecard_since(tmp_path, capsys):
    runs = _runs_root(tmp_path)
    agents = {
        "chef": {"cli": "codex", "model": None, "role": "plan"},
        "coder": {"cli": "claude", "model": "m1", "role": "code"},
    }
    _write_run(runs / "old", started_at="2026-04-01T00:00:00Z", agents=agents)
    _write_run(runs / "new", started_at="2026-06-01T00:00:00Z", agents=agents)

    assert cli.main(["model", "scorecard", "--target", str(tmp_path), "--since", "2026-05-01", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    claude = next(m for m in payload["models"] if m["label"] == "claude/m1")
    assert claude["runs"] == 1


def test_cli_scorecard_multi_runs_dir(tmp_path, capsys):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _write_run(
        a / "r1",
        agents={
            "chef": {"cli": "codex", "model": None, "role": "plan"},
            "coder": {"cli": "claude", "model": "from-a", "role": "code"},
        },
    )
    _write_run(
        b / "r2",
        started_at="2026-05-27T14:00:00Z",
        agents={
            "chef": {"cli": "codex", "model": None, "role": "plan"},
            "coder": {"cli": "claude", "model": "from-b", "role": "code"},
        },
    )

    assert (
        cli.main(
            [
                "model",
                "scorecard",
                "--runs-dir",
                str(a),
                "--runs-dir",
                str(b),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    labels = {m["label"] for m in payload["models"]}
    assert "claude/from-a" in labels
    assert "claude/from-b" in labels


def test_cli_verbose_lists_skipped_dirs_and_reasons(tmp_path, capsys):
    runs = _runs_root(tmp_path)
    _write_run(runs / "good")
    (runs / "empty").mkdir()
    bad = runs / "bad-json"
    bad.mkdir()
    (bad / "run.json").write_text("not json\n")

    assert cli.main(["model", "scorecard", "--target", str(tmp_path), "--verbose"]) == 0
    out = capsys.readouterr().out
    assert "empty" in out
    assert "bad-json" in out
    # reason text present
    assert "missing" in out.lower() or "invalid" in out.lower() or "run.json" in out.lower()


def test_cli_is_read_only_no_writes(tmp_path):
    runs = _runs_root(tmp_path)
    _write_run(runs / "r1")
    before = {p.relative_to(tmp_path): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    assert cli.main(["model", "scorecard", "--target", str(tmp_path), "--json"]) == 0

    after_files = {p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file()}
    assert after_files == set(before)
    for rel, mtime in before.items():
        assert (tmp_path / rel).stat().st_mtime_ns == mtime


def test_invalid_since_exits_nonzero(tmp_path, capsys):
    _runs_root(tmp_path)
    code = cli.main(["model", "scorecard", "--target", str(tmp_path), "--since", "not-a-date"])
    assert code != 0
    err = capsys.readouterr().err
    assert "since" in err.lower() or "YYYY-MM-DD" in err


def test_runs_dir_is_additive_with_target(tmp_path):
    # --runs-dir roots are extra; the target's default runs dir still scans.
    target = tmp_path / "proj"
    extra = tmp_path / "elsewhere" / "runs"
    _write_run(target / ".brigade" / "runs" / "r-target")
    _write_run(extra / "r-extra", started_at="2026-05-27T14:00:00Z")

    card = model_scorecard.build_scorecard(target=target, runs_dirs=[extra])
    assert card.scanned == 2


def test_noop_ignores_brigade_housekeeping_changes(tmp_path):
    # A run whose only ground-truth changes live under .brigade/ (roster
    # edits, artifacts) did no task work: still a suspected no-op.
    runs = _runs_root(tmp_path)
    _write_run(
        runs / "r1",
        results=[{"worker": "coder", "task": "t", "ok": True, "detail": "", "text": "done"}],
        ground_truth={
            "available": True,
            "cwd": str(tmp_path),
            "diffstat": ".brigade/roster.toml | 2 +-",
            "changed_files": [".brigade/roster.toml"],
        },
    )

    card = model_scorecard.build_scorecard(target=tmp_path)
    by_label = {row.label: row for row in card.models}
    assert by_label["claude/claude-fable-5"].suspected_no_op == 1
