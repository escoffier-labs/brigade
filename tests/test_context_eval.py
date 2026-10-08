"""Contract tests: context_eval must read what graphtrail_delta actually writes (#1644)."""

import json
from pathlib import Path

from brigade import aboyeur, context_eval, graphtrail_delta


def _diff_node(file_path: str, name: str) -> dict:
    return {
        "file_path": file_path,
        "qualified_name": name,
        "kind": "function",
        "start_line": 1,
        "end_line": 4,
    }


def _write_real_sidecar(monkeypatch, tmp_path: Path, diff_payload: dict) -> dict:
    """Run the real sidecar writer with graphtrail's diff output stubbed."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    snapshot = run_dir / graphtrail_delta.SNAPSHOT_NAME
    snapshot.write_text("before")
    before = {
        "ok": True,
        "binary": "graphtrail",
        "db_path": str(tmp_path / ".graphtrail" / "graphtrail.db"),
        "before_snapshot_path": str(snapshot),
    }

    def fake_run(binary, db_path, command, *extra, timeout, json_output=False, stage=None):
        stdout = json.dumps(diff_payload) if command == "diff" else ""
        return {"argv": [command], "returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}

    def fake_backup(source, destination, *, timeout=None):
        Path(destination).write_text("after")

    monkeypatch.setattr(graphtrail_delta, "_run_graphtrail", fake_run)
    monkeypatch.setattr(graphtrail_delta, "_backup_sqlite", fake_backup)
    delta = graphtrail_delta.capture_after_and_diff(tmp_path, run_dir, before)
    assert delta["ok"] is True
    return delta


def test_extract_delta_files_reads_sidecar_written_by_graphtrail_delta(monkeypatch, tmp_path):
    delta = _write_real_sidecar(
        monkeypatch,
        tmp_path,
        {
            "summary": {"added_nodes": 1, "changed_nodes": 1},
            "added_nodes": [_diff_node("src/brigade/context_eval.py", "new_fn")],
            "changed_nodes": [_diff_node("tests/test_aboyeur.py", "test_it")],
            "removed_nodes": [],
        },
    )

    sidecar = json.loads(Path(delta["sidecar_path"]).read_text())
    assert "changed_nodes" not in sidecar
    assert context_eval.extract_delta_files(delta["sidecar_path"]) == [
        "src/brigade/context_eval.py",
        "tests/test_aboyeur.py",
    ]


def test_context_eval_for_run_scores_real_sidecar(monkeypatch, tmp_path):
    delta = _write_real_sidecar(
        monkeypatch,
        tmp_path,
        {
            "added_nodes": [_diff_node("src/brigade/context_eval.py", "new_fn")],
            "changed_nodes": [_diff_node("tests/test_aboyeur.py", "test_it")],
        },
    )
    brief = aboyeur.CodeGraphBrief(attached=True, text="- `tests/test_aboyeur.py:10`\n", bytes=30)

    result = aboyeur._context_eval_for_run(brief, delta)

    assert result is not None
    assert result["counts"]["delta_files"] == 2
    assert result["brief_hit_rate"] == 0.5
    assert "truncated" not in result


def test_context_eval_for_run_flags_truncated_delta_and_withholds_rate(monkeypatch, tmp_path):
    limit = graphtrail_delta.CODE_REFERENCE_NODE_LIMIT
    nodes = [_diff_node(f"src/pkg/mod_{index:03d}.py", f"fn_{index}") for index in range(limit + 5)]
    delta = _write_real_sidecar(monkeypatch, tmp_path, {"changed_nodes": nodes})
    sidecar = json.loads(Path(delta["sidecar_path"]).read_text())
    assert sidecar["code_reference_nodes_truncated"] is True
    brief = aboyeur.CodeGraphBrief(attached=True, text="- `src/pkg/mod_000.py:1`\n", bytes=30)

    result = aboyeur._context_eval_for_run(brief, delta)

    assert result is not None
    assert result["truncated"] is True
    assert result["counts"]["delta_files"] == limit
    assert result["brief_hit_rate"] is None


def test_extract_delta_files_still_reads_legacy_node_keys():
    payload = {"changed_nodes": [{"file_path": "a/b.py"}], "removed_nodes": [{"file_path": "c.py"}]}

    assert context_eval.extract_delta_files(payload) == ["a/b.py", "c.py"]


def test_extract_delta_files_prefers_code_reference_nodes():
    payload = {
        "code_reference_nodes": [_diff_node("a/b.py", "f")],
        "changed_nodes": [{"file_path": "legacy.py"}],
    }

    assert context_eval.extract_delta_files(payload) == ["a/b.py"]


def test_every_key_context_eval_reads_is_written_by_graphtrail_delta(monkeypatch, tmp_path):
    delta = _write_real_sidecar(monkeypatch, tmp_path, {"changed_nodes": [_diff_node("a.py", "f")]})
    sidecar = json.loads(Path(delta["sidecar_path"]).read_text())

    for key in context_eval.DELTA_SIDECAR_KEYS:
        assert key in sidecar


def test_evaluate_reports_precision_and_f05_next_to_recall():
    result = context_eval.evaluate(
        ["src/a.py", "src/b.py", "src/c.py", "src/d.py"],
        ["src/a.py", "src/e.py"],
    )

    assert result["brief_hit_rate"] == 0.5
    assert result["brief_precision"] == 0.25
    # F0.5 weights precision: (1.25 * 0.25 * 0.5) / (0.25 * 0.25 + 0.5)
    assert result["brief_f05"] == 0.278


def test_a_brief_that_lists_everything_scores_full_recall_but_low_precision():
    delta = ["src/a.py"]
    everything = context_eval.evaluate([f"src/{name}.py" for name in "abcdefghij"], delta)
    tight = context_eval.evaluate(["src/a.py"], delta)

    assert everything["brief_hit_rate"] == tight["brief_hit_rate"] == 1.0
    assert everything["brief_precision"] == 0.1
    assert tight["brief_precision"] == 1.0
    assert tight["brief_f05"] > everything["brief_f05"]


def test_empty_brief_has_no_precision_and_no_f05():
    result = context_eval.evaluate([], ["src/a.py"])

    assert result["brief_hit_rate"] == 0.0
    assert result["brief_precision"] is None
    assert result["brief_f05"] is None


def test_no_hits_scores_zero_f05():
    result = context_eval.evaluate(["src/x.py"], ["src/a.py"])

    assert result["brief_precision"] == 0.0
    assert result["brief_f05"] == 0.0


def test_truncated_delta_withholds_precision_and_f05_too(monkeypatch, tmp_path):
    limit = graphtrail_delta.CODE_REFERENCE_NODE_LIMIT
    nodes = [_diff_node(f"src/pkg/mod_{index:03d}.py", f"fn_{index}") for index in range(limit + 5)]
    delta = _write_real_sidecar(monkeypatch, tmp_path, {"changed_nodes": nodes})
    brief = aboyeur.CodeGraphBrief(attached=True, text="- `src/pkg/mod_000.py:1`\n", bytes=30)

    result = aboyeur._context_eval_for_run(brief, delta)

    assert result is not None
    assert result["brief_precision"] is None
    assert result["brief_f05"] is None


def test_outcome_rank_handles_records_with_and_without_precision(tmp_path, capsys):
    from brigade import outcome, outcome_cmd

    def record(artifact, task, context):
        return outcome.OutcomeRecord(
            artifact, "skill", task, "verify", 1, f"ref-{task}", "2026-06-20T00:00:00+00:00", context_eval=context
        )

    outcome_cmd.append_records(
        tmp_path,
        [
            record("skill-old", "t1", {"brief_hit_rate": 0.5, "hits": ["a.py"], "missed": ["b.py"]}),
            record("skill-new", "t2", {"brief_hit_rate": 1.0, "brief_precision": 0.25, "brief_f05": 0.294}),
            record("skill-new", "t3", {"brief_hit_rate": 0.5}),
            record("skill-new", "t4", {"brief_hit_rate": 1.0, "brief_precision": 0.75, "brief_f05": 0.789}),
        ],
    )

    assert outcome_cmd.rank(target=tmp_path, json_output=True) == 0
    ranking = {item["artifact_id"]: item for item in json.loads(capsys.readouterr().out)["ranking"]}
    assert ranking["skill-old"]["brief_hit_rate"] == 0.5
    assert "brief_precision" not in ranking["skill-old"]
    assert ranking["skill-new"]["brief_hit_samples"] == 3
    assert ranking["skill-new"]["brief_precision"] == 0.5
    assert ranking["skill-new"]["brief_precision_samples"] == 2

    assert outcome_cmd.rank(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "brief_hit: 0.500 (n=1)" in out
    assert "brief_precision: 0.500 (n=2)" in out
