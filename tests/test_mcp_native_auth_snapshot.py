"""#2694 review round: one snapshot per config file, an honest report, and no row for unwritten items.

Real code paths in a scratch HOME. The only patches wrap a reader or the planner to edit the live
file at a chosen moment, which is how a concurrent edit is simulated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, mcp_apply_snapshot, mcp_cmd
from tests._home import set_home
from tests.test_mcp_native_auth import (
    HELPER,
    URL,
    _claude_servers,
    _import_claude,
    _set_catalog_fields,
    _set_catalog_url,
    _sync,
    _write_claude,
)

REASON = "native_auth_helper_precedence"
BEARER_VAR = "MARKER_SNAP_VAR_6d02"
MARKERS = (HELPER, "MARKER_HELPER_7f3a", "/opt/fake/helper", BEARER_VAR)
PLAIN = {"docs": {"type": "http", "url": URL}}
WITH_HELPER = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert mcp_cmd.init(target=repo, json_output=True) == 0
    return repo, home


def _rows(payload, harness="claude-user"):
    return [
        f
        for r in payload["fidelity"]["servers"]
        if r["harness"] == harness and r["server"] == "docs"
        for f in r["fields"]
        if f["reason"] == REASON
    ]


def _item(payload):
    return next(i for i in payload["items"] if i["server"] == "docs")


def _edit_after_first_snapshot(monkeypatch, path, servers):
    """The next snapshot of ``path`` is taken, then the live file is edited (a concurrent change)."""
    real, fired = mcp_apply_snapshot.read_snapshot, []

    def wrapped(target):
        result = real(target)
        if target == path and not fired:
            fired.append(1)
            _write_claude(path.parent, servers)
        return result

    monkeypatch.setattr(mcp_apply_snapshot, "read_snapshot", wrapped)
    return fired


def _edit_after_plan(monkeypatch, home, servers):
    """The live file is edited right after the planner read it and before the apply snapshot."""
    real, fired = mcp_cmd._plan_for_harness, []

    def wrapped(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(1)
            _write_claude(home, servers)
        return result

    monkeypatch.setattr(mcp_cmd, "_plan_for_harness", wrapped)
    return fired


# --------------------------------------------------------------------------- #
# 1. One snapshot: an edit after it is drift, never a silent overwrite
# --------------------------------------------------------------------------- #


def test_sync_helper_added_after_the_snapshot_is_drift_and_survives(box, capsys, monkeypatch):
    repo, home = box
    path = _write_claude(home, PLAIN)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    fired = _edit_after_first_snapshot(monkeypatch, path, WITH_HELPER)
    sidecar = mcp_cmd.state_path(repo)
    state_before = sidecar.read_bytes() if sidecar.is_file() else None

    rc, out, payload = _sync(capsys, repo, force=False)

    assert fired and rc == 2 and payload["errors"]
    assert _claude_servers(path) == WITH_HELPER  # the helper is still there, byte for byte as edited
    assert (sidecar.read_bytes() if sidecar.is_file() else None) == state_before
    assert BEARER_VAR not in out and "/opt/fake/helper" not in out


def test_profile_helper_added_after_the_snapshot_is_drift_and_survives(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert mcp_cmd.init(target=ws, json_output=True) == 0
    mcp_cmd.canonical_path(ws).write_text(
        json.dumps({"servers": {"docs": {"transport": "http", "url": URL, "bearer_token_env_var": BEARER_VAR}}})
    )
    path = _write_claude(home, PLAIN)
    fired = _edit_after_first_snapshot(monkeypatch, path, WITH_HELPER)

    capsys.readouterr()
    rc = harness_profile_cmd.sync(harness="claude", workspace=ws, home=home, json_output=True, write=True, adopt=True)
    out = capsys.readouterr()
    text = out.out + out.err

    assert fired and rc != 0
    assert _claude_servers(path) == WITH_HELPER
    assert BEARER_VAR not in text and "/opt/fake/helper" not in text


def test_profile_uninstall_helper_added_after_the_snapshot_keeps_the_entry(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert mcp_cmd.init(target=ws, json_output=True) == 0
    mcp_cmd.canonical_path(ws).write_text(json.dumps({"servers": {"docs": {"transport": "http", "url": URL}}}))
    assert harness_profile_cmd.sync(harness="claude", workspace=ws, home=home, json_output=True, write=True) == 0
    path = home / ".claude.json"
    mcp_cmd.canonical_path(ws).write_text(json.dumps({"servers": {}}))
    fired = _edit_after_first_snapshot(monkeypatch, path, WITH_HELPER)

    capsys.readouterr()
    rc = harness_profile_cmd.sync(harness="claude", workspace=ws, home=home, json_output=True, write=True)
    capsys.readouterr()

    assert fired and rc != 0
    assert _claude_servers(path) == WITH_HELPER


# --------------------------------------------------------------------------- #
# 2. The report describes what was written when the live entry moved after the plan
# --------------------------------------------------------------------------- #


def test_current_item_whose_helper_vanished_is_a_conflict_not_a_skip_with_a_precedence_row(box, capsys, monkeypatch):
    repo, home = box
    path = _write_claude(home, WITH_HELPER)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    assert _sync(capsys, repo, force=False)[0] == 0  # converged: current, helper wins
    _edit_after_plan(monkeypatch, home, PLAIN)  # the helper is removed after the planner saw it
    before = path.read_bytes()

    rc, out, payload = _sync(capsys, repo, adopt=False, force=False)

    assert rc == 1
    item = _item(payload)
    assert (item["status"], item["action"], item["reason"]) == ("conflicted", "conflict", "live_entry_changed")
    assert _rows(payload) == []
    assert any(
        f["blocking"] and f["reason"] == "live_entry_changed"
        for r in payload["fidelity"]["servers"]
        for f in r["fields"]
    )
    assert _claude_servers(path) == PLAIN and path.read_bytes() != before  # only the simulated edit, no write by sync
    assert BEARER_VAR not in out


def test_update_item_gains_its_precedence_row_when_a_helper_appeared_after_the_plan(box, capsys, monkeypatch):
    repo, home = box
    path = _write_claude(home, PLAIN)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    _edit_after_plan(monkeypatch, home, WITH_HELPER)  # the planner saw no helper; the apply sees one

    rc, out, payload = _sync(capsys, repo, adopt=True, force=False)

    assert rc == 0
    assert _item(payload)["action"] == "update"
    assert [(r["field"], r["blocking"]) for r in _rows(payload)] == [("bearer_token_env_var", False)]
    assert _claude_servers(path) == WITH_HELPER  # the helper wins and is kept
    assert BEARER_VAR not in out


def test_update_item_loses_its_precedence_row_when_the_helper_vanished_after_the_plan(box, capsys, monkeypatch):
    repo, home = box
    path = _write_claude(home, WITH_HELPER)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    assert _sync(capsys, repo, force=False)[0] == 0
    _set_catalog_url(repo, "docs", URL + "/v2")  # same origin: a planned update that keeps the helper
    _edit_after_plan(monkeypatch, home, PLAIN)  # then the helper disappears before the apply

    rc, out, payload = _sync(capsys, repo, adopt=False, force=False)

    assert rc == 0
    assert _item(payload)["action"] == "update"
    assert _rows(payload) == []  # nothing is omitted any more: the catalog auth was written
    written = _claude_servers(path)["docs"]
    assert "headersHelper" not in written and written["headers"] == {"Authorization": "Bearer ${" + BEARER_VAR + "}"}


# --------------------------------------------------------------------------- #
# 3. No precedence row for an item that is not written
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adopt", [False, True], ids=["not-adopted", "adopted"])
def test_unowned_entry_without_adopt_has_no_precedence_row(box, capsys, adopt):
    repo, home = box
    live = {"docs": {**WITH_HELPER["docs"], "extra": "x"}}  # differs from the projection, so it is not current
    path = _write_claude(home, live)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    rc, out, payload = _sync(capsys, repo, adopt=adopt, force=False, write=False)
    item = _item(payload)
    if adopt:
        assert (item["status"], item["action"]) == ("adopted", "update") and len(_rows(payload)) == 1
    else:
        assert (item["status"], item["action"]) == ("foreign", "conflict")
        assert _rows(payload) == []
        assert REASON not in out
    assert _claude_servers(path) == live


def test_item_blocked_for_another_reason_has_no_precedence_row(box, capsys):
    repo, home = box
    _write_claude(home, WITH_HELPER)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", bearer_token_env_var=BEARER_VAR)
    _set_catalog_url(repo, "docs", "https://other.example.net/v1")
    rc, out, payload = _sync(capsys, repo, adopt=True, force=False)
    assert rc == 1 and _item(payload)["reason"] == "native_auth_origin_changed"
    assert _rows(payload) == []
