"""Expensive prefilter parity checks, isolated for per-file test budgets."""

import json
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name", list(json.loads((Path(__file__).parent / "fixtures/memory-scan-adversarial.json").read_text()))
)
def test_s18_round5_prefilter_parity_corpus(name, monkeypatch):
    from brigade import memory_content_scan as scan

    vectors = runpy.run_path(str(Path(__file__).with_name("memory_scan_adversarial.py")))["vectors"]

    # Compare the same v6 rules with and without the necessary-condition
    # filters, including both Unicode and ASCII modes and all six positions.
    texts = [text for _, text in vectors(name, 512 * 1024)]
    expected = [scan.blocked_rule([text]) for text in texts]
    monkeypatch.setattr(scan, "_rule_possible", lambda name, text: True)
    monkeypatch.setattr(scan, "_assignment_search", lambda pattern, text: bool(pattern.search(text)))
    assert [scan.blocked_rule([text]) for text in texts] == expected
