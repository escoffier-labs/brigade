"""Hub ownership map: policy charters joined to what each owner holds right now.

Read-only. ``attribute`` is a pure matcher over plain dataclasses, so it is
tested without a database. Nothing here enforces a charter: an item no charter
matches lands in ``unassigned``, and an item two charters match is listed under
both and reported in ``overlaps``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA = "brigade.fleet_owners.v1"
SOURCE_LIMIT = 500


@dataclass(frozen=True)
class Holding:
    """One live item, normalized. Selector fields are not part of the public shape."""

    kind: str
    ref: str
    repo: str
    node_label: str
    detail: str
    since: str
    nodes: tuple[str, ...] = ()
    harness: str = ""
    conductor: str = ""
    session_harness: str = ""
    adapter: str = ""

    def public(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "repo": self.repo,
            "node_label": self.node_label,
            "detail": self.detail,
            "since": self.since,
        }


@dataclass(frozen=True)
class Overlap:
    item: Holding
    owners: tuple[str, ...]


def _matches(match: Mapping[str, Any], item: Holding) -> bool:
    if match.get("grokbot_jobs") and item.kind == "grokbot-job":
        return True
    if set(match.get("nodes") or ()).intersection(item.nodes):
        return True
    for key, value in (
        ("harnesses", item.harness),
        ("conductors", item.conductor),
        ("session_harnesses", item.session_harness),
        ("adapters", item.adapter),
    ):
        if value and value in (match.get(key) or ()):
            return True
    return False


def attribute(
    charters: Mapping[str, Mapping[str, Any]], items: Sequence[Holding]
) -> tuple[dict[str, list[Holding]], list[Holding], list[Overlap]]:
    """Split *items* into per-owner lists, an unassigned list, and overlaps.

    An item matches an owner when any one selector in that owner's ``match``
    record matches (OR). A charter with no selectors matches nothing. An item
    two charters match is listed under both and once in the overlaps.
    """
    by_owner: dict[str, list[Holding]] = {name: [] for name in charters}
    unassigned: list[Holding] = []
    overlaps: list[Overlap] = []
    for item in items:
        owners = [name for name in sorted(charters) if _matches(charters[name].get("match") or {}, item)]
        if not owners:
            unassigned.append(item)
            continue
        for name in owners:
            by_owner[name].append(item)
        if len(owners) > 1:
            overlaps.append(Overlap(item=item, owners=tuple(owners)))
    return by_owner, unassigned, overlaps
