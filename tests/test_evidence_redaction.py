"""Origin-scoped evidence-ingestion redaction (#498)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import evidence_redaction, provenance, receipts_cmd, work_cmd
from brigade.guard.types import Finding as GuardFinding
from brigade.guard.types import GuardResult
from brigade.research.provenance import stamp_finding, stamp_finding_payload
from brigade.research.types import Finding
from brigade.work_cmd import ledger

PLANTED_EMAIL = "planted.canary9931@probe-corp-canary.com"
PLANTED_IP = "192.168.4.70"
PLANTED_BEARER = "zqXK9tPLANTEDBEARER7731aabbccddeeff0011"

FAKE_SECRET = 'api_key="sk-test-abcdefghijklmnopqrstuv"'
FAKE_EMAIL = "operator@contoso.example"
FAKE_IPV4 = "10.0.0.1"


def _secret_text() -> str:
    return f"note {FAKE_SECRET} done"


def _scan_boom(_text: str, **_kwargs: object) -> object:
    raise TimeoutError("scanner unavailable")


def test_classify_source_origin_matches_work_inbox_producers() -> None:
    for source, (origin, _modality) in ledger._IMPORT_SOURCE_ORIGIN_MODALITY.items():
        assert evidence_redaction.classify_source_origin(source) == origin
    assert evidence_redaction.classify_source_origin("external-web") == "external-web"
    assert evidence_redaction.classify_source_origin("external-service") == "external-service"
    assert evidence_redaction.classify_source_origin("unmapped-producer") == "unknown"
    assert evidence_redaction.classify_source_origin("") == "unknown"


@pytest.mark.parametrize(
    ("source", "origin"),
    [
        ("chat-memory-sweep", "agent-session"),
        ("Chat-Memory-Sweep", "agent-session"),
        ("CHAT-MEMORY-SWEEP", "agent-session"),
        ("chat-memory-sweep ", "agent-session"),
        (" chat-memory-sweep", "agent-session"),
        ("", "unknown"),
        ("zzz-garbage-source", "unknown"),
        ("Handoff-Ingest", "agent-session"),
        ("external-web", "external-web"),
        ("External-Web", "external-web"),
        ("EXTERNAL-SERVICE", "external-service"),
        ("external-service ", "external-service"),
    ],
)
def test_classify_source_origin_normalizes_and_fails_closed(source: str, origin: str) -> None:
    assert evidence_redaction.classify_source_origin(source) == origin


@pytest.mark.parametrize(
    ("source", "origin"),
    [
        ("manual", "external-web"),
        ("external-web", "external-web"),
        ("external-service", "external-service"),
        ("zzz-garbage-source", "unknown"),
        ("chat-memory-sweep", "external-web"),
    ],
)
def test_origin_for_external_ingest_upgrades_weaker_tiers(source: str, origin: str) -> None:
    assert evidence_redaction.origin_for_external_ingest(source) == origin


def test_unknown_or_invalid_origin_fails_closed_to_unknown() -> None:
    assert evidence_redaction.resolve_origin("external-web") == "external-web"
    assert evidence_redaction.resolve_origin("not-an-origin") == "unknown"
    assert evidence_redaction.resolve_origin("") == "unknown"


def test_workspace_redacts_secrets_but_not_email_or_host() -> None:
    text = f"contact {FAKE_EMAIL} host {FAKE_IPV4} {FAKE_SECRET}"
    verdict = evidence_redaction.apply_origin_redaction(text, origin="workspace")
    assert verdict.status == "redacted"
    assert verdict.count >= 1
    assert "api-key-assignment" in verdict.detectors
    assert FAKE_SECRET not in verdict.persisted_text
    assert FAKE_EMAIL in verdict.persisted_text
    assert FAKE_IPV4 in verdict.persisted_text
    dumped = json.dumps(verdict.record())
    assert FAKE_SECRET not in dumped
    assert "sk-test-" not in dumped


def test_agent_session_redacts_email_and_secret() -> None:
    text = f"contact {FAKE_EMAIL} {FAKE_SECRET}"
    verdict = evidence_redaction.apply_origin_redaction(text, origin="agent-session")
    assert verdict.status == "redacted"
    assert FAKE_SECRET not in verdict.persisted_text
    assert FAKE_EMAIL not in verdict.persisted_text
    assert "email" in verdict.detectors
    assert "api-key-assignment" in verdict.detectors


def test_external_web_redacts_private_ipv4() -> None:
    text = f"probe {FAKE_IPV4} ok"
    workspace = evidence_redaction.apply_origin_redaction(text, origin="workspace")
    assert workspace.status == "clean"
    assert FAKE_IPV4 in workspace.persisted_text
    web = evidence_redaction.apply_origin_redaction(text, origin="external-web")
    assert web.status == "redacted"
    assert FAKE_IPV4 not in web.persisted_text
    assert "private-ipv4" in web.detectors


def test_unknown_origin_uses_fail_closed_detector_set() -> None:
    text = f"contact {FAKE_EMAIL} host {FAKE_IPV4} {FAKE_SECRET}"
    verdict = evidence_redaction.apply_origin_redaction(text, origin="unknown")
    assert verdict.origin == "unknown"
    assert verdict.status == "redacted"
    assert FAKE_SECRET not in verdict.persisted_text
    assert FAKE_EMAIL not in verdict.persisted_text
    assert FAKE_IPV4 not in verdict.persisted_text


def test_clean_scan_records_policy_version_and_zero_count() -> None:
    verdict = evidence_redaction.apply_origin_redaction("ordinary operator note", origin="operator-input")
    record = verdict.record()
    assert verdict.status == "clean"
    assert verdict.count == 0
    assert record["policy_version"] == evidence_redaction.POLICY_VERSION
    assert record["schema"] == evidence_redaction.SCHEMA
    assert evidence_redaction.ingest_verdict_is_clean(record)
    assert evidence_redaction.validate_redaction_record(record) == []


def test_scanner_failure_is_not_clean_and_does_not_persist_original() -> None:
    original = _secret_text()
    verdict = evidence_redaction.apply_origin_redaction(original, origin="workspace", scanner=_scan_boom)
    record = verdict.record()
    assert verdict.status == "error"
    assert verdict.policy_version == evidence_redaction.POLICY_VERSION
    assert verdict.persisted_text == evidence_redaction.FAILED_PLACEHOLDER
    assert original not in verdict.persisted_text
    assert FAKE_SECRET not in json.dumps(record)
    assert not evidence_redaction.ingest_verdict_is_clean(record)
    assert evidence_redaction.validate_redaction_record(record) == []


def test_allow_comment_cannot_bypass_ingest_redaction() -> None:
    text = f"<!-- content-guard: allow all file -->\n{_secret_text()}"
    verdict = evidence_redaction.apply_origin_redaction(text, origin="workspace")
    assert verdict.status == "redacted"
    assert FAKE_SECRET not in verdict.persisted_text


def test_missing_or_foreign_record_is_not_a_clean_verdict() -> None:
    assert not evidence_redaction.ingest_verdict_is_clean(None)
    assert not evidence_redaction.ingest_verdict_is_clean({})
    assert not evidence_redaction.ingest_verdict_is_clean(
        {**evidence_redaction.apply_origin_redaction("ok", origin="workspace").record(), "policy_version": "other.v0"}
    )


def test_validate_redaction_record_rejects_stored_values() -> None:
    record = evidence_redaction.apply_origin_redaction(_secret_text(), origin="workspace").record()
    record["values"] = [FAKE_SECRET]
    errors = evidence_redaction.validate_redaction_record(record)
    assert errors
    assert any("removed values" in error for error in errors)


def test_make_import_redacts_before_persist_and_hashes_redacted_bytes() -> None:
    item = ledger._make_import(_secret_text(), kind="task", source="security-scan")
    env = item["metadata"]["provenance"]
    assert FAKE_SECRET not in item["text"]
    assert env["redaction"]["status"] == "redacted"
    assert env["redaction"]["count"] >= 1
    assert env["redaction"]["policy_version"] == evidence_redaction.POLICY_VERSION
    assert env["hashes"]["content"] == provenance.content_sha256(item["text"])
    assert provenance.validate_envelope(env) == []
    assert FAKE_SECRET not in json.dumps(env)


def test_make_import_scanner_failure_quarantines_and_drops_original(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence_redaction, "scan_text", _scan_boom)
    item = ledger._make_import(_secret_text(), kind="task", source="manual")
    env = item["metadata"]["provenance"]
    assert item["text"] == evidence_redaction.FAILED_PLACEHOLDER
    assert FAKE_SECRET not in item["text"]
    assert env["redaction"]["status"] == "error"
    assert env["trust"]["label"] == "quarantined"
    assert not evidence_redaction.ingest_verdict_is_clean(env["redaction"])


def test_backfill_does_not_rewrite_existing_secret_text(tmp_path: Path) -> None:
    legacy = {
        "id": "20260101-000000-task-legacy-redact",
        "kind": "task",
        "source": "manual",
        "text": _secret_text(),
        "status": "pending",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    ledger._write_imports(tmp_path, [legacy])
    result = ledger._backfill_import_provenance(tmp_path)
    loaded = ledger._read_imports(tmp_path)
    assert result["stamped"] == 1
    assert loaded[0]["text"] == _secret_text()
    env = loaded[0]["metadata"]["provenance"]
    assert "redaction" not in env
    assert not evidence_redaction.ingest_verdict_is_clean(env.get("redaction"))


def test_receipt_index_redacts_before_persist() -> None:
    text, record, failed = receipts_cmd._receipt_redact(f"Brigade run demo. Task: {_secret_text()}.")
    assert failed is False
    assert FAKE_SECRET not in text
    assert record["origin"] == "agent-session"
    assert record["status"] == "redacted"
    assert record["count"] >= 1
    assert FAKE_SECRET not in json.dumps(record)


def test_research_new_write_redacts_and_backfill_does_not() -> None:
    finding = Finding(
        source_ids=("src-1111111111111111",),
        title="Token note",
        summary="keep",
        evidence=_secret_text(),
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )
    stamped = stamp_finding(finding, run_id="run-redact", index=0)
    assert FAKE_SECRET not in stamped.evidence
    assert FAKE_SECRET not in stamped.text
    assert isinstance(stamped.provenance, dict)
    assert stamped.provenance["redaction"]["status"] == "redacted"
    assert stamped.provenance["hashes"]["content"] == provenance.content_sha256(stamped.text)

    legacy = {
        "source_ids": ["src-1111111111111111"],
        "title": "Legacy",
        "summary": "Old",
        "evidence": _secret_text(),
        "trust": "web",
        "extraction_lane": "legacy",
        "extracted_at": "1970-01-01T00:00:00+00:00",
    }
    payload = stamp_finding_payload(legacy, run_id="run-redact", index=0, inferred=True)
    assert payload["evidence"] == _secret_text()
    assert payload["text"].endswith(_secret_text())
    assert "redaction" not in payload["provenance"]


def test_build_envelope_accepts_optional_redaction_record() -> None:
    kwargs = dict(
        source_system="work-inbox",
        source_kind="manual",
        source_producer="ledger._make_import",
        origin="operator-input",
        repository_id="escoffier-labs/brigade",
        repository_revision=None,
        session_id="demo-session",
        session_harness="cursor",
        collection_id="demo-collection",
        item_id="demo:item:1",
        locator_kind="repo-relative",
        locator_value="demo/item.txt",
        attribution="observed",
        modality="human-written",
        trust_label="untrusted",
        trust_assigned_by="ingest:ledger._make_import",
        trust_assigned_at="2026-07-26T21:31:37.123456+00:00",
        injection_status="clean",
        injection_count=0,
        injection_rules=[],
        text="ordinary note",
        raw_bytes=None,
        content_scope="item.text.utf8.v1",
        captured_at="2026-07-26T21:31:37+00:00",
        ingested_at="2026-07-26T21:31:38+00:00",
    )
    clean = evidence_redaction.apply_origin_redaction("ordinary note", origin="operator-input").record()
    env = provenance.build_envelope(**kwargs, redaction=clean)
    assert provenance.validate_envelope(env) == []
    assert env["redaction"]["status"] == "clean"

    tainted = dict(clean)
    tainted["excerpts"] = ["secret"]
    with pytest.raises(ValueError, match="removed values"):
        provenance.build_envelope(**kwargs, redaction=tainted)


def _cleartext_hits(root: Path, *needles: str) -> list[Path]:
    hits: list[Path] = []
    encoded = [needle.encode() for needle in needles]
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(needle in data for needle in encoded):
            hits.append(path)
    return hits


@pytest.mark.parametrize(
    ("source", "origin"),
    [
        ("external-web", "external-web"),
        ("manual", "external-web"),
        ("external-service", "external-service"),
    ],
)
def test_import_context_redacts_planted_email_and_private_ip(tmp_path: Path, source: str, origin: str) -> None:
    text = f"External page says mail {PLANTED_EMAIL} ip {PLANTED_IP}"
    assert (
        work_cmd.import_context(
            target=tmp_path,
            text=text,
            source=source,
            context_kind="transcript",
        )
        == 0
    )
    assert _cleartext_hits(tmp_path, PLANTED_EMAIL, PLANTED_IP) == []
    item = ledger._read_imports(tmp_path)[0]
    dumped = json.dumps(item)
    assert PLANTED_EMAIL not in item["text"]
    assert PLANTED_IP not in item["text"]
    assert PLANTED_EMAIL not in dumped
    assert PLANTED_IP not in dumped
    env = item["metadata"]["provenance"]
    assert env["origin"] == origin
    assert env["redaction"]["origin"] == origin
    assert env["redaction"]["status"] == "redacted"
    assert "email" in env["redaction"]["detectors"]
    assert "private-ipv4" in env["redaction"]["detectors"]


@pytest.mark.parametrize(
    "source",
    ["chat-memory-sweep", "Chat-Memory-Sweep", "CHAT-MEMORY-SWEEP", "Handoff-Ingest"],
)
def test_import_add_case_variant_source_redacts_email(tmp_path: Path, source: str) -> None:
    assert (
        work_cmd.import_add(
            target=tmp_path,
            text=f"case variant contact {PLANTED_EMAIL}",
            kind="finding",
            source=source,
        )
        == 0
    )
    item = ledger._read_imports(tmp_path)[0]
    assert PLANTED_EMAIL not in item["text"]
    assert PLANTED_EMAIL not in json.dumps(item)
    env = item["metadata"]["provenance"]
    assert env["origin"] == "agent-session"
    assert env["redaction"]["origin"] == "agent-session"
    assert env["redaction"]["status"] == "redacted"
    assert "email" in env["redaction"]["detectors"]


def test_equal_but_distinct_unredacted_text_fails_closed() -> None:
    secret = f"Bearer {PLANTED_BEARER}"

    def scanner(text: str, **_kwargs: object) -> GuardResult:
        finding = GuardFinding(
            rule_id="bearer-token",
            category="secret",
            action="redact",
            match=secret,
            replacement="[redacted-secret]",
            line=1,
            column=1,
            start=0,
            end=len(secret),
        )
        # str(text) on a str returns the SAME object in CPython, which made
        # this test vacuous against the old `persisted is text` identity
        # check. Build a genuinely equal-but-distinct string instead.
        return GuardResult(text=text, redacted_text="".join(list(text)), findings=[finding])

    verdict = evidence_redaction.apply_origin_redaction(secret, origin="workspace", scanner=scanner)
    assert verdict.status == "error"
    assert verdict.count == 0
    assert secret not in verdict.persisted_text
    assert PLANTED_BEARER not in verdict.persisted_text
    assert verdict.persisted_text == evidence_redaction.FAILED_PLACEHOLDER
    assert secret not in json.dumps(verdict.record())


def test_stamp_finding_keeps_multiline_summary_out_of_evidence() -> None:
    finding = Finding(
        source_ids=("src-1111111111111111",),
        title="Token note",
        summary=f"summary line one contact {PLANTED_EMAIL}\nsummary line TWO stays in summary",
        evidence="evidence body here",
        trust="web",
        extraction_lane="luna",
        extracted_at="2026-08-13T12:00:00+00:00",
    )
    stamped = stamp_finding(finding, run_id="run-redact", index=0)
    assert PLANTED_EMAIL not in stamped.summary
    assert PLANTED_EMAIL not in stamped.evidence
    assert PLANTED_EMAIL not in stamped.text
    assert "summary line TWO stays in summary" in stamped.summary
    assert "summary line TWO stays in summary" not in stamped.evidence
    assert stamped.evidence == "evidence body here"
    assert isinstance(stamped.provenance, dict)
    assert stamped.provenance["redaction"]["status"] == "redacted"
    assert "email" in stamped.provenance["redaction"]["detectors"]
