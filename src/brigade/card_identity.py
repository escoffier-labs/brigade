"""Stable card identities with legacy aliases for the file-backed memory store."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

_CARD_ID_RE = re.compile(
    r"^card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CardIdentity:
    """Primary card identity plus compatibility aliases."""

    card_id: str
    aliases: tuple[str, ...]
    explicit: bool


def valid_card_id(value: object) -> str | None:
    """Return a normalized stable ID, or ``None`` when ``value`` is not one."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if _CARD_ID_RE.fullmatch(candidate) else None


def mint_card_id() -> str:
    """Mint the opaque UUID4 identity used by new canonical cards."""
    return f"card-{uuid.uuid4()}"


def normalized_relative_path(value: str) -> str:
    """Normalize a workspace-relative card path for legacy fallback reads."""
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def card_identity(frontmatter: dict[str, Any], relative_path: str) -> CardIdentity:
    """Prefer a valid explicit ID and retain historical keys as aliases."""
    relative_path = normalized_relative_path(relative_path)
    explicit = valid_card_id(frontmatter.get("id")) or valid_card_id(frontmatter.get("card_id"))
    aliases = _legacy_aliases(frontmatter, relative_path)
    if explicit is not None:
        return CardIdentity(
            card_id=explicit, aliases=tuple(alias for alias in aliases if alias != explicit), explicit=True
        )
    legacy = _legacy_primary(frontmatter) or PurePosixPath(relative_path).stem
    return CardIdentity(card_id=legacy, aliases=tuple(alias for alias in aliases if alias != legacy), explicit=False)


def _legacy_aliases(frontmatter: dict[str, Any], relative_path: str) -> list[str]:
    aliases = [relative_path, PurePosixPath(relative_path).stem]
    for key in ("id", "card_id", "topic"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            aliases.append(value.strip())
    return list(dict.fromkeys(aliases))


def _legacy_primary(frontmatter: dict[str, Any]) -> str | None:
    for key in ("id", "card_id", "topic"):
        value = frontmatter.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def identity_collision_aliases(items: list[tuple[str, CardIdentity]]) -> dict[str, tuple[str, ...]]:
    """Return deterministic aliases claimed by more than one primary identity."""
    owners: dict[str, set[str]] = {}
    for path, identity in items:
        for alias in (identity.card_id, *identity.aliases):
            owners.setdefault(alias, set()).add(path)
    return {alias: tuple(sorted(paths)) for alias, paths in sorted(owners.items()) if len(paths) > 1}
