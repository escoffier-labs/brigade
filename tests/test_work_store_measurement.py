"""Tests for scripts/measure_work_store.py (#846 R1 characterization harness)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_work_store.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_work_store_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fixture_specs_match_generated_ledgers():
    module = _load_module()
    for name, spec in module.FIXTURE_SPECS.items():
        description = module.describe_fixture(name)
        assert description["task_count"] == spec.task_count
        assert description["edge_count"] == spec.edge_count
        assert description["blocks_edges"] == spec.blocks_edges
        assert description["provenance_edges"] == spec.provenance_edges
        assert description["ready_count"] == spec.ready_count
        assert description["contains_hostnames"] is False
        assert len(description["digest_sha256"]) == 64


def test_wide_fixture_provenance_does_not_block_readiness():
    module = _load_module()
    ledger = module.build_fixture("wide_500_1000")
    from brigade.work_cmd import edges as edges_mod

    resolution = edges_mod.resolve_readiness(ledger)
    assert len(resolution.ready) == 500
    assert len(resolution.cycles) == 0
    assert all(edge["type"] == "discovered-from" for edge in ledger["edges"])


def test_chain_and_diamond_ready_counts():
    module = _load_module()
    from brigade.work_cmd import edges as edges_mod

    chain = module.build_fixture("chain_50")
    diamond = module.build_fixture("diamond_200")
    assert len(edges_mod.resolve_readiness(chain).ready) == 1
    assert len(edges_mod.resolve_readiness(diamond).ready) == 1


def test_percentile_nearest_rank():
    module = _load_module()
    values = [10, 20, 30, 40, 50]
    assert module._percentile_nearest_rank(values, 50) == 30
    assert module._percentile_nearest_rank(values, 95) == 50
    with pytest.raises(ValueError):
        module._percentile_nearest_rank([], 50)


def test_measure_matrix_json_and_sqlite_report_shape(tmp_path):
    module = _load_module()
    report = module.measure_matrix(
        tmp_path,
        shapes=["json_ledger", "sqlite_wal"],
        datasets=["chain_50"],
        warmups=0,
        trials=2,
    )
    assert report["issue"] == 846
    assert report["slice"] == "R1"
    assert report["kind"] == "work-store-characterization"
    assert report["anonymous_metrics"] == "disabled_in_harness"
    assert {"environment", "fixtures", "results", "decision", "capability_notes"} <= set(report)
    assert report["environment"]["filesystem_type"]
    assert isinstance(report["environment"]["directory_fsync_supported"], bool)
    assert len(report["results"]) == 2
    for result in report["results"]:
        assert result["status"] == "measured"
        assert result["pass"] is True
        assert result["gates"]["issue_737_graph"] is True
        assert result["gates"]["issue_738_claim"] is True
        latency = result["mutation_latency"]
        for key in ("p50_ns", "p95_ns", "max_ns", "min_ns", "samples"):
            assert isinstance(latency[key], int)
        assert result["claim_race"]["codes"] == [0, 13]
        assert result["claim_race"]["winner_count"] == 1
        assert result["claim_race"]["exit_13_count"] == 1


def test_measure_matrix_marks_dolt_shapes_blocked(tmp_path):
    module = _load_module()
    report = module.measure_matrix(
        tmp_path,
        shapes=["dolt_cli_per_command", "embedded_dolt", "dolt_sql_server"],
        datasets=["chain_50"],
        warmups=0,
        trials=1,
    )
    assert len(report["results"]) == 3
    for result in report["results"]:
        assert result["status"] == "blocked"
        assert result["mutation_latency"] is None
        assert result["pass"] is None
        assert result["gates"]["issue_738_claim"] is None
        assert "reason" in result


def test_decision_record_keeps_json_and_excludes_shared_service():
    module = _load_module()
    decision = module.decision_record()
    assert decision["store_decision"] == "keep_machine_local_json_ledger"
    assert decision["backend_adoption"] is False
    assert decision["brigade_workstore_v1"] is False
    assert decision["beads"] == "excluded_under_770"
    assert decision["shared_store_conformance_fixture"] == "not_approved"
    assert "portable_sync" in decision["current_need"]


def test_write_report_is_canonical_and_diffable(tmp_path):
    module = _load_module()
    report = {"b": 1, "a": {"z": [1, 2], "y": True}}
    output = tmp_path / "nested" / "out.json"
    module.write_report(report, output)
    assert output.read_text() == json.dumps(report, indent=2, sort_keys=True) + "\n"


def test_cli_writes_measurement_and_fixture_manifest(tmp_path):
    output = tmp_path / "measurements" / "report.json"
    manifest = tmp_path / "fixtures" / "manifest.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--output",
            str(output),
            "--fixture-manifest",
            str(manifest),
            "--shapes",
            "json_ledger,sqlite_wal,dolt_sql_server",
            "--datasets",
            "chain_50",
            "--warmups",
            "0",
            "--trials",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text())
    assert payload["issue"] == 846
    assert len(payload["results"]) == 3
    statuses = {item["shape"]: item["status"] for item in payload["results"]}
    assert statuses["json_ledger"] == "measured"
    assert statuses["sqlite_wal"] == "measured"
    assert statuses["dolt_sql_server"] == "blocked"
    fixture_payload = json.loads(manifest.read_text())
    assert "chain_50" in fixture_payload["fixtures"]
    assert fixture_payload["fixtures"]["chain_50"]["task_count"] == 50


def test_measure_matrix_rejects_nonpositive_trials(tmp_path):
    module = _load_module()
    with pytest.raises(ValueError):
        module.measure_matrix(tmp_path, shapes=["json_ledger"], datasets=["chain_50"], trials=0)


def test_no_leftover_measurement_dirs(tmp_path):
    module = _load_module()
    module.measure_matrix(
        tmp_path,
        shapes=["json_ledger"],
        datasets=["chain_50"],
        warmups=0,
        trials=1,
    )
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith("brigade-work-store-measure-")]
    assert leftovers == []
