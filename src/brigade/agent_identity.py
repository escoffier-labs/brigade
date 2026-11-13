"""Best-effort agent identity shared by observed checks and outcomes."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import localio
from .selection import KNOWN_HARNESSES

UNKNOWN = "unknown"
MODEL_SOURCES = ("hook-session-start", "hook-model-switch", "hook-event", "env", UNKNOWN)


@dataclass(frozen=True)
class AgentIdentity:
    harness: str
    harness_source: str
    model: str
    model_source: str
    agent_type: str = UNKNOWN

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


# Ordered (env var -> harness name) auto-detection. An explicit
# BRIGADE_CONTEXT_HARNESS override wins; otherwise a known signal implies a name.
_HARNESS_SIGNALS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude-code"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("CODEX_SANDBOX", "codex"),
    ("CODEX_SANDBOX_NETWORK_DISABLED", "codex"),
)


def detect_harness(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (harness, source). Best-effort: env override, then known signals."""
    environ = os.environ if environ is None else environ
    override = environ.get("BRIGADE_CONTEXT_HARNESS", "").strip()
    if override:
        return localio.slugify(override, fallback="harness"), "env"
    for var, name in _HARNESS_SIGNALS:
        if environ.get(var):
            return name, "auto"
    generic = environ.get("AI_AGENT", "").strip()
    if generic:
        return localio.slugify(generic, fallback="harness"), "auto"
    return "unknown", "unknown"


def env_model(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    environ = os.environ if environ is None else environ
    model = environ.get("BRIGADE_CONTEXT_MODEL", "").strip()
    return (model, "env") if model else (UNKNOWN, UNKNOWN)


def session_fingerprint(harness: str, session_id: str) -> str:
    key = "claude_session_id" if harness == "claude-code" else f"{harness}_session_id"
    return localio.stable_hash({key: session_id})


# Hook defaults plus memory_verify_seat.CLAUDE_BUILTIN_AGENTS. Custom names stay unknown.
AGENT_TYPES = frozenset((UNKNOWN, "main", "subagent", "claude", "Explore", "Plan", "general-purpose"))
# Families listed by the harness/model catalog, without provider paths or free text.
MODEL_FAMILIES = ("claude-", "gpt-", "gemini-", "kimi-", "glm-", "composer-", "muse-")


def map_identity(harness: Any, model: Any, agent_type: Any) -> dict[str, str]:
    """Map untrusted identity fields to closed names or a shaped model id."""
    # Drop a harness variant suffix such as a context-window tag in square brackets.
    model = re.sub(r"\[[a-z0-9]{1,8}\]$", "", model.lower()) if isinstance(model, str) else ""
    return {
        "harness": harness if isinstance(harness, str) and harness in (*KNOWN_HARNESSES, "claude-code") else UNKNOWN,
        "model": model if re.fullmatch(r"[a-z0-9.-]{3,48}", model) and model.startswith(MODEL_FAMILIES) else UNKNOWN,
        "agent_type": agent_type if isinstance(agent_type, str) and agent_type in AGENT_TYPES else UNKNOWN,
    }


def claude_session_model(target: Path, fingerprint: str) -> tuple[str, str] | None:
    from .claude_hooks import runtime

    try:
        for state in runtime.iter_session_states(target.resolve(), limit=256):
            if state.get("session_fingerprint") == fingerprint:
                model = map_identity("claude-code", state.get("model"), "unknown")["model"]
                source = state.get("model_source")
                if isinstance(state.get("model"), str) and source in MODEL_SOURCES:
                    return model, source
    except Exception:
        pass
    return None


def for_verify_run(target: Path, environ: Mapping[str, str] | None = None) -> AgentIdentity:
    environ = os.environ if environ is None else environ
    harness, harness_source = detect_harness(environ)
    model, source = env_model(environ)
    if model == UNKNOWN:
        for key in ("BRIGADE_AGENT_SESSION", "BRIGADE_CLAUDE_SESSION"):
            fingerprint = environ.get(key, "")
            if re.fullmatch(r"[0-9a-f]{16}", fingerprint):
                cached = claude_session_model(target, fingerprint)
                if cached is not None:
                    model, source = cached
                    break
    return AgentIdentity(**map_identity(harness, model, UNKNOWN), harness_source=harness_source, model_source=source)


def stamp_verify_identity(receipt: dict[str, Any], target: Path) -> None:
    """Stamp identity before signing; preserve strict harness-session matching."""
    receipt["agent_identity"] = for_verify_run(target).as_dict()
    claude_session = os.environ.get("BRIGADE_CLAUDE_SESSION")
    if claude_session and re.fullmatch(r"[0-9a-f]{16}", claude_session):
        receipt["harness_session"] = {"harness": "claude", "fingerprint": claude_session}
