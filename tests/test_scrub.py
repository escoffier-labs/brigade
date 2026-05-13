"""Tests for solo-mise scrub policy resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from solo_mise import scrub as scrub_mod


def test_resolve_named_policy_prefers_target(tmp_path: Path):
    target = tmp_path / "ws"
    (target / ".solo-mise" / "policies").mkdir(parents=True)
    local = target / ".solo-mise" / "policies" / "public-repo.json"
    local.write_text("{}")
    scanner = tmp_path / "scanner"
    (scanner / "policies").mkdir(parents=True)
    (scanner / "policies" / "public-repo.json").write_text("{}")

    p = scrub_mod._resolve_policy(target, scanner, "public-repo")
    assert p == local


def test_resolve_named_policy_falls_back_to_scanner(tmp_path: Path):
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    (scanner / "policies").mkdir(parents=True)
    fallback = scanner / "policies" / "public-content.json"
    fallback.write_text("{}")

    p = scrub_mod._resolve_policy(target, scanner, "public-content")
    assert p == fallback


def test_resolve_explicit_path_is_used_verbatim(tmp_path: Path):
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    scanner.mkdir()
    explicit = tmp_path / "my-policy.json"
    explicit.write_text("{}")

    p = scrub_mod._resolve_policy(target, scanner, str(explicit))
    assert p == explicit


def test_resolve_rejects_traversal_in_bare_name(tmp_path: Path):
    """A bare name (no `/`, no `.json` suffix) containing `..` is rejected.

    Names like `..` would otherwise resolve to `.solo-mise/policies/...json`
    which still ends up inside the policy directory, but cleanly rejecting
    `..` tokens up front matches the documented "simple slug" contract.
    """
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    scanner.mkdir()
    with pytest.raises(ValueError):
        scrub_mod._resolve_policy(target, scanner, "..")


def test_resolve_path_with_slash_treated_as_literal(tmp_path: Path):
    """A value containing `/` is treated as a literal path, not a name.

    The user-supplied path is returned verbatim. If it doesn't exist the
    caller surfaces a "policy not found" error; if it does exist the
    user took responsibility for typing it. This matches the documented
    "if it looks like a path, use it as a path" rule.
    """
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    scanner.mkdir()
    result = scrub_mod._resolve_policy(target, scanner, "../escape")
    assert result == Path("../escape")


def test_scrub_returns_4_on_unsafe_bare_name(tmp_path: Path, monkeypatch):
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    scanner.mkdir()
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(scanner))
    rc = scrub_mod.run(target=target, policy="..", dry_run=True)
    assert rc == 4
