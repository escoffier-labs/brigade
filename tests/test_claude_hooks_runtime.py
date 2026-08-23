"""RED: Claude work-loop hook runtime behavior (issue #249)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from brigade import cli, localio
from brigade.claude_hooks import envelope
from brigade.claude_hooks.package import PACKAGE_REF
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
            graphtrail_delta_timeout_seconds=cfg.graphtrail_delta_timeout_seconds,
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
            graphtrail_delta_timeout_seconds=cfg.graphtrail_delta_timeout_seconds,
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

    def fake_recall(_target: Path, payload: dict) -> str:
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
    assert "--capture brigade-work" in reason
    assert "python -m pytest -q" in reason


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
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=reader_session, stop_hook_active=False))
        is None
    )


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
    from brigade.work_cmd.verification import _tree_fingerprint

    receipt_tree = _tree_fingerprint(target)
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
    assert _tree_fingerprint(target) != receipt_tree

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
    from brigade.work_cmd.verification import _tree_fingerprint

    receipt_tree = _tree_fingerprint(target)
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
    assert _tree_fingerprint(target) != receipt_tree
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
    fingerprint_calls = 0

    def sequenced_fingerprint(repo: Path) -> str | None:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return "baseline" if fingerprint_calls < 4 else None

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
    handoff.write_text("durable finding\n")
    command = "printf '%s\\n' finding >> .claude/memory-handoffs/handoff.md"
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
    assert updated["last_write_at"] >= updated["last_verification_write_at"]
    assert updated["last_verification_write_at"] == state["last_verification_write_at"]
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False)) is None
    )


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
    artifact = target / ".brigade" / "runs" / "detached-1" / "run.json"
    runtime.handle_payload(
        "PostToolUse",
        _payload(
            target,
            "PostToolUse",
            session_id=session_id,
            tool_name="Bash",
            tool_input={"command": f"mkdir -p {artifact.parent} && touch {artifact}"},
        ),
    )

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


def test_repo_worktree_fingerprint_hashes_large_untracked_without_read_bytes(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    large = target / "model.cache"
    with large.open("wb") as handle:
        handle.seek(100 * 1024 * 1024 - 1)
        handle.write(b"\0")
    hash_calls: list[str] = []
    real_run = runtime._run_snapshot_git

    def tracked_run(repo: Path, *git_args: str):
        if git_args[:1] == ("hash-object",):
            hash_calls.append(git_args[-1])
        return real_run(repo, *git_args)

    def forbid_read_bytes(self: Path, *args, **kwargs):
        raise AssertionError("repo_worktree_fingerprint must not read whole file bytes in-process")

    monkeypatch.setattr(runtime, "_run_snapshot_git", tracked_run)
    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)

    fingerprint = runtime.repo_worktree_fingerprint(target)

    assert fingerprint is not None
    assert "model.cache" in hash_calls


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


def test_repo_worktree_fingerprint_returns_none_when_hash_object_fails_for_untracked(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    (target / "new.txt").write_text("content")
    real_run = runtime._run_snapshot_git

    def fake_run(repo: Path, *git_args: str):
        if git_args[:1] == ("hash-object",):
            return None
        return real_run(repo, *git_args)

    monkeypatch.setattr(runtime, "_run_snapshot_git", fake_run)
    assert runtime.repo_worktree_fingerprint(target) is None


def test_posttooluse_fails_closed_when_untracked_state_check_fails(tmp_path: Path, monkeypatch):
    target = _git_wired_claude(tmp_path)
    session_id = "hash-object-fail"
    out_file = target / "new.txt"
    out_file.write_text("before")
    real_run = runtime._run_snapshot_git

    def fake_run(repo: Path, *git_args: str):
        if git_args[:1] == ("hash-object",):
            return None
        return real_run(repo, *git_args)

    monkeypatch.setattr(runtime, "_run_snapshot_git", fake_run)
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
    target = _wired_claude(tmp_path)
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
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False)) is None
    )


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
    from brigade.work_cmd.verification import _tree_fingerprint

    receipt_tree = _tree_fingerprint(target)
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
    from brigade.work_cmd.verification import _tree_fingerprint

    receipt_tree = _tree_fingerprint(target)
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
    env[runtime._HOOK_TEST_HANG_ENV] = "30"
    env[runtime._HOOK_TIMEOUT_ENV] = "0.25"
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
        env=env,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert completed.returncode == 0
    assert elapsed < 2.0
    assert json.loads(completed.stdout) == envelope.degraded_envelope("PreToolUse")
    assert "timed out" in envelope.log_path(target).read_text(encoding="utf-8")
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


def test_posttooluse_read_skill_sets_exercised_artifact(tmp_path: Path):
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
    assert state["exercised_artifact_id"] == "taste"


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


def test_verify_replacement_uses_session_exercised_artifact(tmp_path: Path):
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
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--capture taste" in reason
    assert "--capture brigade-work" not in reason


def test_verify_replacement_falls_back_to_brigade_work(tmp_path: Path):
    target = _wired_claude(tmp_path)
    result = runtime.handle_payload(
        "PreToolUse",
        _payload(target, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"}),
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--capture brigade-work" in reason


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
