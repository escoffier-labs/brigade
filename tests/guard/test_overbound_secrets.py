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
