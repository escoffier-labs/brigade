"""Hub retriever positions stay separate from fusion source positions."""

from brigade.memory_hub_search import fuse


def test_hub_ranks_survive_a_local_winner():
    shared = {"card_id": "shared", "content_hash": "same", "path": "memory/cards/shared.md"}
    lineage = [{"scope": "fleet", "card_id": "ancestor", "revision": 1}]
    matches = fuse(
        {
            "hub": [
                {"card_id": "other", "scope": "fleet", "content_hash": "other"},
                {
                    **shared,
                    "scope": "fleet",
                    "ranks": {"lexical": 8, "dense": 3},
                    "lineage": lineage,
                    "source_demoted": True,
                },
            ],
            "local": [shared],
            "replica": [{**shared, "scope": "fleet"}],
        },
        20,
        home_scope="fleet",
    )
    match = next(match for match in matches if match["card_id"] == "shared")
    assert match["source"] == "local"
    assert match["ranks"] == {"hub": 2, "local": 1, "replica": 1}
    assert match["hub_ranks"] == {"lexical": 8, "dense": 3}
    assert match["lineage"] == lineage
    assert match["source_demoted"] is True


def test_hub_ranks_update_for_each_contributing_hub_match():
    shared = {"card_id": "shared", "content_hash": "same", "scope": "fleet"}
    [match] = fuse(
        {"hub": [{**shared, "ranks": {"lexical": 9}}, {**shared, "ranks": {"dense": 3}}]},
        20,
    )
    assert match["ranks"] == {"hub": 2}
    assert match["hub_ranks"] == {"dense": 3}
