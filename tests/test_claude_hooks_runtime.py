"""RED: Claude work-loop hook runtime behavior (issue #249)."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from brigade import cli, localio
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
    assert "work brief: test" in first["hookSpecificOutput"]["additionalContext"]
    assert second is None
    assert calls == [target.resolve()]


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


def test_posttooluse_snapshot_fails_open_when_state_cannot_be_inspected(tmp_path: Path, monkeypatch):
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
    assert runtime.read_session_state(target, session_id).get("pending_bash_fingerprint") is None

    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is False
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


def test_posttooluse_does_not_record_bash_write_when_hash_object_fails_for_untracked(tmp_path: Path, monkeypatch):
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
    assert "pending_bash_fingerprint" not in state

    out_file.write_text("after")
    succeeded = {**pretool, "hook_event_name": "PostToolUse"}
    assert runtime.handle_payload("PostToolUse", succeeded) is None
    assert runtime.read_session_state(target, session_id)["write_observed"] is False
    assert (
        runtime.handle_payload("Stop", _payload(target, "Stop", session_id=session_id, stop_hook_active=False)) is None
    )


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

    assert cli.main(["work", "hook-run", "--event", "PostToolUse", "--package", "brigade-claude-work-loop@1.0.0"]) == 0
    assert calls == [("PostToolUse", "brigade-claude-work-loop@1.0.0")]


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


def test_stop_accepts_failed_or_rejected_routed_receipt_and_nudges_handoff(tmp_path: Path, monkeypatch):
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
                "harness_session": {
                    "harness": "claude",
                    "fingerprint": state["session_fingerprint"],
                },
            }
        )
        + "\n"
    )

    result = runtime.handle_payload("Stop", _payload(target, "Stop", session_id="receipt", stop_hook_active=False))
    assert "decision" not in result
    assert "Memory Handoff" in result["hookSpecificOutput"]["additionalContext"]


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
            package="brigade-claude-work-loop@1.0.0",
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
