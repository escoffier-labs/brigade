"""Pure syntax checks for conductor-launched Claude cloud routine sessions.

A conductor launches a Claude cloud session through a one-time routine and
then registers it here. Brigade never launches the session, never calls the
routines interface and never holds its credential. This module performs no
I/O. It validates caller-supplied identifiers and words only, and it does not
verify that a routine or run exists.
"""

from __future__ import annotations

import re

# Brigade validation limits, not a claim about the provider's complete schema.
_ROUTINE_ID = re.compile(r"trig_[A-Za-z0-9_-]{1,128}")
_STATUS_WORD = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ISSUE_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
_INVALID_ROUTINE = "Invalid Claude routine identity"
_INVALID_STATUS = "Invalid Claude observed status"
_INVALID_ISSUE = "Invalid Claude issue reference"

LIFECYCLE_SOURCE = "conductor-reported"
OBSERVATION_REASON = "conductor-routed"
LAUNCH_REFUSAL_REASON = "conductor-routed-launch"
RUNBOOK = "docs/claude-cloud-capabilities.md#conductor-runbook"
SOURCE_DETAIL = (
    "conductor-launched routine sessions are tracked from the local registry; "
    "status comes from GitHub and conductor-reported run state; launch stays with the conductor"
)
LAUNCH_REFUSAL_DETAIL = (
    "Brigade does not launch Claude cloud sessions. The conductor launches them through "
    "one-time Claude Code routines, then records the session with "
    "`brigade run cloud register --provider claude-cloud`. Runbook: " + RUNBOOK
)


def normalize_routine_id(value: object) -> str:
    """Return a case-sensitive ``trig_`` routine ID or raise a fixed error."""
    if not isinstance(value, str) or not _ROUTINE_ID.fullmatch(value):
        raise ValueError(_INVALID_ROUTINE)
    return value


def normalize_observed_status(value: object) -> str:
    """Return one lower-case status word that a conductor read from a routine run."""
    if not isinstance(value, str):
        raise ValueError(_INVALID_STATUS)
    word = value.lower()
    if not _STATUS_WORD.fullmatch(word):
        raise ValueError(_INVALID_STATUS)
    return word


def normalize_issue(value: object, repository: str) -> int:
    """Return an issue number from a number or the bound repository's issue URL."""
    if not isinstance(value, str):
        raise ValueError(_INVALID_ISSUE)
    number = value
    prefix = "https://github.com/"
    if value.startswith(prefix):
        slug, separator, number = value[len(prefix) :].partition("/issues/")
        if not separator or slug.lower() != repository:
            raise ValueError(_INVALID_ISSUE)
    if not _ISSUE_NUMBER.fullmatch(number) or int(number) > (1 << 63) - 1:
        raise ValueError(_INVALID_ISSUE)
    return int(number)
