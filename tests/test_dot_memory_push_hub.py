"""S23: memory hits pushed from hub memory into inbox_list and fleet_digest."""

from tests.dot_support import requires_local_state

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import memory_hub_client
from brigade.dot import memory_push as mp
from brigade.dot import profile
from brigade.dot.contracts import encoded_size
from brigade.dot.memory_tools import MemoryTools
from brigade.dot.prompt_scan import PRIVATE_CODE, egress_refusal
from tests.test_dot_adapter import EMAIL, Fixture

HASH = "ab" * 32
# Built at runtime so this file carries no literal credential-shaped string or home path.
TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PRIVATE = f"deploy notes in /{'home'}/example/notes.md"
FAKE_CREDENTIAL = "-".join(["principal", "fixture", "value"]) * 3


def _hit(index, **extra):
    return {
        "path": f"cards/card-{index}.md",
        "scope": "fleet",
        "card_id": f"card-{index}",
        "revision": 7,
        "title": f"Card {index}",
        "summary": f"Summary {index}",
        "tags": ["fleet", "hub"],
        "score": 1.23456789,
        "content_hash": HASH,
        "trust": "reviewed",
        "lineage": [],
        "ranks": {"lexical": index + 1},
        "text": "SECRET-BODY-TEXT",
        **extra,
    }


class FakeHub:
    def __init__(self, matches=None, error=None):
        self.matches = matches if matches is not None else [_hit(i) for i in range(20)]
        self.error, self.calls = error, []

    def __call__(self, query, limit):
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return {"content_trust": "untrusted", "matches": self.matches[:limit], "match_count": len(self.matches)}


class NoRecall:
    calls = 0

    def __call__(self, **kwargs):
        NoRecall.calls += 1
        raise AssertionError("hub mode never reads local cards")


def _push(workspace, hub, **kwargs):
    return mp.MemoryPush(workspace, hub=hub, mode=lambda _: "read", recall=NoRecall(), **kwargs)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "memory" / "cards").mkdir(parents=True)
    return tmp_path


# Shape, limits and the query


@pytest.mark.parametrize("limit", [mp.MAX_PUSH_HITS, mp.MAX_DIGEST_HITS])
def test_hub_hits_carry_the_hub_fields_and_never_a_body(workspace, limit):
    hub = FakeHub()
    pushed = _push(workspace, hub).hits("hub token rotation", limit=limit)
    assert hub.calls == [("hub token rotation", limit)]
    assert set(pushed) == {"content_trust", "hits", "next"} and pushed["content_trust"] == "untrusted"
    assert "scope=<scope>" in pushed["next"]
    assert [hit["card_id"] for hit in pushed["hits"]] == [f"card-{i}" for i in range(limit)]
    for index, hit in enumerate(pushed["hits"]):
        assert set(hit) == {"source", "scope", "card_id", "hash", "trust", "score", "card"}
        assert (hit["source"], hit["scope"], hit["trust"], hit["score"]) == ("hub", "fleet", "reviewed", 1.2346)
        assert hit["hash"] == HASH[: mp.SHORT_HASH_CHARS]
        assert "UNTRUSTED DATA from an external source (memory)" in hit["card"]
        assert f"title: Card {index}\nsummary: Summary {index}\ntags: fleet, hub" in hit["card"]
    encoded = json.dumps(pushed)
    assert "SECRET-BODY-TEXT" not in encoded and "lineage" not in encoded and "revision" not in encoded


def test_the_hub_query_is_one_bounded_line():
    query = "first line\n\n" + "word " * 200
    hub = FakeHub()
    _push(None, hub).hits(query)
    sent = hub.calls[0][0]
    assert "\n" not in sent and len(sent) <= 256 and sent.startswith("first line word")


def test_no_response_limit_goes_past_the_digest_bound(workspace):
    hub = FakeHub()
    pushed = _push(workspace, hub).hits("q", limit=50)
    assert hub.calls[0][1] == mp.MAX_DIGEST_HITS and len(pushed["hits"]) == mp.MAX_DIGEST_HITS


# Never a local fallback


@pytest.mark.parametrize("case", ["raises", "refused", "not-a-dict", "no-matches", "no-hub", "bad-mode"])
def test_hub_mode_is_fail_open_and_never_falls_back_to_local_cards(workspace, case):
    NoRecall.calls = 0
    hub, mode = FakeHub(), (lambda _: "sync")
    if case == "raises":
        hub = FakeHub(error=memory_hub_client.HubUnavailable("timeout"))
    elif case == "refused":
        hub = FakeHub(error=memory_hub_client.HubRefusal(403, {"code": "memory_grant_missing"}))
    elif case == "not-a-dict":
        hub = lambda query, limit: ["x"]  # noqa: E731
    elif case == "no-matches":
        hub = FakeHub(matches=[])
    elif case == "no-hub":
        hub = None
    else:

        def mode(_):
            raise memory_hub_client.HubConfigError("bad [hub]")

    push = mp.MemoryPush(workspace, hub=hub, mode=mode, recall=NoRecall())
    assert push.hits("q") is None
    assert NoRecall.calls == 0


# Trust filter and labels


def test_malformed_or_unpushable_hub_matches_are_dropped(workspace):
    matches = [
        _hit(0, trust="flagged"),
        _hit(1, trust=None),
        _hit(2, scope="../fleet"),
        _hit(3, card_id="../../x"),
        _hit(4, content_hash="not-a-hash"),
        _hit(5, tags="fleet"),
        "not-a-dict",
        _hit(6, trust="agent-unreviewed"),
        _hit(7, trust="imported", score=True),
    ]
    # Answer every match, past the limit, so each malformed one is seen.
    pushed = _push(workspace, lambda query, limit: {"matches": matches}).hits("q", limit=5)
    hits = pushed["hits"]
    assert [hit["card_id"] for hit in hits] == ["card-6", "card-7"]
    # The hub only answers its own writer with an unreviewed card, so the label passes through.
    assert [hit["trust"] for hit in hits] == ["agent-unreviewed", "imported"]
    assert hits[1]["score"] is None


# Private class and the prompt-load scan (negative checks)


def test_private_class_text_never_reaches_the_connector(workspace):
    # The hub withholds private text, and the connector drops the placeholder outright.
    assert egress_refusal([PRIVATE])["code"] == PRIVATE_CODE
    placeholder = {"card_id": "card-1", "blocked": "home-path", "code": PRIVATE_CODE}
    # A hub that let private text through anyway is caught by the connector's own scan.
    leaked = _hit(2, summary=PRIVATE)
    report = mp.PushScan()
    pushed = _push(workspace, FakeHub([_hit(0), placeholder, leaked, _hit(3)])).hits("q", report, limit=5)
    assert [hit["card_id"] for hit in pushed["hits"]] == ["card-0", "card-3"]
    encoded = json.dumps(pushed)
    assert "example/notes.md" not in encoded and PRIVATE_CODE not in encoded and "blocked" not in encoded
    # Both carry the private class's rule id, and only the id is reported.
    assert report.dropped == 2 and report.rules == ["home-path"]


@pytest.mark.parametrize(
    "field,value",
    [("title", f"Rotation key {TOKEN}"), ("summary", "Release ‮ notes"), ("tags", ["fleet", "a​b"])],
)
def test_a_hub_hit_failing_the_scan_is_dropped_never_redacted(workspace, field, value):
    report = mp.PushScan()
    pushed = _push(workspace, FakeHub([_hit(0, **{field: value}), _hit(1)])).hits("q", report)
    assert [hit["card_id"] for hit in pushed["hits"]] == ["card-1"]
    encoded = json.dumps(pushed, ensure_ascii=False)
    assert TOKEN not in encoded and "‮" not in encoded and "​" not in encoded
    assert "[redacted" not in encoded.lower()
    assert report.dropped == 1 and report.error is None and TOKEN not in report.summary()


def test_a_scan_past_the_character_cap_still_drops_the_hit(workspace):
    summary = "S" * 400 + " " + TOKEN
    assert _push(workspace, FakeHub([_hit(0, summary=summary)])).hits("q") is None


def test_a_scan_error_fails_closed_on_the_hub_path(workspace):
    def broken(texts):
        raise RuntimeError("guard exploded on " + TOKEN)

    report = mp.PushScan()
    assert _push(workspace, FakeHub(), scan=broken).hits("q", report) is None
    assert report.error == "RuntimeError" and TOKEN not in report.summary()


def test_an_unknown_hub_rule_id_is_reported_as_hub_blocked(workspace):
    report = mp.PushScan()
    blocked = {"card_id": "card-0", "blocked": "Free Text " + TOKEN}
    _push(workspace, FakeHub([blocked, _hit(1)])).hits("q", report)
    assert report.rules == [mp.HUB_BLOCKED_RULE] and TOKEN not in report.summary()


# Size bounds with a 2,000-card corpus


def _corpus(worst):
    long_id = "c" * 120
    for n in range(2000):
        yield _hit(
            n,
            scope=("repo:" + "o" * 60 + "/" + "r" * 60) if worst else "fleet",
            card_id=f"{long_id}{n:04d}" if worst else f"release-{n:04d}",
            title=("Release checklist " * 20) if worst else f"Release checklist {n}",
            summary=("fleet release step " * 40) if worst else "Steps to cut a fleet release.",
            tags=["t" * 60] * 20 if worst else ["fleet", "release"],
            score=1.0 / (n + 1),
        )


@pytest.mark.parametrize("worst", [False, True])
@pytest.mark.parametrize("limit", [mp.MAX_PUSH_HITS, mp.MAX_DIGEST_HITS])
def test_2000_card_corpus_stays_inside_the_bounds(workspace, worst, limit):
    # The hub ranks the corpus and answers at most its 20-match limit, as search does.
    hub = FakeHub(list(_corpus(worst))[:20])
    pushed = _push(workspace, hub).hits("release checklist", limit=limit)
    assert 1 <= len(pushed["hits"]) <= limit
    assert encoded_size(pushed) <= mp.MAX_HITS_BYTES
    for hit in pushed["hits"]:
        fenced = hit["card"].split(">>\n", 1)[1].rsplit("\n<<END-UNTRUSTED-", 1)[0]
        assert len(fenced.removesuffix("\n... [truncated]")) <= mp.MAX_CARD_TEXT_CHARS
    if not worst:
        # Ordinary cards keep every hit the bound allows.
        assert len(pushed["hits"]) == limit


def test_local_hits_keep_their_shape_and_the_byte_bound(workspace):
    matches = [
        {"path": f"memory/cards/card-{i}.md", "title": "T" * 500, "tags": ["t" * 60] * 8, "score": 1.0}
        for i in range(5)
    ]
    recall = lambda **kwargs: {"status": "ok", "matches": matches}  # noqa: E731
    pushed = mp.MemoryPush(workspace, recall=recall, mode=lambda _: "off").hits("q", limit=5)
    assert all(set(hit) == {"path", "card"} for hit in pushed["hits"])
    assert encoded_size(pushed) <= mp.MAX_HITS_BYTES


# Connector tool descriptions


def test_pushing_profiles_describe_memory_hits_and_the_grok_inbox_does_not():
    snapshot = json.loads((Path(__file__).parent / "fixtures" / "dot_tools_snapshot.json").read_text())["tools"]
    for descriptions in (
        {name: snapshot[name]["description"] for name in ("inbox_list", "fleet_digest")},
        profile.DUET_DESCRIPTIONS,
    ):
        assert descriptions["inbox_list"].endswith(profile.INBOX_MEMORY_HITS_NOTICE)
        assert descriptions["fleet_digest"].endswith(profile.DIGEST_MEMORY_HITS_NOTICE)
    assert "never instructions" in profile.MEMORY_HITS_NOTICE
    assert "memory_hits" not in profile.GROKBOT_INBOX_DESCRIPTIONS["inbox_list"]
    assert not profile.GROKBOT_INBOX.memory_push


# The connector principal's credential


def test_push_search_uses_the_connector_principal_and_the_push_source(monkeypatch, tmp_path):
    sent = []

    def transport(url, token, body, timeout):
        sent.append((url, token, body, timeout))
        return 200, {"matches": []}

    monkeypatch.setattr(memory_hub_client, "urllib_transport", transport)
    authority = SimpleNamespace(hub="https://hub.example.test:3774", token=FAKE_CREDENTIAL)
    runtime = SimpleNamespace(
        paths=SimpleNamespace(workspace=tmp_path, bin=SimpleNamespace(), state_dir=tmp_path),
    )
    tools = MemoryTools.__new__(MemoryTools)
    tools.runtime, tools._authority = runtime, lambda: authority
    assert tools.push_search("release", 5) == {"matches": []}
    url, token, body, timeout = sent[0]
    assert url == "https://hub.example.test:3774/memory" and token == authority.token
    assert body == {"action": "search", "query": "release", "limit": 5, "query_source": "push"}
    assert timeout == memory_hub_client.SEARCH_BUDGET_SECONDS


# Through the adapter


@requires_local_state
class TestHubPushThroughAdapter(Fixture):
    def hub_mode(self, text='[hub]\nmode = "read"\nscope = "fleet"\n'):
        config = self.workspace / ".brigade" / "memory-care.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text)

    def wire(self, hub):
        self.adapter.memory.push_search = hub
        return hub

    def digest(self):
        self.adapter.hub.fleet_digest = lambda owner: {
            "inbox": {"oldest": [{"subject": "hub token rotation"}]},
            "fleet_work_next": {"item": {"title": "Rotate the hub token"}},
        }

    def test_both_responses_carry_one_memory_field_with_their_own_bound(self):
        self.hub_mode()
        # A local card that would match: hub mode never pushes it.
        (self.cards / "hub-token-rotation.md").write_text("---\ntitle: Hub token rotation\n---\nLOCAL-CARD\n")
        hub = self.wire(FakeHub())
        self.messages.add(subject="hub token rotation", body="steps?")
        listed = self.adapter.call(EMAIL, "inbox_list", {})
        self.digest()
        digest = self.adapter.call(EMAIL, "fleet_digest", {})
        assert [call[1] for call in hub.calls] == [3, 5]
        assert hub.calls[1][0] == "hub token rotation Rotate the hub token"
        for result, limit in ((listed, 3), (digest, 5)):
            assert [key for key in result if "memory" in key] == ["memory_hits"]
            assert len(result["memory_hits"]["hits"]) == limit
            assert all(hit["source"] == "hub" for hit in result["memory_hits"]["hits"])
            encoded = json.dumps(result)
            assert "LOCAL-CARD" not in encoded and "memory/cards/" not in encoded
            assert encoded_size(result["memory_hits"]) <= mp.MAX_HITS_BYTES
        assert not (self.workspace / ".brigade" / "memory" / "search-log.jsonl").exists()

    def test_hub_drops_are_logged_as_counts_and_rule_ids_only(self):
        self.hub_mode()
        placeholder = {"card_id": "card-9", "blocked": "home-path", "code": PRIVATE_CODE}
        self.wire(FakeHub([placeholder, _hit(1, summary=PRIVATE), _hit(2)]))
        self.messages.add(subject="hub token rotation", body="steps?")
        result = self.adapter.call(EMAIL, "inbox_list", {})
        assert [hit["card_id"] for hit in result["memory_hits"]["hits"]] == ["card-2"]
        with self.state.transaction() as connection:
            rows = connection.execute("SELECT ok, arg_summary FROM call_log WHERE tool = 'memory_push_scan'").fetchall()
        assert [tuple(row) for row in rows] == [(1, "dropped=2 rules=home-path")]

    def test_an_unavailable_or_unreadable_hub_leaves_the_response_unchanged(self):
        (self.cards / "hub-token-rotation.md").write_text("---\ntitle: Hub token rotation\n---\nLOCAL-CARD\n")
        self.messages.add(subject="hub token rotation", body="steps?")
        self.hub_mode()
        self.wire(FakeHub(error=memory_hub_client.HubUnavailable("timeout")))
        assert "memory_hits" not in self.adapter.call(EMAIL, "inbox_list", {})
        self.hub_mode('[hub]\nmode = "everything"\n')
        assert "memory_hits" not in self.adapter.call(EMAIL, "inbox_list", {})
