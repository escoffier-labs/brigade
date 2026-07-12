"""Commands for creating and checking aboyeur rosters."""

from __future__ import annotations

import sys
from pathlib import Path

from . import agents
from . import doctor as doctor_mod
from . import roster as roster_mod

DEFAULT_ROSTER_REL = ".brigade/roster.toml"


def default_roster_text(
    *, ollama_model: str = "llama3.3", max_workers: int = 4, review_model: str | None = None
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


def init(
    target: Path,
    *,
    force: bool = False,
    ollama_model: str = "llama3.3",
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
        if agents.detect(agent.cli):
            checks.append((doctor_mod.OK, f"agent: {name}", f"{agent.cli} via {binary}; timeout={timeout:g}s"))
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
            else:
                checks.append(
                    (
                        doctor_mod.FAIL,
                        f"agent: {name} model",
                        f"{agent.cli} does not support model pinning; drop model= or switch cli",
                    )
                )

    return doctor_mod._report(checks)
