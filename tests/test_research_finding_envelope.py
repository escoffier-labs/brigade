"""Research finding envelopes, text projection, and backfill (#584)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli, provenance
from brigade.research import registry
from brigade.research.provenance import (
    backfill_research_provenance,
    map_legacy_trust_origin_modality,
    stamp_finding,
    stamp_finding_payload,
)
from brigade.research.types import Finding, finding_text


def _finding(
    *, trust: str = "web", title: str = "Title", summary: str = "Summary", evidence: str = "Evidence"
) -> Finding:
    return Finding(
        source_ids=("src-1111111111111111",),
        title=title,
        summary=summary,
        evidence=evidence,
        trust=trust,  # type: ignore[arg-type]
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )


def test_finding_text_is_exact_title_summary_evidence_projection() -> None:
    assert finding_text("T", "S", "E") == "T\nS\nE"
    finding = _finding(title="T", summary="S", evidence="E")
    assert finding.text == "T\nS\nE"


def test_legacy_trust_maps_only_to_origin_and_modality() -> None:
    assert map_legacy_trust_origin_modality("local") == ("workspace", "tool-output")
    assert map_legacy_trust_origin_modality("web") == ("external-web", "external-web")
    assert map_legacy_trust_origin_modality("cli") == ("external-service", "tool-output")
    assert map_legacy_trust_origin_modality("browser") == ("external-web", "external-web")
    assert map_legacy_trust_origin_modality("browser-ai") == ("external-web", "model-generated")


def test_stamp_finding_ignores_legacy_trust_for_envelope_label() -> None:
    finding = stamp_finding(_finding(trust="local"), run_id="run-1", index=0)
    env = finding.provenance
    assert isinstance(env, dict)
    assert provenance.validate_envelope(env) == []
    assert finding.trust == "local"
    assert env["origin"] == "workspace"
    assert env["modality"] == "tool-output"
    assert env["trust"]["label"] != "local"
    assert env["trust"]["label"] in {"untrusted", "quarantined"}
    assert env["trust"]["label"] not in {"reviewed", "verified"}
    assert env["hashes"]["content"] == provenance.content_sha256(finding.text)
    assert env["source"]["system"] == "research"


def test_inbound_trust_claim_cannot_assign_envelope_trust() -> None:
    payload = stamp_finding_payload(
        {
            "source_ids": ["src-1111111111111111"],
            "title": "T",
            "summary": "S",
            "evidence": "E",
            "trust": "web",
            "extraction_lane": "luna",
            "extracted_at": "2026-08-13T12:00:00+00:00",
            "provenance": {"trust": {"label": "verified"}},
        },
        run_id="run-1",
        index=0,
    )
    env = payload["provenance"]
    assert provenance.validate_envelope(env) == []
    assert env["trust"]["label"] != "verified"
    assert env["trust"]["label"] != "web"
    assert payload["trust"] == "web"


def test_write_findings_and_checkpoint_persist_envelope(tmp_path: Path) -> None:
    run_id = "20260817-finding-env"
    registry.create_run(tmp_path, question="q", run_id=run_id, caps={})
    finding = _finding(trust="cli")
    registry.write_findings(tmp_path, run_id, [finding])
    stored = json.loads((registry.run_dir(tmp_path, run_id) / registry.FINDINGS_ARTIFACT).read_text())
    row = stored["findings"][0]
    assert row["text"] == "Title\nSummary\nEvidence"
    assert row["trust"] == "cli"
    assert provenance.validate_envelope(row["provenance"]) == []
    assert row["provenance"]["origin"] == "external-service"
    assert row["provenance"]["modality"] == "tool-output"

    registry.save_checkpoint(tmp_path, run_id, {"findings": [row], "round": 1})
    checkpoint = registry.load_checkpoint(tmp_path, run_id)
    assert checkpoint is not None
    assert checkpoint["findings"][0]["text"] == row["text"]
    assert provenance.validate_envelope(checkpoint["findings"][0]["provenance"]) == []


def test_research_backfill_is_idempotent_and_untrusted(tmp_path: Path) -> None:
    run_id = "20260817-backfill"
    registry.create_run(tmp_path, question="q", run_id=run_id, caps={})
    legacy = {
        "source_ids": ["src-1111111111111111"],
        "title": "Legacy",
        "summary": "Old",
        "evidence": "Bytes",
        "trust": "web",
        "extraction_lane": "legacy",
        "extracted_at": "1970-01-01T00:00:00+00:00",
    }
    registry.save_checkpoint(tmp_path, run_id, {"findings": [legacy], "round": 1})
    # Overwrite with a true legacy row (save_checkpoint stamps new writes).
    path = registry.run_dir(tmp_path, run_id) / "checkpoint.json"
    path.write_text(json.dumps({"findings": [legacy], "round": 1}) + "\n")

    first = backfill_research_provenance(tmp_path)
    assert first["stamped"] == 1
    assert first["inferred"] == 1
    assert first["trusted"] == 0
    checkpoint = json.loads(path.read_text())
    env = checkpoint["findings"][0]["provenance"]
    assert checkpoint["findings"][0]["trust"] == "web"
    assert checkpoint["findings"][0]["text"] == "Legacy\nOld\nBytes"
    assert provenance.validate_envelope(env) == []
    assert env["attribution"] == "inferred"
    assert env["trust"]["label"] == "unknown"
    assert env["trust"]["label"] not in {"reviewed", "verified", "untrusted"}

    second = backfill_research_provenance(tmp_path)
    assert second["stamped"] == 0
    assert second["unchanged"] >= 1
    assert json.loads(path.read_text())["findings"][0]["provenance"] == env


def test_research_provenance_backfill_cli(tmp_path: Path, capsys) -> None:
    run_id = "20260817-cli-backfill"
    registry.create_run(tmp_path, question="q", run_id=run_id, caps={})
    path = registry.run_dir(tmp_path, run_id) / "checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "source_ids": ["src-1111111111111111"],
                        "title": "T",
                        "summary": "S",
                        "evidence": "E",
                        "trust": "local",
                        "extraction_lane": "legacy",
                        "extracted_at": "1970-01-01T00:00:00+00:00",
                    }
                ]
            }
        )
        + "\n"
    )
    assert cli.main(["research", "provenance", "backfill", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stamped"] == 1
    assert payload["trusted"] == 0
    assert payload["inferred"] == 1
