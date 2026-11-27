"""Route shell write evidence to each affected wired worktree."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import localio
from . import producer_session
from .anchored_io import SessionIntegrityError
from .command_tokens import (
    EVENT_BUDGET,
    MAX_COMMAND_CHARS,
    MAX_OBSERVED_ROOTS,
    RecognitionLimit,
    UnreadableCommand,
    event_wired_target as resolve_wired_target,
)

_BASH_FINGERPRINT = "pending_bash_fingerprint"
_BASH_STARTED = "pending_bash_started_at"
_BASH_COMMANDS = "pending_bash_commands"
PENDING_BASH_KEYS = (_BASH_FINGERPRINT, _BASH_STARTED, _BASH_COMMANDS)
_MAX_PENDING_COMMANDS = 8


def _tool_use_id(payload: Mapping[str, Any]) -> str | None:
    """Read the optional Claude tool invocation identifier."""
    value = payload.get("tool_use_id")
    return value if isinstance(value, str) and value else None


def _clamp_start(value: object, now: datetime) -> datetime:
    """Give an unreadable or future start a receipt boundary at the current time."""
    try:
        stamp = localio.parse_iso_datetime(value)
    except OverflowError:
        return now
    return stamp if stamp is not None and stamp <= now else now


def _pending_commands(state: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Read structurally valid records, including those whose timestamps need repair."""
    commands = state.get(_BASH_COMMANDS)
    if not isinstance(commands, dict):
        return {}
    return {
        key: value
        for key, value in commands.items()
        if isinstance(key, str)
        and key
        and isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0]
    }


def restore_pending(state: dict[str, Any], current: Mapping[str, Any], *, now: datetime | None = None) -> None:
    """Restore bounded, valid pending records from the current session state."""
    now = now or localio.utc_now()
    fingerprint, started = _pending_record(current, None)
    commands = {
        key: [baseline, _clamp_start(stamp, now).isoformat()]
        for key, (baseline, stamp) in _pending_commands(current).items()
    }
    raw_commands = current.get(_BASH_COMMANDS)
    malformed_map = _BASH_COMMANDS in current and (
        not isinstance(raw_commands, dict) or len(raw_commands) != len(commands)
    )
    has_baseline = isinstance(fingerprint, str) and bool(fingerprint)
    malformed_legacy = (_BASH_FINGERPRINT in current or _BASH_STARTED in current) and not (
        has_baseline and isinstance(started, str)
    )
    for key in PENDING_BASH_KEYS:
        state.pop(key, None)
    if has_baseline or started is not None:
        state[_BASH_FINGERPRINT] = fingerprint if has_baseline else _rt()._UNAVAILABLE_FINGERPRINT
        state[_BASH_STARTED] = _clamp_start(started, now).isoformat()
    oversized = len(commands) > _MAX_PENDING_COMMANDS
    if oversized:
        newest = sorted(commands, key=lambda key: commands[key][1], reverse=True)[:_MAX_PENDING_COMMANDS]
        commands = {key: commands[key] for key in newest}
    if malformed_map or malformed_legacy or oversized:
        _stamp_write(state, now.isoformat())
    if commands:
        state[_BASH_COMMANDS] = commands


def _pending_record(state: Mapping[str, Any], tool_id: str | None) -> tuple[object, object]:
    """Return the baseline and start for an identified tool or the legacy slot."""
    if tool_id is None:
        return state.get(_BASH_FINGERPRINT), state.get(_BASH_STARTED)
    record = _pending_commands(state).get(tool_id)
    return (record[0], record[1]) if record else (None, None)


def pending_start(state: Mapping[str, Any], *, unknown_at: datetime) -> datetime | None:
    """Return the newest pending Bash start in either state format."""
    legacy = _pending_record(state, None)
    starts: list[object] = [record[1] for record in _pending_commands(state).values()]
    if any(value is not None for value in legacy):
        starts.append(legacy[1])
    return max((_clamp_start(stamp, unknown_at) for stamp in starts), default=None)


def include_pending_at_stop(state: dict[str, Any]) -> None:
    """Include pending Bash work in Stop's receipt boundary without consuming it."""
    # Use the start as a receipt boundary without consuming the pending record.
    # A new receipt can cover it, and a delayed completion can still match it.
    pending = pending_start(state, unknown_at=localio.utc_now())
    if pending is None:
        return
    state["write_observed"] = True
    previous = localio.parse_iso_datetime(state.get("last_verification_write_at") or state.get("last_write_at"))
    if previous is None or pending > previous:
        state["last_verification_write_at"] = pending.isoformat()


def _stamp_write(state: dict[str, Any], written_at: str, *, handoff: bool = False) -> None:
    """Advance the session's write timestamps together."""
    state["write_observed"] = True
    state["last_write_at"] = state["last_attributed_write_at"] = written_at
    if not handoff:
        state["last_verification_write_at"] = written_at


def record_write(
    state: dict[str, Any],
    target: Path,
    payload: dict[str, Any],
    *,
    handoff: bool = False,
    fingerprint: str | None = None,
) -> None:
    """Record a completed or possible write and its producer evidence."""
    written_at = localio.utc_now_iso()
    producer_session.record_write(state, target, payload, written_at)
    _stamp_write(state, written_at, handoff=handoff)
    if fingerprint is not None:
        state["repo_fingerprint"] = fingerprint


def begin_bash(
    target: Path, session_id: str, state: dict[str, Any], payload: dict[str, Any], baseline: str | None
) -> None:
    """Store a Bash start against the latest state under the session lock."""
    rt = _rt()
    tool_id = _tool_use_id(payload)
    baseline = baseline or rt._UNAVAILABLE_FINGERPRINT
    started = localio.utc_now_iso()

    def save_pending(current: dict[str, Any]) -> None:
        if tool_id is None:
            occupied = any(value is not None for value in _pending_record(current, None))
            current[_BASH_FINGERPRINT] = baseline
            current[_BASH_STARTED] = started
            if occupied:
                # Either completion could arrive first when neither has an ID.
                current[_BASH_FINGERPRINT] = rt._UNAVAILABLE_FINGERPRINT
                record_write(current, target, payload)
            return
        commands = _pending_commands(current)
        replaced = tool_id in commands
        if replaced:
            record_write(current, target, payload)
        elif len(commands) >= _MAX_PENDING_COMMANDS:
            oldest = min(commands, key=lambda key: commands[key][1])
            del commands[oldest]
            record_write(current, target, payload)
        commands[tool_id] = [rt._UNAVAILABLE_FINGERPRINT if replaced else baseline, started]
        current[_BASH_COMMANDS] = commands

    rt._write_session_state_preserving_latch(
        target, session_id, state, log_target=target, pending_bash_update=save_pending
    )


def _rt() -> Any:
    # Runtime owns the shell lexer and state machinery, including test seams.
    from . import runtime

    return runtime


def _literal_path(raw: str, cwd: Path) -> Path | None:
    if not raw or any(char in raw for char in "$`*?[]{}"):
        return None
    return _rt()._resolve_command_path(raw, cwd)


def _git_command(tokens: list[str], cwd: Path) -> tuple[list[str], Path]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-C", "-c", "--git-dir", "--work-tree"}:
            if index + 1 >= len(tokens):
                return [], cwd
            if token == "-C":
                path = _literal_path(tokens[index + 1], cwd)
                if path is None:
                    return [], cwd
                cwd = path
            elif token in {"--git-dir", "--work-tree"}:
                return [], cwd  # Nonstandard layouts retain fingerprint fallback.
            index += 2
        elif token.startswith("-C"):
            path = _literal_path(token[2:], cwd)
            if path is None:
                return [], cwd
            cwd = path
            index += 1
        elif token.startswith("-"):
            index += 1
        else:
            return tokens[index:], cwd
    return [], cwd


# Short options whose next token is a value, not a path the command writes
# (`touch -r REF` only reads REF's times). Long `--opt=value` forms are already
# dropped as options.
_OPTION_VALUES = {"touch": "rdt", "truncate": "rs", "mkdir": "m", "mv": "St", "cp": "St", "install": "gmoSt"}
_TARGET_DIRECTORY_COMMANDS = {"cp", "install", "mv"}
# Reserved words that can open a segment before the real command name.
_SHELL_KEYWORDS = {"!", "{", "(", "if", "then", "elif", "else", "do", "while", "until", "time"}


def _target_directory(tokens: list[str]) -> str | None:
    """The ``-t DIR`` / ``--target-directory=DIR`` destination of cp, install, or mv."""
    for index, token in enumerate(tokens):
        if token == "--":
            return None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token == "--target-directory":
            return following
        if token.startswith("--target-directory="):
            return token.split("=", 1)[1]
        if token.startswith("-") and not token.startswith("--") and "t" in token[1:]:
            rest = token[token.index("t", 1) + 1 :]
            return rest or following
    return None


def _without_option_values(name: str, tokens: list[str]) -> list[str] | None:
    if name not in _OPTION_VALUES:
        return tokens  # option-value rules only describe the write commands listed above
    letters = _OPTION_VALUES[name]
    kept: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token == "--":
            letters = ""
        elif letters and token.startswith("-") and not token.startswith("--") and len(token) > 1:
            skip = token[-1] in letters
        elif token == "--target-directory" and name in _TARGET_DIRECTORY_COMMANDS:
            skip = True  # read separately by _target_directory
        elif token in {"--reference", "--date", "--size", "--mode", "--suffix", "--owner", "--group"}:
            return None  # a separated long-option value: stay conservative
        kept.append(token)
    return kept


def _write_operands(tokens: list[str]) -> list[str] | None:
    rt = _rt()
    name = Path(tokens[0]).name
    operand_tokens = _without_option_values(name, tokens[1:])
    if operand_tokens is None:
        return None
    args = rt._command_path_arguments(operand_tokens)
    if name == "sed":
        if not any(arg.startswith("-i") for arg in tokens[1:]):
            return []
        # With -e/-f the script is an option operand. Otherwise it is the
        # first positional operand. Never mistake the expression for a path.
        if any(arg in {"-e", "-f", "--expression", "--file"} for arg in tokens[1:]):
            return None
        return args[1:] or None
    if name == "ruff":
        if tokens[1:2] != ["format"] or {"--check", "--diff"}.intersection(tokens[2:]):
            return []
        return rt._command_path_arguments(tokens[2:]) or ["."]
    if name in rt._BASH_WRITE_COMMANDS:
        if name in {"apply_patch", "patch", "mktemp"}:
            return None
        if name in _TARGET_DIRECTORY_COMMANDS:
            directory = _target_directory(tokens[1:])
            if directory is not None:
                # cp/install sources are read; mv sources are removed, so they change too.
                return [directory, *args] if name == "mv" else [directory]
            if name in {"cp", "install"}:
                # Source operands are read; the final operand is the destination.
                return args[-1:] or None
        return args if args or name == "tee" else None
    return []


def bash_scopes(command: object, cwd: Path, *, depth: int = 0) -> dict[Path, bool]:
    """Map observation roots to whether an explicit source write names them.

    Observation roots keep fingerprint detection for scripts and subagents.
    Unresolved write operands conservatively retain their command cwd.
    """
    if not isinstance(command, str):
        return {}
    budget = _rt().EVENT_BUDGET.get()
    if budget is None:
        return _scan_bash_scopes(command, cwd, depth=depth)
    key = command, cwd, depth
    if key not in budget.scopes:
        budget.scopes[key] = _scan_bash_scopes(command, cwd, depth=depth)
    return budget.scopes[key]


def _scan_bash_scopes(command: str, cwd: Path, *, depth: int) -> dict[Path, bool]:
    rt = _rt()
    if not command.strip():
        return {}
    rt.charge_event(len(command))
    try:
        tokens = rt._shell_path_tokens(rt._strip_heredoc_bodies(command))
    except ValueError:
        return {}
    scopes: dict[Path, bool] = {}

    def add(path: Path, confident: bool = False) -> None:
        target = resolve_wired_target(str(path))
        if target is not None:
            if EVENT_BUDGET.get() is not None and len(scopes) >= MAX_OBSERVED_ROOTS and target not in scopes:
                raise RecognitionLimit
            scopes[target] = scopes.get(target, False) or confident

    def destination(raw: str, effective: Path) -> None:
        path = _literal_path(raw, effective)
        if path is None:
            add(effective, True)
        elif not rt._path_targets_brigade_internal_artifact(resolve_wired_target(str(path)) or effective, str(path)):
            add(path, True)

    # Never observe less than the pre-attribution discovery did: every directory
    # it found stays an observation root, so a fingerprint catches what the
    # segment parser below cannot place.
    try:
        for candidate in rt._command_candidate_paths(command, cwd):
            add(candidate)
    except ValueError:
        pass

    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    effective = cwd
    for index, segment in enumerate(segments):
        stripped = rt._strip_command_prefixes(segment, keywords=_SHELL_KEYWORDS)
        if not stripped:
            continue
        add(effective)
        for raw in rt._redirect_file_targets(segment):
            destination(raw, effective)
        name = Path(stripped[0]).name
        if name == "cd":
            args = stripped[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            piped = (index > 0 and separators[index - 1] in rt._PIPELINE_SEPARATORS) or (
                index < len(separators) and separators[index] in rt._PIPELINE_SEPARATORS
            )
            path = _literal_path(args[0], effective) if len(args) == 1 and args[0] != "-" else None
            if path is not None and not piped:
                effective = path
                add(effective)
            continue
        nested = rt._nested_shell_script(stripped)
        if nested is not None and depth < 4:
            for target, confident in bash_scopes(nested, effective, depth=depth + 1).items():
                add(target, confident)
            continue
        if name == "git":
            subcommand, git_cwd = _git_command(stripped, effective)
            add(git_cwd, bool(subcommand and subcommand[0] in rt._GIT_WRITE_COMMANDS))
            continue
        operands = _write_operands(stripped)
        if operands is None:
            add(effective, True)
            # Still observe every literal path operand, so a fingerprint catches a
            # write the parser could not place.
            for raw in rt._command_path_arguments(stripped[1:]):
                path = _literal_path(raw, effective)
                if path is not None:
                    add(path)
        else:
            for operand in operands:
                destination(operand, effective)
    return scopes


def touches_worktree(target: Path, command: object, *, cwd: object = None) -> bool:
    effective = (
        _rt()._resolve_command_path(cwd, Path.cwd(), expand_vars=False) if isinstance(cwd, str) and cwd else target
    )
    return bash_scopes(command, effective).get(target.resolve(), False)


def mark_analysis_failure(event: str, exc: Exception) -> None:
    budget = EVENT_BUDGET.get()
    if (
        budget is None
        or isinstance(exc, SessionIntegrityError)
        or (event == "PreToolUse" and not isinstance(exc, (RecognitionLimit, UnreadableCommand)))
    ):
        raise exc
    budget.attribution_incomplete = True
    budget.unreadable |= not isinstance(exc, RecognitionLimit)


def post_bash_write(
    event: str,
    target: Path,
    payload: dict[str, Any],
    pending: tuple[object, object],
    session_id: str,
    fingerprint: Callable[[Path], str | None],
) -> tuple[bool, str | None, bool, str | None]:
    """Detect writes without catching errors from subsequent state updates."""
    rt = _rt()
    budget = EVENT_BUDGET.get()
    if budget is not None and budget.attribution_incomplete:
        return True, None, False, None
    command = rt._as_tool_input(payload.get("tool_input")).get("command")
    try:
        routed = rt._is_routed_verify(command)
        capture = rt._parse_capture_flag(command) if routed and event == "PostToolUse" else None
        baseline, started = pending
        if not isinstance(baseline, str) or not baseline:
            # A command with no explicit write operands can still change the tree.
            return True, capture, False, None
        wrote = touches_worktree(target, command, cwd=payload.get("cwd"))
        brigade_command = routed or rt._is_brigade_run(command)
        # A failed write-capable command can exit before it changes any file.
        if (not wrote and not brigade_command) or (wrote and event == "PostToolUseFailure"):
            wrote = rt._bash_write_detected(
                target,
                baseline,
                session_id=session_id,
                started_at=started,
                fingerprint=fingerprint,
            )
        handoff = wrote and rt._bash_write_targets_handoffs(target, command)
        updated_fingerprint = fingerprint(target) if wrote else None
        return wrote, capture, handoff, updated_fingerprint
    except Exception as exc:  # noqa: BLE001 - only attribution, never state updates
        mark_analysis_failure(event, exc)
        return True, None, False, None


def finish_bash(
    event: str,
    target: Path,
    payload: dict[str, Any],
    state: dict[str, Any],
    session_id: str,
    fingerprint: Callable[[Path], str | None],
) -> bool:
    """Consume the completed start and atomically save its write evidence."""
    tool_id = _tool_use_id(payload)
    pending = _pending_record(state, tool_id)
    wrote, capture, handoff, updated_fp = post_bash_write(event, target, payload, pending, session_id, fingerprint)

    def finish_pending(current: dict[str, Any]) -> None:
        nonlocal wrote
        # Attribution runs outside the state lock. Use the locked record to
        # detect a competing completion, replacement, or eviction.
        matched_snapshot = pending == _pending_record(current, tool_id)
        commands = _pending_commands(current)
        legacy_pending = any(key in current for key in (_BASH_FINGERPRINT, _BASH_STARTED))
        if tool_id is not None and tool_id in commands:
            del commands[tool_id]
        elif legacy_pending and (tool_id is None or not commands):
            current.pop(_BASH_FINGERPRINT, None)
            current.pop(_BASH_STARTED, None)
        elif tool_id is None and len(commands) == 1:
            # An ID-less completion can retire a sole keyed start conservatively.
            commands.clear()
        if commands:
            current[_BASH_COMMANDS] = commands
        else:
            current.pop(_BASH_COMMANDS, None)
        wrote = wrote or not matched_snapshot
        if wrote:
            current.pop("pending_write_at", None)
            record_write(
                current,
                target,
                payload,
                handoff=matched_snapshot and handoff,
                fingerprint=updated_fp if matched_snapshot else None,
            )

    saved = _rt()._write_session_state_preserving_latch(
        target,
        session_id,
        state,
        log_target=target,
        capture_artifact_id=capture,
        pending_bash_update=finish_pending,
    )
    return bool(saved and wrote)


def _linked_pending_roots(primary: Path, payload: dict[str, Any]) -> dict[Path, bool]:
    """Recover pre-event destinations if the post-event scan cannot finish."""
    rt = _rt()
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return {}
    state = rt.read_session_state(primary, session)
    roots = state.get("session_repos") if state else None
    if not isinstance(roots, list):
        return {}
    return dict.fromkeys((Path(root) for root in roots[:MAX_OBSERVED_ROOTS] if isinstance(root, str) and root), False)


def handle_event(event: str, payload: dict[str, Any], *, pin: Path | None) -> dict[str, Any] | None:
    """Route Bash events to the wired primary and observed worktrees."""
    rt = _rt()
    budget = EVENT_BUDGET.get()
    if budget is None:
        return rt._handle_event(event, payload, pin=pin)
    command = rt._as_tool_input(payload.get("tool_input")).get("command")
    raw_cwd = payload.get("cwd")
    try:
        if budget.attribution_incomplete or (isinstance(command, str) and len(command) > MAX_COMMAND_CHARS):
            raise RecognitionLimit
        primary = rt.effective_hook_target(payload, pin=pin)
        if primary is None:
            return None
        cwd = (
            rt._resolve_command_path(raw_cwd, Path.cwd(), expand_vars=False)
            if isinstance(raw_cwd, str) and raw_cwd
            else primary
        )
        scopes = bash_scopes(command, cwd)
    except Exception as exc:  # noqa: BLE001 - target discovery and attribution only
        mark_analysis_failure(event, exc)
        primary = rt._bash_fallback_target(payload, pin)
        if primary is None:
            return None
        scopes = _linked_pending_roots(primary, payload) if event != "PreToolUse" else {}
    targets = [primary, *scopes]
    bash_starts: list[Callable[[], None]] = []
    result = None
    for target in dict.fromkeys(targets):
        # Reuse the normal pin/home policy for every additional root.
        if target != primary and rt.effective_hook_target({"cwd": str(target)}, pin=pin) != target:
            continue
        if target != primary and event != "PreToolUse" and not scopes.get(target):
            # A post event without a corresponding pre event has no baseline
            # to compare in an observation-only root. Do not create state there.
            state = rt.read_session_state(target, str(payload.get("session_id") or ""))
            if not state or pending_start(state, unknown_at=localio.utc_now()) is None:
                continue
        budget.target = target
        with budget.in_phase("followup"):
            current = rt._handle_event(event, payload, pin=pin, target_override=target, bash_starts=bash_starts)
        if current is not None:
            result = current
            if current.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                return result
    for start in bash_starts:
        start()
    return result
