"""Resolve the work mode that governs a verification command (#2235).

Write attribution observes several roots for one Bash call, so the hook runs
once per root. Verification enforcement must not depend on which root is being
handled: it follows the directory each check actually runs in, after any
``cd``. Mode resolution still goes through ``work_mode``; this module only
finds the execution directories. A check whose directory it cannot place, or
that runs outside any wired repository, keeps the handled root's mode, which is
how every check was judged before.
"""

from __future__ import annotations

from pathlib import Path

from .. import work_mode
from ..wiring import resolve_wired_target
from .write_attribution import _literal_path, _rt

_KEYWORDS = {"!", "{", "(", "if", "then", "elif", "else", "do", "while", "until", "time"}
# A cd inside a group, subshell, or control structure may not persist or may be
# conditional, so a command mixing cd with these is not resolved.
_GROUPING_CHARS = "(){}"
_UNSTABLE_CD_SEPARATORS = {"||"}
_DETACHED_CD_SEPARATORS = {"|", "|&", "&"}
_MAX_DEPTH = 4


def verifier_cwds(command: str, cwd: Path | None, *, depth: int = 0) -> list[Path | None]:
    """Directories in which each verification segment of ``command`` runs.

    ``None`` marks a verification whose directory cannot be determined. An
    empty list means no verification segment was found.
    """
    rt = _rt()
    try:
        tokens = rt._shell_tokens(rt._strip_heredoc_bodies(command))
    except ValueError:
        return [None]
    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    found: list[Path | None] = []
    effective = cwd
    # Judged on token text: `(cd x` and `{ cd x` hide the cd from a segment-head check.
    unplaceable_cd = any(token.lstrip("({") == "cd" for token in tokens) and (
        any(char in token for token in tokens for char in _GROUPING_CHARS)
        or any(segment and segment[0] in rt._SHELL_CONTROL_TOKENS for segment in segments)
    )
    for index, segment in enumerate(segments):
        stripped = rt._strip_command_prefixes(segment)
        while stripped and stripped[0] in _KEYWORDS:
            stripped = rt._strip_command_prefixes(stripped[1:])
        if not stripped:
            continue
        before = separators[index - 1] if index > 0 else ""
        after = separators[index] if index < len(separators) else ""
        if Path(stripped[0]).name == "cd":
            if before in _DETACHED_CD_SEPARATORS or after in _DETACHED_CD_SEPARATORS:
                continue  # runs in a subshell or the background; the shell's cwd is unchanged
            args = stripped[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            path = None
            if effective is not None and len(args) == 1 and args[0] != "-":
                path = _literal_path(args[0], effective)
            unstable = before in _UNSTABLE_CD_SEPARATORS or after in _UNSTABLE_CD_SEPARATORS
            effective = None if unstable else path
            continue
        nested = rt._nested_shell_script(stripped)
        if nested is not None:
            if depth >= _MAX_DEPTH:
                found.append(None)
            else:
                found.extend(verifier_cwds(nested, effective, depth=depth + 1))
            continue
        if rt._segment_is_verifier(segment) or rt._is_brigade_verify(rt._strip_env(segment)):
            found.append(None if unplaceable_cd else effective)
    return found


def verification_in_yolo(command: object, cwd: object, root: Path) -> bool:
    """True only when every verification in ``command`` is governed by yolo mode.

    ``cwd`` is the tool call's working directory, and ``root`` stands in for it
    when absent. A verification that runs in a wired repository follows that
    repository's mode. One whose directory cannot be placed, or that runs
    outside every wired repository, follows ``root``'s mode, as before #2235.
    So an enforced workspace stays enforced for a dynamic ``cd``, and a yolo
    repository is not newly denied for one.
    """
    if not isinstance(command, str) or not command.strip():
        return False
    root_yolo = work_mode.is_yolo(root)
    start = Path(cwd) if isinstance(cwd, str) and cwd else root
    cwds = verifier_cwds(command, start)
    if not cwds:
        return root_yolo
    for path in cwds:
        wired = resolve_wired_target(str(path)) if path is not None else None
        if not (work_mode.is_yolo(wired) if wired is not None else root_yolo):
            return False
    return True
