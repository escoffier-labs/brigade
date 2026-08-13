"""Tests for the versioned code-graph export contract (#887)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import code_export


def _write_fake_db(target: Path) -> Path:
    db = target / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"fake-graphtrail-index")
    return db


def test_export_payload_schema_and_module_map_from_jsonl(monkeypatch, tmp_path):
    _write_fake_db(tmp_path)
    jsonl = "\n".join(
        [
            json.dumps({"type": "node", "id": "src/pkg/mod.py", "language": "python", "symbols": 4}),
            json.dumps({"type": "node", "id": "src/other/util.py", "language": "python", "symbols": 2}),
            json.dumps({"type": "node", "id": "tests/test_mod.py", "language": "python", "symbols": 1}),
            json.dumps(
                {
                    "type": "edge",
                    "source": "src/pkg/mod.py",
                    "target": "src/other/util.py",
                    "calls": 3,
                }
            ),
        ]
    )

    def fake_run(argv, **kwargs):
        from brigade import proc

        joined = " ".join(argv)
        if " stats " in f" {joined} " and "--json" in argv:
            return proc.Result(0, json.dumps({"files": 3, "symbols": 7}), "")
        if " export " in f" {joined} ":
            return proc.Result(0, jsonl, "")
        return proc.Result(1, "", "unexpected")

    monkeypatch.setattr(code_export.proc, "run", fake_run)
    monkeypatch.setattr(code_export.context_cmd, "_graphtrail_bin", lambda: "/bin/graphtrail")

    payload = code_export.export_payload(tmp_path)
    assert payload["schema"] == "brigade.code-graph-export.v1"
    assert payload["schema_version"] == 1
    assert payload["module_map"]["truncation"]["hidden_modules"] == 0
    modules = {row["label"]: row for row in payload["module_map"]["modules"]}
    assert modules["pkg"]["symbol_count"] == 4
    assert modules["other"]["symbol_count"] == 2
    assert payload["module_map"]["edges"][0]["weight"] == 3


def test_export_truncation_labels_hidden_modules(monkeypatch, tmp_path):
    _write_fake_db(tmp_path)
    lines = []
    for index in range(code_export.MODULE_CAP + 5):
        lines.append(
            json.dumps({"type": "node", "id": f"src/m{index}/file.py", "language": "python", "symbols": index + 1})
        )
    jsonl = "\n".join(lines)

    def fake_run(argv, **kwargs):
        from brigade import proc

        if "stats" in argv and "--json" in argv:
            return proc.Result(0, "{}", "")
        if "export" in argv:
            return proc.Result(0, jsonl, "")
        return proc.Result(1, "", "")

    monkeypatch.setattr(code_export.proc, "run", fake_run)
    monkeypatch.setattr(code_export.context_cmd, "_graphtrail_bin", lambda: "/bin/graphtrail")

    payload = code_export.export_payload(tmp_path)
    trunc = payload["module_map"]["truncation"]
    assert trunc["shown_modules"] == code_export.MODULE_CAP
    assert trunc["hidden_modules"] == 5
    assert "showing top" in (trunc.get("note") or "").lower()


def test_export_impact_section_matches_graphtrail_edges(monkeypatch, tmp_path):
    _write_fake_db(tmp_path)
    impact_edges = [
        {
            "source": "caller_fn",
            "target": "target_fn",
            "kind": "calls",
            "hops": 1,
            "source_file": "src/a.py",
            "target_file": "src/b.py",
        }
    ]
    search_rows = [
        {
            "qualified_name": "target_fn",
            "name": "target_fn",
            "file_path": "src/b.py",
            "kind": "function",
        }
    ]
    affected = {
        "affected_tests": [{"file_path": "tests/test_b.py", "min_hops": 1, "via": ["test_x"]}],
        "impacted_files": [],
        "changed_files": ["src/b.py"],
        "missing_files": [],
        "truncated": False,
    }

    def fake_run(argv, **kwargs):
        from brigade import proc

        if "stats" in argv and "--json" in argv:
            return proc.Result(0, "{}", "")
        if "export" in argv:
            return proc.Result(0, "", "")
        if "search" in argv:
            return proc.Result(0, json.dumps(search_rows), "")
        if "impact" in argv:
            return proc.Result(0, json.dumps(impact_edges), "")
        if "callers" in argv:
            return proc.Result(0, "[]", "")
        if "affected" in argv:
            return proc.Result(0, json.dumps(affected), "")
        return proc.Result(1, "", "")

    monkeypatch.setattr(code_export.proc, "run", fake_run)
    monkeypatch.setattr(code_export.context_cmd, "_graphtrail_bin", lambda: "/bin/graphtrail")

    payload = code_export.export_payload(tmp_path, symbol="target_fn")
    assert payload["impact"]["edges"] == impact_edges
    assert payload["impact"]["affected_tests"] == affected


def test_cli_export_json_prints_contract(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        code_export,
        "export_payload",
        lambda target, **kwargs: {"schema": "brigade.code-graph-export.v1", "schema_version": 1},
    )
    assert code_export.run_cli(tmp_path, ["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == 1


def test_code_cmd_routes_export_json_to_contract(monkeypatch, tmp_path):
    from brigade import code_cmd

    called: list[str] = []

    def fake_cli(target, forwarded):
        called.append("cli")
        assert "--json" in forwarded
        return 0

    monkeypatch.setattr(code_export, "run_cli", fake_cli)
    assert code_cmd.run("export", ["--target", str(tmp_path), "--json"]) == 0
    assert called == ["cli"]
