"""Commands for creating and checking aboyeur rosters."""

from __future__ import annotations

import sys
from pathlib import Path

from . import agents
from . import doctor as doctor_mod
from . import model_inventory
from . import roster as roster_mod
from . import templates

DEFAULT_ROSTER_REL = ".brigade/roster.toml"

# Small on purpose: a starter roster must never name a model whose absence
# triggers a multi-GB `ollama pull` (a 43GB default once filled a root disk).
DEFAULT_OLLAMA_MODEL = "llama3.2:3b"


def default_roster_text(
    *, ollama_model: str = DEFAULT_OLLAMA_MODEL, max_workers: int = 4, review_model: str | None = None
) -> str:
    return f"""# Brigade aboyeur roster.
# Edit agent roles and CLI refs to match the tools installed on this machine.

orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "Plan the work, choose useful workers, and synthesize the final answer."

[agents.coder]
cli = "codex"
role = "Make precise code changes and report what changed."

# Brigade never auto-pulls ollama models: dispatch fails unless the model is
# already local. Run `ollama pull {ollama_model}` once before using this seat.
[agents.local_researcher]
cli = "ollama:{ollama_model}"
role = "Research locally and summarize useful findings."
{_reviewer_seat(review_model)}
[limits]
max_workers = {max_workers}
timeout_seconds = 600
allow_models = ["codex", "ollama:*"]

# Cross-model example: pin a model per agent with `model = ...`
# (supported: claude, codex, grok, opencode, pi, kimi, cursor, antigravity).
# Pin reasoning with `reasoning = "high"` for codex, grok, opencode, or pi.
# Cursor workers may opt into reviewed ACP transport with
# `transport = "acpx"` and `transport_version = "0.12.0"`.
# A `codex-cloud:<env-id>` seat submits the task to Codex Cloud, polls it to a
# terminal state, and returns the summary plus unified diff (never auto-applied;
# land it with `codex cloud apply <task-id>`). Allow it with "codex-cloud:*".
# Fable 5 plans and synthesizes, GPT 5.5 executes, the handoff records the run.
# Use a model id your CLI account supports (ChatGPT-account codex takes "gpt-5.5").
#
# orchestrator = "architect"
#
# [agents.architect]
# cli = "claude"
# model = "claude-fable-5"
# role = "Plan the work, choose useful workers, and synthesize the final answer."
#
# [agents.builder]
# cli = "codex"
# model = "gpt-5.5"
# reasoning = "xhigh"
# role = "Make precise code changes and report what changed."
#
# [agents.composer]
# cli = "grok"
# model = "grok-composer-2.5-fast"
# role = "Draft fast first-pass changes for the architect to review."
"""


def _reviewer_seat(review_model: str | None) -> str:
    if not review_model:
        return ""
    # A reviewer on a different model than the coder makes review
    # independence structural instead of stylistic (issue #125).
    return f"""
[agents.reviewer]
cli = "codex"
model = "{review_model}"
role = "Inspect code and reports, verify claims against the actual diff, and flag problems."
"""


def preset_roster_paths() -> tuple[Path, ...]:
    rosters_dir = templates.template_root() / "rosters"
    return tuple(sorted(rosters_dir.glob("*.toml")))


def init(
    target: Path,
    *,
    force: bool = False,
    ollama_model: str = DEFAULT_OLLAMA_MODEL,
    max_workers: int = 4,
    review_model: str | None = None,
) -> int:
    if max_workers < 1:
        print("error: --max-workers must be a positive integer", file=sys.stderr)
        return 2
    if not ollama_model.strip():
        print("error: --ollama-model must be non-empty", file=sys.stderr)
        return 2
    if review_model is not None and not review_model.strip():
        print("error: --review-model must be non-empty when provided", file=sys.stderr)
        return 2

    target = target.expanduser()
    path = target / DEFAULT_ROSTER_REL
    if path.exists() and not force:
        print(f"error: roster already exists at {path}; pass --force to overwrite", file=sys.stderr)
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        default_roster_text(
            ollama_model=ollama_model.strip(),
            max_workers=max_workers,
            review_model=review_model.strip() if review_model else None,
        )
    )
    print(f"wrote {path}")
    return 0


def doctor(target: Path, *, roster_path: Path | None = None) -> int:
    target = target.expanduser()

    checks: list[doctor_mod.CheckResult] = []
    try:
        path = roster_mod.resolve_roster_path(target, roster_path)
        loaded = roster_mod.load_roster(path)
    except FileNotFoundError as exc:
        checks.append((doctor_mod.FAIL, "roster: file", f"{exc}; run `brigade roster init`"))
        return doctor_mod._report(checks)
    except ValueError as exc:
        checks.append((doctor_mod.FAIL, "roster: file", f"invalid {path}: {exc}"))
        return doctor_mod._report(checks)

    checks.append((doctor_mod.OK, "roster: file", str(path)))
    checks.append((doctor_mod.OK, "roster: orchestrator", loaded.orchestrator))
    checks.append((doctor_mod.OK, "roster: max_workers", str(loaded.max_workers)))
    checks.append((doctor_mod.OK, "roster: timeout_seconds", str(loaded.timeout_seconds)))
    if loaded.sandbox is not None:
        checks.append((doctor_mod.INFO, "roster: sandbox", loaded.sandbox))
    if loaded.allow_models:
        checks.append((doctor_mod.OK, "roster: allow_models", ", ".join(loaded.allow_models)))
    else:
        checks.append((doctor_mod.WARN, "roster: allow_models", "not set; explicit model allow-list recommended"))

    inventory_inspector = model_inventory.ModelInventoryInspector()
    for name, agent in loaded.agents.items():
        timeout = roster_mod.timeout_for(agent, loaded)
        if agent.cli is None:
            # Endpoint-mode agent: model is the HTTP model, not a CLI model pin.
            checks.append(
                (
                    doctor_mod.OK,
                    f"agent: {name}",
                    f"endpoint {agent.endpoint} model={agent.model}; timeout={timeout:g}s",
                )
            )
            continue
        binary = agents.command_for(agent.cli)
        detected = agents.detect(agent.cli)
        if detected:
            checks.append((doctor_mod.OK, f"agent: {name}", f"{agent.cli} via {binary}; timeout={timeout:g}s"))
            if agent.cli.startswith("ollama:"):
                ollama_model = agent.cli[len("ollama:") :]
                inventory = inventory_inspector.inspect(agent.cli, ollama_model)
                assert inventory is not None
                checks.append(_model_inventory_check(name, inventory))
        else:
            detail = f"{agent.cli} needs `{binary}` on PATH; timeout={timeout:g}s"
            if agent.cli == "claude":
                detail += "; Claude is optional, edit the roster if you are not using it"
            checks.append((doctor_mod.WARN, f"agent: {name}", detail))
        if agent.model is not None:
            if agent.cli.startswith("ollama:"):
                checks.append(
                    (doctor_mod.FAIL, f"agent: {name} model", "ollama names its model in the cli ref; drop model=")
                )
            elif agents.supports_model_pinning(agent.cli):
                checks.append((doctor_mod.OK, f"agent: {name} model", f"{agent.model} via {agent.cli}"))
                if detected and agent.transport == "direct":
                    inventory = inventory_inspector.inspect(agent.cli, agent.model)
                    if inventory is not None:
                        checks.append(_model_inventory_check(name, inventory))
            else:
                checks.append(
                    (
                        doctor_mod.FAIL,
                        f"agent: {name} model",
                        f"{agent.cli} does not support model pinning; drop model= or switch cli",
                    )
                )
            # Endpoint-mode agents are intentionally exempt: their model is a
            # remote HTTP model name, not a local CLI route that needs Cloudflare
            # env vars (the cli=None branch above already continued past this).
            if agents.is_cloudflare_ai_gateway_route(agent.model):
                missing = agents.missing_cloudflare_ai_gateway_env_vars()
                label = f"agent: {name} cloudflare gateway"
                if missing:
                    checks.append(
                        (
                            doctor_mod.FAIL,
                            label,
                            f"requires env vars: {', '.join(missing)}; set them before running",
                        )
                    )
                else:
                    checks.append((doctor_mod.OK, label, "required env vars are set"))
        if agent.reasoning is not None:
            if agents.supports_reasoning(agent.cli):
                checks.append((doctor_mod.OK, f"agent: {name} reasoning", f"{agent.reasoning} via {agent.cli}"))
            else:
                checks.append(
                    (
                        doctor_mod.FAIL,
                        f"agent: {name} reasoning",
                        f"{agent.cli} does not support reasoning pins; drop reasoning= or switch cli",
                    )
                )
        if agent.transport == "acpx":
            from . import acpx_adapter

            if agents.proc.which("acpx") is None:
                checks.append((doctor_mod.WARN, f"agent: {name} acpx", "acpx is not installed"))
            else:
                installed, detail = acpx_adapter.installed_version()
                if installed == agent.transport_version:
                    checks.append((doctor_mod.OK, f"agent: {name} acpx", f"version {installed}"))
                    auth = acpx_adapter.cursor_auth_status()
                    auth_status = doctor_mod.OK if auth.state == "authenticated" else doctor_mod.FAIL
                    checks.append((auth_status, f"agent: {name} cursor auth", auth.detail))
                else:
                    checks.append(
                        (
                            doctor_mod.FAIL,
                            f"agent: {name} acpx",
                            f"requires {agent.transport_version}; found {installed or detail}",
                        )
                    )

    return doctor_mod._report(checks)


def _model_inventory_check(agent_name: str, result: model_inventory.ModelInventoryResult) -> doctor_mod.CheckResult:
    status = doctor_mod.OK if result.state == "exact" else doctor_mod.WARN
    return (status, f"agent: {agent_name} model inventory", f"{result.state}: {result.detail}")
