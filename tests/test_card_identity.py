"""Stable card IDs, legacy fallbacks, and claim()-based dual-read (#867)."""

from __future__ import annotations

import re

import pytest

from brigade.card_fingerprint import ensure_card_id_frontmatter, reinforce_existing_card
from brigade.card_identity import (
    IdentityIndex,
    card_identity,
    mapping_old_id,
    mint_card_id,
    mint_claimed_card_id,
    mint_stable_card_id,
    valid_card_id,
)

_CARD_ID = re.compile(r"^card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def test_mint_card_id_is_opaque_uuid4():
    first = mint_card_id()
    second = mint_card_id()
    assert first != second
    assert _CARD_ID.fullmatch(first)
    assert valid_card_id(first) == first.lower()
    assert valid_card_id("legacy-topic") is None
    assert valid_card_id("card-not-a-uuid") is None


def test_legacy_cards_use_path_fallback_and_keep_aliases():
    identity = card_identity({"topic": "rollout"}, "memory/cards/renamed.md")
    assert identity.explicit is False
    assert identity.card_id == "rollout"
    assert "memory/cards/renamed.md" in identity.aliases
    assert "renamed" in identity.aliases
    assert mapping_old_id(identity, "memory/cards/renamed.md") == "rollout"


def test_explicit_id_survives_rename_and_keeps_legacy_alias():
    card_id = "card-123e4567-e89b-42d3-a456-426614174000"
    identity = card_identity({"id": card_id, "topic": "rollout"}, "memory/cards/renamed.md")
    assert identity.explicit is True
    assert identity.card_id == card_id
    assert "rollout" in identity.aliases
    assert "memory/cards/renamed.md" in identity.aliases
    assert mapping_old_id(identity, "memory/cards/renamed.md") == card_id


def test_claim_marks_alias_collisions_unresolvable_without_rewriting():
    index: IdentityIndex[str] = IdentityIndex()
    index.claim("memory/cards/beta.md", "beta.md")
    index.claim("memory/cards/beta.md", "alpha.md")
    assert index.is_collision("memory/cards/beta.md")
    assert index.resolve("memory/cards/beta.md") is None
    assert index.colliding_keys() == ("memory/cards/beta.md",)


@pytest.mark.parametrize("id_field", ["id", "card_id"])
def test_migrated_keys_resolve_to_survivor_and_third_card_collision_stays_unresolvable(id_field):
    survivor_id = "card-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    removed_id = "card-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    keys = [removed_id, "memory/cards/removed.md", "removed"]
    survivor = object()
    identity = card_identity(
        {id_field: survivor_id, "aliases": [*keys, " removed ", survivor_id]},
        "memory/cards/survivor.md",
    )
    index: IdentityIndex[object] = IdentityIndex()
    index.claim_identity(identity, survivor)
    assert identity.card_id == survivor_id
    assert identity.aliases[-3:] == tuple(keys)
    for key in [survivor_id, *keys]:
        assert index.resolve(key) is survivor

    third = object()
    index.claim_identity(card_identity({"topic": "removed"}, "memory/cards/third.md"), third)
    assert index.resolve("removed") is None
    assert index.is_collision("removed")
    assert index.resolve(removed_id) is survivor
    index.claim_identity(identity, survivor)
    assert index.resolve("removed") is None


@pytest.mark.parametrize("metadata", [{}, {"id": "legacy"}, {"id": 42}, {"card_id": "card-invalid"}])
def test_explicit_aliases_cannot_grant_identity_to_legacy_cards(metadata):
    identity = card_identity({**metadata, "aliases": ["removed"]}, "memory/cards/current.md")
    index: IdentityIndex[object] = IdentityIndex()
    index.claim_identity(identity, object())
    assert index.resolve("removed") is None
    assert index.resolve("current") is not None


@pytest.mark.parametrize(
    "aliases",
    [
        "removed",
        ("removed",),
        {"removed": True},
        ["removed", 42],
        ["removed", None],
        ["removed", ["nested"]],
        ["removed", {"nested": "value"}],
        ["removed", " "],
        ["removed", "line\nbreak"],
        ["removed", "x" * 1025],
        ["removed", *[f"old-{i}" for i in range(64)]],
    ],
)
def test_malformed_or_unbounded_alias_lists_do_not_resolve_any_migrated_key(aliases):
    identity = card_identity(
        {"id": "card-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "aliases": aliases},
        "memory/cards/current.md",
    )
    index: IdentityIndex[object] = IdentityIndex()
    index.claim_identity(identity, object())
    assert index.resolve("removed") is None
    assert index.resolve("current") is not None


def test_mint_claimed_card_id_never_replaces_existing():
    existing = "card-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    index: IdentityIndex[str] = IdentityIndex()
    index.claim(existing, "kept.md")
    minted = mint_claimed_card_id(index, "new.md")
    assert minted != existing
    assert _CARD_ID.fullmatch(minted)
    assert index.resolve(existing) == "kept.md"
    assert index.resolve(minted) == "new.md"


def test_mint_claimed_card_id_is_deterministic_for_the_same_seed():
    seed = "path:memory/cards/incomplete-legacy.md"
    first = mint_stable_card_id(seed)
    second = mint_stable_card_id(seed)
    other = mint_stable_card_id("path:memory/cards/complete-legacy.md")
    assert first == second
    assert first != other
    assert valid_card_id(first) == first
    index: IdentityIndex[str] = IdentityIndex()
    claimed = mint_claimed_card_id(index, "incomplete-legacy.md", seed=seed)
    again: IdentityIndex[str] = IdentityIndex()
    assert mint_claimed_card_id(again, "incomplete-legacy.md", seed=seed) == claimed
    assert claimed == first


def test_ensure_card_id_preserves_valid_id_on_repeat_write():
    card_id = "card-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    first = ensure_card_id_frontmatter("---\ntopic: demo\n---\n\nbody\n")
    match = re.search(
        r"^id: (card-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
        first,
        re.MULTILINE,
    )
    assert match is not None
    repeated = ensure_card_id_frontmatter(first)
    assert f"id: {match.group(1)}" in repeated
    preserved = ensure_card_id_frontmatter("---\ntopic: other\n---\n\nnew\n", preserve_id=card_id)
    assert f"id: {card_id}" in preserved
    assert preserved.count("id:") == 1


def test_reinforce_does_not_change_valid_id(tmp_path):
    from datetime import date

    card_id = "card-cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    path = tmp_path / "card.md"
    path.write_text(f"---\nid: {card_id}\ntopic: demo\n---\n\nbody\n", encoding="utf-8")
    reinforce_existing_card(path, today=date(2026, 8, 18), evidence_pointer="README.md")
    text = path.read_text(encoding="utf-8")
    assert f"id: {card_id}" in text
    assert text.count("id:") == 1
