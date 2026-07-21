"""Tests for the GraphTrail code-graph section of context packs."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import context_cmd, proc


def _make_db(target: Path) -> Path:
    db = target / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("")  # presence is enough; proc.run is mocked in these tests
    return db


def test_code_graph_summary_none_without_db(tmp_target):
    tmp_target.mkdir(parents=True)
    assert context_cmd._code_graph_summary(tmp_target, {"id": "t1", "text": "do x"}) is None


def test_code_graph_summary_none_without_task(tmp_target, monkeypatch):
    tmp_target.mkdir(parents=True)
    _make_db(tmp_target)
    monkeypatch.setattr(context_cmd.component_bins, "resolve", lambda name, **kw: "/x/" + name)
    assert context_cmd._code_graph_summary(tmp_target, None) is None


def test_code_graph_summary_parses_and_trims(tmp_target, monkeypatch):
    tmp_target.mkdir(parents=True)
    _make_db(tmp_target)
    monkeypatch.setattr(context_cmd.component_bins, "resolve", lambda name, **kw: "/x/" + name)

    pack = {
        "schema_version": 1,
        "task": "handoff lint",
        "entry_points": [
            {
                "qualified_name": "lint",
                "kind": "function",
                "file_path": "a.py",
                "start_line": 5,
                "signature": "drop me",
            }
        ],
        "callers": [{"x": 1}, {"x": 2}],
        "callees": [],
        "related_files": ["a.py", "b.py"],
    }

    def fake_run(args, **kw):
        assert Path(args[0]).name == "graphtrail"
        assert "context" in args and "--json" in args
        return proc.Result(code=0, stdout=json.dumps(pack), stderr="")

    monkeypatch.setattr(context_cmd.proc, "run", fake_run)

    out = context_cmd._code_graph_summary(tmp_target, {"id": "t1", "text": "handoff lint"})
    assert out is not None
    assert out["schema_version"] == 1
    assert out["query"] == "handoff lint"
    assert out["entry_points"][0]["qualified_name"] == "lint"
    assert "signature" not in out["entry_points"][0]  # trimmed to the four fields
    assert out["caller_count"] == 2
    assert out["callee_count"] == 0
    assert out["related_files"] == ["a.py", "b.py"]


def test_code_graph_summary_none_on_nonzero_exit(tmp_target, monkeypatch):
    tmp_target.mkdir(parents=True)
    _make_db(tmp_target)
    monkeypatch.setattr(context_cmd.component_bins, "resolve", lambda name, **kw: "/x/" + name)
    monkeypatch.setattr(context_cmd.proc, "run", lambda args, **kw: proc.Result(code=1, stdout="", stderr="boom"))
    assert context_cmd._code_graph_summary(tmp_target, {"text": "x"}) is None


def test_context_payload_always_has_code_graph_key(tmp_target):
    # No db and no task -> code_graph is None, but the key is always present and the pack builds.
    tmp_target.mkdir(parents=True)
    payload = context_cmd._context_payload(tmp_target, kind="repo")
    assert "code_graph" in payload
    assert payload["code_graph"] is None
