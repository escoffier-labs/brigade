"""Static index of the top-level ``brigade`` commands.

``brigade.cli`` builds a command family's subparsers only when argv selects
that family. Every other family gets a placeholder subparser that carries the
same name, help line, and aliases, so ``brigade --help``, argparse's
"invalid choice" list, and shell lookups see the same top-level surface
without importing 60 command modules. Rows are in registration order, which is
also argparse's choice order.

``tests/test_cli_lazy_parser.py`` checks every row against the real
``register`` calls. When a family's top-level help or aliases change, update
the row here too.
"""

from __future__ import annotations

from typing import NamedTuple


class CommandEntry(NamedTuple):
    name: str
    # Module under ``brigade.cli`` that owns ``register(sub)`` and dispatch.
    module: str
    help: str
    aliases: tuple[str, ...] = ()
    # Gated behind ``brigade extras``: a disabled stub replaces the family.
    extras: bool = False


EXTRAS_STUB_HELP = "(extras, disabled) enable with `brigade extras on`"

COMMANDS: tuple[CommandEntry, ...] = (
    CommandEntry("hub", "hub_connector", "Manage hub connectors."),
    CommandEntry("init", "init", "Materialize a selection into a target directory."),
    CommandEntry("doctor", "doctor", "Verify a target workspace (target-scoped checks only unless --operator)."),
    CommandEntry("bug", "bug", "Prepare and submit a redacted bug report."),
    CommandEntry("version", "version", "Show Brigade version."),
    CommandEntry("status", "status", "Show which components are present and healthy."),
    CommandEntry("profiles", "profiles", "Inspect built-in component profiles."),
    CommandEntry("receipts", "receipts", "Verify local Brigade receipt digests."),
    CommandEntry("daily", "daily", "Run the personal daily operator loop."),
    CommandEntry("care", "care", "Install, audit, and remove operator-owned scheduled care entries."),
    CommandEntry("add", "add", "Install and wire a component's managed tools."),
    CommandEntry("setup", "setup", "Install pinned native Brigade components."),
    CommandEntry("update", "update", "Update Brigade through an immutable stable or beta channel."),
    CommandEntry(
        "components",
        "stations",
        "Inspect the built-in component catalog and discover station.json files.",
        ("stations",),
    ),
    CommandEntry("code", "code", "Run code graph commands through Brigade."),
    CommandEntry(
        "evidence", "evidence", "Inspect and query the evidence ledger: receipts, search, crawl and export plans."
    ),
    CommandEntry("search", "search", "Inspect and plan code graph + code-search station health."),
    CommandEntry("tokens", "tokens", "Inspect and plan Token Glace + usage-tracker station health."),
    CommandEntry("pantry", "pantry", "Inspect and plan agentpantry session-auth sync.", extras=True),
    CommandEntry("notifications", "notifications", "Inspect and plan operator notification wiring.", extras=True),
    CommandEntry("budgets", "budgets", "Inspect Brigade's canonical operator budgets.", extras=True),
    CommandEntry("untrusted", "untrusted", "Wrap and scan untrusted context.", extras=True),
    CommandEntry("skills", "skills", "Manage reviewed cross-harness skill packs."),
    CommandEntry("harness", "harness", "Install, inspect, or remove narrow harness onboarding profiles."),
    CommandEntry("operator", "operator", "Plan and initialize safe local operator config."),
    CommandEntry("runbook", "runbook", "Plan, run, resume, and close out explicit local runbooks.", extras=True),
    CommandEntry("dogfood", "dogfood", "Run a safe Brigade dogfood review with a configured agent CLI.", extras=True),
    CommandEntry("release", "release", "Inspect local release readiness.", extras=True),
    CommandEntry("roadmap", "roadmap", "Inspect roadmap completion state.", extras=True),
    CommandEntry("repos", "repos", "Inspect local repository fleet readiness.", extras=True),
    CommandEntry("handoff", "handoff", "Inspect memory handoff inbox health."),
    CommandEntry("memory", "memory", "Inspect local memory maintenance workflows."),
    CommandEntry("work", "work", "Inspect and manage a daily Brigade work session."),
    CommandEntry("friction", "friction", "Capture and scan workflow friction.", extras=True),
    CommandEntry("workflow", "workflow", "Mine local receipts for repeatable workflow candidates.", extras=True),
    CommandEntry("chat", "chat", "Inspect and import local chat surface exports.", extras=True),
    CommandEntry("context", "context", "Plan and build local context engineering packs.", extras=True),
    CommandEntry("projects", "projects", "Audit local side-project consolidation decisions.", extras=True),
    CommandEntry(
        "learn", "learn", "Plan local self-learning candidates without mutating memory or source.", extras=True
    ),
    CommandEntry("outcome", "outcome", "Inspect the verified outcome ledger."),
    CommandEntry("model", "model", "Inspect model performance across Brigade runs."),
    CommandEntry(
        "research", "research", "Run local-first deep research grounded in a trusted local corpus.", extras=True
    ),
    CommandEntry("center", "center", "Read local operator-center summaries.", extras=True),
    CommandEntry("run", "run", "Run a bounded cross-model orchestration task."),
    CommandEntry("route", "route", "Show the deterministic route a task composes: signals, stages, waves, holds."),
    CommandEntry("roster", "roster", "Create and check agent rosters."),
    CommandEntry("runs", "runs", "Inspect Brigade run artifacts."),
    CommandEntry("node", "node", "Show or initialize this machine's Brigade node identity."),
    CommandEntry("fleet", "fleet", "Fleet sync: report run events to a central hub over Tailscale."),
    CommandEntry("guard", "guard", "Run the embedded content guard."),
    CommandEntry("governance", "governance", "Export a privacy-bounded Brigade scope inventory."),
    CommandEntry("scrub", "scrub", "Run content-guard against a target."),
    CommandEntry("security", "security", "Scan agent workspace security posture."),
    CommandEntry("tools", "tools", "Inspect local portable tool and skill catalog."),
    CommandEntry("mcp", "mcp", "Sync one canonical MCP server catalog into each tool's native config."),
    CommandEntry("projection", "projection", "Recover multi-file projection operations."),
    CommandEntry("handoff-template", "handoff_template", "Print the handoff TEMPLATE.md."),
    CommandEntry("ingest", "ingest", "Process writer memory-handoff inboxes into canonical memory."),
    CommandEntry("reconfigure", "reconfigure", "Adjust an existing install to a new Selection."),
    CommandEntry("completions", "completions", "Print a shell completion script (bash, zsh, or fish)."),
    CommandEntry("extras", "extras", "Enable or disable the extras command surface."),
)

COMMANDS_BY_NAME: dict[str, CommandEntry] = {
    alias: entry for entry in COMMANDS for alias in (entry.name, *entry.aliases)
}
