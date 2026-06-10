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


def _opencode_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    return ["opencode", "run", prompt]


def _antigravity_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["agy", "--sandbox", "--print", prompt]
    return ["agy", "--print", prompt]


def _pi_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["pi", "--tools", "read,grep,find,ls", "-p", prompt]
    return ["pi", "-p", prompt]


def _cursor_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["cursor-agent", "-p", "--mode", "plan", "--output-format", "text", prompt]
    return ["cursor-agent", "-p", "--output-format", "text", prompt]


def _read_only_prompt(prompt: str) -> str:
    return f"Read-only planning run. Inspect and answer, but do not modify files or run mutating commands.\n\n{prompt}"


def _aider_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["aider", "--no-auto-commits", "--dry-run", "--message", prompt]
    return ["aider", "--yes", "--no-auto-commits", "--message", prompt]


def _goose_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["goose", "run", "--no-session", "-t", task]


def _continue_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["cn", "-p", prompt, "--readonly"]
    return ["cn", "-p", prompt]


def _copilot_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["copilot", "-p", task]


def _qwen_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    mode = "plan" if read_only or sandbox == "read-only" else "yolo"
    return ["qwen", "-p", prompt, "--approval-mode", mode]


def _kimi_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    argv = ["kimi", "--print", "-p", prompt, "--final-message-only"]
    if read_only or sandbox == "read-only":
        argv.insert(1, "--plan")
    return argv


def _adal_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["adal", "-q", task]


def _openhands_argv(prompt: str, read_only: bool, sandbox: str | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["openhands", "--headless", "-t", task]


_ADAPTERS: dict[str, Callable[[str, bool, str | None], List[str]]] = {
    "claude": _claude_argv,
    "codex": _codex_argv,
    "opencode": _opencode_argv,
    "antigravity": _antigravity_argv,
    "pi": _pi_argv,
    "cursor": _cursor_argv,
    "aider": _aider_argv,
    "goose": _goose_argv,
    "continue": _continue_argv,
    "copilot": _copilot_argv,
    "qwen": _qwen_argv,
    "kimi": _kimi_argv,
    "adal": _adal_argv,
    "openhands": _openhands_argv,
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
    if cli_ref == "antigravity":
        return "agy"
    if cli_ref == "cursor":
        return "cursor-agent"
    if cli_ref == "continue":
        return "cn"
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
        raise ValueError(
            f"unknown agent cli: {cli_ref!r} "
            "(known: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, "
            "copilot, qwen, kimi, adal, openhands, ollama:<model>)"
        )
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
