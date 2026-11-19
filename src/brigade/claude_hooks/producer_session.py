"""Hook session as the producer of a manifest verify trial (#2589).

Ordinary Claude Code sessions never run ``brigade work start``, so a manifest
verify receipt had no producer session and was ineligible
(``producer_session_missing``). The hook already keeps per-session state, so
this module lets that state stand in for a work session:

* when the hook first creates a session's state, and again when a task-epoch reset (``/clear``)
  starts a new task in it, it records the dirty files of the target in the work-session shape,
  ``start.git.dirty_files`` (``stamp_start``, ``carry``). Earlier epochs' starts are kept, bounded, in
  ``start_history``, and each start has an ``id`` that a receipt's producer binding records;
* a verify that resolves exactly one Claude session with such state names it as
  ``claude-hook:<fingerprint>`` and reads its start snapshot back as a session
  payload (``resolve_producer``, ``load_payload``).

The id has a prefix a work session id can never carry (those start with a
timestamp), so the two never collide. The start snapshot is bounded and fails
open: when git errors, times out, or reports more than the caps allow, nothing is
recorded and that session simply cannot produce trials. A snapshot is never
truncated, because a partial list could hide a dirty subject. Its format is
``git_status_snapshot``'s NUL-separated porcelain, so quoting and renames cannot misread it.

A start snapshot only proves the subject was clean when the session began. It does not say
who changed it afterwards, so a hook session is a producer only for a subject it wrote itself
(``ownership_reason``). The hooks record each Edit, Write and NotebookEdit target before the
write and keep a bounded list of timestamped write events per session, one per completed write:
its completion time, tool, and the target-relative path of a file tool, or no path for a Bash write
the hooks detected. The session must have a file-tool event for the subject since its start, and no
other session in the target may have an event since then that names the subject or names no path.
Completion time is what counts, so a write that began before this session and finished after it is
inside the window. When another session's list lost events inside the window, or a state that
reports a write has no matching event, or any state in the target cannot be read, the window cannot
be covered and the result is ``concurrent_session_ownership``. A subject changed only through
Bash has no path-bearing event of this session's own, so it is never a hook-session trial. That
fails closed.

The state is caller-side and forgeable like the rest of the hook state. Every
ownership check in ``verify_trial`` still runs against it.
"""

from __future__ import annotations

import hashlib
import re
import posixpath
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .. import agent_identity, git_status_snapshot, localio, outcome_attribution
from . import anchored_io

HOOK_PRODUCER_PREFIX = "claude-hook:"
START_KEY = "start"
HISTORY_KEY = "start_history"
EVENTS_KEY = "write_events"
EVICTED_KEY = "write_events_evicted_at"
GIT_TIMEOUT_SECONDS = 2.0
MAX_DIRTY_LINES = 512
MAX_DIRTY_LINE_CHARS = 1024
# Earlier epochs' starts kept so a receipt bound to one still resolves after a later ``/clear``.
MAX_START_HISTORY = 3
# Write events kept per session. A session that outruns it sets ``EVICTED_KEY``, which fails closed.
MAX_WRITE_EVENTS = 64
_FILE_WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
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


def _valid_git(git: object) -> dict[str, Any] | None:
    """A recorded git snapshot within the caps, normalized, else ``None``."""
    if not git_status_snapshot.valid_snapshot(git):
        return None
    assert isinstance(git, Mapping)
    lines, prefix = git["dirty_files"], git["prefix"]
    if len(lines) > MAX_DIRTY_LINES or any(len(line) > MAX_DIRTY_LINE_CHARS for line in lines):
        return None
    if len(prefix) > MAX_DIRTY_LINE_CHARS:
        return None
    return {"dirty_files": list(lines), "status_format": git_status_snapshot.STATUS_FORMAT, "prefix": prefix}


def _start_id(at: str, git: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(f"{at}\n{git_status_snapshot.fingerprint(git)}".encode()).hexdigest()
    return digest[:16]


def _valid_start(value: object) -> dict[str, Any] | None:
    """One recorded start (``id``, ``at``, ``git``) whose id matches its content, else ``None``."""
    if not isinstance(value, Mapping):
        return None
    git = _valid_git(value.get("git"))
    at = value.get("at")
    if git is None or not isinstance(at, str) or localio.parse_iso_datetime(at) is None:
        return None
    if value.get("id") != _start_id(at, git):
        return None
    return {"id": value["id"], "at": at, "git": git}


def _valid_history(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    starts = [start for item in value if (start := _valid_start(item)) is not None]
    return starts[-MAX_START_HISTORY:]


def snapshot_start(target: Path) -> dict[str, Any] | None:
    """One bounded start snapshot of ``target`` with its id, or ``None`` on any failure or overflow."""
    git = _valid_git(git_status_snapshot.snapshot(target, untracked_all=True, timeout=GIT_TIMEOUT_SECONDS))
    if git is None:
        return None
    at = localio.utc_now_iso()
    return {"id": _start_id(at, git), "at": at, "git": git}


def stamp_start(state: dict[str, Any], target: Path) -> None:
    """Record a fresh start-of-task snapshot, replacing the current one."""
    state.pop(START_KEY, None)
    start = snapshot_start(target)
    if start is not None:
        state[START_KEY] = start


def carry(state: dict[str, Any], persisted: Mapping[str, Any], target: Path, task_epoch: str | None) -> None:
    """Carry a persisted state's starts and write events into a freshly normalized one.

    The events and earlier starts outlive a task epoch. On a reset, when ``task_epoch`` names an epoch
    other than the persisted one (a ``/clear`` or a new dispatched task), the persisted current start moves into ``start_history`` and a fresh snapshot
    becomes current, so work done after the reset can produce a trial and a receipt bound to the
    earlier start still finds it. Otherwise the persisted start stays current unchanged.
    """
    reset = task_epoch is not None and persisted.get("task_epoch") != task_epoch
    restore_events(state, persisted)
    history = _valid_history(persisted.get(HISTORY_KEY))
    state.pop(START_KEY, None)
    state.pop(HISTORY_KEY, None)
    current = _valid_start(persisted.get(START_KEY))
    if reset:
        if current is not None:
            history = [*history, current][-MAX_START_HISTORY:]
        stamp_start(state, target)
    elif current is not None:
        state[START_KEY] = current
    if history:
        state[HISTORY_KEY] = history


def _select_start(state: Mapping[str, Any], start_id: str | None) -> dict[str, Any] | None:
    """The start a receipt bound to (``start_id``), else the current one when none was named."""
    current = _valid_start(state.get(START_KEY))
    if start_id is None:
        return current
    for start in [current, *reversed(_valid_history(state.get(HISTORY_KEY)))]:
        if start is not None and start["id"] == start_id:
            return start
    return None


def _valid_events(value: object) -> list[dict[str, Any]]:
    """Well-formed write events, oldest first. Anything else in the list is dropped."""
    if not isinstance(value, list):
        return []
    events = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        event_id, tool, path = item.get("id"), item.get("tool"), item.get("path")
        at = item.get("at")
        if not isinstance(event_id, str) or not isinstance(tool, str) or localio.parse_iso_datetime(at) is None:
            continue
        if path is not None and not isinstance(path, str):
            continue
        event: dict[str, Any] = {"id": event_id, "at": at, "tool": tool, "path": path}
        if item.get("outside") is True:
            event["outside"] = True
        events.append(event)
    return sorted(events, key=lambda event: event["at"])


def _store_events(state: dict[str, Any], events: list[dict[str, Any]], evicted_at: str | None) -> None:
    """Keep the newest ``MAX_WRITE_EVENTS`` and remember the newest completion time dropped."""
    dropped = events[:-MAX_WRITE_EVENTS]
    events = events[-MAX_WRITE_EVENTS:]
    stamps = [stamp for stamp in (evicted_at, *(event["at"] for event in dropped)) if stamp]
    state.pop(EVENTS_KEY, None)
    state.pop(EVICTED_KEY, None)
    if events:
        state[EVENTS_KEY] = events
    if stamps:
        state[EVICTED_KEY] = max(stamps, key=lambda stamp: localio.parse_iso_datetime(stamp) or datetime.min)


def _valid_evicted(value: object) -> str | None:
    return value if isinstance(value, str) and localio.parse_iso_datetime(value) is not None else None


def restore_events(state: dict[str, Any], persisted: Mapping[str, Any]) -> None:
    _store_events(state, _valid_events(persisted.get(EVENTS_KEY)), _valid_evicted(persisted.get(EVICTED_KEY)))


def merge_events(state: dict[str, Any], current: Mapping[str, Any]) -> None:
    """Union this hook's events with those another hook of the session persisted meanwhile."""
    merged = {event["id"]: event for event in _valid_events(current.get(EVENTS_KEY))}
    merged.update({event["id"]: event for event in _valid_events(state.get(EVENTS_KEY))})
    stamps = [
        stamp for stamp in (_valid_evicted(current.get(EVICTED_KEY)), _valid_evicted(state.get(EVICTED_KEY))) if stamp
    ]
    evicted = max(stamps, key=lambda stamp: localio.parse_iso_datetime(stamp) or datetime.min) if stamps else None
    _store_events(state, sorted(merged.values(), key=lambda event: event["at"]), evicted)


def record_write(state: dict[str, Any], target: Path, payload: Mapping[str, Any], written_at: str) -> None:
    """Add the completed write of this PostToolUse as an event.

    A file tool names its target-relative path, or is marked ``outside`` when the path is not in the
    target and so cannot be the subject. A Bash write the hooks detected names no path.
    """
    from . import dependencies

    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    event: dict[str, Any] = {"id": uuid4().hex, "at": written_at, "tool": str(tool), "path": None}
    if tool in _FILE_WRITE_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path")
        event["path"] = dependencies.relative_path(target, raw, payload.get("cwd"))
        if event["path"] is None:
            event["outside"] = True
    events = [*_valid_events(state.get(EVENTS_KEY)), event]
    _store_events(state, sorted(events, key=lambda item: item["at"]), _valid_evicted(state.get(EVICTED_KEY)))


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


def load_payload(target: Path, session_id: str, start_id: str | None = None) -> dict[str, Any] | None:
    """Resolve ``claude-hook:<fingerprint>`` to a work-session-shaped payload.

    Reads only this target's hook state, never the environment, so a later
    scoring pass resolves it again from the receipt alone. Exactly one state may
    match the fingerprint and it must carry a valid start snapshot, otherwise
    there is no producer. ``start_id`` selects the recorded start a receipt was bound to, among the
    current one and the bounded earlier epochs'. Without it the current start is read.
    """
    state = _own_state(target, session_id)
    if state is None:
        return None
    start = _select_start(state, start_id)
    if start is None:
        return None
    return {"id": session_id, START_KEY: start}


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


def _foreign_wrote(state: Mapping[str, Any], subject_path: str, since: datetime) -> bool:
    """Whether another session may have written the subject, or could have, at or after ``since``.

    True when its event list names the subject or names no path in the window, when the list
    lost events inside the window, when it reports a write that has no event, or when one of its
    file-tool writes to the subject is still in flight or was announced since ``since``.
    """
    from . import dependencies

    events = _valid_events(state.get(EVENTS_KEY))
    evicted = localio.parse_iso_datetime(state.get(EVICTED_KEY))
    if evicted is not None and evicted >= since:
        return True
    stamps = [(localio.parse_iso_datetime(event["at"]), event) for event in events]
    for stamp, event in stamps:
        if stamp is None or stamp < since or event.get("outside"):
            continue
        if event["path"] is None or _maybe_same_file(event["path"], subject_path):
            return True
    last_write = localio.parse_iso_datetime(state.get("last_write_at"))
    if state.get("write_observed") is True and last_write is not None and last_write >= since:
        newest = max((stamp for stamp, _event in stamps if stamp is not None), default=None)
        if newest is None or newest < last_write:
            return True
    pending = localio.parse_iso_datetime(state.get("pending_write_at"))
    announced_since = min(since, pending) if pending is not None else since
    for check in dependencies.write_checks(dict(state)):
        checked_at = localio.parse_iso_datetime(check.get("checked_at"))
        if (
            checked_at is not None
            and checked_at >= announced_since
            and _maybe_same_file(check.get("file_path"), subject_path)
        ):
            return True
    return False


def ownership_reason(target: Path, session_id: str, subject_path: str, start_id: str | None = None) -> str | None:
    """Why this hook session is not the producer of ``subject_path``, or ``None`` when it is.

    ``concurrent_session_ownership`` when another session in the target has a write event, since
    the bound start, that names the subject or names no path, when its events cannot cover that
    window, or when any session state in the target cannot be read. ``no_owned_work`` when this
    session has no Edit, Write or NotebookEdit event on the subject itself. A change made only
    through Bash has none, so it fails closed. The events come from live hook state, so the check
    reruns at projection time and a later foreign write still counts.
    """
    state = _own_state(target, session_id)
    fingerprint = fingerprint_from_producer_id(session_id)
    start = _select_start(state, start_id) if state is not None else None
    started = localio.parse_iso_datetime(start["at"]) if start is not None else None
    if state is None or fingerprint is None or start is None or started is None:
        return "producer_session_missing"
    resolved = target.expanduser().resolve()
    try:
        others = anchored_io.read_states_strict(
            resolved, modified_since=started - _MTIME_SLACK, max_bytes=outcome_attribution.MAX_STATE_BYTES
        )
    except (OSError, ValueError):
        # Another session's state cannot be listed, read or parsed, so its writes cannot be ruled out.
        return "concurrent_session_ownership"
    for other in others:
        if not outcome_attribution._state_matches(other, fingerprint) and _foreign_wrote(other, subject_path, started):
            return "concurrent_session_ownership"
    if not _owns_subject(state, start, subject_path):
        return "no_owned_work"
    return None


def _owns_subject(state: Mapping[str, Any], start: Mapping[str, Any], subject_path: str) -> bool:
    """Whether this session's own file-tool events, since ``start``, include a write to the subject."""
    from . import manifest_loop

    started = localio.parse_iso_datetime(start["at"])
    for event in _valid_events(state.get(EVENTS_KEY)):
        stamp = localio.parse_iso_datetime(event["at"])
        if event["tool"] in _FILE_WRITE_TOOLS and stamp is not None and started is not None and stamp >= started:
            if _same_file(event["path"], subject_path):
                return True
    current = _valid_start(state.get(START_KEY))
    if current is not None and current["id"] == start["id"]:
        return any(_same_file(path, subject_path) for path in manifest_loop.session_written_paths(dict(state)))
    return False


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
