"""BM25 card search and prompt-driven recall without prompt text on disk (#2196)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from brigade import config, memory_bm25, memory_cmd, memory_hooks, memory_recall_log
from brigade.claude_hooks import runtime
from brigade.install import install_selection
from brigade.memory_retrieval_eval.harness import run_eval
from brigade.selection import Selection

# A token that appears nowhere else, so any copy of the prompt on disk is findable.
SENTINEL = "zqxsentinelprompt4471"


def _card(root: Path, name: str, title: str, body: str, tags: list[str] | None = None) -> None:
    cards = root / "memory" / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    tag_line = f"tags: [{', '.join(tags)}]\n" if tags else ""
    (cards / name).write_text(f"---\ntitle: {title}\n{tag_line}---\n{body}\n", encoding="utf-8")


def _search_log(root: Path) -> list[dict]:
    path = root / ".brigade" / "memory" / "search-log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _tree_files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and needle.encode() in path.read_bytes():
            hits.append(path.relative_to(root).as_posix())
    return hits


# --- ranker ---


def test_rare_term_outweighs_common_term():
    texts = [f"common filler words number {i}" for i in range(8)] + ["common rare"]
    ranked = memory_bm25.rank(texts, "common rare")
    assert ranked[0][0] == 8
    assert len(ranked) == 9
    # The old scorer gave every "common" card the same credit as the rare hit.
    assert ranked[0][1] > 2 * ranked[1][1]


def test_length_normalization_prefers_the_shorter_card():
    short = "restic backup"
    long = "restic backup " + " ".join(f"pad{i}" for i in range(40))
    ranked = memory_bm25.rank([long, short, "unrelated text"], "restic")
    assert [doc for doc, _ in ranked] == [1, 0]


def test_scores_are_floats_rounded_to_four_decimals():
    ranked = memory_bm25.rank(["alpha beta gamma", "alpha", "delta"], "alpha")
    for _, score in ranked:
        assert isinstance(score, float)
        assert score == round(score, 4)


def test_tokens_split_on_punctuation_and_never_match_inside_words():
    assert memory_bm25.tokenize("OpenClaw-gateway/v2.1 token_refresh") == [
        "openclaw",
        "gateway",
        "v2",
        "1",
        "token_refresh",
    ]
    assert memory_bm25.rank(["the gateway restarted"], "gate") == []


def test_repeated_query_terms_count_once():
    texts = ["restic snapshot", "restic", "snapshot"]
    assert memory_bm25.rank(texts, "restic restic snapshot") == memory_bm25.rank(texts, "restic snapshot")


def test_index_is_built_from_scratch_every_call():
    texts = ["restic backup"]
    first = memory_bm25.rank(texts, "restic")
    texts.append("restic restic restic")
    assert memory_bm25.rank(texts, "restic") != first


def test_query_without_word_characters_keeps_literal_list_all(tmp_path: Path):
    _card(tmp_path, "a.md", "Alpha", "alpha body")
    _card(tmp_path, "b.md", "Beta", "beta body")
    payload = memory_cmd.search_cards_payload(tmp_path, ":")
    assert payload["match_count"] == 2
    assert {match["score"] for match in payload["matches"]} == {0.0}
    assert memory_cmd.search_cards_payload(tmp_path, "   ")["match_count"] == 0


def test_search_payload_shape_is_unchanged_and_score_is_float(tmp_path: Path):
    _card(tmp_path, "net.md", "Networking", "vlan trunking notes", tags=["network"])
    payload = memory_cmd.search_cards_payload(tmp_path, "vlan")
    assert set(payload) == {"target", "query", "match_count", "matches"}
    (match,) = payload["matches"]
    assert set(match) == {"path", "card_id", "card_aliases", "title", "tags", "summary", "score"}
    assert isinstance(match["score"], float) and match["score"] > 0


def test_search_text_output_prints_score_to_two_decimals(tmp_path: Path, capsys):
    _card(tmp_path, "net.md", "Networking", "vlan trunking notes")
    assert memory_cmd.search(target=tmp_path, query="vlan") == 0
    line = capsys.readouterr().out.splitlines()[1]
    score = line.split("[", 1)[1].split("]", 1)[0]
    assert len(score.split(".")[1]) == 2
    float(score)


# --- retrieval eval ---


def test_eval_reports_bm25_beside_current_and_does_not_regress_fixture_recall():
    report = run_eval(adapters=["current", "bm25", "substring", "grep"])
    adapters = report["adapters"]
    assert set(adapters) == {"current", "bm25", "substring", "grep"}
    current = adapters["current"]["overall"]["recall_at_k"]
    # current is production search, which is BM25: the direct adapter must agree.
    assert adapters["bm25"]["overall"] == adapters["current"]["overall"]
    # Overall metrics ignore order inside the top k, so compare each query's ranking too.
    assert [q["ranked"] for q in adapters["bm25"]["queries"]] == [q["ranked"] for q in adapters["current"]["queries"]]
    assert current >= adapters["substring"]["overall"]["recall_at_k"]


def test_bm25_eval_adapter_keeps_the_literal_fallback_for_punctuation_queries():
    from brigade.memory_retrieval_eval.adapters import bm25_adapter
    from brigade.memory_retrieval_eval.corpus import CardDoc

    def card(card_id: str, body: str) -> CardDoc:
        return CardDoc(
            card_id=card_id, path=f"memory/cards/{card_id}.md", title=card_id, tags=(), summary="", body=body
        )

    search = bm25_adapter([card("colon", "key: value"), card("plain", "no punctuation")])
    assert search(":", limit=5) == [("colon", 0.0)]


# --- recall query source and private search log ---


def test_recall_without_prompt_is_labeled_cwd(tmp_path: Path):
    hub = tmp_path / "hub"
    session = tmp_path / "comet-tools"
    session.mkdir()
    _card(hub, "comet.md", "Comet tools", "comet notes")
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session)
    assert payload["query_source"] == "cwd"
    assert payload["query"] == "comet tools"
    assert _search_log(hub) == []


def test_prompt_recall_searches_the_prompt_and_logs_only_a_keyed_hash(tmp_path: Path):
    hub = tmp_path / "hub"
    session = tmp_path / "unrelated-folder"
    session.mkdir()
    _card(hub, "comet.md", "Comet token rotation", "rotate the comet token monthly")
    _card(hub, "folder.md", "Unrelated folder", "folder notes")
    prompt = f"  how do I rotate the comet token {SENTINEL}\n"
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=session, query=prompt, query_source="prompt")
    assert payload["query_source"] == "prompt"
    assert payload["matches"][0]["title"] == "Comet token rotation"
    assert "folder.md" not in json.dumps(payload["matches"])

    (entry,) = _search_log(hub)
    assert set(entry) == {"ts", "query_hmac", "query_source", "card_ids", "card_aliases"}
    assert entry["query_source"] == "prompt"
    assert entry["card_ids"] == [match["card_id"] for match in payload["matches"]]
    normalized = " ".join(prompt.lower().split())
    assert entry["query_hmac"] != hashlib.sha256(normalized.encode()).hexdigest()
    key = memory_recall_log.load_or_create_key(hub)
    assert key is not None
    assert entry["query_hmac"] == memory_recall_log.query_digest(key, prompt.upper())
    assert _tree_files_containing(hub, SENTINEL) == []
    if sys.platform != "win32":
        key_mode = stat.S_IMODE(os.stat(memory_recall_log.key_path(hub)).st_mode)
        assert key_mode == 0o600


def test_explicit_memory_search_still_logs_the_raw_query(tmp_path: Path, capsys):
    _card(tmp_path, "comet.md", "Comet guide", "comet body")
    assert memory_cmd.search(target=tmp_path, query="Comet guide", json_output=True) == 0
    capsys.readouterr()
    (entry,) = _search_log(tmp_path)
    assert entry["query"] == "comet guide"
    assert "query_hmac" not in entry
    assert not memory_recall_log.key_path(tmp_path).exists()


def test_corrupt_key_skips_the_log_and_keeps_recall(tmp_path: Path):
    hub = tmp_path / "hub"
    _card(hub, "comet.md", "Comet guide", "comet body")
    key = memory_recall_log.key_path(hub)
    key.parent.mkdir(parents=True)
    key.write_text("not hex\n", encoding="utf-8")
    payload = memory_hooks.recall_cards_payload(target=hub, cwd=tmp_path, query="comet", query_source="prompt")
    assert payload["matches"]
    assert _search_log(hub) == []


# --- UserPromptSubmit hook, end to end ---


@pytest.fixture
def wired_with_hub(tmp_path: Path) -> tuple[Path, Path]:
    target = tmp_path / "repo"
    assert install_selection(target, Selection(depth="repo", harnesses=["claude"], owner="claude", includes=[])) == 0
    hub = tmp_path / "hub"
    _card(hub, "comet.md", "Comet token rotation", "rotate the comet token monthly", tags=["comet"])
    cfg = config.load_config(target)
    assert cfg is not None
    config.write_config(target, dataclasses.replace(cfg, memory_recall_target=str(hub)))
    return target, hub


def _prompt_payload(target: Path, prompt: str) -> dict:
    return {
        "session_id": "session-prompt",
        "cwd": str(target),
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def test_user_prompt_submit_recalls_from_prompt_without_writing_it(tmp_path, wired_with_hub, monkeypatch):
    target, hub = wired_with_hub
    monkeypatch.setattr(runtime, "_run_brief", lambda _repo: "work brief: test")
    result = runtime.handle_payload(
        "UserPromptSubmit", _prompt_payload(target, f"How should I rotate the comet token? {SENTINEL}")
    )

    assert result is not None
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "memory recall (from your prompt):" in context
    assert "Comet token rotation" in context
    assert "rotate the comet token monthly" not in context
    assert SENTINEL not in json.dumps(result)

    (entry,) = _search_log(hub)
    assert entry["query_source"] == "prompt"
    assert entry["card_ids"]
    assert "query" not in entry

    injections = list((target / ".brigade" / "work" / "claude-hooks" / "injections").glob("*"))
    assert injections, "the hook output file is where a leaked prompt would land"
    for root in (target, hub):
        assert _tree_files_containing(root, SENTINEL) == []


def test_user_prompt_submit_without_recall_match_injects_nothing(wired_with_hub):
    target, hub = wired_with_hub
    assert runtime.handle_payload("UserPromptSubmit", _prompt_payload(target, f"{SENTINEL} nothing here")) is None
    assert _tree_files_containing(target, SENTINEL) == []
    assert _tree_files_containing(hub, SENTINEL) == []


def test_vocabulary_build_scores_match_a_full_build():
    texts = ["restic backup nightly", "restic", "backup offsite copy", "unrelated words only"]
    full = memory_bm25.Bm25Index.build(texts)
    partial = memory_bm25.Bm25Index.build(texts, vocabulary={"restic", "backup"})
    assert partial.scores("restic backup") == full.scores("restic backup")
    assert set(partial.postings) == {"restic", "backup"}


def test_control_only_prompt_recalls_nothing_and_skips_the_cwd_fallback(wired_with_hub, monkeypatch):
    target, _hub = wired_with_hub
    calls: list[dict] = []
    monkeypatch.setattr(memory_hooks, "recall_cards_payload", lambda **kwargs: calls.append(kwargs) or {})
    assert memory_hooks.recall_text_for_hook(wired_target=target, prompt="\x00\x01 \t") == ""
    assert calls == []


def test_prompt_recall_keeps_the_key_out_of_git(tmp_path: Path):
    hub = tmp_path / "hub"
    _card(hub, "comet.md", "Comet token rotation", "rotate the comet token")
    memory_hooks.recall_cards_payload(target=hub, cwd=tmp_path, query="comet token", query_source="prompt")
    key = memory_recall_log.key_path(hub)
    assert key.is_file()
    assert (key.parent / ".gitignore").read_text(encoding="utf-8") == f"/{key.name}\n"


def test_key_creation_falls_back_when_hard_links_are_unsupported(tmp_path: Path, monkeypatch):
    def no_links(_src, _dst):
        raise OSError("hard links not supported")

    monkeypatch.setattr(memory_recall_log.os, "link", no_links)
    hub = tmp_path / "hub"
    key = memory_recall_log.load_or_create_key(hub)
    assert key is not None and len(key) == 32
    assert memory_recall_log.load_or_create_key(hub) == key
    assert [p.name for p in memory_recall_log.key_path(hub).parent.iterdir() if p.name.endswith(".tmp")] == []


def test_search_log_append_fails_open_while_another_writer_holds_the_lock(tmp_path: Path):
    from brigade.inbox_lock import held_file_lock

    log = tmp_path / memory_cmd.SEARCH_LOG_REL
    log.parent.mkdir(parents=True)
    entry = {"ts": "2026-11-03T00:00:00Z", "query": "comet", "card_ids": []}
    with held_file_lock(log.with_suffix(".lock"), deadline_seconds=1.0):
        memory_cmd._append_search_log_entry(tmp_path, entry)
    assert not log.exists()
    memory_cmd._append_search_log_entry(tmp_path, entry)
    assert [row["query"] for row in _search_log(tmp_path)] == ["comet"]
