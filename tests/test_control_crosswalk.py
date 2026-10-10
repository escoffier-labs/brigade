"""Tests for the versioned control crosswalk and `brigade evidence controls`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli, control_crosswalk, localio
from brigade.control_crosswalk import (
    EVIDENCE_CONTROLS_SCHEMA,
    HEADER_NOTICE,
    SCHEMA,
    PROVENANCE_FIELDS,
    VALID_DISPOSITIONS,
    VALID_MAPPING_STATUSES,
    VALID_OBLIGATION_BEARERS,
    VALID_RELATIONSHIPS,
    load_crosswalk,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_CROSSWALK_DOC = REPO_ROOT / "docs" / "control-crosswalk.md"


@pytest.mark.parametrize("attempts,cached_bytes,budget", [(2, 0, 4), (20, 3, 16)])
def test_assessment_reader_charges_failed_growing_reads_to_shared_budget(
    tmp_path, monkeypatch, attempts, cached_bytes, budget
):
    import os

    from brigade.control_crosswalk import _AssessmentReader, _ReadRefusal

    monkeypatch.setattr(control_crosswalk, "_ASSESSMENT_BYTE_BUDGET", budget)
    paths = [tmp_path / f"growing-{index}.json" for index in range(attempts)]
    for path in paths:
        path.write_bytes(b"abcdef")
    cached_path = tmp_path / "cached.json"
    cached_path.write_bytes(b"x" * cached_bytes)
    real_fstat, real_read = os.fstat, os.read
    transported = 0

    def stale_size(fd):
        fields = list(real_fstat(fd))
        fields[6] = 0  # The file grows after this size observation.
        return os.stat_result(fields)

    def counted_read(fd, count):
        nonlocal transported
        chunk = real_read(fd, count)
        transported += len(chunk)
        return chunk

    monkeypatch.setattr(os, "fstat", stale_size)
    monkeypatch.setattr(os, "read", counted_read)
    reader = _AssessmentReader(tmp_path)
    try:
        assert reader.read(cached_path, 4) == b"x" * cached_bytes
        for path in paths:
            with pytest.raises(_ReadRefusal, match="read_limit_exceeded"):
                reader.read(path, 4)
            assert reader.used == transported
            assert reader.errors[path] == "read_limit_exceeded"
            assert path not in reader.contents
        assert transported == budget + 1
        assert reader.read(cached_path, 4) == b"x" * cached_bytes
        assert reader.used == transported
        if cached_bytes:
            with pytest.raises(_ReadRefusal, match="read_limit_exceeded"):
                reader.read(cached_path, cached_bytes - 1)
            assert reader.used == transported
    finally:
        reader.close()


def test_assessment_reader_charges_chunks_before_later_io_error(tmp_path, monkeypatch):
    import os

    from brigade.control_crosswalk import _AssessmentReader

    path = tmp_path / "receipt.json"
    path.write_bytes(b"abcdef")
    real_read = os.read
    calls = 0

    def interrupted_read(fd, count):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("read failed after partial transport")
        return real_read(fd, min(count, 2))

    monkeypatch.setattr(os, "read", interrupted_read)
    reader = _AssessmentReader(tmp_path)
    try:
        with pytest.raises(OSError, match="read failed after partial transport"):
            reader.read(path)
        assert reader.used == 2
        assert reader.errors[path] == "discovery_unreadable"
        assert path not in reader.contents
    finally:
        reader.close()


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="native Windows assessment integration")
def test_native_windows_assessment_traverses_brigade_and_reads_receipt(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "verify-a"), run_id="verify-a", status="failed")
    result = _assess(target, "EC-01")
    assert result["outcome"] == "failed"
    assert result["population"]["failed"] == 1
    assert result["artifacts"][0]["relpath"] == ".brigade/work/verify-runs/verify-a/receipt.json"


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
    # The producer records the intended check list alongside command results.
    receipt["planned_commands"] = [str(c.get("command") or "true") for c in receipt["commands"]]
    if producer_run_id is not None:
        receipt["producer_run_id"] = producer_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def test_crosswalk_json_loads_and_validates():
    crosswalk = load_crosswalk()
    assert crosswalk["schema"] == SCHEMA
    assert crosswalk["crosswalk_version"] == 2
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
        unmapped_claims = {
            e["claim_id"] for e in crosswalk["unmapped_evidence_properties"] if e["framework_id"] == framework_id
        }
        for claim_id in claim_ids:
            matches = [
                m for m in crosswalk["mappings"] if m["framework_id"] == framework_id and m["claim_id"] == claim_id
            ]
            if claim_id in unmapped_claims:
                assert matches == [], f"unmapped claim {claim_id} also has {framework_id} rows"
            else:
                assert matches, f"no mapping for framework={framework_id} claim={claim_id}"

    not_sourced_ids = {f["id"] for f in crosswalk["frameworks"] if f.get("mapping_status") == "identifiers-not-sourced"}
    for framework_id in not_sourced_ids:
        rows = [m for m in crosswalk["mappings"] if m["framework_id"] == framework_id]
        assert rows == [], f"identifiers-not-sourced framework {framework_id} has rows"
        unmapped = [e for e in crosswalk["unmapped_evidence_properties"] if e["framework_id"] == framework_id]
        assert unmapped == [], f"identifiers-not-sourced framework {framework_id} has unmapped entries"

    for entry in crosswalk["unmapped_evidence_properties"]:
        assert entry["claim_id"] in claim_ids
        assert entry["framework_id"] in framework_ids
        assert "control_id" not in entry


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
    rendered = control_crosswalk.render_doc()
    assert "Not mapped in crosswalk version 2" in rendered
    for name in not_sourced_names:
        assert name in rendered, f"framework {name} missing from rendered doc"


def test_completed_verify_receipt_is_structure_only_until_integrity_adapter(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    run_dir = target / ".brigade" / "work" / "verify-runs" / "20260101-000000-test"
    _write_verify_receipt(run_dir, run_id="20260101-000000-test")

    evaluated = control_crosswalk.evaluate_controls(target)

    ec01 = next(m for m in evaluated["mappings"] if m["claim_id"] == "EC-01" and m["framework_id"] == "iso-42001-2023")
    ec02 = next(m for m in evaluated["mappings"] if m["claim_id"] == "EC-02" and m["framework_id"] == "iso-42001-2023")
    # The receipt is unsigned and its digest is not rederived (#1618), so a
    # well-formed successful receipt is structure, not validated evidence.
    assert ec01["state"] == "untested"
    assert ec01["evidence_outcome"] == "structure_observed"
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
    assert ec01["state"] == "untested"
    assert ec01["evidence_outcome"] == "structure_observed"

    scoped_other = control_crosswalk.evaluate_controls(target, run_id="producer-999")
    ec01_other = next(
        m for m in scoped_other["mappings"] if m["claim_id"] == "EC-01" and m["framework_id"] == "iso-42001-2023"
    )
    assert ec01_other["state"] == "untested"
    assert ec01_other["evidence_outcome"] == "absent"


def test_ec02_evidence_failed_on_unsigned_attestation(tmp_path, monkeypatch):
    import shutil

    # The fake envelope is refused while parsing, before ssh-keygen would run,
    # so the fixture does not depend on ssh-keygen being installed.  The
    # missing-tool path is covered by test_missing_ssh_keygen_is_unavailable_not_failed.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: "/nonexistent/ssh-keygen" if name == "ssh-keygen" else real_which(name)
    )
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
    # v2: a parseable index is discovered and structurally observed with an honest
    # label; it is not validated evidence until an integrity verifier is wired.
    assert all(m["state"] == "untested" for m in ec11_rows), [m["state"] for m in ec11_rows]
    assert all(m["validation_level"] == "structure_observed" for m in ec11_rows)
    assert all(m["evidence_outcome"] == "structure_observed" for m in ec11_rows)
    readiness = evaluated["evidence_readiness"]["EC-11"]
    assert readiness["population"]["discovered"] == 1
    assert readiness["dimensions"]["integrity"] == {"status": "not_checked", "reason": "verifier_not_wired"}


def test_ec12_evidence_package_manifest_untested_passed_failed(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()

    evaluated = control_crosswalk.evaluate_controls(target)
    ec12_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-12" and m["relationship"] != "no-relationship"
    ]
    assert ec12_rows
    assert all(m["state"] == "untested" for m in ec12_rows), [m["state"] for m in ec12_rows]

    pkg_dir = target / ".brigade" / "evidence-packages" / "pkg-1"
    pkg_dir.mkdir(parents=True)
    entries = [
        {
            "path": "receipt.json",
            "sha256": "a" * 64,
            "bytes": 2,
            "media_type": "application/json",
            "kind": "receipt",
        }
    ]
    entries.sort(key=lambda e: e["path"])
    manifest = {
        "schema": "brigade.evidence_package.v1",
        "schema_version": 1,
        "created_at": "2026-09-06T00:00:00Z",
        "source": {"verify_run_id": "run-1", "receipt_sha256": "b" * 64},
        "entries": entries,
        "entries_sha256": localio.canonical_json_digest(entries),
        "limitations": [],
    }
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # A listed entry that is absent is rejected, not passed.
    evaluated = control_crosswalk.evaluate_controls(target)
    ec12_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-12" and m["relationship"] != "no-relationship"
    ]
    assert all(m["state"] == "evidenced_failed" for m in ec12_rows), [m["state"] for m in ec12_rows]
    assert evaluated["evidence_readiness"]["EC-12"]["reason"] == "entry_missing"

    # v2: a self-consistent manifest with present entries is structurally observed.
    # Entry contents are not rehashed until the strict package verifier is wired,
    # so it is not validated evidence.
    (pkg_dir / "receipt.json").write_text("{}", encoding="utf-8")
    evaluated = control_crosswalk.evaluate_controls(target)
    ec12_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-12" and m["relationship"] != "no-relationship"
    ]
    assert all(m["state"] == "untested" for m in ec12_rows), [m["state"] for m in ec12_rows]
    assert all(m["evidence_outcome"] == "structure_observed" for m in ec12_rows)

    manifest["entries_sha256"] = "c" * 64
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    evaluated = control_crosswalk.evaluate_controls(target)
    ec12_rows = [
        m for m in evaluated["mappings"] if m["claim_id"] == "EC-12" and m["relationship"] != "no-relationship"
    ]
    assert all(m["state"] == "evidenced_failed" for m in ec12_rows), [m["state"] for m in ec12_rows]


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
    rendered = control_crosswalk.render_doc()
    committed = CONTROL_CROSSWALK_DOC.read_text()
    assert committed == rendered


def test_rendered_doc_matches_committed_doc_without_artifacts(tmp_path):
    """--render-doc must be a pure function of the template and not read .brigade."""
    assert CONTROL_CROSSWALK_DOC.is_file()
    target = tmp_path / "ws"
    target.mkdir()
    doc_path = tmp_path / "control-crosswalk.md"
    rc = control_crosswalk.controls(target, render_doc_path=doc_path)
    assert rc == 0
    assert not (target / ".brigade").exists()
    rendered = doc_path.read_text()
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


NIST_AI_RMF = "nist-ai-rmf-1.0"
AI_100_1_PDF = "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf"

# Issue #1616: the twelve reviewed NIST AI RMF dispositions, keyed by claim.
# Value: (disposition, mapped control IDs, superseded control ID or None).
NIST_DISPOSITIONS = {
    "EC-01": ("conditional-support", {"MEASURE 1.1", "MEASURE 2.1"}, None),
    "EC-02": ("corrected-mismatch", {"MEASURE 2.1"}, "GOVERN 5.2"),
    "EC-03": ("unmapped-evidence-property", set(), "GOVERN 5.2"),
    "EC-04": ("corrected-mismatch", {"GOVERN 2.1"}, "GOVERN 1.2"),
    "EC-05": ("conditional-support", {"GOVERN 2.1"}, None),
    "EC-06": ("corrected-mismatch", {"MANAGE 4.1", "MANAGE 4.3"}, "MEASURE 2.1"),
    "EC-07": ("corrected-mismatch", {"GOVERN 1.6"}, "GOVERN 5.1"),
    "EC-08": ("corrected-mismatch", {"MANAGE 4.1"}, "MANAGE 2.4"),
    "EC-09": ("corrected-mismatch", {"MEASURE 2.7", "MEASURE 2.10"}, "MAP 2.3"),
    "EC-10": ("conditional-support", {"MEASURE 1.1", "MEASURE 3.1"}, None),
    "EC-11": ("unmapped-evidence-property", set(), "MANAGE 2.4"),
    "EC-12": ("unmapped-evidence-property", set(), "GOVERN 5.2"),
}


def test_nist_ai_rmf_twelve_dispositions_are_pinned():
    crosswalk = load_crosswalk()
    rows = [m for m in crosswalk["mappings"] if m["framework_id"] == NIST_AI_RMF]
    unmapped = {e["claim_id"]: e for e in crosswalk["unmapped_evidence_properties"] if e["framework_id"] == NIST_AI_RMF}
    assert {c["id"] for c in crosswalk["claims"]} == set(NIST_DISPOSITIONS)
    for claim_id, (disposition, controls, superseded) in NIST_DISPOSITIONS.items():
        assert disposition in VALID_DISPOSITIONS
        claim_rows = [m for m in rows if m["claim_id"] == claim_id]
        assert {m["control_id"] for m in claim_rows} == controls, claim_id
        if disposition == "unmapped-evidence-property":
            assert claim_rows == []
            assert unmapped[claim_id]["disposition"] == disposition
            assert unmapped[claim_id]["superseded_control_id"] == superseded
            continue
        assert claim_id not in unmapped
        for row in claim_rows:
            assert row["disposition"] == disposition
            assert row.get("superseded_control_id") == superseded
            assert row["relationship"] in {"supports", "partially-supports"}


def test_nist_ai_rmf_rows_carry_full_provenance():
    crosswalk = load_crosswalk()
    framework = next(f for f in crosswalk["frameworks"] if f["id"] == NIST_AI_RMF)
    assert framework["source_locator"] == AI_100_1_PDF
    assert "AI 100-1" in framework["edition"]
    entries = [m for m in crosswalk["mappings"] if m["framework_id"] == NIST_AI_RMF]
    entries += [e for e in crosswalk["unmapped_evidence_properties"] if e["framework_id"] == NIST_AI_RMF]
    for entry in entries:
        assert entry["edition"] == framework["edition"]
        assert entry["source_locator"].startswith(AI_100_1_PDF + " Section 5.")
        assert "Table " in entry["source_locator"]
        assert entry["verification_limit"].strip()
        assert "does not establish that an organizational control or process operated" in entry["verification_limit"]
        if "control_id" in entry:
            for key in PROVENANCE_FIELDS:
                if key != "superseded_control_id":
                    assert entry[key].strip(), (entry["claim_id"], key)
            assert entry["applicability_condition"] != "any"
            assert entry["source_locator"].endswith(entry["control_id"])
            assert "-" not in entry["control_id"]
        else:
            for key in ("artifact_contract", "responsible_actor", "applicability_condition"):
                assert entry[key].strip(), (entry["claim_id"], key)
            assert entry["applicability_condition"].startswith("Not applicable:"), entry["claim_id"]


def test_nist_ai_600_1_stays_not_sourced():
    crosswalk = load_crosswalk()
    framework = next(f for f in crosswalk["frameworks"] if f["id"] == "nist-ai-600-1")
    assert framework["mapping_status"] == "identifiers-not-sourced"
    assert not [m for m in crosswalk["mappings"] if m["framework_id"] == "nist-ai-600-1"]


def test_licensed_clause_text_is_marked_unverified():
    crosswalk = load_crosswalk()
    licensed = [f for f in crosswalk["frameworks"] if f["licensing"] == "licensed"]
    assert licensed
    rendered = control_crosswalk.render_doc()
    for framework in licensed:
        assert framework["clause_text_status"] == "unverified"
        assert "unverified" in framework["note"]
    assert rendered.count("Clause text: unverified.") == len(licensed)


def test_crosswalk_qualifies_immutability():
    raw = (REPO_ROOT / "src" / "brigade" / "templates" / "control-crosswalk.json").read_text()
    assert "append-only" not in raw.lower()
    assert "are immutable" not in raw
    crosswalk = load_crosswalk()
    assert "not immutable" in crosswalk["integrity_boundary"]
    assert "not detectable without a digest, signature or copy held outside" in crosswalk["integrity_boundary"]
    assert "tamper-evident" in crosswalk["integrity_boundary"]
    assert "Neither means the control is fulfilled" in crosswalk["relationship_semantics"]


def test_ec12_claim_matches_receipts_package_commands(capsys):
    crosswalk = load_crosswalk()
    claim = next(c for c in crosswalk["claims"] if c["id"] == "EC-12")
    assert claim["producer_command"].startswith("brigade receipts export package ")
    assert ".brigade/evidence-packages/" in claim["producer_command"]
    assert claim["verifier_command"].startswith("brigade receipts verify-package ")
    assert claim["state_rule"] == control_crosswalk._STATE_RULES["EC-12"]
    for c in crosswalk["claims"]:
        assert c["state_rule"] == control_crosswalk._STATE_RULES[c["id"]], c["id"]
    help_text = {}
    for argv in (["receipts", "export", "package", "--help"], ["receipts", "verify-package", "--help"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 0, argv
        help_text[argv[1]] = capsys.readouterr().out
    for flag in ("--run-id", "--out"):
        assert flag in claim["producer_command"]
        assert flag in help_text["export"], flag
        assert f"[{flag}" not in help_text["export"], f"{flag} should be required"


def test_json_output_lists_unmapped_properties_without_state(tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    evaluated = control_crosswalk.evaluate_controls(target, framework_id=NIST_AI_RMF)
    assert {e["claim_id"] for e in evaluated["unmapped_evidence_properties"]} == {"EC-03", "EC-11", "EC-12"}
    assert all("state" not in e for e in evaluated["unmapped_evidence_properties"])
    for entry in evaluated["unmapped_evidence_properties"]:
        for key in ("artifact_contract", "responsible_actor", "applicability_condition"):
            assert entry[key].strip(), (entry["claim_id"], key)
    assert {m["claim_id"] for m in evaluated["mappings"]}.isdisjoint({"EC-03", "EC-11", "EC-12"})
    assert all(m["verification_limit"] for m in evaluated["mappings"])
    other = control_crosswalk.evaluate_controls(target, framework_id="iso-42001-2023")
    assert other["unmapped_evidence_properties"] == []


def test_rendered_doc_shows_unmapped_property_contracts():
    crosswalk = load_crosswalk()
    rendered = control_crosswalk.render_doc()
    assert "| Artifact contract | Responsible actor | Applicability |" in rendered
    for entry in crosswalk["unmapped_evidence_properties"]:
        for key in ("artifact_contract", "responsible_actor", "applicability_condition"):
            assert entry[key] in rendered, (entry["claim_id"], key)


# --- brigade.claim_readiness.v1 contract (#1617) ---------------------------

RUN = "research-run"


def _ws(tmp_path: Path) -> Path:
    target = tmp_path / "ws"
    target.mkdir()
    return target


def _assess(target: Path, claim_id: str, run_id: str | None = None, **kwargs):
    return control_crosswalk.assess_claim(target, claim_id, run_id, **kwargs).to_dict()


def _verify_dir(target: Path, name: str) -> Path:
    return target / ".brigade" / "work" / "verify-runs" / name


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture_ec01(target: Path) -> None:
    _write_json(
        _verify_dir(target, "v1") / "receipt.json",
        {"run_id": "v1", "producer_run_id": RUN, "status": "completed", "commands": []},
    )


def _fixture_ec03(target: Path) -> None:
    from brigade import cosign_attestation

    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1", producer_run_id=RUN)
    _write_json(
        _verify_dir(target, "v1") / "attestation.sigstore.json",
        {"mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE, "dsseEnvelope": {}},
    )


def _fixture_ec07(target: Path) -> None:
    _write_json(target / ".brigade" / "governance" / "inventory.json", {})


def _fixture_ec09(target: Path) -> None:
    _write_json(target / ".brigade" / "work" / "guard" / "audit.json", {"summary": {}})


def _fixture_ec10(target: Path) -> None:
    path = target / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n", encoding="utf-8")


def _fixture_ec11(target: Path) -> None:
    path = target / ".brigade" / "work" / "verify-archive" / "index.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")


def _package_manifest(entries: list[dict], *, verify_run_id: str | None = None) -> dict:
    manifest: dict = {
        "schema": "brigade.evidence_package.v1",
        "schema_version": 1,
        "created_at": "2026-09-06T00:00:00Z",
        "entries": entries,
        "entries_sha256": localio.canonical_json_digest(entries),
        "limitations": [],
    }
    if verify_run_id is not None:
        manifest["source"] = {"verify_run_id": verify_run_id, "receipt_sha256": "b" * 64}
    return manifest


_RECEIPT_ENTRY = {
    "path": "receipt.json",
    "sha256": "a" * 64,
    "bytes": 2,
    "media_type": "application/json",
    "kind": "receipt",
}


def _fixture_ec12(target: Path) -> None:
    _write_json(
        target / ".brigade" / "evidence-packages" / "pkg-1" / "manifest.json", _package_manifest([_RECEIPT_ENTRY])
    )


# claim -> (fixture, workspace outcome, workspace reason, run-scoped outcome)
_ISSUE_FIXTURES = {
    "EC-01": (_fixture_ec01, "invalid", "schema_missing", "invalid"),
    "EC-03": (_fixture_ec03, "invalid", "dsse_signatures_missing", "invalid"),
    "EC-07": (_fixture_ec07, "invalid", "schema_missing", "incomplete"),
    "EC-09": (_fixture_ec09, "incomplete", "no_explicit_verdict", "incomplete"),
    "EC-10": (_fixture_ec10, "invalid", "invalid_jsonl_line", "incomplete"),
    "EC-11": (_fixture_ec11, "invalid", "schema_missing", "incomplete"),
    "EC-12": (_fixture_ec12, "rejected", "entry_missing", "incomplete"),
}


@pytest.mark.parametrize("claim_id", sorted(_ISSUE_FIXTURES))
def test_issue_1617_fixtures_are_not_presented_as_validated(tmp_path, claim_id):
    fixture, workspace_outcome, workspace_reason, run_outcome = _ISSUE_FIXTURES[claim_id]
    target = _ws(tmp_path)
    fixture(target)

    # The issue reproduction call keeps its signature and no longer passes.
    state = control_crosswalk._evaluate_claim_state(target, claim_id, RUN)
    assert state != "evidenced_passed"
    assert state in control_crosswalk.VALID_STATES

    run_scoped = _assess(target, claim_id, RUN)
    assert run_scoped["outcome"] == run_outcome
    assert run_scoped["outcome"] != "validated"
    assert run_scoped["validation_level"] != "claim_validated"

    workspace = _assess(target, claim_id)
    assert workspace["outcome"] == workspace_outcome
    assert workspace_reason in workspace["reason_codes"]
    assert workspace["population"]["discovered"] == 1
    assert workspace["artifacts"][0]["outcome"] == workspace_outcome


def test_issue_fixture_ec12_rejected_in_workspace_scope_and_unbound_for_run(tmp_path):
    target = _ws(tmp_path)
    _fixture_ec12(target)
    assert control_crosswalk._evaluate_claim_state(target, "EC-12", None) == "evidenced_failed"
    run_scoped = _assess(target, "EC-12", RUN)
    # Whole-workspace artifacts are never inferred to belong to the requested run.
    assert run_scoped["population"]["unbound"] == 1
    assert run_scoped["population"]["in_scope"] == 0
    assert run_scoped["reason"] == "artifact_not_run_bound"
    assert run_scoped["artifacts"][0]["outcome"] == "rejected"
    assert run_scoped["legacy_state"] == "untested"


def test_empty_intended_check_list_never_shows_test_execution(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    receipt_path = _verify_dir(target, "v1") / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["commands"] = []
    receipt_path.write_text(json.dumps(receipt))

    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "incomplete"
    assert readiness["reason"] == "empty_commands"
    assert readiness["dimensions"]["population"] == {"status": "unknown", "reason": "empty_commands"}
    assert readiness["legacy_state"] == "untested"


def test_well_formed_discovery_fixture_is_discoverable_with_honest_label(tmp_path):
    target = _ws(tmp_path)
    _write_json(
        target / ".brigade" / "governance" / "inventory.json",
        {"schema": "brigade.governance_inventory.v1", "generated_at": "2026-09-01T00:00:00+00:00"},
    )
    readiness = _assess(target, "EC-07")
    assert readiness["validation_level"] == "structure_observed"
    assert readiness["outcome"] == "structure_observed"
    assert readiness["population"]["discovered"] == 1
    assert readiness["legacy_state"] == "untested"
    assert readiness["verifier"] == {"name": None, "status": "not_wired"}
    assert readiness["dimensions"]["subject"]["status"] == "not_applicable"


def test_mixed_population_failed_sibling_is_not_hidden(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "a-pass"), run_id="a-pass")
    _write_verify_receipt(
        _verify_dir(target, "b-fail"),
        run_id="b-fail",
        commands=[{"command": "pytest", "status": "completed", "exit_code": 1}],
    )
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "failed"
    assert readiness["legacy_state"] == "evidenced_failed"
    assert readiness["population"]["structure_observed"] == 1
    assert readiness["population"]["failed"] == 1
    assert [a["relpath"] for a in readiness["artifacts"]] == [
        ".brigade/work/verify-runs/a-pass/receipt.json",
        ".brigade/work/verify-runs/b-fail/receipt.json",
    ]


def test_mixed_population_incomplete_and_malformed_siblings_are_not_hidden(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "a-pass"), run_id="a-pass")
    _write_verify_receipt(_verify_dir(target, "b-running"), run_id="b-running", status="running")
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "incomplete"
    assert readiness["population"]["structure_observed"] == 1
    assert readiness["population"]["incomplete"] == 1
    assert readiness["legacy_state"] == "untested"

    (_verify_dir(target, "c-bad")).mkdir(parents=True)
    (_verify_dir(target, "c-bad") / "receipt.json").write_text("{not json")
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "invalid"
    assert readiness["population"]["invalid"] == 1


def test_mixed_population_valid_and_rejected_package_manifests(tmp_path):
    target = _ws(tmp_path)
    good = target / ".brigade" / "evidence-packages" / "pkg-a"
    _write_json(good / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))
    (good / "receipt.json").write_text("{}")
    bad_manifest = _package_manifest([_RECEIPT_ENTRY])
    bad_manifest["entries_sha256"] = "c" * 64
    bad = target / ".brigade" / "evidence-packages" / "pkg-b"
    _write_json(bad / "manifest.json", bad_manifest)
    (bad / "receipt.json").write_text("{}")

    readiness = _assess(target, "EC-12")
    assert readiness["outcome"] == "rejected"
    assert readiness["reason"] == "entries_digest_mismatch"
    assert readiness["population"]["structure_observed"] == 1
    assert readiness["population"]["rejected"] == 1
    assert readiness["dimensions"]["integrity"]["status"] == "failed"


def test_wrong_run_receipt_is_excluded_and_counted(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "run-b"), run_id="run-b", producer_run_id="producer-2")
    readiness = _assess(target, "EC-01", "producer-1")
    assert readiness["outcome"] == "absent"
    assert readiness["reason"] == "only_out_of_scope"
    assert readiness["population"]["wrong_run"] == 1
    assert readiness["population"]["in_scope"] == 0
    assert readiness["artifacts"][0]["scope"] == "wrong_run"

    scoped = _assess(target, "EC-01", "producer-2")
    assert scoped["outcome"] == "structure_observed"
    assert scoped["artifacts"][0]["run_binding"] == "producer_run_id"


def test_wrong_run_package_manifest_uses_recorded_verify_run_id(tmp_path):
    target = _ws(tmp_path)
    pkg = target / ".brigade" / "evidence-packages" / "pkg-1"
    _write_json(pkg / "manifest.json", _package_manifest([_RECEIPT_ENTRY], verify_run_id="other-run"))
    (pkg / "receipt.json").write_text("{}")
    readiness = _assess(target, "EC-12", RUN)
    assert readiness["population"]["wrong_run"] == 1
    assert readiness["outcome"] == "absent"
    bound = _assess(target, "EC-12", "other-run")
    assert bound["population"]["in_scope"] == 1
    assert bound["artifacts"][0]["run_binding"] == "source.verify_run_id"


def test_ec02_unreadable_receipt_no_longer_skips_run_check(tmp_path):
    target = _ws(tmp_path)
    run_dir = _verify_dir(target, "v1")
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text("{broken")
    (run_dir / "attestation.json").write_text("{}")
    readiness = _assess(target, "EC-02", RUN)
    assert readiness["population"]["unbound"] == 1
    assert readiness["population"]["in_scope"] == 0
    assert readiness["outcome"] == "incomplete"
    assert readiness["legacy_state"] == "untested"


def test_missing_ssh_keygen_is_unavailable_not_failed(tmp_path, monkeypatch):
    import shutil

    target = _ws(tmp_path)
    run_dir = _verify_dir(target, "v1")
    _write_verify_receipt(run_dir, run_id="v1")
    (run_dir / "attestation.json").write_text("{}")
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "ssh-keygen" else real_which(name))

    readiness = _assess(target, "EC-02")
    assert readiness["outcome"] == "unavailable"
    assert readiness["verifier"]["status"] == "unavailable"
    assert readiness["reason"] == "verifier_tool_unavailable"
    assert readiness["dimensions"]["signature"]["status"] == "unavailable"
    assert control_crosswalk._evaluate_claim_state(target, "EC-02", None) == "untested"


def test_missing_git_is_unavailable_and_non_repo_is_not_applicable(tmp_path, monkeypatch):
    import shutil

    target = _ws(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    not_repo = _assess(target, "EC-08")
    assert not_repo["outcome"] == "not_applicable"
    assert not_repo["reason"] == "not_git_repository"
    assert not_repo["legacy_state"] == "not_applicable"

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "git" else real_which(name))
    missing = _assess(target, "EC-08")
    assert missing["outcome"] == "unavailable"
    assert missing["verifier"]["status"] == "unavailable"
    assert missing["legacy_state"] == "untested"


def _stub_attestation(monkeypatch, result) -> None:
    from brigade import attestation

    def fake_verify(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(attestation, "verify_attestation", fake_verify)


def _ec02_target(tmp_path: Path) -> Path:
    target = _ws(tmp_path)
    run_dir = _verify_dir(target, "v1")
    _write_verify_receipt(run_dir, run_id="v1")
    (run_dir / "attestation.json").write_text("{}")
    return target


def test_signed_ok_without_rederived_subject_is_rejected(tmp_path, monkeypatch):
    from brigade import attestation

    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)

    target = _ec02_target(tmp_path)
    _stub_attestation(monkeypatch, attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK))
    readiness = _assess(target, "EC-02")
    assert readiness["dimensions"]["signature"]["status"] == "passed"
    assert readiness["dimensions"]["subject"] == {"status": "failed", "reason": "subject_mismatch"}
    assert readiness["outcome"] == "rejected"
    assert readiness["legacy_state"] == "evidenced_failed"


def test_untrusted_key_fails_authorization_independently(tmp_path, monkeypatch):
    from brigade import attestation

    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)

    target = _ec02_target(tmp_path)
    _stub_attestation(monkeypatch, attestation.AttestationVerifyResult(status=attestation.STATUS_UNTRUSTED_KEY))
    readiness = _assess(target, "EC-02")
    assert readiness["dimensions"]["authorization"] == {"status": "failed", "reason": "untrusted_key"}
    assert readiness["outcome"] == "rejected"


def test_signed_ok_rederived_validates_and_verifier_exception_is_unavailable(tmp_path, monkeypatch):
    from brigade import attestation

    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)

    target = _ec02_target(tmp_path)
    _stub_attestation(
        monkeypatch,
        attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, rederived=True, run_id="v1"),
    )
    readiness = _assess(target, "EC-02")
    assert readiness["outcome"] == "validated"
    assert readiness["validation_level"] == "claim_validated"
    assert readiness["legacy_state"] == "evidenced_passed"

    _stub_attestation(monkeypatch, RuntimeError("boom"))
    readiness = _assess(target, "EC-02")
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_error"


def test_passed_signature_cannot_override_failed_prerequisite():
    from brigade import control_readiness

    obs = control_readiness.ArtifactObservation(
        relpath="x.json",
        level="claim_validated",
        proposed="validated",
        dimensions=control_readiness.dims(
            integrity="passed",
            signature="passed",
            authorization="passed",
            subject="passed",
            freshness=("failed", None),
            population="passed",
        ),
    )
    required = frozenset({"integrity", "signature", "authorization", "subject", "freshness", "population"})
    assert control_readiness.finalize_artifact(obs, required).outcome == "rejected"

    presence = control_readiness.ArtifactObservation(
        relpath="y.json", level="discovered", proposed="validated", dimensions=control_readiness.dims()
    )
    # A presence-only observation can never display validated evidence.
    assert control_readiness.finalize_artifact(presence, frozenset({"population"})).outcome == "discovered_only"


def test_adapter_seam_cannot_bypass_independent_dimensions(tmp_path, monkeypatch):
    from brigade import control_readiness

    target = _ws(tmp_path)
    pkg = target / ".brigade" / "evidence-packages" / "pkg-1"
    _write_json(pkg / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))

    def adapter(_target, package_dir):
        return control_readiness.ArtifactObservation(
            relpath=".brigade/evidence-packages/pkg-1/manifest.json",
            level="claim_validated",
            proposed="validated",
            dimensions=control_readiness.dims(
                integrity="passed", subject=("failed", "subject_mismatch"), population="passed"
            ),
        )

    monkeypatch.setitem(control_crosswalk._VERIFIER_ADAPTERS, "evidence-package", adapter)
    readiness = _assess(target, "EC-12")
    assert readiness["outcome"] == "rejected"
    assert readiness["reason"] == "subject_mismatch"


def test_journal_partial_tail_is_incomplete_and_chain_error_rejected(tmp_path):
    target = _ws(tmp_path)
    journal = target / ".brigade" / "runs" / "run-1" / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b'{"partial":')
    readiness = _assess(target, "EC-06")
    assert readiness["outcome"] == "incomplete"
    assert "journal_partial_tail" in readiness["reason_codes"]
    assert readiness["legacy_state"] == "untested"

    journal.write_bytes(b"not json\n")
    readiness = _assess(target, "EC-06")
    assert readiness["outcome"] == "rejected"
    assert readiness["legacy_state"] == "evidenced_failed"


def test_approval_statuses_map_to_distinct_outcomes(tmp_path, monkeypatch):
    from brigade import approval

    target = _ws(tmp_path)
    (target / ".brigade" / "runs" / "run-1").mkdir(parents=True)
    expected = {
        ("APPROVED", "PASSED"): ("validated", "evidenced_passed"),
        ("APPROVED", "FAILED"): ("failed", "evidenced_failed"),
        ("DENIED", None): ("failed", "evidenced_failed"),
        ("HELD", None): ("incomplete", "untested"),
        ("SOD-INDETERMINATE", None): ("incomplete", "untested"),
        ("APPROVAL-INVALID", None): ("rejected", "evidenced_failed"),
        ("UNAPPROVED", None): ("absent", "untested"),
    }
    for (status, sod), (outcome, state) in expected.items():
        verification = approval.ApprovalVerification("run-1", status, None, {"result": sod} if sod else None)
        monkeypatch.setattr(approval, "verify_run_approval", lambda *a, _v=verification, **k: _v)
        readiness = _assess(target, "EC-05")
        assert (readiness["outcome"], readiness["legacy_state"]) == (outcome, state), status


def test_assessment_period_freshness(tmp_path):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")

    unassessed = _assess(target, "EC-01")
    assert unassessed["dimensions"]["freshness"] == {"status": "not_applicable", "reason": "period_not_supplied"}
    assert "freshness" not in unassessed["required_dimensions"]

    inside = _assess(target, "EC-01", period=("2025-12-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"))
    assert inside["outcome"] == "structure_observed"
    assert inside["dimensions"]["freshness"]["status"] == "passed"
    assert "freshness" in inside["required_dimensions"]

    outside = _assess(target, "EC-01", period=("2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00"))
    assert outside["population"]["out_of_period"] == 1
    assert outside["outcome"] == "absent"
    assert outside["legacy_state"] == "untested"


def test_assessment_period_missing_timestamp_is_incomplete(tmp_path):
    target = _ws(tmp_path)
    receipt_path = _verify_dir(target, "v1") / "receipt.json"
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    receipt = json.loads(receipt_path.read_text())
    del receipt["started_at"], receipt["completed_at"]
    receipt_path.write_text(json.dumps(receipt))
    readiness = _assess(target, "EC-01", period=("2026-01-01T00:00:00+00:00", "2026-12-31T00:00:00+00:00"))
    assert readiness["dimensions"]["freshness"] == {"status": "unknown", "reason": "timestamp_missing"}
    assert readiness["outcome"] == "incomplete"


@pytest.mark.parametrize(
    "period",
    [
        ("2026-01-01T00:00:00", "2026-02-01T00:00:00+00:00"),
        ("2026-03-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"),
        ("not-a-date", "2026-02-01T00:00:00+00:00"),
    ],
)
def test_assessment_period_rejects_naive_reversed_or_unparseable(tmp_path, period):
    with pytest.raises(control_crosswalk.ControlCrosswalkError):
        control_crosswalk.evaluate_controls(_ws(tmp_path), period=period)


def test_truncated_population_never_validates(tmp_path, monkeypatch):
    target = _ws(tmp_path)
    for name in ("a", "b", "c"):
        _write_verify_receipt(_verify_dir(target, name), run_id=name)
    monkeypatch.setattr(control_crosswalk, "_MAX", 2)
    readiness = _assess(target, "EC-01")
    assert readiness["population"]["truncated"] is True
    assert readiness["population"]["discovered"] == 2
    assert readiness["population"]["structure_observed"] == 2
    assert readiness["outcome"] == "incomplete"
    assert readiness["reason"] == "population_truncated"
    assert readiness["legacy_state"] == "untested"


def test_v2_envelope_is_deterministic_bounded_and_relative(tmp_path):
    from brigade import control_readiness

    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    _fixture_ec12(target)
    _fixture_ec10(target)
    first = control_crosswalk.evaluate_controls(target)
    second = control_crosswalk.evaluate_controls(target)
    first.pop("evaluated_at")
    second.pop("evaluated_at")
    assert first == second
    assert first["schema"] == "brigade.evidence_controls.v2"
    assert first["readiness_contract"] == control_readiness.READINESS_CONTRACT
    assert first["assessment_period"] is None
    assert sorted(first["evidence_readiness"]) == sorted(c["id"] for c in load_crosswalk()["claims"])
    rendered = json.dumps(first["evidence_readiness"])
    assert str(tmp_path) not in rendered
    for readiness in first["evidence_readiness"].values():
        assert readiness["outcome"] in control_readiness.OUTCOMES
        assert readiness["validation_level"] in control_readiness.LEVELS
        assert set(readiness["reason_codes"]) <= control_readiness.REASON_CODES
        assert set(readiness["dimensions"]) == set(control_readiness.DIMENSIONS)
        assert len(readiness["artifacts"]) <= control_readiness.MAX_REPORTED_ARTIFACTS
        assert readiness["legacy_state"] in control_crosswalk.VALID_STATES
    for row in first["mappings"]:
        assert row["state"] in control_crosswalk.VALID_STATES
        assert row["state"] == first["evidence_readiness"][row["claim_id"]]["legacy_state"] or (
            row["relationship"] == "no-relationship" and row["state"] == "not_applicable"
        )


def test_readiness_stays_separate_from_scoring_and_enforcement(tmp_path, capsys):
    target = _ws(tmp_path)
    _write_verify_receipt(
        _verify_dir(target, "v1"),
        run_id="v1",
        commands=[{"command": "pytest", "status": "completed", "exit_code": 1}],
    )
    rc = cli.main(["evidence", "controls", "--target", str(target), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["notice"] == HEADER_NOTICE
    assert payload["evidence_readiness"]["EC-01"]["outcome"] == "failed"
    rendered = json.dumps(payload)
    for forbidden in ('"score"', '"compliant"', '"certified"'):
        assert forbidden not in rendered

    rc = cli.main(["evidence", "controls", "--target", str(target)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "[evidenced_failed | structure_observed/failed]" in text
    assert HEADER_NOTICE in text


def test_rendered_doc_documents_state_contract_and_migration():
    rendered = control_crosswalk.render_doc()
    assert "## Evidence state contract" in rendered
    assert "brigade.evidence_controls.v2" in rendered
    assert "brigade.claim_readiness.v1" in rendered
    assert "Migration from `brigade.evidence_controls.v1`" in rendered
    assert "period_not_supplied" in rendered


# --- #1617 review-fix regressions -------------------------------------------


def test_ec02_attestation_copied_from_another_run_dir_is_rejected(tmp_path, monkeypatch):
    from brigade import attestation

    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)

    target = _ws(tmp_path)
    run_dir = _verify_dir(target, "run-b")
    _write_verify_receipt(run_dir, run_id="run-b", producer_run_id=RUN)
    (run_dir / "attestation.json").write_text("{}")
    calls: list[dict] = []

    def fake_verify(path, **kwargs):
        calls.append(kwargs)
        # A valid attestation for run-a, re-derived against run-a's receipt.
        return attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, rederived=True, run_id="run-a")

    monkeypatch.setattr(attestation, "verify_attestation", fake_verify)
    readiness = _assess(target, "EC-02", RUN)
    assert readiness["outcome"] == "rejected"
    assert readiness["dimensions"]["signature"]["status"] == "passed"
    assert readiness["dimensions"]["subject"] == {"status": "failed", "reason": "subject_mismatch"}
    assert readiness["legacy_state"] == "evidenced_failed"
    # Re-derivation is pinned to the attestation's own directory receipt.
    assert calls[0]["receipt"]["run_id"] == "run-b"


def _append_events(journal: Path, run_id: str, count: int = 2) -> None:
    from brigade import run_journal

    journal.parent.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        run_journal.append_event(
            journal,
            run_id=run_id,
            event_type="run.planning.started",
            payload={"detail": f"step {index}"},
            idempotency_key=f"evt-{index}",
            expected_previous_sequence=index,
            recorded_at="2026-01-01T00:00:00.000000Z",
        )


def test_ec06_intact_journal_copied_from_another_run_is_rejected(tmp_path):
    target = _ws(tmp_path)
    runs = target / ".brigade" / "runs"
    _append_events(runs / "run-a" / "events" / "lifecycle.jsonl", "run-a")
    own = _assess(target, "EC-06", "run-a")
    assert own["outcome"] == "validated"
    # A successful claim has no primary reason code: null, never "".
    assert own["reason"] is None
    assert own["dimensions"]["subject"]["status"] == "passed"

    copied = runs / "run-b" / "events" / "lifecycle.jsonl"
    copied.parent.mkdir(parents=True)
    copied.write_bytes((runs / "run-a" / "events" / "lifecycle.jsonl").read_bytes())
    readiness = _assess(target, "EC-06", "run-b")
    assert readiness["outcome"] == "rejected"
    assert readiness["dimensions"]["subject"] == {"status": "failed", "reason": "run_binding_mismatch"}
    assert readiness["dimensions"]["integrity"]["status"] == "passed"
    assert readiness["reason"] == "run_binding_mismatch"


def _git_commit(target: Path, message: str) -> None:
    import subprocess

    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            message,
        ],
        cwd=target,
        check=True,
    )


def test_ec08_validates_recent_trailer_in_repo_longer_than_window(tmp_path, monkeypatch):
    import shutil
    import subprocess

    from brigade import causal_receipt

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    target = _ws(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    run_json = {"run_id": "run-1", "status": "completed"}
    _write_json(target / ".brigade" / "runs" / "run-1" / "run.json", run_json)
    digest = causal_receipt.receipt_digest(run_json)
    # Older history outside the defined window carries a mismatching trailer.
    _git_commit(target, "old\n\nBrigade-Run: run-1\nBrigade-Receipt: sha256:" + "0" * 64)
    for index in range(24):
        _git_commit(target, f"filler {index}")
    _git_commit(target, f"recent\n\nBrigade-Run: run-1\nBrigade-Receipt: sha256:{digest}")

    readiness = _assess(target, "EC-08")
    assert readiness["outcome"] == "validated"
    assert readiness["legacy_state"] == "evidenced_passed"
    assert readiness["population"]["truncated"] is False
    assert readiness["population"]["window"] == {
        "kind": "most_recent_commits",
        "limit": 20,
        "inspected": 20,
        "with_trailer": 1,
        "without_trailer": 19,
        "unreadable": 0,
    }
    assert readiness["population"]["in_scope"] == 1
    assert "population_truncated" not in readiness["reason_codes"]
    assert _assess(target, "EC-08", "run-1")["outcome"] == "validated"


def test_ec08_missing_git_stays_unavailable_under_run_scope(tmp_path, monkeypatch):
    import shutil

    target = _ws(tmp_path)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "git" else real_which(name))
    readiness = _assess(target, "EC-08", RUN)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_tool_unavailable"
    assert "verifier_tool_unavailable" in readiness["reason_codes"]
    assert readiness["population"]["unbound"] == 0
    assert readiness["population"]["in_scope"] == 1
    assert readiness["verifier"]["status"] == "unavailable"
    assert readiness["dimensions"]["population"] == {"status": "unavailable", "reason": "verifier_tool_unavailable"}


def test_ec08_whole_history_verifier_error_stays_unavailable_under_run_scope(tmp_path, monkeypatch):
    import subprocess

    target = _ws(tmp_path)
    monkeypatch.setattr(control_crosswalk, "_is_git_repo", lambda _target: True)
    monkeypatch.setattr(
        control_crosswalk, "_git", lambda ctx, *args: subprocess.CompletedProcess(args, 128, "", "fatal")
    )
    readiness = _assess(target, "EC-08", RUN)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_error"
    assert "artifact_not_run_bound" not in readiness["reason_codes"]


def test_selected_run_is_inspected_directly_amid_many_siblings(tmp_path):
    target = _ws(tmp_path)
    runs = target / ".brigade" / "runs"
    for index in range(control_readiness_max() + 5):
        (runs / f"a-{index:04d}").mkdir(parents=True)
    _append_events(runs / "zz-selected" / "events" / "lifecycle.jsonl", "zz-selected")

    selected = _assess(target, "EC-06", "zz-selected")
    assert selected["outcome"] == "validated"
    assert selected["population"]["truncated"] is False
    assert selected["population"]["in_scope"] == 1

    workspace = _assess(target, "EC-06")
    assert workspace["population"]["truncated"] is True
    assert workspace["outcome"] == "incomplete"
    assert workspace["reason"] == "population_truncated"


def control_readiness_max() -> int:
    from brigade import control_readiness

    return control_readiness.MAX_DISCOVERED_PER_CLAIM


def test_directory_scan_reads_a_bounded_number_of_entries(tmp_path, monkeypatch):
    import os

    target = _ws(tmp_path)
    for index in range(12):
        (_verify_dir(target, f"r{index:02d}")).mkdir(parents=True)
    monkeypatch.setattr(control_crosswalk, "_MAX", 3)
    real_scandir = os.scandir
    pulled: list[str] = []

    class CountingScandir:
        def __init__(self, path):
            self._inner = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()

        def __iter__(self):
            for entry in self._inner:
                pulled.append(entry.name)
                yield entry

    monkeypatch.setattr(os, "scandir", CountingScandir)
    readiness = _assess(target, "EC-01")
    assert len(pulled) == 4
    assert readiness["population"]["truncated"] is True
    assert readiness["outcome"] == "incomplete"


def test_unsafe_run_id_is_rejected(tmp_path, capsys):
    target = _ws(tmp_path)
    for bad in ("../escape", "a/b", "..", "."):
        with pytest.raises(control_crosswalk.ControlCrosswalkError):
            control_crosswalk.assess_claim(target, "EC-06", bad)
    rc = cli.main(["evidence", "controls", "--target", str(target), "--run-id", "../escape"])
    assert rc == 2
    assert "bare run directory name" in capsys.readouterr().err


def test_unreadable_or_symlinked_discovery_root_is_not_absent(tmp_path):
    target = _ws(tmp_path)
    root = target / ".brigade" / "work" / "verify-runs"
    root.parent.mkdir(parents=True)
    root.write_text("not a directory")
    readiness = _assess(target, "EC-01", RUN)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "discovery_unreadable"
    assert readiness["population"]["unbound"] == 0

    root.unlink()
    elsewhere = target / "elsewhere"
    elsewhere.mkdir()
    try:
        root.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "invalid"
    assert readiness["reason"] == "symlink_refused"


def _agg_obs(relpath: str, level: str, proposed: str, **dims):
    from brigade import control_readiness

    return control_readiness.ArtifactObservation(
        relpath=relpath, level=level, proposed=proposed, dimensions=control_readiness.dims(**dims)
    )


_VALIDATED_DIMS = {"integrity": "passed", "subject": "passed", "population": "passed"}
_STRUCTURE_DIMS = {
    "integrity": ("not_checked", "verifier_not_wired"),
    "subject": "not_applicable",
    "population": "passed",
}


@pytest.mark.parametrize("validated_first", [True, False])
@pytest.mark.parametrize(
    ("sibling_level", "sibling_outcome", "sibling_dims", "expected"),
    [
        ("structure_observed", "structure_observed", _STRUCTURE_DIMS, "structure_observed"),
        ("discovered", "discovered_only", {}, "discovered_only"),
        ("structure_observed", "incomplete", {"population": ("unknown", "journal_empty")}, "incomplete"),
        ("claim_validated", "failed", {**_VALIDATED_DIMS}, "failed"),
        ("claim_validated", "rejected", {"integrity": ("failed", "journal_chain_error")}, "rejected"),
    ],
)
def test_validated_artifact_never_lifts_a_weaker_sibling(
    validated_first, sibling_level, sibling_outcome, sibling_dims, expected
):
    from brigade import control_readiness

    names = ("a.json", "b.json") if validated_first else ("b.json", "a.json")
    observations = [
        _agg_obs(names[0], "claim_validated", "validated", **_VALIDATED_DIMS),
        _agg_obs(names[1], sibling_level, sibling_outcome, **sibling_dims),
    ]
    readiness = control_readiness.aggregate(
        claim_id="EC-XX",
        evaluator={},
        verifier={},
        required=frozenset({"integrity", "subject", "population"}),
        observations=observations,
        truncated=False,
        scan_limit=200,
        run_scoped=False,
        period_supplied=False,
    )
    assert readiness.outcome == expected
    assert readiness.legacy_state() != "evidenced_passed"
    assert readiness.population["validated"] == 1
    if expected in {"structure_observed", "discovered_only"}:
        assert readiness.level != "claim_validated"
        assert any(readiness.dimensions[name]["status"] != "passed" for name in ("integrity", "subject"))


def test_population_not_applicable_is_not_upgraded_without_enumeration():
    from brigade import control_readiness

    signed = {"integrity": "passed", "subject": "passed", "population": "not_applicable"}
    common = {
        "claim_id": "EC-XX",
        "evaluator": {},
        "verifier": {},
        "required": frozenset({"integrity", "subject", "population"}),
        "scan_limit": 200,
        "run_scoped": False,
        "period_supplied": False,
    }
    complete = control_readiness.aggregate(
        observations=[_agg_obs("a.json", "claim_validated", "validated", **signed)], truncated=False, **common
    )
    assert complete.dimensions["population"]["status"] == "passed"
    truncated = control_readiness.aggregate(
        observations=[_agg_obs("a.json", "claim_validated", "validated", **signed)], truncated=True, **common
    )
    assert truncated.dimensions["population"] == {"status": "unknown", "reason": "population_truncated"}
    assert truncated.outcome == "incomplete"


def test_adapter_outside_the_vocabulary_is_a_verifier_error(tmp_path, monkeypatch):
    from brigade import control_readiness

    target = _ws(tmp_path)
    pkg = target / ".brigade" / "evidence-packages" / "pkg-1"
    _write_json(pkg / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))

    def adapter(_target, _package_dir):
        obs = _agg_obs("/abs/elsewhere.json", "claim_validated", "validated", **_VALIDATED_DIMS)
        obs.dimensions["integrity"] = {"status": "trusted", "reason": None}
        return obs

    monkeypatch.setitem(control_crosswalk._VERIFIER_ADAPTERS, "evidence-package", adapter)
    readiness = _assess(target, "EC-12")
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_error"
    assert readiness["artifacts"][0]["relpath"] == ".brigade/evidence-packages/pkg-1/manifest.json"

    with pytest.raises(control_readiness.ReadinessContractError):
        control_readiness.validate_observation(object())


def test_ec10_period_uses_real_outcome_record_ts(tmp_path):
    from brigade import outcome, outcome_cmd

    target = _ws(tmp_path)
    record = outcome.OutcomeRecord(
        artifact_id="card-1",
        artifact_kind="card",
        task_id="task-1",
        source="test",
        signal_value=1,
        evidence_ref="verify:run-1",
        ts="2026-09-10T12:00:00Z",
    )
    path = target / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(outcome_cmd._record_payload(record)) + "\n", encoding="utf-8")

    inside = _assess(target, "EC-10", period=("2026-09-01T00:00:00+00:00", "2026-09-30T00:00:00+00:00"))
    assert inside["dimensions"]["freshness"]["status"] == "passed"
    assert inside["outcome"] == "structure_observed"
    outside = _assess(target, "EC-10", period=("2026-10-01T00:00:00+00:00", "2026-10-31T00:00:00+00:00"))
    assert outside["population"]["out_of_period"] == 1
    assert outside["outcome"] == "absent"


@pytest.mark.parametrize(
    ("status", "outcome", "reason", "state"),
    [
        ("failed", "failed", "status_failed", "evidenced_failed"),
        ("rejected", "failed", "status_rejected", "evidenced_failed"),
        ("canceled", "incomplete", "status_canceled", "untested"),
        ("running", "incomplete", "status_not_terminal", "untested"),
    ],
)
def test_verify_receipt_terminal_states_stay_distinct(tmp_path, status, outcome, reason, state):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1", status=status)
    readiness = _assess(target, "EC-01")
    assert (readiness["outcome"], readiness["reason"], readiness["legacy_state"]) == (outcome, reason, state)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda r: r.pop("planned_commands"), "planned_commands_missing"),
        (lambda r: r.update(planned_commands=[]), "planned_commands_missing"),
        (lambda r: r.update(planned_commands=["true", "pytest"]), "planned_commands_mismatch"),
        (lambda r: r.update(planned_commands=["pytest"]), "planned_commands_mismatch"),
        (lambda r: r["commands"][0].pop("command"), "planned_commands_mismatch"),
        (lambda r: r["commands"][0].update(status="interrupted"), "command_not_terminal"),
    ],
)
def test_ec01_needs_planned_checks_and_terminal_command_evidence(tmp_path, mutate, reason):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    receipt_path = _verify_dir(target, "v1") / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    mutate(receipt)
    receipt_path.write_text(json.dumps(receipt))
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "incomplete"
    assert readiness["reason"] == reason
    assert readiness["legacy_state"] == "untested"


def test_ec12_discovers_root_and_one_level_manifests_only(tmp_path):
    target = _ws(tmp_path)
    root = target / ".brigade" / "evidence-packages"
    _write_json(root / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))
    (root / "receipt.json").write_text("{}")
    _write_json(root / "pkg-1" / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))
    (root / "pkg-1" / "receipt.json").write_text("{}")
    _write_json(root / "group" / "pkg-2" / "manifest.json", _package_manifest([_RECEIPT_ENTRY]))

    readiness = _assess(target, "EC-12")
    assert [a["relpath"] for a in readiness["artifacts"]] == [
        ".brigade/evidence-packages/manifest.json",
        ".brigade/evidence-packages/pkg-1/manifest.json",
    ]
    assert readiness["outcome"] == "structure_observed"
    assert readiness["legacy_state"] == "untested"
    assert "verifier_not_wired" in readiness["reason_codes"]


@pytest.mark.parametrize(
    "selected_run,binding", [(RUN, "source.producer_run_id"), ("verify-other", "source.verify_run_id")]
)
def test_package_scope_accepts_each_recorded_run_namespace(tmp_path, selected_run, binding):
    target = _ws(tmp_path)
    pkg = target / ".brigade" / "evidence-packages" / "pkg-1"
    manifest = _package_manifest([_RECEIPT_ENTRY], verify_run_id="verify-other")
    manifest["source"]["producer_run_id"] = RUN
    _write_json(pkg / "manifest.json", manifest)
    (pkg / "receipt.json").write_text("{}")
    observed = _assess(target, "EC-12", selected_run)
    assert observed["population"]["in_scope"] == 1
    assert observed["population"]["wrong_run"] == 0
    assert observed["artifacts"][0]["run_binding"] == binding
    assert observed["outcome"] == "incomplete"
    assert observed["validation_level"] == "structure_observed"
    assert observed["dimensions"]["subject"]["status"] == "unknown"
    foreign = _assess(target, "EC-12", "foreign-producer")
    assert foreign["population"]["wrong_run"] == 1
    assert foreign["outcome"] == "absent"


def test_no_relationship_mapping_uses_declared_validation_level_enum(tmp_path, monkeypatch):
    from brigade import control_readiness

    crosswalk = load_crosswalk()
    row = crosswalk["mappings"][0]
    row["relationship"] = "no-relationship"
    monkeypatch.setattr(control_crosswalk, "load_crosswalk", lambda: crosswalk)
    evaluated = control_crosswalk.evaluate_controls(tmp_path)
    mapped = next(
        m
        for m in evaluated["mappings"]
        if m["claim_id"] == row["claim_id"]
        and m["framework_id"] == row["framework_id"]
        and m["control_id"] == row["control_id"]
    )
    assert mapped["validation_level"] in control_readiness.LEVELS
    assert mapped["validation_level"] == "none"
    assert mapped["state"] == mapped["evidence_outcome"] == "not_applicable"


# --- #1616/#1617 integration review fixes -----------------------------------


def test_ec01_forged_well_shaped_receipt_without_digest_never_validates(tmp_path):
    target = _ws(tmp_path)
    # Hand-written, well-shaped, terminal and subject-bound, with no stored digest.
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    receipt = json.loads((_verify_dir(target, "v1") / "receipt.json").read_text())
    assert "digests" not in receipt

    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "structure_observed"
    assert readiness["validation_level"] == "structure_observed"
    assert readiness["legacy_state"] == "untested"
    assert "integrity" in readiness["required_dimensions"]
    assert readiness["dimensions"]["integrity"] == {"status": "not_checked", "reason": "verifier_not_wired"}
    assert readiness["verifier"]["status"] == "not_wired"
    assert control_crosswalk._evaluate_claim_state(target, "EC-01", None) != "evidenced_passed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["commands"][0].update(command="pytest -k other"),
        lambda r: r.update(planned_commands=["b", "a"], commands=[{**r["commands"][0], "command": c} for c in "ab"]),
    ],
    ids=["different-check", "reordered"],
)
def test_ec01_command_must_match_planned_check_at_same_position(tmp_path, mutate):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "v1"), run_id="v1")
    receipt_path = _verify_dir(target, "v1") / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    mutate(receipt)
    receipt_path.write_text(json.dumps(receipt))
    readiness = _assess(target, "EC-01")
    assert readiness["outcome"] == "incomplete"
    assert readiness["reason"] == "planned_commands_mismatch"


def _ec08_repo(tmp_path: Path, monkeypatch) -> Path:
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    target = _ws(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    return target


def test_ec08_missing_local_receipt_is_incomplete_not_a_digest_failure(tmp_path, monkeypatch):
    from brigade import causal_receipt

    target = _ec08_repo(tmp_path, monkeypatch)
    run_json = {"run_id": "run-1", "status": "completed"}
    _write_json(target / ".brigade" / "runs" / "run-1" / "run.json", run_json)
    digest = causal_receipt.receipt_digest(run_json)
    _git_commit(target, f"matching\n\nBrigade-Run: run-1\nBrigade-Receipt: sha256:{digest}")
    _git_commit(target, "absent\n\nBrigade-Run: run-2\nBrigade-Receipt: sha256:" + "1" * 64)

    readiness = _assess(target, "EC-08")
    assert readiness["outcome"] == "incomplete"
    assert readiness["legacy_state"] == "untested"
    assert readiness["reason"] == "entry_missing"
    assert readiness["dimensions"]["integrity"] == {"status": "unknown", "reason": "entry_missing"}
    assert readiness["population"]["validated"] == 1
    assert readiness["population"]["rejected"] == 0
    assert "trailer_digest_mismatch" not in readiness["reason_codes"]
    assert readiness["population"]["window"]["with_trailer"] == 2

    # A real digest mismatch on a present receipt stays rejected.
    _write_json(target / ".brigade" / "runs" / "run-2" / "run.json", {"run_id": "run-2"})
    readiness = _assess(target, "EC-08")
    assert readiness["outcome"] == "rejected"
    assert readiness["reason"] == "trailer_digest_mismatch"


def test_ec08_symlinked_local_receipt_is_refused_separately(tmp_path, monkeypatch):
    target = _ec08_repo(tmp_path, monkeypatch)
    real = target / "elsewhere" / "run.json"
    _write_json(real, {"run_id": "run-1"})
    link = target / ".brigade" / "runs" / "run-1" / "run.json"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    _git_commit(target, "linked\n\nBrigade-Run: run-1\nBrigade-Receipt: sha256:" + "2" * 64)
    readiness = _assess(target, "EC-08")
    assert readiness["outcome"] == "invalid"
    assert readiness["reason"] == "symlink_refused"
    assert "entry_missing" not in readiness["reason_codes"]


def test_ec04_discovers_record_request_nonce_path(tmp_path, monkeypatch):
    from brigade import attestation

    target = _ws(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-1"
    # agent_request.record_request writes requests/<nonce>.json.
    nonce_path = run_dir / "requests" / ("ab" * 16 + ".json")
    _write_json(nonce_path, {"payloadType": "application/vnd.in-toto+json"})
    seen: list[str] = []

    def fake_verify(path, **kwargs):
        seen.append(Path(path).relative_to(kwargs["target"] / ".brigade/runs/run-1").as_posix())
        return attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, run_id="run-1")

    monkeypatch.setattr(attestation, "verify_attestation", fake_verify)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)
    readiness = _assess(target, "EC-04", "run-1")
    assert seen == ["requests/" + "ab" * 16 + ".json"]
    assert readiness["outcome"] == "structure_observed"
    assert readiness["validation_level"] == "structure_observed"
    assert readiness["legacy_state"] == "untested"
    assert readiness["population"]["in_scope"] == 1
    assert readiness["artifacts"][0]["relpath"] == ".brigade/runs/run-1/requests/" + "ab" * 16 + ".json"

    # The legacy root alias is still discovered alongside the producer path.
    _write_json(run_dir / "agent-request.json", {})
    readiness = _assess(target, "EC-04", "run-1")
    assert readiness["population"]["in_scope"] == 2

    # A signed request naming another run is not evidence for this one.
    monkeypatch.setattr(
        attestation,
        "verify_attestation",
        lambda path, **kwargs: attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, run_id="x"),
    )
    assert _assess(target, "EC-04", "run-1")["outcome"] == "rejected"


@pytest.mark.parametrize("run_id", [None, "linked-run"])
@pytest.mark.parametrize("period", [None, ("2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")])
def test_ec04_refuses_linked_run_with_only_producer_requests(tmp_path, monkeypatch, run_id, period):
    from brigade import attestation

    target = _ws(tmp_path)
    runs = target / ".brigade" / "runs"
    sibling = runs / "valid-run" / "requests" / ("ab" * 16 + ".json")
    _write_json(sibling, {})
    destination = tmp_path / "external-run"
    _write_json(destination / "requests" / ("cd" * 16 + ".json"), {})
    _write_json(destination / "run.json", {"started_at": "2025-01-01T00:00:00Z"})
    (runs / "linked-run").symlink_to(destination, target_is_directory=True)
    seen = []

    def verify(path, **kwargs):
        live_path = target / Path(path).relative_to(kwargs["target"])
        assert live_path == sibling
        seen.append(live_path)
        return attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, run_id="valid-run")

    monkeypatch.setattr(attestation, "verify_attestation", verify)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)
    readiness = _assess(target, "EC-04", run_id, period=period)
    assert readiness["outcome"] == "invalid"
    assert "symlink_refused" in readiness["reason_codes"]
    assert any(artifact["relpath"] == ".brigade/runs/linked-run" for artifact in readiness["artifacts"])
    assert seen == ([sibling] if run_id is None else [])


def _approval_journal(run_dir: Path, *, broken: bool) -> None:
    import base64

    from brigade import run_journal

    nonce = "cd" * 16
    _write_json(run_dir / "run.json", {"run_id": run_dir.name, "tree_fingerprint": "f" * 40})
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    run_journal.append_event(
        journal,
        run_id=run_dir.name,
        event_type="approval",
        payload={
            "decision": "allow",
            "nonce": nonce,
            "attestation_path": f"approvals/{nonce}.json",
            "approver_keyid": "SHA256:fake",
            "approver_principal": "reviewer@example.invalid",
            "expires_at": "2026-01-02T00:00:00Z",
            "scope": "run",
            "statement_sha256": "e" * 64,
            "subject_tree": "f" * 40,
        },
        idempotency_key="approval-1",
        expected_previous_sequence=0,
        recorded_at="2026-01-01T00:00:00.000000Z",
    )
    statement = base64.b64encode(json.dumps({"predicateType": "x"}).encode()).decode()
    _write_json(run_dir / "approvals" / f"{nonce}.json", {"payload": statement, "signatures": []})
    if broken:
        with journal.open("a", encoding="utf-8") as handle:
            handle.write("not json\n")


def test_ec05_known_journal_failure_survives_missing_ssh_keygen(tmp_path, monkeypatch):
    target = _ws(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-1"
    _approval_journal(run_dir, broken=True)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: False)

    readiness = _assess(target, "EC-05", "run-1")
    assert readiness["outcome"] == "rejected"
    assert readiness["legacy_state"] == "evidenced_failed"
    assert readiness["dimensions"]["integrity"] == {"status": "failed", "reason": "journal_chain_error"}
    assert readiness["verifier"]["status"] == "wired"
    assert readiness["artifacts"][0]["relpath"] == ".brigade/runs/run-1/events/lifecycle.jsonl"

    # An intact journal whose approval needs a signature check stays unavailable.
    _approval_journal(target / ".brigade" / "runs" / "run-2", broken=False)
    readiness = _assess(target, "EC-05", "run-2")
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_tool_unavailable"


@pytest.mark.parametrize(
    ("journal_case", "outcome", "reason"),
    [
        ("malformed_prefix", "rejected", "journal_chain_error"),
        ("partial_tail", "incomplete", "journal_partial_tail"),
        ("event_bound", "incomplete", "read_limit_exceeded"),
        ("byte_bound", "incomplete", "read_limit_exceeded"),
        ("clean", "absent", "no_artifacts"),
        ("empty", "absent", "no_artifacts"),
        ("missing", "absent", "no_artifacts"),
    ],
)
def test_ec05_unapproved_journal_cannot_hide_behind_valid_sibling(tmp_path, monkeypatch, journal_case, outcome, reason):
    from brigade import approval, run_checkpoint, run_journal

    from tests import test_approval as approval_fixtures

    # Use a real signed approval and real journal reads for both runs. The
    # existing status-mapping mock cannot expose parser short-circuiting.
    good_id = "approved-run"
    target, key, _ = approval_fixtures._workspace(tmp_path, run_id=good_id, requester_principal=None)
    approval_fixtures._record_v1_approval(target, key, run_id=good_id)
    good_dir = target / ".brigade/runs" / good_id
    good_journal = good_dir / "events/lifecycle.jsonl"
    good_report = run_journal.read_journal_bounded(good_journal)
    assert approval.verify_run_approval(target, good_dir).status == "APPROVED"
    assert _assess(target, "EC-05")["outcome"] == "validated"

    bad_dir = target / ".brigade/runs/unapproved-run"
    _write_json(bad_dir / "run.json", {"run_id": bad_dir.name})
    journal = bad_dir / "events/lifecycle.jsonl"
    journal.parent.mkdir()
    if journal_case == "malformed_prefix":
        _approval_journal(bad_dir, broken=False)
        journal.write_bytes(b"null\n" + journal.read_bytes())
    elif journal_case == "partial_tail":
        journal.write_bytes(b'{"partial":')
    elif journal_case == "empty":
        journal.write_bytes(b"")
    elif journal_case != "missing":
        count = 1 if journal_case == "clean" else 3
        for sequence in range(1, count + 1):
            run_journal.append_event(
                journal,
                run_id=bad_dir.name,
                event_type="run.created",
                payload={"status": "started"},
                idempotency_key=f"created-{sequence}",
                expected_previous_sequence=sequence - 1,
                recorded_at="2026-09-03T11:59:00.000000Z",
            )
        if journal_case in {"event_bound", "byte_bound"}:
            run_journal.append_event(
                journal,
                run_id=bad_dir.name,
                event_type="approval",
                payload=good_report.events[-1].payload,
                idempotency_key="hidden-approval",
                expected_previous_sequence=count,
                recorded_at="2026-09-03T12:01:00.000000Z",
            )
            if journal_case == "event_bound":
                monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_EVENTS", len(good_report.events))
            else:
                limit = good_journal.stat().st_size
                assert journal.stat().st_size > limit
                monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", limit)

    expected_status = "APPROVAL-INVALID" if journal_case in {"event_bound", "byte_bound"} else "UNAPPROVED"
    assert approval.verify_run_approval(target, bad_dir).status == expected_status
    if journal_case == "malformed_prefix":
        report = run_journal.read_journal_bounded(journal)
        assert report.events == []
        assert report.chain_errors

    scoped = _assess(target, "EC-05", bad_dir.name)
    assert scoped["outcome"] == outcome
    assert scoped["reason"] == reason
    mixed = _assess(target, "EC-05")
    assert mixed["population"]["validated"] == 1
    if outcome == "absent":
        assert mixed["outcome"] == "validated"
        assert mixed["population"]["discovered"] == 1
    else:
        assert mixed["outcome"] == outcome
        assert mixed["legacy_state"] == ("evidenced_failed" if outcome == "rejected" else "untested")
        assert mixed["population"][outcome] == 1
        assert mixed["population"]["discovered"] == 2
        assert reason in mixed["reason_codes"]


@pytest.mark.parametrize("run_record", [None, "{broken", "[]"])
def test_ec05_invalid_run_record_survives_missing_ssh_keygen(tmp_path, monkeypatch, run_record):
    from brigade import approval

    target = _ws(tmp_path)
    run_dir = target / ".brigade" / "runs" / "invalid-record"
    _approval_journal(run_dir, broken=False)
    run_json = run_dir / "run.json"
    if run_record is None:
        run_json.unlink(missing_ok=True)
    else:
        run_json.write_text(run_record)
    assert approval.verify_run_approval(target, run_dir).status == "APPROVAL-INVALID"
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: False)

    readiness = _assess(target, "EC-05", "invalid-record")
    assert readiness["outcome"] == "rejected"
    assert readiness["legacy_state"] == "evidenced_failed"
    assert readiness["dimensions"]["integrity"] == {"status": "failed", "reason": "approval_invalid"}


@pytest.mark.parametrize("git_error", ["dubious", "corrupt", "timeout", "oserror"])
def test_ec08_git_probe_failure_is_unavailable_not_not_applicable(tmp_path, monkeypatch, git_error):
    import subprocess

    target = _ws(tmp_path)
    monkeypatch.setattr(control_crosswalk.shutil, "which", lambda name: "synthetic-git")

    def failed_probe(ctx, *args, **kwargs):
        if git_error == "timeout":
            raise subprocess.TimeoutExpired("git", 10)
        if git_error == "oserror":
            raise OSError("synthetic unavailable tool")
        detail = "fatal: detected dubious ownership" if git_error == "dubious" else "fatal: corrupted repository"
        return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr=detail)

    monkeypatch.setattr(control_crosswalk, "_git", failed_probe)
    readiness = _assess(target, "EC-08")
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_error"
    assert readiness["legacy_state"] == "untested"


@pytest.mark.parametrize("claim_id", ["EC-04", "EC-05", "EC-06"])
@pytest.mark.parametrize("run_id", [None, RUN])
def test_run_discovery_root_file_is_unavailable(tmp_path, claim_id, run_id):
    target = _ws(tmp_path)
    root = target / ".brigade" / "runs"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory")
    readiness = _assess(target, claim_id, run_id)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "discovery_unreadable"


@pytest.mark.parametrize("claim_id", ["EC-04", "EC-05", "EC-06"])
@pytest.mark.parametrize("run_id", [None, RUN])
def test_run_discovery_root_stat_error_is_unavailable(tmp_path, monkeypatch, claim_id, run_id):
    target = _ws(tmp_path)
    root = target / ".brigade" / "runs"
    root.mkdir(parents=True)
    _deny_artifact_metadata(monkeypatch, root)
    readiness = _assess(target, claim_id, run_id)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "discovery_unreadable"


@pytest.mark.parametrize("status", [[], {}])
def test_unhashable_receipt_status_is_invalid(tmp_path, status):
    target = _ws(tmp_path)
    _write_json(
        target / ".brigade/work/verify-runs/verify-a/receipt.json",
        {"schema_version": 2, "run_id": "verify-a", "status": status},
    )
    result = _assess(target, "EC-01")
    assert result["outcome"] == "invalid"
    assert result["reason"] == "schema_missing"


@pytest.mark.parametrize(
    "receipt,expected,reason",
    [
        ({"run_id": "run-b", "status": "completed"}, "rejected", "run_binding_mismatch"),
        ({}, "invalid", "schema_missing"),
    ],
)
@pytest.mark.parametrize("run_id", [None, "run-a"])
def test_trailer_requires_receipt_identity(tmp_path, receipt, expected, reason, run_id):
    import subprocess

    from brigade import causal_receipt

    target = _ws(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    _write_json(target / ".brigade/runs/run-a/run.json", receipt)
    message = "fixture\n\nBrigade-Run: run-a\nBrigade-Receipt: sha256:" + causal_receipt.receipt_digest(receipt)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            message,
        ],
        cwd=target,
        check=True,
    )
    result = _assess(target, "EC-08", run_id)
    assert result["outcome"] == expected
    assert result["reason"] == reason
    assert result["dimensions"]["subject"]["status"] != "passed"


@pytest.mark.parametrize(
    "claim,relative",
    [
        ("EC-01", ".brigade/work/verify-runs"),
        ("EC-02", ".brigade/work/verify-runs"),
        ("EC-03", ".brigade/work/verify-runs"),
        ("EC-12", ".brigade/evidence-packages"),
        ("EC-04", ".brigade/runs/run-a/requests"),
    ],
)
def test_shared_discovery_stat_refusal_is_unavailable(tmp_path, monkeypatch, claim, relative):
    target = _ws(tmp_path)
    root = target / relative
    root.mkdir(parents=True)
    _deny_artifact_metadata(monkeypatch, root)
    result = _assess(target, claim)
    assert result["outcome"] == "unavailable"
    assert result["reason"] == "discovery_unreadable"


@pytest.mark.parametrize("ssh_available", [True, False])
@pytest.mark.parametrize("bound", ["byte", "event"])
def test_approval_read_bound_is_incomplete(tmp_path, monkeypatch, ssh_available, bound):
    from brigade import run_checkpoint

    target = _ws(tmp_path)
    journal = target / ".brigade/runs/run-a/events/lifecycle.jsonl"
    journal.parent.mkdir(parents=True)
    if bound == "byte":
        with journal.open("wb") as stream:
            stream.truncate(run_checkpoint.MAX_JOURNAL_BYTES + 1)
    else:
        _approval_journal(journal.parent.parent, broken=False)
        monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_EVENTS", 0)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: ssh_available)
    result = _assess(target, "EC-05")
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "read_limit_exceeded"
    assert result["dimensions"]["integrity"]["status"] != "failed"


@pytest.mark.parametrize("ssh_available", [True, False])
def test_approval_journal_read_error_is_unavailable(tmp_path, monkeypatch, ssh_available):
    from brigade import run_journal

    target = _ws(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-a"
    _approval_journal(run_dir, broken=False)
    original = run_journal._open_nofollow
    journal = run_dir / "events" / "lifecycle.jsonl"

    def refused(path, *args, **kwargs):
        if Path(path).parts[-4:] == journal.parts[-4:]:
            raise PermissionError("synthetic journal read refusal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(run_journal, "_open_nofollow", refused)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: ssh_available)
    readiness = _assess(target, "EC-05")
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "verifier_error"
    assert readiness["dimensions"]["integrity"]["status"] != "failed"


@pytest.mark.parametrize("run_id", [None, "run-a"])
def test_valid_legacy_request_does_not_hide_unreadable_request_population(tmp_path, monkeypatch, run_id):
    from brigade import attestation

    target = _ws(tmp_path)
    run_dir = target / ".brigade" / "runs" / "run-a"
    _write_json(run_dir / "request.json", {})
    root = run_dir / "requests"
    root.mkdir()
    _deny_artifact_metadata(monkeypatch, root)
    monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)
    _stub_attestation(
        monkeypatch,
        attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, run_id="run-a"),
    )
    readiness = _assess(target, "EC-04", run_id)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "discovery_unreadable"
    assert readiness["legacy_state"] == "untested"
    assert any(artifact["outcome"] == "structure_observed" for artifact in readiness["artifacts"])


@pytest.mark.parametrize("claim_id", ["EC-02", "EC-12"])
@pytest.mark.parametrize("run_id", [None, RUN])
def test_unreadable_artifact_sibling_cannot_be_omitted(tmp_path, monkeypatch, claim_id, run_id):
    from brigade import attestation, control_readiness

    target = _ws(tmp_path)
    if claim_id == "EC-02":
        valid = _verify_dir(target, "a-valid")
        _write_verify_receipt(valid, run_id="a-valid", producer_run_id=RUN)
        receipt = json.loads((valid / "receipt.json").read_text())
        receipt.update(tree_fingerprint="1" * 40, baseline_commit="2" * 40, changes_patch_sha256="3" * 64)
        receipt["digests"] = {
            "algorithm": "sha256",
            "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
        }
        _write_json(valid / "receipt.json", receipt)
        _write_json(valid / "attestation.json", {})
        denied = _verify_dir(target, "b-unreadable") / "attestation.json"
        _write_json(denied, {})
        monkeypatch.setattr(control_crosswalk, "_ssh_keygen_available", lambda: True)
        _stub_attestation(
            monkeypatch,
            attestation.AttestationVerifyResult(status=attestation.STATUS_SIGNED_OK, rederived=True, run_id="a-valid"),
        )
    else:
        root = target / ".brigade" / "evidence-packages"
        manifest = _package_manifest([])
        manifest["source"] = {"producer_run_id": RUN}
        _write_json(root / "a-valid" / "manifest.json", manifest)
        denied = root / "b-unreadable" / "manifest.json"
        _write_json(denied, manifest)

        def adapter(_target, package_dir):
            return control_readiness.ArtifactObservation(
                relpath="manifest.json",
                level="claim_validated",
                proposed="validated",
                dimensions=control_readiness.dims(integrity="passed", subject="passed", population="not_applicable"),
            )

        monkeypatch.setitem(control_crosswalk._VERIFIER_ADAPTERS, "evidence-package", adapter)
    _deny_artifact_metadata(monkeypatch, denied)
    readiness = _assess(target, claim_id, run_id)
    assert readiness["outcome"] == "unavailable"
    assert readiness["reason"] == "discovery_unreadable"
    assert readiness["legacy_state"] == "untested"
    assert readiness["population"]["validated"] == 1


def _deny_artifact_metadata(monkeypatch, denied: Path) -> None:
    """Deny metadata for the same object at the held-parent stat boundary."""
    import os

    from brigade import dirfd

    parent = denied.parent.stat()
    original = dirfd.stat_child

    def refused(descriptor, name):
        held = os.fstat(descriptor)
        if (held.st_dev, held.st_ino) == (parent.st_dev, parent.st_ino) and name == denied.name:
            raise PermissionError("synthetic artifact metadata refusal")
        return original(descriptor, name)

    monkeypatch.setattr(dirfd, "stat_child", refused)


@pytest.mark.parametrize("run_id", [None, "b-unreadable"])
@pytest.mark.parametrize("denied_location", ["parent", "leaf"])
def test_journal_metadata_refusal_does_not_abort_or_hide_population(tmp_path, monkeypatch, run_id, denied_location):
    target = _ws(tmp_path)
    runs = target / ".brigade" / "runs"
    _append_events(runs / "a-valid" / "events" / "lifecycle.jsonl", "a-valid")
    refused = runs / "b-unreadable" / "events" / "lifecycle.jsonl"
    _append_events(refused, "b-unreadable")
    _deny_artifact_metadata(monkeypatch, refused.parent if denied_location == "parent" else refused)
    result = _assess(target, "EC-06", run_id)
    assert result["outcome"] == "unavailable"
    assert result["reason"] == "discovery_unreadable"
    assert result["legacy_state"] == "untested"
    assert result["population"]["validated"] == (1 if run_id is None else 0)
    assert any(
        artifact["outcome"] == "unavailable" and artifact["scope"] == "in_scope" for artifact in result["artifacts"]
    )


@pytest.mark.parametrize("run_id", [None, RUN])
@pytest.mark.parametrize(
    "claim_id,relative,payload",
    [
        ("EC-07", ".brigade/governance/inventory.json", {}),
        ("EC-09", ".brigade/work/guard/audit.json", {"summary": {"blocked": False}}),
        ("EC-10", "memory/outcome/records.jsonl", {"schema_version": 1}),
        ("EC-11", ".brigade/work/verify-archive/index.jsonl", {"run_id": "archived-run"}),
    ],
)
def test_workspace_artifact_metadata_refusal_is_unavailable(tmp_path, monkeypatch, run_id, claim_id, relative, payload):
    target = _ws(tmp_path)
    artifact = target / relative
    _write_json(artifact, payload)
    _deny_artifact_metadata(monkeypatch, artifact)
    result = _assess(target, claim_id, run_id)
    assert result["outcome"] == "unavailable"
    assert result["reason"] == "discovery_unreadable"
    assert result["legacy_state"] == "untested"
    assert result["population"]["validated"] == 0
    assert any(item["outcome"] == "unavailable" and item["scope"] == "in_scope" for item in result["artifacts"])


@pytest.mark.parametrize("run_id", [None, "run-a"])
@pytest.mark.parametrize("denied_location", ["parent", "leaf"])
def test_trailer_receipt_metadata_refusal_is_unavailable(tmp_path, monkeypatch, run_id, denied_location):
    import subprocess

    from brigade import causal_receipt

    target = _ws(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    receipt = {"run_id": "run-a", "status": "completed"}
    path = target / ".brigade" / "runs" / "run-a" / "run.json"
    _write_json(path, receipt)
    _git_commit(
        target, "fixture\n\nBrigade-Run: run-a\nBrigade-Receipt: sha256:" + causal_receipt.receipt_digest(receipt)
    )
    _deny_artifact_metadata(monkeypatch, path.parent if denied_location == "parent" else path)
    result = _assess(target, "EC-08", run_id)
    assert result["outcome"] == "unavailable"
    assert result["reason"] == "discovery_unreadable"
    assert result["legacy_state"] == "untested"
    assert result["population"]["validated"] == 0


@pytest.mark.parametrize("run_id", [None, "run-a"])
@pytest.mark.parametrize("linked_target", ["missing", "empty"])
def test_trailer_symlinked_run_without_receipt_is_invalid(tmp_path, run_id, linked_target):
    import subprocess

    from brigade import causal_receipt

    target = _ws(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    receipt = {"run_id": "run-a", "status": "completed"}
    run_dir = target / ".brigade" / "runs" / "run-a"
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    destination = tmp_path / "linked-run"
    if linked_target == "empty":
        destination.mkdir()
    run_dir.symlink_to(destination, target_is_directory=True)
    _git_commit(
        target, "fixture\n\nBrigade-Run: run-a\nBrigade-Receipt: sha256:" + causal_receipt.receipt_digest(receipt)
    )
    result = _assess(target, "EC-08", run_id)
    assert result["outcome"] == "invalid"
    assert result["reason"] == "symlink_refused"
    assert result["population"]["validated"] == 0


def test_ec04_signed_post_dispatch_request_cannot_validate_ordering(tmp_path):
    import shutil

    from brigade import agent_request, attestation, run_journal

    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is required for signed requests")
    target = _ws(tmp_path)
    key, _ = attestation.keygen(target, principal="requester")
    run_dir = target / ".brigade/runs/run-a"
    _write_json(run_dir / "run.json", {"run_id": "run-a", "started_at": "2026-01-01T00:00:00Z"})
    journal = run_dir / "events/lifecycle.jsonl"
    journal.parent.mkdir()
    run_journal.append_event(
        journal,
        run_id="run-a",
        event_type="run.dispatch.requested",
        payload={"seat": "worker", "attempt": 1},
        idempotency_key="dispatch:worker:1",
        expected_previous_sequence=0,
        recorded_at="2026-01-01T00:00:01.000000Z",
    )
    nonce = "ab" * 16
    statement = agent_request.build_statement(
        run_id="run-a",
        baseline_commit="a" * 40,
        task="fixture task",
        requested_at="2026-01-01T00:00:02.000000Z",
        nonce=nonce,
    )
    envelope = attestation.create_envelope(statement, key)
    _write_json(run_dir / f"requests/{nonce}.json", envelope)
    run_journal.append_event(
        journal,
        run_id="run-a",
        event_type="request.signed",
        payload=agent_request.event_payload(
            requester_principal="requester",
            requester_keyid=envelope["signatures"][0]["keyid"],
            baseline_commit="a" * 40,
            task="fixture task",
            nonce=nonce,
            statement_sha256=localio.canonical_json_digest(statement),
            attestation_path=f"requests/{nonce}.json",
        ),
        idempotency_key=f"request:{nonce}",
        expected_previous_sequence=1,
        recorded_at="2026-01-01T00:00:02.000000Z",
    )
    result = _assess(target, "EC-04", "run-a")
    assert result["dimensions"]["signature"]["status"] == "passed"
    assert result["dimensions"]["subject"]["status"] == "passed"
    assert result["outcome"] == "structure_observed"
    assert result["validation_level"] == "structure_observed"
    assert result["legacy_state"] == "untested"
    assert "verifier_not_wired" in result["reason_codes"]


@pytest.mark.parametrize("status", ["failed", "rejected"])
def test_failed_wrong_directory_receipt_is_rejected(tmp_path, status):
    target = _ws(tmp_path)
    _write_verify_receipt(_verify_dir(target, "verify-a"), run_id="verify-b", status=status)
    result = _assess(target, "EC-01")
    assert result["outcome"] == "rejected"
    assert result["dimensions"]["subject"] == {"status": "failed", "reason": "run_binding_mismatch"}
    assert "run_binding_mismatch" in result["reason_codes"]
    assert result["population"]["failed"] == 0
    assert result["population"]["rejected"] == 1


@pytest.mark.parametrize(
    "dimension,reason",
    [
        ("integrity", "trailer_digest_mismatch"),
        ("signature", "signature_mismatch"),
        ("subject", "run_binding_mismatch"),
        ("population", "no_records"),
        ("authorization", "untrusted_key"),
    ],
)
def test_failed_operation_cannot_mask_required_trust_failure(dimension, reason):
    from brigade import control_readiness

    obs = _agg_obs("receipt.json", "claim_validated", "failed", **{dimension: ("failed", reason)})
    assert control_readiness.finalize_artifact(obs, frozenset({dimension})).outcome == "rejected"


@pytest.mark.parametrize("run_id", [None, "run-a"])
@pytest.mark.parametrize("claim", ["EC-01", "EC-04", "EC-05", "EC-06", "EC-08", "EC-12"])
def test_brigade_symlink_ancestor_refused_before_discovery_or_read(tmp_path, monkeypatch, run_id, claim):
    import os
    import subprocess

    from brigade import run_journal

    target = _ws(tmp_path)
    external = tmp_path / "external-evidence"
    external.mkdir()
    _approval_journal(external / "runs/run-a", broken=False)
    _write_verify_receipt(external / "work/verify-runs/verify-a", run_id="verify-a")
    try:
        (target / ".brigade").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    reads: list[Path] = []
    scans: list[Path] = []
    original_scandir = os.scandir

    def observe_scan(path):
        scans.append(Path(path))
        return original_scandir(path)

    def forbidden_read(path, *args, **kwargs):
        reads.append(Path(path))
        raise AssertionError("linked evidence must not be read")

    monkeypatch.setattr(os, "scandir", observe_scan)
    monkeypatch.setattr(run_journal, "read_journal_bounded", forbidden_read)
    monkeypatch.setattr(control_crosswalk, "_read_json_object", forbidden_read)
    if claim == "EC-08":
        monkeypatch.setattr(control_crosswalk.shutil, "which", lambda name: "fixture-git")
        monkeypatch.setattr(control_crosswalk, "_is_git_repo", lambda _target: True)

        def history(ctx, *args):
            stdout = (
                "a" * 40 + "\n"
                if args[-1] == "--format=%H"
                else ("fixture\nBrigade-Run: run-a\nBrigade-Receipt: sha256:" + "b" * 64 + "\x002026-01-01T00:00:00Z\n")
            )
            return subprocess.CompletedProcess(args, 0, stdout, "")

        monkeypatch.setattr(control_crosswalk, "_git", history)
    result = _assess(target, claim, run_id)
    assert result["outcome"] == "invalid"
    assert result["reason"] == "symlink_refused"
    assert result["dimensions"]["population"]["status"] == "unknown"
    assert result["population"]["validated"] == 0
    assert reads == []
    assert scans == []


@pytest.mark.parametrize("claim", ["EC-05", "EC-06"])
def test_lifecycle_symlink_ancestor_is_refused_before_verifier(tmp_path, monkeypatch, claim):
    from brigade import approval, run_journal

    target = _ws(tmp_path)
    run_dir = target / ".brigade/runs/run-a"
    _write_json(run_dir / "run.json", {"run_id": "run-a"})
    external = tmp_path / "external-events"
    external.mkdir()
    (external / "lifecycle.jsonl").write_text("fixture\n")
    (run_dir / "events").symlink_to(external, target_is_directory=True)
    calls: list[object] = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("linked journal must not reach verifier")

    monkeypatch.setattr(approval, "verify_run_approval", forbidden)
    monkeypatch.setattr(run_journal, "read_journal_bounded", forbidden)
    result = _assess(target, claim, "run-a")
    assert result["outcome"] == "invalid"
    assert result["reason"] == "symlink_refused"
    assert calls == []


@pytest.mark.parametrize("run_id", [None, "run-a"])
def test_ec08_oversized_commit_message_is_read_limit_exceeded(tmp_path, run_id):
    import shutil
    import subprocess

    from brigade import proc

    if shutil.which("git") is None:
        pytest.skip("git is required for commit history")
    target = _ws(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    message_file = tmp_path / "large-message.txt"
    message_file.write_bytes(b"x" * (proc.MAX_CAPTURE_BYTES + 1))
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-q",
            "-F",
            str(message_file),
        ],
        cwd=target,
        check=True,
    )
    result = _assess(target, "EC-08", run_id)
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "read_limit_exceeded"
    assert result["population"]["window"]["unreadable"] == 1
    assert result["population"]["window"]["without_trailer"] == 0
    assert result["dimensions"]["population"]["status"] == "unknown"
    assert result["artifacts"][0]["scope"] == "in_scope"


def test_ec08_oversized_git_stderr_is_read_limit_exceeded(tmp_path, monkeypatch):
    import os
    import sys

    from brigade import proc

    if os.name != "posix":
        pytest.skip("executable script fixture requires POSIX")
    target = _ws(tmp_path)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    git = tools_dir / "git"
    git.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.buffer.write(b'x' * {proc.MAX_CAPTURE_BYTES + 1})\n")
    git.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools_dir))
    result = _assess(target, "EC-08", "run-a")
    assert result["outcome"] == "incomplete"
    assert result["reason"] == "read_limit_exceeded"
    assert result["dimensions"]["population"]["status"] == "unknown"
    assert result["artifacts"][0]["scope"] == "in_scope"


@pytest.mark.parametrize("claim", ["EC-01", "EC-04", "EC-06", "EC-12"])
def test_ancestor_metadata_error_preserves_unavailable_population(tmp_path, monkeypatch, claim):
    target = _ws(tmp_path)
    ancestor = target / ".brigade"
    ancestor.mkdir()
    _deny_artifact_metadata(monkeypatch, ancestor)
    result = _assess(target, claim, "run-a")
    assert result["outcome"] == "unavailable"
    assert result["reason"] == "discovery_unreadable"
    assert result["dimensions"]["population"]["status"] == "unavailable"
    assert result["population"]["validated"] == 0


def test_assessed_target_symlink_remains_canonical(tmp_path):
    target = _ws(tmp_path)
    _approval_journal(target / ".brigade/runs/run-a", broken=False)
    alias = tmp_path / "target-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = _assess(alias, "EC-06", "run-a")
    assert result["outcome"] == "validated"
    assert result["artifacts"][0]["relpath"] == ".brigade/runs/run-a/events/lifecycle.jsonl"


@pytest.mark.skipif(__import__("os").name != "posix", reason="deterministic POSIX syscall interleaving")
@pytest.mark.parametrize("operation", ["read", "scan", "read-aba", "scan-aba"])
def test_ancestor_swap_cannot_redirect_assessment(tmp_path, monkeypatch, operation):
    import os

    target = _ws(tmp_path)
    run = _verify_dir(target, "original-run")
    _write_verify_receipt(run, run_id=run.name, status="failed")
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_verify_receipt(outside / "work/verify-runs/forged-run", run_id="forged-run")
    ancestor = target / ".brigade"
    held = target / "original-brigade"
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            ancestor.rename(held)
            ancestor.symlink_to(outside, target_is_directory=True)
            swapped = True

    original_open = os.open
    original_scan = os.scandir

    def restore():
        if operation.endswith("-aba") and ancestor.is_symlink():
            ancestor.unlink()
            held.rename(ancestor)

    def intercepted_open(path, flags, *args, **kwargs):
        if operation.startswith("read") and Path(path).name == "receipt.json":
            swap()
        descriptor = original_open(path, flags, *args, **kwargs)
        restore()
        return descriptor

    def intercepted_scan(path):
        if operation.startswith("scan"):
            swap()
        iterator = original_scan(path)
        restore()
        return iterator

    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {intercepted_open})
    monkeypatch.setattr(os, "open", intercepted_open)
    monkeypatch.setattr(os, "scandir", intercepted_scan)
    result = _assess(target, "EC-01")
    assert swapped
    assert result["outcome"] == "failed"
    assert all("forged-run" not in artifact["relpath"] for artifact in result["artifacts"])


@pytest.mark.skipif(__import__("os").name != "posix", reason="deterministic POSIX ancestor swap")
@pytest.mark.parametrize("claim", ["EC-02", "EC-05"])
def test_signed_delegation_uses_held_dependency_snapshot(tmp_path, monkeypatch, claim):
    import shutil

    from brigade import approval, attestation
    from tests import test_approval as fixtures

    target, key, _ = fixtures._workspace(tmp_path, requester_principal=None)
    if claim == "EC-02":
        fixtures._write_verify_receipt(target, producer_key=key)
        delegate = attestation.verify_attestation
    else:
        fixtures._record_v1_approval(target, key)
        delegate = approval.verify_run_approval
    assert _assess(target, claim)["outcome"] == "validated"
    ancestor = target / ".brigade"
    outside = tmp_path / "outside"
    shutil.copytree(ancestor, outside)
    (outside / "attestation/allowed_signers").write_text("")
    moved = target / "original-brigade"
    swapped = False

    def intercepted(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            ancestor.rename(moved)
            ancestor.symlink_to(outside, target_is_directory=True)
            swapped = True
        return delegate(*args, **kwargs)

    monkeypatch.setattr(
        attestation if claim == "EC-02" else approval,
        "verify_attestation" if claim == "EC-02" else "verify_run_approval",
        intercepted,
    )
    result = _assess(target, claim)
    assert swapped
    assert result["outcome"] == "validated"


@pytest.mark.parametrize("dependency", ["truncated", "budget", "linked", "malformed"])
def test_approval_dependency_refusals_cannot_validate_a_signed_claim(tmp_path, monkeypatch, dependency):
    from tests import test_approval as fixtures

    target, key, _ = fixtures._workspace(tmp_path, requester_principal=None)
    fixtures._record_v1_approval(target, key)
    assert _assess(target, "EC-05")["outcome"] == "validated"
    if dependency == "budget":
        monkeypatch.setattr(control_crosswalk, "_ASSESSMENT_BYTE_BUDGET", 1)
    elif dependency == "truncated":
        _verify_dir(target, "extra-run").mkdir()
        monkeypatch.setattr(control_crosswalk, "_MAX", 1)
    elif dependency == "linked":
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        (_verify_dir(target, fixtures.VERIFY_ID) / "attestation.json").unlink(missing_ok=True)
        (_verify_dir(target, fixtures.VERIFY_ID) / "attestation.json").symlink_to(outside)
    else:
        _write_json(_verify_dir(target, "unparseable-run") / "receipt.json", [])
    result = _assess(target, "EC-05")
    assert result["outcome"] != "validated"
    expected = {
        "truncated": "read_limit_exceeded",
        "budget": "read_limit_exceeded",
        "linked": "symlink_refused",
        "malformed": "invalid_json",
    }[dependency]
    assert expected in result["reason_codes"]


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX FIFO safety")
def test_assessment_refuses_nonregular_trust_without_reading_private_keys(tmp_path, monkeypatch):
    import os

    from brigade import dirfd
    from tests import test_approval as fixtures

    target, key, _ = fixtures._workspace(tmp_path, requester_principal=None)
    fixtures._record_v1_approval(target, key)
    original_open = dirfd.open_child_file
    opened = []

    def observe(parent, name, flags, mode=0o600):
        opened.append(name)
        assert name != "signing-key"
        return original_open(parent, name, flags, mode)

    monkeypatch.setattr(dirfd, "open_child_file", observe)
    assert _assess(target, "EC-05")["outcome"] == "validated"
    assert "signing-key.pub" in opened
    signers = target / ".brigade/attestation/allowed_signers"
    signers.unlink()
    os.mkfifo(signers)
    assert _assess(target, "EC-05")["outcome"] == "unavailable"


def test_approval_assessment_never_stages_an_unignored_private_signing_key(tmp_path):
    import subprocess

    from tests import test_approval as fixtures

    target, key, _ = fixtures._workspace(tmp_path, requester_principal=None)
    fixtures._record_v1_approval(target, key)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    _git_commit(target, "fixture")
    # A failing clean filter detects Git attempting to read/stage this path.
    # Keep it unignored so a blanket add -A actually exercises the regression.
    attributes = target / ".gitattributes"
    attributes.write_text(".brigade/attestation/signing-key filter=private-key-refusal\n")
    marker = target / "private-key-read-attempt"
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "filter.private-key-refusal.clean",
            f'echo attempted > "{marker}"; exit 1',
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(target), "config", "filter.private-key-refusal.required", "true"], check=True)
    _assess(target, "EC-05")
    assert not marker.exists()


def test_approval_snapshot_preserves_original_git_tree_and_workspace_key_identity(tmp_path, monkeypatch):
    import subprocess

    from brigade import approval, attestation
    from tests import test_approval as fixtures

    target, key, _ = fixtures._workspace(tmp_path, requester_principal=None)
    fixtures._record_v1_approval(target, key)
    (target / ".gitignore").write_text(".brigade/\n")
    (target / "tracked.txt").write_text("original tree\n")
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", ".gitignore", "tracked.txt"], check=True)
    _git_commit(target, "fixture")
    (target / "tracked.txt").write_text("changed live tree\n")
    expected_tree = localio.tree_fingerprint(target)
    expected_key = attestation.get_key_fingerprint(attestation.default_key_path(target))
    delegate = approval.verify_run_approval
    seen = []

    def verify(snapshot_target, run_dir, **kwargs):
        context = kwargs["context"]
        assert snapshot_target != target
        assert context.target == target
        assert context.live_tree == expected_tree
        assert context.workspace_keyid == expected_key
        assert not (snapshot_target / ".brigade/attestation/signing-key").exists()
        seen.append(context)
        return delegate(snapshot_target, run_dir, **kwargs)

    monkeypatch.setattr(approval, "verify_run_approval", verify)
    result = _assess(target, "EC-05")
    assert seen
    assert result["outcome"] == "rejected"
    assert "approval_stale" in result["reason_codes"]
