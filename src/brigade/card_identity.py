"""Stable card identities with legacy aliases for the file-backed memory store."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Generic, TypeVar

_CARD_ID_RE = re.compile(
    r"^card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

T = TypeVar("T")


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


class IdentityIndex(Generic[T]):
    """Dual-read index: explicit IDs and legacy aliases resolve to one record.

    ``claim`` is the collision contract used by the vault projector (#934). A
    key claimed by two different records becomes unresolvable. Canonical cards
    are never rewritten here.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, T | None] = {}

    def claim(self, key: str, record: T) -> None:
        existing = self._by_key.get(key)
        if existing is None:
            if key not in self._by_key:
                self._by_key[key] = record
        elif existing is not record:
            self._by_key[key] = None

    def claim_identity(self, identity: CardIdentity, record: T) -> None:
        self.claim(identity.card_id, record)
        for alias in identity.aliases:
            self.claim(alias, record)

    def resolve(self, key: str) -> T | None:
        """Return the unique record for ``key``, or ``None`` if missing or colliding."""
        return self._by_key.get(key)

    def get(self, key: str) -> T | None:
        return self.resolve(key)

    def available(self, key: str) -> bool:
        return key not in self._by_key

    def is_collision(self, key: str) -> bool:
        return key in self._by_key and self._by_key[key] is None

    def colliding_keys(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, value in self._by_key.items() if value is None))

    def records(self) -> tuple[T, ...]:
        seen: list[T] = []
        for value in self._by_key.values():
            if value is not None and all(value is not item for item in seen):
                seen.append(value)
        return tuple(seen)


def mint_stable_card_id(seed: str) -> str:
    """Derive a valid ``card-<uuid4>`` from a stable seed so dry-run matches apply.

    The digest is UUID5 over ``seed``, then the version nibble is forced to 4 so
    the ID keeps the ``card-<uuid4>`` shape ``valid_card_id`` accepts.
    """
    digest = uuid.uuid5(uuid.NAMESPACE_URL, seed)
    data = bytearray(digest.bytes)
    data[6] = (data[6] & 0x0F) | 0x40
    return f"card-{uuid.UUID(bytes=bytes(data))}"


def mint_claimed_card_id(index: IdentityIndex[T], record: T, *, seed: str | None = None) -> str:
    """Mint one unused card ID and claim it. Never replaces a claimed key.

    When ``seed`` is set, the ID is derived deterministically so a dry-run
    receipt predicts the IDs ``--apply`` will write.
    """
    for attempt in range(8):
        if seed is None:
            card_id = mint_card_id()
        else:
            card_id = mint_stable_card_id(seed if attempt == 0 else f"{seed}:{attempt}")
        if index.available(card_id):
            index.claim(card_id, record)
            return card_id
    raise RuntimeError("unable to mint a unique card id")


def mapping_old_id(identity: CardIdentity, relative_path: str) -> str:
    """Consumer-facing legacy key for dry-run receipts (never a card body).

    Non-explicit cards resolve as ``card_identity.card_id`` (topic, stem, or
    path) — the same key care queues, search logs, and refresh imports record.
    The ``path:`` prefix is not a consumer key.
    """
    if identity.card_id:
        return identity.card_id
    return f"path:{normalized_relative_path(relative_path)}"
