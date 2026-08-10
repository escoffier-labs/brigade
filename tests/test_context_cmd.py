"""Tests for the GraphTrail code-graph section of context packs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
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
        assert args[0] == "/x/graphtrail"
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


def test_context_freshness_snapshots_are_safe_and_detect_drift(tmp_target, monkeypatch):
    tmp_target.mkdir(parents=True)
    (tmp_target / "README.md").write_text("first\n")
    monkeypatch.setattr(context_cmd, "_now", lambda: datetime(2026, 8, 10, tzinfo=timezone.utc))

    payload = context_cmd._context_payload(tmp_target, kind="repo")
    freshness = payload["freshness"]
    assert freshness["generator"]["id"] == "brigade.context"
    assert freshness["sources"][0] == {
        "path": "README.md",
        "exists": True,
        "sha256": context_cmd.sha256(b"first\n").hexdigest(),
    }
    assert all(not Path(item["path"]).is_absolute() for item in freshness["sources"])

    payload.update({"pack_id": "pack-one", "created_at": freshness["generated_at"]})
    (tmp_target / "README.md").write_text("second\n")
    issue_types = {item["issue_type"] for item in context_cmd._context_pack_issues(tmp_target, payload)}
    assert "source_drift" in issue_types


def test_context_freshness_reconciles_dependent_receipts(tmp_target, monkeypatch):
    tmp_target.mkdir(parents=True)
    monkeypatch.setattr(context_cmd, "_now", lambda: datetime(2026, 8, 10, tzinfo=timezone.utc))
    payload = context_cmd._context_payload(tmp_target, kind="repo")
    payload.update({"pack_id": "pack-one", "created_at": payload["freshness"]["generated_at"]})

    receipt = tmp_target / ".brigade" / "work" / "closeouts" / "run-one" / "closeout.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"status":"closed"}\n')
    issues = context_cmd._context_pack_issues(tmp_target, payload)
    assert any(item["issue_type"] == "dependent_receipt_drift" for item in issues)


def test_context_freshness_malformed_and_private_paths_fail_safely(tmp_target):
    tmp_target.mkdir(parents=True)
    pack = {
        "pack_id": "pack-one",
        "kind": "repo",
        "freshness": {
            "generator": [],
            "sources": [{"path": "/private/operator/source.md", "exists": True, "sha256": "a" * 64}],
            "dependent_receipts": ["not-an-object"],
        },
        "source_references": [{"path": "/private/operator/source.md", "exists": True}],
    }
    issues = context_cmd._context_pack_issues(tmp_target, pack)
    issue_types = {item["issue_type"] for item in issues}
    assert {
        "malformed_generator_snapshot",
        "unsafe_source_path",
        "malformed_dependent_receipt_snapshot",
        "unsafe_source_reference",
    } <= issue_types
    assert "/private/operator" not in json.dumps(issues)


def test_context_freshness_generator_drift_is_deterministic(tmp_target):
    tmp_target.mkdir(parents=True)
    payload = context_cmd._context_payload(tmp_target, kind="repo")
    payload.update({"pack_id": "pack-one", "created_at": payload["freshness"]["generated_at"]})
    payload["freshness"]["generator"]["version"] = "older"
    issues = context_cmd._context_pack_issues(tmp_target, payload)
    assert any(item["issue_type"] == "generator_drift" for item in issues)
