"""Report local verification evidence for the immutable tips in a Git push."""

from __future__ import annotations

import io
import json
import ntpath
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache, cached_property, partial
from itertools import groupby, zip_longest
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import Any, Literal

from .. import config, localio, verify_manifest_git
from ..verify_command import split_verify_command
from . import constants, push_gate_display, verify_gate, verify_required
from .verify_push_input import INPUT_BYTES, branch_updates

_BUDGET_SECONDS = 2.0
_RECEIPT_LIMIT = 128
_RECEIPT_BYTES = 4 * 1024 * 1024
_TIMEOUT = "The push check reached its two-second time limit."
_RUN_NAME = re.compile(r"[0-9]{8}-[0-9]{6}-work-verify-[0-9a-f]{6}")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_UNFINISHED = {"running", "busy", "canceled"}
_EARLIEST = datetime.min.replace(tzinfo=UTC)
_MASKED_PASS = "newer_required_failure"
_UNCONFIRMED_PLAN = "planned_command_unconfirmed"
_AMBIGUOUS_INVOCATION = "required_check_invocation"
_OLDER_PLAN_BASE = "older_plan_base"
_DIFFERENT_PLAN_BASE = "different_plan_base"
_PLAN_COMMANDS = 64
_PLAN_CHARS = 16 * 1024
_PLAN_TOKENS = 1024
_WRAPPER_DEPTH = 8
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_READ_ONLY_OPTIONS = {"--version", "--help", "-h", "--collect-only", "--co"}


@dataclass(frozen=True)
class _Verdict:
    status: str
    reason: str | None = None
    remedy: str | None = None
    reason_code: str | None = None
    tree: str | None = None

    def public(self, ref: str | None) -> dict[str, Any]:
        return {"ref": ref, "status": self.status, "reason": self.reason, "remedy": self.remedy}


@dataclass
class _Invocation:
    variables: set[str] = field(default_factory=set)
    wrapper: str = ""
    spelling: str = ""

    def include(self, other: _Invocation) -> None:
        self.variables.update(other.variables)
        self.wrapper = self.wrapper or other.wrapper
        self.spelling = self.spelling or other.spelling

    def message(self) -> str | None:
        if self.variables:
            return _env_prefix_message(tuple(sorted(self.variables)))
        if self.wrapper:
            name = push_gate_display.terminal_text(self.wrapper)
            return (
                f"A run of the required check through {name} does not count. Run the required check again as declared."
            )
        if self.spelling:
            name = push_gate_display.terminal_text(self.spelling)
            return (
                f"A run of the required check under the spelling {name} does not count. "
                "Run the required check again as declared."
            )
        return None


@dataclass(frozen=True)
class _Candidate:
    receipt: dict[str, Any]
    started_at: datetime | None
    ran_required: bool
    eligible: bool
    failed: bool
    reason: str
    uncertain: bool = False
    invocation_notice: str | None = None
    rerun_argv: list[str] | None = None


class _Indeterminate(Exception):
    def __init__(self, message: str, reason_code: str | None = None, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.remedy = remedy


def _remaining(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _Indeterminate(_TIMEOUT, "check_timeout")
    return remaining


def _receipt_started_at(receipt: dict[str, Any]) -> datetime | None:
    try:
        return localio.parse_iso_datetime(receipt.get("started_at"))
    except OverflowError:
        return None


def _read_stdin(deadline: float) -> str:
    try:
        fd = sys.stdin.fileno()
    except io.UnsupportedOperation:
        return sys.stdin.read(INPUT_BYTES + 1)

    result: Queue[bytes | OSError] = Queue(maxsize=1)

    def read_fd() -> None:
        # Use the descriptor directly so a stalled pipe cannot hold Python's
        # buffered stdin lock during interpreter shutdown.
        data = bytearray()
        try:
            while len(data) <= INPUT_BYTES:
                chunk = os.read(fd, min(65536, INPUT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            result.put(bytes(data))
        except OSError as exc:
            result.put(exc)

    Thread(target=read_fd, daemon=True).start()
    try:
        data = result.get(timeout=_remaining(deadline))
    except Empty:
        raise _Indeterminate(_TIMEOUT, "check_timeout") from None
    if isinstance(data, OSError):
        raise data
    return os.fsdecode(data)


def _valid_oid(value: object) -> bool:
    return value is None or (isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None)


def _valid_command(command: object) -> bool:
    if not isinstance(command, dict):
        return False
    argv, status, outcomes = command.get("argv"), command.get("status"), command.get("test_outcomes")
    if argv is None and status != "rejected":
        return False
    if argv is not None and (not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv)):
        return False
    if status is not None and not isinstance(status, str):
        return False
    env = command.get("env")
    if env is not None and (not isinstance(env, list) or not all(isinstance(name, str) for name in env)):
        return False
    return outcomes is None or (
        isinstance(outcomes, dict)
        and all(
            type(value) is bool if key == "no_tests_ran" else type(value) is int and value >= 0
            for key, value in outcomes.items()
        )
    )


def _valid_receipt(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    status, commands = data.get("status"), data.get("commands")
    if not isinstance(status, str) or not isinstance(commands, list) or "tree_fingerprint" not in data:
        return False
    if "planned_commands" in data:
        planned = data["planned_commands"]
        if (
            not isinstance(planned, list)
            or not planned
            or not all(isinstance(command, str) and command.strip() for command in planned)
        ):
            return False
    inherited = data.get("inherited_test_env")
    if inherited is not None and not isinstance(inherited, dict):
        return False
    stamp = data.get("gating")
    if stamp is not None and not isinstance(stamp, dict):
        return False
    plan = stamp.get("plan") if stamp is not None else None
    if plan is not None and (not isinstance(plan, dict) or not _valid_oid(plan.get("snapshot_tree"))):
        return False
    return (
        _valid_oid(data["tree_fingerprint"])
        and (data.get("tree_stable") is None or type(data["tree_stable"]) is bool)
        and (status in _UNFINISHED or bool(commands))
        and all(_valid_command(command) for command in commands)
    )


def _name_second(name: str) -> datetime | None:
    if _RUN_NAME.fullmatch(name) is not None:
        try:
            return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            pass
    return None


def _snapshot_tree(receipt: dict[str, Any]) -> str | None:
    return ((receipt.get("gating") or {}).get("plan") or {}).get("snapshot_tree")


def _move_out_command(path: Path) -> str:
    return f'mv -- {push_gate_display.shell_arg(path.as_posix())} "$(mktemp -d)"'


class _Receipts:
    def __init__(self, target: Path, deadline: float) -> None:
        self.target = target
        self.root = target / ".brigade" / "work" / "verify-runs"
        self.deadline = deadline
        self._cap_error: _Indeterminate | None = None

    def _names(self) -> Iterator[str]:
        try:
            if not stat.S_ISDIR(self.root.lstat().st_mode):
                raise _Indeterminate(
                    f"Verification receipt store {str(self.root)!r} must be a directory. "
                    "Move the symbolic link or other file and restore a real receipt directory.",
                    remedy=_move_out_command(self.root),
                )
            with os.scandir(self.root) as dirs:
                for entry in dirs:
                    _remaining(self.deadline)
                    if entry.is_symlink():
                        raise self._entry_problem(entry.name, "is a symbolic link")
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            (self.root / entry.name / "receipt.json").lstat()
                        except FileNotFoundError:
                            continue  # A run that never wrote a receipt contributes no evidence.
                        yield entry.name
        except FileNotFoundError:
            return

    def _entry_problem(self, name: str, description: str, *, receipt: bool = False) -> _Indeterminate:
        directory = self.root / name
        entry = directory / "receipt.json" if receipt else directory
        return _Indeterminate(
            f"Verification entry {str(entry)!r} {description}. Move the run out of the receipt store.",
            remedy=_move_out_command(directory),
        )

    def inventory(self, *, limit: int | None = None) -> tuple[list[tuple[str, datetime]], list[str]]:
        named: list[tuple[str, datetime]] = []
        stray: list[str] = []
        for name in self._names():
            second = _name_second(name)
            if second is None:
                stray.append(name)
            elif limit is None or len(named) < limit:
                named.append((name, second))
        return named, stray

    @cached_property
    def entries(self) -> list[tuple[str, datetime | None, dict[str, Any] | _Indeterminate | None]]:
        if self._cap_error is not None:
            raise self._cap_error
        named, stray = self.inventory(limit=_RECEIPT_LIMIT + 1)
        entries: list[tuple[str, datetime | None, dict[str, Any] | _Indeterminate | None]] = []
        ignored = 0
        first_ignored = ""
        for name in stray:
            receipt = self._load(name, ignore_intake_refusals=True)
            if isinstance(receipt, _Indeterminate):
                ignored += 1
                first_ignored = first_ignored or name
            elif receipt is not None:
                # Only the binding is needed to reject a matching stray directory.
                entries.append(
                    (
                        name,
                        None,
                        {
                            "tree_fingerprint": receipt["tree_fingerprint"],
                            "gating": {"plan": {"snapshot_tree": _snapshot_tree(receipt)}},
                        },
                    )
                )
        if ignored:
            push_gate_display.diagnostic(
                f"push gate: ignoring malformed receipt in a non-run directory {str(self.root / first_ignored)!r} "
                f"({ignored} entr{'y' if ignored == 1 else 'ies'}).",
            )
        if len(named) > _RECEIPT_LIMIT:
            try:
                _, archive = config.resolve_verify_archive(self.target)
            except (OSError, ValueError):
                archive = self.target / config.DEFAULT_VERIFY_ARCHIVE_DIR
            self._cap_error = _Indeterminate(
                f"The local receipt scan reached its limit of {_RECEIPT_LIMIT} in {str(self.root)!r}. "
                f"Set verify_runs_keep in {str(self.target / config.CONFIG_REL_PATH)!r} to at most {_RECEIPT_LIMIT} "
                f"and check that the verify archive {str(archive)!r} is writable and has space.",
                remedy=f"brigade work verify push-gate prune --target {push_gate_display.shell_arg(str(self.target))}",
            )
            raise self._cap_error
        entries.extend(
            (name, second, self._load(name, ignore_intake_refusals=True))
            for name, second in sorted(named, reverse=True)
        )
        return entries

    def _load(self, name: str, *, ignore_intake_refusals: bool = False) -> dict[str, Any] | _Indeterminate | None:
        _remaining(self.deadline)
        path = self.root / name / "receipt.json"
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                return self._entry_problem(name, "is not a regular file", receipt=True)
            with path.open("rb") as stream:
                _remaining(self.deadline)
                raw = stream.read(_RECEIPT_BYTES + 1)
            _remaining(self.deadline)
            if len(raw) > _RECEIPT_BYTES:
                return self._entry_problem(name, "exceeds the size limit", receipt=True)
            data = json.loads(raw)
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            _remaining(self.deadline)
            return self._entry_problem(name, "is malformed", receipt=True)
        except OSError:
            return self._entry_problem(name, "could not be read", receipt=True)
        valid = _valid_receipt(data)
        _remaining(self.deadline)
        if not valid:
            return self._entry_problem(name, "is malformed", receipt=True)
        # The writer records an intake refusal without argv, before starting a child.
        # Keep it visible to the prune command, which inventories receipts rather than judging evidence.
        commands = data["commands"]
        if (
            ignore_intake_refusals
            and data["status"] == "rejected"
            and ("planned_commands" not in data or len(data["planned_commands"]) == len(commands))
            and all(
                record.get("status") == "rejected" and record.get("exit_code") == 2 and "argv" not in record
                for record in commands
            )
        ):
            return None
        # Keep the saved evaluator inputs and omit unrelated receipt data.
        return {
            **{
                key: data[key]
                for key in (
                    "status",
                    "tree_stable",
                    "tree_fingerprint",
                    "started_at",
                    "gating",
                    "planned_commands",
                    "inherited_test_env",
                )
                if key in data
            },
            "commands": [
                {key: record[key] for key in ("argv", "status", "test_outcomes", "env") if record.get(key) is not None}
                for record in commands
            ],
        }

    def matching(self, tree: str) -> Iterator[Iterator[dict[str, Any]]]:
        entries = self.entries
        now = datetime.now(UTC)
        ordered: list[tuple[datetime, dict[str, Any] | _Indeterminate]] = []
        for name, second, receipt in entries:
            _remaining(self.deadline)
            if receipt is None:
                continue
            if isinstance(receipt, _Indeterminate):
                if second is None or second > now:
                    raise receipt
                ordered.append((second, receipt))
                continue
            if receipt["tree_fingerprint"] != tree or _snapshot_tree(receipt) not in (None, tree):
                continue
            started = _receipt_started_at(receipt)
            if second is None or started is None or started.replace(microsecond=0) != second:
                raise self._entry_problem(name, "has an invalid or mismatched start timestamp")
            if started > now:
                raise self._entry_problem(name, "has a start timestamp in the future")
            ordered.append((second, receipt))

        for _, group in groupby(ordered, key=lambda entry: entry[0]):
            yield self._group(group)

    @staticmethod
    def _group(group: Iterator[tuple[datetime, dict[str, Any] | _Indeterminate]]) -> Iterator[dict[str, Any]]:
        for _, receipt in group:
            if isinstance(receipt, _Indeterminate):
                raise receipt
            yield receipt


def _remedy(
    target: Path,
    policy: verify_gate._GatePolicy,
    receipt: dict[str, Any],
    *,
    fresh: bool = False,
    argv: list[str] | None = None,
) -> str:
    def render(argv: list[str]) -> str:
        return (
            f"brigade work verify run --target {push_gate_display.shell_arg(str(target))} "
            f"--argv-json {push_gate_display.shell_arg(json.dumps(argv))}"
        )

    stamp = receipt.get("gating")
    plan = stamp.get("plan") if isinstance(stamp, dict) else None
    if argv is None and isinstance(plan, dict):
        argv = plan.get("run_argv")
    if isinstance(argv, list) and all(isinstance(arg, str) and arg for arg in argv):
        prefix = policy.argv_prefix
        if len(argv) > len(prefix) and tuple(argv[: len(prefix)]) == prefix:
            return render(argv) + (" --no-reuse" if fresh else "")
    command = render(verify_gate._suggested_argv(policy))
    if fresh:
        command += " --no-reuse"
    if policy.needs_arguments:
        command += "\nFind the required test files with brigade work verify required --worktree."
    return command


def _tree_verdict(
    target: Path, policy: verify_gate._GatePolicy, tree: str, receipts: _Receipts, deadline: float
) -> _Verdict:
    prefix = list(policy.argv_prefix)

    rejection: tuple[tuple[bool, datetime], _Candidate] | None = None
    failure: _Verdict | None = None

    def invocation_rejection() -> _Verdict | None:
        if rejection is None or rejection[1].invocation_notice is None:
            return None
        candidate = rejection[1]
        return _Verdict(
            "rejected",
            candidate.invocation_notice,
            _remedy(target, policy, candidate.receipt, fresh=True),
            _AMBIGUOUS_INVOCATION,
        )

    try:
        for group in receipts.matching(tree):
            candidates = (
                _classify_receipt(policy, prefix, tree, receipt, deadline, target=target) for receipt in group
            )
            ordered = _order_candidates(candidates) if prefix else list(candidates)
            for candidate in ordered:
                if not prefix and candidate.receipt["status"] in _UNFINISHED:
                    continue
                if candidate.uncertain:
                    if candidate.invocation_notice:
                        raise _Indeterminate(
                            candidate.invocation_notice,
                            _AMBIGUOUS_INVOCATION,
                            remedy=_remedy(target, policy, candidate.receipt, fresh=True),
                        )
                    if candidate.reason == _UNCONFIRMED_PLAN:
                        raise _Indeterminate(
                            "The planned commands and recorded results do not establish "
                            "an outcome for the required check."
                        )
                    raise _Indeterminate(
                        "The most recent possible run of the required check has an inconclusive status."
                    )
                if candidate.eligible:
                    if failure is None:
                        return _Verdict("ok")
                    return invocation_rejection() or replace(
                        failure,
                        reason=f"{failure.reason} An earlier run had passed.",
                        reason_code=_MASKED_PASS,
                    )
                if failure is None:
                    if candidate.failed:
                        # The remaining scan can only confirm whether an earlier pass existed.
                        failure = _rejected(
                            target,
                            policy,
                            candidate,
                            "The most recent run of the required check",
                        )
                    rank = (
                        candidate.ran_required or bool(candidate.invocation_notice),
                        candidate.started_at or _EARLIEST,
                    )
                    if rejection is None or rank > rejection[0]:
                        rejection = rank, candidate
    except _Indeterminate:
        if failure is None:
            raise

    if failure is not None:
        return invocation_rejection() or failure
    _remaining(deadline)
    if rejection is None:
        return _Verdict(
            "missing_receipt", "No local verification receipt matches this tree.", _remedy(target, policy, {})
        )
    _, candidate = rejection
    if notice := invocation_rejection():
        return notice
    run_label = "The most recent run of the required check" if candidate.ran_required else "The last run"
    return _rejected(target, policy, candidate, run_label)


def _env_prefix_message(names: tuple[str, ...]) -> str:
    shown = ", ".join(push_gate_display.terminal_text(name) for name in names[:4])
    if len(names) > 4:
        shown += f" and {len(names) - 4} more"
    return (
        f"A run with {shown} set in front of the required check does not count. "
        "Run the required check again without the environment prefix."
    )


# Only options with a known command boundary are parsed. For example,
# env's --split-string parses a command and env -C changes its working directory.
# Combined short options are deliberately opaque.
_WRAPPER_FLAGS = {
    "env": {"-", "-i", "--ignore-environment", "-v", "--debug"},
    "command": {"-p"},
    "nice": set(),
    "nohup": set(),
    "time": {"-p", "--portability", "-a", "--append", "-v", "--verbose", "-q", "--quiet"},
    "timeout": {"--foreground", "--preserve-status", "-v", "--verbose"},
    "stdbuf": set(),
}
_WRAPPER_VALUES = {
    "env": {"-u", "--unset"},
    "nice": {"-n", "--adjustment"},
    "time": {"-f", "--format", "-o", "--output"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
}
_OPAQUE_LAUNCHERS = constants.SCANNER_HIGH_RISK_COMMANDS | constants._POLICY_LAUNCHERS | {"eval", "just", "make", "tox"}
_INTERPRETERS = {"node", "ruby", "perl", "php"}
_PYTHON_LAUNCHER = re.compile(r"python[0-9.]*|py")
_PYTHON_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}
_SHELL_LAUNCHERS = {"bash", "sh", "dash", "zsh", "ksh", "fish"}
_SHELL_COMMAND = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_FIRST_SCRIPT_WORD = re.compile(r"""^\s*(?:exec\s+)?(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))""")
_SCRIPT_SUFFIXES = {"py", "sh", "bash", "rb", "js", "pl", "exe", "bat", "cmd"}
_PYTEST_ALIAS = re.compile(r"^py\.test(?=$|\.(?:exe|cmd|bat)$)")
_DURATION = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[smhd]?")


def _unwrap(argv: tuple[str, ...], launcher: str) -> tuple[tuple[str, ...], list[str]] | None:
    index = 1
    values = _WRAPPER_VALUES.get(launcher, set())
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if arg in _WRAPPER_FLAGS[launcher] or (launcher == "nice" and re.fullmatch(r"-[0-9]+", arg)):
            index += 1
        elif arg in values:
            index += 2
        elif (arg.startswith("--") and arg.partition("=")[0] in values and "=" in arg) or (
            len(arg) > 2 and arg[:2] in values
        ):
            index += 1
        elif arg.startswith("-"):
            return None
        else:
            break
    if launcher == "timeout":
        if index >= len(argv) or not _DURATION.fullmatch(argv[index]):
            return None
        index += 1
    env_start = index
    if launcher == "env":
        while index < len(argv) and _ENV_ASSIGNMENT.match(argv[index]):
            index += 1
    elif index < len(argv) and _ENV_ASSIGNMENT.match(argv[index]):
        return None
    names = [arg.partition("=")[0] for arg in argv[env_start:index]]
    return argv[index:], names


def _without_assignments(argv: Sequence[str]) -> tuple[str, ...]:
    start = next((i for i, arg in enumerate(argv) if not _ENV_ASSIGNMENT.match(arg)), len(argv))
    return tuple(argv[start:])


def _generic_launcher(name: str) -> bool:
    return (
        name in _OPAQUE_LAUNCHERS
        or name in _WRAPPER_FLAGS
        or name in _INTERPRETERS
        or _PYTHON_LAUNCHER.fullmatch(name) is not None
    )


def _launcher_target(launcher: str, args: Sequence[str]) -> str:
    if launcher in _WRAPPER_FLAGS:
        unwrapped = _unwrap((launcher, *args), launcher)
        if unwrapped is not None and unwrapped[0]:
            return unwrapped[0][0]
    subcommands = constants._POLICY_LAUNCHER_SUBCOMMANDS.get(launcher, set())
    if launcher == "npm":
        subcommands = subcommands | {"run"}
    python = _PYTHON_LAUNCHER.fullmatch(launcher) is not None
    values = iter(args)
    for arg in values:
        if python:
            if arg in _PYTHON_VALUE_OPTIONS:
                next(values, None)
                continue
            if arg.startswith("-c") or arg == "-":
                return ""
            if arg.startswith("-m") and len(arg) > 2:
                return arg[2:]
        if launcher in _SHELL_LAUNCHERS and _SHELL_COMMAND.fullmatch(arg):
            return ""
        if arg in {"-m", "--"}:
            return next(values, "")
        if arg and not arg.startswith("-") and not _ENV_ASSIGNMENT.match(arg):
            if arg in subcommands:
                continue
            return arg
    return ""


def _script_suffix(name: str) -> tuple[str, bool]:
    stem, _, suffix = name.rpartition(".")
    return (stem, True) if suffix.casefold() in _SCRIPT_SUFFIXES else (name, False)


def _mentions_check(launcher: str, args: Sequence[str], name: str, *, windows: bool) -> bool:
    if not name:
        return False
    terms = (name, "py.test") if name == "pytest" else (name,)
    alternatives = "|".join(re.escape(term) for term in terms)
    flags = re.IGNORECASE if windows else 0
    script_suffix = "|".join(sorted(_SCRIPT_SUFFIXES))
    mention = re.compile(rf"(?<![\w.-])(?:{alternatives})(?:\.(?:{script_suffix}))?(?![\w.-])", flags)
    if any(mention.search(arg) for arg in args):
        return True

    target = _launcher_target(launcher, args)
    if not target and launcher in _SHELL_LAUNCHERS:
        source = next(
            (value for option, value in zip(args, args[1:], strict=False) if _SHELL_COMMAND.fullmatch(option)), ""
        )
        if first := _FIRST_SCRIPT_WORD.match(source):
            target = next(word for word in first.groups() if word is not None)
    path = target.replace("\\", "/")
    script = path.rsplit("/", 1)[-1]
    stem, is_script = _script_suffix(script)
    if not is_script or (script.startswith("test_") and "tests" in path.split("/")[:-1]):
        return False
    segments = "|".join(re.escape(_script_suffix(term)[0]) for term in terms)
    return re.search(rf"(?:^|[-_])(?:{segments})(?:$|[-_])", stem, flags) is not None


def _runner_alias(program: str) -> str:
    directory, separator, name = program.rpartition("/")
    return directory + separator + _PYTEST_ALIAS.sub("pytest", name)


def _program_spelling(executable: str, *, windows: bool) -> str:
    return constants.normalize_policy_executable(executable, windows=True) if windows else executable


def _required_identity(
    argv: Sequence[str],
    prefix: Sequence[str],
    *,
    windows: bool,
    env_names: Sequence[str] = (),
    invocation: _Invocation | None = None,
) -> tuple[str, ...] | Literal["ambiguous"] | None:
    """Recognize an exact invocation or possible evidence of the required check."""
    if not prefix or not argv:
        return None
    args = _without_assignments(argv)
    names = [*env_names, *(arg.partition("=")[0] for arg in argv[: len(argv) - len(args)])]
    original = tuple(argv)
    exact = original[: len(prefix)] == tuple(prefix)

    normalize = partial(_program_spelling, windows=windows)
    basename = ntpath.basename if windows else posixpath.basename
    outer_wrapper = args[0] if args and basename(normalize(args[0])) in _WRAPPER_FLAGS else ""

    def ambiguous(*, spelling: str = "") -> Literal["ambiguous"]:
        if invocation is not None:
            invocation.variables.update(name for name in names if _ENV_NAME.fullmatch(name))
            invocation.wrapper = invocation.wrapper or outer_wrapper
            invocation.spelling = invocation.spelling or spelling
        return "ambiguous"

    if exact:
        if len(original) == 2 and original[1] in _READ_ONLY_OPTIONS:
            return None
        if outer_wrapper:
            if basename(normalize(outer_wrapper)) == "env" and (unwrapped := _unwrap(args, "env")) is not None:
                names.extend(unwrapped[1])
            return ambiguous()
        return ambiguous() if names else original
    if _ENV_ASSIGNMENT.match(prefix[0]):
        return None

    policy_program = normalize(prefix[0])
    policy_basename = _runner_alias(basename(policy_program))
    policy_launcher = policy_basename
    target = _launcher_target(policy_launcher, prefix[1:]) if _generic_launcher(policy_launcher) else ""
    name = _runner_alias(basename(normalize(target))) if target else policy_launcher
    possible_match = False
    for depth in range(_WRAPPER_DEPTH + 1):
        if not args:
            break
        program = normalize(args[0])
        launcher = basename(program)
        same_program = program == policy_program
        if same_program and len(args) == 2 and args[1] in _READ_ONLY_OPTIONS:
            return None
        if same_program and tuple(prefix[1:]) == args[1 : len(prefix)]:
            return ambiguous(spelling=args[0] if args[0] != prefix[0] else "")
        if not same_program and _runner_alias(basename(program)) == policy_basename:
            return ambiguous(spelling=args[0])
        if program == launcher and launcher in _WRAPPER_FLAGS and depth < _WRAPPER_DEPTH:
            unwrapped = _unwrap(args, launcher)
            if unwrapped is not None:
                possible_match |= same_program and _mentions_check(launcher, args[1:], name, windows=windows)
                args, wrapper_names = unwrapped
                names.extend(wrapper_names)
                continue
        if same_program and not target:
            return ambiguous(spelling=args[0] if args[0] != prefix[0] else "")
        if _generic_launcher(launcher) and _mentions_check(launcher, args[1:], name, windows=windows):
            return ambiguous()
        break
    return ambiguous() if possible_match else None


def _planned_argvs(command: str) -> set[tuple[str, ...]]:
    # The writer preserves raw --command text but uses POSIX shlex.join for
    # argv lists on every platform. The receipt does not identify the form.
    if len(command) > _PLAN_CHARS or "\x00" in command:
        return set()
    parsers = (shlex.split, split_verify_command) if os.name == "nt" else (shlex.split,)
    alternatives: set[tuple[str, ...]] = set()
    for parse in parsers:
        try:
            argv = parse(command)
        except ValueError:
            continue
        if len(argv) > _PLAN_TOKENS:
            return set()
        if argv and argv[0]:
            alternatives.add(tuple(argv))
    return alternatives


def _required_plan_unconfirmed(
    planned: list[str],
    commands: list[dict[str, Any]],
    identities: list[tuple[tuple[str, ...] | Literal["ambiguous"] | None, _Invocation]],
    prefix: list[str],
    deadline: float,
    invocation: _Invocation,
) -> bool:
    if len(planned) > _PLAN_COMMANDS:
        return True
    for command, record, recorded in zip_longest(planned, commands, identities):
        _remaining(deadline)
        recorded_identity, recorded_invocation = recorded if recorded is not None else (None, _Invocation())
        if command is None:
            if recorded_identity is not None:
                invocation.include(recorded_invocation)
                return True
            continue
        possible = _planned_argvs(command)
        if not possible:
            return True
        planned_invocation = _Invocation()
        planned_identities = {
            _required_identity(argv, prefix, windows=os.name == "nt", invocation=planned_invocation)
            for argv in possible
        }
        if isinstance(recorded_identity, tuple):
            if recorded_identity not in planned_identities:
                invocation.include(planned_invocation)
                return True
        elif any(isinstance(identity, tuple) for identity in planned_identities):
            invocation.include(recorded_invocation)
            return True
        elif "ambiguous" in planned_identities or recorded_identity == "ambiguous":
            if record is None or not any(
                _without_assignments(record.get("argv") or ()) == _without_assignments(argv) for argv in possible
            ):
                invocation.include(planned_invocation)
                invocation.include(recorded_invocation)
                return True
    return False


def _recheck_plan(
    target: Path,
    policy: verify_gate._GatePolicy,
    tree: str,
    old_base: str,
    receipt: dict[str, Any],
    required: list[dict[str, Any]],
    deadline: float,
) -> tuple[bool, str, list[str] | None]:
    base = policy.base_commit
    assert base is not None
    # If planning fails, the saved command can still be run against the current base.
    argv = receipt["gating"]["plan"].get("run_argv") or required[0]["argv"]
    try:
        ancestry = verify_manifest_git._git(
            target, "merge-base", "--is-ancestor", old_base, base, deadline=deadline, git_env=_offline_git_env()
        )
        if ancestry.returncode == 1:
            return False, _DIFFERENT_PLAN_BASE, argv
        if ancestry.returncode != 0:
            return False, _OLDER_PLAN_BASE, argv
        fields, _, entries = verify_required.plan_snapshot(target, base, tree, deadline=deadline, offline=True)
        tests = fields.get("tests", [])
        if (
            fields["mode"] != "selective"
            or not tests
            or any(verify_gate._coverage_path(path) != path for path in tests)
        ):
            return False, _OLDER_PLAN_BASE, argv
        argv = [*policy.argv_prefix, *tests]
        covered = verify_gate._covered_files({**receipt, "commands": required}, policy, entries)
        if set(tests) <= covered:
            _remaining(deadline)
            return True, "passed_required_check", None
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, _Indeterminate):
        pass
    return False, _OLDER_PLAN_BASE, argv


def _classify_receipt(
    policy: verify_gate._GatePolicy,
    prefix: list[str],
    tree: str,
    receipt: dict[str, Any],
    deadline: float,
    *,
    target: Path | None = None,
) -> _Candidate:
    eligible, reason = verify_gate.evaluate_receipt(policy, receipt, tree, deadline=deadline)
    if reason == "policy_timeout":
        raise _Indeterminate(_TIMEOUT, "check_timeout")
    old_base = None
    if target is not None and reason == "policy_changed" and policy.needs_plan_coverage:
        recorded = ((receipt.get("gating") or {}).get("plan") or {}).get("base_commit")
        if isinstance(recorded, str) and _valid_oid(recorded) and recorded != policy.base_commit:
            prior_policy = replace(policy, base_commit=recorded)
            if verify_gate.evaluate_receipt(prior_policy, receipt, tree, deadline=deadline)[0]:
                eligible, old_base = True, recorded
    identities = []
    required = []
    ambiguous = False
    excluded = _Invocation()
    inconclusive = _Invocation()
    prefixed_coverage = False
    for command in receipt["commands"]:
        _remaining(deadline)
        argv = command.get("argv") or ()
        env_names = command.get("env") or ()
        invocation = _Invocation()
        identity = _required_identity(argv, prefix, windows=os.name == "nt", env_names=env_names, invocation=invocation)
        identities.append((identity, invocation))
        if isinstance(identity, tuple):
            required.append(command)
        elif identity == "ambiguous":
            excluded.include(invocation)
            # A saved coverage verdict may include a command whose inline
            # assignments the writer moved out of argv. Do not use that stamp.
            prefixed_coverage |= bool(policy.needs_plan_coverage and env_names and argv[: len(prefix)] == prefix)
            if _command_status(receipt, command) != "completed" or _failed_required_check(receipt, [command]):
                ambiguous = True
                inconclusive.include(invocation)
    statuses = {_command_status(receipt, command) for command in required}
    failed = _failed_required_check(receipt, required)
    plan_invocation = _Invocation()
    plan_unconfirmed = (
        bool(prefix)
        and "planned_commands" in receipt
        and _required_plan_unconfirmed(
            receipt["planned_commands"], receipt["commands"], identities, prefix, deadline, plan_invocation
        )
    )
    uncertain = plan_unconfirmed or (
        bool(prefix)
        and not failed
        and (
            ambiguous
            or (
                "planned_commands" not in receipt
                and receipt["status"] not in {"completed", "failed", "rejected", "timed_out"}
            )
            or any(command.get("argv") is None for command in receipt["commands"])
            or bool(statuses - {"completed", "rejected"})
        )
    )
    if plan_unconfirmed:
        eligible, reason = False, _UNCONFIRMED_PLAN
    elif "timed_out" in statuses:
        eligible, reason = False, "required_check_timed_out"
    elif failed or uncertain or "rejected" in statuses:
        if eligible:
            eligible, reason = False, "not_completed"
    elif prefix and eligible:
        eligible, reason = verify_gate._required_check_verdict(receipt, policy)
        if eligible and (
            prefixed_coverage
            or not any(
                (not policy.needs_arguments or len(command["argv"]) > len(prefix))
                and (not policy.needs_passed_tests or verify_gate._tests_passed(command))
                for command in required
            )
        ):
            eligible, reason = False, "not_the_required_check"
    rerun_argv = None
    if eligible and old_base is not None and target is not None:
        eligible, reason, rerun_argv = _recheck_plan(target, policy, tree, old_base, receipt, required, deadline)
    inconclusive.include(plan_invocation)
    notice = inconclusive.message() if uncertain else excluded.message() if not required or prefixed_coverage else None
    return _Candidate(
        receipt,
        _receipt_started_at(receipt),
        bool(required),
        eligible and not uncertain and not failed,
        failed or (bool(required) and not eligible and not uncertain and not prefixed_coverage),
        reason,
        uncertain,
        notice,
        rerun_argv,
    )


def _order_candidates(candidates: Iterator[_Candidate]) -> list[_Candidate]:
    """Order one second of runs without using run ID suffixes to break a pass/failure tie."""
    group = list(candidates)
    passes = {item.started_at for item in group if item.eligible}
    failures = {item.started_at for item in group if item.failed or item.uncertain}
    if passes and failures and (None in passes or None in failures or passes & failures):
        raise _Indeterminate(
            "The push check could not order a pass and failure in the same second. "
            "Run verification for the pushed tree again."
        )
    return sorted(group, key=lambda item: item.started_at or _EARLIEST, reverse=True)


def _command_status(receipt: dict[str, Any], command: dict[str, Any]) -> str:
    status: str | None = command.get("status")
    return receipt["status"] if status is None else status


def _failed_required_check(receipt: dict[str, Any], commands: list[dict[str, Any]]) -> bool:
    """Recognize required command failure, timeout, or recorded test failures and errors."""
    for command in commands:
        outcomes = command.get("test_outcomes")
        status = _command_status(receipt, command)
        if isinstance(outcomes, dict):
            for count in (outcomes.get("failed"), outcomes.get("errors")):
                if type(count) is int and count > 0:
                    return True
        if status in ("failed", "timed_out"):
            return True
    return False


def _rejected(
    target: Path,
    policy: verify_gate._GatePolicy,
    candidate: _Candidate,
    run_label: str,
) -> _Verdict:
    receipt, reason = candidate.receipt, candidate.reason
    explanation, _ = verify_gate._plan_guidance(receipt, reason)
    if reason == "required_check_timed_out":
        explanation = "The last run timed out."
    elif reason in {_OLDER_PLAN_BASE, _DIFFERENT_PLAN_BASE}:
        base = "an older base" if reason == _OLDER_PLAN_BASE else "a different base"
        explanation = f"The required check was run against {base}. Run it again after the merge."
    explanation = explanation or f"The last run has an ineligible verdict ({reason.replace('_', ' ')})."
    if explanation.startswith("The last run"):
        explanation = run_label + explanation.removeprefix("The last run")
    return _Verdict(
        "rejected",
        explanation,
        _remedy(target, policy, receipt, argv=candidate.rerun_argv),
    )


def _offline_git_env() -> dict[str, str]:
    # Object reads in partial clones must stay offline even on Git versions
    # that do not recognize GIT_NO_LAZY_FETCH.
    return {
        **verify_manifest_git.trusted_git_env(),
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _check(
    target: Path, updates: list[tuple[str, str]], ref: str | None, deadline: float, *, details: bool = False
) -> list[dict[str, Any]]:
    git_env = _offline_git_env()
    git = partial(verify_manifest_git._git, deadline=deadline, git_env=git_env)
    try:
        root = git(target, "rev-parse", "--show-toplevel")
        if root.returncode != 0:
            raise _Indeterminate("Git could not locate the worktree.")
        target = Path(os.fsdecode(root.stdout.rstrip(b"\r\n")))
        if ref is not None:
            resolved = git(target, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            if resolved.returncode:
                raise ValueError("The supplied ref does not resolve to a local commit.")
            updates = [(ref, resolved.stdout.decode("ascii").strip())]
    except (subprocess.TimeoutExpired, _Indeterminate) as exc:
        verdict = (
            _Verdict("indeterminate", _TIMEOUT, reason_code="check_timeout")
            if isinstance(exc, subprocess.TimeoutExpired)
            else _Verdict("indeterminate", str(exc), reason_code=exc.reason_code)
        )
        if details:
            return [
                {**verdict.public(name), "tip": commit, "tree": None, "reason_code": verdict.reason_code}
                for name, commit in updates
            ]
        names: list[str | None] = [name for name, _ in updates] if updates else [ref]
        return [verdict.public(name) for name in names]

    receipts = _Receipts(target, deadline)
    by_commit: dict[str, _Verdict] = {}
    by_tree: dict[tuple[verify_gate._GatePolicy, str], _Verdict] = {}

    bases = verify_manifest_git._BaseResolver(target, deadline=deadline, git_env=git_env)

    @cache
    def committed_policy(commit: str) -> verify_gate._GatePolicy | None:
        return verify_gate._committed_policy(target, commit, deadline, git_env=git_env)

    @cache
    def read_tree(commit: str) -> str:
        resolved = git(target, "rev-parse", "--verify", f"{commit}^{{commit}}^{{tree}}")
        if resolved.returncode:
            raise _Indeterminate("Git could not read the pushed commit's tree.")
        return resolved.stdout.decode("ascii").strip()

    observed_trees: dict[str, str] = {}

    def check_tip(commit: str) -> _Verdict:
        _remaining(deadline)
        tree = observed_trees[commit] = read_tree(commit)
        base = bases.resolve(commit)
        if base.timed_out:
            raise _Indeterminate(base.message or _TIMEOUT, "check_timeout")
        if base.status != "resolved" or base.commit is None:
            raise _Indeterminate(base.message or "The trusted Git base is unresolved.")
        if base.default_branch_tip is None:
            raise _Indeterminate("Git could not find a fetched default branch tip.")
        policy = committed_policy(base.commit)
        if policy is None:
            tip = base.default_branch_tip
            tip_policy = committed_policy(tip) if tip != base.commit else None
            if tip_policy is None:
                return _Verdict("not_applicable", tree=tree)
            if tip_policy.mode == "unresolved":
                raise _Indeterminate(verify_gate._policy_error(tip_policy))
            return _Verdict(
                "base_predates_policy",
                f"Update the branch from {base.reference} to pick up the required check.",
                tree=tree,
            )
        if policy.mode == "unresolved":
            raise _Indeterminate(verify_gate._policy_error(policy))
        try:
            key = (policy, tree)
            if key not in by_tree:
                by_tree[key] = _tree_verdict(target, policy, tree, receipts, deadline)
        except (subprocess.TimeoutExpired, _Indeterminate) as exc:
            reason = _TIMEOUT if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
            code = "check_timeout" if isinstance(exc, subprocess.TimeoutExpired) else exc.reason_code
            recovery = exc.remedy if isinstance(exc, _Indeterminate) else None
            return _Verdict("indeterminate", reason, recovery or _remedy(target, policy, {}, fresh=True), code, tree)
        return replace(by_tree[key], tree=tree)

    results = []
    for name, commit in updates:
        if commit not in by_commit:
            try:
                by_commit[commit] = check_tip(commit)
            except subprocess.TimeoutExpired:
                by_commit[commit] = _Verdict("indeterminate", _TIMEOUT, reason_code="check_timeout")
            except _Indeterminate as exc:
                by_commit[commit] = _Verdict("indeterminate", str(exc), reason_code=exc.reason_code)
            except OSError as exc:
                by_commit[commit] = _Verdict("indeterminate", str(exc), reason_code="check_failed")
        verdict = by_commit[commit]
        result = verdict.public(name)
        if details:
            result.update(tip=commit, tree=verdict.tree or observed_trees.get(commit), reason_code=verdict.reason_code)
        results.append(result)
    return results


def push_check(*, target: Path, ref: str | None, json_output: bool) -> int:
    """Read Git pre-push updates or check a single local commit ref."""
    deadline = monotonic() + _BUDGET_SECONDS
    try:
        updates = branch_updates(_read_stdin(deadline)) if ref is None else []
        results = _check(target, updates, ref, deadline) if updates or ref is not None else []
    except ValueError as exc:
        push_gate_display.diagnostic(f"push-check: {exc}")
        return 2
    except (OSError, _Indeterminate) as exc:
        results = [_Verdict("indeterminate", str(exc)).public(ref)]

    if json_output:
        print(json.dumps({"refs": results}))
    else:
        for result in results:
            if result["status"] == "not_applicable":
                continue
            name = result["ref"] or "push-check"
            print(push_gate_display.terminal_text(f"{name}: {result['status']}"))
            if result["reason"]:
                print(push_gate_display.terminal_text(result["reason"]))
            if result["remedy"]:
                for line in result["remedy"].split("\n"):
                    print(push_gate_display.terminal_text(line))

    statuses = {result["status"] for result in results}
    if statuses & {"missing_receipt", "rejected"}:
        return 1
    return 3 if statuses & {"base_predates_policy", "indeterminate"} else 0
