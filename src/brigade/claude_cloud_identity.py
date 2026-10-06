"""Pure syntax normalization for explicitly supplied Claude subscription cloud IDs.

This module establishes no provider lifecycle, repository binding, or holder
permission. It performs no I/O and never launches or contacts Claude.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Brigade validation limits, not a claim about the provider's complete ID schema.
_SESSION_ID = re.compile(r"(?:session_|cse_)[A-Za-z0-9_-]{1,128}")
_MAX_INPUT_LENGTH = 4096
_INVALID_IDENTITY = "Invalid Claude cloud session identity"


@dataclass(frozen=True)
class ClaudeCloudIdentity:
    """Canonical case-sensitive session ID returned by the normalizer."""

    session_id: str

    @property
    def url(self) -> str:
        """Return the canonical HTTPS URL without query or fragment."""
        return f"https://claude.ai/code/{self.session_id}"


def normalize_claude_cloud_identity(value: str) -> ClaudeCloudIdentity:
    """Accept a documented ID prefix or an exact-authority HTTPS session URL.

    Suffixes contain 1..128 ASCII letters, digits, underscores or hyphens.
    Inputs contain at most 4096 printable ASCII characters without whitespace.
    URLs require the literal authority ``claude.ai`` and one ``/code/<id>``
    path. Query and fragment are discarded, but still checked for unsafe input
    characters. No decoding or whitespace trimming occurs. Invalid input raises
    a fixed ``ValueError`` that never includes the supplied value.
    """
    if (
        not isinstance(value, str)
        or len(value) > _MAX_INPUT_LENGTH
        or any(not 33 <= ord(char) <= 126 for char in value)
    ):
        raise ValueError(_INVALID_IDENTITY)

    if _SESSION_ID.fullmatch(value):
        return ClaudeCloudIdentity(session_id=value)

    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError(_INVALID_IDENTITY) from None

    if parsed.scheme != "https" or parsed.netloc != "claude.ai" or not parsed.path.startswith("/code/"):
        raise ValueError(_INVALID_IDENTITY)
    session_id = parsed.path[len("/code/") :]
    if not _SESSION_ID.fullmatch(session_id):
        raise ValueError(_INVALID_IDENTITY)
    return ClaudeCloudIdentity(session_id=session_id)
