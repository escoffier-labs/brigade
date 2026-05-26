"""Commands for creating and checking aboyeur rosters."""
from __future__ import annotations

import sys
from pathlib import Path

from . import agents
from . import doctor as doctor_mod
from . import roster as roster_mod

DEFAULT_ROSTER_REL = ".brigade/roster.toml"


def default_roster_text(*, ollama_model: str = "llama3.3", max_workers: int = 4) -> str:
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

[limits]
max_workers = {max_workers}
timeout_seconds = 600
allow_models = ["codex", "ollama:*"]
"""


def init(target: Path, *, force: bool = False, ollama_model: str = "llama3.3", max_workers: int = 4) -> int:
    if max_workers < 1:
        print("error: --max-workers must be a positive integer", file=sys.stderr)
        return 2
    if not ollama_model.strip():
        print("error: --ollama-model must be non-empty", file=sys.stderr)
        return 2

    target = target.expanduser()
    path = target / DEFAULT_ROSTER_REL
    if path.exists() and not force:
        print(f"error: roster already exists at {path}; pass --force to overwrite", file=sys.stderr)
        return 2

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_roster_text(ollama_model=ollama_model.strip(), max_workers=max_workers))
    print(f"wrote {path}")
    return 0


def doctor(target: Path, *, roster_path: Path | None = None) -> int:
    target = target.expanduser()
    path = roster_path.expanduser() if roster_path is not None else target / DEFAULT_ROSTER_REL

    checks: list[doctor_mod.CheckResult] = []
    try:
        loaded = roster_mod.load_roster(path)
    except FileNotFoundError:
        checks.append((doctor_mod.FAIL, "roster: file", f"missing at {path}; run `brigade roster init`"))
        return doctor_mod._report(checks)
    except ValueError as exc:
        checks.append((doctor_mod.FAIL, "roster: file", f"invalid {path}: {exc}"))
        return doctor_mod._report(checks)

    checks.append((doctor_mod.OK, "roster: file", str(path)))
    checks.append((doctor_mod.OK, "roster: orchestrator", loaded.orchestrator))
    checks.append((doctor_mod.OK, "roster: max_workers", str(loaded.max_workers)))
    checks.append((doctor_mod.OK, "roster: timeout_seconds", str(loaded.timeout_seconds)))
    if loaded.allow_models:
        checks.append((doctor_mod.OK, "roster: allow_models", ", ".join(loaded.allow_models)))
    else:
        checks.append((doctor_mod.WARN, "roster: allow_models", "not set; explicit model allow-list recommended"))

    for name, agent in loaded.agents.items():
        binary = agents.command_for(agent.cli)
        timeout = roster_mod.timeout_for(agent, loaded)
        if agents.detect(agent.cli):
            checks.append((doctor_mod.OK, f"agent: {name}", f"{agent.cli} via {binary}; timeout={timeout:g}s"))
        else:
            detail = f"{agent.cli} needs `{binary}` on PATH; timeout={timeout:g}s"
            if agent.cli == "claude":
                detail += "; Claude is optional, edit the roster if you are not using it"
            checks.append((doctor_mod.WARN, f"agent: {name}", detail))

    return doctor_mod._report(checks)
