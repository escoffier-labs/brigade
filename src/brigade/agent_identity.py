"""Best-effort agent identity shared by observed checks and outcomes."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
    model = model.lower() if isinstance(model, str) else ""
    return {
        "harness": harness if isinstance(harness, str) and harness in (*KNOWN_HARNESSES, "claude-code") else UNKNOWN,
        "model": model if re.fullmatch(r"[a-z0-9.-]{3,48}", model) and model.startswith(MODEL_FAMILIES) else UNKNOWN,
        "agent_type": agent_type if isinstance(agent_type, str) and agent_type in AGENT_TYPES else UNKNOWN,
    }
