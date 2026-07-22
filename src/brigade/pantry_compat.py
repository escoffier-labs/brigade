"""Agent Pantry version compatibility probe (Brigade-side).

Agent Pantry is a separate Go binary at a process boundary; Brigade never
imports it. Before Brigade invokes any installed agentpantry ``doctor``,
``status``, or ``inventory`` surface, it probes ``agentpantry version --json``
and enforces an evidence-backed floor so a stale or malformed install is
reported as incompatible rather than silently invoking surfaces the
installed binary does not expose.

This module is the single shared parser/comparator for that probe so the
managed doctor, pantry status/doctor, and expiry-alert paths do not duplicate
parsing. It adds no dependency and never raises: a missing binary, nonzero
probe, malformed JSON, non-string version, prerelease-shaped, or below-floor
version all collapse to an incompatible :class:`VersionProbe` with a precise
detail string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from . import proc

# v0.5.0 is the first tagged release exposing every Brigade-invoked surface:
# ``doctor --json --no-net``, ``status --json``, ``inventory --json``, and
# ``version --json``. v0.4.1 lacks ``inventory``. This floor is evidence-backed
# and intentionally integer-compared so multi-digit segments (e.g. 0.10.3) sort
# correctly rather than lexically.
AGENTPANTRY_MIN_VERSION: Tuple[int, int, int] = (0, 5, 0)

# Conservative per-segment length bound. Each numeric segment of a released
# semver triple is realistically small; bounding the digit count keeps parsing
# non-throwing and bounded for arbitrarily long numeric segments. A hostile or
# malformed version field with a multi-megabyte numeric run would otherwise
# drive unbounded regex matching and ``int()`` allocation. Oversized segments
# collapse to the fixed :data:`_INVALID_VERSION_LABEL` via :func:`parse_version`
# returning ``None``.
_MAX_SEGMENT_DIGITS = 6

# Strict ASCII numeric semver triple with an optional leading ``v``. ``[0-9]``
# (not ``\d``) rejects Unicode digits so only ASCII numeric triples parse; a
# non-ASCII-digit run (e.g. Arabic-Indic or fullwidth digits) fails to match and
# collapses to :data:`_INVALID_VERSION_LABEL`. The ``{1,N}`` per-segment bound
# and the ``$`` anchor reject prerelease-shaped (``0.5.0-rc.1``), dev (``dev``),
# unknown (``unknown``), oversized, and any other non-triple strings. No
# build/prerelease suffix is accepted: only released triples pass.
_VERSION_RE = re.compile(
    rf"^v?([0-9]{{1,{_MAX_SEGMENT_DIGITS}}})\.([0-9]{{1,{_MAX_SEGMENT_DIGITS}}})\.([0-9]{{1,{_MAX_SEGMENT_DIGITS}}})$"
)

# Longest strict semver triple: optional ``v`` plus three bounded segments and two
# dots (e.g. ``v999999.999999.999999``). Reject overlong raw input before
# ``strip`` or regex matching so hostile whitespace-padded strings cannot force
# an unbounded scan.
_MAX_RAW_VERSION_LEN = 1 + (3 * _MAX_SEGMENT_DIGITS) + 2

# Fixed sanitized label for any unparsable version string (dev, unknown,
# prerelease-shaped, arbitrary content, or anything that could carry secret
# material). Never echo the raw invalid version field or any other stdout back
# to callers; collapse to this constant so managed doctor / pantry JSON cannot
# leak it. Successfully parsed triples (including below-floor ones) remain
# normalized semver because that is bounded numeric data, not raw content.
_INVALID_VERSION_LABEL = "invalid-version"


@dataclass(frozen=True)
class VersionProbe:
    """Result of probing ``agentpantry version --json``.

    ``compatible`` is True only when a parseable non-prerelease semver triple
    was observed at or above :data:`AGENTPANTRY_MIN_VERSION`. ``observed``
    carries the normalized semver triple for a successfully parsed version
    (including below-floor ones, which are bounded numeric data), or a short
    fixed label for a probe failure / unparsable value. For any unparsable
    string (dev, unknown, prerelease-shaped, or arbitrary content up to and
    including secret material) ``observed`` is the fixed sanitized label
    :data:`_INVALID_VERSION_LABEL`; the raw invalid version field and any
    other stdout never reach ``observed`` or ``detail``. ``detail`` always
    contains the literal floor expectation and the observed value or probe
    failure so callers can surface it verbatim.
    """

    compatible: bool
    observed: str
    detail: str

    @property
    def incompatible(self) -> bool:
        return not self.compatible


def floor_label() -> str:
    """Return the human-readable floor expectation, e.g. ``expected >= 0.5.0``."""
    major, minor, patch = AGENTPANTRY_MIN_VERSION
    return f"expected >= {major}.{minor}.{patch}"


def parse_version(value: object) -> Optional[Tuple[int, int, int]]:
    """Parse a version value into an integer ``(major, minor, patch)`` triple.

    Returns ``None`` for non-strings, missing values, prerelease-shaped, dev,
    unknown, non-ASCII-digit, oversized, or otherwise unparsable inputs.
    Comparison is integer, not lexical. Never raises: the per-segment digit
    bound keeps ``int()`` conversion bounded, and a conversion ``ValueError``
    is caught defensively and collapsed to ``None`` so the caller surfaces the
    fixed :data:`_INVALID_VERSION_LABEL` rather than propagating an exception.
    """
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_RAW_VERSION_LEN:
        return None
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _format_triple(triple: Tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in triple)


def _unparsable_observed_label(raw_version: object) -> str:
    """Return a non-leaking observed label for an unparsable version value.

    A missing field collapses to ``"missing"`` and a non-string value to
    ``"non-string"``; neither carries raw stdout. Any string that fails
    :func:`parse_version` (dev, unknown, prerelease-shaped, empty, oversized
    numeric segments, non-ASCII digits, or arbitrary content up to and
    including secret material) collapses to the fixed
    :data:`_INVALID_VERSION_LABEL` so the raw value never reaches observed or
    detail and therefore never reaches managed doctor / pantry JSON.
    """
    if raw_version is None:
        return "missing"
    if not isinstance(raw_version, str):
        return "non-string"
    return _INVALID_VERSION_LABEL


def probe_agentpantry_version() -> VersionProbe:
    """Probe ``agentpantry version --json`` and enforce the floor.

    Never raises. The caller is responsible for gating on whether the binary
    is installed/detected; this function only runs the probe and interprets it.
    A nonzero probe exit (including command-not-found 127), non-JSON stdout, a
    non-object JSON value, a missing/non-string/prerelease-shaped version
    field, or a below-floor version all yield an incompatible probe.
    """
    expected = floor_label()
    result = proc.run(["agentpantry", "version", "--json"], timeout=10.0)
    if result.code != 0:
        observed = f"probe exit {result.code}"
        return VersionProbe(
            compatible=False,
            observed=observed,
            detail=f"agentpantry version probe failed (probe exit {result.code}); {expected}",
        )
    data = result.json()
    if not isinstance(data, dict):
        observed = "malformed"
        return VersionProbe(
            compatible=False,
            observed=observed,
            detail=f"agentpantry version output is not a JSON object; {expected}",
        )
    raw_version = data.get("version")
    parsed = parse_version(raw_version)
    if parsed is None:
        observed = _unparsable_observed_label(raw_version)
        return VersionProbe(
            compatible=False,
            observed=observed,
            detail=f"agentpantry version unparsable ({observed}); {expected}",
        )
    observed = _format_triple(parsed)
    if parsed < AGENTPANTRY_MIN_VERSION:
        return VersionProbe(
            compatible=False,
            observed=observed,
            detail=f"agentpantry version {observed} below floor; {expected}",
        )
    return VersionProbe(
        compatible=True,
        observed=observed,
        detail=f"agentpantry version {observed}; {expected}",
    )
