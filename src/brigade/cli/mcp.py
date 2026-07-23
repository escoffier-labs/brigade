"""brigade mcp command group."""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path


def _target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to operate on.")


def _write_mode(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview changes without writing (default).")
    mode.add_argument("--write", action="store_true", help="Apply the planned changes.")


def register(sub: argparse._SubParsersAction) -> None:
    p_mcp = sub.add_parser("mcp", help="Sync one canonical MCP server catalog into each tool's native config.")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", metavar="<mcp-command>")
    mcp_sub.required = True

    p_init = mcp_sub.add_parser("init", help="Scaffold .brigade/mcp.json and the ownership sidecar.")
    _target(p_init)
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing canonical file.")
    p_init.add_argument("--no-gitignore", action="store_true", help="Do not touch .gitignore.")
    p_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_add = mcp_sub.add_parser("add", help="Add or update a server in the canonical catalog.")
    _target(p_add)
    p_add.add_argument("--name", required=True, help="Server name (the key under mcpServers/servers/mcp).")
    p_add.add_argument("--command", default=None, help="Executable for a stdio server.")
    p_add.add_argument(
        "--args", default="", help='Command arguments as one string, e.g. "-y @modelcontextprotocol/server-github".'
    )
    p_add.add_argument(
        "--env", action="append", default=[], metavar="KEY=ref:VAR", help="Env entry: KEY=ref:VAR or KEY=value."
    )
    p_add.add_argument("--url", default=None, help="URL for a remote (http/sse) server.")
    p_add.add_argument("--transport", default="stdio", choices=["stdio", "http", "sse"], help="Transport.")
    p_add.add_argument("--timeout", type=int, default=None, help="Timeout in seconds.")
    p_add.add_argument("--targets", nargs="*", default=None, help="Restrict to these harnesses (default: all).")
    p_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_list = mcp_sub.add_parser("list", help="List servers in the canonical catalog.")
    _target(p_list)
    p_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_plan = mcp_sub.add_parser("plan", help="Show what a sync would do (read-only).")
    _target(p_plan)
    p_plan.add_argument("--name", default=None, help="Plan a single server.")
    p_plan.add_argument("--harness", default=None, help="Plan a single target harness.")
    p_plan.add_argument("--user-scope", action="store_true", help="Include user-scoped targets (e.g. antigravity).")
    p_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_sync = mcp_sub.add_parser("sync", help="Merge the catalog into each tool's config (dry-run unless --write).")
    _target(p_sync)
    p_sync.add_argument("--name", default=None, help="Sync a single server.")
    p_sync.add_argument("--harness", default=None, help="Sync a single target harness.")
    p_sync.add_argument("--write", action="store_true", help="Actually write files (otherwise dry-run).")
    p_sync.add_argument("--force", action="store_true", help="Overwrite servers edited outside Brigade.")
    p_sync.add_argument("--prune", action="store_true", help="Remove pristine orphans dropped from the catalog.")
    p_sync.add_argument("--adopt", action="store_true", help="Take ownership of same-named foreign servers.")
    p_sync.add_argument("--user-scope", action="store_true", help="Include user-scoped targets (e.g. antigravity).")
    p_sync.add_argument(
        "--allow-global-stdio",
        action="store_true",
        help="Acknowledge writing stdio MCP servers into a user-wide client config (one child process per stdio server per active client session).",
    )
    p_sync.add_argument(
        "--verify", action="store_true", help="After --write, verify runtime health for selected servers."
    )
    p_sync.add_argument(
        "--verify-timeout",
        type=float,
        default=None,
        help="Per-server runtime verification timeout in seconds (with --write --verify).",
    )
    p_sync.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_verify = mcp_sub.add_parser("verify", help="Probe MCP runtime health for selected servers.")
    _target(p_verify)
    p_verify.add_argument("--name", default=None, help="Verify a single server.")
    p_verify.add_argument("--harness", default=None, help="Verify servers for a single target harness.")
    p_verify.add_argument("--user-scope", action="store_true", help="Include user-scoped targets (e.g. antigravity).")
    p_verify.add_argument("--timeout", type=float, default=None, help="Per-server verification timeout in seconds.")
    p_verify.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_doctor = mcp_sub.add_parser("doctor", help="Validate the canonical catalog and report gaps.")
    _target(p_doctor)
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_import = mcp_sub.add_parser("import", help="Read an existing tool's MCP config into the canonical catalog.")
    _target(p_import)
    p_import.add_argument("harness", help="Source harness to import from (e.g. claude, cursor, codex).")
    p_import.add_argument("--merge", action="store_true", help="Write discovered servers into .brigade/mcp.json.")
    p_import.add_argument("--user-scope", action="store_true", help="Allow importing a user-scoped target.")
    p_import.add_argument(
        "--keep-secrets",
        action="store_true",
        help="Keep literal env secrets verbatim instead of demoting to ${VAR} refs (for syncing working configs).",
    )
    p_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_pi_bridge = mcp_sub.add_parser(
        "pi-bridge",
        help="Bridge the canonical MCP catalog into Pi tools (discover, call, install, uninstall).",
    )
    pi_bridge_sub = p_pi_bridge.add_subparsers(dest="pi_bridge_command", metavar="<pi-bridge-command>")
    pi_bridge_sub.required = True

    p_pi_discover = pi_bridge_sub.add_parser("discover", help="List namespaced MCP tools from the canonical catalog.")
    _target(p_pi_discover)
    p_pi_discover.add_argument("--timeout", type=float, default=None, help="Per-server timeout in seconds.")
    p_pi_discover.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_pi_call = pi_bridge_sub.add_parser("call", help="Call one namespaced MCP tool.")
    _target(p_pi_call)
    p_pi_call.add_argument("--tool", required=True, help="Qualified tool name (server__tool).")
    p_pi_call.add_argument("--args-json", default="{}", help="JSON object of tool arguments.")
    p_pi_call.add_argument("--timeout", type=float, default=None, help="Per-call timeout in seconds.")
    p_pi_call.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_pi_install = pi_bridge_sub.add_parser(
        "install", help="Install the generated Pi extension and catalog projection."
    )
    _target(p_pi_install)
    _write_mode(p_pi_install)
    p_pi_install.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_pi_uninstall = pi_bridge_sub.add_parser("uninstall", help="Remove Brigade-owned Pi MCP bridge artifacts.")
    _write_mode(p_pi_uninstall)
    p_pi_uninstall.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_mcp.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import mcp_cmd, pi_mcp_cmd

    if args.mcp_command == "pi-bridge":
        if args.pi_bridge_command == "discover":
            return pi_mcp_cmd.discover(target=args.target, timeout=args.timeout, json_output=args.json)
        if args.pi_bridge_command == "call":
            return pi_mcp_cmd.call(
                target=args.target,
                tool=args.tool,
                args_json=args.args_json,
                timeout=args.timeout,
                json_output=args.json,
            )
        if args.pi_bridge_command == "install":
            return pi_mcp_cmd.install(target=args.target, write=args.write, json_output=args.json)
        if args.pi_bridge_command == "uninstall":
            return pi_mcp_cmd.uninstall(write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown pi-bridge command: {args.pi_bridge_command}")
        return 2

    if args.mcp_command == "init":
        return mcp_cmd.init(
            target=args.target, force=args.force, update_gitignore=not args.no_gitignore, json_output=args.json
        )
    if args.mcp_command == "add":
        return mcp_cmd.add(
            target=args.target,
            name=args.name,
            command=args.command,
            args=shlex.split(args.args),
            env=args.env,
            url=args.url,
            transport=args.transport,
            timeout=args.timeout,
            targets=args.targets,
            json_output=args.json,
        )
    if args.mcp_command == "list":
        return mcp_cmd.list_servers(target=args.target, json_output=args.json)
    if args.mcp_command == "plan":
        return mcp_cmd.plan(
            target=args.target,
            name=args.name,
            harness=args.harness,
            user_scope=args.user_scope,
            json_output=args.json,
        )
    if args.mcp_command == "sync":
        return mcp_cmd.sync(
            target=args.target,
            name=args.name,
            harness=args.harness,
            write=args.write,
            force=args.force,
            prune=args.prune,
            adopt=args.adopt,
            user_scope=args.user_scope,
            allow_global_stdio=args.allow_global_stdio,
            verify_runtime=args.verify,
            verify_timeout=args.verify_timeout,
            json_output=args.json,
        )
    if args.mcp_command == "verify":
        return mcp_cmd.verify(
            target=args.target,
            name=args.name,
            harness=args.harness,
            user_scope=args.user_scope,
            timeout=args.timeout,
            json_output=args.json,
        )
    if args.mcp_command == "doctor":
        return mcp_cmd.doctor(target=args.target, json_output=args.json)
    if args.mcp_command == "import":
        return mcp_cmd.import_servers(
            target=args.target,
            harness=args.harness,
            merge=args.merge,
            user_scope=args.user_scope,
            keep_secrets=args.keep_secrets,
            json_output=args.json,
        )
    args._brigade_parser.error(f"unknown mcp command: {args.mcp_command}")
    return 2
