"""Tests for the versioned control crosswalk and `brigade evidence controls`."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli, control_crosswalk
from brigade.control_crosswalk import (
    EVIDENCE_CONTROLS_SCHEMA,
    HEADER_NOTICE,
    SCHEMA,
    VALID_MAPPING_STATUSES,
    VALID_OBLIGATION_BEARERS,
    VALID_RELATIONSHIPS,
    load_crosswalk,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CROSSWALK_DOC = REPO_ROOT / "docs" / "control-crosswalk.md"


def _write_verify_receipt(
    run_dir: Path,
    *,
    run_id: str,
    producer_run_id: str | None = None,
    status: str = "completed",
    commands: list[dict] | None = None,
) -> None:
    receipt = {
        "schema_version": 2,
        "run_id": run_id,
        "target": str(run_dir.parents[2]),
        "status": status,
        "started_at": "2026-01-01T00:00:00+00:00",
        "completed_at": "2026-01-01T00:00:01+00:00",
        "path": str(run_dir),
        "commands": commands or [{"command": "true", "status": "completed", "exit_code": 0}],
    }
    if producer_run_id is not None:
        receipt["producer_run_id"] = producer_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def test_crosswalk_json_loads_and_validates():
    crosswalk = load_crosswalk()
    assert crosswalk["schema"] == SCHEMA
    assert crosswalk["crosswalk_version"] == 1
    assert crosswalk["generated_by"] == "brigade"

    claim_ids = {c["id"] for c in crosswalk["claims"]}
    framework_ids = {f["id"] for f in crosswalk["frameworks"]}

    assert len(crosswalk["claims"]) >= 12
    assert len(crosswalk["frameworks"]) >= 5

    for mapping in crosswalk["mappings"]:
        assert mapping["claim_id"] in claim_ids
        assert mapping["framework_id"] in framework_ids
        assert mapping["relationship"] in VALID_RELATIONSHIPS
        assert mapping["obligation_bearer"] in VALID_OBLIGATION_BEARERS

    for framework in crosswalk["frameworks"]:
        assert "publication_date" in framework
        assert framework["licensing"] in {"public", "licensed"}
        if "mapping_status" in framework:
            assert framework["mapping_status"] in VALID_MAPPING_STATUSES

    licensed_ids = {f["id"] for f in crosswalk["frameworks"] if f["licensing"] == "licensed"}
    for mapping in crosswalk["mappings"]:
        if mapping["framework_id"] in licensed_ids:
            assert len(mapping["rationale"]) <= 200
            assert '"' not in mapping["rationale"]
            assert "'" not in mapping["rationale"]

    for framework in crosswalk["frameworks"]:
        if framework.get("mapping_status", "mapped") != "mapped":
            # identifiers-not-sourced frameworks intentionally have zero rows.
            continue
        framework_id = framework["id"]
        for claim_id in claim_ids:
            matches = [
                m for m in crosswalk["mappings"] if m["framework_id"] == framework_id and m["claim_id"] == claim_id
            ]
            assert matches, f"no mapping for framework={framework_id} claim={claim_id}"

    not_sourced_ids = {f["id"] for f in crosswalk["frameworks"] if f.get("mapping_status") == "identifiers-not-sourced"}
    for framework_id in not_sourced_ids:
        rows = [m for m in crosswalk["mappings"] if m["framework_id"] == framework_id]
        assert rows == [], f"identifiers-not-sourced framework {framework_id} has rows"


def test_iso_42001_a_6_2_8_supports_ec_01():
    crosswalk = load_crosswalk()
    matches = [
        m
        for m in crosswalk["mappings"]
        if m["framework_id"] == "iso-42001-2023" and m["control_id"] == "A.6.2.8" and m["claim_id"] == "EC-01"
    ]
    assert matches
    assert matches[0]["relationship"] == "supports"


def test_iso_42001_a_5_has_no_relationship_row():
    crosswalk = load_crosswalk()
    matches = [m for m in crosswalk["mappings"] if m["framework_id"] == "iso-42001-2023" and m["control_id"] == "A.5"]
    assert matches
    assert all(m["relationship"] == "no-relationship" for m in matches)


def test_no_relationship_rows_render_not_applicable():
    evaluated = control_crosswalk.evaluate_controls(REPO_ROOT)
    for row in evaluated["mappings"]:
        if row["relationship"] == "no-relationship":
            assert row["state"] == "not_applicable", (
                f"no-relationship row {row['framework_id']} {row['control_id']} -> {row['claim_id']} "
                f"rendered state {row['state']}"
            )


def test_framework_not_mapped_appears_in_doc():
    crosswalk = load_crosswalk()
    not_sourced_names = {
        f["name"] for f in crosswalk["frameworks"] if f.get("mapping_status") == "identifiers-not-sourced"
    }
    assert not_sourced_names
    evaluated = control_crosswalk.evaluate_controls(REPO_ROOT)
    rendered = control_crosswalk.render_doc(evaluated)
    assert "Not mapped in crosswalk version 1" in rendered
    for name in not_sourced_names:
        assert name in rendered, f"framework {name} missing from rendered doc"


def test_completed_verify_receipt_yields_ec_01_passed_ec_02_untested(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    run_dir = target / ".brigade" / "work" / "verify-runs" / "20260101-000000-test"
    _write_verify_receipt(run_dir, run_id="20260101-000000-test")

    evaluated = control_crosswalk.evaluate_controls(target)

    ec01 = next(m for m in evaluated["mappings"] if m["claim_id"] == "EC-01" and m["framework_id"] == "iso-42001-2023")
    ec02 = next(m for m in evaluated["mappings"] if m["claim_id"] == "EC-02" and m["framework_id"] == "iso-42001-2023")
    assert ec01["state"] == "evidenced_passed"
    assert ec02["state"] == "untested"


def test_run_scoped_query_excludes_other_producer_runs(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    _write_verify_receipt(
        target / ".brigade" / "work" / "verify-runs" / "run-a",
        run_id="run-a",
        producer_run_id="producer-1",
    )
    _write_verify_receipt(
        target / ".brigade" / "work" / "verify-runs" / "run-b",
        run_id="run-b",
        producer_run_id="producer-2",
    )

    scoped = control_crosswalk.evaluate_controls(target, run_id="producer-1")
    ec01 = next(m for m in scoped["mappings"] if m["claim_id"] == "EC-01" and m["framework_id"] == "iso-42001-2023")
    assert ec01["state"] == "evidenced_passed"

    scoped_other = control_crosswalk.evaluate_controls(target, run_id="producer-999")
    ec01_other = next(
        m for m in scoped_other["mappings"] if m["claim_id"] == "EC-01" and m["framework_id"] == "iso-42001-2023"
    )
    assert ec01_other["state"] == "untested"


def test_ec02_evidence_failed_on_unsigned_attestation(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    run_dir = target / ".brigade" / "work" / "verify-runs" / "20260101-000000-test"
    _write_verify_receipt(run_dir, run_id="20260101-000000-test")
    (run_dir / "attestation.json").write_text(
        json.dumps(
            {
                "payloadType": "application/vnd.in-toto+json",
                "payload": "dGVzdA==",
                "signatures": [{"keyid": "fake-key", "sig": "ZmFrZQ=="}],
                "brigade": {"profile": "brigade.sshsig-dsse.v1", "namespace": "test"},
            }
        )
    )

    evaluated = control_crosswalk.evaluate_controls(target)
    ec02_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-02" and m["relationship"] != "no-relationship"
    ]
    assert ec02_rows
    assert all(m["state"] == "evidenced_failed" for m in ec02_rows), [m["state"] for m in ec02_rows]


def test_ec11_parses_multiline_index_jsonl(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    archive_dir = target / ".brigade" / "work" / "verify-archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "index.jsonl").write_text('{"run_id": "a", "path": "x"}\n{"run_id": "b", "path": "y"}\n')

    evaluated = control_crosswalk.evaluate_controls(target)
    ec11_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-11" and m["relationship"] != "no-relationship"
    ]
    assert ec11_rows
    assert all(m["state"] == "evidenced_passed" for m in ec11_rows), [m["state"] for m in ec11_rows]


def test_json_output_is_sorted_and_includes_schema(tmp_path, capsys):
    target = tmp_path / "ws"
    target.mkdir()
    rc = cli.main(["evidence", "controls", "--target", str(target), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["schema"] == EVIDENCE_CONTROLS_SCHEMA
    assert list(payload.keys()) == sorted(payload.keys())
    assert payload["notice"] == HEADER_NOTICE


def test_rendered_doc_matches_committed_doc():
    assert CONTROL_CROSSWALK_DOC.is_file()
    evaluated = control_crosswalk.evaluate_controls(REPO_ROOT)
    rendered = control_crosswalk.render_doc(evaluated)
    committed = CONTROL_CROSSWALK_DOC.read_text()
    assert committed == rendered


def test_header_notice_in_text_and_json_outputs(tmp_path, capsys):
    target = tmp_path / "ws"
    target.mkdir()
    rc = cli.main(["evidence", "controls", "--target", str(target)])
    assert rc == 0
    assert HEADER_NOTICE in capsys.readouterr().out


def test_cli_rejects_nonexistent_target():
    rc = cli.main(["evidence", "controls", "--target", "/nonexistent/path"])
    assert rc == 2
