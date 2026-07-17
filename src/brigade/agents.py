"""Agent CLI adapters for one-shot model calls.

Each adapter reaches a model through the user's own authenticated CLI. Brigade
does not store provider keys or import provider SDKs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from . import proc
from .result_integrity import validate_final_output

_OLLAMA_PREFIX = "ollama:"
_CODEX_CLOUD_PREFIX = "codex-cloud:"
_GROK_RESULT_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["answer"]},
            "answer": {"type": "string", "minLength": 1},
        },
        "required": ["kind", "answer"],
        "additionalProperties": False,
    },
    separators=(",", ":"),
)


def _claude_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    return ["claude", "-p", prompt]


def _codex_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    if sandbox:
        return ["codex", "exec", "--sandbox", sandbox, prompt]
    if read_only:
        return ["codex", "exec", "--sandbox", "read-only", prompt]
    return ["codex", "exec", prompt]


def _opencode_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    return ["opencode", "run", prompt]


def _antigravity_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["agy", "--sandbox", "--print", prompt]
    effective_cwd = cwd if cwd is not None else Path.cwd().resolve()
    argv = ["agy", "--add-dir", str(effective_cwd)]
    argv.extend(["--dangerously-skip-permissions", "--print", prompt])
    return argv


def _pi_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["pi", "--tools", "read,grep,find,ls", "-p", prompt]
    return ["pi", "-p", prompt]


def _cursor_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    # Headless `cursor-agent -p` refuses to run in a workspace it has not
    # trusted yet, and the refusal exits 0, so an untrusted directory no-ops
    # while looking like success. `--trust` clears the gate for plan runs;
    # write runs also need `-f` so command approvals do not stall the worker.
    if read_only or sandbox == "read-only":
        return ["cursor-agent", "-p", "--mode", "plan", "--output-format", "text", "--trust", prompt]
    return ["cursor-agent", "-p", "--output-format", "text", "-f", prompt]


def _read_only_prompt(prompt: str) -> str:
    return f"Read-only planning run. Inspect and answer, but do not modify files or run mutating commands.\n\n{prompt}"


def _aider_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["aider", "--no-auto-commits", "--dry-run", "--message", prompt]
    return ["aider", "--yes", "--no-auto-commits", "--message", prompt]


def _goose_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["goose", "run", "--no-session", "-t", task]


def _continue_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    if read_only or sandbox == "read-only":
        return ["cn", "-p", prompt, "--readonly"]
    return ["cn", "-p", prompt]


def _copilot_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["copilot", "-p", task]


def _qwen_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    mode = "plan" if read_only or sandbox == "read-only" else "yolo"
    return ["qwen", "-p", prompt, "--approval-mode", mode]


def _kimi_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    argv = ["kimi", "--print", "-p", prompt, "--final-message-only"]
    if read_only or sandbox == "read-only":
        argv.insert(1, "--plan")
    else:
        argv.insert(1, "--yolo")
    return argv


def _adal_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["adal", "-q", task]


def _openhands_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["openhands", "--headless", "-t", task]


def _grok_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    # run_agent replaces plan mode with the native read-only filesystem sandbox
    # before dispatch. Keep this base shape stable for argv construction callers.
    if read_only or sandbox == "read-only":
        return ["grok", "-p", prompt, "--permission-mode", "plan"]
    return ["grok", "-p", prompt, "--always-approve"]


def _amp_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["amp", "-x", task]


def _crush_argv(prompt: str, read_only: bool, sandbox: str | None, cwd: Path | None) -> List[str]:
    task = _read_only_prompt(prompt) if read_only or sandbox == "read-only" else prompt
    return ["crush", "run", task]


_ADAPTERS: dict[str, Callable[[str, bool, str | None, Path | None], List[str]]] = {
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
    "grok": _grok_argv,
    "amp": _amp_argv,
    "crush": _crush_argv,
}


# How strongly each adapter enforces a read-only run:
#   hard - a native sandbox or tool allowlist the model cannot escape
#   soft - read-only is only a prompt instruction the model may ignore
#   none - read_only is not applied to this CLI at all
# Brigade is "loud about exceptions", so `brigade run --read-only` warns when an
# assigned harness is soft or none rather than implying a guarantee it cannot make.
READ_ONLY_ENFORCEMENT: dict[str, str] = {
    "codex": "hard",
    "antigravity": "hard",
    "pi": "hard",
    "cursor": "hard",
    "aider": "hard",
    "continue": "hard",
    "qwen": "hard",
    "kimi": "hard",
    "goose": "soft",
    "copilot": "soft",
    "adal": "soft",
    "openhands": "soft",
    "grok": "hard",
    "amp": "soft",
    "crush": "soft",
    "claude": "none",
    "opencode": "none",
}


def read_only_enforcement(cli_ref: str, *, sandbox: str | None = None, transport: str = "direct") -> str:
    """Return how strongly cli_ref enforces read-only: 'hard', 'soft', or 'none'."""
    if transport == "acpx":
        return "hard"
    if cli_ref.startswith(_OLLAMA_PREFIX):
        return "none"
    if cli_ref.startswith(_CODEX_CLOUD_PREFIX):
        # Cloud tasks run in an isolated remote environment; the local tree is
        # never modified (diffs are only applied via `codex cloud apply`).
        return "hard"
    if cli_ref == "codex" and sandbox in {"workspace-write", "danger-full-access"}:
        # Codex gives an explicit sandbox precedence over read_only. Other hard
        # adapters select their native read-only mode before consulting sandbox.
        return "soft"
    return READ_ONLY_ENFORCEMENT.get(cli_ref, "none")


def direct_cursor_read_only_limitation(model: str | None) -> str | None:
    """Describe a model-specific direct Cursor plan-mode output limitation."""
    normalized = (model or "").strip().lower()
    if normalized.startswith("composer-"):
        return (
            "direct Cursor plan mode does not return Composer findings as assistant text; "
            'use transport = "acpx" with the reviewed transport_version'
        )
    if normalized.startswith("grok-"):
        return (
            "direct Cursor plan mode returned no assistant text for this Grok model; "
            'retry with transport = "acpx" and the reviewed transport_version'
        )
    return None


def _parse_grok_final_output(stdout: str) -> tuple[str, str]:
    """Extract a schema-constrained final answer from Grok's JSON envelope."""
    base_error = "grok exited 0 without a structured final response"
    raw = stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw, base_error
    if not isinstance(payload, dict):
        return raw, base_error

    diagnostic_parts: list[str] = []
    stop_reason = payload.get("stopReason")
    if isinstance(stop_reason, str) and stop_reason:
        diagnostic_parts.append(f"stopReason={stop_reason}")
    structured_output_error = payload.get("structuredOutputError")
    structured_error_present = "structuredOutputError" in payload and structured_output_error is not None
    if structured_error_present:
        if isinstance(structured_output_error, str) and structured_output_error:
            diagnostic_parts.append(structured_output_error)
        else:
            diagnostic_parts.append("structuredOutputError was present")

    display_text = payload.get("text")
    fallback = display_text.strip() if isinstance(display_text, str) else raw
    if isinstance(display_text, str):
        try:
            nested = json.loads(display_text)
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict) and isinstance(nested.get("answer"), str):
            fallback = nested["answer"].strip()

    structured = payload.get("structuredOutput")
    answer = structured.get("answer") if isinstance(structured, dict) else None
    exact_shape = (
        isinstance(structured, dict)
        and set(structured) == {"kind", "answer"}
        and structured.get("kind") == "answer"
        and isinstance(answer, str)
        and bool(answer.strip())
    )
    if not exact_shape and not structured_error_present:
        diagnostic_parts.append("structured output did not match expected schema")
    successful_stop = stop_reason == "EndTurn"
    no_structured_error = not structured_error_present
    if not successful_stop or not no_structured_error or not exact_shape:
        detail = f"{base_error} ({'; '.join(diagnostic_parts)})" if diagnostic_parts else base_error
        return fallback, detail[:200]
    assert isinstance(answer, str)
    return answer.strip(), ""


@dataclass(frozen=True)
class AgentResult:
    text: str
    ok: bool
    detail: str = ""
    # app-server transport extras; None/"" on the exec path.
    thread_id: str | None = None
    status: str = ""
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout_log: str | None = None
    stderr_log: str | None = None
    duration_seconds: float | None = None
    transport: str = "cli"
    requested_model: str | None = None
    effective_model: str | None = None
    reasoning: str | None = None
    stop_reason: str | None = None
    protocol_version: int | None = None
    session_id: str | None = None
    request_id: str | None = None
    acpx_version: str | None = None
    safe_events: tuple[dict[str, object], ...] = ()
    failure_phase: str | None = None
    failure_kind: str | None = None


def is_known(cli_ref: str) -> bool:
    # a bare "codex-cloud:" with no environment id is not a valid seat
    has_cloud_env = cli_ref.startswith(_CODEX_CLOUD_PREFIX) and len(cli_ref) > len(_CODEX_CLOUD_PREFIX)
    return cli_ref in _ADAPTERS or cli_ref.startswith(_OLLAMA_PREFIX) or has_cloud_env


def command_for(cli_ref: str) -> str:
    if cli_ref.startswith(_OLLAMA_PREFIX):
        return "ollama"
    if cli_ref.startswith(_CODEX_CLOUD_PREFIX):
        return "codex"
    if cli_ref == "antigravity":
        return "agy"
    if cli_ref == "cursor":
        return "cursor-agent"
    if cli_ref == "continue":
        return "cn"
    return cli_ref


def _pin_after_cmd(argv: List[str], flag: str, model: str) -> List[str]:
    """Insert `flag model` right after the command (argv[0])."""
    return [argv[0], flag, model, *argv[1:]]


def _pin_after_subcmd(argv: List[str], flag: str, model: str) -> List[str]:
    """Insert `flag model` after a subcommand (argv[1], e.g. `run`)."""
    return [argv[0], argv[1], flag, model, *argv[2:]]


def _pin_before_prompt(argv: List[str], flag: str, model: str) -> List[str]:
    """Insert `flag model` before the final positional (the prompt)."""
    return [*argv[:-1], flag, model, argv[-1]]


# Adapters that accept a per-invocation model flag, with the flag and where it is
# placed in that adapter's argv. Each entry is verified against the CLI's --help.
# claude/codex outputs stay byte-identical to their pre-registry form.
_MODEL_PIN: dict[str, tuple[str, Callable[[List[str], str, str], List[str]]]] = {
    "claude": ("--model", _pin_after_cmd),  # claude --model X -p <prompt>
    "codex": ("-m", _pin_before_prompt),  # codex exec [--sandbox M] -m X <prompt>
    "grok": ("-m", _pin_after_cmd),  # grok -m X -p <prompt>
    "opencode": ("-m", _pin_after_subcmd),  # opencode run -m X <prompt>   (X = provider/model)
    "pi": ("--model", _pin_after_cmd),  # pi --model X [--tools ...] -p <prompt>
    "kimi": ("-m", _pin_after_cmd),  # kimi -m X [--plan] --print -p <prompt> --final-message-only
    "cursor": ("--model", _pin_after_cmd),  # cursor-agent --model X -p --output-format text -f <prompt>
    "antigravity": ("--model", _pin_after_cmd),  # agy --model X [--sandbox] --print <prompt>
}

_REASONING_ADAPTERS = frozenset({"codex", "opencode", "pi", "grok"})


def supports_model_pinning(cli_ref: str) -> bool:
    """True if cli_ref accepts a per-agent `model=` pin. Ollama refs name their
    own model and return False here."""
    return cli_ref in _MODEL_PIN


def supports_reasoning(cli_ref: str) -> bool:
    return cli_ref in _REASONING_ADAPTERS


def _with_reasoning(cli_ref: str, argv: List[str], reasoning: str) -> List[str]:
    if cli_ref == "codex":
        return [*argv[:-1], "-c", f'model_reasoning_effort="{reasoning}"', argv[-1]]
    if cli_ref == "opencode":
        return [*argv[:-1], "--variant", reasoning, argv[-1]]
    if cli_ref == "pi":
        return [argv[0], "--thinking", reasoning, *argv[1:]]
    if cli_ref == "grok":
        return [argv[0], "--reasoning-effort", reasoning, *argv[1:]]
    supported = ", ".join(sorted(_REASONING_ADAPTERS))
    raise ValueError(f"{cli_ref!r} does not support reasoning pins (supported: {supported})")


def _with_model(cli_ref: str, argv: List[str], model: str) -> List[str]:
    entry = _MODEL_PIN.get(cli_ref)
    if entry is None:
        supported = ", ".join(sorted(_MODEL_PIN))
        raise ValueError(f"{cli_ref!r} does not support model pinning (supported: {supported})")
    flag, placer = entry
    return placer(argv, flag, model)


def build_argv(
    cli_ref: str,
    prompt: str,
    read_only: bool = False,
    sandbox: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    cwd: Path | None = None,
) -> List[str]:
    if cli_ref.startswith(_OLLAMA_PREFIX):
        ollama_model = cli_ref[len(_OLLAMA_PREFIX) :]
        if not ollama_model:
            raise ValueError(f"ollama reference needs a model: {cli_ref!r}")
        if model is not None:
            raise ValueError(f"{cli_ref!r} already names a model; drop the separate model setting")
        if reasoning is not None:
            raise ValueError(f"{cli_ref!r} does not support reasoning pins")
        return ["ollama", "run", ollama_model, prompt]

    if cli_ref.startswith(_CODEX_CLOUD_PREFIX):
        raise ValueError("codex-cloud seats run a submit/poll flow; call run_agent, not build_argv")
    builder = _ADAPTERS.get(cli_ref)
    if builder is None:
        raise ValueError(
            f"unknown agent cli: {cli_ref!r} "
            "(known: claude, codex, opencode, antigravity, pi, cursor, aider, goose, continue, "
            "copilot, qwen, kimi, adal, openhands, grok, amp, crush, ollama:<model>, "
            "codex-cloud:<env-id>)"
        )
    argv = builder(prompt, read_only, sandbox, cwd)
    if model is not None:
        argv = _with_model(cli_ref, argv, model)
    if reasoning is not None:
        argv = _with_reasoning(cli_ref, argv, reasoning)
    return argv


def detect(cli_ref: str) -> bool:
    return proc.which(command_for(cli_ref)) is not None


def ollama_model_present(model: str) -> tuple[bool, str]:
    """Check whether an ollama model is already pulled locally.

    `ollama run` on a missing model silently auto-pulls it (tens of GB for
    large models), so callers must refuse to dispatch instead of letting the
    pull start. Returns (present, detail); detail explains a False result.
    """
    listing = proc.run(["ollama", "list"], timeout=15.0)
    if listing.code != 0:
        reason = listing.stderr.strip() or f"exit {listing.code}"
        return False, f"could not list local ollama models ({reason[:120]}); is the ollama server running?"
    names = {line.split()[0] for line in listing.stdout.splitlines()[1:] if line.strip()}
    wanted = {model} if ":" in model else {model, f"{model}:latest"}
    if names & wanted:
        return True, ""
    return False, (
        f"ollama model {model!r} is not pulled locally; brigade never auto-pulls. "
        f"Run `ollama pull {model}` yourself or point the seat at an installed model"
    )


def resolve_env_overrides(env: dict[str, str]) -> tuple[dict[str, str] | None, str]:
    """Resolve a seat env table into concrete child overrides.

    Keys ending in _REF name a parent environment variable holding the value;
    the override is injected under the key minus the suffix so secrets never
    live in the roster. Returns (overrides, error): error is non-empty when a
    referenced variable is not set, and never contains a secret value.
    """

    resolved: dict[str, str] = {}
    for key, value in env.items():
        if key.endswith("_REF"):
            target = key[: -len("_REF")]
            if not target:
                return None, f"env override {key}: resolved variable name is empty"
            referenced = os.environ.get(value)
            if referenced is None:
                return None, f"env override {key}: referenced variable {value} is not set"
            resolved[target] = referenced
        else:
            resolved[key] = value
    return resolved, ""


def run_agent(
    cli_ref: str,
    prompt: str,
    timeout: float = 600.0,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    env: dict[str, str] | None = None,
) -> AgentResult:
    if not detect(cli_ref):
        return AgentResult(text="", ok=False, detail=f"{command_for(cli_ref)} not installed")

    if cli_ref.startswith(_CODEX_CLOUD_PREFIX):
        env_id = cli_ref[len(_CODEX_CLOUD_PREFIX) :]
        if not env_id:
            return AgentResult(
                text="",
                ok=False,
                detail="codex-cloud reference needs an environment id: codex-cloud:<env-id>",
            )
        if model is not None:
            return AgentResult(
                text="",
                ok=False,
                detail="codex-cloud does not take a model pin; the cloud environment sets the model",
            )
        from . import codex_cloud

        return codex_cloud.run_cloud_task(prompt, env_id=env_id, timeout=timeout, cwd=cwd)

    if cli_ref.startswith(_OLLAMA_PREFIX):
        ollama_model = cli_ref[len(_OLLAMA_PREFIX) :]
        if ollama_model:
            present, missing_detail = ollama_model_present(ollama_model)
            if not present:
                return AgentResult(text="", ok=False, detail=missing_detail)

    child_env: dict[str, str] | None = None
    if env is not None:
        overrides, env_error = resolve_env_overrides(env)
        if overrides is None:
            return AgentResult(
                text="",
                ok=False,
                detail=env_error,
                failure_phase="dispatch",
                failure_kind="env-ref-missing",
                requested_model=model,
            )
        child_env = dict(os.environ)
        child_env.update(overrides)

    cursor_limitation = None
    if cli_ref == "cursor" and (read_only or sandbox == "read-only"):
        cursor_limitation = direct_cursor_read_only_limitation(model)
        if cursor_limitation is not None and (model or "").strip().lower().startswith("composer-"):
            return AgentResult(
                text="",
                ok=False,
                detail=cursor_limitation,
                requested_model=model,
            )

    argv = build_argv(
        cli_ref,
        prompt,
        read_only=read_only,
        sandbox=sandbox,
        model=model,
        reasoning=reasoning,
        cwd=cwd,
    )
    structured_grok = cli_ref == "grok" and (read_only or sandbox == "read-only")
    if structured_grok:
        if argv[-2:] != ["--permission-mode", "plan"]:
            return AgentResult(
                text="",
                ok=False,
                detail="internal error: grok read-only argv missing --permission-mode plan",
                requested_model=model,
                reasoning=reasoning,
            )
        argv[-2:] = ["--sandbox", "read-only", "--always-approve"]
        argv.extend(["--json-schema", _GROK_RESULT_SCHEMA])
    result = proc.run(
        argv,
        timeout=timeout,
        cwd=cwd,
        env=child_env,
    )
    text = result.stdout.strip()
    structured_error = ""
    if structured_grok:
        text, structured_error = _parse_grok_final_output(result.stdout)
    if result.code != 0:
        detail = result.stderr.strip() or f"exit {result.code}"
        return AgentResult(
            text=text,
            ok=False,
            detail=detail[:200],
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            timed_out=result.code == 124,
            requested_model=model,
            reasoning=reasoning,
        )
    if structured_error:
        return AgentResult(
            text=text,
            ok=False,
            detail=structured_error,
            failure_phase="output-validation",
            failure_kind="malformed-final-output",
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            requested_model=model,
            reasoning=reasoning,
        )
    if not text:
        detail = "empty output"
        if cursor_limitation is not None:
            detail = cursor_limitation
        elif cli_ref in {"cursor", "grok"}:
            detail = f"{cli_ref} exited 0 without output; check trust, permissions, and model availability"
        return AgentResult(
            text="",
            ok=False,
            detail=detail,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            requested_model=model,
            reasoning=reasoning,
        )
    output_failure = validate_final_output(text)
    if output_failure is not None:
        return AgentResult(
            text=text,
            ok=False,
            detail=output_failure.detail,
            failure_phase="output-validation",
            failure_kind=output_failure.kind,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.code,
            requested_model=model,
            reasoning=reasoning,
        )
    return AgentResult(
        text=text,
        ok=True,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.code,
        requested_model=model,
        reasoning=reasoning,
    )


def run_codex_appserver(
    server,
    prompt: str,
    *,
    timeout: float,
    cwd: Path | None,
    read_only: bool = False,
    sandbox: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    on_event=None,
) -> AgentResult:
    """Run one codex worker as a thread + turn on a shared app-server.

    Mirrors run_agent's contract: empty output is a failure, detail is capped.
    """
    from . import codex_appserver

    effective_sandbox = sandbox if sandbox is not None else ("read-only" if read_only else None)
    try:
        thread = server.start_thread(cwd=cwd, model=model, sandbox=effective_sandbox)
        turn_kwargs = {"timeout": timeout, "on_event": on_event}
        if reasoning is not None:
            turn_kwargs["effort"] = reasoning
        turn = thread.run_turn(prompt, **turn_kwargs)
    except codex_appserver.AppServerError as exc:
        return AgentResult(
            text="",
            ok=False,
            detail=str(exc)[:200],
            status="failed",
            transport="codex-app-server",
            requested_model=model,
            reasoning=reasoning,
        )
    text = turn.text.strip()
    if not turn.ok:
        return AgentResult(
            text=text,
            ok=False,
            detail=(turn.detail or f"turn {turn.status}")[:200],
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=model,
            reasoning=reasoning,
        )
    if not text:
        return AgentResult(
            text="",
            ok=False,
            detail="empty output",
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=model,
            reasoning=reasoning,
        )
    output_failure = validate_final_output(text)
    if output_failure is not None:
        return AgentResult(
            text=text,
            ok=False,
            detail=output_failure.detail,
            failure_phase="output-validation",
            failure_kind=output_failure.kind,
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=model,
            reasoning=reasoning,
        )
    return AgentResult(
        text=text,
        ok=True,
        thread_id=turn.thread_id,
        status=turn.status,
        transport="codex-app-server",
        requested_model=model,
        reasoning=reasoning,
    )
