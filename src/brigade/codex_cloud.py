"""Codex Cloud adapter: submit a task, poll to a terminal state, return the result.

A roster seat references it as ``cli = "codex-cloud:<env-id>"`` (environment ids
come from the Codex Cloud workspace; browse them with ``codex cloud``). The
worker's text is the task's final status plus its unified diff. The diff is
NEVER applied locally; apply it deliberately with ``codex cloud apply <task-id>``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import proc

SUBMIT_TIMEOUT = 120.0
POLL_TIMEOUT = 60.0
DIFF_TIMEOUT = 120.0
POLL_INTERVAL = 15.0
DIFF_CAP = 20_000

# Status keywords scanned (word-bounded, case-insensitive) in `codex cloud status`.
TERMINAL_OK = ("ready", "completed", "succeeded", "applied", "finished")
TERMINAL_FAIL = ("failed", "errored", "error", "cancelled", "canceled", "expired")

_ID_PATTERNS = (
    re.compile(r"https?://\S*/tasks?/([A-Za-z0-9_-]+)"),
    re.compile(r"task[\s_-]*id[:=\s]+([A-Za-z0-9_-]{6,})", re.I),
    re.compile(r"\b(task_[A-Za-z0-9-]{4,})\b"),
)


def parse_task_id(text: str) -> str | None:
    for pat in _ID_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    token = text.strip()
    if token and "\n" not in token and " " not in token and 6 <= len(token) <= 80:
        return token
    return None


def _scan_status(text: str) -> str | None:
    """Return the terminal status keyword from status output.

    The installed CLI prints `[STATUS] <task title>` as the first line, so a
    leading bracket token is authoritative when present. Otherwise scan only
    status-shaped lines (`Status: ...`), so incidental words in a task title
    ("fix failed tests") cannot terminate polling. Whole-text scanning is the
    last resort for unrecognized formats.
    """
    brackets = re.findall(r"^\s*\[([A-Za-z_ -]+)\]", text, re.M)
    if brackets:
        scope = "\n".join(brackets)
    else:
        status_lines = [line for line in text.splitlines() if re.match(r"\s*(task\s+)?(status|state)\b", line, re.I)]
        scope = "\n".join(status_lines) if status_lines else text
    lowered = scope.lower()
    for word in TERMINAL_FAIL + TERMINAL_OK:
        if re.search(rf"\b{word}\b", lowered):
            return word
    return None


def run_cloud_task(
    prompt: str,
    *,
    env_id: str,
    timeout: float,
    cwd: Path | None = None,
    attempts: int = 1,
    branch: str | None = None,
    poll_interval: float = POLL_INTERVAL,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """Submit, poll, and collect one Codex Cloud task. Mirrors run_agent's contract."""
    from .agents import AgentResult

    argv = ["codex", "cloud", "exec", "--env", env_id]
    if attempts > 1:
        argv += ["--attempts", str(attempts)]
    if branch:
        argv += ["--branch", branch]
    argv.append(prompt)

    deadline = clock() + timeout
    submit = proc.run(argv, timeout=min(timeout, SUBMIT_TIMEOUT), cwd=cwd)
    if submit.code != 0:
        detail = submit.stderr.strip() or submit.stdout.strip() or f"exit {submit.code}"
        return AgentResult(text="", ok=False, detail=f"cloud submit failed: {detail}"[:200])

    task_id = parse_task_id(submit.stdout) or parse_task_id(submit.stderr)
    if task_id is None:
        head = submit.stdout.strip()[:150]
        return AgentResult(text="", ok=False, detail=f"could not parse cloud task id from: {head}")

    def remaining(floor: float = 5.0) -> float:
        return max(floor, deadline - clock())

    status_text = ""
    status_word = None
    while True:
        st = proc.run(
            ["codex", "cloud", "status", task_id],
            timeout=min(POLL_TIMEOUT, remaining()),
            cwd=cwd,
        )
        status_text = (st.stdout + "\n" + st.stderr).strip()
        if st.code == 0:
            status_word = _scan_status(status_text)
            if status_word in TERMINAL_FAIL:
                return AgentResult(
                    text=status_text[:DIFF_CAP],
                    ok=False,
                    detail=f"cloud task {task_id} {status_word}"[:200],
                    thread_id=task_id,
                    status=status_word,
                )
            if status_word in TERMINAL_OK:
                break
        if clock() >= deadline:
            return AgentResult(
                text=status_text[:DIFF_CAP],
                ok=False,
                detail=(
                    f"cloud task {task_id} still pending after {int(timeout)}s; check `codex cloud status {task_id}`"
                )[:200],
                thread_id=task_id,
                status="pending",
            )
        sleep(poll_interval)

    diff = proc.run(
        ["codex", "cloud", "diff", task_id],
        timeout=min(DIFF_TIMEOUT, remaining(floor=30.0)),
        cwd=cwd,
    )
    parts = [f"codex cloud task {task_id} [{status_word}]", status_text]
    if diff.code != 0:
        err = diff.stderr.strip() or f"exit {diff.code}"
        parts.append(f"WARNING: `codex cloud diff {task_id}` failed: {err[:300]}")
    elif diff.stdout.strip():
        parts += [
            f"Unified diff (NOT applied locally; apply with `codex cloud apply {task_id}`):",
            diff.stdout.strip()[:DIFF_CAP],
        ]
    else:
        parts.append("No diff produced (research or no-change task).")
    text = "\n\n".join(p for p in parts if p)
    return AgentResult(text=text, ok=True, thread_id=task_id, status=status_word or "")
