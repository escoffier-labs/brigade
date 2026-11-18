"""#2693: a live Claude ``headersHelper`` outranks catalog auth that came from the Codex-native carriers.

Real code paths in a scratch HOME. With a helper on the live entry, the Claude-family projection
drops ``bearer_token_env_var``, ``http_headers`` and ``env_http_headers`` (reported, not blocking),
keeps the helper and converges. Canonical ``headers`` next to a helper stays blocked.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import harness_profile_cmd, mcp_adapters, mcp_cmd, mcp_native_auth
from tests._home import set_home
from tests.test_mcp_native_auth import (
    HELPER,
    OTHER_URL,
    URL,
    _blocked,
    _claude_servers,
    _import_claude,
    _import_codex,
    _run,
    _set_catalog_fields,
    _set_catalog_url,
    _statuses,
    _sync,
    _write_claude,
    _write_codex,
)

REASON = "native_auth_helper_precedence"
TOKEN_VAR = "MARKER_PREC_TOKEN_5b8e"
HDR_NAME = "X-Marker-Prec-Name"
HDR_VALUE = "marker-prec-value-3c1d"
ENV_NAME = "MARKER_PREC_ENV_2a9f"
PLANTED = (HELPER, "MARKER_HELPER_7f3a", "/opt/fake/helper", TOKEN_VAR, HDR_NAME, HDR_VALUE, ENV_NAME)

CARRIERS = {
    "bearer_token_env_var": f'bearer_token_env_var = "{TOKEN_VAR}"\n',
    "http_headers": f'http_headers = {{ "{HDR_NAME}" = "{HDR_VALUE}" }}\n',
    "env_http_headers": f'env_http_headers = {{ "{HDR_NAME}" = "{ENV_NAME}" }}\n',
}
CATALOG_VALUES = {
    "bearer_token_env_var": TOKEN_VAR,
    "http_headers": {HDR_NAME: HDR_VALUE},
    "env_http_headers": {HDR_NAME: ENV_NAME},
}
LIVE = {"docs": {"type": "http", "url": URL, "headersHelper": HELPER}}


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    assert mcp_cmd.init(target=repo, json_output=True) == 0
    return repo, home


def _no_planted(*texts):
    for text in texts:
        for marker in PLANTED:
            assert marker not in text


def _precedence_rows(payload, harness="claude-user"):
    rows = [r for r in payload["fidelity"]["servers"] if r["harness"] == harness and r["server"] == "docs"]
    return [f for r in rows for f in r["fields"] if f["reason"] == REASON]


def _setup_both_tools(box, capsys, carrier):
    repo, home = box
    claude = _write_claude(home, LIVE)
    codex = _write_codex(home, f'[mcp_servers.docs]\nurl = "{URL}"\n{CARRIERS[carrier]}')
    assert _import_claude(capsys, repo)[0] == 0
    rc, out, payload = _import_codex(capsys, repo)
    assert rc == 0 and payload["native_auth_merged"] == ["docs"]
    _no_planted(out)
    return repo, claude, codex


@pytest.mark.parametrize("carrier", sorted(CARRIERS))
def test_issue_scenario_converges_for_each_codex_native_carrier(box, capsys, carrier):
    repo, claude, codex = _setup_both_tools(box, capsys, carrier)
    catalog = mcp_cmd.canonical_path(repo).read_bytes()

    rc, out, payload = _sync(capsys, repo, force=False)  # --adopt, no --force
    assert rc == 0
    _no_planted(out)
    assert _claude_servers(claude) == LIVE  # helper identical, no header written beside it
    assert _statuses(capsys, repo)["docs"]["status"] == "current"
    rows = _precedence_rows(payload)
    assert [(r["field"], r["status"], r["blocking"]) for r in rows] == [(carrier, "intentionally_omitted", False)]

    before = claude.read_bytes()
    rc, out, payload = _sync(capsys, repo, force=False)  # second run: nothing to write
    assert rc == 0 and claude.read_bytes() == before
    assert {i["action"] for i in payload["items"]} == {"skip"}
    assert mcp_cmd.canonical_path(repo).read_bytes() == catalog  # a sync never edits the catalog

    # Codex is untouched by the rule: it keeps its own field and stays faithful.
    codex_before = tomllib.loads(codex.read_text())["mcp_servers"]["docs"]
    rc, out, payload = _sync(capsys, repo, harness="codex-user", force=False)
    assert rc == 0
    codex_after = tomllib.loads(codex.read_text())["mcp_servers"]["docs"]
    assert codex_after[carrier] == codex_before[carrier] and codex_after["url"] == URL
    assert all(f["reason"] != REASON for r in payload["fidelity"]["servers"] for f in r["fields"])
    _no_planted(out)


def test_precedence_is_one_nonblocking_line_in_text_output(box, capsys):
    repo, claude, _ = _setup_both_tools(box, capsys, "bearer_token_env_var")
    rc, out = _run(capsys, mcp_cmd.sync, target=repo, harness="claude-user", adopt=True, write=True, user_scope=True)
    assert rc == 0
    lines = [ln for ln in out.splitlines() if REASON in ln]
    assert lines == [f"fidelity claude-user/docs [user] bearer_token_env_var: intentionally_omitted ({REASON})"]
    _no_planted(out)


def test_project_scope_claude_harness_has_the_same_rule(box, capsys):
    repo, home = box
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": LIVE}, indent=2) + "\n")
    _write_codex(home, f'[mcp_servers.docs]\nurl = "{URL}"\n{CARRIERS["bearer_token_env_var"]}')
    rc, _ = _run(
        capsys, mcp_cmd.import_servers, target=repo, harness="claude", merge=True, user_scope=False, json_output=True
    )
    assert rc == 0 and _import_codex(capsys, repo)[0] == 0
    rc, out, payload = _sync(capsys, repo, harness="claude", force=False)
    assert rc == 0 and json.loads((repo / ".mcp.json").read_text())["mcpServers"] == LIVE
    assert [r["field"] for r in _precedence_rows(payload, "claude")] == ["bearer_token_env_var"]
    _no_planted(out)


def test_adding_a_bearer_to_a_helper_backed_server_changes_nothing_live(box, capsys):
    repo, home = box
    claude = _write_claude(home, LIVE)
    _import_claude(capsys, repo)
    assert _sync(capsys, repo, force=False)[0] == 0
    before = claude.read_bytes()
    for var in (TOKEN_VAR, "MARKER_PREC_TOKEN_OTHER"):  # add, then swap
        _set_catalog_fields(repo, "docs", bearer_token_env_var=var)
        rc, out, payload = _sync(capsys, repo, adopt=False, force=False)
        assert rc == 0 and claude.read_bytes() == before
        assert [r["field"] for r in _precedence_rows(payload)] == ["bearer_token_env_var"]
        assert _statuses(capsys, repo)["docs"]["status"] == "current"
        assert var not in out
    rc, _, payload = _sync(capsys, repo, adopt=False, force=False)
    assert rc == 0 and {i["action"] for i in payload["items"]} == {"skip"}


# --------------------------------------------------------------------------- #
# What stays blocked
# --------------------------------------------------------------------------- #

BLOCKED = {
    "canonical-headers": {"headers": {"X-Team": {"literal": "blue"}}},
    "canonical-authorization": {"headers": {"Authorization": {"ref": "SOME_TOKEN_VAR"}}},
    "mixed-bearer": {"headers": {"X-Team": {"literal": "blue"}}, "bearer_token_env_var": TOKEN_VAR},
    "mixed-http-headers": {"headers": {"X-Team": {"literal": "blue"}}, "http_headers": {HDR_NAME: HDR_VALUE}},
    "mixed-env-headers": {"headers": {"X-Team": {"literal": "blue"}}, "env_http_headers": {HDR_NAME: ENV_NAME}},
    "shadowed-authorization": {
        "headers": {"Authorization": {"ref": "SOME_TOKEN_VAR"}},
        "bearer_token_env_var": TOKEN_VAR,
    },
}


@pytest.mark.parametrize("flags", [(False, False), (True, True)], ids=["plain", "adopt-force"])
@pytest.mark.parametrize("extra", list(BLOCKED.values()), ids=list(BLOCKED))
def test_canonical_headers_next_to_a_helper_stay_blocked(box, capsys, extra, flags):
    repo, home = box
    claude = _write_claude(home, LIVE)
    _import_claude(capsys, repo)
    assert _sync(capsys, repo, force=False)[0] == 0
    _set_catalog_fields(repo, "docs", **extra)
    before = claude.read_bytes()
    rc, out, payload = _sync(capsys, repo, adopt=flags[0], force=flags[1])
    assert rc == 1
    _blocked(payload, "native_auth_source_collision")
    assert claude.read_bytes() == before
    assert _precedence_rows(payload) == []
    _no_planted(out)


@pytest.mark.parametrize("flags", [(False, False), (True, True)], ids=["plain", "adopt-force"])
@pytest.mark.parametrize("carrier", sorted(CARRIERS))
def test_origin_change_with_a_codex_native_carrier_is_still_blocked(box, capsys, carrier, flags):
    repo, home = box
    claude = _write_claude(home, LIVE)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", **{carrier: CATALOG_VALUES[carrier]})
    _set_catalog_url(repo, "docs", OTHER_URL)
    rc, out, payload = _sync(capsys, repo, adopt=flags[0], force=flags[1])
    assert rc == 1
    _blocked(payload, "native_auth_origin_changed")
    assert _claude_servers(claude) == LIVE
    _no_planted(out)


def test_transport_change_with_a_codex_native_carrier_is_still_blocked(box, capsys):
    repo, home = box
    claude = _write_claude(home, LIVE)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", transport="stdio", url=None, command="npx", bearer_token_env_var=TOKEN_VAR)
    rc, out, payload = _sync(capsys, repo)
    assert rc == 1 and _claude_servers(claude) == LIVE
    _no_planted(out)
    assert not [i for i in payload["items"] if i["status"] != "conflicted" and i["server"] == "docs"]


@pytest.mark.parametrize(
    "extra",
    [
        {"http_headers": {HDR_NAME: "${SOME_VAR}"}},
        {"http_headers": {"Authorization": HDR_VALUE}, "bearer_token_env_var": TOKEN_VAR},
    ],
    ids=["templated-static", "duplicate-native-name"],
)
def test_unrepresentable_native_auth_stays_blocked_with_a_helper(box, capsys, extra):
    repo, home = box
    claude = _write_claude(home, LIVE)
    _import_claude(capsys, repo)
    _set_catalog_fields(repo, "docs", **extra)
    rc, out, payload = _sync(capsys, repo)
    assert rc == 2 and _claude_servers(claude) == LIVE
    assert [(i["status"], i["action"]) for i in payload["items"] if i["server"] == "docs"] == [("invalid", "conflict")]
    assert any(
        f["reason"] == "native_auth_unrepresentable" and f["blocking"]
        for r in payload["fidelity"]["servers"]
        for f in r["fields"]
    )
    _no_planted(out)


# --------------------------------------------------------------------------- #
# No helper: behavior is exactly what it was
# --------------------------------------------------------------------------------- #


def _project(repo, harness, live, **fields):
    server = mcp_adapters.CanonicalServer(name="docs", transport="http", url=URL, **fields)
    return mcp_cmd._project_server(repo, harness, server, mcp_cmd.ProjectionContext(live, None, None))


@pytest.mark.parametrize("harness", ["claude", "claude-user"])
@pytest.mark.parametrize(
    ("fields", "headers"),
    [
        ({"headers": {"X-Api": {"ref": "FAKE_VAR"}}}, {"X-Api": "${FAKE_VAR}"}),
        ({"bearer_token_env_var": "FAKE_TOKEN"}, {"Authorization": "Bearer ${FAKE_TOKEN}"}),
        ({"http_headers": {"X-Static": "fake"}}, {"X-Static": "fake"}),
        ({"env_http_headers": {"X-Env": "FAKE_ENV"}}, {"X-Env": "${FAKE_ENV}"}),
    ],
    ids=["headers", "bearer_token_env_var", "http_headers", "env_http_headers"],
)
@pytest.mark.parametrize("live", [{}, {"type": "http", "url": URL}, {"type": "http", "url": URL, "headersHelper": ""}])
def test_without_a_live_helper_every_carrier_is_still_projected_into_headers(box, harness, fields, headers, live):
    repo, _ = box
    assert _project(repo, harness, live, **fields) == {"url": URL, "type": "http", "headers": headers}


@pytest.mark.parametrize("harness", ["claude", "claude-user"])
@pytest.mark.parametrize(
    "fields",
    [
        {"http_headers": {"X-A": "${SOME_VAR}"}},
        {"http_headers": {"Authorization": "x"}, "bearer_token_env_var": "FAKE_TOKEN"},
    ],
)
def test_without_a_live_helper_unrepresentable_auth_still_raises(box, harness, fields):
    repo, _ = box
    with pytest.raises(ValueError, match="cannot preserve Codex native"):
        _project(repo, harness, {}, **fields)


def test_other_harnesses_have_no_precedence_rule(box):
    repo, _ = box
    live = {"type": "http", "url": URL, "headersHelper": HELPER}
    entry = _project(repo, "cursor-user", live, bearer_token_env_var="FAKE_TOKEN")
    assert "Authorization" in entry["headers"] and "headersHelper" not in entry


@pytest.mark.parametrize("harness", ["claude", "claude-user"])
def test_projection_with_a_helper_drops_carriers_and_keeps_the_key(box, harness):
    repo, _ = box
    live = {"type": "http", "url": URL, "headersHelper": HELPER}
    server = mcp_adapters.CanonicalServer(
        name="docs",
        transport="http",
        url=URL,
        bearer_token_env_var="FAKE_TOKEN",
        http_headers={"X-A": "a"},
        env_http_headers={"X-B": "FAKE_B"},
    )
    context = mcp_cmd.ProjectionContext(live, None, None)
    assert mcp_cmd._project_server(repo, harness, server, context) == live
    with pytest.raises(mcp_native_auth.NativeAuthSourceCollision):
        mcp_cmd._project_server(repo, harness, replace(server, headers={"H": {"literal": "v"}}), context)


# --------------------------------------------------------------------------- #
# Harness profile path
# --------------------------------------------------------------------------- #


def test_harness_profile_converges_with_a_helper_and_a_codex_native_carrier(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    set_home(monkeypatch, home)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert mcp_cmd.init(target=ws, json_output=True) == 0
    mcp_cmd.canonical_path(ws).write_text(
        json.dumps({"servers": {"docs": {"transport": "http", "url": URL, "bearer_token_env_var": TOKEN_VAR}}})
    )
    live_path = home / ".claude.json"
    live_path.write_text(json.dumps({"mcpServers": LIVE}, indent=2) + "\n")

    def run(fn, **kw):
        capsys.readouterr()
        rc = fn(harness="claude", workspace=ws, home=home, json_output=True, **kw)
        captured = capsys.readouterr()
        out = captured.out + captured.err
        _no_planted(out)
        return rc, json.loads(out)

    def items(payload):
        return [i for r in payload["results"] for i in r.get("mcp", {}).get("items", [])]

    rc, payload = run(harness_profile_cmd.sync, write=True, adopt=True)
    assert rc == 0 and [i["status"] for i in items(payload)] == ["adopted"]
    assert json.loads(live_path.read_text())["mcpServers"] == LIVE
    before = live_path.read_bytes()
    assert run(harness_profile_cmd.doctor, verify_mcp=True)[0] == 0
    rc, payload = run(harness_profile_cmd.sync, write=True, adopt=False)
    assert rc == 0 and [(i["status"], i["action"]) for i in items(payload)] == [("current", "none")]
    assert live_path.read_bytes() == before
    state = "\n".join(p.read_text() for p in sorted((home / ".claude" / "brigade").glob("*.json")))
    _no_planted(state)
