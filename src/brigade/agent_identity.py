"""Best-effort agent identity shared by observed checks and outcomes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from . import localio

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
