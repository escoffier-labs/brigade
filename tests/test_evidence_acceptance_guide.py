from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs/evidence-acceptance.md"


def test_evidence_acceptance_guide_covers_identity_time_and_fleet_boundaries() -> None:
    text = " ".join(GUIDE.read_text().split()).lower()

    for required in (
        "controlling-principal mapping",
        "authorized interval",
        "independent trust-root",
        "unknown",
        "local declared time",
        "authenticated source assertion",
        "independently anchored time",
        "same human",
        "hostile-tenant isolation",
        "#1093",
        "#1151",
        "#1352",
        "#1213",
    ):
        assert required in text


def test_evidence_acceptance_guide_has_bounded_examples_and_audit_roles() -> None:
    text = " ".join(GUIDE.read_text().split())

    for example in (
        "Same human, two keys",
        "Key rotation",
        "Revoked key",
        "Expired approval",
        "Unknown requester",
        "Missing historical policy",
        "Shared-workspace signer",
    ):
        assert example in text

    for packet_element in (
        "request",
        "exact changes",
        "actual checks",
        "approval",
        "review",
        "release",
        "failed",
        "interrupted",
    ):
        assert packet_element in text.lower()

    assert "Software evidence" in text
    assert "Organizational process" in text
    assert "Auditor decision" in text
    assert "assessor chooses the sample size" in text
    assert "not an audit conclusion or product certification" in text
