"""Validate fleet claim keys in retained local job records."""

from __future__ import annotations


def valid_claim_target(value: object) -> bool:
    """A fleet claim key: bounded text with no control characters."""
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
