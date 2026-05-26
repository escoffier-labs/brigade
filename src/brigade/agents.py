"""Agent CLI adapters for one-shot model calls.

Each adapter reaches a model through the user's own authenticated CLI. Brigade
does not store provider keys or import provider SDKs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from . import proc

_OLLAMA_PREFIX = "ollama:"


def _claude_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    return ["claude", "-p", prompt]


def _codex_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if sandbox:
        return ["codex", "exec", "--sandbox", sandbox, prompt]
    if read_only:
        return ["codex", "exec", "--sandbox", "read-only", prompt]
    return ["codex", "exec", prompt]


_ADAPTERS: dict[str, Callable[[str, bool, str | None], List[str]]] = {
    "claude": _claude_argv,
    "codex": _codex_argv,
}


@dataclass(frozen=True)
class AgentResult:
    text: str
    ok: bool
    detail: str = ""


def is_known(cli_ref: str) -> bool:
    return cli_ref in _ADAPTERS or cli_ref.startswith(_OLLAMA_PREFIX)


def command_for(cli_ref: str) -> str:
    if cli_ref.startswith(_OLLAMA_PREFIX):
        return "ollama"
    return cli_ref


def build_argv(
    cli_ref: str,
    prompt: str,
    read_only: bool = False,
    sandbox: str | None = None,
) -> List[str]:
    if cli_ref.startswith(_OLLAMA_PREFIX):
        model = cli_ref[len(_OLLAMA_PREFIX) :]
        if not model:
            raise ValueError(f"ollama reference needs a model: {cli_ref!r}")
        return ["ollama", "run", model, prompt]

    builder = _ADAPTERS.get(cli_ref)
    if builder is None:
        raise ValueError(f"unknown agent cli: {cli_ref!r} (known: claude, codex, ollama:<model>)")
    return builder(prompt, read_only, sandbox)


def detect(cli_ref: str) -> bool:
    return proc.which(command_for(cli_ref)) is not None


def run_agent(
    cli_ref: str,
    prompt: str,
    timeout: float = 600.0,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox: str | None = None,
) -> AgentResult:
    if not detect(cli_ref):
        return AgentResult(text="", ok=False, detail=f"{command_for(cli_ref)} not installed")

    result = proc.run(build_argv(cli_ref, prompt, read_only=read_only, sandbox=sandbox), timeout=timeout, cwd=cwd)
    text = result.stdout.strip()
    if result.code != 0:
        detail = result.stderr.strip() or f"exit {result.code}"
        return AgentResult(text=text, ok=False, detail=detail[:200])
    if not text:
        return AgentResult(text="", ok=False, detail="empty output")
    return AgentResult(text=text, ok=True)
