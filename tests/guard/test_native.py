"""Exercise the binary when supplied, and keep fallback independent of Rust."""

import json
import os
import re
import runpy
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from brigade.guard.engine import scan_text
from brigade.guard import native as native_module
from brigade.guard.native import MIN_NATIVE_CHARS, native_spans, rule_spans
from brigade.guard.rules import DEFAULT_RULES
from brigade.guard.types import Rule


@pytest.fixture(autouse=True)
def _reset_native_failures():
    native_module.reset_native_failures()
    yield
    native_module.reset_native_failures()


def test_native_matches_python(monkeypatch):
    if not os.environ.get("BRIGADE_GUARD_BIN"):
        pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
    text = "'C:/Users/example/a b.txt' /root/example localhost port 12345\n"  # content-guard: allow windows-home-path content-guard: allow root-path
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    expected = scan_text(text)
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    assert native_spans(text, list(DEFAULT_RULES)) is not None
    assert scan_text(text) == expected


@pytest.mark.parametrize(
    "text",
    ["é localhost", "K@example.com", "\x1clocalhost", "😀 /root/example"],  # content-guard: allow root-path
)
def test_unicode_and_space_controls_keep_python_semantics(text, monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    expected = scan_text(text)
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    assert native_spans(text, list(DEFAULT_RULES)) is None
    assert scan_text(text) == expected


def test_custom_patterns_keep_python_semantics(monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    assert native_spans("abc", [Rule("custom", "pii", r"(?u)\w+", "")]) is None


def test_auto_threshold(monkeypatch):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "auto")
    run = Mock(side_effect=OSError)
    monkeypatch.setattr("brigade.guard.native.subprocess.run", run)
    monkeypatch.setenv("BRIGADE_GUARD_BIN", "unused")
    assert native_spans("a" * (MIN_NATIVE_CHARS - 1), list(DEFAULT_RULES)) is None
    run.assert_not_called()
    assert native_spans("a" * MIN_NATIVE_CHARS, list(DEFAULT_RULES)) is None
    run.assert_called_once()


@pytest.mark.parametrize("failure", ["exit", "timeout", "json", "version", "offsets", "shape"])
def test_native_errors_fall_back_once(monkeypatch, caplog, failure):
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    expected = scan_text("localhost")
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    monkeypatch.setenv("BRIGADE_GUARD_BIN", "unused")
    payload = {"protocol": 99, "rules": []}
    if failure == "offsets":
        payload = {"protocol": 1, "rules": [{"id": r.id, "spans": []} for r in DEFAULT_RULES]}
        payload["rules"][0]["spans"] = [{"start": -1, "end": 2, "byte_start": -1, "byte_end": 2}]
    run = Mock(return_value=Mock(stdout="invalid" if failure == "json" else json.dumps(payload)))
    if failure == "shape":
        run.return_value.stdout = "null"
    if failure == "exit":
        run.side_effect = subprocess.CalledProcessError(1, "unused")
    if failure == "timeout":
        run.side_effect = subprocess.TimeoutExpired("unused", 30)
    monkeypatch.setattr("brigade.guard.native.subprocess.run", run)
    assert scan_text("localhost") == expected
    assert len(caplog.records) == 1
    assert "using Python re" in caplog.text


def test_parity_drive_uses_native_matching(monkeypatch):
    if not os.environ.get("BRIGADE_GUARD_BIN"):
        pytest.skip("set BRIGADE_GUARD_BIN to exercise the built engine")
    helper = Path(__file__).resolve().parents[2] / "registry/skills/verify-brigade/control-brigade.py"
    fixture = runpy.run_path(str(helper))["GUARD_PARITY_FIXTURE"]
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    spans = native_spans(fixture, list(DEFAULT_RULES))
    assert spans is not None, "the acceptance fixture must execute in PCRE2, without fallback"
    assert spans == [[m.span() for m in re.finditer(r.pattern, fixture, r.flags)] for r in DEFAULT_RULES]


def test_python_fallback_spans_stay_lazy_per_rule(monkeypatch):
    # The fallback must not buffer every match before the engine filters them;
    # discarded spans (such as fenced code with scan_code_blocks off) used to stream.
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "python")
    rules = list(DEFAULT_RULES)
    spans = rule_spans("plain text " * 100, rules)
    assert len(spans) == len(rules)
    assert all(not isinstance(per_rule, list) for per_rule in spans)
    assert all(hasattr(per_rule, "__next__") for per_rule in spans)


def test_failed_binary_is_not_retried_in_the_same_process(tmp_path, monkeypatch):
    # A hung or broken binary must not stall every large file of a directory scan.
    # Patch the process call instead of running a script, so this works on every OS.
    fake = tmp_path / "brigade-guard"
    fake.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def failing_run(argv, **_kwargs):
        calls.append(list(argv))
        raise native_module.subprocess.CalledProcessError(3, argv)

    monkeypatch.setattr(native_module.subprocess, "run", failing_run)
    monkeypatch.setenv("BRIGADE_GUARD_BIN", str(fake))
    monkeypatch.setenv("BRIGADE_GUARD_BACKEND", "native")
    rules = list(DEFAULT_RULES)
    assert native_spans("plain ascii text", rules) is None
    assert native_spans("more plain ascii text", rules) is None
    assert len(calls) == 1
