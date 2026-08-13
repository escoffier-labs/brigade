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


def test_module_map_excludes_worktrees_and_dedupes_by_repo_path():
    file_graph = {
        "nodes": {
            "src/brigade/cli.py": 10,
            "./src/brigade/cli.py": 10,
            ".worktrees/pr-906/src/brigade/cli.py": 99,
            ".worktrees/pr-906/tests/test_cli.py": 80,
            "tests/test_cli.py": 2,
        },
        "edges": [
            ("src/brigade/cli.py", "tests/test_cli.py", 1),
            (".worktrees/pr-906/src/brigade/cli.py", ".worktrees/pr-906/tests/test_cli.py", 50),
        ],
    }
    module_map = code_export._build_module_map(file_graph, changed_files=[])
    ids = [row["id"] for row in module_map["modules"]]
    labels = [row["label"] for row in module_map["modules"]]
    assert all(".worktrees" not in module_id for module_id in ids)
    assert labels.count("tests") <= 1
    assert (
        sum(
            row["symbol_count"]
            for row in module_map["modules"]
            if row["id"].endswith("brigade") or "brigade" in row["id"]
        )
        == 10
    )


def test_module_map_disambiguates_test_labels_by_parent_package():
    file_graph = {
        "nodes": {
            "tests/test_cli.py": 1,
            "engines/code-graph/tests/graph_tests.rs": 4,
            "src/brigade/cli.py": 8,
        },
        "edges": [],
    }
    module_map = code_export._build_module_map(file_graph, changed_files=[])
    labels = {row["id"]: row["label"] for row in module_map["modules"]}
    test_labels = [label for label in labels.values() if "test" in label.lower()]
    assert len(set(test_labels)) == len(test_labels)
    assert any("code-graph" in label for label in test_labels)


def test_module_map_defaults_to_top_modules_by_connectivity():
    nodes = {f"src/iso{index}/file.py": 100 + index for index in range(20)}
    nodes["src/hub/core.py"] = 3
    nodes["src/spoke/a.py"] = 1
    nodes["src/spoke/b.py"] = 1
    edges = [
        ("src/spoke/a.py", "src/hub/core.py", 9),
        ("src/spoke/b.py", "src/hub/core.py", 8),
    ]
    for index in range(20):
        edges.append((f"src/iso{index}/file.py", f"src/iso{index}/file.py", 1))
    module_map = code_export._build_module_map({"nodes": nodes, "edges": edges}, changed_files=[])
    ids = [row["id"] for row in module_map["modules"]]
    assert len(ids) == code_export.MODULE_CAP
    assert code_export.MODULE_CAP == 15
    assert any(module_id.endswith("hub") or module_id == "src/hub" for module_id in ids)
    trunc = module_map["truncation"]
    assert trunc["hidden_modules"] == 7
    note = (trunc.get("note") or "").lower()
    assert "showing top 15 of" in note
    assert "edges hidden" in note


def test_module_map_insights_name_hub_isolated_and_change():
    file_graph = {
        "nodes": {
            "src/brigade/cli.py": 2737,
            "src/other/util.py": 2,
            "src/lonely/x.py": 1,
            "tests/test_cli.py": 4,
        },
        "edges": [
            ("src/other/util.py", "src/brigade/cli.py", 6),
            ("tests/test_cli.py", "src/brigade/cli.py", 2),
        ],
    }
    module_map = code_export._build_module_map(
        file_graph,
        changed_files=["src/other/util.py"],
    )
    insights = module_map["insights"]
    assert insights["total_modules"] == 4
    assert insights["most_connected"]["inbound"] >= 2
    assert "brigade" in insights["most_connected"]["label"]
    assert insights["isolated_count"] >= 1
    assert "other" in insights["biggest_change"]["label"]
    hub = next(row for row in module_map["modules"] if "brigade" in row["id"])
    assert hub["inbound_count"] >= 2
    assert "other" in hub["dependents"] or "util" in " ".join(hub["dependents"]).lower()
    assert hub["attributed_tests"]
    assert hub["top_files"][0]["path"].endswith("cli.py")


def test_module_map_insights_name_package_not_largest_test_module():
    file_graph = {
        "nodes": {
            "src/brigade/cli.py": 100,
            "src/brigade/work_cmd/run.py": 40,
            "src/other/util.py": 2,
            "src/lonely/x.py": 1,
            "tests/test_cli.py": 9000,
            "engines/code-graph/tests/graph_tests.rs": 80,
            "src/a/a.py": 1,
            "src/b/b.py": 1,
        },
        "edges": [
            ("src/other/util.py", "src/brigade/cli.py", 6),
            ("src/brigade/work_cmd/run.py", "src/brigade/cli.py", 3),
            ("tests/test_cli.py", "src/brigade/cli.py", 2),
            ("src/a/a.py", "tests/test_cli.py", 4),
            ("src/b/b.py", "tests/test_cli.py", 4),
            ("src/other/util.py", "tests/test_cli.py", 4),
        ],
    }
    insights = code_export._build_module_map(file_graph, changed_files=[])["insights"]
    assert insights["core"]["label"] == "brigade"
    assert "test" not in insights["core"]["label"].lower()
    assert insights["largest"]["label"] == "tests"
    assert insights["largest"]["symbol_count"] == 9000
    assert insights["core"]["symbol_count"] == 9225
    assert "brigade" in insights["most_connected"]["label"]
    assert "test" not in insights["most_connected"]["label"].lower()
    assert insights["isolated_count"] == 1
