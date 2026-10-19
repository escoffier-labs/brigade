"""Charter section of the fleet policy document (``owners``).

Each owner is an agent or surface in the fleet (a bot, a bridge, a gateway)
with a declared charter: what it owns, what it must never touch, how work
reaches it, and the selectors that attribute live hub rows to it. The section
rides the versioned policy document, so it inherits revisions, compare-and-swap
saves, history, and rollback. Nothing here enforces a charter.

This module imports ``fleet_policy`` lazily, inside the parse functions,
because ``fleet_policy`` imports this module to validate the section.
"""

from __future__ import annotations

from typing import Any

OWNER_FIELDS = ("label", "summary", "owns", "never", "intake", "contact", "match")
MATCH_LIST_KEYS = ("nodes", "harnesses", "conductors", "session_harnesses", "adapters", "principals")
MATCH_KEYS = (*MATCH_LIST_KEYS, "grokbot_jobs")


def parse_owners(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize the ``owners`` mapping. ``None`` means no charters."""
    from . import fleet_policy as policy

    records = policy._records({} if raw is None else raw, "owners")
    return {
        policy._required_name(name, "owner name"): _parse_owner(str(name), records[name]) for name in sorted(records)
    }


def _parse_owner(name: str, raw: Any) -> dict[str, Any]:
    from . import fleet_policy as policy

    where = f"owner '{name}'"
    body = policy._mapping(raw, where)
    policy._reject_unknown(body, OWNER_FIELDS, where)
    label = policy._text(body.get("label"), f"{where} field 'label'", optional=False)
    if not label:
        raise policy.FleetPolicyError(f"fleet policy {where} field 'label' must not be empty")
    contact = body.get("contact")
    return {
        "label": label,
        "summary": policy._text(body.get("summary"), f"{where} field 'summary'") or None,
        "owns": _text_list(body.get("owns"), f"{where} field 'owns'"),
        "never": _text_list(body.get("never"), f"{where} field 'never'"),
        "intake": _text_list(body.get("intake"), f"{where} field 'intake'"),
        "contact": None if contact is None else policy._identity(contact, f"{where} field 'contact'"),
        "match": _parse_match(body.get("match"), f"{where} match"),
    }


def _text_list(raw: Any, where: str) -> list[str]:
    from . import fleet_policy as policy

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise policy.FleetPolicyError(f"fleet policy {where} must be a list")
    if len(raw) > policy.MAX_LIST_ITEMS:
        raise policy.FleetPolicyError(f"fleet policy {where} must hold at most {policy.MAX_LIST_ITEMS} items")
    items: list[str] = []
    for index, item in enumerate(raw):
        text = policy._text(item, f"{where}[{index}]", optional=False)
        if not text:
            raise policy.FleetPolicyError(f"fleet policy {where}[{index}] must not be empty")
        items.append(text)
    return items


def _parse_match(raw: Any, where: str) -> dict[str, Any]:
    from . import fleet_policy as policy

    body = {} if raw is None else policy._mapping(raw, where)
    policy._reject_unknown(body, MATCH_KEYS, where)
    parsed: dict[str, Any] = {
        key: policy._name_list(body.get(key), f"{where} field '{key}'") for key in MATCH_LIST_KEYS
    }
    parsed["grokbot_jobs"] = policy._bool(body.get("grokbot_jobs"), f"{where} field 'grokbot_jobs'", default=False)
    return parsed
