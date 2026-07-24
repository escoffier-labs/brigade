"""Focused tests for the Brigade-side Agent Pantry version compatibility probe.

The probe is the single shared parser/comparator used by the managed doctor,
pantry status/doctor, and expiry-alert paths before any installed agentpantry
surface is invoked. These tests pin the floor, the integer (not lexical)
comparison, and the rejection rules for dev/unknown/missing/non-string/
prerelease-shaped/unparsable versions, plus the no-leak guarantee: detail and
observed never carry stdout beyond the version field.
"""

from __future__ import annotations

import json

import pytest

from brigade import pantry_compat


def test_floor_is_evidence_backed_0_5_0():
    assert pantry_compat.AGENTPANTRY_MIN_VERSION == (0, 5, 0)
    assert pantry_compat.floor_label() == "expected >= 0.5.0"


def test_parse_version_accepts_plain_and_v_prefixed_triples():
    assert pantry_compat.parse_version("0.5.0") == (0, 5, 0)
    assert pantry_compat.parse_version("v0.5.0") == (0, 5, 0)
    assert pantry_compat.parse_version("v0.10.3") == (0, 10, 3)
    assert pantry_compat.parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_rejects_non_triples_and_prerelease_shapes():
    assert pantry_compat.parse_version("dev") is None
    assert pantry_compat.parse_version("unknown") is None
    assert pantry_compat.parse_version("0.5") is None
    assert pantry_compat.parse_version("0.5.0.0") is None
    assert pantry_compat.parse_version("0.5.0-dev") is None
    assert pantry_compat.parse_version("0.5.0-rc.1") is None
    assert pantry_compat.parse_version("0.5.0+build.1") is None
    assert pantry_compat.parse_version("") is None
    assert pantry_compat.parse_version("  ") is None
    assert pantry_compat.parse_version(None) is None
    assert pantry_compat.parse_version(42) is None
    assert pantry_compat.parse_version(["0.5.0"]) is None


def _probe(monkeypatch, stdout: str, code: int = 0, stderr: str = ""):
    monkeypatch.setattr(
        pantry_compat.proc,
        "run",
        lambda args, **kw: pantry_compat.proc.Result(code=code, stdout=stdout, stderr=stderr),
    )
    return pantry_compat.probe_agentpantry_version()


def test_probe_compatible_at_exact_floor(monkeypatch):
    probe = _probe(monkeypatch, json.dumps({"version": "0.5.0"}))
    assert probe.compatible is True
    assert probe.incompatible is False
    assert probe.observed == "0.5.0"
    assert "expected >= 0.5.0" in probe.detail
    assert "0.5.0" in probe.detail


def test_probe_compatible_above_floor_multi_digit(monkeypatch):
    # Integer comparison: 0.10.3 must sort above 0.5.0 (lexical compare would
    # put "0.10.3" below "0.5.0" because "1" < "5").
    probe = _probe(monkeypatch, json.dumps({"version": "v0.10.3"}))
    assert probe.compatible is True
    assert probe.observed == "0.10.3"


def test_probe_incompatible_below_floor(monkeypatch):
    probe = _probe(monkeypatch, json.dumps({"version": "0.4.1"}))
    assert probe.compatible is False
    assert probe.observed == "0.4.1"
    assert "expected >= 0.5.0" in probe.detail
    assert "0.4.1" in probe.detail
    assert "below floor" in probe.detail


def test_probe_incompatible_on_nonzero_exit(monkeypatch):
    probe = _probe(monkeypatch, stdout="", code=1, stderr="flag not defined")
    assert probe.compatible is False
    assert "probe exit 1" in probe.observed
    assert "expected >= 0.5.0" in probe.detail
    assert "probe exit 1" in probe.detail


def test_probe_incompatible_on_malformed_json(monkeypatch):
    probe = _probe(monkeypatch, stdout="totally-not-json")
    assert probe.compatible is False
    assert probe.observed == "malformed"
    assert "expected >= 0.5.0" in probe.detail


def test_probe_incompatible_on_non_object_json(monkeypatch):
    probe = _probe(monkeypatch, stdout=json.dumps(["0.5.0"]))
    assert probe.compatible is False
    assert probe.observed == "malformed"
    assert "expected >= 0.5.0" in probe.detail


def test_probe_incompatible_on_missing_version_field(monkeypatch):
    probe = _probe(monkeypatch, stdout=json.dumps({"other": "field"}))
    assert probe.compatible is False
    assert probe.observed == "missing"
    assert "expected >= 0.5.0" in probe.detail
    assert "missing" in probe.detail


def test_probe_incompatible_on_non_string_version(monkeypatch):
    probe = _probe(monkeypatch, stdout=json.dumps({"version": 42}))
    assert probe.compatible is False
    assert probe.observed == "non-string"
    assert "expected >= 0.5.0" in probe.detail
    assert "is not a string" in probe.detail


@pytest.mark.parametrize("version", ["dev", "unknown", "0.5.0-dev", "0.5.0-rc.1", "0.5", ""])
def test_probe_incompatible_on_unparsable_string_versions(monkeypatch, version):
    probe = _probe(monkeypatch, stdout=json.dumps({"version": version}))
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert "rejected by version policy" in probe.detail
    assert "unreleased or non-semver build" in probe.detail
    assert pantry_compat.released_floor_label() in probe.detail
    assert "unparsable" not in probe.detail
    # Any unparsable string collapses to the fixed sanitized label; the raw
    # invalid version field never reaches observed or detail.
    if version:
        assert version not in probe.observed
        # detail carries the fixed safe floor text, so a raw value that
        # overlaps the floor (e.g. "0.5" is a substring of "expected released
        # >= 0.5.0") would make a bare substring assertion vacuously fail.
        # Strip the fixed floor text first; the raw invalid field must not
        # appear in the remainder, which still proves no raw content leaks.
        assert version not in probe.detail.replace(pantry_compat.released_floor_label(), "")


def test_probe_unparsable_secret_version_field_never_leaks(monkeypatch):
    # Obvious secret material placed in the version field must not reach
    # observed/detail (and therefore not reach managed doctor / pantry JSON).
    secret = "AKIA-DEADFAKE-SECRET-KEY-DO-NOT-LEAK"
    probe = _probe(monkeypatch, stdout=json.dumps({"version": secret}))
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert secret not in probe.observed
    assert secret not in probe.detail
    assert "rejected by version policy" in probe.detail


def test_probe_unparsable_version_field_never_leaks_other_stdout(monkeypatch):
    # Even when stdout carries extra fields alongside an unparsable version,
    # only the fixed sanitized label surfaces; no other stdout content leaks.
    stdout = json.dumps({"version": "prerelease", "secret": "leak-me", "path": "/home/user/private"})
    probe = _probe(monkeypatch, stdout=stdout)
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert "leak-me" not in probe.observed
    assert "leak-me" not in probe.detail
    assert "/home/user/private" not in probe.observed
    assert "/home/user/private" not in probe.detail
    assert "prerelease" not in probe.observed
    assert "prerelease" not in probe.detail
    assert "rejected by version policy" in probe.detail


def test_probe_below_floor_still_exposes_normalized_semver(monkeypatch):
    # Successfully parsed versions (including below-floor ones) remain
    # normalized semver because that is bounded numeric data, not raw content.
    # Extra stdout fields never reach observed/detail regardless of parse.
    stdout = json.dumps({"version": "0.4.1", "secret": "leak-me", "path": "/home/user/private"})
    probe = _probe(monkeypatch, stdout=stdout)
    assert probe.compatible is False
    assert probe.observed == "0.4.1"
    assert "0.4.1" in probe.detail
    assert "below floor" in probe.detail
    assert "leak-me" not in probe.observed
    assert "leak-me" not in probe.detail
    assert "/home/user/private" not in probe.observed
    assert "/home/user/private" not in probe.detail


def test_probe_uses_version_json_surface(monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append((args, kw))
        return pantry_compat.proc.Result(code=0, stdout='{"version": "0.5.0"}', stderr="")

    monkeypatch.setattr(pantry_compat.proc, "run", fake_run)
    probe = pantry_compat.probe_agentpantry_version()
    assert probe.compatible is True
    assert seen[0][0] == ["agentpantry", "version", "--json"]
    # The probe is bounded so a hung binary cannot block doctor for long.
    assert seen[0][1]["timeout"] == 10.0


def test_parse_version_rejects_huge_whitespace_padded_input():
    # Overlong raw input must fail before strip/regex so hostile padding cannot
    # force an unbounded scan.
    bound = pantry_compat._MAX_RAW_VERSION_LEN
    padded = (" " * (bound * 1000)) + "0.5.0" + (" " * (bound * 1000))
    assert len(padded) > bound
    assert pantry_compat.parse_version(padded) is None


def test_parse_version_rejects_oversized_numeric_segments():
    # Arbitrarily long numeric segments must not throw or allocate unbounded
    # ints; the conservative per-segment bound collapses them to None (which
    # the probe surfaces as the fixed invalid-version label).
    bound = pantry_compat._MAX_SEGMENT_DIGITS
    oversized = "0" * (bound + 1)
    assert pantry_compat.parse_version(f"{oversized}.5.0") is None
    assert pantry_compat.parse_version(f"0.{oversized}.0") is None
    assert pantry_compat.parse_version(f"0.5.{oversized}") is None
    # A segment exactly at the bound still parses.
    at_bound = "9" * bound
    assert pantry_compat.parse_version(f"{at_bound}.5.0") == (int(at_bound), 5, 0)


def test_parse_version_rejects_non_ascii_digits():
    # ``\d`` would match Unicode digits; the parser accepts ASCII numerics only
    # so Arabic-Indic and fullwidth digit runs collapse to None.
    assert pantry_compat.parse_version("٠.٥.٠") is None
    assert pantry_compat.parse_version("v٠.٥.٠") is None
    assert pantry_compat.parse_version("１.２.３") is None
    assert pantry_compat.parse_version("0.5.০") is None


def test_probe_oversized_segment_collapses_to_invalid_version_label(monkeypatch):
    # A hostile version field with an arbitrarily long numeric segment must not
    # throw, allocate unbounded ints, or echo raw content. It collapses to the
    # fixed sanitized label in observed/detail and never leaks the raw run.
    bound = pantry_compat._MAX_SEGMENT_DIGITS
    huge = "9" * (bound * 1000)
    raw_version = f"{huge}.5.0"
    probe = _probe(monkeypatch, stdout=json.dumps({"version": raw_version}))
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert huge not in probe.observed
    assert huge not in probe.detail
    assert raw_version not in probe.observed
    assert raw_version not in probe.detail
    assert pantry_compat.released_floor_label() in probe.detail


def test_probe_non_ascii_digit_version_collapses_to_invalid_version_label(monkeypatch):
    # Non-ASCII-digit version fields must collapse to the fixed sanitized
    # label without echoing the raw Unicode content.
    raw_version = "٠.٥.٠"
    probe = _probe(monkeypatch, stdout=json.dumps({"version": raw_version}))
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert raw_version not in probe.observed
    assert raw_version not in probe.detail
    assert pantry_compat.released_floor_label() in probe.detail


def test_probe_oversized_segment_never_leaks_other_stdout(monkeypatch):
    # Even with an oversized segment alongside extra stdout fields, only the
    # fixed sanitized label surfaces; no other stdout content leaks.
    bound = pantry_compat._MAX_SEGMENT_DIGITS
    huge = "7" * (bound * 500)
    stdout = json.dumps({"version": f"{huge}.5.0", "secret": "leak-me", "path": "/home/user/private"})
    probe = _probe(monkeypatch, stdout=stdout)
    assert probe.compatible is False
    assert probe.observed == "invalid-version"
    assert huge not in probe.observed
    assert huge not in probe.detail
    assert "leak-me" not in probe.observed
    assert "leak-me" not in probe.detail
    assert "/home/user/private" not in probe.observed
    assert "/home/user/private" not in probe.detail
