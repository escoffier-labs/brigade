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
