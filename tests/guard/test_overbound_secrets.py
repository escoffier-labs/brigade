"""Ceilings must block and redact the complete secret, on either backend."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from brigade.guard import scan_text
from brigade.guard.native import native_spans
from brigade.guard.rules import DEFAULT_RULES
from brigade.evidence_redaction import apply_origin_redaction

CASES = json.loads((Path(__file__).parents[1] / "fixtures/memory-scan-overbound.json").read_text())


@pytest.mark.parametrize("name", list(CASES))
@pytest.mark.parametrize("backend", ["python", "native"])
def test_round5_guard_and_evidence_redact_complete_secret(name, backend, monkeypatch):
    case = CASES[name]
    text = "".join(case["prefix_parts"]) + case["unit"] * case["repeat"] + "".join(case["suffix_parts"])
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", backend)
    if backend == "native":
        if not os.environ.get("BRIGADE_GUARD_BIN"):
            pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
        assert native_spans(text, list(DEFAULT_RULES)) is not None
    tail = case["unit"] * 60
    # PEM lines contain 64-character runs. Checking 60 catches any leaked line.
    if "private-key" in name:
        tail = "a" * 60
    result = scan_text(text + "\nUnrelated note.\n")
    assert any(f.category == "secret" for f in result.findings), name
    assert tail not in result.redacted_text, name
    assert result.redacted_text.endswith("\nUnrelated note.\n")
    verdict = apply_origin_redaction(text, origin="agent-session")
    assert verdict.status == "redacted"
    assert tail not in verdict.persisted_text, name
    process = subprocess.run(
        [sys.executable, "-m", "brigade", "guard", "redact"], input=text, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert tail not in process.stdout, name


@pytest.fixture(params=["python", "native"])
def pem_backend(request, monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", request.param)
    if request.param == "native":
        if not os.environ.get("BRIGADE_GUARD_BIN"):
            pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
        assert native_spans("ordinary text", list(DEFAULT_RULES)) is not None


@pytest.mark.parametrize("ending", ["closed", "unterminated"])
@pytest.mark.parametrize("repeat", [2, 140])
def test_pem_trailing_findings_keep_secret_and_unrelated_text(pem_backend, ending, repeat):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    body = ("a" * 64 + "\n") * repeat
    closing = "-----END PRIVATE KEY-----\n" if ending == "closed" else ""
    # Synthetic canaries exercise independent trailing decisions.
    trailing = "Contact planted@probe-corp-canary.com host 10.23.45.67 logs /home/example-user/work done.\n"  # content-guard: allow email content-guard: allow private-ipv4 content-guard: allow home-path
    text = header + body + closing + trailing
    expected_guard = scan_text(trailing).redacted_text
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert "a" * 60 not in result.redacted_text
    assert result.redacted_text.endswith(expected_guard)
    assert {"email", "private-ipv4", "home-path"} <= {f.rule_id for f in result.findings}
    assert "Contact " in expected_guard and " done." in expected_guard
    for origin in ("agent-session", "workspace", "external-web"):
        verdict = apply_origin_redaction(text, origin=origin)
        assert verdict.status == "redacted"
        assert "private-key-block" in verdict.detectors
        assert "a" * 60 not in verdict.persisted_text
        assert verdict.persisted_text.endswith(apply_origin_redaction(trailing, origin=origin).persisted_text)
    process = subprocess.run(
        [sys.executable, "-m", "brigade", "guard", "redact"], input=text, capture_output=True, text=True, timeout=30
    )
    assert process.returncode == 1
    assert "a" * 60 not in process.stdout
    assert expected_guard in process.stdout


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\r\n\r\n"])
def test_pem_header_only_keeps_following_paragraph(pem_backend, separator):
    text = "A log mentions:\n-----BEGIN " + "RSA PRIVATE KEY-----" + separator + "The following paragraph stays.\n"
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert result.redacted_text.endswith(separator + "The following paragraph stays.\n")
    verdict = apply_origin_redaction(text, origin="agent-session")
    assert verdict.persisted_text.endswith(separator + "The following paragraph stays.\n")


@pytest.mark.parametrize("origin", ["agent-session", "workspace", "external-web"])
def test_expanded_pem_supersedes_contained_nonsecret_edits(pem_backend, origin):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    text = (
        header
        + "Proc-Type: 4,ENCRYPTED\nDEK-Info: 10.23.45.67 planted@probe-corp-canary.com\n\n"  # content-guard: allow email content-guard: allow private-ipv4
        + "a" * 64
        + "\n"
    )
    result = scan_text(text)
    assert any(f.category == "secret" for f in result.findings)
    assert result.redacted_text == "[redacted-private-key]\n"
    verdict = apply_origin_redaction(text, origin=origin)
    assert verdict.status == "redacted"
    assert verdict.persisted_text == "[redacted-private-key]\n"


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n"])
def test_pem_blank_after_body_ends_mask(pem_backend, separator):
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    text = header + "a" * 64 + separator + "Paragraph\n"
    result = scan_text(text)
    assert result.redacted_text == "[redacted-private-key]" + separator + "Paragraph\n"
