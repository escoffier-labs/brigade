"""Command Deck overview renderers over synthetic DeckView objects."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import fleet_command_deck as deck
from brigade import fleet_deck_overview as overview
from brigade import fleet_deck_shell

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_C = "33333333-3333-4333-8333-333333333333"
HOSTILE = "<script>alert(1)</script>"


def _stamp(offset_seconds: int) -> str:
    return (NOW - timedelta(seconds=offset_seconds)).isoformat()


def _machine(machine_id: str, node_id: str, **overrides) -> deck.MachineState:
    fields = {
        "id": machine_id,
        "node_id": node_id,
        "os": "linux",
        "state": "available",
        "last_observed_at": _stamp(60),
        "load": "0.44",
        "capacity": "10",
        "active_claims": "0",
        "available_slots": "8",
        "unused_reason": "idle-capacity",
    }
    fields.update(overrides)
    return deck.MachineState(**fields)


def _run(node_id: str, run_id: str, repo: str, **overrides) -> deck.LiveRun:
    fields = {
        "node_id": node_id,
        "run_id": run_id,
        "repo": repo,
        "seat": "seat-a",
        "harness": "codex",
        "state": "run.started",
        "bucket": "running",
        "age_seconds": 5,
        "elapsed_seconds": 125,
    }
    fields.update(overrides)
    return deck.LiveRun(**fields)


def _station(node_id: str, label: str, capacity: int, runs=(), *, enrolled=True, heard=60) -> deck.StationView:
    tiles = tuple(deck.Tile(run=run, claim=None, collision=False) for run in runs)
    return deck.StationView(
        station=deck.StationConfig(node_id, label, capacity),
        label=label,
        enrolled=enrolled,
        busy=len(tiles),
        last_heard=None if heard is None else _stamp(heard),
        tiles=tiles,
    )


def _plane(machines=(), *, queue=None, present=("policy", "machines", "queue")) -> deck.ControlPlane:
    return deck.ControlPlane(
        machines=tuple(machines),
        queue=queue or deck.QueueState(eligible_count="3", queued_count="3", blocked_known=True),
        present=frozenset(present),
    )


def _view(stations=(), *, plane=None, **fields) -> deck.DeckView:
    defaults = {"rail": (), "repos": (), "outcomes": (), "observers": ()}
    defaults.update(fields)
    return deck.DeckView(stations=tuple(stations), control_plane=plane, **defaults)


def _mixed_view() -> deck.DeckView:
    """linux-a working with fresh telemetry, linux-b stale, win-a not enrolled."""
    stations = (
        _station(NODE_A, "linux-a", 10, [_run(NODE_A, "r1", "skillet"), _run(NODE_A, "r2", "canary-repo")]),
        _station(NODE_B, "linux-b", 8),
        _station(NODE_C, "win-a", 4, enrolled=False, heard=None),
    )
    machines = (
        _machine("linux-a", NODE_A),
        _machine(
            "linux-b",
            NODE_B,
            state="unknown",
            last_observed_at=_stamp(7200),
            available_slots="unknown",
            unused_reason="stale-telemetry",
        ),
        _machine("win-a", NODE_C, os="windows"),
    )
    return _view(stations, plane=_plane(machines))


# --- format_age and collapse_outcomes ------------------------------------------------


def test_format_age_boundaries():
    cases = {
        0: "0s",
        11: "11s",
        59: "59s",
        60: "1m",
        240: "4m",
        3599: "59m",
        3600: "1h 0m",
        4380: "1h 13m",
        86399: "23h 59m",
        86400: "1d 0h",
        40 * 86400 + 7200: "40d 2h",
    }
    for seconds, expected in cases.items():
        assert overview.format_age(seconds) == expected, seconds
    assert overview.format_age(-5) == "0s"
    assert overview.format_age(None) == deck.UNKNOWN


def test_seconds_since_reads_z_and_naive_stamps_and_rejects_junk():
    assert overview.seconds_since("2026-10-18T11:59:00Z", NOW) == 60
    assert overview.seconds_since("2026-10-18T11:59:00", NOW) == 60
    assert overview.seconds_since("2026-10-18T12:05:00+00:00", NOW) == 0
    for junk in (None, "", deck.UNKNOWN, "not a time"):
        assert overview.seconds_since(junk, NOW) is None


def test_collapse_outcomes_merges_only_adjacent_identical_rows():
    done_a = _run(NODE_A, "o1", "skillet", state="run.completed")
    done_a2 = _run(NODE_A, "o2", "skillet", state="run.completed")
    failed = _run(NODE_A, "o3", "skillet", state="run.failed")
    done_other_node = _run(NODE_B, "o4", "skillet", state="run.completed")
    done_again = _run(NODE_A, "o5", "skillet", state="run.completed")
    rows = overview.collapse_outcomes([done_a, done_a2, failed, done_other_node, done_again, done_again])
    assert [(run.run_id, count) for run, count in rows] == [("o1", 2), ("o3", 1), ("o4", 1), ("o5", 2)]
    assert overview.collapse_outcomes([]) == ()


# --- machine join and station status ---------------------------------------------------


def test_machine_join_uses_node_id_never_the_label():
    station = _station(NODE_A, "linux-a", 4)
    matching = _machine("policy-name", NODE_A)
    same_label = _machine("linux-a", NODE_B)
    no_node = _machine("linux-a", deck.UNKNOWN)
    assert overview.machine_for(_view([station], plane=_plane([same_label, no_node, matching])), station) is matching
    assert overview.machine_for(_view([station], plane=_plane([same_label, no_node])), station) is None
    assert overview.machine_for(_view([station]), station) is None


def test_station_status_precedence():
    fresh = _machine("m", NODE_A)
    stale = _machine("m", NODE_A, unused_reason="stale-telemetry")
    never = _machine("m", NODE_A, unused_reason="unknown-telemetry")
    gone = _machine("m", NODE_A, state="offline")
    busy = _station(NODE_A, "a", 2, [_run(NODE_A, "r", "x")])
    idle = _station(NODE_A, "a", 2)
    unenrolled = _station(NODE_A, "a", 2, enrolled=False)
    assert overview.station_status(busy, fresh) == "working"
    assert overview.station_status(idle, fresh) == "idle"
    assert overview.station_status(idle, None) == "unknown"
    assert overview.station_status(idle, stale) == "stale"
    assert overview.station_status(busy, never) == "stale"
    assert overview.station_status(idle, gone) == "offline"
    assert overview.station_status(unenrolled, stale) == "offline"


# --- summary cards ------------------------------------------------------------------


def _card_value(html: str, name: str) -> str:
    card = re.search(rf'data-card="{name}">(.*?)</article>', html, re.S)
    assert card, name
    value = re.search(r'class="card-value">([^<]*)<', card.group(1))
    assert value, name
    return value.group(1)


def test_cards_count_busy_idle_stale_offline_and_not_enrolled():
    html = overview.render_summary(_mixed_view())
    assert _card_value(html, "working") == "2"
    assert "of 22 slots" in html
    assert '<progress class="bar" value="2" max="22"' in html
    assert _card_value(html, "idle") == "8"
    assert _card_value(html, "offline") == "2"
    assert _card_value(html, "queued") == "3"


def test_cards_say_unknown_without_a_control_plane_and_working_still_counts():
    view = _view([_station(NODE_A, "linux-a", 4, [_run(NODE_A, "r1", "skillet")])])
    html = overview.render_summary(view)
    assert _card_value(html, "working") == "1"
    for name in ("idle", "offline", "queued"):
        assert _card_value(html, name) == "unknown", name


def test_queued_card_is_unknown_when_the_queue_section_is_absent_never_zero():
    machines = [_machine("linux-a", NODE_A)]
    absent = _view([_station(NODE_A, "linux-a", 4)], plane=_plane(machines, present=("machines",)))
    assert _card_value(overview.render_summary(absent), "queued") == "unknown"
    unreported = deck.QueueState(eligible_count=deck.UNKNOWN, queued_count=deck.UNKNOWN)
    unknown = _view([_station(NODE_A, "linux-a", 4)], plane=_plane(machines, queue=unreported))
    assert _card_value(overview.render_summary(unknown), "queued") == "unknown"
    zero = deck.QueueState(eligible_count="0", queued_count="0", blocked_known=True)
    empty = _view([_station(NODE_A, "linux-a", 4)], plane=_plane(machines, queue=zero))
    assert _card_value(overview.render_summary(empty), "queued") == "0"


def test_a_station_without_a_machine_row_adds_no_idle_slots():
    view = _view([_station(NODE_A, "linux-a", 4)], plane=_plane([_machine("linux-a", NODE_B)]))
    assert _card_value(overview.render_summary(view), "idle") == "0"
    assert _card_value(overview.render_summary(view), "offline") == "0"


def test_no_stations_renders_zero_working_without_a_bar():
    html = overview.render_summary(_view())
    assert _card_value(html, "working") == "0"
    assert "<progress" not in html


# --- alerts ----------------------------------------------------------------------------


def _alert_view() -> deck.DeckView:
    rail = (
        deck.RailEntry("failed", NODE_A, "skillet", "r1", "external.failed", 7200),
        deck.RailEntry("unknown", NODE_B, "canary-repo", "r2", "external.unknown", 86400),
        deck.RailEntry("collision", "linux-a", "shared", "", "held by linux-a, live on shared"),
    )
    plane = deck.ControlPlane(
        machines=(
            _machine("linux-a", NODE_A),
            _machine(
                "linux-b", NODE_B, state="unknown", last_observed_at=_stamp(3600), unused_reason="stale-telemetry"
            ),
        ),
        integrations=(
            deck.IntegrationState(id="routine-one", routine_state="needs-attention", detail="away prompt pending"),
        ),
        queue=deck.QueueState(
            eligible_count="2",
            queued_count="5",
            blocked=({"id": "job-1", "reason": "no eligible machine"},),
            blocked_known=True,
        ),
        present=frozenset({"machines", "queue", "integrations"}),
    )
    stations = (_station(NODE_A, "linux-a", 4), _station(NODE_B, "linux-b", 4))
    return _view(stations, plane=plane, rail=rail)


def test_alerts_come_in_order_attention_collision_stale_coverage():
    alerts = overview.page_alerts(_alert_view(), NOW)
    assert [alert.kind for alert in alerts] == [
        "failed",
        "unknown",
        "integration",
        "blocked",
        "collision",
        "stale telemetry",
        "coverage",
    ]
    by_kind = {alert.kind: alert for alert in alerts}
    assert by_kind["failed"].title == "skillet on linux-a"
    assert by_kind["failed"].since == "first observed 2h 0m ago"
    assert by_kind["unknown"].since == "first observed 1d 0h ago"
    assert by_kind["collision"].title == "shared"
    assert by_kind["collision"].detail == "held by linux-a, live on shared"
    assert by_kind["collision"].since == "first observed unknown"
    assert by_kind["integration"].detail == "routine needs-attention · away prompt pending"
    assert by_kind["blocked"].title == "job-1" and by_kind["blocked"].detail == "no eligible machine"
    assert by_kind["stale telemetry"].title == "linux-b"
    assert by_kind["stale telemetry"].since == "last telemetry 1h 0m ago"
    assert by_kind["coverage"].since == "ongoing"
    assert by_kind["coverage"].detail.startswith("no telemetry for: ")


def test_attention_and_collision_alerts_match_the_attention_items_count():
    view = _alert_view()
    alerts = overview.page_alerts(view, NOW)
    counted = [alert for alert in alerts if alert.kind not in {"stale telemetry", "coverage"}]
    assert len(counted) == len(deck.attention_items(view))


@pytest.mark.parametrize("identified", [False, True])
@pytest.mark.parametrize("reason", ["not-ready", "blocker", "attempt-limit"])
def test_queue_exclusions_preserve_actionable_aggregates_and_identified_blocks(identified, reason):
    blocked = [{"reason": reason, "count": 3}]
    if identified:
        blocked.append({"id": "job-1", "reason": "not-ready"})
    plane = deck.control_plane_from_snapshot(
        {
            "policy": {},
            "sessions": [],
            "machines": [],
            "quota": [],
            "routes": [],
            "queue": {"eligible_count": 2, "queued_count": 2, "blocked": blocked},
            "integrations": [],
            "authority": {"active": True, "status": "active"},
        }
    )
    view = _view(plane=plane)
    actionable = reason in {"blocker", "attempt-limit"}
    count = int(actionable) + int(identified)
    expected = f"NEEDS ATTENTION ({count})" if count else "ALL CLEAR"
    assert deck.deck_verdict(view) == overview.verdict_text(view, NOW) == expected
    alerts = overview.page_alerts(view, NOW)
    expected_alerts = [("blocked", "job-1")] if identified else []
    if actionable:
        expected_alerts.append(("blocked", "3 work items blocked"))
    assert [(item.kind, item.title) for item in alerts] == expected_alerts
    if actionable:
        assert alerts[-1].detail == reason
    html = deck.render_deck(view, nonce="nonce", now=NOW)
    assert f"Active alerts ({count})" in html
    assert "Queue exclusions" in html and 'href="/deck/work"' in html
    assert f"3 excluded · {reason}" in html
    assert 'class="row-title">unknown<' not in html
    assert "none blocked" not in html
    details = deck.render_control_plane(plane)
    assert "3 excluded" in details
    assert "No blocked reasons reported" not in details
    assert f"unknown &middot; {reason}" not in details
    if not identified:
        assert "first observed unknown" not in html


@pytest.mark.parametrize("count", [True, False, None, "3", 0, -1, 1.5])
@pytest.mark.parametrize("reason", ["not-ready", "blocker", "attempt-limit", "unrecognized"])
def test_malformed_aggregate_blocks_are_not_silently_cleared(count, reason):
    queue = deck.QueueState(
        eligible_count="0", queued_count="0", blocked=({"reason": reason, "count": count},), blocked_known=True
    )
    view = _view(plane=_plane(queue=queue))
    assert deck.deck_verdict(view) == overview.verdict_text(view, NOW) == "NEEDS ATTENTION (1)"
    assert [(alert.kind, alert.detail) for alert in overview.page_alerts(view, NOW) if alert.kind == "blocked"] == [
        ("blocked", reason)
    ]


@pytest.mark.parametrize("reason", ["unrecognized", " blocker ", "not-ready\n", None, [], {}])
def test_unknown_aggregate_reason_is_not_silently_cleared(reason):
    queue = deck.QueueState(
        eligible_count="0", queued_count="0", blocked=({"reason": reason, "count": 3},), blocked_known=True
    )
    view = _view(plane=_plane(queue=queue))
    assert deck.deck_verdict(view) == overview.verdict_text(view, NOW) == "NEEDS ATTENTION (1)"
    assert len([alert for alert in overview.page_alerts(view, NOW) if alert.kind == "blocked"]) == 1


def test_missing_control_plane_is_an_alert_not_silence():
    alerts = overview.page_alerts(_view(), NOW)
    assert [(alert.kind, alert.title) for alert in alerts] == [("coverage", "Control-plane snapshot missing")]
    assert alerts[0].detail == "no telemetry for: control-plane telemetry"


def test_stale_machine_with_unreadable_observation_time_says_no_telemetry_received():
    plane = _plane([_machine("m", NODE_A, last_observed_at=deck.UNKNOWN, unused_reason="unknown-telemetry")])
    alerts = overview.page_alerts(_view([_station(NODE_A, "linux-a", 2)], plane=plane), NOW)
    stale = [alert for alert in alerts if alert.kind == "stale telemetry"]
    assert [alert.since for alert in stale] == ["no telemetry received"]


def test_unenrolled_station_is_counted_offline_but_raises_no_stale_alert():
    plane = _plane([_machine("m", NODE_A, unused_reason="stale-telemetry")])
    view = _view([_station(NODE_A, "linux-a", 2, enrolled=False)], plane=plane)
    assert [alert.kind for alert in overview.page_alerts(view, NOW) if alert.kind == "stale telemetry"] == []


def test_alert_section_renders_cards_count_and_empty_state():
    html = overview.render_alerts(_alert_view(), NOW)
    assert 'id="alerts"' in html and "Active alerts (7)" in html
    assert html.count('class="alert alert--') == 7
    assert 'class="alert-kind">collision<' in html
    assert "held by linux-a, live on shared" in html
    assert "first observed 2h 0m ago" in html
    assert "</section>" not in html.split('id="alerts"', 1)[1].rsplit("</section>", 1)[0]
    clear = deck.control_plane_from_snapshot(
        {
            "policy": {},
            "sessions": [],
            "machines": [],
            "quota": [],
            "quota_effective": [],
            "routes": [],
            "queue": {"eligible_count": 0, "queued_count": 0, "blocked": []},
            "integrations": [],
            "authority": {"active": True, "status": "active"},
        }
    )
    empty = overview.render_alerts(_view(plane=clear), NOW)
    assert "Active alerts (0)" in empty and "No active alerts" in empty and "<li" not in empty


def test_alerts_escape_hostile_text():
    rail = (deck.RailEntry("failed", NODE_A, HOSTILE, "r1", HOSTILE, 5),)
    view = _view([_station(NODE_A, HOSTILE, 2)], rail=rail)
    html = overview.render_alerts(view, NOW)
    assert HOSTILE not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- header and verdict ---------------------------------------------------------------


def test_header_counts_machines_slots_and_providers():
    view = _mixed_view()
    html = overview.render_header(view, NOW)
    assert "3 machines · 22 slots · 0 cloud providers" in html
    assert "Updated 12:00 UTC" in html
    single = overview.render_header(_view([_station(NODE_A, "linux-a", 1)]), NOW)
    assert "1 machine · 1 slot · 0 cloud providers" in single


def test_verdict_counts_attention_items_and_stale_telemetry_together():
    view = _mixed_view()
    assert overview.verdict_text(view, NOW) == "NEEDS ATTENTION (1)"
    entry = deck.RailEntry(kind="failed", node_id=NODE_A, repo="skillet", run_id="r9", detail="external.failed")
    with_failure = _view(view.stations, plane=view.control_plane, rail=(entry,))
    assert overview.verdict_text(with_failure, NOW) == "NEEDS ATTENTION (2)"
    assert 'verdict-status--attention">NEEDS ATTENTION (2)' in overview.render_header(with_failure, NOW)


def test_verdict_falls_back_to_the_deck_verdict_when_nothing_needs_attention():
    assert overview.verdict_text(_view(), NOW) == "COVERAGE UNKNOWN (1)"
    assert "ALL CLEAR" not in overview.render_header(_view(), NOW)
    full = deck.control_plane_from_snapshot(
        {
            "policy": {"version": "1"},
            "sessions": [],
            "machines": [],
            "quota": [],
            "quota_effective": [],
            "routes": [],
            "queue": {"eligible_count": 0, "queued_count": 0, "blocked": []},
            "integrations": [],
            "authority": {"active": True, "status": "active"},
        }
    )
    assert overview.verdict_text(_view(plane=full), NOW) == "ALL CLEAR"
    assert "verdict-status--clear" in overview.render_header(_view(plane=full), NOW)


def test_an_enrolled_station_missing_from_the_machines_snapshot_is_never_all_clear():
    full = deck.control_plane_from_snapshot(
        {
            "policy": {"version": "1"},
            "sessions": [],
            "machines": [],
            "quota": [],
            "quota_effective": [],
            "routes": [],
            "queue": {"eligible_count": 0, "queued_count": 0, "blocked": []},
            "integrations": [],
            "authority": {"active": True, "status": "active"},
        }
    )
    view = _view([_station(NODE_A, "linux-a", 4)], plane=full)
    assert overview.verdict_text(view, NOW) == "COVERAGE UNKNOWN (1)"
    alerts = [alert for alert in overview.page_alerts(view, NOW) if alert.kind == "no telemetry row"]
    assert [alert.title for alert in alerts] == ["linux-a"]
    assert "no telemetry row" in overview.render_alerts(view, NOW)
    not_enrolled = _view([_station(NODE_A, "linux-a", 4, enrolled=False)], plane=full)
    assert overview.verdict_text(not_enrolled, NOW) == "ALL CLEAR"


# --- working now and queued -----------------------------------------------------------------


def test_stale_and_unknown_working_rows_link_to_the_attention_board():
    for bucket in ("stale", "unknown"):
        run = _run(NODE_A, "r1", "skillet", bucket=bucket)
        html = overview.render_work_lists(_view([_station(NODE_A, "linux-a", 4, [run])]))
        assert "repo=skillet&amp;attention=1" in html, bucket
    running = _run(NODE_A, "r2", "skillet")
    html = overview.render_work_lists(_view([_station(NODE_A, "linux-a", 4, [running])]))
    assert "attention=1" not in html


def test_working_rows_show_repo_badge_seat_machine_elapsed_and_a_board_link():
    html = overview.render_work_lists(_mixed_view())
    assert "Working now (2)" in html
    assert html.count('class="row"') == 2
    assert 'href="/view/machines?node=11111111-1111-4111-8111-111111111111&amp;repo=skillet"' in html
    assert "seat-a · linux-a" in html
    assert 'class="row-time">2m<' in html
    assert 'class="badge ' in html


def test_working_row_flags_collisions_and_non_running_buckets():
    run = _run(NODE_A, "r1", "shared", bucket="queued", seat="")
    claim = deck.Claim(target="shared", owner_node="linux-a", owner_conductor="conductor-1", ttl_remaining=60)
    station = deck.StationView(
        station=deck.StationConfig(NODE_A, "linux-a", 2),
        label="linux-a",
        enrolled=True,
        busy=1,
        last_heard=None,
        tiles=(deck.Tile(run=run, claim=claim, collision=True),),
    )
    html = overview.render_work_lists(_view([station]))
    assert "! collision" in html
    assert 'class="chip chip-muted">queued<' in html
    assert "conductor-1 · linux-a" in html


def test_working_list_empty_state_and_hostile_names_are_inert():
    assert "Nothing is running." in overview.render_work_lists(_view([_station(NODE_A, "linux-a", 2)]))
    hostile = _station(NODE_A, HOSTILE, 2, [_run(NODE_A, "r1", HOSTILE, seat=HOSTILE)])
    html = overview.render_work_lists(_view([hostile]))
    assert HOSTILE not in html and "&lt;script&gt;" in html


def test_queued_panel_states():
    unknown = overview.render_work_lists(_view())
    assert "Queue unknown" in unknown
    zero = deck.QueueState(eligible_count="0", queued_count="0", blocked_known=True)
    assert "Nothing queued." in overview.render_work_lists(_view(plane=_plane(queue=zero)))
    positive = deck.QueueState(eligible_count="2", queued_count="2", blocked_known=True)
    assert "2 queued, none blocked." in overview.render_work_lists(_view(plane=_plane(queue=positive)))
    unsure = deck.QueueState(eligible_count="2", queued_count="2", blocked_known=False)
    assert "2 queued, blocked reasons unknown." in overview.render_work_lists(_view(plane=_plane(queue=unsure)))
    unreported = deck.QueueState(eligible_count=deck.UNKNOWN, queued_count=deck.UNKNOWN, blocked_known=True)
    assert "Queued items unknown." in overview.render_work_lists(_view(plane=_plane(queue=unreported)))
    blocked = deck.QueueState(
        eligible_count="1",
        queued_count="1",
        blocked=({"id": "job-1", "reason": HOSTILE},),
        blocked_known=True,
    )
    html = overview.render_work_lists(_view(plane=_plane(queue=blocked)))
    assert "job-1" in html and "blocked · &lt;script&gt;" in html and HOSTILE not in html


# --- machines table ---------------------------------------------------------------------------


def _rows(html: str) -> list[str]:
    return re.findall(r"<tr data-status=.*?</tr>", html, re.S)


def test_machine_table_joins_on_node_id_and_shows_each_column():
    rows = _rows(overview.render_machine_table(_mixed_view(), NOW))
    assert len(rows) == 3
    first, second, third = rows
    assert "dot--working" in first and 'title="11111111-1111-4111-8111-111111111111">linux-a<' in first
    assert "2 busy, 8 idle" in first and "44%" in first and ">0<" in first
    assert "available" in first and "idle-capacity" in first
    assert ">1m<" in first and "last run 1m" in first
    assert "dot--stale" in second and "stale-telemetry" in second and "2h 0m" in second
    assert "dot--offline" in third and "not enrolled" in third and "last run never" in third


def test_missing_node_id_gives_unknown_cells_not_a_label_match():
    plane = _plane([_machine("linux-a", deck.UNKNOWN)])
    html = overview.render_machine_table(_view([_station(NODE_A, "linux-a", 4)], plane=plane), NOW)
    (row,) = _rows(html)
    assert "44%" not in row and "idle-capacity" not in row
    assert row.count(">unknown<") >= 3
    assert "last run 1m" in row


def test_seat_bar_text_equals_its_segments():
    for capacity, busy in ((10, 2), (4, 4), (16, 0), (3, 5)):
        runs = [_run(NODE_A, f"r{index}", "x") for index in range(busy)]
        station = _station(NODE_A, "linux-a", capacity, runs)
        plane = _plane([_machine("linux-a", NODE_A, available_slots=str(capacity))])
        html = overview.render_machine_table(_view([station], plane=plane), NOW)
        idle = max(capacity - busy, 0)
        assert f"{busy} busy, {idle} idle" in html
        assert html.count("seg--busy") == min(busy, capacity)
        assert html.count("seg--free") == idle


def test_stale_and_offline_machines_show_unavailable_seats_not_idle():
    rows = _rows(overview.render_machine_table(_mixed_view(), NOW))
    stale, offline = rows[1], rows[2]
    for row in (stale, offline):
        assert " idle<" not in row and "seg--free" not in row
        assert "seg--off" in row and " unavailable<" in row
    capacity = int(re.search(r"(\d+) unavailable<", stale).group(1))
    assert stale.count("seg--off") == capacity


def test_wide_machines_scale_to_a_fixed_number_of_segments():
    station = _station(NODE_A, "linux-a", 17, [_run(NODE_A, "r1", "x"), _run(NODE_A, "r2", "x")])
    plane = _plane([_machine("linux-a", NODE_A, available_slots="15")])
    html = overview.render_machine_table(_view([station], plane=plane), NOW)
    assert "seats-wide" not in html and '<progress class="seats' not in html
    assert html.count('class="seg ') == overview.SEGMENT_LIMIT
    assert html.count("seg--busy") >= 1
    assert "seats--scaled" in html and "2 busy, 15 idle" in html


def _seat_view(machine_slots: str, runs: int = 2, capacity: int = 10, **machine_fields) -> deck.DeckView:
    station = _station(NODE_A, "linux-a", capacity, [_run(NODE_A, f"r{i}", "x") for i in range(runs)])
    machine = _machine("linux-a", NODE_A, available_slots=machine_slots, **machine_fields)
    return _view([station], plane=_plane([machine]))


def test_free_seats_takes_the_smaller_of_capacity_and_reported_slots():
    station = _station(NODE_A, "linux-a", 10, [_run(NODE_A, "r1", "x"), _run(NODE_A, "r2", "x")])
    assert overview.free_seats(station, None) == 8
    assert overview.free_seats(station, _machine("m", NODE_A, available_slots="10")) == 8
    assert overview.free_seats(station, _machine("m", NODE_A, available_slots="6")) == 6
    assert overview.free_seats(station, _machine("m", NODE_A, available_slots="unknown")) == 8
    over = _station(NODE_A, "linux-a", 2, [_run(NODE_A, f"r{i}", "x") for i in range(3)])
    assert overview.free_seats(over, None) == 0


def test_card_and_row_agree_when_the_hub_reports_every_slot_free():
    view = _seat_view("10")
    assert _card_value(overview.render_summary(view), "idle") == "8"
    (row,) = _rows(overview.render_machine_table(view, NOW))
    assert "2 busy, 8 idle" in row and "claimed" not in row


def test_card_and_row_agree_when_claims_hold_seats_the_station_cannot_see():
    view = _seat_view("6")
    assert _card_value(overview.render_summary(view), "idle") == "6"
    (row,) = _rows(overview.render_machine_table(view, NOW))
    assert "2 busy, 6 idle, 2 claimed" in row
    assert row.count("seg--busy") == 2 and row.count("seg--free") == 6 and row.count("seg--off") == 2


def test_idle_card_equals_the_sum_of_idle_seats_in_the_machine_rows():
    view = _mixed_view()
    rows = _rows(overview.render_machine_table(view, NOW))
    idle = sum(int(m.group(1)) for row in rows for m in re.finditer(r'seat-text">[^<]*?(\d+) idle', row))
    assert idle > 0
    assert _card_value(overview.render_summary(view), "idle") == str(idle)


def test_station_status_marks_fresh_non_available_machines_limited():
    idle = _station(NODE_A, "a", 4)
    for state in ("busy", "draining", "unavailable"):
        assert overview.station_status(idle, _machine("m", NODE_A, state=state)) == "limited", state
    assert overview.station_status(idle, _machine("m", NODE_A, state=deck.UNKNOWN)) == "idle"
    assert overview.station_status(idle, _machine("m", NODE_A, state="offline")) == "offline"
    stale = _machine("m", NODE_A, state="draining", unused_reason="stale-telemetry")
    assert overview.station_status(idle, stale) == "stale"


def test_a_draining_machine_is_unavailable_and_counts_toward_neither_card():
    station = _station(NODE_A, "linux-a", 4)
    plane = _plane([_machine("linux-a", NODE_A, state="draining", available_slots="4")])
    view = _view([station], plane=plane)
    summary = overview.render_summary(view)
    assert _card_value(summary, "idle") == "0" and _card_value(summary, "offline") == "0"
    (row,) = _rows(overview.render_machine_table(view, NOW))
    assert 'data-status="limited"' in row and "dot--limited" in row
    assert "4 unavailable" in row and " idle<" not in row and "seg--free" not in row
    assert row.count("seg--off") == 4
    sidebar = overview.render_sidebar(view, "overview", NOW, worklore_enabled=True)
    assert 'class="tree-meta">draining<' in sidebar and "dot--limited" in sidebar


def test_the_limited_summary_escapes_the_machine_state():
    plane = _plane([_machine("linux-a", NODE_A, state=HOSTILE)])
    html = overview.render_sidebar(
        _view([_station(NODE_A, "linux-a", 2)], plane=plane), "overview", NOW, worklore_enabled=True
    )
    assert HOSTILE not in html and "&lt;script&gt;" in html


def test_wide_machines_scale_unavailable_seats_to_off_segments():
    stale = _machine("linux-b", NODE_B, state="unknown", unused_reason="stale-telemetry", available_slots="unknown")
    view = _view([_station(NODE_B, "linux-b", 64)], plane=_plane([stale]))
    (row,) = _rows(overview.render_machine_table(view, NOW))
    assert row.count("seg--off") == overview.SEGMENT_LIMIT and "64 unavailable" in row
    assert "seg--free" not in row and '<progress class="seats-wide' not in row
    fresh = _view([_station(NODE_A, "linux-a", 64)], plane=_plane([_machine("linux-a", NODE_A, available_slots="64")]))
    fresh_row = overview.render_machine_table(fresh, NOW)
    assert "seg--off" not in fresh_row and fresh_row.count("seg--free") == overview.SEGMENT_LIMIT


def test_wide_machines_with_claimed_seats_keep_an_off_segment():
    runs = [_run(NODE_A, "r1", "x"), _run(NODE_A, "r2", "x")]
    view = _view(
        [_station(NODE_A, "linux-a", 20, runs)], plane=_plane([_machine("linux-a", NODE_A, available_slots="10")])
    )
    (row,) = _rows(overview.render_machine_table(view, NOW))
    assert "2 busy, 10 idle, 8 claimed" in row
    assert row.count('class="seg ') == overview.SEGMENT_LIMIT
    assert row.count("seg--busy") >= 1 and row.count("seg--off") >= 1


def test_scaled_segments_sum_to_the_limit_and_keep_every_present_category():
    for counts, capacity in (((2, 15, 0), 17), ((0, 60, 4), 64), ((64, 0, 0), 64), ((1, 0, 63), 64), ((1, 1, 62), 64)):
        busy, free, off = overview.scaled_segments(counts, capacity)
        assert busy + free + off == overview.SEGMENT_LIMIT, counts
        for count, units in zip(counts, (busy, free, off), strict=True):
            assert (units >= 1) == (count > 0), counts
    assert overview.scaled_segments((64, 0, 0), 64) == (16, 0, 0)
    assert overview.scaled_segments((2, 15, 0), 17)[0] >= 1
    assert overview.scaled_segments((0, 60, 4), 64)[2] >= 1
    assert overview.scaled_segments((1, 0, 63), 64)[0] == 1


def test_an_enrolled_station_without_a_machine_is_unknown_not_idle():
    station = _station(NODE_A, "linux-a", 4)
    assert overview.station_status(station, None) == "unknown"
    gone = _station(NODE_C, "win-a", 4, enrolled=False, heard=None)
    assert overview.station_status(gone, None) == "offline"


def test_unmatched_station_row_is_unknown_and_adds_no_idle_or_offline():
    stations = (_station(NODE_A, "linux-a", 4), _station(NODE_B, "linux-b", 8))
    view = _view(stations, plane=_plane([_machine("linux-b", NODE_B, available_slots="8")]))
    rows = _rows(overview.render_machine_table(view, NOW))
    unmatched = rows[0]
    assert 'data-status="unknown"' in unmatched and "dot--unknown" in unmatched
    assert "4 unknown" in unmatched and "seg--free" not in unmatched and "seg--busy" not in unmatched
    assert unmatched.count("seg--off") == 4 and " idle<" not in unmatched
    idle = sum(int(m.group(1)) for row in rows for m in re.finditer(r'seat-text">[^<]*?(\d+) idle', row))
    summary = overview.render_summary(view)
    assert idle == 8 and _card_value(summary, "idle") == str(idle)
    assert _card_value(summary, "offline") == "0"
    sidebar = overview.render_sidebar(view, "overview", NOW, worklore_enabled=True)
    assert 'class="tree-meta">unknown<' in sidebar and "dot--unknown" in sidebar


def test_unknown_station_with_busy_runs_reports_busy_and_unknown_seats():
    station = _station(NODE_A, "linux-a", 4, [_run(NODE_A, "r1", "x")])
    (row,) = _rows(overview.render_machine_table(_view([station]), NOW))
    assert "1 busy, 3 unknown" in row
    assert row.count("seg--busy") == 1 and row.count("seg--off") == 3 and "seg--free" not in row


def test_load_cell_handles_unparseable_and_out_of_range_values():
    def load_of(value: str) -> str:
        plane = _plane([_machine("m", NODE_A, load=value)])
        (row,) = _rows(overview.render_machine_table(_view([_station(NODE_A, "a", 2)], plane=plane), NOW))
        return row

    assert "100%" in load_of("1") and "0%" in load_of("0")
    assert "100%" in load_of("7.5")
    for junk in ("unknown", "nan", "inf", ""):
        assert '<progress class="bar bar--thin"' not in load_of(junk), junk


def test_machine_table_empty_state_and_escaping():
    assert "No stations configured" in overview.render_machine_table(_view(), NOW)
    plane = _plane([_machine(HOSTILE, NODE_A, os=HOSTILE, unused_reason=HOSTILE, state=HOSTILE)])
    html = overview.render_machine_table(_view([_station(NODE_A, HOSTILE, 2)], plane=plane), NOW)
    assert HOSTILE not in html and "&lt;script&gt;" in html


# --- below the fold ----------------------------------------------------------------------------


def test_cloud_workers_render_compact_cards_and_empty_state():
    worker = deck.CloudWorker(
        provider="cursor",
        used=1,
        limit=3,
        circuit_state="closed",
        leases=(deck.CloudLease("task-1", "example/tool", "label one", "running", "https://example.test/a"),),
    )
    html = overview.render_cloud_workers(_view(cloud_workers=(worker,)))
    assert " cursor</h3>" in html and ">1/3<" in html and "circuit: closed" in html
    assert "label one" in html and "example/tool" in html
    assert "No cloud providers configured." in overview.render_cloud_workers(_view())


def test_sessions_table_keeps_the_path_in_a_tooltip_only():
    session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="claude",
        session_id="sess-1",
        repo_identity="github.com/example/tool",
        identity_scope="fleet",
        repo_label="tool",
        checkout_path=HOSTILE,
        branch="main",
        dirty_paths=("a.py", "b.py"),
        dirty_truncated=True,
    )
    html = overview.render_sessions(_view(interactive_sessions=(session,)))
    assert "1 session(s)" in html and "claude/sess-1" in html and ">main<" in html and ">2+<" in html
    assert 'title="&lt;script&gt;alert(1)&lt;/script&gt;"' in html
    assert html.count("&lt;script&gt;") == 1
    assert "No interactive sessions." in overview.render_sessions(_view())


def test_outcomes_collapse_adjacent_duplicates_and_label_the_node():
    outcomes = (
        _run(NODE_A, "o1", "skillet", state="run.completed"),
        _run(NODE_A, "o2", "skillet", state="run.completed"),
        _run(NODE_B, "o3", "skillet", state="run.failed"),
    )
    view = _view([_station(NODE_A, "linux-a", 2)], outcomes=outcomes)
    html = overview.render_outcomes(view)
    assert "3 shown" in html
    assert html.count("run.completed") == 1 and "<span>×2</span>" in html
    assert "skillet · linux-a" in html and "skillet · 22222222-222" in html
    assert "No recent outcomes." in overview.render_outcomes(_view())


def test_observed_groups_and_observers_fold_away():
    group = deck.ObservedWorkGroup(
        key="group-one",
        label=HOSTILE,
        coverage="partial",
        source_ref="source",
        proxy_ref="proxy",
        parent_ref="parent",
        work_ids=("w-1", "w-2"),
    )
    view = _view(observed_work_groups=(group,), observers=((NODE_C, _stamp(30)),))
    groups = overview.render_observed_groups(view, NOW)
    assert groups.startswith("<details><summary>Observed work groups (1)</summary>")
    assert 'href="/deck/observed/group-one"' in groups and HOSTILE not in groups
    assert "2 configured observation records" in groups
    observers = overview.render_observers(view, NOW)
    assert "Other observers (1)" in observers and "33333333-333" in observers and "heard 30s ago" in observers


def test_overview_body_orders_sections_and_folds_control_plane_details():
    html = overview.render_overview(_mixed_view(), NOW, worklore_enabled=True)
    order = [
        'class="page-head"',
        'aria-label="Fleet summary"',
        'id="alerts"',
        'aria-labelledby="working-title"',
        'id="machines-table"',
        'id="cloud-workers"',
        'id="interactive-sessions"',
        'id="timeline"',
        "<summary>Control plane details</summary>",
        "<summary>Other observers (0)</summary>",
        "<footer>",
    ]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert html.count("<main") == 1 and "<nav" not in html
    assert "Observed work groups" not in html


def test_observed_groups_hidden_when_worklore_is_off():
    group = deck.ObservedWorkGroup("g", "Group", "c", "s", "p", "q", ("w-1",))
    view = _view(observed_work_groups=(group,))
    assert "Observed work groups" in overview.render_overview(view, NOW, worklore_enabled=True)
    assert "Observed work groups" not in overview.render_overview(view, NOW, worklore_enabled=False)


# --- sidebar and shell -------------------------------------------------------------------------


def test_sidebar_nav_marks_the_active_page_and_hides_work_when_off():
    view = _mixed_view()
    html = overview.render_sidebar(view, "repos", NOW, worklore_enabled=False)
    assert '<nav class="deck-nav" aria-label="Command Deck">' in html
    assert '<a href="/deck/repos" aria-current="page">repos</a>' in html
    assert '<a href="/deck">overview <span class="nav-count">3</span></a>' in html
    assert html.count("aria-current") == 1
    assert "/deck/work" not in html
    for href in ("/deck/roster", "/deck/policy", "/view/machines"):
        assert f'href="{href}"' in html
    assert 'href="/deck/work"' in overview.render_sidebar(view, "overview", NOW, worklore_enabled=True)


def test_sidebar_machine_tree_shows_dots_summaries_children_and_cloud():
    cloud = (
        deck.CloudWorker("claude", 0, 4, "closed", ()),
        deck.CloudWorker("cursor", 1, 3, "open", ()),
    )
    view = _view(_mixed_view().stations, plane=_mixed_view().control_plane, cloud_workers=cloud)
    html = overview.render_sidebar(view, "overview", NOW, worklore_enabled=True)
    assert "data-tree-filter" in html and "<input" not in html
    assert 'Machines <span class="nav-count">3</span>' in html
    assert 'class="tree-meta">2 busy<' in html
    assert 'class="tree-meta">stale<' in html and 'class="tree-meta">not enrolled<' in html
    assert "skillet · seat-a" in html and "canary-repo · seat-a" in html and ">2m<" in html
    assert 'Cloud <span class="nav-count">2</span>' in html
    assert 'class="tree-meta">0/4<' in html and 'class="tree-meta">1/3 · open<' in html
    assert html.count("data-filter-row") == 5
    assert "Hub telemetry 1m ago" in html


def test_sidebar_without_a_view_is_navigation_only():
    html = overview.render_sidebar(None, "roster", NOW, worklore_enabled=False)
    assert "tree-list" not in html and "data-tree-filter" not in html and "Hub telemetry" not in html
    assert '<a href="/deck/roster" aria-current="page">roster</a>' in html
    assert '<a href="/deck">overview</a>' in html


def test_sidebar_footer_says_unknown_without_telemetry_and_escapes_labels():
    html = overview.render_sidebar(_view([_station(NODE_A, HOSTILE, 2)]), "overview", NOW, worklore_enabled=True)
    assert "Hub telemetry unknown" in html
    assert HOSTILE not in html and "&lt;script&gt;" in html


def test_shell_wraps_sidebar_and_body_and_ships_a_constant_filter_script():
    page = fleet_deck_shell.deck_page(
        "Title",
        "roster",
        '<aside class="shell-sidebar">side</aside>',
        "<main>body</main>",
        "n0nce",
        as_of="2026-10-18 12:00:00 UTC",
        refresh_seconds=None,
    )
    assert '<div class="shell" data-active="roster"><aside class="shell-sidebar">side</aside>' in page
    assert '<div class="shell-main"><main>body</main></div></div>' in page
    assert 'http-equiv="refresh"' not in page
    assert f'<script nonce="n0nce">{fleet_deck_shell.FILTER_SCRIPT}</script>' in page
    script = fleet_deck_shell.FILTER_SCRIPT
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "location", "fetch("):
        assert banned not in script, banned
    assert "textContent" in script and "createElement('input')" in script


def test_filter_script_waits_for_the_body_and_keeps_focus_across_refresh():
    script = fleet_deck_shell.FILTER_SCRIPT
    assert "DOMContentLoaded" in script and "readyState" in script
    assert "deck-filter-focus" in script and "setSelectionRange" in script
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in script, banned
    refreshed = fleet_deck_shell.deck_page("t", "overview", "", "", "n", as_of="x")
    assert '<meta http-equiv="refresh" content="10">' in refreshed
    hostile = fleet_deck_shell.deck_page("t", '"><x>', "", "", "n", as_of="x")
    assert 'data-active="&quot;&gt;&lt;x&gt;"' in hostile


# --- the assembled page --------------------------------------------------------------------------


def test_render_deck_escapes_hostile_repo_in_sidebar_table_and_alerts():
    station = _station(NODE_A, "linux-a", 2, [_run(NODE_A, "r1", HOSTILE)])
    rail = (deck.RailEntry("failed", NODE_A, HOSTILE, "r9", HOSTILE, 5),)
    page = deck.render_deck(_view([station], rail=rail), nonce="n0nce", now=NOW)
    assert HOSTILE not in page
    assert page.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 3
    assert page.count("<script") == 1


def test_old_station_cards_and_script_are_gone():
    page = deck.render_deck(_mixed_view(), nonce="n0nce", now=NOW)
    # Markup only: the leftover CSS rules are removed and tested in Task 13.
    body = page.split("<body", 1)[1]
    for marker in ("station-card", "run-stack", "data-elapsed", "tickElapsed", "Needs you", "heard "):
        assert marker not in body, marker


def test_every_class_in_the_rendered_page_has_a_rule():
    group = deck.ObservedWorkGroup("g", "Group", "c", "s", "p", "q", ("w-1",))
    session = deck.InteractiveSession(NODE_A, "claude", "s1", "id", "fleet", "tool", "/p", "main", (), False)
    view = _view(
        _mixed_view().stations,
        plane=_mixed_view().control_plane,
        rail=_alert_view().rail,
        outcomes=(_run(NODE_A, "o1", "skillet", state="run.completed"),),
        observers=((NODE_C, _stamp(30)),),
        cloud_workers=(deck.CloudWorker("claude", 0, 4, "closed", ()),),
        interactive_sessions=(session,),
        observed_work_groups=(group,),
    )
    page = deck.render_deck(view, nonce="n0nce", now=NOW)
    css = re.search(r'<style nonce="n0nce">(.*?)</style>', page, re.S).group(1)
    used = {name for match in re.findall(r'class="([^"]*)"', page) for name in match.split()}

    def styled(name: str) -> bool:
        # A modifier such as alert--failed may rely on its base rule.
        return any(re.search(rf"\.{re.escape(base)}(?![\w-])", css) for base in (name, name.split("--")[0]))

    unstyled = sorted(name for name in used if not styled(name))
    assert unstyled == [], unstyled


def test_deck_modules_stay_inside_their_size_budgets():
    source = Path(deck.__file__).parent

    def lines(name: str) -> int:
        return len((source / name).read_text(encoding="utf-8").splitlines())

    assert lines("fleet_command_deck.py") < 1953
    assert lines("fleet_deck_overview.py") < 750
    assert lines("fleet_deck_shell.py") < 250


def test_the_station_card_code_is_gone_from_the_command_deck_module():
    for name in ("_tile_html", "_interactive_session_html", "_cloud_worker_html", "_empty_rail_text", "_SCRIPT"):
        assert not hasattr(deck, name), name
    assert not hasattr(deck, "heard_age")
    css = deck._STYLE
    for selector in (".station-card", ".stations", ".run-stack", ".dashboard-grid", ".attention-panel", ".capacity"):
        assert selector not in css, selector


def test_policy_disabled_and_draining_machines_are_limited_with_the_policy_word():
    for reason, word in (
        ("machine-disabled", "disabled"),
        ("machine-draining", "draining"),
        ("routing-disabled", "routing off"),
    ):
        machine = _machine("linux-a", NODE_A, state="available", unused_reason=reason, available_slots="0")
        station = _station(NODE_A, "linux-a", 4)
        view = _view([station], plane=_plane([machine]))
        assert overview.station_status(station, machine) == "limited"
        assert overview.summarize(view).idle == "0"
        rows = _rows(overview.render_machine_table(view, NOW))
        assert "4 unavailable" in rows[0]
        assert f">{word}<" in overview.render_sidebar(view, "overview", NOW, worklore_enabled=False)
