"""Conservative validation for provider output presented as a final result."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OutputFailure:
    kind: str
    detail: str


_PROVIDER_ERROR = re.compile(
    r"^\s*error:\s*nonretriableerror:\s*provider error\b",
    re.IGNORECASE | re.MULTILINE,
)
_OPERATIONAL_ERRORS = (
    (
        "authentication-error",
        re.compile(
            r"^\s*error:.*\b(?:authentication (?:required|failed|failure)|not authenticated|"
            r"unauthorized|invalid (?:api )?key)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "rate-limit-error",
        re.compile(
            r"^\s*error:.*\b(?:rate limit(?:ed| exceeded)?|quota exceeded)\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "provider-setting-error",
        re.compile(
            r"^\s*error:.*\b(?:unknown model|invalid model|model [^\n]+ (?:is )?not (?:available|found))\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "network-error",
        re.compile(
            r"^\s*error:.*\b(?:failed to connect|unable to connect|network error|"
            r"connection (?:refused|failed))\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
)
_CLAUSE_SPLIT = re.compile(r"(?:\r?\n)+|(?<=[.!?])\s+")
_FUTURE_INTENT = re.compile(
    r"\A\s*(?:(?:first|next|now)\s*,?\s*)?"
    r"(?:i\s+(?:will|shall|need to|plan to|am going to)|i['’](?:ll|m going to)|let me)\b",
    re.IGNORECASE,
)
_PROGRESS_ACTION = re.compile(
    r"\A\s*(?:reviewing|gathering|inspecting|reading|checking|searching|running|locating|"
    r"listing|opening|looking|finding|examining|analyzing|exploring|tracing|investigating)\b",
    re.IGNORECASE,
)
_ONGOING_PROGRESS = re.compile(
    r"\A\s*(?:(?:first|next|now)\s*,?\s*)?i(?:\s+am|['’]m)\s+"
    r"(?:reviewing|gathering|inspecting|reading|checking|searching|running|locating|"
    r"listing|opening|looking|finding|examining|analyzing|exploring|tracing|investigating)\b",
    re.IGNORECASE,
)
_OUTCOME_MARKER = re.compile(
    r"\b(?:found|identified|shows?|reveals?|"
    r"no (?:actionable )?(?:issues|findings|changes|regressions)|"
    r"(?:tests?|checks?) passed|(?:do not|don['’]t) see)\b",
    re.IGNORECASE,
)
_FINAL_TEXT_KEYS = frozenset({"answer", "content", "final", "final_answer", "output", "response", "text"})
_TOOL_KEYS = frozenset(
    {
        "function_call",
        "function_calls",
        "tool_call",
        "tool_calls",
        "tool_use",
        "tool_uses",
    }
)
_TOOL_RESULT_TYPES = frozenset({"function_result", "tool_result", "tool_response"})


def _contains_tool_marker(value: object) -> bool:
    if isinstance(value, dict):
        if any(str(key).lower() in _TOOL_KEYS for key in value):
            return True
        event_type = value.get("type")
        if isinstance(event_type, str):
            normalized_type = event_type.lower()
            if (
                normalized_type in _TOOL_KEYS
                or normalized_type in _TOOL_RESULT_TYPES
                or normalized_type.endswith("_call_output")
            ):
                return True
        normalized_keys = {str(key).lower() for key in value}
        if "call_id" in normalized_keys and "output" in normalized_keys:
            return True
        if "arguments" in normalized_keys and normalized_keys & {"function", "name"}:
            return True
        return any(_contains_tool_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_tool_marker(item) for item in value)
    return False


def _contains_final_text(value: object) -> bool:
    if isinstance(value, dict):
        if any(str(key).lower() in _TOOL_KEYS for key in value):
            return False
        role = value.get("role")
        event_type = value.get("type")
        if isinstance(role, str) and role.lower() in {"function", "tool"}:
            return False
        if isinstance(event_type, str):
            normalized_type = event_type.lower()
            if normalized_type in _TOOL_RESULT_TYPES or normalized_type.endswith("_call_output"):
                return False
        if "call_id" in value and "output" in value:
            return False
        if isinstance(event_type, str) and event_type.lower() in _TOOL_KEYS:
            return False
        normalized_keys = {str(key).lower() for key in value}
        if "arguments" in normalized_keys and normalized_keys & {"function", "name"}:
            return False
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in _TOOL_KEYS:
                continue
            if normalized_key in _FINAL_TEXT_KEYS and isinstance(item, str) and item.strip():
                return True
            if _contains_final_text(item):
                return True
    elif isinstance(value, list):
        return any(_contains_final_text(item) for item in value)
    return False


def _tool_only(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if payload is not None and _contains_tool_marker(payload) and not _contains_final_text(payload):
        return True
    return bool(
        re.fullmatch(
            r"\s*(?:(?:"
            r"<(?P<tag>tool_call|function_call|tool_use)\b[^>]*>[\s\S]*?</(?P=tag)>|"
            r"<(?:tool_call|function_call|tool_use)\b[^>]*/>"
            r")\s*)+",
            text,
            re.IGNORECASE,
        )
    )


def _progress_only(text: str) -> bool:
    clauses = [clause.strip() for clause in _CLAUSE_SPLIT.split(text) if clause.strip()]
    if not clauses:
        return False
    explicit_progress = False
    for clause in clauses:
        if _FUTURE_INTENT.match(clause) or _ONGOING_PROGRESS.match(clause):
            explicit_progress = True
            continue
        if _PROGRESS_ACTION.match(clause):
            if _OUTCOME_MARKER.search(clause):
                return False
            if ":" in clause:
                tail = clause.split(":", 1)[1].strip()
                tail_is_progress = bool(
                    _FUTURE_INTENT.match(tail)
                    or _ONGOING_PROGRESS.match(tail)
                    or _PROGRESS_ACTION.match(tail)
                    or re.match(r"(?:first|next|now)\b", tail, re.IGNORECASE)
                )
                if tail and not tail_is_progress:
                    return False
            explicit_progress = True
            continue
        return False
    return explicit_progress


def validate_final_output(text: str) -> OutputFailure | None:
    """Return a typed failure only for deterministic non-final output shapes."""
    stripped = text.strip()
    if not stripped:
        return OutputFailure("empty-output", "provider returned no final result")
    if "```" not in stripped:
        provider_error = _PROVIDER_ERROR.search(stripped)
        if provider_error is not None:
            prefix = stripped[: provider_error.start()].strip()
            if not prefix or _progress_only(prefix):
                diagnostic = stripped[provider_error.start() :].strip()
                return OutputFailure(
                    "provider-error",
                    f"provider returned an error instead of a final result: {diagnostic}"[:200],
                )
        for kind, pattern in _OPERATIONAL_ERRORS:
            operational_error = pattern.search(stripped)
            if operational_error is None:
                continue
            prefix = stripped[: operational_error.start()].strip()
            if not prefix or _progress_only(prefix):
                diagnostic = stripped[operational_error.start() :].strip()
                return OutputFailure(
                    kind,
                    f"provider returned an operational error instead of a final result: {diagnostic}"[:200],
                )
    if _tool_only(stripped):
        return OutputFailure(
            "tool-only-output",
            "provider returned tool-call data without a final result",
        )
    if _progress_only(stripped):
        return OutputFailure(
            "non-final-output",
            "provider returned progress or intent without a final result",
        )
    return None
