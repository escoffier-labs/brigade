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

A start snapshot only proves the subject was clean when the session began. It does not say
who changed it afterwards, so a hook session is a producer only for a subject it wrote itself
(``ownership_reason``). The hooks record each Edit, Write and NotebookEdit target before the
write (``manifest_loop.session_written_paths``). The session must have such a record for the
subject, and no other session in the target may have one, or an unattributable write, since
this session started. A subject changed only through Bash has no record, so it is never a
hook-session trial. That fails closed.

The state is caller-side and forgeable like the rest of the hook state. Every
ownership check in ``verify_trial`` still runs against it.
"""

from __future__ import annotations

import os
import re
import subprocess
import posixpath
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import agent_identity, localio, outcome_attribution
from . import anchored_io

HOOK_PRODUCER_PREFIX = "claude-hook:"
START_KEY = "start"
GIT_TIMEOUT_SECONDS = 2.0
MAX_DIRTY_LINES = 512
MAX_DIRTY_LINE_CHARS = 1024
OWNED = "owned"
# Slack for the state-file mtime prefilter only. The write timestamps themselves compare exactly.
_MTIME_SLACK = timedelta(seconds=2)
_FINGERPRINT = re.compile(r"[0-9a-f]{16}")


def producer_id(fingerprint: str) -> str:
    return f"{HOOK_PRODUCER_PREFIX}{fingerprint}"


def fingerprint_from_producer_id(session_id: object) -> str | None:
    """The session fingerprint a hook producer id names, or ``None`` for any other id."""
    if not isinstance(session_id, str) or not session_id.startswith(HOOK_PRODUCER_PREFIX):
        return None
    fingerprint = session_id[len(HOOK_PRODUCER_PREFIX) :]
    return fingerprint if _FINGERPRINT.fullmatch(fingerprint) else None


def _session_key(session_id: str) -> str:
    """The comparable form of a session id: a hook producer id reduces to its bare fingerprint."""
    return fingerprint_from_producer_id(session_id) or session_id


def same_session(producer_session: object, verifier_session: object) -> bool:
    """Whether a producer id and a verifier id name one session.

    A verify run names its Claude session by bare fingerprint while the producer
    is ``claude-hook:<fingerprint>``, and ``BRIGADE_VERIFY_SESSION_ID`` may carry
    either form. Both sides reduce to the bare fingerprint, so neither spelling lets
    a session read as independent of its own producer id.
    """
    if not isinstance(producer_session, str) or not isinstance(verifier_session, str):
        return False
    if not producer_session or not verifier_session:
        return False
    return _session_key(producer_session) == _session_key(verifier_session)


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
    """One bounded ``git status --short --untracked-files=all``, split into lines. ``None`` on any failure."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(target), "status", "--short", "--untracked-files=all"],
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


def _own_state(target: Path, session_id: str) -> dict[str, Any] | None:
    """The one hook state ``claude-hook:<fingerprint>`` names in this target, else ``None``.

    Exactly one state may match the fingerprint and, when it records a target, that target
    must be this one.
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
    return state


def load_payload(target: Path, session_id: str) -> dict[str, Any] | None:
    """Resolve ``claude-hook:<fingerprint>`` to a work-session-shaped payload.

    Reads only this target's hook state, never the environment, so a later
    scoring pass resolves it again from the receipt alone. Exactly one state may
    match the fingerprint and it must carry a valid start snapshot, otherwise
    there is no producer.
    """
    state = _own_state(target, session_id)
    if state is None:
        return None
    dirty_files = _start_dirty_files(state.get(START_KEY))
    if dirty_files is None:
        return None
    return {"id": session_id, START_KEY: {"git": {"dirty_files": dirty_files}}}


def _normalize_path(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def _same_file(recorded: object, subject_path: str) -> bool:
    return isinstance(recorded, str) and bool(recorded) and _normalize_path(recorded) == _normalize_path(subject_path)


def _maybe_same_file(recorded: object, subject_path: str) -> bool:
    """Either-way suffix match, so a foreign record under a different root still vetoes."""
    if not isinstance(recorded, str) or not recorded:
        return False
    left, right = _normalize_path(recorded), _normalize_path(subject_path)
    return left == right or left.endswith(f"/{right}") or right.endswith(f"/{left}")


def _write_checks_since(state: Mapping[str, Any], since: datetime) -> list[dict[str, Any]]:
    from . import dependencies

    checks = []
    for check in dependencies.write_checks(dict(state)):
        checked_at = localio.parse_iso_datetime(check.get("checked_at"))
        if checked_at is not None and checked_at >= since:
            checks.append(check)
    return checks


def _foreign_wrote(state: Mapping[str, Any], subject_path: str, since: datetime) -> bool:
    """Whether another session's state records, or cannot rule out, a write to the subject since ``since``."""
    from . import dependencies

    if any(_maybe_same_file(check.get("file_path"), subject_path) for check in _write_checks_since(state, since)):
        return True
    last_write = localio.parse_iso_datetime(state.get("last_write_at"))
    if last_write is None or last_write < since or state.get("write_observed") is not True:
        return False
    # A write happened in the window. File tools leave a record naming their path, and those that
    # name another file were just ruled out. With no record in the window the write is a shell
    # command, so its path is unknown. Records are also capped, and a full window whose oldest
    # record is already past ``since`` may have dropped the subject's.
    if not _write_checks_since(state, since):
        return True
    checks = dependencies.write_checks(dict(state))
    oldest = localio.parse_iso_datetime(checks[0].get("checked_at")) if checks else None
    return len(checks) >= dependencies.MAX_CHECKS and oldest is not None and oldest > since


def ownership_reason(target: Path, session_id: str, subject_path: str) -> str | None:
    """Why this hook session is not the producer of ``subject_path``, or ``None`` when it is.

    ``concurrent_session_ownership`` when another session in the target recorded a write to the
    subject, or a write that names no path, since this session started. ``no_owned_work`` when this
    session recorded no Edit, Write or NotebookEdit on the subject itself. A change made only
    through Bash has no record, so it fails closed. The records come from live hook state, so the
    check reruns at projection time and a later foreign write still counts.
    """
    state = _own_state(target, session_id)
    fingerprint = fingerprint_from_producer_id(session_id)
    started = localio.parse_iso_datetime(state.get("started_at")) if state is not None else None
    if state is None or fingerprint is None or started is None:
        return "producer_session_missing"
    resolved = target.expanduser().resolve()
    try:
        for other in anchored_io.iter_states(resolved, modified_since=started - _MTIME_SLACK):
            if outcome_attribution._state_matches(other, fingerprint):
                continue
            if _foreign_wrote(other, subject_path, started):
                return "concurrent_session_ownership"
    except OSError:
        # Other sessions' state cannot be read, so their writes cannot be ruled out.
        return "producer_session_missing"
    from . import manifest_loop

    if not any(_same_file(path, subject_path) for path in manifest_loop.session_written_paths(state)):
        return "no_owned_work"
    return None


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
