"""Retrieval protects cards, while bulk listings do not count as use."""

import os
from datetime import date, timedelta

import pytest

from brigade import memory_card_reads as reads, memory_hooks, memory_vault, obsidian_vault, dispatch_memory
from brigade import memory_care_retire as retire, memory_care_tend as tend, memory_cmd
from tests.test_memory_archive import setup_card
from tests.test_memory_archive_fix5 import _hub, _row
from tests.test_memory_vault_search import _write_vault_config
from tests.test_memory_vault_projection import _write_card
from tests.test_memory_mcp_core import stores, _tools, _card  # noqa: F401
from tests.test_hook_hub_recall import wired, _read  # noqa: F401


@pytest.mark.parametrize("source", ["cwd", "workspace"])
def test_checkout_recall_records_reads_without_search_log(tmp_path, source):
    setup_card(tmp_path)
    cwd = tmp_path if source == "workspace" else tmp_path / "unique-word"
    cwd.mkdir(exist_ok=True)
    payload = memory_hooks.recall_cards_payload(target=tmp_path, cwd=cwd)
    if source == "workspace":
        # Workspace recall uses a fixed query, so give it a matching card.
        (tmp_path / "memory/cards/old.md").write_text("---\ncard_id: old\ntitle: workspace\n---\nworkspace\n")
        payload = memory_hooks.recall_cards_payload(target=tmp_path, cwd=cwd)
    assert payload["matches"]
    assert reads.load(tmp_path).last_read
    assert not (tmp_path / ".brigade/memory/search-log.jsonl").exists()


def test_read_tool_records_in_owning_memory_root(wired):  # noqa: F811
    target, memory = wired
    assert _read(target, memory / "memory/cards/comet.md")
    assert reads.last_read(reads.load(memory), ["memory/cards/comet.md"]) == date.today()
    assert reads.load(target).last_read == {}


def test_dispatch_local_context_records_reads(tmp_path):
    setup_card(tmp_path)
    assert dispatch_memory._local_matches(tmp_path, "unique-word")
    assert reads.last_read(reads.load(tmp_path), ["memory/cards/old.md"]) == date.today()


@pytest.mark.skipif(os.name == "nt", reason="vault containment checks are unavailable on Windows")
@pytest.mark.parametrize("operation", ["search", "show"])
def test_vault_projection_reads_protect_canonical_card(tmp_path, operation, capsys):
    _write_card(tmp_path, "alpha", title="Alpha", body="rotation policy", tags=["rotation"])
    vault = tmp_path / "vault"
    vault.mkdir()
    assert obsidian_vault.project(target=tmp_path, vault=vault, json_output=True) == 0
    capsys.readouterr()
    _write_vault_config(tmp_path, vault, [("memory", "Brigade Memory", False)])
    memory_vault.build_index(tmp_path)
    hits = memory_vault.search_payload(tmp_path, "rotation")["hits"]
    assert hits
    if operation == "show":
        (tmp_path / reads.READS_REL).unlink(missing_ok=True)
        memory_vault.show_payload(tmp_path, hits[0]["id"])
    assert reads.last_read(reads.load(tmp_path), ["memory/cards/alpha.md"]) == date.today()


def test_listings_record_nothing(stores):  # noqa: F811
    repo, user = stores
    _card(repo, "old.md", "Old", "content")
    tools = _tools(repo)
    assert tools.call("list_cards", {})[0]
    assert tools.resources()
    assert reads.load(repo).last_read == {}
    assert reads.load(user).last_read == {}


@pytest.mark.parametrize("problem", ["young", "malformed", "repeat"])
def test_incomplete_hub_stats_are_not_nonuse(tmp_path, monkeypatch, problem):
    # Feed wire replies through the real hub client validator. Large integer days
    # pass its nonnegative-count schema but cannot be represented as Python dates.
    day = 10**12 if problem == "malformed" else 20000
    since = (date.today() - date(1970, 1, 1)).days if problem == "young" else 1
    _hub(monkeypatch, [[_row("a", day)]], since=since, repeated_cursor=problem == "repeat")
    assert reads.hub_last_reads(tmp_path, sleep=lambda _: None) is None


def test_no_expiry_is_never_candidate_however_long_unread(tmp_path):
    setup_card(tmp_path)
    reads.record(tmp_path, [], today=date.today() - timedelta(days=1000))
    assert not retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date.today())["candidates"]


def test_local_read_retries_lock_once_with_longer_wait(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from brigade import inbox_lock

    original = inbox_lock.held_file_lock
    deadlines = []

    @contextmanager
    def contended(path, *, deadline_seconds):
        deadlines.append(deadline_seconds)
        if len(deadlines) == 1:
            raise inbox_lock.InboxLockTimeout("busy")
        with original(path, deadline_seconds=deadline_seconds):
            yield

    monkeypatch.setattr(inbox_lock, "held_file_lock", contended)
    reads.record(tmp_path, iter(["seen"]))
    assert len(deadlines) == 2 and min(deadlines) > 1
    assert reads.last_read(reads.load(tmp_path), ["seen"]) == date.today()


@pytest.mark.parametrize("operation", ["dispatch", "content", "hook"])
def test_read_ledger_failure_does_not_fail_retrieval(tmp_path, monkeypatch, operation):
    attempts = []

    def broken(*args, **kwargs):
        attempts.append(1)
        raise OSError("ledger unavailable")

    setup_card(tmp_path)
    monkeypatch.setattr(reads.store, "write_json", broken)
    if operation == "dispatch":
        assert dispatch_memory._local_matches(tmp_path, "unique-word")
    elif operation == "hook":
        assert memory_hooks._recall_cards_payload_impl(target=tmp_path, cwd=tmp_path / "unique-word")["matches"]
    else:
        reads.record_content(tmp_path, "body", "memory/cards/old.md")
    assert len(attempts) == 2


def test_read_tool_ledger_failure_still_observes_card(wired, monkeypatch):  # noqa: F811
    target, memory = wired

    def broken(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(reads, "record", broken)
    assert _read(target, memory / "memory/cards/comet.md")
