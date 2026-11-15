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


def test_later_hub_match_without_ranks_clears_older_hub_ranks():
    shared = {"card_id": "shared", "content_hash": "same", "scope": "fleet"}
    [match] = fuse({"hub": [{**shared, "ranks": {"lexical": 9}}, dict(shared)]}, 20)
    assert match["ranks"] == {"hub": 2}
    assert "hub_ranks" not in match


def test_hub_records_local_winner_before_fusion(tmp_path, monkeypatch):
    from brigade import memory_hub_search as search
    from brigade.memory_hub_client import HubSettings

    monkeypatch.setenv("BRIGADE_SESSION_ID", "session-one")
    monkeypatch.setenv("BRIGADE_SESSION_KIND", "interactive")
    shared = {"card_id": "shared", "content_hash": "same", "path": "memory/cards/shared.md"}
    monkeypatch.setattr(search, "_local_matches", lambda *a: ([shared], None))
    requests = []

    def transport(url, token, body, timeout):
        requests.append(body)
        return 200, {
            "search_id": 1,
            "matches": [{"scope": "fleet", "card_id": "other"}, {**shared, "scope": "fleet"}],
            "retrieval": {"mode": "lexical", "stale": False},
        }

    settings = HubSettings(
        mode="read", scope="fleet", read_scopes=("fleet",), hub_url="https://hub.example.test", token="fake-token"
    )
    result = search.search_payload(tmp_path, "query", settings=settings, transport=transport)
    match = next(m for m in result["matches"] if m["card_id"] == "shared")
    assert match["source"] == "local"
    assert match["ranks"]["hub"] == 2
    assert requests[0]["session_id"] == "session-one"
    assert requests[0]["session_kind"] == "interactive"
    monkeypatch.setenv("BRIGADE_RUN_ID", "job")
    search.search_payload(tmp_path, "query", settings=settings, transport=transport)
    assert "session_id" not in requests[-1]
    assert "session_kind" not in requests[-1]
