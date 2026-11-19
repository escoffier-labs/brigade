"""Hook session as the producer of a manifest verify trial (#2589).

Ordinary Claude Code sessions never run ``brigade work start``, so a manifest
verify receipt had no producer session and was ineligible
(``producer_session_missing``). The hook already keeps per-session state, so
this module lets that state stand in for a work session:

* when the hook first creates a session's state it records the dirty files of the
  target in the work-session shape, ``start.git.dirty_files`` (``stamp_start``);
* a verify that resolves exactly one Claude session with such state names it as
  ``claude-hook:<fingerprint>`` and reads its start snapshot back as a session
  payload (``resolve_producer``, ``load_payload``).

The id has a prefix a work session id can never carry (those start with a
timestamp), so the two never collide. The start snapshot is bounded and fails
open: when git errors, times out, or reports more than the caps allow, nothing is
recorded and that session simply cannot produce trials. A snapshot is never
truncated, because a partial list could hide a dirty subject.

The state is caller-side and forgeable like the rest of the hook state. Every
ownership check in ``verify_trial`` still runs against it.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import agent_identity, outcome_attribution

HOOK_PRODUCER_PREFIX = "claude-hook:"
START_KEY = "start"
GIT_TIMEOUT_SECONDS = 2.0
MAX_DIRTY_LINES = 512
MAX_DIRTY_LINE_CHARS = 1024
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


def producer_id(fingerprint: str) -> str:
    return f"{HOOK_PRODUCER_PREFIX}{fingerprint}"


def fingerprint_from_producer_id(session_id: object) -> str | None:
    """The session fingerprint a hook producer id names, or ``None`` for any other id."""
    if not isinstance(session_id, str) or not session_id.startswith(HOOK_PRODUCER_PREFIX):
        return None
    fingerprint = session_id[len(HOOK_PRODUCER_PREFIX) :]
    return fingerprint if _FINGERPRINT.fullmatch(fingerprint) else None


def same_session(producer_session: object, verifier_session: object) -> bool:
    """Whether a producer id and a verifier id name one session.

    A verify run names its Claude session by bare fingerprint while the producer
    is ``claude-hook:<fingerprint>``, so a plain equality test would call a
    session's own verify independent.
    """
    if not isinstance(producer_session, str) or not isinstance(verifier_session, str):
        return False
    if not producer_session or not verifier_session:
        return False
    bare = fingerprint_from_producer_id(producer_session)
    return producer_session == verifier_session or (bare is not None and bare == verifier_session)


def _valid_dirty_files(value: object) -> list[str] | None:
    if not isinstance(value, list) or len(value) > MAX_DIRTY_LINES:
        return None
    if not all(isinstance(line, str) and len(line) <= MAX_DIRTY_LINE_CHARS for line in value):
        return None
    return list(value)


def _start_dirty_files(start: object) -> list[str] | None:
    git = start.get("git") if isinstance(start, Mapping) else None
    return _valid_dirty_files(git.get("dirty_files")) if isinstance(git, Mapping) else None


def snapshot_dirty_files(target: Path) -> list[str] | None:
    """One bounded ``git status --short``, split into lines. ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(target), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return _valid_dirty_files(result.stdout.splitlines())


def stamp_start(state: dict[str, Any], target: Path) -> None:
    """Record the start-of-session dirty files on a freshly created state."""
    state.pop(START_KEY, None)
    dirty_files = snapshot_dirty_files(target)
    if dirty_files is not None:
        state[START_KEY] = {"git": {"dirty_files": dirty_files}}


def restore_start(state: dict[str, Any], persisted: Mapping[str, Any]) -> None:
    """Carry a valid recorded snapshot through state normalization unchanged."""
    state.pop(START_KEY, None)
    dirty_files = _start_dirty_files(persisted.get(START_KEY))
    if dirty_files is not None:
        state[START_KEY] = {"git": {"dirty_files": dirty_files}}


def load_payload(target: Path, session_id: str) -> dict[str, Any] | None:
    """Resolve ``claude-hook:<fingerprint>`` to a work-session-shaped payload.

    Reads only this target's hook state, never the environment, so a later
    scoring pass resolves it again from the receipt alone. Exactly one state may
    match the fingerprint and it must carry a valid start snapshot, otherwise
    there is no producer.
    """
    fingerprint = fingerprint_from_producer_id(session_id)
    if fingerprint is None:
        return None
    resolved = target.expanduser().resolve()
    matches = [
        state
        for state in outcome_attribution._iter_states(resolved)
        if outcome_attribution._state_matches(state, fingerprint)
    ]
    if len(matches) != 1:
        return None
    state = matches[0]
    recorded_target = state.get("target")
    if isinstance(recorded_target, str) and recorded_target:
        try:
            if Path(recorded_target).expanduser().resolve() != resolved:
                return None
        except OSError:
            return None
    dirty_files = _start_dirty_files(state.get(START_KEY))
    if dirty_files is None:
        return None
    return {"id": session_id, START_KEY: {"git": {"dirty_files": dirty_files}}}


def resolve_producer(target: Path, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """The single hook session this verify runs in, as a producer payload, else ``None``.

    The identity comes from ``agent_identity.claude_session_fingerprint``, the one resolver the
    receipt stamp and the verifier id also use: the hook-printed value alone, else Claude Code's
    own id, never one that disagrees with ``BRIGADE_AGENT_SESSION`` and never inside a
    ``brigade run`` worker.
    """
    fingerprint = agent_identity.claude_session_fingerprint(environ)
    if fingerprint is None:
        return None
    return load_payload(target, producer_id(fingerprint))


def resolve_session(
    target: Path,
    work_session_id: str | None,
    work_session_payload: dict[str, Any] | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Producer ``(id, payload)``: an active work session wins, else the hook session, else none."""
    if work_session_id or work_session_payload is not None:
        return work_session_id, work_session_payload
    hook = resolve_producer(target, environ)
    if hook is None:
        return None, None
    return str(hook["id"]), hook
