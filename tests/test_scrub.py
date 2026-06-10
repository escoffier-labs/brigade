"""Tests for brigade scrub policy resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from brigade import scrub as scrub_mod


def test_resolve_named_policy_prefers_target(tmp_path: Path):
    target = tmp_path / "ws"
    (target / ".brigade" / "policies").mkdir(parents=True)
    local = target / ".brigade" / "policies" / "public-repo.json"
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
    fallback = scanner / "policies" / "scanner-only.json"
    fallback.write_text("{}")

    p = scrub_mod._resolve_policy(target, scanner, "scanner-only")
    assert p == fallback


def test_resolve_named_policy_falls_back_to_packaged_policy(tmp_path: Path):
    target = tmp_path / "ws"
    target.mkdir()
    scanner = tmp_path / "scanner"
    scanner.mkdir()

    p = scrub_mod._resolve_policy(target, scanner, "personal")
    assert p.name == "personal.json"
    assert p.is_file()
    assert "templates/policies" in p.as_posix()


def test_hook_status_detects_configured_global_pre_push(tmp_path: Path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/usr/bin/env bash\n")
    hook.chmod(0o755)
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(tmp_path / "content-guard"))

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["git", "-C", str(target)] and argv[-1] == "core.hooksPath":
            class Result:
                returncode = 0
                stdout = str(hooks)

            return Result()
        if argv[:3] == ["git", "-C", str(target)] and argv[-1] == "--git-dir":
            class Result:
                returncode = 0
                stdout = ".git"

            return Result()
        raise AssertionError(argv)

    monkeypatch.setattr(scrub_mod.subprocess, "run", fake_run)
    status = scrub_mod.hook_status(target, policy="personal")

    assert status["pre_push_hook_enabled"] is True
    assert status["pre_push_hook_mode"] == "configured-hooks-path"
    assert status["configured_pre_push_hook_path"] == str(hook)
    assert status["policy_exists"] is True
    assert calls


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

    Names like `..` would otherwise resolve to `.brigade/policies/...json`
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


def _fake_git_run(target, *, global_hooks, local_hooks_path=None):
    def fake_run(argv, **kwargs):
        if argv[:3] == ["git", "-C", str(target)] and argv[-1] == "core.hooksPath":
            if "--local" in argv:
                class Result:
                    returncode = 0 if local_hooks_path else 1
                    stdout = local_hooks_path or ""
                return Result()

            class Result:
                returncode = 0
                stdout = str(global_hooks)
            return Result()
        if argv[:3] == ["git", "-C", str(target)] and argv[-1] == "--git-dir":
            class Result:
                returncode = 0
                stdout = ".git"
            return Result()
        raise AssertionError(argv)
    return fake_run


def test_hook_status_flags_inherited_hookspath_without_content_guard(tmp_path: Path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/usr/bin/env bash\necho unrelated personal hook\n")
    hook.chmod(0o755)
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(tmp_path / "content-guard"))
    monkeypatch.setattr(scrub_mod.subprocess, "run", _fake_git_run(target, global_hooks=hooks))

    status = scrub_mod.hook_status(target, policy="personal")

    assert status["pre_push_hook_enabled"] is False
    assert status["pre_push_hook_mode"] == "external-hooks-path"
    assert any(check["name"] == "content_guard_hook_unrelated" for check in status["checks"])


def test_hook_status_accepts_inherited_hookspath_running_content_guard(tmp_path: Path, monkeypatch):
    target = tmp_path / "repo"
    target.mkdir()
    hooks = tmp_path / "global-hooks"
    hooks.mkdir()
    hook = hooks / "pre-push"
    hook.write_text("#!/usr/bin/env bash\nexec content-guard scan --policy public-repo\n")
    hook.chmod(0o755)
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(tmp_path / "content-guard"))
    monkeypatch.setattr(scrub_mod.subprocess, "run", _fake_git_run(target, global_hooks=hooks))

    status = scrub_mod.hook_status(target, policy="personal")

    assert status["pre_push_hook_enabled"] is True
    assert status["pre_push_hook_mode"] == "configured-hooks-path"
