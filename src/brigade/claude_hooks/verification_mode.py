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
# Text that makes a command more than a plain list: grouping, subshells, command and
# process substitution.
_COMPLEX_MARKERS = ("(", ")", "{", "}", "`", "$(", "<(", ">(")
_MAX_DEPTH = 4
# Options through which a wrapper or tool runs the check somewhere other than the shell's
# directory (`env -C`, `make -C`, `npm --prefix`, `uv --directory`, ...). A verifier that
# carries one has an undetermined directory.
_CWD_OPTIONS = (
    "-C",
    "--chdir",
    "--directory",
    "--cwd",
    "--prefix",
    "--rootdir",
    "--manifest-path",
    # Workspace and project selectors run the check in a member package's directory.
    "-w",
    "--workspace",
    "--workspaces",
    "--filter",
    "--package",
    "--project",
    "--dir",
)
# Subcommands that select a workspace member (`yarn workspace <name> test`).
_WORKSPACE_SUBCOMMANDS = {"workspace", "workspaces"}


def _overrides_cwd(segment: list[str]) -> bool:
    if any(token in _WORKSPACE_SUBCOMMANDS for token in segment[1:]):
        return True
    for token in segment:
        if token in _CWD_OPTIONS or any(token.startswith(f"{option}=") for option in _CWD_OPTIONS if option != "-C"):
            return True
        if token.startswith("-C") and not token.startswith("--"):
            return True  # `-C<dir>` written as one token
    return False


def _plain_and_chain(command: str, segments: list[list[str]], separators: list[str]) -> bool:
    """True when ``command`` is commands joined only by ``&&``, with nothing that hides one.

    In such a chain a ``cd`` that fails stops every later command, so each later
    command runs in the ``cd`` destination or not at all. Any other separator
    (``;``, ``||``, ``&``, a pipe, a newline), a group, a control structure, or a
    substitution can let a command run somewhere else.
    """
    rt = _rt()
    if any(separator != "&&" for separator in separators):
        return False
    if any(marker in command for marker in _COMPLEX_MARKERS):
        return False
    return not any(
        segment and (segment[0] in rt._SHELL_CONTROL_TOKENS or segment[0] in _KEYWORDS) for segment in segments
    )


# Shells and inline-code flags through which a runner (`uv run`, `npx`, `poetry run`, ...)
# can carry a script that changes directory. Checks after the allowlisted cd may use neither.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd", "busybox"}
_INLINE_CODE_FLAGS = {"-c", "-e", "--command", "--eval"}


def _carries_script(segment: list[str]) -> bool:
    """True when ``segment`` could run code the walker cannot see, such as ``uv run bash -c '...'``."""
    for token in segment[1:]:
        name = Path(token).name.lower().removesuffix(".exe")
        if name in _SHELLS or token in _INLINE_CODE_FLAGS or any(char.isspace() for char in token):
            return True
    return False


def _cd_then_checks(segments: list[list[str]]) -> bool:
    """True when the command is one ``cd`` to an absolute literal path, then only checks.

    This is an allowlist, not a model of the shell. The ``cd`` comes first, names an
    absolute or ``./`` path with no ``..`` component (so ``CDPATH`` and the logical
    directory play no part), and every later command is a verification with no working-directory
    option and no inline script. Anything else after the ``cd``, such as ``eval``, ``source``, another ``cd``,
    a nested shell, or an ordinary command, could change directory unseen.
    """
    rt = _rt()
    pairs = [(segment, stripped) for segment in segments if (stripped := rt._strip_command_prefixes(segment))]
    if not pairs or Path(pairs[0][1][0]).name != "cd":
        return False
    args = pairs[0][1][1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if len(args) != 1 or ".." in Path(args[0]).parts:
        return False
    # Bash looks a bare relative name up in CDPATH, but never an absolute path or one that
    # starts with `./`, so only those two forms land where the walker resolves them.
    if not (Path(args[0]).is_absolute() or args[0].startswith("./")):
        return False
    for segment, head in pairs[1:]:
        if Path(head[0]).name in {"cd", "pushd", "popd"} or rt._nested_shell_script(head) is not None:
            return False
        if not (rt._segment_is_verifier(segment) or rt._is_brigade_verify(rt._strip_env(segment))):
            return False
        if _overrides_cwd(segment) or _carries_script(head):
            return False
    return True


def verifier_cwds(command: str, cwd: Path | None, *, depth: int = 0) -> list[Path | None]:
    """Directories in which each verification segment of ``command`` runs.

    ``None`` marks a verification whose directory cannot be determined. An
    empty list means no verification segment was found. A ``cd`` moves later
    verifications only inside a plain ``&&`` chain; a command that changes
    directory any other way marks every verification in it undetermined.
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
    # Judged on token text: `(cd x`, `{ cd x`, `$(cd x` and a backtick cd hide it from a segment-head check.
    changes_directory = any(token.lstrip("({`$") in {"cd", "pushd", "popd"} for token in tokens)
    if changes_directory and not (_plain_and_chain(command, segments, separators) and _cd_then_checks(segments)):
        return [None]
    found: list[Path | None] = []
    effective = cwd
    for segment in segments:
        stripped = rt._strip_command_prefixes(segment)
        while stripped and stripped[0] in _KEYWORDS:
            stripped = rt._strip_command_prefixes(stripped[1:])
        if not stripped:
            continue
        if Path(stripped[0]).name == "cd":
            args = stripped[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            path = None
            if effective is not None and len(args) == 1 and args[0] != "-":
                path = _literal_path(args[0], effective)
            effective = path
            continue
        nested = rt._nested_shell_script(stripped)
        if nested is not None:
            if depth >= _MAX_DEPTH:
                found.append(None)
            else:
                # A wrapper such as `env -C dir bash -c ...` starts the nested shell elsewhere.
                start = None if _overrides_cwd(stripped) else effective
                found.extend(verifier_cwds(nested, start, depth=depth + 1))
            continue
        if rt._segment_is_verifier(segment) or rt._is_brigade_verify(rt._strip_env(segment)):
            found.append(None if _overrides_cwd(segment) else effective)
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
