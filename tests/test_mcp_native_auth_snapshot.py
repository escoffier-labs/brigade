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


# --------------------------------------------------------------------------- #
# Final review: no second read in profile removal, and drift names the MCP config
# --------------------------------------------------------------------------- #

_PROFILE_MODULES = ("brigade.harness_profile_cmd", "brigade.mcp_cmd", "brigade.mcp_apply_snapshot")


def _pbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert mcp_cmd.init(target=ws, json_output=True) == 0
    return ws, home


def _profile(fn, ws, home, capsys, **kw):
    capsys.readouterr()
    rc = fn(harness="claude", workspace=ws, home=home, json_output=True, **kw)
    out = capsys.readouterr()
    return rc, json.loads(out.out), out.out + out.err


def _forbid_reads_after_snapshot(monkeypatch, path):
    """Once ``read_snapshot`` has read ``path``, any read of it from the profile layer fails the test.

    The projection kernel still checks the file against ``expected_before``; that is the drift check
    and is not a profile-layer read, so only frames in the profile modules count.
    """
    import sys

    armed, real_snapshot = [], mcp_apply_snapshot.read_snapshot

    def snapshot(target):
        result = real_snapshot(target)
        if target == path:
            armed.append(1)
        return result

    def guard(real):
        def wrapped(self, *args, **kwargs):
            if armed and self == path and sys._getframe(1).f_globals.get("__name__") in _PROFILE_MODULES:
                raise AssertionError(f"second read of {self.name} after the apply snapshot")
            return real(self, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(mcp_apply_snapshot, "read_snapshot", snapshot)
    for name in ("read_text", "read_bytes", "open"):
        monkeypatch.setattr(Path, name, guard(getattr(Path, name)))
    return armed


def _managed_docs(ws, home, capsys, *, helper):
    mcp_cmd.canonical_path(ws).write_text(json.dumps({"servers": {"docs": {"transport": "http", "url": URL}}}))
    rc, _, _ = _profile(harness_profile_cmd.sync, ws, home, capsys, write=True)
    assert rc == 0
    path = home / ".claude.json"
    if helper:
        _write_claude(home, WITH_HELPER)
    return path


def _ownership_kept(home):
    root = home / ".claude" / "brigade"
    return '"docs"' in "\n".join(p.read_text() for p in sorted(root.glob("*.json")))


@pytest.mark.parametrize("helper", [False, True], ids=["plain", "helper"])
@pytest.mark.parametrize("operation", ["catalog-removal", "uninstall"])
def test_profile_removal_never_rereads_the_config_after_the_snapshot(tmp_path, monkeypatch, capsys, operation, helper):
    ws, home = _pbox(tmp_path, monkeypatch)
    path = _managed_docs(ws, home, capsys, helper=helper)
    if operation == "catalog-removal":
        mcp_cmd.canonical_path(ws).write_text(json.dumps({"servers": {}}))
        fn = harness_profile_cmd.sync
    else:
        fn = harness_profile_cmd.uninstall
    armed = _forbid_reads_after_snapshot(monkeypatch, path)

    rc, payload, text = _profile(fn, ws, home, capsys, write=True)

    assert armed  # the guard was live for the removal run
    assert "/opt/fake/helper" not in text
    if helper:  # a helper-bearing entry is never removed and its ownership record is kept
        assert rc == 1 and _claude_servers(path) == WITH_HELPER and _ownership_kept(home)
    else:
        assert rc == 0 and "docs" not in _claude_servers(path)


def test_profile_drift_names_the_mcp_config_and_says_to_rerun(tmp_path, monkeypatch, capsys):
    ws, home = _pbox(tmp_path, monkeypatch)
    mcp_cmd.canonical_path(ws).write_text(
        json.dumps({"servers": {"docs": {"transport": "http", "url": URL, "bearer_token_env_var": BEARER_VAR}}})
    )
    path = _write_claude(home, PLAIN)
    real, fired = mcp_apply_snapshot.read_snapshot, []

    def one_byte_after_snapshot(target):
        result = real(target)
        if target == path and not fired:
            fired.append(1)
            data = bytearray(path.read_bytes())
            data[-1] = ord(" ")  # the trailing newline becomes a space: one byte, still valid JSON
            path.write_bytes(bytes(data))
        return result

    monkeypatch.setattr(mcp_apply_snapshot, "read_snapshot", one_byte_after_snapshot)

    rc, payload, text = _profile(harness_profile_cmd.sync, ws, home, capsys, write=True, adopt=True)

    assert fired and rc != 0
    conflict = payload["results"][0]["conflicts"][0]
    assert conflict["surface"] == "mcp" and conflict["path"] == str(path)
    assert "claude-user MCP config" in conflict["detail"] and "rerun" in conflict["detail"]
    assert not path.read_bytes().endswith(b"\n") and _claude_servers(path) == PLAIN  # nothing written
    assert BEARER_VAR not in text
