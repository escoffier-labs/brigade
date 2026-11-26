"""Decode local Codex and Claude batch results."""

from __future__ import annotations

import json
import re
from collections.abc import Callable


_LOCAL_SESSION_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def parse_local_batch_result(
    cli_ref: str, text: str, stderr: str, scrub: Callable[[str], str]
) -> tuple[str, str | None, str | None]:
    if cli_ref == "codex":
        # Codex exec prints the thread ID in its stderr preamble in text mode.
        match = re.search(rf"^session id: ({_LOCAL_SESSION_UUID.pattern})[ \t]*$", stderr, re.MULTILINE)
        codex_id = scrub(match[1]) if match else ""
        return text, codex_id if _LOCAL_SESSION_UUID.fullmatch(codex_id) else None, None

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return "", None, None
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return "", None, None

    answer, identifier = payload.get("result"), payload.get("session_id")
    session_id = None
    if isinstance(identifier, str) and _LOCAL_SESSION_UUID.fullmatch(identifier):
        candidate = scrub(identifier)
        if _LOCAL_SESSION_UUID.fullmatch(candidate):
            session_id = candidate

    if payload.get("is_error") is True:
        errors = payload.get("errors")
        for label in [payload.get("subtype"), *(errors if isinstance(errors, list) else [errors])]:
            if isinstance(label, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", label):
                error_class = scrub(label)
                if error_class == label:
                    return "", session_id, error_class
        return "", session_id, "claude-error"
    return answer.strip() if isinstance(answer, str) else "", session_id, None
