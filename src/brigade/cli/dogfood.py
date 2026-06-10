"""brigade dogfood command group."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..dogfood_cmd import DEFAULT_TIMEOUT_SECONDS


def register(sub: argparse._SubParsersAction) -> None:
    # dogfood
    p_dogfood = sub.add_parser("dogfood", help="Run a safe Brigade dogfood review with a configured agent CLI.")
    p_dogfood.add_argument(
        "dogfood_args",
        nargs="*",
        help="Dogfood task, or `init` to write local dogfood defaults.",
    )
    p_dogfood.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_dogfood.add_argument("--output-dir", type=Path, default=None, help="Directory for run artifacts.")
    p_dogfood.add_argument(
        "--agent-cli",
        default=None,
        help="Agent CLI for dogfood runs: codex, claude, opencode, antigravity, pi, cursor, or ollama:<model>.",
    )
    p_dogfood.add_argument(
        "--handoff-inbox",
        type=Path,
        default=None,
        help="Memory Handoff inbox. Defaults to .codex/memory-handoffs under the effective target.",
    )
    p_dogfood.add_argument("--force", action="store_true", help="Overwrite an existing dogfood config during init.")
    p_dogfood.add_argument("--no-handoff", action="store_true", help="Do not write a Memory Handoff.")
    p_dogfood.add_argument("--no-inspect", action="store_true", help="Do not print the artifact summary afterward.")
    p_dogfood.add_argument(
        "--native-read-only-sandbox",
        action="store_true",
        help="Use Codex's native read-only sandbox instead of the dogfood trusted-workspace default.",
    )
    p_dogfood.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-agent timeout.")
    p_dogfood.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import dogfood_cmd

    dogfood_args = list(args.dogfood_args)
    if dogfood_args and dogfood_args[0] == "init":
        if len(dogfood_args) > 1:
            print("error: dogfood init does not accept a task argument", file=sys.stderr)
            return 2
        return dogfood_cmd.init(
            target=args.target,
            artifacts_dir=args.output_dir,
            handoff_inbox=args.handoff_inbox,
            agent_cli=args.agent_cli or dogfood_cmd.DEFAULT_AGENT_CLI,
            force=args.force,
            handoff=not args.no_handoff,
            inspect=not args.no_inspect,
            native_read_only_sandbox=args.native_read_only_sandbox,
            timeout_seconds=args.timeout_seconds,
        )
    if dogfood_args and dogfood_args[0] == "status":
        if len(dogfood_args) > 1:
            print("error: dogfood status does not accept a task argument", file=sys.stderr)
            return 2
        return dogfood_cmd.status(target=args.target)
    if dogfood_args and dogfood_args[0] == "latest":
        if len(dogfood_args) > 1:
            print("error: dogfood latest does not accept a task argument", file=sys.stderr)
            return 2
        return dogfood_cmd.latest(target=args.target)
    if dogfood_args and dogfood_args[0] == "next":
        if len(dogfood_args) > 1:
            print("error: dogfood next does not accept a task argument", file=sys.stderr)
            return 2
        return dogfood_cmd.next_step(target=args.target)
    task = " ".join(dogfood_args) if dogfood_args else None
    return dogfood_cmd.run(
        task,
        target=args.target,
        output_dir=args.output_dir,
        handoff=not args.no_handoff,
        handoff_inbox=args.handoff_inbox,
        agent_cli=args.agent_cli,
        inspect=not args.no_inspect,
        native_read_only_sandbox=args.native_read_only_sandbox,
        timeout_seconds=args.timeout_seconds,
    )
