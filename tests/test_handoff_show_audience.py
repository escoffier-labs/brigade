"""Worklore ``handoff show`` may use the S24 local recall audience, and no other dispatch path may."""

from __future__ import annotations

import json

import pytest

from brigade import dispatch_memory
from brigade.aboyeur import briefs
from brigade.claude_hooks import recall_audience
from brigade.cli import fleet_work_prepared_handoff as handoff_cli
from tests.test_dispatch_memory import (  # noqa: F401 - fixtures
    CONNECTOR,
    LOCAL,
    PRIVATE_SUMMARY,
    _assert_private_withheld,
    _isolated,
    _private_hub,
    hub_memory,
)
from tests.test_fleet_work_prepared_handoff_cli import hub, show  # noqa: F401 - the Worklore handoff fixture


def _memory_json(capsys) -> dict[str, object]:
    assert show("--json") == 0
    return json.loads(capsys.readouterr().out)["memory_context"]


def _granted(monkeypatch, value: object = recall_audience.LOCAL) -> None:
    monkeypatch.setattr(recall_audience, "audience", lambda: value)


def test_a_host_opted_in_to_local_recall_shows_the_local_packet(
    tmp_path,
    monkeypatch,
    capsys,
    hub,  # noqa: F811
    hub_memory,  # noqa: F811
):
    target = _private_hub(tmp_path, hub_memory)
    monkeypatch.chdir(target)
    _granted(monkeypatch)

    memory = _memory_json(capsys)

    assert memory["audience"] == LOCAL
    summaries = [item.get("summary") for item in memory["items"]]
    assert PRIVATE_SUMMARY in summaries

    assert show() == 0
    human = capsys.readouterr().out
    assert dispatch_memory.HEADING in human and PRIVATE_SUMMARY in human and "audience: local |" in human


@pytest.mark.parametrize("value", [recall_audience.CONNECTOR, "LOCAL", "local ", "", None, "remote"])
def test_anything_but_an_exact_local_answer_is_the_connector_audience(
    tmp_path,
    monkeypatch,
    capsys,
    hub,  # noqa: F811
    hub_memory,  # noqa: F811
    value,
):
    target = _private_hub(tmp_path, hub_memory)
    monkeypatch.chdir(target)
    _granted(monkeypatch, value)

    memory = _memory_json(capsys)

    assert memory["audience"] == CONNECTOR
    assert PRIVATE_SUMMARY not in json.dumps(memory)
    assert show() == 0
    _assert_private_withheld(capsys.readouterr().out)


def test_a_missing_marker_is_the_connector_audience(
    tmp_path,
    monkeypatch,
    capsys,
    hub,  # noqa: F811
    hub_memory,  # noqa: F811
):
    target = _private_hub(tmp_path, hub_memory)
    monkeypatch.chdir(target)
    monkeypatch.delenv(recall_audience.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(recall_audience.REMOTE_ENV, raising=False)
    assert not recall_audience.marker_path().exists()

    assert handoff_cli._audience() == CONNECTOR
    memory = _memory_json(capsys)

    assert memory["audience"] == CONNECTOR
    assert PRIVATE_SUMMARY not in json.dumps(memory)


def test_an_unreadable_marker_is_the_connector_audience(monkeypatch):
    monkeypatch.delenv(recall_audience.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(recall_audience.REMOTE_ENV, raising=False)
    path = recall_audience.marker_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert recall_audience.marker_problem() is not None
    assert handoff_cli._audience() == CONNECTOR


def test_a_marker_reader_that_raises_is_the_connector_audience(monkeypatch):
    def broken() -> str:
        raise RuntimeError("marker reader failed")

    monkeypatch.setattr(recall_audience, "audience", broken)

    assert handoff_cli._audience() == CONNECTOR


def _opt_in(monkeypatch) -> None:
    """Write a real S24 marker in the isolated Brigade home, or skip where this host cannot."""
    monkeypatch.delenv(recall_audience.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(recall_audience.REMOTE_ENV, raising=False)
    refusal = recall_audience.write_marker()
    if refusal is not None:
        pytest.skip(f"this host cannot opt in to local recall: {refusal}")
    assert recall_audience.audience() == recall_audience.LOCAL


def test_a_real_marker_grants_local_to_handoff_show(
    tmp_path,
    monkeypatch,
    capsys,
    hub,  # noqa: F811
    hub_memory,  # noqa: F811
):
    target = _private_hub(tmp_path, hub_memory)
    monkeypatch.chdir(target)
    _opt_in(monkeypatch)

    assert _memory_json(capsys)["audience"] == LOCAL

    # The environment can only narrow: a remote session gets connector on the same host.
    monkeypatch.setenv(recall_audience.REMOTE_ENV, "1")
    assert _memory_json(capsys)["audience"] == CONNECTOR


@pytest.mark.parametrize("route", ["default", "adapter", "aboyeur", "native-t3-child"])
def test_other_dispatch_paths_stay_connector_on_an_opted_in_host(tmp_path, monkeypatch, hub_memory, route):  # noqa: F811
    target = _private_hub(tmp_path, hub_memory)
    _opt_in(monkeypatch)

    if route == "default":
        packet = dispatch_memory.build(target, "hub deploy")
        assert packet is not None and packet.audience == CONNECTOR
        text = packet.block
    elif route == "adapter":
        text = dispatch_memory.connector_text(target, "hub deploy")
    elif route == "aboyeur":
        text = briefs.with_memory_context(briefs.EvidenceBrief(attached=False), target, "hub deploy").text
    elif route == "native-t3-child":
        built = dispatch_memory.block_text(target, "hub deploy")
        text = dispatch_memory.for_remote(f"Sub-task header\n{built}\nDo the work.")

    _assert_private_withheld(text)
