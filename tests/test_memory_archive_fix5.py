"""Hub read-stat paging and index line-boundary regressions."""

from datetime import date, timedelta

from brigade import memory_archive as archive
from brigade import memory_card_reads as reads
from brigade import memory_care_retire as retire
from brigade import memory_care_tend as tend
from brigade import memory_cmd
from brigade import memory_hub_client as hubc


def _row(card_id, day):
    return {"card_id": card_id, "reads_7d": 1, "reads_30d": 1, "total_reads": 1, "last_read_day": day}


def _hub(monkeypatch, pages, *, refusals=0, since=1, repeated_cursor=False):
    """A fake client that serves the given pages and refuses the first `refusals` page requests with 429."""
    calls = []
    left = [refusals]

    class Client:
        def call(self, action, **kwargs):
            if action == "status":
                return {"credential": "node"}
            calls.append(kwargs.get("after"))
            if left[0]:
                left[0] -= 1
                raise hubc.HubRefusal(429, {"code": "memory_read_stats_rate_limited"})
            index = 0 if kwargs.get("after") is None else int(kwargs["after"])
            following = str(index + 1) if index + 1 < len(pages) else None
            return {
                "scope": "fleet",
                "next_after": "0" if repeated_cursor else following,
                "read_counts_since_day": since,
                "rows": pages[index],
            }

    monkeypatch.setattr(hubc, "load_settings", lambda _: hubc.HubSettings(mode="sync", scope="fleet"))
    monkeypatch.setattr(hubc, "client_for", lambda _: Client())
    return calls


def test_paged_read_stats_are_paced_and_complete(tmp_path, monkeypatch):
    calls = _hub(monkeypatch, [[_row("a", 20000)], [_row("b", 20001)], [_row("c", 20002)]])
    waits = []
    result = reads.hub_last_reads(tmp_path, sleep=waits.append)
    assert result == {
        name: date(1970, 1, 1) + timedelta(days=day) for name, day in zip("abc", (20000, 20001, 20002), strict=True)
    }
    assert calls == [None, "1", "2"]
    # One pause before each continuation page, none before the first.
    assert waits == [reads.HUB_PAGE_GAP_SECONDS, reads.HUB_PAGE_GAP_SECONDS]


def test_rate_limited_page_is_retried(tmp_path, monkeypatch):
    _hub(monkeypatch, [[_row("a", 20000)]], refusals=2)
    waits = []
    assert reads.hub_last_reads(tmp_path, sleep=waits.append) == {"a": date(1970, 1, 1) + timedelta(days=20000)}
    assert len(waits) == 2


def test_unreadable_hub_stats_retire_nothing(tmp_path, monkeypatch, capsys):
    _hub(monkeypatch, [[_row("a", 20000)]], refusals=99)
    assert reads.hub_last_reads(tmp_path, sleep=lambda _: None) is None
    assert "unavailable" in capsys.readouterr().err
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ncard_id: old\nexpires_at: 2026-01-01\n---\nEnded\n")
    reads.record(tmp_path, [], today=date(2026, 1, 1))
    monkeypatch.setattr(reads, "hub_last_reads", lambda _, **_kw: None)
    plan = retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))
    assert plan["candidates"] == []
    assert plan["skipped"]["hub_reads_unavailable"] == 1
    assert path.exists()


def test_hub_not_configured_still_retires(tmp_path, monkeypatch):
    path = tmp_path / "memory/cards/old.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ncard_id: old\nexpires_at: 2026-01-01\n---\nEnded\n")
    reads.record(tmp_path, [], today=date(2026, 1, 1))
    assert reads.hub_last_reads(tmp_path) == {}
    plan = retire.plan(tmp_path, memory_cmd.MemoryCareConfig(), tend.TendConfig(), today=date(2026, 11, 12))
    assert len(plan["candidates"]) == 1


def test_undo_keeps_line_boundaries_for_an_unterminated_last_link(tmp_path):
    card = tmp_path / "memory/cards/old.md"
    card.parent.mkdir(parents=True)
    card.write_text("body\n")
    index = tmp_path / "MEMORY.md"
    index.write_text("Header\n- [Old](memory/cards/old.md)")
    d = archive.archive_card(tmp_path, "memory/cards/old.md", reason="ended", config=memory_cmd.MemoryCareConfig())
    assert index.read_text() == "Header\n"
    index.write_text("Header\n- [New](memory/cards/new.md)")
    archive.restore(tmp_path, d)
    lines = index.read_text().splitlines()
    assert sorted(lines[1:]) == ["- [New](memory/cards/new.md)", "- [Old](memory/cards/old.md)"]
    assert lines[0] == "Header"
