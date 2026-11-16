"""Recover partial writes and interrupt reviews through installed Codex hooks."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from brigade import lesson_activity as activity, lesson_io as io, lesson_review as review
from tests.test_lesson_review import event, proposal, seat
from tests.test_lesson_review_r4 import scope as scope


@pytest.mark.parametrize("offset", [0, 1, 10, 100])
@pytest.mark.parametrize("destination", ["outbox", "handoff-inbox"])
def test_process_exit_during_exclusive_write_recovers(scope, monkeypatch, offset, destination):
    event(scope)
    event(scope)
    calls = seat(monkeypatch, proposal(scope))
    write = io.write
    interrupted = []
    build = review.drafts.build_draft
    drafts = []

    def timestamped_draft(**kwargs):
        staged = build(**kwargs)
        old = Path(staged["path"])
        # Force a different timestamp on retry without depending on wall time.
        name = f"20260101-00000{len(drafts)}-" + old.name.split("-", 2)[2]
        path = old.with_name(name)
        old.rename(path)
        drafts.append(path)
        return {**staged, "path": str(path)}

    monkeypatch.setattr(review.drafts, "build_draft", timestamped_draft)

    def crash(path, data, *, exclusive=False):
        if exclusive and path.parent.name == destination and not interrupted:
            interrupted.append(path)
            script = """
import os
import sys
from pathlib import Path
from brigade import lesson_io as io
fdopen = os.fdopen
class Interrupted:
    def __init__(self, fd, *args):
        self.handle = fdopen(fd, *args)
    def __enter__(self):
        return self
    def write(self, data):
        self.handle.write(data[:int(sys.argv[2])])
        self.handle.flush()
        os.fsync(self.handle.fileno())
        os._exit(91)
    def __exit__(self, *args):
        self.handle.close()
io.os.fdopen = Interrupted
io.write(Path(sys.argv[1]), sys.stdin.buffer.read(), exclusive=True)
"""
            result = subprocess.run([sys.executable, "-c", script, str(path), str(offset)], input=data)
            assert result.returncode == 91
            assert not path.exists()
            temporaries = list(path.parent.glob(".lesson-*.tmp"))
            assert len(temporaries) == 1
            assert temporaries[0].read_bytes() == data[:offset]
            if os.name == "posix":
                assert temporaries[0].stat().st_mode & 0o077 == 0
            raise OSError("interrupted exclusive write")
        return write(path, data, exclusive=exclusive)

    monkeypatch.setattr(io, "write", crash)
    assert review.run(scope, now=111)["status"] == "retry"
    assert len(interrupted) == 1
    assert review.run(scope, now=500)["status"] == "committed"
    assert "quarantine" not in activity.load(scope)
    assert not list(interrupted[0].parent.glob(".lesson-*.tmp"))
    assert len(calls) == 2
    assert len(list((scope / activity.ROOT / "outbox/processed").glob("*.md"))) == 1
    copies = list((scope / "memory/handoff-inbox").glob("lesson-*.md"))
    assert len(copies) == 1
    assert io.read_bytes(copies[0]) == io.read_bytes(next((scope / activity.ROOT / "outbox/processed").glob("*.md")))


def test_exclusive_write_never_replaces_existing_file(tmp_path):
    path = tmp_path / "complete.md"
    io.write(path, b"original", exclusive=True)
    with pytest.raises(FileExistsError):
        io.write(path, b"replacement", exclusive=True)
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".lesson-*.tmp"))


@pytest.mark.parametrize("phase", ["lesson-select", "lesson-propose"])
def test_installed_codex_prompt_interrupts_publication(scope, monkeypatch, capsys, phase):
    from brigade import cli, codex_observe_hooks, observed_checks
    from tests.test_work_mode import _wired, _set_mode

    monkeypatch.setattr(observed_checks, "_capture_clock", lambda: 0.0)
    target = _wired(scope, harnesses=("codex",))
    _set_mode(target, "observe", capsys)
    # The wired workspace is nested inside the lesson fixture.
    for relative in (
        ".brigade/lesson-review.json",
        ".brigade/receipt-signing-key",
        ".brigade/work/current",
        ".brigade/roster.toml",
    ):
        io.write(target / relative, (scope / relative).read_bytes())
    assert codex_observe_hooks.codex_observe_hooks_installed(target)
    hooks = json.loads((target / ".codex/hooks.json").read_text())["hooks"]
    handler = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert handler["async"] is True
    event(target)
    event(target)
    seat(monkeypatch, proposal(target))
    invoke = review._invoke

    def interrupt(*args, **kwargs):
        response = invoke(*args, **kwargs)
        if args[-1] == phase:
            import io as streams

            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "example-session",
                "cwd": str(target),
                "prompt": "Correction: retain the current evidence before publication.",
            }
            monkeypatch.setattr("sys.stdin", streams.StringIO(json.dumps(payload)))
            assert cli.main(shlex.split(handler["command"])[1:]) == 0
            assert not observed_checks.read_checks(target)[0]
        return response

    monkeypatch.setattr(review, "_invoke", interrupt)
    assert review.run(target, now=111)["status"] == "interrupted"
    state = activity.load(target)
    assert state["reviewed"] == 0
    assert state["events"][-1]["kind"] == "UserPromptSubmit"
    assert not list((target / "memory/handoff-inbox").glob("lesson-*.md"))
    assert not list((target / activity.ROOT / "outbox").glob("*.md"))
