"""RED: Claude work-loop hook runtime behavior (issue #249)."""

from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from types import SimpleNamespace

from brigade import cli, localio
from brigade.claude_hooks import command_tokens, envelope
from brigade.claude_hooks.package import PACKAGE_REF
from brigade.claude_hooks import fingerprint as hook_fingerprint
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.selection import Selection


def _wired_claude(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    return target


def _git_wired_claude(tmp_path: Path) -> Path:
    target = _wired_claude(tmp_path)
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def _payload(target: Path, event: str, *, session_id: str = "session-1", **extra):
    return {
        "session_id": session_id,
        "cwd": str(target),
        "hook_event_name": event,
        **extra,
    }


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return _wired_claude(tmp_path)


def _write_payload(target: Path, session_id: str = "s1"):
    return _payload(
        target,
        "PostToolUse",
        session_id=session_id,
        tool_name="Write",
        tool_input={"file_path": str(target / "src.py"), "content": "x\n"},
    )


def _unverified_write_stop(target: Path, session_id: str = "s1"):
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload("PostToolUse", _write_payload(target, session_id=session_id))
    return _payload(target, "Stop", session_id=session_id, stop_hook_active=False)


def _capture_presence(monkeypatch):
    calls = []

    def upsert(snap, **_kwargs):
        calls.append(SimpleNamespace(action="upsert", snapshot=snap))
        return True

    def end(snap, **_kwargs):
        calls.append(SimpleNamespace(action="end", snapshot=snap))
        return True

    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "http://127.0.0.1:3774")
    monkeypatch.setattr("brigade.fleet_client.upsert_session", upsert)
    monkeypatch.setattr("brigade.fleet_client.end_session", end)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")
    return calls


def test_session_start_injects_brief_once_per_repo(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    calls: list[Path] = []

    def fake_brief(repo: Path) -> str:
        calls.append(repo)
        return "work brief: test\nnext: fix issue"

    monkeypatch.setattr(runtime, "_run_brief", fake_brief)
    first = runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    second = runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))

    assert first["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = first["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("[Brigade] If this context appears truncated")
    assert "work brief: test" in context
    assert second is None
    assert calls == [target.resolve()]


def test_session_start_merges_recall_with_brief_once(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    hub = tmp_path / "hub"
    hub.mkdir()
    cards = hub / "memory" / "cards"
    cards.mkdir(parents=True)
    (cards / "astro.md").write_text(
        '---\ntitle: Astro Notes\ntags: ["astro"]\n---\nSECRET_BODY_TOKEN\n',
        encoding="utf-8",
    )
    from brigade.config import Config, load_config, write_config

    cfg = load_config(target)
    assert cfg is not None
    write_config(
        target,
        Config(
            version=cfg.version,
            selection=cfg.selection,
            memory_recall_target=str(hub),
            code_graph_delta_timeout_seconds=cfg.code_graph_delta_timeout_seconds,
            capture_before_retry=cfg.capture_before_retry,
            verify_runs_keep=cfg.verify_runs_keep,
            verify_archive_enabled=cfg.verify_archive_enabled,
            verify_archive_dir=cfg.verify_archive_dir,
            run_lock_wait_seconds=cfg.run_lock_wait_seconds,
        ),
    )
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "work brief: test")
    session_cwd = target / "astro-portfolio"
    session_cwd.mkdir()
    first = runtime.handle_payload(
        "SessionStart",
        _payload(target, "SessionStart", cwd=str(session_cwd)),
    )
    second = runtime.handle_payload(
        "SessionStart",
        _payload(target, "SessionStart", cwd=str(session_cwd)),
    )
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "work brief: test" in context
    assert "memory recall: astro portfolio" in context
    assert "Astro Notes" in context
    assert "SECRET_BODY_TOKEN" not in context
    assert second is None


def test_session_start_recall_fail_open_keeps_brief(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    from brigade.config import Config, load_config, write_config

    cfg = load_config(target)
    assert cfg is not None
    write_config(
        target,
        Config(
            version=cfg.version,
            selection=cfg.selection,
            memory_recall_target=str(tmp_path / "missing-hub"),
            code_graph_delta_timeout_seconds=cfg.code_graph_delta_timeout_seconds,
            capture_before_retry=cfg.capture_before_retry,
            verify_runs_keep=cfg.verify_runs_keep,
            verify_archive_enabled=cfg.verify_archive_enabled,
            verify_archive_dir=cfg.verify_archive_dir,
            run_lock_wait_seconds=cfg.run_lock_wait_seconds,
        ),
    )
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "work brief: only")
    result = runtime.handle_payload("SessionStart", _payload(target, "SessionStart"))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "work brief: only" in context
    assert "memory recall:" not in context


def test_session_start_compact_source_restores_brief_and_recall_once(tmp_path: Path, monkeypatch):
    from brigade.claude_hooks import compaction_marker

    target = _wired_claude(tmp_path)
    session_cwd = target / "astro-portfolio"
    session_cwd.mkdir()
    recall_text = "memory recall: astro portfolio\n- Astro Notes | tags: astro | memory/cards/astro.md"
    recall_calls: list[str | None] = []

    def fake_recall(_target: Path, payload: dict, **_kwargs: object) -> str:
        recall_calls.append(payload.get("source") if isinstance(payload.get("source"), str) else None)
        return recall_text

    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "compact-restored-brief")
    monkeypatch.setattr(runtime, "_run_recall", fake_recall)

    startup = runtime.handle_payload(
        "SessionStart",
        _payload(target, "SessionStart", source="startup", cwd=str(session_cwd)),
    )
    assert startup is not None
    assert "compact-restored-brief" in startup["hookSpecificOutput"]["additionalContext"]
    assert recall_calls == ["startup"]

    recall_calls.clear()
    compact = runtime.handle_payload(
        "SessionStart",
        _payload(target, "SessionStart", source="compact", cwd=str(session_cwd)),
    )
    assert compact is not None
    context = compact["hookSpecificOutput"]["additionalContext"]
    assert compaction_marker.RESTORE_PREFIX in context
    assert "compact-restored-brief" in context
    assert "memory recall: astro portfolio" in context
    assert context.count("memory recall: astro portfolio") == 1
    assert recall_calls == ["compact"]
    assert (
        runtime.handle_payload(
            "SessionStart",
            _payload(target, "SessionStart", source="startup", cwd=str(session_cwd)),
        )
        is None
    )


def test_all_events_are_inert_for_unwired_repo(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runtime, "_run_brief", lambda target: (_ for _ in ()).throw(AssertionError(target)))
    assert runtime.handle_payload("SessionStart", _payload(tmp_path, "SessionStart")) is None
    assert (
        runtime.handle_payload(
            "PreToolUse",
            _payload(tmp_path, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest"}),
        )
        is None
    )
    assert runtime.handle_payload("Stop", _payload(tmp_path, "Stop", stop_hook_active=False)) is None


def test_session_start_hints_init_for_unwired_git_repo(tmp_path: Path):
    repo = tmp_path / "fresh-repo"
    (repo / ".git").mkdir(parents=True)
    result = runtime.handle_payload("SessionStart", _payload(repo, "SessionStart"))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert f"brigade init --target {repo.resolve()}" in context
    assert "--harnesses claude" in context
    assert "brigade work brief" in context
    assert context.startswith("[Brigade] If this context appears truncated")


def test_session_start_stays_silent_for_home_directory(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home.resolve()))
    assert runtime.handle_payload("SessionStart", _payload(home, "SessionStart")) is None


def test_other_events_stay_silent_for_unwired_git_repo(tmp_path: Path):
    repo = tmp_path / "fresh-repo-2"
    (repo / ".git").mkdir(parents=True)
    assert (
        runtime.handle_payload(
            "PreToolUse",
            _payload(repo, "PreToolUse", tool_name="Bash", tool_input={"command": "pytest"}),
        )
        is None
    )
    assert runtime.handle_payload("Stop", _payload(repo, "Stop", stop_hook_active=False)) is None


def test_pretooluse_denies_raw_verification_with_exact_replacement(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    specific = result["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert "brigade work verify run" in reason
    assert f"--target {target.resolve()}" in reason
    assert "--capture auto" in reason
    assert "python -m pytest -q" in reason


def test_pretooluse_denies_raw_focused_verify_without_matching_similar_names(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "./scripts/verify-focused"}),
    )

    assert runtime.is_raw_verification("./scripts/verify-focused")
    assert not runtime.is_raw_verification("./scripts/verify-focused-extra")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_avoids_recursion_and_false_positive_noise(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command in (
        'brigade work verify run --target . --command "pytest" --capture brigade-work',
        "echo pytest",
        "echo '$(pytest -q)'",
        'echo "(pytest -q)"',
        "sh -c 'echo pytest'",
        "bash -o errexit -c 'echo pytest'",
        "bash script.sh -c pytest",
        "if true; then echo pytest; fi",
        "{ echo pytest; }",
        "python -V -m pytest",
        "python -c \"print('pytest')\"",
        "ruff format src/",
        "rg test src",
        "cat tests/test_cli.py",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        assert result is None, command


def test_pretooluse_denies_compound_containing_verifier(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "cd src && pytest -q"}),
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretooluse_denies_verifier_smuggled_with_routed_verify(tmp_path: Path):
    target = _wired_claude(tmp_path)
    routed = 'brigade work verify run --target . --command "pytest" --capture brigade-work'
    for command in (
        f"pytest -q && {routed}",
        f"{routed} && pytest -q",
        f"cd src && pytest -q; {routed}",
        "pytest brigade work verify",
        f"{routed}\npytest -q",
        f"pytest -q\n{routed}",
        "echo ok\npytest -q",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command


def test_pretooluse_denies_confident_verifier_wrappers(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command in (
        "uv run pytest -q",
        "uv run -- pytest -q",
        "uv run --no-sync pytest -q",
        "uv run --python 3.12 pytest -q",
        "uv run --with rich pytest -q",
        "uv --directory src run pytest -q",
        "poetry run pytest -q",
        "poetry run -- pytest -q",
        "poetry run -C src pytest -q",
        "poetry -C src run pytest -q",
        "npx jest",
        "npm run test:unit",
        "npm run -- test",
        "npm run -s test",
        "npm run --silent test",
        "pnpm --filter app run test",
        "yarn run -- test",
        "bun run -- test",
        "make -p test",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command


def test_pretooluse_denies_standard_shell_and_npx_wrappers(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command in (
        "env CI=1 pytest -q",
        "env -i CI=1 pytest -q",
        "env -v pytest -q",
        "env -iv pytest -q",
        "env -Csrc pytest -q",
        "env -uFOO pytest -q",
        "env --argv0=verify pytest -q",
        "env -S 'pytest -q'",
        "env --split-string='pytest -q'",
        "command pytest -q",
        "command -p pytest -q",
        "npx --yes jest",
        "npx --prefer-offline jest",
        "npx -y jest",
        "npx --prefix /tmp jest",
        "npx --node-options=--test jest",
        "npx --future-option /tmp jest",
        "python -u -m pytest -q",
        "python -B -m unittest",
        "python -d -m pytest -q",
        "python -X dev -m pytest -q",
        "python -W ignore -m pytest -q",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command


def test_pretooluse_withholds_exact_guidance_for_unknown_npx_options(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(
            target,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "npx --future-option /tmp jest"},
        ),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Use:" not in reason


def test_pretooluse_withholds_exact_guidance_for_unknown_runner_options(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(
            target,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "uv --future-option value run pytest -q"},
        ),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Use:" not in reason


def test_pretooluse_compound_guidance_routes_only_the_verifier_segment(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command, expected in (
        ("pytest -q; echo done", "pytest -q"),
        ("pytest -q\ntrue", "pytest -q"),
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        replacement = shlex.split(reason.split("Use: ", 1)[1])
        routed_command = replacement[replacement.index("--command") + 1]
        assert routed_command == expected, command


def test_pretooluse_denies_nested_verifiers_without_unsafe_guidance(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command in (
        "(pytest -q)",
        'echo "$(pytest -q)"',
        "pytest -q > result.txt",
        "printf setup | pytest -q",
        "cd -P src && pytest -q",
        "cd src && cd nested && pytest -q",
        "cd src; pytest -q",
        "cd src\npytest -q",
        "cd src && true && pytest -q",
        "sh -c 'pytest -q'",
        "bash -lc 'pytest -q'",
        "pytest -q 2>&1",
        "pytest -q &> result.txt",
        "printf setup |& pytest -q",
        'echo "$(echo "$(pytest -q)")"',
        "if true; then pytest -q; fi",
        "{ pytest -q; }",
        "bash -o errexit -c 'pytest -q'",
        "bash -O extglob -c 'pytest -q'",
        "sh -o errexit -c 'pytest -q'",
        "cd src && pytest -q",
        "pushd src && pytest -q",
        "builtin cd src && pytest -q",
        "command cd src && pytest -q",
        "source setup.sh && pytest -q",
        "echo setup && pytest -q",
        "cd - && pytest -q",
        "cd ~ && pytest -q",
        'cd "$SOURCE_ROOT" && pytest -q',
        'PYTHONPATH="$PWD/src" python -m pytest -q',
        "pytest -q tests/test_*.py",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command
        assert "Use:" not in reason, command
        assert "Split shell grouping" in reason, command


def test_pretooluse_guidance_preserves_safe_verifier_context(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command, expected in (
        ("CI=1 pytest -q", "CI=1 pytest -q"),
        ("env -C src pytest -q", "env -C src pytest -q"),
        ("command pytest -q", "pytest -q"),
        ("command -p pytest -q", "pytest -q"),
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        replacement = shlex.split(reason.split("Use: ", 1)[1])
        routed_command = replacement[replacement.index("--command") + 1]
        assert routed_command == expected, command


def test_pretooluse_denies_make_with_global_options(tmp_path: Path):
    target = _wired_claude(tmp_path)
    for command in (
        "make -C src test",
        "make --directory=src test",
        "make -j4 test",
        "make -j 4 check",
        "make -s verify",
    ):
        result = runtime.handle_payload(
            "PreToolUse",
            _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command}),
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command


def test_posttooluse_records_python_c_write_via_repo_snapshot(tmp_path: Path):
    target = _wired_claude(tmp_path)
    session_id = "python-c-write"
    out_file = target / "snapshot.py"
    command = f"{sys.executable} -c \"from pathlib import Path; Path({str(out_file)!r}).write_text('x')\""
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": command},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is False
    assert "pending_bash_fingerprint" in runtime.read_session_state(target, session_id)

    out_file.write_text("x")
    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    state = runtime.read_session_state(target, session_id)
    assert state["write_observed"] is True
    assert "pending_bash_fingerprint" not in state

    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_posttooluse_records_bash_write_when_other_session_only_reads(tmp_path: Path):
    """A read-only neighbor must not discard this session's Bash write.

    Probe from #987 review: B runs a Python write of ``real_source.py``; inside
    that Bash window A does one ``git log -1`` PreToolUse and nothing else.
    Bare session-state mtime is not foreign write evidence.
    """
    target = _wired_claude(tmp_path)
    writer_session = "session-b-build"
    reader_session = "session-a-git-log"
    out_file = target / "real_source.py"
    command = f"{sys.executable} -c \"from pathlib import Path; Path({str(out_file)!r}).write_text('built')\""
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=writer_session,
        tool_name="Bash",
        tool_input={"command": command},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, writer_session)["write_observed"] is False

    assert (
        runtime.handle_payload(
            "PreToolUse",
            _payload(
                target,
                "PreToolUse",
                session_id=reader_session,
                tool_name="Bash",
                tool_input={"command": "git log -1"},
            ),
        )
        is None
    )
    reader_state = runtime.read_session_state(target, reader_session)
    assert reader_state["write_observed"] is False
    assert "last_write_at" not in reader_state
    assert "pending_write_at" not in reader_state

    out_file.write_text("built")
    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    writer_state = runtime.read_session_state(target, writer_session)
    assert writer_state["write_observed"] is True
    assert writer_state.get("last_write_at")

    blocked = runtime.handle_payload(
        "Stop", _payload(target, "Stop", session_id=writer_session, stop_hook_active=False)
    )
    assert blocked["decision"] == "block"


def test_posttooluse_ignores_concurrent_session_write_during_read_only_bash(tmp_path: Path):
    target = _wired_claude(tmp_path)
    reader_session = "read-only-bash"
    writer_session = "concurrent-writer"
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=reader_session,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    pending_reader_state = runtime.read_session_state(target, reader_session)
    assert "pending_bash_fingerprint" in pending_reader_state
    assert "pending_bash_started_at" in pending_reader_state

    changed = target / "concurrent.py"
    changed.write_text("changed\n")
    assert (
        runtime.handle_payload(
            "PostToolUse",
            _payload(
                target,
                "PostToolUse",
                session_id=writer_session,
                tool_name="Write",
                tool_input={"file_path": str(changed)},
            ),
        )
        is None
    )
    assert runtime.read_session_state(target, writer_session)["write_observed"] is True

    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    reader_state = runtime.read_session_state(target, reader_session)
    assert reader_state["write_observed"] is False
    assert "pending_bash_fingerprint" not in reader_state
    assert "pending_bash_started_at" not in reader_state
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader_session, stop_hook_active=False))
        is None
    )


def test_posttooluse_ignores_interleaved_concurrent_write_with_stale_fingerprint(tmp_path: Path):
    """Issue #704: foreign writes must not re-arm closeout when the writer's
    recorded repo_fingerprint lags the live tree (second write on disk before
    that writer's next PostToolUse). Exact fingerprint equality is the escape
    the #395/#380 fix missed.
    """
    target = _wired_claude(tmp_path)
    reader_session = "reader-stale-fp"
    writer_session = "writer-stale-fp"
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=reader_session,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None

    first = target / "first.py"
    first.write_text("first\n")
    assert (
        runtime.handle_payload(
            "PostToolUse",
            _payload(
                target,
                "PostToolUse",
                session_id=writer_session,
                tool_name="Write",
                tool_input={"file_path": str(first)},
            ),
        )
        is None
    )
    writer_state = runtime.read_session_state(target, writer_session)
    assert writer_state["write_observed"] is True
    recorded_fp = writer_state["repo_fingerprint"]

    # Second concurrent write lands before the writer records the new fingerprint.
    second = target / "second.py"
    second.write_text("second\n")
    live_fp = runtime.repo_worktree_fingerprint(target)
    assert live_fp is not None
    assert recorded_fp != live_fp

    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    reader_state = runtime.read_session_state(target, reader_session)
    assert reader_state["write_observed"] is False
    assert "last_write_at" not in reader_state
    assert "pending_bash_fingerprint" not in reader_state
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader_session, stop_hook_active=False))
        is None
    )


def test_stop_keeps_prior_receipt_when_interleaved_foreign_write_lags_fingerprint(tmp_path: Path, monkeypatch):
    """Issue #704 receipt treadmill: a session that already verified must not
    have last_write_at raised by a concurrent session whose recorded fingerprint
    is no longer current.
    """
    target = _wired_claude(tmp_path)
    reader_session = "verified-reader"
    writer_session = "active-writer"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader_session))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader_session,
            tool_name="Write",
            tool_input={"file_path": str(target / "own.py")},
        ),
    )
    state = runtime.read_session_state(target, reader_session)
    write_at = state["last_verification_write_at"]
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": write_at,
                "completed_at": write_at,
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )

    pretool = _payload(
        target,
        "PreToolUse",
        session_id=reader_session,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None

    first = target / "foreign-a.py"
    first.write_text("foreign-a\n")
    assert (
        runtime.handle_payload(
            "PostToolUse",
            _payload(
                target,
                "PostToolUse",
                session_id=writer_session,
                tool_name="Write",
                tool_input={"file_path": str(first)},
            ),
        )
        is None
    )
    (target / "foreign-b.py").write_text("foreign-b\n")
    assert (
        runtime.repo_worktree_fingerprint(target)
        != runtime.read_session_state(target, writer_session)["repo_fingerprint"]
    )

    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    updated = runtime.read_session_state(target, reader_session)
    assert updated["last_verification_write_at"] == write_at
    assert updated["last_write_at"] == write_at

    handoff = target / ".claude" / "memory-handoffs" / "handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("durable finding\n")
    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader_session, stop_hook_active=False))
    assert result is None


def _write_session_receipt(target: Path, session_id: str, *, tree_fingerprint: str | None = None) -> dict:
    state = runtime.read_session_state(target, session_id)
    stamp = state["last_verification_write_at"]
    run_dir = target / ".brigade" / "work" / "verify-runs" / f"run-{session_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": f"run-{session_id}",
        "status": "completed",
        "started_at": stamp,
        "completed_at": stamp,
        "target": str(target.resolve()),
        "harness_session": {
            "harness": "claude",
            "fingerprint": state["session_fingerprint"],
        },
    }
    if tree_fingerprint is not None:
        payload["tree_fingerprint"] = tree_fingerprint
    (run_dir / "receipt.json").write_text(json.dumps(payload) + "\n")
    return state


def test_stop_does_not_block_when_concurrent_session_dirties_audited_tree(tmp_path: Path, monkeypatch):
    """Issue #959: session B's receipt must still close out after session A writes."""
    target = _git_wired_claude(tmp_path)
    (target / "owned.py").write_text("owned = 1\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True, text=True)

    reader = "session-b"
    writer = "session-a"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned = 2\n"},
        ),
    )
    receipt_tree = localio.tree_fingerprint(target)
    assert receipt_tree is not None
    _write_session_receipt(target, reader, tree_fingerprint=receipt_tree)

    (target / "foreign.py").write_text("foreign\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=writer,
            tool_name="Write",
            tool_input={"file_path": str(target / "foreign.py"), "content": "foreign\n"},
        ),
    )
    assert localio.tree_fingerprint(target) != receipt_tree

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is None or result.get("decision") != "block"


def test_stop_does_not_rearm_after_read_only_bash_during_foreign_writes(tmp_path: Path, monkeypatch):
    """Issue #959: another session's writes must not bump this session's last_write_at."""
    target = _wired_claude(tmp_path)
    reader = "session-b-bash"
    writer = "session-a-loop"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    state = _write_session_receipt(target, reader)
    write_at = state["last_write_at"]

    pretool = _payload(
        target,
        "PreToolUse",
        session_id=reader,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None

    changed = target / "foreign-loop.py"
    changed.write_text("pass 1\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=writer,
            tool_name="Write",
            tool_input={"file_path": str(changed), "content": "pass 1\n"},
        ),
    )
    changed.write_text("pass 2\n")

    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    updated = runtime.read_session_state(target, reader)
    assert updated["last_write_at"] == write_at
    assert updated["last_verification_write_at"] == state["last_verification_write_at"]

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is None or result.get("decision") != "block"


def test_stop_ignores_other_harness_receipt_as_this_session_write(tmp_path: Path, monkeypatch):
    """Issue #959: a Codex/other-harness capture must not look like this session's write."""
    target = _wired_claude(tmp_path)
    reader = "claude-hub-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    state = _write_session_receipt(target, reader)
    write_at = state["last_write_at"]

    pretool = _payload(
        target,
        "PreToolUse",
        session_id=reader,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None

    (target / "codex.py").write_text("from the other agent\n")
    other_dir = target / ".brigade" / "work" / "verify-runs" / "codex-run"
    other_dir.mkdir(parents=True)
    (other_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "codex-run",
                "status": "completed",
                "started_at": localio.utc_now_iso(),
                "completed_at": localio.utc_now_iso(),
                "target": str(target.resolve()),
                "harness_session": {"harness": "codex", "fingerprint": "codex-other"},
            }
        )
        + "\n"
    )

    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None
    updated = runtime.read_session_state(target, reader)
    assert updated["last_write_at"] == write_at

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is None or result.get("decision") != "block"


def test_stop_warns_when_foreign_write_outruns_this_session_receipt(tmp_path: Path, monkeypatch):
    """Issue #959: a threshold another process can raise must not hard-block closeout."""
    target = _wired_claude(tmp_path)
    reader = "blocked-session"
    writer = "busy-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    _write_session_receipt(target, reader)

    later = localio.utc_now().isoformat()
    state = runtime.read_session_state(target, reader)
    state["last_write_at"] = later
    state["last_verification_write_at"] = later
    runtime.write_session_state(target, reader, state)

    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=writer,
            tool_name="Write",
            tool_input={"file_path": str(target / "foreign.py"), "content": "foreign\n"},
        ),
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is not None
    assert result.get("decision") != "block"
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "concurrent session" in context
    assert "not blocked" in context


def test_stop_still_blocks_without_receipt_when_concurrent_session_is_active(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    reader = "never-captured"
    writer = "other-writer"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=writer,
            tool_name="Write",
            tool_input={"file_path": str(target / "foreign.py"), "content": "foreign\n"},
        ),
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is not None
    assert result["decision"] == "block"


def test_stop_still_blocks_when_this_session_writes_after_its_receipt(tmp_path: Path, monkeypatch):
    """Busy-hub order: this session writes after capture, then a neighbor writes.

    The prior order (foreign write, then own write) hid the Stop downgrade:
    ``_foreign_write_since`` was evaluated against this session's later
    ``last_write_at``, so the neighbor's earlier write did not count.
    """
    target = _wired_claude(tmp_path)
    reader = "wrote-again"
    writer = "background-writer"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    _write_session_receipt(target, reader)
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned again\n"},
        ),
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=writer,
            tool_name="Write",
            tool_input={"file_path": str(target / "foreign.py"), "content": "foreign\n"},
        ),
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is not None
    assert result["decision"] == "block"


def test_stop_still_blocks_own_unverified_write_when_neighbor_only_reads(tmp_path: Path, monkeypatch):
    """Own Write after capture still hard-blocks when a neighbor only reads."""
    target = _wired_claude(tmp_path)
    reader = "wrote-again-read-neighbor"
    neighbor = "read-only-neighbor"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    _write_session_receipt(target, reader)
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned again\n"},
        ),
    )
    assert (
        runtime.handle_payload(
            "PreToolUse",
            _payload(
                target,
                "PreToolUse",
                session_id=neighbor,
                tool_name="Bash",
                tool_input={"command": "git log -1"},
            ),
        )
        is None
    )
    neighbor_state = runtime.read_session_state(target, neighbor)
    assert neighbor_state["write_observed"] is False
    assert "last_write_at" not in neighbor_state

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is not None
    assert result["decision"] == "block"


def test_stop_still_blocks_own_unverified_write_when_other_session_receipt_only(tmp_path: Path, monkeypatch):
    """Own Write after capture still hard-blocks when another session only captures."""
    target = _wired_claude(tmp_path)
    reader = "wrote-again-receipt-neighbor"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned\n"},
        ),
    )
    _write_session_receipt(target, reader)
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=reader,
            tool_name="Write",
            tool_input={"file_path": str(target / "owned.py"), "content": "owned again\n"},
        ),
    )
    other_dir = target / ".brigade" / "work" / "verify-runs" / "other-session-only"
    other_dir.mkdir(parents=True)
    (other_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "other-session-only",
                "status": "completed",
                "started_at": localio.utc_now_iso(),
                "completed_at": localio.utc_now_iso(),
                "target": str(target.resolve()),
                "harness_session": {"harness": "claude", "fingerprint": "other-session-fp"},
            }
        )
        + "\n"
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader, stop_hook_active=False))
    assert result is not None
    assert result["decision"] == "block"


def test_receipt_since_accepts_tree_drift_when_foreign_session_wrote(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True, text=True)
    (target / "source.py").write_text("value = 2\n")

    session_id = "audited-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    fingerprint = runtime._session_fingerprint(session_id)
    receipt_tree = localio.tree_fingerprint(target)
    threshold = localio.utc_now() - timedelta(seconds=10)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "matching"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "matching",
                "target": str(target.resolve()),
                "status": "completed",
                "started_at": (threshold + timedelta(seconds=1)).isoformat(),
                "completed_at": (threshold + timedelta(seconds=2)).isoformat(),
                "tree_fingerprint": receipt_tree,
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )

    (target / "foreign.py").write_text("other session\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id="foreign-writer",
            tool_name="Write",
            tool_input={"file_path": str(target / "foreign.py"), "content": "other session\n"},
        ),
    )
    assert localio.tree_fingerprint(target) != receipt_tree
    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is True


def test_posttooluse_snapshot_fails_closed_when_state_cannot_be_inspected(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    session_id = "snapshot-unavailable"
    monkeypatch.setattr(runtime, "repo_worktree_fingerprint", lambda repo: None)
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, session_id).get("pending_bash_fingerprint") == "unavailable"

    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is True
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_posttooluse_fails_closed_when_post_command_snapshot_is_unavailable(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    session_id = "post-snapshot-unavailable"
    post_command = False

    def sequenced_fingerprint(repo: Path) -> str | None:
        return None if post_command else "baseline"

    monkeypatch.setattr(runtime, "repo_worktree_fingerprint", sequenced_fingerprint)
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": f"{sys.executable} -c \"print('noop')\""},
    )

    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, session_id)["pending_bash_fingerprint"] == "baseline"
    post_command = True
    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None

    assert runtime.read_session_state(target, session_id)["write_observed"] is True
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


@pytest.mark.parametrize("command", ["gh --version", "jq --version", "rg --version"])
def test_posttooluse_unlisted_read_only_command_does_not_observe_write(tmp_path: Path, command: str):
    target = _git_wired_claude(tmp_path)
    session_id = f"read-only-{command.split()[0]}"
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": command},
    )

    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"}) is None

    state = runtime.read_session_state(target, session_id)
    assert state["write_observed"] is False
    assert "pending_bash_fingerprint" not in state
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False)) is None
    )


def test_final_bash_handoff_write_does_not_require_verification_again(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    session_id = "bash-handoff-last"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": state["last_verification_write_at"],
                "completed_at": state["last_verification_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    handoff = target / ".claude" / "memory-handoffs" / "handoff.md"
    pre = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": "printf '%s\\n' finding >> .claude/memory-handoffs/handoff.md"},
    )
    assert runtime.handle_payload("PreToolUse", pre) is None
    handoff.write_text("durable finding\n")
    runtime.handle_payload("PostToolUse", {**pre, "hook_event_name": "PostToolUse"})

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_write_at"] >= updated["last_verification_write_at"]
    assert updated["last_verification_write_at"] == state["last_verification_write_at"]
    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert result is None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/old/new/' src/app.py && printf done >> .claude/memory-handoffs/note.md",
        "sed -i 's/old/new/' src/app.py > .claude/memory-handoffs/note.md",
        "ruff format src/app.py > .claude/memory-handoffs/note.md",
        "python fix.py > .claude/memory-handoffs/note.md",
        "git commit -am fix > .claude/memory-handoffs/note.md",
        "mv src/app.py .claude/memory-handoffs/app.md",
        "truncate -s 0 src/app.py .claude/memory-handoffs/note.md",
        "cp -t src .claude/memory-handoffs/note.md",
        "cp --target-directory src .claude/memory-handoffs/note.md",
        "install --target-directory=src .claude/memory-handoffs/note.md",
        "install -d src .claude/memory-handoffs/note",
        "install --directory src .claude/memory-handoffs/note",
        "mv -tsrc .claude/memory-handoffs/note.md",
        "mv --target-directory=src .claude/memory-handoffs/note.md",
        "install -dm755 src .claude/memory-handoffs/note",
    ],
)
def test_mixed_bash_code_and_handoff_write_requires_new_verification(tmp_path: Path, monkeypatch, command: str):
    target = _wired_claude(tmp_path)
    session_id = "mixed-bash-handoff"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": state["last_verification_write_at"],
                "completed_at": state["last_verification_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": command},
        ),
    )

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_verification_write_at"] > state["last_verification_write_at"]
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_stop_ignores_postcapture_brigade_run_artifact_writes(tmp_path: Path, monkeypatch):
    """Issue #483: detached brigade run artifacts under .brigade/runs/ must not re-arm closeout."""
    target = _wired_claude(tmp_path)
    session_id = "brigade-runs-artifact"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": state["last_verification_write_at"],
                "completed_at": state["last_verification_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    artifact = Path(".brigade", "runs", "detached-1", "run.json")
    pre = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": f"mkdir -p {artifact.parent.as_posix()} && touch {artifact.as_posix()}"},
    )
    assert runtime.handle_payload("PreToolUse", pre) is None
    (target / artifact).parent.mkdir(parents=True)
    (target / artifact).write_text("{}\n")
    runtime.handle_payload("PostToolUse", {**pre, "hook_event_name": "PostToolUse"})

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_verification_write_at"] == state["last_verification_write_at"]
    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert "decision" not in result


def test_stop_still_blocks_after_source_edit_post_capture(tmp_path: Path, monkeypatch):
    """Issue #483: real source edits after capture must still trip the closeout gate."""
    target = _wired_claude(tmp_path)
    session_id = "source-edit-post-capture"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": state["last_verification_write_at"],
                "completed_at": state["last_verification_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "src.py"), "content": "changed\n"},
        ),
    )

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_verification_write_at"] > state["last_verification_write_at"]
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_posttooluse_brigade_run_does_not_record_write(tmp_path: Path, monkeypatch):
    """Issue #483: brigade run bash must not bump verification write timestamps."""
    target = _wired_claude(tmp_path)
    session_id = "brigade-run-bash"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": "brigade run --detach -- echo noop"},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert "pending_bash_fingerprint" in runtime.read_session_state(target, session_id)

    runtime.handle_payload("PostToolUse", {**pretool, "hook_event_name": "PostToolUse"})

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_verification_write_at"] == state["last_verification_write_at"]
    assert "pending_bash_fingerprint" not in updated


def test_repo_worktree_fingerprint_detects_dirty_tracked_same_size_rewrite(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    tracked = target / "tracked.txt"
    tracked.write_text("version-a\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=target, check=True, capture_output=True, text=True)
    tracked.write_text("version-b\n")
    assert len("version-a\n") == len("version-b\n")
    status_before = subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain", "-u", "--no-renames"],
        text=True,
    )
    baseline = runtime.repo_worktree_fingerprint(target)
    tracked.write_text("version-c\n")
    assert len("version-b\n") == len("version-c\n")
    status_after = subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain", "-u", "--no-renames"],
        text=True,
    )
    assert status_before == status_after
    updated = runtime.repo_worktree_fingerprint(target)
    assert baseline is not None
    assert updated is not None
    assert baseline != updated


def test_repo_worktree_fingerprint_detects_untracked_same_size_rewrite(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    untracked = target / "new.txt"
    untracked.write_text("aaaa")
    status_before = subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain", "-u", "--no-renames"],
        text=True,
    )
    baseline = runtime.repo_worktree_fingerprint(target)
    untracked.write_text("bbbb")
    status_after = subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain", "-u", "--no-renames"],
        text=True,
    )
    assert status_before == status_after
    updated = runtime.repo_worktree_fingerprint(target)
    assert baseline is not None
    assert updated is not None
    assert baseline != updated


def test_repo_worktree_fingerprint_detects_untracked_tail_byte_change(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    untracked = target / "large.bin"
    content_a = b"a" * 65536 + b"x"
    content_b = b"a" * 65536 + b"y"
    assert len(content_a) == 65537 == len(content_b)
    untracked.write_bytes(content_a)
    baseline = runtime.repo_worktree_fingerprint(target)
    untracked.write_bytes(content_b)
    updated = runtime.repo_worktree_fingerprint(target)
    assert baseline is not None
    assert updated is not None
    assert baseline != updated


def test_repo_worktree_fingerprint_signs_large_untracked_without_reading_it(tmp_path: Path, monkeypatch):
    # The stat-only path (POSIX). Windows also signs content; see the
    # fingerprint tests.
    monkeypatch.setattr(hook_fingerprint, "_SIGN_CONTENT", False)
    target = _git_wired_claude(tmp_path)
    large = target / "model.cache"
    with large.open("wb") as handle:
        handle.seek(100 * 1024 * 1024 - 1)
        handle.write(b"\0")
    git_calls: list[tuple[str, ...]] = []
    real_run = hook_fingerprint._run_snapshot_git

    def tracked_run(repo: Path, *git_args: str):
        git_calls.append(git_args)
        return real_run(repo, *git_args)

    def forbid_read(self: Path, *args, **kwargs):
        raise AssertionError("repo_worktree_fingerprint must not read untracked file content")

    monkeypatch.setattr(hook_fingerprint, "_run_snapshot_git", tracked_run)
    monkeypatch.setattr(Path, "read_bytes", forbid_read)
    monkeypatch.setattr(Path, "open", forbid_read)

    fingerprint = runtime.repo_worktree_fingerprint(target)
    lines = hook_fingerprint._git_worktree_fingerprint_lines(target)

    assert fingerprint is not None
    assert lines is not None
    assert any(line.startswith("untracked\tmodel.cache\tstat:104857600:") for line in lines)
    assert not any("model.cache" in arg for args in git_calls for arg in args)


def test_wired_target_from_payload_ignores_incidental_repo_paths(tmp_path: Path):
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    incidental = _configured_git_repo(tmp_path / "incidental")
    command = f"rg pattern {incidental}/tracked.txt"

    resolved = runtime.wired_target_from_payload(
        _payload(
            cwd_repo,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": command},
        )
    )

    assert resolved == cwd_repo.resolve()


def test_wired_target_from_payload_without_cwd_uses_named_repo_not_process_dir(tmp_path: Path):
    named = _configured_git_repo(tmp_path / "named")
    # Payload omits cwd; the command explicitly names a wired repo.
    payload = {
        "session_id": "no-cwd",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": f"rg pattern {named}/tracked.txt"},
    }

    resolved = runtime.wired_target_from_payload(payload)

    assert resolved == named.resolve()


def test_wired_target_from_payload_without_cwd_and_no_named_repo_returns_none(tmp_path: Path):
    # Payload omits cwd and the command mentions no wired repo path.
    payload = {
        "session_id": "no-cwd",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
    }

    resolved = runtime.wired_target_from_payload(payload)

    assert resolved is None


def _fail_untracked_lstat(monkeypatch, name: str) -> None:
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path).name == name:
            raise PermissionError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(hook_fingerprint.os, "lstat", lstat)


def test_repo_worktree_fingerprint_returns_none_when_untracked_lstat_fails(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "new.txt").write_text("content")
    _fail_untracked_lstat(monkeypatch, "new.txt")
    assert runtime.repo_worktree_fingerprint(target) is None


def test_posttooluse_fails_closed_when_untracked_state_check_fails(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    session_id = "untracked-lstat-fail"
    out_file = target / "new.txt"
    out_file.write_text("before")
    _fail_untracked_lstat(monkeypatch, "new.txt")
    command = f"{sys.executable} -c \"from pathlib import Path; Path({str(out_file)!r}).write_text('after')\""
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": command},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    state = runtime.read_session_state(target, session_id)
    assert state["write_observed"] is False
    assert state["pending_bash_fingerprint"] == "unavailable"

    out_file.write_text("after")
    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is True
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_posttooluse_records_bash_write_on_dirty_tracked_same_size_rewrite(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    session_id = "dirty-tracked-rewrite"
    tracked = target / "tracked.txt"
    tracked.write_text("version-a\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=target, check=True, capture_output=True, text=True)
    tracked.write_text("version-b\n")
    command = f"{sys.executable} -c \"from pathlib import Path; Path({str(tracked)!r}).write_text('version-c\\\\n')\""
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": command},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is False

    tracked.write_text("version-c\n")
    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is True


def test_posttooluse_records_only_successful_writes(tmp_path: Path):
    target = _wired_claude(tmp_path)
    pretool = _payload(
        target,
        "PreToolUse",
        session_id="write",
        tool_name="Write",
        tool_input={"file_path": str(target / "file.py")},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, "write")["write_observed"] is False
    assert runtime.handle_payload("Stop", _payload(target, "Stop", session_id="write")) is None

    posttool = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", posttool) is None
    assert runtime.read_session_state(target, "write")["write_observed"] is True


def test_cli_accepts_managed_posttooluse_event(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_hook_run(*, event: str, package: str) -> int:
        calls.append((event, package))
        return 0

    monkeypatch.setattr(runtime, "hook_run", fake_hook_run)

    assert cli.main(["work", "hook-run", "--event", "PostToolUse", "--package", PACKAGE_REF]) == 0
    assert calls == [("PostToolUse", PACKAGE_REF)]


def test_posttooluse_records_only_successful_confident_bash_writes(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    command = "sed -i 's/old/new/' file.py"
    pretool = _payload(
        target,
        "PreToolUse",
        session_id="bash-write",
        tool_name="Bash",
        tool_input={"command": command},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, "bash-write")["write_observed"] is False

    failed = {**pretool, "hook_event_name": "PostToolUseFailure"}
    assert runtime.handle_payload("PostToolUseFailure", failed) is None
    assert runtime.read_session_state(target, "bash-write")["write_observed"] is False

    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, "bash-write")["write_observed"] is True


@pytest.mark.parametrize(
    "command,write,touches",
    [
        ("gh pr checks 1 2>&1 | tail -3", False, False),
        ("ls >&2", False, False),
        ("ls 2>/dev/null", False, False),
        ("echo x > /tmp/outside.log", True, False),
        ("echo x > notes.md", True, True),
        ("echo x >> notes.md", True, True),
        ("ls &>out.txt", True, True),
        ("ls >& out.txt", True, True),
        ("ls >", True, True),
    ],
)
def test_confident_bash_write_reads_redirect_targets(tmp_path: Path, command, write, touches):
    target = _wired_claude(tmp_path)
    assert runtime._is_confident_bash_write(command) is write
    assert (write and runtime._bash_confident_write_touches_worktree(target, command)) is touches


def test_read_only_command_with_stderr_merge_does_not_arm_stop(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="merge"))
    tool = {"command": "git status 2>&1 | head -3"}
    for event in ("PreToolUse", "PostToolUse"):
        runtime.handle_payload(event, _payload(target, event, session_id="merge", tool_name="Bash", tool_input=tool))
    assert runtime.read_session_state(target, "merge")["write_observed"] is False
    assert runtime.handle_payload("Stop", _payload(target, "Stop", session_id="merge", stop_hook_active=False)) is None


def test_stop_does_not_block_read_only_or_repeated_stop(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="read-only"))
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id="read-only", stop_hook_active=False)) is None
    )

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="write"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id="write",
            tool_name="Edit",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="write", stop_hook_active=False))
    assert blocked["decision"] == "block"
    assert "brigade work verify run" in blocked["reason"]
    assert runtime.handle_payload("Stop", _payload(target, "Stop", session_id="write", stop_hook_active=True)) is None


def test_stop_quotes_target_in_replacement_guidance(tmp_path: Path, monkeypatch):
    target = tmp_path / "repo with spaces"
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(target, selection) == 0
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="write"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id="write",
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="write", stop_hook_active=False))

    state = runtime.read_session_state(target, "write")
    assert f"BRIGADE_CLAUDE_SESSION={state['session_fingerprint']}" in result["reason"]
    assert f"--target {shlex.quote(str(target.resolve()))}" in result["reason"]


def test_stop_rejects_failed_or_rejected_routed_receipt(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="receipt"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id="receipt",
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, "receipt")
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "rejected",
                "started_at": state["last_write_at"],
                "completed_at": state["last_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="receipt", stop_hook_active=False))
    assert result["decision"] == "block"


def test_final_handoff_write_does_not_require_verification_again(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    session_id = "handoff-last"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": state["last_write_at"],
                "completed_at": state["last_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    handoff = target / ".claude" / "memory-handoffs" / "handoff.md"
    handoff.write_text("durable finding\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(handoff)},
        ),
    )

    updated = runtime.read_session_state(target, session_id)
    assert updated["last_write_at"] >= updated["last_verification_write_at"]
    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert result is None


@pytest.mark.parametrize("wrapper", ["tokenjuice", "token-glace"])
def test_stop_accepts_session_receipt_after_wrapped_verify_posttooluse(tmp_path: Path, monkeypatch, wrapper: str):
    target = _wired_claude(tmp_path)
    session_id = f"wrapped-verify-{wrapper}"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state = runtime.read_session_state(target, session_id)
    original = 'brigade work verify run --target . --command "true" --capture brigade-work'
    pretool = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": original},
    )
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert runtime.read_session_state(target, session_id)["pending_bash_fingerprint"]

    run_dir = target / ".brigade" / "work" / "verify-runs" / "wrapped-run"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "wrapped-run",
                "status": "completed",
                "started_at": state["last_write_at"],
                "completed_at": state["last_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )

    wrapped = f"{wrapper} wrap --source claude-code -- /bin/bash -lc {shlex.quote(original)}"
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": wrapped},
        ),
    )
    posttool_state = runtime.read_session_state(target, session_id)
    assert posttool_state["last_write_at"] == state["last_write_at"]
    assert "pending_bash_fingerprint" not in posttool_state

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert "decision" not in result
    assert "Memory Handoff" in result["hookSpecificOutput"]["additionalContext"]


def test_stop_rejects_receipt_created_before_later_write(tmp_path: Path):
    target = _wired_claude(tmp_path)
    now = localio.utc_now()
    session_id = "write-after-receipt"
    fingerprint = runtime._session_fingerprint(session_id)
    runtime.write_session_state(
        target,
        session_id,
        {
            "session_id": session_id,
            "session_fingerprint": fingerprint,
            "target": str(target.resolve()),
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "last_write_at": (now - timedelta(minutes=30)).isoformat(),
            "briefed": True,
            "write_observed": True,
            "verify_denied_count": 0,
        },
    )
    run_dir = target / ".brigade" / "work" / "verify-runs" / "before-write"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "started_at": (now - timedelta(hours=1)).isoformat(),
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))

    assert result["decision"] == "block"


def test_receipt_since_requires_exact_completed_audit_boundary(tmp_path: Path):
    target = _git_wired_claude(tmp_path)
    (target / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True, text=True)
    (target / "source.py").write_text("value = 2\n")

    threshold = localio.utc_now()
    fingerprint = runtime._session_fingerprint("audited-session")
    receipt_tree = localio.tree_fingerprint(target)
    assert receipt_tree is not None
    root = target / ".brigade" / "work" / "verify-runs"

    def write_receipt(run_id: str, **overrides: object) -> None:
        run_dir = root / run_id
        run_dir.mkdir(parents=True)
        payload = {
            "run_id": run_id,
            "target": str(target.resolve()),
            "status": "completed",
            "started_at": (threshold + timedelta(seconds=1)).isoformat(),
            "completed_at": (threshold + timedelta(seconds=2)).isoformat(),
            "tree_fingerprint": receipt_tree,
            "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            **overrides,
        }
        (run_dir / "receipt.json").write_text(json.dumps(payload) + "\n")

    invalid_cases = [
        {"completed_at": None},
        {"completed_at": "not-a-timestamp"},
        {"status": "running"},
        {"status": "failed"},
        {"status": "rejected"},
        {"started_at": (threshold - timedelta(seconds=1)).isoformat()},
        {"target": str(tmp_path / "other")},
        {"tree_fingerprint": "0" * 40},
        {"harness_session": {"harness": "claude", "fingerprint": runtime._session_fingerprint("other")}},
    ]
    for index, overrides in enumerate(invalid_cases):
        write_receipt(f"invalid-{index}", **overrides)

    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is False

    write_receipt("matching")

    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is True


def _tracked_verify_capture_boundary(tmp_path: Path) -> tuple[Path, Path, datetime, str]:
    target = _git_wired_claude(tmp_path)
    source = target / "source.py"
    receipt_path = target / ".brigade" / "work" / "verify-runs" / "captured" / "receipt.json"
    outcome_records = target / "memory" / "outcome" / "records.jsonl"
    outcome_lock = outcome_records.parent / ".records.lock"
    miseledger_cursor = target / ".brigade" / "work" / "miseledger-export-cursor.json"
    miseledger_export = target / ".brigade" / "work" / "miseledger-export-captured.jsonl"

    source.write_text("value = 1\n")
    for path in (receipt_path, outcome_records, outcome_lock, miseledger_cursor, miseledger_export):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("baseline\n")
    subprocess.run(
        [
            "git",
            "add",
            "-f",
            "source.py",
            ".brigade/work/verify-runs/captured/receipt.json",
            "memory/outcome/records.jsonl",
            "memory/outcome/.records.lock",
            ".brigade/work/miseledger-export-cursor.json",
            ".brigade/work/miseledger-export-captured.jsonl",
        ],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True, text=True)

    source.write_text("value = 2\n")
    threshold = localio.utc_now()
    fingerprint = runtime._session_fingerprint("captured-session")
    receipt_tree = localio.tree_fingerprint(target)
    assert receipt_tree is not None
    receipt_path.write_text(
        json.dumps(
            {
                "run_id": "captured",
                "target": str(target.resolve()),
                "status": "completed",
                "started_at": (threshold + timedelta(seconds=1)).isoformat(),
                "completed_at": (threshold + timedelta(seconds=2)).isoformat(),
                "tree_fingerprint": receipt_tree,
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )
    outcome_records.write_text('{"signal_value": 1}\n')
    outcome_lock.write_text("lock\n")
    miseledger_cursor.write_text('{"raw_hashes": ["captured"]}\n')
    miseledger_export.write_text('{"receipt": "captured"}\n')

    return target, source, threshold, fingerprint


def test_receipt_since_ignores_verify_and_outcome_capture_artifacts(tmp_path: Path):
    target, _, threshold, fingerprint = _tracked_verify_capture_boundary(tmp_path)

    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is True


def test_receipt_since_rejects_source_edit_after_outcome_capture(tmp_path: Path):
    target, source, threshold, fingerprint = _tracked_verify_capture_boundary(tmp_path)
    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is True

    source.write_text("value = 3\n")

    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is False


def test_receipt_since_fails_open_when_tree_snapshot_is_unavailable(tmp_path: Path):
    target = _wired_claude(tmp_path)
    threshold = localio.utc_now()
    fingerprint = runtime._session_fingerprint("non-git-session")
    run_dir = target / ".brigade" / "work" / "verify-runs" / "matching"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "target": str(target.resolve()),
                "status": "completed",
                "started_at": (threshold + timedelta(seconds=1)).isoformat(),
                "completed_at": (threshold + timedelta(seconds=2)).isoformat(),
                "tree_fingerprint": None,
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )

    assert runtime._receipt_since(target, threshold.isoformat(), session_fingerprint=fingerprint) is True


def test_hook_run_normalizes_malformed_persisted_state_before_denial(tmp_path: Path, capsys):
    target = _wired_claude(tmp_path)
    session_id = "malformed"
    runtime.write_session_state(
        target,
        session_id,
        {
            "session_id": session_id,
            "target": str(target.resolve()),
            "started_at": localio.utc_now_iso(),
            "briefed": True,
            "write_observed": False,
            "session_fingerprint": {"invalid": True},
            "verify_denied_count": "not-an-integer",
        },
    )
    payload = _payload(
        target,
        "PreToolUse",
        session_id=session_id,
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
    )
    capsys.readouterr()

    assert (
        runtime.hook_run(
            event="PreToolUse",
            package=PACKAGE_REF,
            stdin_text=json.dumps(payload),
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    state = runtime.read_session_state(target, session_id)
    assert state["session_fingerprint"] == runtime._session_fingerprint(session_id)
    assert state["verify_denied_count"] == 1


def test_stop_persists_normalized_future_timestamps(tmp_path: Path):
    target = _wired_claude(tmp_path)
    session_id = "future-state"
    fingerprint = runtime._session_fingerprint(session_id)
    future = localio.utc_now() + timedelta(days=1)
    runtime.write_session_state(
        target,
        session_id,
        {
            "session_id": session_id,
            "session_fingerprint": fingerprint,
            "target": str(target.resolve()),
            "started_at": future.isoformat(),
            "last_write_at": future.isoformat(),
            "briefed": True,
            "write_observed": True,
            "verify_denied_count": 0,
        },
    )

    first = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert first["decision"] == "block"
    normalized = runtime.read_session_state(target, session_id)
    assert localio.parse_iso_datetime(normalized["started_at"]) <= localio.utc_now()
    assert "last_write_at" not in normalized

    run_dir = target / ".brigade" / "work" / "verify-runs" / "after-normalize"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "started_at": localio.utc_now_iso(),
                "completed_at": localio.utc_now_iso(),
                "target": str(target.resolve()),
                "harness_session": {"harness": "claude", "fingerprint": fingerprint},
            }
        )
        + "\n"
    )

    second = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert "decision" not in second
    assert "Memory Handoff" in second["hookSpecificOutput"]["additionalContext"]


def test_stop_does_not_accept_another_claude_sessions_receipt(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")
    for session_id in ("writer-a", "writer-b"):
        runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id="writer-a",
            tool_name="Edit",
            tool_input={"file_path": str(target / "file.py")},
        ),
    )
    state_a = runtime.read_session_state(target, "writer-a")
    state_b = runtime.read_session_state(target, "writer-b")
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-b"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-b",
                "status": "completed",
                "started_at": state_a["started_at"],
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state_b["session_fingerprint"],
                },
            }
        )
        + "\n"
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="writer-a", stop_hook_active=False))
    assert result["decision"] == "block"


def test_hook_run_rejects_stale_package_without_output(capsys):
    payload = {"session_id": "x", "cwd": ".", "hook_event_name": "SessionStart"}
    assert (
        runtime.hook_run(event="SessionStart", package="brigade-claude-work-loop@0.0.1", stdin_text=json.dumps(payload))
        == 0
    )
    assert capsys.readouterr().out == ""


def test_hook_run_process_exits_within_timeout_when_operation_hangs(tmp_path: Path):
    target = _wired_claude(tmp_path)
    payload = _payload(
        target,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": "pytest -q"},
    )
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(repo_root / "src"), env.get("PYTHONPATH"))))

    def run_hook(hang_seconds: str, hook_timeout: str = "0.25") -> tuple[subprocess.CompletedProcess[str], float]:
        started = time.monotonic()
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "brigade",
                "work",
                "hook-run",
                "--event",
                "PreToolUse",
                "--package",
                PACKAGE_REF,
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=target,
            env={**env, runtime._HOOK_TEST_HANG_ENV: hang_seconds, runtime._HOOK_TIMEOUT_ENV: hook_timeout},
            timeout=15,
            check=False,
        )
        return completed, time.monotonic() - started

    # Interpreter startup and teardown vary with runner load; a no-hang run of
    # the same command measures them so the bound covers only the timeout path.
    # The baseline gets a long budget: on a loaded runner the hook's own work
    # can pass 0.25 s, and a second timeout would latch the hook off
    # (HOOK_TIMEOUT_LATCH_AFTER) and change the envelope under test.
    baseline, baseline_elapsed = run_hook("0", hook_timeout="10")
    assert baseline.returncode == 0
    log = envelope.log_path(target)
    logged_before = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "timed out" not in logged_before
    completed, elapsed = run_hook("30")

    assert completed.returncode == 0
    assert elapsed - baseline_elapsed < 1.0
    assert json.loads(completed.stdout) == envelope.degraded_envelope("PreToolUse")
    assert "timed out" in log.read_text(encoding="utf-8")[len(logged_before) :]
    assert "resource_tracker" not in completed.stderr


def test_posttool_failure_keeps_routed_failure_in_the_loop(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PostToolUseFailure",
        _payload(
            target,
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={"command": 'brigade work verify run --target . --command "pytest" --capture brigade-work'},
            error="exit 1",
        ),
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "failed or rejected verification" in context
    assert "before retrying" in context


def _configured_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    selection = Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])
    assert install_selection(path, selection) == 0
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "tracked.txt").write_text("before\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "test baseline"], cwd=path, check=True, capture_output=True, text=True)
    return path


def test_wrapped_verify_credits_target_repo_not_cwd_repo(tmp_path: Path, monkeypatch):
    target = _configured_git_repo(tmp_path / "target")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "wrapped-cross-repo"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "tracked.txt"), "content": "after\n"},
        ),
    )
    state = runtime.read_session_state(target, session_id)

    inner = f"cd {target} && brigade work verify run --target . --command 'true' --capture brigade-work 2>&1 | tail -3"
    wrapped = "tokenjuice wrap --source claude-code --min-reduce-chars 2000 -- /bin/bash -lc " + shlex.quote(inner)
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": wrapped},
            )
        )
        == target.resolve()
    )

    other = _configured_git_repo(tmp_path / "other")
    combined = f"cd {target} && echo one 2>&1 | tail -1; cd {other} && echo two 2>&1 | tail -1"
    combined_wrapped = "tokenjuice wrap --source claude-code --min-reduce-chars 2000 -- /bin/bash -lc " + shlex.quote(
        combined
    )
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": combined_wrapped},
            )
        )
        == target.resolve()
    )

    run_dir = target / ".brigade" / "work" / "verify-runs" / "wrapped-run"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "wrapped-run",
                "status": "completed",
                "started_at": state["last_write_at"],
                "completed_at": state["last_write_at"],
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            cwd_repo,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": wrapped},
        ),
    )
    handoff = target / ".claude" / "memory-handoffs" / "note.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("note")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(handoff)},
        ),
    )

    stopped = runtime.handle_payload("Stop", _payload(cwd_repo, "Stop", session_id=session_id, stop_hook_active=False))
    assert stopped is None


def test_heredoc_bodies_are_data_not_commands(tmp_path: Path):
    repo = _wired_claude(tmp_path / "repo")
    other = _wired_claude(tmp_path / "other")
    prose = (
        f"cd {repo} && cat > notes.md <<'EOF'\n"
        "Run checks through `brigade work verify run` with atomic capture.\n"
        f"Earlier this failed in {other} after a bare pytest run.\n"
        "EOF\n"
        "echo done"
    )
    assert not runtime.is_raw_verification(prose)
    assert (
        runtime.wired_target_from_payload(
            _payload(tmp_path, "PreToolUse", tool_name="Bash", tool_input={"command": prose})
        )
        == repo.resolve()
    )
    assert runtime._is_confident_bash_write(prose)

    captured = (
        f"cd {repo} && brigade work verify run --target . "
        "--command 'true' --capture brigade-work <<'EOF'\n"
        "unrelated document text\n"
        "EOF"
    )
    assert not runtime.is_raw_verification(captured)
    pretool = runtime.handle_payload(
        "PreToolUse",
        _payload(repo, "PreToolUse", tool_name="Bash", tool_input={"command": captured}),
    )
    assert pretool is None


def test_stop_skips_repos_that_no_longer_exist(tmp_path: Path, monkeypatch):
    import shutil

    target = _configured_git_repo(tmp_path / "target")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "vanished-repo"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "tracked.txt"), "content": "after\n"},
        ),
    )
    runtime.handle_payload("SessionStart", _payload(cwd_repo, "SessionStart", session_id=session_id, cwd=str(cwd_repo)))
    shutil.rmtree(target)

    stopped = runtime.handle_payload("Stop", _payload(cwd_repo, "Stop", session_id=session_id, stop_hook_active=False))
    assert stopped is None


def test_stop_blocks_linked_mutated_repo_without_receipt(tmp_path: Path, monkeypatch):
    target = _configured_git_repo(tmp_path / "target")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "linked-mutated-stop"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "tracked.txt"), "content": "after\n"},
        ),
    )
    runtime.handle_payload("SessionStart", _payload(cwd_repo, "SessionStart", session_id=session_id, cwd=str(cwd_repo)))

    runtime.handle_payload(
        "PostToolUse",
        _payload(
            cwd_repo,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": f"cd {target} && pwd"},
        ),
    )

    blocked = runtime.handle_payload("Stop", _payload(cwd_repo, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert str(target.resolve()) in blocked["reason"]


def test_wrapped_verify_uses_effective_cwd_not_first_cd(tmp_path: Path):
    first = _configured_git_repo(tmp_path / "first")
    actual = _configured_git_repo(tmp_path / "actual")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "effective-cwd"

    inner = f"cd {first} && cd {actual} && brigade work verify run --target . --command 'true' --capture brigade-work"
    wrapped = "tokenjuice wrap --source claude-code --min-reduce-chars 2000 -- /bin/bash -lc " + shlex.quote(inner)
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": wrapped},
            )
        )
        == actual.resolve()
    )

    nested_inner = f"cd {first} && /bin/bash -lc {shlex.quote(f'cd {actual} && brigade work verify run --target . --command true --capture brigade-work')}"
    nested_wrapped = "tokenjuice wrap --source claude-code --min-reduce-chars 2000 -- /bin/bash -lc " + shlex.quote(
        nested_inner
    )
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": nested_wrapped},
            )
        )
        == actual.resolve()
    )


def test_pipeline_cd_does_not_carry_into_right_side(tmp_path: Path):
    first = _configured_git_repo(tmp_path / "first")
    actual = _configured_git_repo(tmp_path / "actual")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "pipeline-scope"

    piped = f"cd {first} | brigade work verify run --target . --command 'true' --capture brigade-work"
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": piped},
            )
        )
        == cwd_repo.resolve()
    )

    piped_other = (
        f"cd {first} | cd {actual} && brigade work verify run --target . --command 'true' --capture brigade-work"
    )
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": piped_other},
            )
        )
        == actual.resolve()
    )


def test_subshell_cd_does_not_carry_to_parent_scope(tmp_path: Path):
    first = _configured_git_repo(tmp_path / "first")
    actual = _configured_git_repo(tmp_path / "actual")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "subshell-scope"

    subshell = f"(cd {first}) && brigade work verify run --target . --command 'true' --capture brigade-work"
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": subshell},
            )
        )
        == cwd_repo.resolve()
    )

    nested = (
        f"cd {first} && (cd {actual} && brigade work verify run --target . --command 'true' --capture brigade-work)"
    )
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": nested},
            )
        )
        == actual.resolve()
    )


def test_unmatched_quote_and_quoted_literal_do_not_attribute_cd(tmp_path: Path):
    first = _configured_git_repo(tmp_path / "first")
    actual = _configured_git_repo(tmp_path / "actual")
    cwd_repo = _configured_git_repo(tmp_path / "cwdrepo")
    session_id = "quote-scope"

    unmatched = f'cd "{first} && brigade work verify run --target . --command true --capture brigade-work'
    assert runtime._verifier_target_from_command(unmatched, cwd_repo.resolve()) is None
    assert runtime._command_candidate_paths(unmatched, cwd_repo.resolve()) == []

    quoted_literal = f"echo 'cd {first}' && brigade work verify run --target . --command 'true' --capture brigade-work"
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": quoted_literal},
            )
        )
        == cwd_repo.resolve()
    )

    literal_then_cd = f"printf 'cd {first}' && cd {actual} && brigade work verify run --target . --command 'true' --capture brigade-work"
    assert (
        runtime.wired_target_from_payload(
            _payload(
                cwd_repo,
                "PostToolUse",
                session_id=session_id,
                tool_name="Bash",
                tool_input={"command": literal_then_cd},
            )
        )
        == actual.resolve()
    )


def test_heredoc_scanner_is_quote_aware_and_queues_delimiters(tmp_path: Path):
    quoted_multiline = "printf '<<EOF\npytest\nEOF\n' && echo ok"
    assert not runtime.is_raw_verification(quoted_multiline)
    assert "pytest" in runtime._strip_heredoc_bodies(quoted_multiline)

    assert "pytest" in runtime._strip_heredoc_bodies("printf '<<EOF' && pytest")
    assert runtime.is_raw_verification("printf '<<EOF' && pytest")

    dual = "cat <<ONE <<TWO\nalpha\nONE\nbeta\nTWO\necho done"
    stripped = runtime._strip_heredoc_bodies(dual)
    assert stripped == "cat <<ONE <<TWO\necho done"
    assert "alpha" not in stripped
    assert "beta" not in stripped
    assert not runtime.is_raw_verification("cat <<ONE <<TWO\npytest\nTWO\necho done")

    commented = "# <<END\npytest -q\nEND"
    assert runtime.is_raw_verification(commented)
    assert "pytest" in runtime._strip_heredoc_bodies(commented)

    commented_apostrophe = "# ' <<END\npytest -q\nEND"
    assert runtime.is_raw_verification(commented_apostrophe)
    assert "pytest" in runtime._strip_heredoc_bodies(commented_apostrophe)

    inline = "echo hi # <<END not a heredoc\npytest -q"
    assert runtime.is_raw_verification(inline)
    assert "pytest" in runtime._strip_heredoc_bodies(inline)

    quoted_hash = "printf '# <<END' && cat <<EOF\nbody\nEOF\necho done"
    stripped_hash = runtime._strip_heredoc_bodies(quoted_hash)
    assert stripped_hash == "printf '# <<END' && cat <<EOF\necho done"
    assert "body" not in stripped_hash

    backslash = "cat <<\\END\npytest -q\nEND\necho done"
    assert not runtime.is_raw_verification(backslash)
    stripped_bs = runtime._strip_heredoc_bodies(backslash)
    assert stripped_bs == "cat <<\\END\necho done"
    assert "pytest" not in stripped_bs


def test_posttooluse_read_skill_is_observed_not_an_exercised_artifact(tmp_path: Path):
    target = _wired_claude(tmp_path)
    skill = target / ".claude" / "skills" / "taste" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# taste\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(skill)},
        ),
    )
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert "exercised_artifact_id" not in state  # session-derived ids are veto-only, never caller typed
    assert any(item.endswith("taste/SKILL.md") for item in state["read_files"])


def test_posttooluse_read_external_skill_does_not_set_exercised_artifact(tmp_path: Path):
    target = _wired_claude(tmp_path)
    reference_skill = tmp_path / "other-repo" / ".claude" / "skills" / "reference-only" / "SKILL.md"
    reference_skill.parent.mkdir(parents=True)
    reference_skill.write_text("# reference-only\n")

    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": str(reference_skill)},
        ),
    )

    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert "exercised_artifact_id" not in state


def test_verify_replacement_after_a_skill_read_still_says_auto(tmp_path: Path):
    target = _wired_claude(tmp_path)
    skill = target / ".claude" / "skills" / "taste" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# taste\n")
    runtime.handle_payload(
        "PostToolUse",
        _payload(target, "PostToolUse", tool_name="Read", tool_input={"file_path": str(skill)}),
    )
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--capture auto" in reason
    assert "--capture taste" not in reason


def test_verify_replacement_prefers_an_explicitly_typed_capture(tmp_path: Path):
    target = _wired_claude(tmp_path)
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": 'brigade work verify run --target . --command "true" --capture taste'},
        ),
    )
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--capture taste" in reason
    assert "--capture auto" not in reason


def test_verify_replacement_names_the_hook_target_when_the_verifier_checkout_differs(tmp_path: Path):
    hook_target = tmp_path / "hook"
    verify_target = tmp_path / "verify"
    hook_target.mkdir()
    verify_target.mkdir()
    same = runtime._verify_replacement(hook_target, "pytest", "0123456789abcdef", hook_target=None)
    assert "BRIGADE_CLAUDE_SESSION_TARGET" not in same
    other = runtime._verify_replacement(verify_target, "pytest", "0123456789abcdef", hook_target=hook_target)
    assert f"BRIGADE_CLAUDE_SESSION_TARGET={hook_target}" in other
    assert "--capture auto" in other


def test_verify_replacement_falls_back_to_auto_capture(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--capture auto" in reason


def test_posttooluse_routed_verify_records_capture_artifact(tmp_path: Path):
    target = _wired_claude(tmp_path)
    command = 'brigade work verify run --target . --command "true" --capture ultra-work-scout'
    runtime.handle_payload(
        "PostToolUse",
        _payload(target, "PostToolUse", tool_name="Bash", tool_input={"command": command}),
    )
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert state["exercised_artifact_id"] == "ultra-work-scout"


def test_init_hint_quotes_hostile_directory_names(tmp_path: Path):
    repo = tmp_path / "evil;rm -rf tmp"
    (repo / ".git").mkdir(parents=True)
    result = runtime.handle_payload("SessionStart", _payload(repo, "SessionStart"))
    context = result["hookSpecificOutput"]["additionalContext"]
    import shlex as _shlex

    assert _shlex.quote(str(repo.resolve())) in context
    tokens = _shlex.split(context.split(": brigade ", 1)[1].splitlines()[0])
    assert tokens[:2] == ["init", "--target"]
    assert tokens[2] == str(repo.resolve())


def test_stop_ignores_stale_entries_from_prior_task_with_reused_session_id(tmp_path: Path, monkeypatch):
    """A dispatched task reusing a session id must not re-arm the stop gate for
    repos only earlier tasks touched (#992)."""
    repo_a = _configured_git_repo(tmp_path / "repo-a")
    repo_b = _configured_git_repo(tmp_path / "repo-b")
    session_id = "fleet-reused-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    # Task 1: mutate repo A, link repo B into the session graph, then abandon
    # the task while its verification gate is still armed.
    runtime.handle_payload("SessionStart", _payload(repo_a, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            repo_a,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(repo_a / "tracked.txt"), "content": "after\n"},
        ),
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            repo_a,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": f"cd {repo_b} && pwd"},
        ),
    )
    blocked = runtime.handle_payload("Stop", _payload(repo_a, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"
    assert str(repo_a.resolve()) in blocked["reason"]

    # Task 2: same session id, different repo, read-only. The stale entries
    # from task 1 must not block this task or rejoin its repo graph.
    runtime.handle_payload(
        "SessionStart", _payload(repo_b, "SessionStart", session_id=session_id, source="startup", cwd=str(repo_b))
    )
    stopped = runtime.handle_payload(
        "Stop", _payload(repo_b, "Stop", session_id=session_id, stop_hook_active=False, cwd=str(repo_b))
    )
    assert stopped is None
    state_b = runtime.read_session_state(repo_b, session_id)
    assert str(repo_a.resolve()) not in (state_b.get("session_repos") or [])


def test_read_only_task_does_not_inherit_prior_task_mutation(tmp_path: Path, monkeypatch):
    """mutated/write_observed must be re-evaluated per task, not latched for the
    lifetime of a reused session id (#992)."""
    target = _configured_git_repo(tmp_path / "repo")
    session_id = "fleet-reused-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Edit",
            tool_input={"file_path": str(target / "tracked.txt")},
        ),
    )
    assert runtime.read_session_state(target, session_id)["write_observed"] is True

    # A later dispatched task with the same session id only reads this repo.
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", session_id=session_id, tool_name="Bash", tool_input={"command": "ls"}),
    )
    runtime.handle_payload(
        "PostToolUse",
        _payload(target, "PostToolUse", session_id=session_id, tool_name="Bash", tool_input={"command": "ls"}),
    )
    assert runtime.read_session_state(target, session_id)["write_observed"] is False
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False)) is None
    )


def test_new_task_that_mutates_is_still_gated(tmp_path: Path, monkeypatch):
    """Per-task scoping must not weaken the loop: a task that writes is gated."""
    target = _configured_git_repo(tmp_path / "repo")
    session_id = "fleet-reused-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "tracked.txt"), "content": "after\n"},
        ),
    )
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"
    assert "brigade work verify run" in blocked["reason"]


def test_resume_source_keeps_current_task_gate(tmp_path: Path, monkeypatch):
    """Resuming (or compacting) a session continues the same task; its armed
    gate must survive the SessionStart."""
    target = _configured_git_repo(tmp_path / "repo")
    session_id = "resumed-session"
    monkeypatch.setattr(runtime, "_run_brief", lambda repo: "brief")

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="startup"))
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Write",
            tool_input={"file_path": str(target / "tracked.txt"), "content": "after\n"},
        ),
    )
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id, source="resume"))
    blocked = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))
    assert blocked["decision"] == "block"


def test_runtime_presence_shim_lives_outside_runtime():
    from brigade.claude_hooks import presence as presence_mod

    assert not hasattr(runtime, "_emit_presence")
    assert hasattr(presence_mod, "emit_presence")
    assert presence_mod.emit_presence.__doc__


def test_claude_presence_start_refresh_and_accepted_stop(target, monkeypatch):
    calls = _capture_presence(monkeypatch)
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="s1"))
    runtime.handle_payload("PostToolUse", _write_payload(target, session_id="s1"))
    state = runtime.read_session_state(target, "s1")
    write_at = state["last_verification_write_at"]
    run_dir = target / ".brigade" / "work" / "verify-runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "status": "completed",
                "started_at": write_at,
                "completed_at": write_at,
                "target": str(target.resolve()),
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )
    runtime.handle_payload("Stop", _payload(target, "Stop", session_id="s1", stop_hook_active=False))
    # Stop fires once per assistant turn; the thread is still open.
    assert [call.action for call in calls] == ["upsert", "upsert"]
    assert all(call.action != "end" for call in calls)

    runtime.handle_payload("SessionEnd", _payload(target, "SessionEnd", session_id="s1", reason="clear"))
    assert [call.action for call in calls] == ["upsert", "upsert", "end"]
    assert Path(calls[-1].snapshot.checkout_path) == target.resolve()
    end_calls = [call for call in calls if call.action == "end"]
    assert len(end_calls) == 1


def test_blocked_stop_does_not_end_presence(target, monkeypatch):
    calls = _capture_presence(monkeypatch)
    result = runtime.handle_payload("Stop", _unverified_write_stop(target, session_id="s1"))
    assert result["decision"] == "block"
    assert all(call.action != "end" for call in calls)


def _capture_heartbeat(monkeypatch) -> list[tuple[str, str]]:
    """Record the heartbeat events handle_payload dispatches, without a Hub."""
    events: list[tuple[str, str]] = []

    def on_event(event, target, session_id, **_kwargs):
        events.append((event, session_id))
        return None

    monkeypatch.setattr(runtime.heartbeat, "on_event", on_event)
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    monkeypatch.setattr(runtime, "_run_recall", lambda *_args, **_kwargs: "")
    return events


def test_handle_payload_routes_live_events_to_the_heartbeat(target, monkeypatch):
    events = _capture_heartbeat(monkeypatch)

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="s1"))
    runtime.handle_payload("UserPromptSubmit", _payload(target, "UserPromptSubmit", session_id="s1"))
    runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", session_id="s1", tool_name="Read", tool_input={"file_path": str(target)}),
    )

    assert events == [
        ("SessionStart", "s1"),
        ("UserPromptSubmit", "s1"),
        ("PreToolUse", "s1"),
    ]


def test_session_end_reaches_the_heartbeat_and_stop_never_ends_the_session(target, monkeypatch):
    """Stop fires once per assistant turn; only SessionEnd closes the thread."""
    events = _capture_heartbeat(monkeypatch)

    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id="s1"))
    stop = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="s1", stop_hook_active=False))

    assert stop is None
    assert ("SessionEnd", "s1") not in events

    assert runtime.handle_payload("SessionEnd", _payload(target, "SessionEnd", session_id="s1", reason="clear")) is None
    assert events[-1] == ("SessionEnd", "s1")


def test_bash_hook_events_fingerprint_the_worktree_once_each(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    calls: list[str] = []

    def counted(repo: Path) -> str:
        calls.append(str(repo))
        return f"fp-{len(calls)}"

    monkeypatch.setattr(runtime, "repo_worktree_fingerprint", counted)
    pretool = _payload(target, "PreToolUse", session_id="fp-once", tool_name="Bash", tool_input={"command": "ls"})
    posttool = {**pretool, "hook_event_name": "PostToolUse"}
    # New session state and the Bash baseline share one fingerprint.
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert len(calls) == 1
    assert runtime.read_session_state(target, "fp-once")["pending_bash_fingerprint"] == "fp-1"
    # A changed worktree is detected and recorded from the same post-command fingerprint.
    assert runtime.handle_payload("PostToolUse", posttool) is None
    assert len(calls) == 2
    state = runtime.read_session_state(target, "fp-once")
    assert state["write_observed"] is True
    assert state["repo_fingerprint"] == "fp-2"
    # Later events keep the recorded baseline instead of re-fingerprinting for it.
    assert runtime.handle_payload("PreToolUse", pretool) is None
    assert len(calls) == 3


def _verified_write_stop(target: Path, monkeypatch, session_id: str = "s-handoff", **extra):
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "brief")
    stop = _unverified_write_stop(target, session_id=session_id)
    _write_session_receipt(target, session_id)
    return {**stop, **extra}


def _stop_text(result) -> str:
    if result is None:
        return ""
    return json.dumps(result)


def test_stop_advises_handoff_by_judgment_when_no_close_line(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    stop = _verified_write_stop(target, monkeypatch, last_assistant_message="Done. Tests pass.")
    text = _stop_text(runtime.handle_payload("Stop", stop))
    assert "brigade handoff draft" in text
    assert "Nothing durable: <reason>" in text


def test_stop_accepts_final_assistant_nothing_durable_line(tmp_path: Path, monkeypatch):
    target = _wired_claude(tmp_path)
    stop = _verified_write_stop(
        target,
        monkeypatch,
        last_assistant_message="Fixed the typo and ran the check.\n\nNothing durable: one-line typo fix",
    )
    result = runtime.handle_payload("Stop", stop)
    assert result is None or result.get("decision") != "block"
    assert "Memory Handoff" not in _stop_text(result)


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt": "Nothing durable: user said so"},
        {"tool_response": {"output": "Nothing durable: tool printed this"}},
        {"tool_input": {"command": "echo 'Nothing durable: quoted'"}},
        {"last_assistant_message": "Nothing durable: not the final line\nDone."},
        {"last_assistant_message": "Nothing durable:"},
        {"last_assistant_message": "Nothing durable: ok", "stop_hook_active": True},
    ],
)
def test_stop_ignores_nothing_durable_outside_final_assistant_line(tmp_path: Path, monkeypatch, extra):
    target = _wired_claude(tmp_path)
    stop = _verified_write_stop(target, monkeypatch, **extra)
    if extra.get("stop_hook_active"):
        assert runtime.handle_payload("Stop", stop) is None
        return
    assert "Memory Handoff" in _stop_text(runtime.handle_payload("Stop", stop))


# --- Manifest verify in the loop (#2589) ---------------------------------------------------------

_MANIFEST_SUBJECT = "src/subject.py"
_MANIFEST_SOURCE = "verify/manifests/subject-manifest.json"


def _manifest_repo(tmp_path: Path) -> Path:
    """A git repo with one tracked manifest bound to ``src/subject.py``, committed clean."""
    target = _git_wired_claude(tmp_path)
    (target / "src").mkdir()
    (target / _MANIFEST_SUBJECT).write_text("value = 1\n")
    manifests = target / "verify" / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "subject-manifest.json").write_text(
        json.dumps(
            {
                "schema": "brigade.verify_manifest.v1",
                "schema_version": 1,
                "manifest_id": "subject-manifest",
                "binding_mode": "patch_backed",
                "verifier_id": "brigade.verify.pytest",
                "patch_source": "worktree",
                "subject": {
                    "artifact_kind": "skill",
                    "artifact_id": "brigade-work",
                    "subject_path": _MANIFEST_SUBJECT,
                },
                "scope_globs": [_MANIFEST_SUBJECT],
                "checks": [
                    {
                        "check_id": "pytest.subject",
                        "check_role": "effectiveness",
                        "command": "python -m pytest -q tests/test_subject.py",
                    }
                ],
            }
        )
        + "\n"
    )
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True, text=True)
    return target


def _deny_reason(target: Path) -> str:
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_deny_without_manifest_match_keeps_todays_message(tmp_path: Path):
    target = _manifest_repo(tmp_path)
    (target / "src" / "other.py").write_text("other = 1\n")  # not a manifest subject

    reason = _deny_reason(target)

    assert "--command" in reason and "--capture auto" in reason
    assert "--manifest" not in reason
    assert "tracked verify manifest" not in reason


def test_deny_with_manifest_subject_change_adds_manifest_command_and_keeps_own(tmp_path: Path):
    target = _manifest_repo(tmp_path)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")

    reason = _deny_reason(target)

    own = f"--command {shlex.quote('python -m pytest -q')} --capture auto"
    assert own in reason  # the agent's own check is kept
    assert (
        f"--target {shlex.quote(str(target.resolve()))} --manifest verify/manifests/subject-manifest.json --capture auto"
        in reason
    )
    assert _MANIFEST_SUBJECT in reason


def test_deny_ignores_a_scope_glob_only_change(tmp_path: Path):
    target = _manifest_repo(tmp_path)
    manifest_path = target / "verify" / "manifests" / "subject-manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["scope_globs"] = [_MANIFEST_SUBJECT, "src/extra/**"]
    manifest_path.write_text(json.dumps(payload) + "\n")
    subprocess.run(["git", "commit", "-am", "scope"], cwd=target, check=True, capture_output=True, text=True)
    (target / "src" / "extra").mkdir()
    (target / "src" / "extra" / "x.py").write_text("x = 1\n")  # in scope_globs, not the subject

    assert "--manifest" not in _deny_reason(target)


def test_deny_fails_open_when_git_errors(tmp_path: Path, monkeypatch):
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")

    def broken_git(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(manifest_loop.subprocess, "run", broken_git)

    reason = _deny_reason(target)

    assert "--command" in reason
    assert "--manifest" not in reason


def _session_writes(target: Path, session_id: str, path: Path) -> None:
    """One Write tool call as the hooks see it: PreToolUse records the path, PostToolUse the write."""
    event_payload = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": path.read_text()}}
    for event in ("PreToolUse", "PostToolUse"):
        runtime.handle_payload(event, _payload(target, event, session_id=session_id, **event_payload))


def _stop_after_subject_write(target: Path, monkeypatch, session_id: str = "manifest-stop") -> tuple[dict, float]:
    """Run SessionStart, write the manifest subject, satisfy the generic gate, and pin the subject mtime."""
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    subject = target / _MANIFEST_SUBJECT
    subject.write_text("value = 2\n")
    _session_writes(target, session_id, subject)
    state = runtime.read_session_state(target, session_id)
    written = localio.parse_iso_datetime(state["started_at"]).timestamp() + 5
    os.utime(subject, (written, written))
    _write_session_receipt(target, session_id, tree_fingerprint=localio.tree_fingerprint(target))
    return _payload(target, "Stop", session_id=session_id, stop_hook_active=False), written


def _write_manifest_receipt(target: Path, *, started_at: float, status: str = "completed") -> None:
    run_dir = target / ".brigade" / "work" / "verify-runs" / "manifest-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(started_at, tz=localio.utc_now().tzinfo).isoformat()
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "manifest-run",
                "status": status,
                "started_at": stamp,
                "completed_at": stamp,
                "target": str(target.resolve()),
                "verify_manifest_id": "subject-manifest",
            }
        )
        + "\n"
    )


def test_stop_without_covered_manifest_change_is_unchanged(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "brief")
    session_id = "no-match"
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    other = target / "src" / "other.py"
    other.write_text("other = 2\n")
    _session_writes(target, session_id, other)
    _write_session_receipt(target, session_id, tree_fingerprint=localio.tree_fingerprint(target))

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))

    assert result is None or result.get("decision") != "block"


def test_stop_blocks_when_manifest_subject_changed_and_no_manifest_receipt(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    stop, _written = _stop_after_subject_write(target, monkeypatch)

    result = runtime.handle_payload("Stop", stop)

    assert result["decision"] == "block"
    assert (
        f"--target {shlex.quote(str(target.resolve()))} --manifest verify/manifests/subject-manifest.json --capture auto"
        in result["reason"]
    )
    assert "no verification receipt" not in result["reason"]  # the generic gate was satisfied


def test_stop_blocks_when_manifest_receipt_predates_the_write(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    stop, written = _stop_after_subject_write(target, monkeypatch)
    _write_manifest_receipt(target, started_at=written - 60)

    result = runtime.handle_payload("Stop", stop)

    assert result["decision"] == "block"
    assert "--manifest verify/manifests/subject-manifest.json" in result["reason"]


def test_stop_allows_when_a_newer_manifest_receipt_exists(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    stop, written = _stop_after_subject_write(target, monkeypatch)
    _write_manifest_receipt(target, started_at=written + 60)

    result = runtime.handle_payload("Stop", stop)

    assert result is None or result.get("decision") != "block"


def test_stop_does_not_count_a_failed_manifest_receipt(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    stop, written = _stop_after_subject_write(target, monkeypatch)
    _write_manifest_receipt(target, started_at=written + 60, status="failed")

    result = runtime.handle_payload("Stop", stop)

    assert result["decision"] == "block"


def test_stop_fails_open_when_git_errors_in_the_manifest_probe(tmp_path: Path, monkeypatch):
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    stop, _written = _stop_after_subject_write(target, monkeypatch)
    probe_attempted = False

    def broken_git(*_args, **_kwargs):
        nonlocal probe_attempted
        probe_attempted = True
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(
        manifest_loop,
        "subprocess",
        SimpleNamespace(
            run=broken_git, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired
        ),
    )

    result = runtime.handle_payload("Stop", stop)

    assert probe_attempted
    assert result is None or result.get("decision") != "block"


def test_manifest_changed_paths_are_repo_relative_posix(tmp_path: Path):
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    (target / "src" / "nested").mkdir()
    (target / "src" / "nested" / "new.py").write_text("n = 1\n")
    (target / _MANIFEST_SUBJECT).write_text("value = 3\n")

    paths = manifest_loop.changed_paths(target)

    assert paths is not None
    assert "src/nested/new.py" in paths and _MANIFEST_SUBJECT in paths
    assert all("\\" not in path and not path.startswith("/") for path in paths)


def _manifest_stop(target: Path, session_id: str) -> dict | None:
    return runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False))


def test_stop_ignores_a_subject_that_was_dirty_before_the_session(tmp_path: Path, monkeypatch):
    """A formatter, checkout or stash pop bumps the mtime of a subject this session never wrote."""
    target = _manifest_repo(tmp_path)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")  # dirty before the session starts
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "brief")
    session_id = "dirty-before"
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
    other = target / "src" / "other.py"
    other.write_text("other = 2\n")
    _session_writes(target, session_id, other)
    started = localio.parse_iso_datetime(runtime.read_session_state(target, session_id)["started_at"]).timestamp()
    os.utime(target / _MANIFEST_SUBJECT, (started + 5, started + 5))  # mtime bumped after session start
    _write_session_receipt(target, session_id, tree_fingerprint=localio.tree_fingerprint(target))

    result = _manifest_stop(target, session_id)

    assert result is None or result.get("decision") != "block"


def test_stop_ignores_a_subject_another_session_wrote(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "brief")
    reader, writer = "reader-session", "writer-session"
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=reader))
    other = target / "src" / "other.py"
    other.write_text("other = 2\n")
    _session_writes(target, reader, other)
    _write_session_receipt(target, reader, tree_fingerprint=localio.tree_fingerprint(target))
    subject = target / _MANIFEST_SUBJECT
    subject.write_text("value = 9\n")
    _session_writes(target, writer, subject)  # a concurrent session in the same worktree

    result = _manifest_stop(target, reader)

    assert result is None or result.get("decision") != "block"


def test_stop_skips_the_manifest_gate_when_the_generic_gate_sees_a_foreign_tree(tmp_path: Path, monkeypatch):
    """#959: the generic gate forgives a tree a concurrent writer drifted after this session's capture."""
    target = _manifest_repo(tmp_path)
    reader, writer = "owner-session", "drift-session"
    stop, _written = _stop_after_subject_write(target, monkeypatch, session_id=reader)
    foreign = target / "src" / "foreign.py"
    foreign.write_text("foreign = 1\n")
    _session_writes(target, writer, foreign)

    result = runtime.handle_payload("Stop", stop)

    assert result is None or result.get("decision") != "block"


def test_stop_ignores_a_deleted_subject_and_the_deny_does_not_name_it(tmp_path: Path, monkeypatch):
    target = _manifest_repo(tmp_path)
    stop, _written = _stop_after_subject_write(target, monkeypatch)
    (target / _MANIFEST_SUBJECT).unlink()
    # Re-capture so the generic gate matches the tree without the subject.
    _write_session_receipt(target, "manifest-stop", tree_fingerprint=localio.tree_fingerprint(target))

    assert "--manifest" not in _deny_reason(target)
    result = runtime.handle_payload("Stop", stop)
    assert result is None or result.get("decision") != "block"


def test_deny_names_the_manifest_for_unsupported_verifier_structure(tmp_path: Path):
    target = _manifest_repo(tmp_path)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")

    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "cd src && pytest -q"}),
    )

    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Split shell grouping" in reason
    assert f"--target {shlex.quote(str(target.resolve()))} --manifest {_MANIFEST_SOURCE} --capture auto" in reason


def test_manifest_command_uses_a_path_that_resolves_when_the_id_is_ambiguous(tmp_path: Path):
    from brigade import verify_manifest
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    manifests = target / "verify" / "manifests"
    (manifests / "duplicate.json").write_text((manifests / "subject-manifest.json").read_text())
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "dup"], cwd=target, check=True, capture_output=True, text=True)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")

    assert verify_manifest.resolve_manifest(target, "subject-manifest")[0] is None  # the id is ambiguous
    covered = manifest_loop.covered_manifests(target)

    assert len(covered) == 1
    assert verify_manifest.resolve_manifest(target, covered[0].source)[0] is not None


def _nested_manifest_repo(tmp_path: Path) -> Path:
    """The manifest workspace is ``pkg/`` inside a larger repo."""
    root = tmp_path / "mono"
    nested = root / "pkg"
    shutil.copytree(_manifest_repo(tmp_path), nested, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    for key, value in (("user.email", "test@example.com"), ("user.name", "Test User")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True, capture_output=True, text=True)
    (root / "outside.py").write_text("outside = 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True, text=True)
    return nested


def test_changed_paths_are_relative_to_a_target_nested_in_a_larger_repo(tmp_path: Path):
    from brigade.claude_hooks import manifest_loop

    nested = _nested_manifest_repo(tmp_path)
    (nested / _MANIFEST_SUBJECT).write_text("value = 2\n")
    (nested.parent / "outside.py").write_text("outside = 2\n")  # in the repo, not under the target

    assert manifest_loop.changed_paths(nested) == [_MANIFEST_SUBJECT]
    covered = manifest_loop.covered_manifests(nested)
    assert [(item.manifest_id, item.subject_path) for item in covered] == [("subject-manifest", _MANIFEST_SUBJECT)]


def test_changed_paths_keep_non_ascii_filenames(tmp_path: Path):
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    (target / "src" / "caf\u00e9.py").write_text("n = 1\n")
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")

    paths = manifest_loop.changed_paths(target)

    assert paths is not None and "src/caf\u00e9.py" in paths and _MANIFEST_SUBJECT in paths


def test_manifest_probe_git_calls_stay_inside_the_hook_budget(tmp_path: Path, monkeypatch):
    from brigade.claude_hooks import manifest_loop

    target = _manifest_repo(tmp_path)
    (target / _MANIFEST_SUBJECT).write_text("value = 2\n")
    real_run = subprocess.run
    timeouts: list[float] = []

    def recording_run(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(manifest_loop.subprocess, "run", recording_run)

    assert manifest_loop.changed_paths(target) == [_MANIFEST_SUBJECT]
    assert timeouts and max(timeouts) <= 2.0  # one shared deadline, not 2 s per call
    assert manifest_loop._GIT_TIMEOUT_SECONDS <= 2.0


@pytest.mark.parametrize(
    "command, expected",
    [
        ("pytest -q", True),
        ("uv run pytest", True),
        ("npx jest", True),
        ("npx --prefix /tmp jest", True),
        ("env -S 'pytest -q'", True),
        ("bash -lc 'pytest -q'", True),
        ("command -p pytest", True),
        ("make -j 4 check", True),
        ("cd x && pytest", True),
        ("npx --unknown option-value jest", True),
        ("uv --unknown option-value run pytest", True),
        ("poetry run --unknown option-value pytest", True),
        ("npm --unknown option-value run check:types", True),
        ("npx --package jest echo done", False),
        ("npm run build", False),
        ("uv run python -V", False),
        ("echo pytest", False),
        ("command -v pytest", False),
        ("python -m brigade work verify run", False),
        ("printf '%s' 'pytest -q'", False),
    ],
)
def test_recognizer_matches_main(command, expected):
    # Captured by running the recognizer at 41780bb5579c before editing it.
    assert runtime.is_raw_verification(command) is expected


def _recognition_input(shape, count):
    words = " ".join(["arg"] * count)
    if shape in {"npx", "uv", "poetry"}:
        runner = shape if shape == "npx" else f"{shape} run"
        return f"{runner} --unknown {words}"
    if shape == "shell":
        return shlex.join(["bash", "-lc", shlex.join(["bash", "-lc", f"echo {words}"])])
    if shape == "chain":
        return "true && " * count + "true"
    assert shape == "env"
    return "env " + "KEY=arg " * count + "echo"


@pytest.mark.parametrize("shape", ["npx", "uv", "poetry", "shell", "chain", "env"])
def test_recognizer_work_bound(shape):
    from brigade.claude_hooks import command_tokens

    for count in (1000, 2000, 4000, 8000):
        command = _recognition_input(shape, count)
        budget = command_tokens.RecognitionBudget()
        assert not runtime._is_raw_verification_text(command, depth=0, budget=budget)
        assert 0 < budget.steps <= 24 * len(command)

    unit = len(_recognition_input(shape, 2)) - len(_recognition_input(shape, 1))
    count = 1 + (65535 - len(_recognition_input(shape, 1))) // unit
    command = _recognition_input(shape, count).ljust(65535)
    budget = command_tokens.RecognitionBudget()
    assert not runtime._is_raw_verification_text(command, depth=0, budget=budget)
    assert 0 < budget.steps <= 24 * len(command)


def test_recognizer_budget_exhaustion_returns_false(monkeypatch):
    from brigade.claude_hooks import command_tokens

    command = "cd src && env -S 'pytest -q'"
    assert runtime.is_raw_verification(command)
    monkeypatch.setattr(command_tokens, "MAX_RECOGNITION_STEPS", 5 * len(command) + 1)
    assert runtime._recognize(command) is command_tokens.Recognition.UNDECIDED
    assert not runtime.is_raw_verification(command)


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_long_runner_command_through_pre_and_post_tool_use(target, monkeypatch, event):
    from brigade import observed_checks

    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    budgets = _measure_event_budget(monkeypatch)
    steps = []
    lead = "pytest" if event == "PreToolUse" else "true"
    for count in (11, 22):
        segments = ["npx --unfamiliar " + "! " * 1360 + "echo"] * count
        command = " && ".join([lead, *segments])
        payload = _payload(
            target, event, session_id=f"runner-{count}", tool_name="Bash", tool_input={"command": command}
        )
        started = time.perf_counter()
        result = runtime.handle_payload(event, payload)
        elapsed = time.perf_counter() - started
        if count == 22:
            assert elapsed < _HOOK_TIME_BACKSTOP_SECONDS
        budget = _assert_hook_steps(command, budgets)
        steps.append(budget.steps)
        assert not budget.stopped
        if event == "PreToolUse":
            output = result["hookSpecificOutput"]
            assert output["permissionDecision"] == "deny"
            assert "Route verification through Brigade" in output["permissionDecisionReason"]
        else:
            assert result is None
    _assert_hook_scaling(*steps)


_STRESS_PREFIXES = {
    "npx": ("", "npx --unfamiliar "),
    "uv": ("", "uv run --unfamiliar "),
    "poetry": ("", "poetry run --unfamiliar "),
    "make": ("", "make --unfamiliar "),
    "npm": ("", "npm --unfamiliar run "),
    "pnpm": ("", "pnpm --unfamiliar run "),
    "yarn": ("", "yarn --unfamiliar run "),
    "bun": ("", "bun --unfamiliar run "),
    "assignments": ("npx --unfamiliar ", "FLAG=demo "),
    "control": ("npx --unfamiliar ", "! "),
    "ruff": ("npx --unfamiliar ", "ruff format "),
    "env": ("npx --unfamiliar ", "env -S '' "),
    "heredoc": ("echo ", r'<<"a\"b" '),
}


def _recognition_stress(shape, size, final="echo"):
    prefix, unit = _STRESS_PREFIXES[shape]
    if shape == "heredoc":
        final = ""
    count = (size - len(prefix) - len(final)) // len(unit)
    return (prefix + unit * count + final).ljust(size)


@pytest.mark.parametrize("shape", _STRESS_PREFIXES)
def test_recognizer_visits_each_position_once_per_shell_depth(shape, monkeypatch):
    from collections import Counter

    from brigade.claude_hooks import command_tokens

    calls = Counter()
    scan = runtime._scan_position

    def counted(tokens, *, depth):
        calls[tokens.position, depth] += 1
        return scan(tokens, depth=depth)

    monkeypatch.setattr(runtime, "_scan_position", counted)
    for size in (300, 1000, 4000, 16000, 65535):
        calls.clear()
        budget = command_tokens.RecognitionBudget()
        result = runtime._recognize(_recognition_stress(shape, size), budget)
        assert result is command_tokens.Recognition.NO
        assert 0 < budget.steps <= 36 * size
        assert calls and max(calls.values()) == 1
        assert len(calls) <= 5 * size


@pytest.mark.parametrize("shape", ["npx", "assignments", "control", "ruff", "env"])
def test_recognizer_reaches_later_segment(shape):
    from brigade.claude_hooks import command_tokens

    command = _recognition_stress(shape, 15000) + " && pytest -q"
    assert runtime._recognize(command) is command_tokens.Recognition.YES


def test_recognizer_shell_lexer_matches_shlex():
    from itertools import product

    words = ("a", "é", "''", "'a b'", '"a b"', '"a\\b"', "\\&", "';'")
    for first, divider, second in product(words, ("", " ", "&&", "\n", ";>"), words):
        command = first + divider + second
        assert runtime._shell_tokens(command) == list(runtime._shell_lexer(command))


def test_recognizer_windows_env_lexer_matches_shlex(monkeypatch):
    from itertools import product

    from brigade.claude_hooks import command_tokens

    monkeypatch.setattr(command_tokens, "os", SimpleNamespace(name="nt"))
    words = ("a", "é", "''", "'a b'", '"a b"', r'"a\b"', r"\&", "';'")
    for first, divider, second in product(words, ("", " ", "&&", "\n", ";>"), words):
        command = first + divider + second
        try:
            expected = shlex.split(command, posix=False)
        except ValueError:
            with pytest.raises(ValueError):
                command_tokens._split_env_words(command)
        else:
            assert command_tokens._split_env_words(command) == expected


@pytest.mark.parametrize("mode", ["strict", "observe", "off"])
@pytest.mark.parametrize("limit", ["length", "work"])
def test_undecided_recognition_enforces_only_strict(target, monkeypatch, mode, limit):
    from brigade.claude_hooks import command_tokens

    monkeypatch.setattr(runtime.work_mode, "resolve_mode", lambda _target: mode)
    if limit == "length":
        command = "pytest " + "a" * 70_000
        assert len(command) > 65536
    else:
        command = "echo done"
        monkeypatch.setattr(command_tokens, "MAX_RECOGNITION_STEPS", 1)
    assert runtime._recognize(command) is runtime.Recognition.UNDECIDED
    assert not runtime.is_raw_verification(command)
    result = runtime.handle_payload(
        "PreToolUse", _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": command})
    )
    if mode != "strict":
        assert result is None
    else:
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        limit_text = "65,536-character" if limit == "length" else "1-step"
        assert reason == (
            f"This command exceeds the {limit_text} classification limit. "
            "Split the command, or move the long content into a file and run the command using that file."
        )


def test_recognizer_finds_verifier_after_long_control_prefix():
    command = "npx --unfamiliar " + "! " * 1500 + "arg pytest"
    assert runtime.is_raw_verification(command)


def test_parenthesized_reader_preserves_quoted_nested_substitution():
    nested = 'printf "%s" "$(echo "$(pytest -q)")"'
    command = "(" + nested + ") && true"
    assert runtime._read_parenthesized(command, 1) == (nested, len(nested) + 1)
    assert runtime.is_raw_verification(command)


def _recognition_compat_commands():
    import random

    rng = random.Random(2851)
    commands = [
        "pytest -q",
        "echo done",
        "python -I -m unittest",
        "ruff format --check",
        "ruff format src/",
        "make -j 4 check",
        "npm run test:unit",
        "command -p pytest",
        "command -v pytest",
        "env -u FLAG echo done",
        "env -S 'env -S \"pytest -q\"'",
        "check",
        "test:unit",
        'echo "$(printf "%s" "$(pytest)")"',
        "true && pytest",
    ]
    runners = [
        "npx --prefix ./src",
        "npx --future ./src",
        "uv run --no-sync",
        "uv run --future ./src",
        "poetry run -q",
        "poetry run --future ./src",
        "make --future ./src",
        "npm --future ./src run",
        "pnpm --silent run",
        "yarn --future ./src run",
        "bun --silent run",
    ]
    wrappers = [
        lambda text: "bash -lc " + shlex.quote(text),
        lambda text: "env -S " + shlex.quote(text),
        lambda text: "env --split-string=" + shlex.quote(text),
        lambda text: "FLAG=demo MODE=demo " + text,
        lambda text: "! ! " + text,
        lambda text: "cd ./src && " + text,
        lambda text: rng.choice(runners) + " " + text,
    ]
    for _ in range(4096):
        command = rng.choice(commands)
        for _ in range(rng.randrange(1, 4)):
            command = rng.choice(wrappers)(command)
        yield command


def _recognition_compat_digest(classify, first_verifier, *, deep_shells=False):
    from hashlib import sha256

    digest = sha256()
    for index, command in enumerate(_recognition_compat_commands()):
        row = [classify(command), first_verifier(command)]
        if deep_shells and index == 1235:
            # The one deliberate difference is a verifier inside shell
            # text that the former nesting cutoff did not visit.
            assert row[0] is True
            row[0] = False
        digest.update(json.dumps(row).encode("ascii") + b"\n")
    return digest.hexdigest()


def test_recognizer_seeded_comparison_with_main():
    # Digest of 4,096 pairs from main at 41780bb5579c. The comparison
    # includes classification and the command chosen for the replacement.
    expected = {
        "nt": "d5326ad3e19c6f5628346c74ad3c0f9574a605d0f30e9cab60339dab99cb7816",
        "posix": "3f01860a9cb3764363fa5c0e7a0e7322070243f22b3d83cd134423e96911ae78",
    }
    digest = _recognition_compat_digest(runtime.is_raw_verification, runtime._first_verifier_command, deep_shells=True)
    assert digest == expected[os.name]


def _bounded_hook_commands(divisor):
    repeated = {shape: _recognition_stress(shape, 60_000 // divisor) for shape in _STRESS_PREFIXES}
    padding = "x" * (2048 // divisor)
    command = f"echo {padding}"
    verification = f"pytest {padding}"
    for _ in range(5):
        command = shlex.join(["bash", "-lc", command])
        verification = shlex.join(["bash", "-lc", verification])
    return {
        **{shape: (command, False) for shape, command in repeated.items()},
        "dollar-spans": ("echo " + "$(true)" * (500 // divisor), False),
        "backtick-spans": ("echo " + "`true` " * (700 // divisor), False),
        "long-spans": ("echo " + "$(true)" * (8000 // divisor), False),
        "nested-substitutions": (f"echo $(echo $(echo $(echo $(echo {padding}))))", False),
        "nested-shell": (command, False),
        "nested-verification": (verification, True),
        "heredoc-body": ("cat <<EOF\n" + "x" * (59_980 // divisor) + "\nEOF\n", False),
        "git-message": ("git commit -m '" + "x" * (59_980 // divisor) + "'", False),
        "and-chain": (("true && " * (8124 // divisor)) + "true", False),
        "semicolon-chain": (("true; " * (10832 // divisor)) + "true", False),
        "pipe-chain": (("true | " * (9284 // divisor)) + "true", False),
        "stdin-limit": ("echo " + "x" * (261_995 // divisor), True),
        **{f"cd-{n}": (("cd a && " * (n // divisor)) + "pytest", True) for n in (200, 400, 1000, 8000)},
    }


_BOUNDED_HOOK_COMMANDS = _bounded_hook_commands(1)
_SHORT_HOOK_COMMANDS = _bounded_hook_commands(2)
_HOOK_TIME_BACKSTOP_SECONDS = 9


def _hook_commands_at_both_sizes(shape):
    return _SHORT_HOOK_COMMANDS[shape], _BOUNDED_HOOK_COMMANDS[shape]


def _assert_hook_steps(command, budgets, *, shape=None):
    assert len(budgets) == 1
    budget = budgets.pop()
    # Five fixed shell layers rescan their payloads while unquoting them.
    per_character = 100 if shape in {"nested-shell", "nested-verification"} else 40
    ceiling = 65_536 + per_character * len(command)
    assert 0 < budget.steps <= ceiling, (budget.steps, ceiling, budget.phase_steps)
    return budget


def _assert_hook_scaling(short_steps, full_steps):
    # Allow 12.5% above doubling for token and path boundaries. Repeating a
    # full suffix scan would approach 4x, even with the fixed setup costs.
    assert 4 * full_steps <= 9 * short_steps, (short_steps, full_steps)


def _measure_event_budget(monkeypatch):
    from brigade.claude_hooks import command_tokens

    budgets = []
    initialize = command_tokens.RecognitionBudget.__init__

    def record(budget):
        initialize(budget)
        budgets.append(budget)

    monkeypatch.setattr(command_tokens.RecognitionBudget, "__init__", record)
    return budgets


@pytest.mark.parametrize("shape", _BOUNDED_HOOK_COMMANDS)
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "PostToolUseFailure"])
def test_claude_bash_event_work_bound(target, monkeypatch, shape, event):
    from brigade import observed_checks
    from brigade.claude_hooks import command_tokens

    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    budgets = _measure_event_budget(monkeypatch)
    steps = []
    for index, (command, deny) in enumerate(_hook_commands_at_both_sizes(shape)):
        payload = _payload(
            target, event, session_id=f"event-{index}", tool_name="Bash", tool_input={"command": command}
        )
        started = time.perf_counter()
        result = runtime.handle_payload(event, payload)
        elapsed = time.perf_counter() - started
        if index == 1:
            assert elapsed < _HOOK_TIME_BACKSTOP_SECONDS
        steps.append(_assert_hook_steps(command, budgets, shape=shape).steps)
        if event == "PreToolUse":
            output = (result or {}).get("hookSpecificOutput", {})
            assert (output.get("permissionDecision") == "deny") is deny
            if len(command) <= command_tokens.MAX_COMMAND_CHARS:
                assert "classification limit" not in output.get("permissionDecisionReason", "")
    _assert_hook_scaling(*steps)


def test_strict_pretooluse_allows_long_git_add_path_list(target, monkeypatch):
    from brigade.claude_hooks import command_tokens

    assert runtime.work_mode.is_strict(target)
    paths = [f"src/example/features/feature_{index:04d}/fixtures/configuration_example.json" for index in range(600)]
    command = shlex.join(["git", "add", "--", *paths])
    assert 30_000 <= len(command) < command_tokens.MAX_COMMAND_CHARS
    budgets = _measure_event_budget(monkeypatch)
    event = "PreToolUse"
    payload = _payload(target, event, tool_name="Bash", tool_input={"command": command})
    assert runtime.handle_payload(event, payload) is None
    assert "classification" not in _assert_hook_steps(command, budgets).stopped


@pytest.mark.parametrize("form", ["copy", "list", "islice", "iterate", "slice"])
def test_token_views_cannot_copy_or_iterate_around_the_meter(monkeypatch, form):
    from itertools import islice

    from brigade.claude_hooks import command_tokens

    budget = command_tokens.RecognitionBudget()
    view = command_tokens.TokenView(["word"] * 64, budget)
    tail = view.drop(1)
    assert view.position[0] is tail.position[0]
    assert not hasattr(tail, "_tokens")
    monkeypatch.setattr(command_tokens, "MAX_RECOGNITION_STEPS", budget.steps + 2)
    if form == "copy":
        assert not hasattr(tail, "copy")
    elif form == "slice":
        with pytest.raises(ValueError):
            _ = tail[:10]
    else:
        with pytest.raises(command_tokens.RecognitionLimit):
            if form == "list":
                list(tail)
            elif form == "islice":
                list(islice(tail, 7))
            else:
                next(iter(tail))


def test_incomplete_attribution_keeps_write_evidence(target):
    command = "cd a && " * 500 + "true"
    event = "PostToolUse"
    runtime.handle_payload(event, _payload(target, event, tool_name="Bash", tool_input={"command": command}))
    state = runtime.read_session_state(target, "session-1")
    assert state and state["write_observed"] and state["last_verification_write_at"]


@pytest.mark.parametrize("mode", ["observe", "off"])
def test_claude_unclassified_capture(target, monkeypatch, mode):
    from brigade import observed_checks

    monkeypatch.setattr(runtime.work_mode, "resolve_mode", lambda _target: mode)
    command = "echo " + "x" * 70_000
    event = "PostToolUse"
    result = runtime.handle_payload(event, _payload(target, event, tool_name="Bash", tool_input={"command": command}))
    assert result is None
    (record,) = observed_checks.read_checks(target)[0]
    assert (record.tool, record.status, record.exit_code) == ("unclassified", "unknown", None)


@pytest.mark.parametrize(
    "event, interrupted, scenario",
    [
        ("PostToolUse", False, "exhausted"),
        ("PostToolUseFailure", False, "exhausted"),
        ("PostToolUseFailure", True, "exhausted"),
        ("PostToolUseFailure", False, "wrote"),
        ("PostToolUseFailure", True, "wrote"),
        ("PostToolUse", False, "attribution-error"),
        ("PostToolUseFailure", False, "attribution-error"),
        ("PostToolUse", False, "recognition-error"),
        ("PostToolUseFailure", False, "recognition-error"),
        ("PostToolUse", False, "write-detection-error"),
        ("PostToolUseFailure", False, "write-detection-error"),
        ("PostToolUse", False, "fingerprint-error"),
        ("PostToolUseFailure", False, "fingerprint-error"),
        ("PostToolUseFailure", False, "no-write"),
        ("PostToolUse", False, "no-command"),
    ],
)
def test_stop_distinguishes_own_bash_write_from_later_foreign_write(
    tmp_path, monkeypatch, event, interrupted, scenario
):
    target = _git_wired_claude(tmp_path)
    (target / "earlier.py").write_text("value = 0\n")
    subprocess.run(["git", "add", "earlier.py"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=target, check=True, capture_output=True)
    session = "own-write"
    monkeypatch.setattr(runtime, "_run_brief", lambda _target: "brief")
    runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session))

    def edit(name, session_id):
        path = target / name
        path.write_text("value = 1\n")
        runtime.handle_payload(
            "PostToolUse",
            _payload(
                target, "PostToolUse", session_id=session_id, tool_name="Write", tool_input={"file_path": str(path)}
            ),
        )

    edit("earlier.py", session)
    tree = localio.tree_fingerprint(target)
    assert tree is not None
    before = _write_session_receipt(target, session, tree_fingerprint=tree)
    wrote = scenario not in {"no-write", "no-command"}
    if scenario != "no-command":
        command = "touch own.py; " if wrote else "touch missing-dir/own.py; "
        if scenario == "exhausted":
            command += "cd a && " * 500
        command += "false" if event == "PostToolUseFailure" else "true"
        pre = _payload(target, "PreToolUse", session_id=session, tool_name="Bash", tool_input={"command": command})
        assert runtime.handle_payload("PreToolUse", pre) is None
        if wrote:
            (target / "own.py").write_text("value = 2\n")
        with monkeypatch.context() as patch:
            if scenario.endswith("-error"):

                def unavailable(*_args, **_kwargs):
                    raise RuntimeError("fixture attribution error")

                module, name = {
                    "attribution-error": (runtime.write_attribution, "bash_scopes"),
                    "recognition-error": (runtime, "_recognize"),
                    "write-detection-error": (runtime.write_attribution, "touches_worktree"),
                    "fingerprint-error": (runtime, "repo_worktree_fingerprint"),
                }[scenario]
                patch.setattr(module, name, unavailable)
            runtime.handle_payload(event, {**pre, "hook_event_name": event, "is_interrupt": interrupted})
    state = runtime.read_session_state(target, session)
    assert state and state["write_observed"]
    stamp = state["last_write_at"]
    assert (stamp > before["last_write_at"]) is wrote
    assert stamp == state["last_attributed_write_at"] == state["last_verification_write_at"]
    assert "pending_bash_fingerprint" not in state and "pending_bash_started_at" not in state

    edit("other.py", "other-session")
    assert runtime._foreign_write_since(target, session, datetime.fromisoformat(stamp))
    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session, stop_hook_active=False))
    assert result
    assert (result.get("decision") == "block") is wrote
    if not wrote:
        from brigade.work_cmd.verify_gate import StopGate

        assert runtime._shared_tree_block_is_foreign(target, session, state, StopGate(target))


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
@pytest.mark.parametrize("integrity_error", [False, True])
def test_bash_post_surfaces_state_errors(target, monkeypatch, event, integrity_error):
    command = {"tool_name": "Bash", "tool_input": {"command": "touch own.py"}}
    assert runtime.handle_payload("PreToolUse", _payload(target, "PreToolUse", **command)) is None
    error_type = runtime.anchored_io.SessionIntegrityError if integrity_error else RuntimeError
    (target / "own.py").touch()

    def broken(*_args, **_kwargs):
        raise error_type("fixture state error")

    if integrity_error:
        monkeypatch.setattr(runtime.write_attribution, "bash_scopes", broken)
    else:
        monkeypatch.setattr(runtime, "_write_session_state_preserving_latch", broken)
    with pytest.raises(error_type, match="fixture state error"):
        runtime.handle_payload(event, _payload(target, event, **command))


def _mock_command_path_error(monkeypatch, winerror):
    original = Path.lstat

    def missing(path):
        if path.name == "unreadable.py":
            if winerror is None:
                raise PermissionError(errno.EACCES, "fixture path access error")
            error = FileNotFoundError(errno.ENOENT, "fixture Windows path error")
            error.winerror = winerror
            raise error
        return original(path)

    monkeypatch.setattr(Path, "lstat", missing)


@pytest.mark.parametrize("kind", ["component", "whole-path", "valid-component"])
def test_windows_command_path_limits_precede_filesystem_probe(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(command_tokens, "_WINDOWS", True)
    paths = []

    def missing(path):
        paths.append(path)
        raise FileNotFoundError(errno.ENOENT, "fixture missing path")

    monkeypatch.setattr(Path, "lstat", missing)
    raw = {
        "component": "🌱" * 128,
        "whole-path": "/".join(["d" * 200] * 164),
        "valid-component": "🌱" * 127 + "x",
    }[kind]
    with command_tokens.command_event():
        if kind == "valid-component":
            assert command_tokens.resolve_command_path(raw, tmp_path) == tmp_path / raw
            assert paths == [tmp_path / raw]
        else:
            with pytest.raises(command_tokens.UnreadableCommand):
                command_tokens.resolve_command_path(raw, tmp_path)
            assert paths == []


@pytest.mark.parametrize("mode", ["strict", "observe", "off"])
@pytest.mark.parametrize(
    "case",
    ["surrogate", "null", "long-component", "windows-missing-component", "path-io", "attribution", "known-verifier"],
)
def test_bash_pretooluse_unreadable_command_is_denied_only_in_strict(target, monkeypatch, mode, case):
    monkeypatch.setattr(runtime.work_mode, "resolve_mode", lambda _target: mode)
    long_component = "echo " + shlex.quote(str(target / ("x" * 300)))
    commands = {
        "surrogate": "pytest /\ud800",
        "null": "echo a\x00b",
        "long-component": long_component,
        "windows-missing-component": long_component,
        "path-io": "echo ./unreadable.py",
        "attribution": "echo done",
        "known-verifier": "pytest -q",
    }
    if case == "windows-missing-component":
        monkeypatch.setattr(command_tokens, "_WINDOWS", True)
    if case == "path-io":
        _mock_command_path_error(monkeypatch, winerror=None)
    if case in {"attribution", "known-verifier"}:

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("fixture attribution error")

        monkeypatch.setattr(runtime.write_attribution, "bash_scopes", unavailable)
    event = "PreToolUse"
    result = runtime.handle_payload(
        event, _payload(target, event, tool_name="Bash", tool_input={"command": commands[case]})
    )
    if mode != "strict":
        assert result is None
        return
    state = runtime.read_session_state(target, "session-1")
    assert state is None or not state.get("write_observed")
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    expected = (
        "The hook recognized a verification command but could not read it completely. "
        "Run the verification through `brigade work verify run`. "
        if case in {"surrogate", "known-verifier"}
        else "The hook could not read this command. "
    )
    assert output["permissionDecisionReason"] == (
        expected + "Split the command, or move the long content into a file and run the command using that file."
    )


def test_bash_hook_transport_denies_unreadable_verifier(target, capsys):
    event = "PreToolUse"
    payload = _payload(target, event, tool_name="Bash", tool_input={"command": "pytest /\ud800"})
    assert runtime.hook_run(event=event, package=runtime.PACKAGE_REF, stdin_text=json.dumps(payload)) == 0
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "brigade work verify run" in output["permissionDecisionReason"]


@pytest.mark.parametrize(
    "event, interrupted",
    [("PostToolUse", False), ("PostToolUseFailure", False), ("PostToolUseFailure", True)],
    ids=["success", "failure", "interrupt"],
)
@pytest.mark.parametrize(
    "scenario",
    [
        "unmatched",
        "duplicate",
        "overlap",
        "anonymous-overlap",
        "different-tool",
        "different-target",
        "read-only",
        "no-bash-events",  # No post event is sent in this control, regardless of the event parameter.
        "matched-write",
        "evicted",
        "evicted-at-stop",
        "legacy",
        "pending-only",
    ],
)
def test_bash_pending_events_at_stop(tmp_path, event, interrupted, scenario):
    target = _git_wired_claude(tmp_path)
    session_id = "pending-session"
    other = _wired_claude(tmp_path / "other") if scenario == "different-target" else target

    def bash(name, tool_use_id="tool-first", *, cwd=target, tool="Bash", command="custom-generator"):
        fields = {"tool_use_id": tool_use_id} if tool_use_id else {}
        return runtime.handle_payload(
            name,
            _payload(
                cwd,
                name,
                session_id=session_id,
                tool_name=tool,
                tool_input={"command": command},
                is_interrupt=interrupted,
                **fields,
            ),
        )

    def edit(filename, session):
        path = target / filename
        path.write_text("value = 1\n")
        runtime.handle_payload(
            "PostToolUse",
            _payload(target, "PostToolUse", session_id=session, tool_name="Write", tool_input={"file_path": str(path)}),
        )

    # The pending-only case has no previously observed write. Its receipt is real
    # session evidence, with a timestamp taken before the command starts.
    if scenario == "pending-only":
        runtime.handle_payload("SessionStart", _payload(target, "SessionStart", session_id=session_id))
        state = runtime.read_session_state(target, session_id)
        state["last_verification_write_at"] = localio.utc_now_iso()
        runtime.write_session_state(target, session_id, state)
    else:
        edit("earlier.py", session_id)
    before = _write_session_receipt(target, session_id, tree_fingerprint=localio.tree_fingerprint(target))
    original_stamp = before.get("last_write_at")
    command = "git status" if scenario in {"read-only", "duplicate"} else "custom-generator"
    command_id = None if scenario in {"anonymous-overlap", "legacy"} else "tool-first"
    if scenario == "legacy":
        # Pre-upgrade state stored only these two fields on disk.
        runtime.write_session_state(
            target,
            session_id,
            {
                **before,
                "pending_bash_fingerprint": runtime.repo_worktree_fingerprint(target),
                "pending_bash_started_at": localio.utc_now_iso(),
            },
        )
    elif scenario not in {"unmatched", "no-bash-events"}:
        assert bash("PreToolUse", command_id, command=command) is None
    if scenario in {"unmatched", "overlap", "anonymous-overlap", "matched-write", "legacy"}:
        (target / "owned.py").write_text("value = 2\n")
    if scenario in {"overlap", "anonymous-overlap"}:
        second_id = None if scenario == "anonymous-overlap" else "tool-second"
        assert bash("PreToolUse", second_id) is None
        if scenario == "anonymous-overlap":
            assert runtime.read_session_state(target, session_id)["last_write_at"] > original_stamp
        bash(event, command_id, command=command)
        if scenario == "overlap":
            assert set(runtime.read_session_state(target, session_id)["pending_bash_commands"]) == {"tool-second"}
        bash(event, second_id)
    elif scenario in {"evicted", "evicted-at-stop"}:
        for index in range(8):
            assert bash("PreToolUse", f"tool-{index}") is None
        assert set(runtime.read_session_state(target, session_id)["pending_bash_commands"]) == {
            f"tool-{index}" for index in range(8)
        }
        for index in range(8):
            bash(event, f"tool-{index}")
        if scenario == "evicted":
            bash(event, command_id)
    elif scenario not in {"no-bash-events", "pending-only"}:
        if scenario == "duplicate":
            bash(event, command_id, command=command)
            assert runtime.read_session_state(target, session_id)["last_write_at"] == original_stamp
        bash(
            event,
            command_id,
            command=command,
            tool="Glob" if scenario == "different-tool" else "Bash",
            cwd=other,
        )
    state = runtime.read_session_state(target, session_id)
    unresolved = scenario in {"different-tool", "different-target", "pending-only"}
    assert bool(state.get("pending_bash_commands")) is unresolved
    assert "pending_bash_fingerprint" not in state and "pending_bash_started_at" not in state
    blocked = scenario not in {"read-only", "no-bash-events"}
    if blocked and not unresolved:
        assert state["last_write_at"] > original_stamp
        assert state["last_write_at"] == state["last_attributed_write_at"] == state["last_verification_write_at"]
    else:
        assert state.get("last_write_at") == original_stamp

    edit("foreign.py", "later-writer")
    stop = _payload(target, "Stop", session_id=session_id)
    assert ((runtime.handle_payload("Stop", stop) or {}).get("decision") == "block") is blocked
    if unresolved:
        assert runtime.read_session_state(target, session_id)["pending_bash_commands"] == state["pending_bash_commands"]
        # A new receipt covers the unresolved start without consuming it.
        receipt = target / ".brigade" / "work" / "verify-runs" / f"run-{session_id}" / "receipt.json"
        saved = json.loads(receipt.read_text())
        saved["started_at"] = saved["completed_at"] = localio.utc_now_iso()
        saved["tree_fingerprint"] = localio.tree_fingerprint(target)
        receipt.write_text(json.dumps(saved) + "\n")
        assert (runtime.handle_payload("Stop", stop) or {}).get("decision") != "block"


@pytest.mark.parametrize("interleave", ["PreToolUse", "PostToolUse"])
def test_bash_pending_updates_merge_with_interleaved_events(tmp_path, monkeypatch, interleave):
    target = _git_wired_claude(tmp_path)
    session = "parallel-session"
    runtime.handle_payload("PostToolUse", _write_payload(target, session_id=session))
    _write_session_receipt(target, session, tree_fingerprint=localio.tree_fingerprint(target))

    def bash(event, tool_id):
        return runtime.handle_payload(
            event,
            _payload(
                target,
                event,
                session_id=session,
                tool_name="Bash",
                tool_use_id=tool_id,
                tool_input={"command": "custom-generator"},
            ),
        )

    save = runtime._write_session_state_preserving_latch
    calls = []

    def save_after_other_event(*args, **kwargs):
        if kwargs.get("pending_bash_update") is not None and not calls:
            calls.append(interleave)
            assert bash(interleave, "tool-second") is None
        return save(*args, **kwargs)

    if interleave == "PostToolUse":
        assert bash("PreToolUse", "tool-first") is None
        assert bash("PreToolUse", "tool-second") is None
        (target / "owned.py").write_text("value = 1\n")
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_write_session_state_preserving_latch", save_after_other_event)
        assert bash(interleave, "tool-first") is None
    assert calls == [interleave]

    state = runtime.read_session_state(target, session)
    if interleave == "PreToolUse":
        assert set(state["pending_bash_commands"]) == {"tool-first", "tool-second"}
        (target / "owned.py").write_text("value = 1\n")
        assert bash("PostToolUse", "tool-first") is None
        assert bash("PostToolUse", "tool-second") is None
    assert "pending_bash_commands" not in runtime.read_session_state(target, session)
    (target / "src.py").write_text("value = 2\n")
    runtime.handle_payload("PostToolUse", _write_payload(target, session_id="later-writer"))
    assert runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session))["decision"] == "block"


def _pending_case_event(repo, event, session="pending-review", **fields):
    return runtime.handle_payload(event, _payload(repo, event, session_id=session, **fields))


def _pending_case_write(repo, session="pending-review"):
    (repo / f"{session}.py").write_text("value = 1\n")
    _pending_case_event(repo, "PostToolUse", session, tool_name="Write", tool_input={"file_path": f"{session}.py"})


def _pending_case_start(repo, *, command="custom-generator", **fields):
    return _pending_case_event(repo, "PreToolUse", tool_name="Bash", tool_input={"command": command}, **fields)


def _pending_case_complete(repo, event="PostToolUse", *, command="custom-generator", **fields):
    return _pending_case_event(repo, event, tool_name="Bash", tool_input={"command": command}, **fields)


def _pending_case_verify(repo, monkeypatch):
    state = runtime.read_session_state(repo, "pending-review")
    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, state["session_fingerprint"])
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(repo),
                "--argv-json",
                json.dumps([sys.executable, "-c", "pass"]),
            ]
        )
        == 0
    )


@pytest.mark.parametrize("shape", ["missing-start", "legacy-future", "map-future", "malformed", "oversized"])
def test_stop_repairs_pending_bash_state_and_accepts_new_verification(tmp_path, monkeypatch, shape):
    repo = _git_wired_claude(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _pending_case_write(repo)
    state = runtime.read_session_state(repo, "pending-review")
    now = localio.utc_now()
    state["started_at"] = (now - timedelta(minutes=5)).isoformat()
    earlier = (now - timedelta(minutes=3)).isoformat()
    for field in ("last_write_at", "last_attributed_write_at", "last_verification_write_at"):
        state[field] = earlier
    runtime.write_session_state(repo, "pending-review", state)
    _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))

    baseline = runtime.repo_worktree_fingerprint(repo)
    assert baseline
    future = (now + timedelta(days=1)).isoformat()
    if shape in {"missing-start", "legacy-future"}:
        state["pending_bash_fingerprint"] = baseline
        if shape == "legacy-future":
            state["pending_bash_started_at"] = future
    elif shape == "malformed":
        state["pending_bash_commands"] = {
            "": [baseline, future],
            "missing-baseline": [None, future],
            "bad-pair": [baseline],
            "unreadable": [baseline, "unknown"],
        }
    elif shape == "oversized":
        state["pending_bash_commands"] = {
            f"tool-{index}": [baseline, (now - timedelta(minutes=2, seconds=9 - index)).isoformat()]
            for index in range(10)
        }
    else:
        state["pending_bash_commands"] = {"tool-future": [baseline, future]}
    runtime.write_session_state(repo, "pending-review", state)
    _pending_case_write(repo, "foreign-review")
    assert _pending_case_event(repo, "Stop")["decision"] == "block"

    repaired = runtime.read_session_state(repo, "pending-review")
    if shape == "oversized":
        assert set(repaired["pending_bash_commands"]) == {f"tool-{index}" for index in range(2, 10)}
        assert (
            repaired["last_write_at"] == repaired["last_verification_write_at"] == repaired["last_attributed_write_at"]
        )
        assert repaired["last_write_at"] > earlier
    else:
        if shape == "malformed":
            assert set(repaired["pending_bash_commands"]) == {"unreadable"}
        if shape in {"missing-start", "legacy-future"}:
            clamped = repaired["pending_bash_started_at"]
        else:
            ((_, clamped),) = repaired["pending_bash_commands"].values()
        assert now <= datetime.fromisoformat(clamped) <= localio.utc_now()

    monkeypatch.setenv(runtime.CLAUDE_SESSION_ENV, repaired["session_fingerprint"])
    assert (
        cli.main(
            [
                "work",
                "verify",
                "run",
                "--target",
                str(repo),
                "--argv-json",
                json.dumps([sys.executable, "-c", "pass"]),
            ]
        )
        == 0
    )
    assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
@pytest.mark.parametrize(
    "order",
    [
        "same-id",
        "completion-first",
        "id-at-start",
        "id-at-completion",
        "late-after-block",
        "late-after-receipt",
        "late-read-after-receipt",
        "shared-subagent-write",
        "shared-subagent-read",
    ],
)
def test_bash_pending_event_orders_at_stop(tmp_path, monkeypatch, order, event):
    repo = _git_wired_claude(tmp_path)
    _pending_case_write(repo)
    before = _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))
    read_only = order in {"shared-subagent-read", "late-read-after-receipt"}
    command = "git status" if read_only else "custom-generator"
    first = {} if order == "id-at-completion" else {"tool_use_id": "tool-first"}
    last = {} if order == "id-at-start" else {"tool_use_id": "tool-first"}
    if order.startswith("shared-subagent"):
        first.update(agent_id="fixture-child", agent_type="fixture")
        last.update(agent_id="fixture-child", agent_type="fixture")
        assert _pending_case_start(repo, tool_use_id="tool-parent") is None
    if order == "completion-first":
        assert _pending_case_complete(repo, event, command=command, **last) is None
        assert runtime.read_session_state(repo, "pending-review")["last_write_at"] > before["last_write_at"]
    assert _pending_case_start(repo, command=command, **first) is None
    if order == "same-id":
        assert _pending_case_start(repo, command=command, **first) is None
        assert runtime.read_session_state(repo, "pending-review")["last_write_at"] > before["last_write_at"]
    if order == "late-after-block":
        assert _pending_case_event(repo, "Stop")["decision"] == "block"
    if order in {"late-after-receipt", "late-read-after-receipt"}:
        _pending_case_verify(repo, monkeypatch)
        assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"

    if not read_only:
        (repo / "owned.py").write_text("value = 2\n")
    _pending_case_event(repo, "PreToolUse", tool_name="Write", tool_input={"file_path": "other.py"})
    assert _pending_case_complete(repo, event, command=command, **last) is None
    after = runtime.read_session_state(repo, "pending-review")
    if order.startswith("shared-subagent"):
        assert set(after["pending_bash_commands"]) == {"tool-parent"}
        assert _pending_case_complete(repo, event, tool_use_id="tool-parent") is None
    if not read_only:
        assert after["last_write_at"] > before["last_write_at"]
        assert "pending_write_at" not in after
    else:
        assert after["last_write_at"] == before["last_write_at"]
        assert "pending_write_at" in after
    state = runtime.read_session_state(repo, "pending-review")
    assert not any(key.startswith("pending_bash_") for key in state)
    _pending_case_write(repo, "foreign-review")
    assert ((_pending_case_event(repo, "Stop") or {}).get("decision") == "block") is (not read_only)


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
@pytest.mark.parametrize("identity", ["id-on-start", "id-on-completion", "matched"])
def test_bash_completion_repairs_observation_only_repository(tmp_path, identity, event):
    repo = _git_wired_claude(tmp_path / "origin")
    observed = _git_wired_claude(tmp_path / "observation")
    for target in (repo, observed):
        _pending_case_write(target)
        _write_session_receipt(target, "pending-review", tree_fingerprint=localio.tree_fingerprint(target))
    command = {"tool_name": "Bash", "tool_input": {"command": f"cd {shlex.quote(observed.as_posix())} && git status"}}
    identified = {"tool_use_id": "tool-first"}
    pre = _payload(repo, "PreToolUse", session_id="pending-review", **command)
    primary = runtime.effective_hook_target(pre)
    assert primary in (repo, observed)
    secondary = observed if primary == repo else repo
    assert (
        _pending_case_event(repo, "PreToolUse", **command, **({} if identity == "id-on-completion" else identified))
        is None
    )
    assert any(key.startswith("pending_bash_") for key in runtime.read_session_state(secondary, "pending-review"))
    assert _pending_case_event(repo, event, **command, **({} if identity == "id-on-start" else identified)) is None
    for target in (repo, observed):
        state = runtime.read_session_state(target, "pending-review")
        assert not any(key.startswith("pending_bash_") for key in state)
        _pending_case_write(target, "foreign-review")
    assert ((_pending_case_event(repo, "Stop") or {}).get("decision") == "block") is (identity != "matched")


@pytest.mark.parametrize("source", ["resume", "compact", "clear", "startup"])
def test_session_start_preserves_pending_bash_only_within_task(tmp_path, monkeypatch, source):
    repo = _git_wired_claude(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(runtime, "_run_brief", lambda *_args: "brief")
    _pending_case_event(repo, "SessionStart", source="startup")
    _pending_case_write(repo)
    _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))
    assert _pending_case_start(repo, tool_use_id="tool-first") is None
    before = runtime.read_session_state(repo, "pending-review")
    if source == "compact":
        assert _pending_case_event(repo, "PreCompact", trigger="auto") is None
        assert (
            runtime.read_session_state(repo, "pending-review")["pending_bash_commands"]
            == before["pending_bash_commands"]
        )
    _pending_case_event(repo, "SessionStart", source=source)
    continuing = source in {"resume", "compact"}
    after = runtime.read_session_state(repo, "pending-review")
    assert after.get("pending_bash_commands") == (before["pending_bash_commands"] if continuing else None)
    _pending_case_write(repo, "foreign-review")
    assert ((_pending_case_event(repo, "Stop") or {}).get("decision") == "block") is continuing


@pytest.mark.parametrize(
    "shape",
    ["bad-pair", "null-map", "linked-null-map", "bad-legacy", "legacy-start-only", "empty-map", "absent"],
)
def test_pending_bash_structural_repair_stamps_once(tmp_path, monkeypatch, shape):
    repo = _git_wired_claude(tmp_path)
    session = "pending-review"
    stop_repo = _git_wired_claude(tmp_path / "primary") if shape == "linked-null-map" else repo
    if stop_repo != repo:
        _pending_case_write(stop_repo)
        _write_session_receipt(stop_repo, session, tree_fingerprint=localio.tree_fingerprint(stop_repo))
        command = f"cd {shlex.quote(repo.as_posix())} && git status"
        assert _pending_case_start(stop_repo, command=command, tool_use_id="link") is None
        assert _pending_case_complete(stop_repo, command=command, tool_use_id="link") is None
    _pending_case_write(repo)
    raw = runtime.read_session_state(repo, session)
    now = localio.utc_now()
    raw["started_at"] = (now - timedelta(minutes=5)).isoformat()
    earlier = (now - timedelta(minutes=3)).isoformat()
    fields = ("last_write_at", "last_attributed_write_at", "last_verification_write_at")
    raw.update(dict.fromkeys(fields, earlier))
    runtime.write_session_state(repo, session, raw)
    _write_session_receipt(repo, session, tree_fingerprint=localio.tree_fingerprint(repo))
    if shape == "bad-pair":
        raw["pending_bash_commands"] = {"only-bad-pair": [raw["repo_fingerprint"]]}
    elif shape in {"null-map", "linked-null-map"}:
        raw["pending_bash_commands"] = None
    elif shape == "bad-legacy":
        raw["pending_bash_fingerprint"] = ""
        raw["pending_bash_started_at"] = None
    elif shape == "legacy-start-only":
        raw["pending_bash_started_at"] = (now - timedelta(minutes=4)).isoformat()
    elif shape == "empty-map":
        raw["pending_bash_commands"] = {}
    runtime.write_session_state(repo, session, raw)
    _pending_case_write(repo, "foreign-review")
    repaired = shape not in {"empty-map", "absent"}
    assert ((_pending_case_event(stop_repo, "Stop") or {}).get("decision") == "block") is repaired
    first = runtime.read_session_state(repo, session)
    stamps = [first[field] for field in fields]
    assert stamps == [stamps[0]] * 3
    assert (stamps[0] > earlier) is repaired
    assert "pending_bash_commands" not in first
    if shape == "legacy-start-only":
        assert first["pending_bash_fingerprint"] == runtime._UNAVAILABLE_FINGERPRINT
        assert first["pending_bash_started_at"] == raw["pending_bash_started_at"]
    assert ((_pending_case_event(stop_repo, "Stop") or {}).get("decision") == "block") is repaired
    second = runtime.read_session_state(repo, session)
    assert [second[field] for field in fields] == stamps
    if repaired:
        _pending_case_verify(repo, monkeypatch)
        assert (_pending_case_event(stop_repo, "Stop") or {}).get("decision") != "block"


@pytest.mark.parametrize("linked", [False, True], ids=["one-worktree", "two-worktrees"])
def test_anonymous_bash_completion_preserves_multiple_keyed_starts(tmp_path, monkeypatch, linked):
    repo = _git_wired_claude(tmp_path / "origin")
    other = _git_wired_claude(tmp_path / "observation") if linked else None
    targets = [repo, other] if other else [repo]
    earlier = {}
    for target in targets:
        _pending_case_write(target)
        earlier[target] = _write_session_receipt(
            target, "pending-review", tree_fingerprint=localio.tree_fingerprint(target)
        )["last_write_at"]
    command = f"cd {shlex.quote(other.as_posix())} && git status" if other else "git status"
    assert _pending_case_start(repo, command=command, tool_use_id="one") is None
    assert _pending_case_start(repo, tool_use_id="two") is None
    assert _pending_case_complete(repo, command=command) is None
    for target in targets:
        state = runtime.read_session_state(target, "pending-review")
        assert set(state.get("pending_bash_commands", {})) == ({"one", "two"} if target == repo else set())
        assert state["last_verification_write_at"] > earlier[target]
        _pending_case_write(target, "foreign-review")
    assert _pending_case_event(repo, "Stop")["decision"] == "block"
    for target in targets:
        _pending_case_verify(target, monkeypatch)
    assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"


@pytest.mark.parametrize("identified", [True, False], ids=["identified", "anonymous"])
def test_mixed_pending_formats_complete_independently(tmp_path, identified):
    repo = _git_wired_claude(tmp_path)
    _pending_case_write(repo)
    before = _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))
    assert _pending_case_start(repo) is None
    assert _pending_case_start(repo, tool_use_id="keyed") is None
    assert _pending_case_complete(repo, **({"tool_use_id": "keyed"} if identified else {})) is None
    state = runtime.read_session_state(repo, "pending-review")
    assert state["last_write_at"] == before["last_write_at"]
    assert ("pending_bash_fingerprint" in state) is identified
    assert ("pending_bash_commands" in state) is not identified
    _pending_case_write(repo, "foreign-review")
    assert _pending_case_event(repo, "Stop")["decision"] == "block"
    assert _pending_case_complete(repo, **({} if identified else {"tool_use_id": "keyed"})) is None
    assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"


@pytest.mark.parametrize("repair_event", ["Stop", "PostToolUse"])
def test_oversized_bash_map_clamps_before_keep_newest(tmp_path, monkeypatch, repair_event):
    repo = _git_wired_claude(tmp_path)
    _pending_case_write(repo)
    before = _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))
    now = localio.utc_now()
    baseline = before["repo_fingerprint"]
    before["pending_bash_commands"] = {
        f"tool-{i}": [baseline, (now - timedelta(minutes=1, seconds=9 - i)).isoformat()] for i in range(10)
    }
    before["pending_bash_commands"]["tool-0"][1] = (now + timedelta(days=1)).isoformat()
    before["pending_bash_commands"]["tool-1"][1] = "unreadable"
    runtime.write_session_state(repo, "pending-review", before)
    _pending_case_write(repo, "foreign-review")
    if repair_event == "Stop":
        assert _pending_case_event(repo, "Stop")["decision"] == "block"
    else:
        assert _pending_case_event(repo, repair_event, tool_name="Glob") is None
    repaired = runtime.read_session_state(repo, "pending-review")
    commands = repaired["pending_bash_commands"]
    assert set(commands) == {"tool-0", "tool-1", *(f"tool-{i}" for i in range(4, 10))}
    for key in ("tool-0", "tool-1"):
        assert now <= datetime.fromisoformat(commands[key][1]) <= localio.utc_now()
    assert repaired["last_verification_write_at"] > before["last_verification_write_at"]
    assert _pending_case_event(repo, "Stop")["decision"] == "block"
    _pending_case_verify(repo, monkeypatch)
    assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"


@pytest.mark.parametrize("stamp", ["unreadable", "future"])
def test_producer_treats_repaired_bash_start_as_in_flight(tmp_path, monkeypatch, stamp):
    repo = _git_wired_claude(tmp_path)
    _pending_case_write(repo)
    raw = _write_session_receipt(repo, "pending-review", tree_fingerprint=localio.tree_fingerprint(repo))
    now = localio.utc_now()
    value = (now + timedelta(days=1)).isoformat() if stamp == "future" else "unreadable"
    raw["pending_bash_commands"] = {"tool-first": [raw["repo_fingerprint"], value]}
    runtime.write_session_state(repo, "pending-review", raw)
    assert runtime.producer_session._bash_in_flight(raw, now)
    _pending_case_write(repo, "foreign-review")
    assert _pending_case_event(repo, "Stop")["decision"] == "block"
    repaired = runtime.read_session_state(repo, "pending-review")
    assert runtime.producer_session._bash_in_flight(repaired, localio.utc_now())
    assert not runtime.producer_session._bash_in_flight(repaired, now + timedelta(hours=2))
    _pending_case_verify(repo, monkeypatch)
    assert (_pending_case_event(repo, "Stop") or {}).get("decision") != "block"
