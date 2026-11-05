"""brigade command-line entrypoint."""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback

from .. import __version__
from .. import extras as _extras_mod

# Shared parser helpers live in cli._common. COMMAND_GROUPS is re-exported here
# so it stays on the public surface (tests reference cli.COMMAND_GROUPS).
from ._common import (
    COMMAND_GROUPS as COMMAND_GROUPS,
    _START_HERE,
    _TopLevelHelpFormatter,
    _grouped_epilog,
)
from ._index import COMMANDS, COMMANDS_BY_NAME, EXTRAS_STUB_HELP

# Command families are imported on first use. ``brigade <family> ...`` imports
# and registers only that family's module; the others get placeholder
# subparsers built from the static index in ``cli._index``. ``_build_parser()``
# still returns the full tree for completions, ROADMAP command checks, and
# tests that walk every subparser.


def __getattr__(name: str):
    """Resolve historical eager attributes (``cli._init_group``, ``cli.fleet``) on demand."""
    if name == "prompt_for_selection":
        # Stays patchable on the package: ``cli.init`` reads it from here.
        from ..prompt import prompt_for_selection as value
    elif name.startswith("_") and name.endswith("_group") and name[1:-6] in _FAMILY_MODULES:
        value = _family_module(name[1:-6])
    elif name in _FAMILY_MODULES:
        value = _family_module(name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


_FAMILY_MODULES = frozenset(entry.module for entry in COMMANDS)

# Leaves whose trailing engine flags bypass argparse, keyed by family. ``code``
# verbs come from cli.code.ENGINE_VERBS, read only when argv selects ``code``.
_PASSTHROUGH_FAMILY_LEAVES = {
    "evidence": frozenset({"crawl", "search", "show", "explain", "stats"}),
    "search": frozenset({"sync", "context", "impact"}),
}

_PLAN_LEAVES = {("evidence", "crawl"), ("search", "sync")}


def _family_module(module: str):
    return importlib.import_module(f".{module}", __name__)


def _selected_command(argv: list[str]) -> str | None:
    """The top-level command argv selects, or None (help, version, or an invalid choice).

    The top-level parser has only value-less flags (-h, --help, --version), so
    the first token that is not a flag is the command argparse will select.
    """
    for token in argv:
        if not token.startswith("-"):
            return token if token in COMMANDS_BY_NAME else None
    return None


def _build_parser() -> argparse.ArgumentParser:
    """Build the full command tree, importing every command family."""
    return _assemble_parser(full=True)


def _build_parser_for(argv: list[str]) -> argparse.ArgumentParser:
    """Build the parser with real subparsers only for the family argv selects."""
    return _assemble_parser(full=False, selected=_selected_command(argv))


def _assemble_parser(*, full: bool, selected: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brigade",
        description=_START_HERE,
        formatter_class=_TopLevelHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"brigade {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    extras_enabled = _extras_mod.enabled()
    selected_entry = COMMANDS_BY_NAME.get(selected) if selected is not None else None

    for entry in COMMANDS:
        disabled = entry.extras and not extras_enabled
        if full or entry is selected_entry:
            if disabled:
                from . import extras as extras_group

                extras_group.register_stub(sub, entry.name)
            else:
                _family_module(entry.module).register(sub)
        else:
            # Same name, help line, and aliases as the real registration, so
            # top-level help and argparse's choice list are unchanged.
            sub.add_parser(
                entry.name,
                help=EXTRAS_STUB_HELP if disabled else entry.help,
                aliases=entry.aliases,
                add_help=False,
            )

    parser.epilog = _grouped_epilog(sub, extras_enabled=extras_enabled)
    return parser


def _peel_passthrough_engine_args(argv: list[str]) -> tuple[list[str], list[str] | None]:
    """Keep opaque engine flags out of argparse for known passthrough leaves."""

    leaf = tuple(argv[:2])
    if len(leaf) < 2:
        return argv, None
    if leaf[0] == "code":
        verbs = frozenset(_family_module("code").ENGINE_VERBS)
    else:
        verbs = _PASSTHROUGH_FAMILY_LEAVES.get(leaf[0], frozenset())
    if leaf[1] not in verbs:
        return argv, None
    if leaf in _PLAN_LEAVES and len(argv) > 2 and argv[2] == "plan":
        return argv, None
    if leaf[0] == "evidence" and leaf[1] in {"show", "explain", "stats"}:
        # Help remains local even after engine flags; a terminator makes it literal.
        for argument in argv[2:]:
            if argument == "--":
                break
            if argument in {"-h", "--help"}:
                return [*argv[:2], argument], None
        split = 2
        if leaf[1] == "show":
            while split < len(argv) and argv[split] == "--forensic-content":
                split += 1
            if "--" in argv[split:]:
                # Dispatch appends consumed forensic flags; keep them before `--`.
                return argv[:2], argv[2:]
        return argv[:split], argv[split:]
    return argv[:2], argv[2:]


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Usage is not a verification run. Resolve --help/-h before any pre-parse
    # atomic-capture policy so the flag list stays discoverable.
    if raw_argv[:1] == ["work"] and _family_module("work").work_verify_run_help_requested(raw_argv):
        parser = _build_parser_for(raw_argv)
        parser.parse_args(raw_argv)
        return 0
    parse_argv, engine_args = _peel_passthrough_engine_args(raw_argv)
    parser = _build_parser_for(parse_argv)
    if not parse_argv:
        print(_START_HERE, file=sys.stderr)
    args = parser.parse_args(parse_argv)
    if engine_args is not None:
        args.engine_args = engine_args

    # Command groups dispatch via set_defaults(func=...). The parser is attached
    # so dispatch functions can call parser.error for unreachable
    # unknown-subcommand cases (required subparsers normally prevent these).
    args._brigade_parser = parser
    func = getattr(args, "func", None)
    if func is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    if args.command == "fleet" and args.fleet_command == "keys" and args.keys_command in {"init", "show", "rotate"}:
        # Offline custody must not persist global diagnostics or launch an
        # update refresh, including after invalid scope or a lost return.
        return func(args)
    if args.command == "bug" and args.bug_command == "diagnose":
        # A selected-project preview must not persist global failure state or
        # read update caches and launch refreshes, including on safe refusal.
        return func(args)
    from .. import bug_last_error

    command = bug_last_error.command_path(parser, parse_argv)
    try:
        exit_code = func(args)
    except SystemExit as exc:
        usage_error = exc.code == 2 and any(
            frame.f_code is argparse.ArgumentParser.error.__code__ for frame, _ in traceback.walk_tb(exc.__traceback__)
        )
        if exc.code not in (None, 0) and command != "bug report" and not usage_error:
            bug_last_error.record(command, exc.code if isinstance(exc.code, int) else 1, exc, args=args)
        raise
    except Exception as exc:
        if command != "bug report":
            bug_last_error.record(command, 1, exc, args=args)
        raise
    if command == "bug report":
        return exit_code
    if exit_code:
        bug_last_error.record(command, exit_code, args=args)
    if args.command == "work" and args.work_command in {"presence-hook", "hook-run"}:
        # Agent hooks must never print update notices or launch cache refreshes.
        return exit_code
    try:
        from .. import update_notify

        update_notify.maybe_notify(parse_argv, exit_code)
    except Exception:  # never let the notice break a command
        pass
    return exit_code


def process_main() -> None:
    """Own process-only exit. Final shutdown callbacks must not spawn children."""
    from ..verify_process import _verify_process_signals

    with _verify_process_signals() as owner:
        exit_code = main()
        if owner.handlers:
            for stream in (sys.stdout, sys.stderr):
                if stream is not None:
                    stream.flush()
        owner.finish()
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    process_main()
