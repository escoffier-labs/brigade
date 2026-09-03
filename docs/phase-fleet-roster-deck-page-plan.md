# Fleet roster page implementation plan

Spec: `docs/phase-fleet-roster-deck-page.md` (approved 2026-09-02). Tracked
here because `docs/plans/` is gitignored in this repo.

Goal: a hub-served `/deck/roster` page that shows the fleet roster and lets
the operator save six role defaults, seat on/off, cloud lane on/off,
consumer defaults, and notes in one revisioned transaction, plus a
`fleet_routing` block in `brigade work brief` so every session reads the pin.

Architecture: `run_preference.py` grows three role fields shared by the CLI,
the hub, and the page. `fleet_hub_preference.py` stores them (schema v19).
A new `fleet_hub_roster_page.py` owns the read projection, HTML render, form
parse, and the single apply transaction; `fleet_hub_http.py` only routes,
authenticates, and enforces body, CSRF, and origin rules. A new
`work_cmd/session/fleet_routing.py` builds and prints the brief block under a
three second budget. Stdlib only, no JavaScript needed on the page.

Key tech: `http.server`, `sqlite3`, `hmac`, `urllib.parse.parse_qs`,
`pytest`, the module size ratchet (`tests/test_module_size_ratchet.py`).

Agentic workers: execute task by task, in order. Tick each checkbox as you
complete it. Run every verification through Brigade:
`brigade work verify run --target . --command "<cmd>" --capture brigade-work`.
Commit after each task with the message given. Leave the worktree clean
between tasks. Do not change behavior the spec does not name.

## File map

- Modify `src/brigade/run_preference.py`: `ROLE_FIELDS`, three new dataclass
  fields, prefix lines.
- Modify `src/brigade/fleet_hub_preference.py`: three columns, additive
  migration, `get_run_preference_meta`, a no-commit upsert used by the page.
- Modify `src/brigade/fleet_hub.py`: `SCHEMA_VERSION = 19`.
- Modify `src/brigade/cli/fleet.py`: three `preference set` flags, printing.
- Create `src/brigade/fleet_hub_roster_page.py`: view, render, parse, apply.
- Modify `src/brigade/fleet_hub_http.py`: `GET/POST /deck/roster`.
- Modify `src/brigade/fleet_command_deck.py`: nav link, `_document` title and
  refresh parameters, form CSS.
- Create `src/brigade/work_cmd/session/fleet_routing.py`: brief block.
- Modify `src/brigade/work_cmd/session/briefing.py`: payload key and print.
- Modify `docs/fleet-sync.md`, `docs/runbooks/fleet-hub-proxmox.md`.
- Test: `tests/test_run_preference.py` (modify),
  `tests/test_fleet_roster_page.py` (create),
  `tests/test_work_cmd_session.py` (modify).

Verification command used throughout (focused suites):

```bash
brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py tests/test_fleet_roster_page.py tests/test_fleet_command_deck.py tests/test_fleet_model_roster.py tests/test_work_cmd_session.py tests/test_module_size_ratchet.py" --capture brigade-work
```

### Task 1: role fields in `run_preference.py`

**Files:**
- Modify: `src/brigade/run_preference.py:20-66`
- Test: `tests/test_run_preference.py`

- [x] Append these tests to `tests/test_run_preference.py`

```python
def test_role_fields_parse_round_trip_and_prefix(tmp_path) -> None:
    raw = {
        "impl": "agy_flash",
        "review": "claude_standby",
        "chef": "chef",
        "research": "researcher",
        "security": "daybreak",
        "scout": "cursor_scout",
        "notes": "cursor via Other Models only",
    }
    pref = run_preference.parse_preference(raw)
    assert pref.research == "researcher"
    assert pref.security == "daybreak"
    assert pref.scout == "cursor_scout"
    assert pref.payload() == raw
    assert run_preference.ROLE_FIELDS == ("impl", "review", "chef", "research", "security", "scout")
    run_preference.write_cached(pref, tmp_path)
    assert run_preference.load_cached(tmp_path) == pref
    prefix = pref.planner_prefix()
    assert "- default research: researcher" in prefix
    assert "- default security: daybreak" in prefix
    assert "- default scout: cursor_scout" in prefix
    assert prefix.index("default chef") < prefix.index("default research")


def test_role_fields_are_seat_names_and_never_dispatch() -> None:
    with pytest.raises(run_preference.RunPreferenceError, match="roster seat name"):
        run_preference.parse_preference({"security": "not a seat"})
    roster = _FakeRoster(orchestrator="chef", agents={"chef": object(), "daybreak": object()})
    pref = run_preference.RunPreference(security="daybreak")
    assert run_preference.resolve_worker(pref, roster, worker=None, task="scan the repo") is None
    # "security" must not trip the secret-key regex.
    assert run_preference.parse_preference({"security": "daybreak"}).security == "daybreak"
```

- [x] Run it, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py -k role_fields" --capture brigade-work`
  Expect FAIL: `AttributeError: module 'brigade.run_preference' has no attribute 'ROLE_FIELDS'` or `RunPreferenceError: unknown preference field: research`.
- [x] Replace lines 20-21 (`ALLOWED_FIELDS = ...`) with:

```python
ROLE_FIELDS = ("impl", "review", "chef", "research", "security", "scout")
ALLOWED_FIELDS = (*ROLE_FIELDS, "notes")
```

- [x] Replace the `RunPreference` dataclass body (fields and `planner_prefix`) with:

```python
@dataclass(frozen=True)
class RunPreference:
    impl: str | None = None
    review: str | None = None
    chef: str | None = None
    research: str | None = None
    security: str | None = None
    scout: str | None = None
    notes: str | None = None

    def payload(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for field in ALLOWED_FIELDS:
            value = getattr(self, field)
            if isinstance(value, str) and value:
                result[field] = value
        return result

    def planner_prefix(self) -> str:
        lines = [
            "Fleet run preference (explicit --worker or a named seat in the task wins):",
        ]
        for role in ROLE_FIELDS:
            value = getattr(self, role)
            if value:
                lines.append(f"- default {role}: {value}")
        if self.notes:
            lines.append(f"- notes: {self.notes}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines) + "\n\n"
```

- [x] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py" --capture brigade-work`
  Expect: all tests pass (the existing `test_hub_preference_get_put_and_rejects_secrets` still passes because the hub side is unchanged so far).
- [x] Commit: `git add -A && git commit -m "feat(preference): add research, security, and scout role fields"`

### Task 2: hub storage for the roles (schema v19)

**Files:**
- Modify: `src/brigade/fleet_hub_preference.py` (whole file)
- Modify: `src/brigade/fleet_hub.py:126` (`SCHEMA_VERSION`)
- Test: `tests/test_run_preference.py`

- [ ] In `tests/test_run_preference.py`, change the `empty ==` assertion inside `test_hub_preference_get_put_and_rejects_secrets` to:

```python
    assert empty == {
        "impl": None,
        "review": None,
        "chef": None,
        "research": None,
        "security": None,
        "scout": None,
        "notes": None,
    }
```

- [ ] Append these tests:

```python
def test_hub_preference_stores_roles_and_meta(tmp_path) -> None:
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    assert fleet_hub.get_run_preference_meta(conn) == {"updated_at": None, "updated_by": None}
    stored = fleet_hub.set_run_preference(
        conn, {"security": "daybreak", "scout": "cursor_scout"}, updated_by="admin"
    )
    assert stored["security"] == "daybreak"
    assert stored["scout"] == "cursor_scout"
    meta = fleet_hub.get_run_preference_meta(conn)
    assert meta["updated_by"] == "admin"
    assert isinstance(meta["updated_at"], str) and meta["updated_at"]
    conn.close()


def test_hub_preference_v18_row_survives_v19_migration(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE run_preference (id INTEGER PRIMARY KEY CHECK (id = 1), impl TEXT, review TEXT, "
        "chef TEXT, notes TEXT, updated_at TEXT NOT NULL, updated_by TEXT)"
    )
    old.execute(
        "INSERT INTO run_preference VALUES (1, 'coder', 'claude_standby', 'chef', 'kept', "
        "'2026-01-01T00:00:00+00:00', 'admin')"
    )
    old.execute("PRAGMA user_version=18")
    old.commit()
    old.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION == 19
    pref = fleet_hub.get_run_preference(conn)
    assert pref["impl"] == "coder" and pref["notes"] == "kept"
    assert pref["research"] is None and pref["security"] is None and pref["scout"] is None
    conn.close()
```

- [ ] Run, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py -k 'hub_preference'" --capture brigade-work`
  Expect FAIL: `AttributeError: module 'brigade.fleet_hub' has no attribute 'get_run_preference_meta'` and the `empty ==` mismatch.
- [ ] Replace `src/brigade/fleet_hub_preference.py` with:

```python
"""Fleet hub storage for the one-row run preference pin (#1223).

Split from ``fleet_hub`` to keep that module under the size ratchet. The
preference carries seat *names* only; ``run_preference.parse_preference``
rejects secret or path material before anything reaches the table.

v19 adds the ``research``, ``security``, and ``scout`` role columns used by
the hub roster page; the migration is additive.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import run_preference

# v13 (#1223): one-row fleet run preference. Seat names only; never secrets.
_RUN_PREFERENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_preference (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    impl TEXT,
    review TEXT,
    chef TEXT,
    research TEXT,
    security TEXT,
    scout TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
"""
_V19_COLUMNS = ("research", "security", "scout")
_FIELDS = run_preference.ALLOWED_FIELDS


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the run_preference table (v12 -> v13) and add the v19 role columns."""
    conn.execute(_RUN_PREFERENCE_SCHEMA)
    present = {str(row[1]) for row in conn.execute("PRAGMA table_info(run_preference)").fetchall()}
    for column in _V19_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE run_preference ADD COLUMN {column} TEXT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_run_preference(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the one-row fleet run preference, or empty fields when unset."""
    columns = ", ".join(_FIELDS)
    row = conn.execute(f"SELECT {columns} FROM run_preference WHERE id = 1").fetchone()
    if row is None:
        return {field: None for field in _FIELDS}
    return {field: row[index] for index, field in enumerate(_FIELDS)}


def get_run_preference_meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    """``updated_at`` and ``updated_by`` of the pin; both ``None`` when unset."""
    row = conn.execute("SELECT updated_at, updated_by FROM run_preference WHERE id = 1").fetchone()
    if row is None:
        return {"updated_at": None, "updated_by": None}
    return {"updated_at": row[0], "updated_by": row[1]}


def upsert_run_preference(conn: sqlite3.Connection, raw: Any, *, updated_by: str | None) -> dict[str, Any]:
    """Validate and write the pin inside the caller's transaction. No commit."""
    from .fleet_hub import FleetHubError

    try:
        parsed = run_preference.parse_preference(raw)
    except run_preference.RunPreferenceError as exc:
        raise FleetHubError(str(exc)) from exc
    payload = parsed.payload()
    columns = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    updates = ", ".join(f"{field}=excluded.{field}" for field in _FIELDS)
    conn.execute(
        f"INSERT INTO run_preference (id, {columns}, updated_at, updated_by) "
        f"VALUES (1, {placeholders}, ?, ?) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (*[payload.get(field) for field in _FIELDS], _utc_now(), updated_by),
    )
    return get_run_preference(conn)


def set_run_preference(conn: sqlite3.Connection, raw: Any, *, updated_by: str | None = None) -> dict[str, Any]:
    """Replace the fleet run preference and commit. Rejects secret or path material."""
    stored = upsert_run_preference(conn, raw, updated_by=updated_by)
    conn.commit()
    return stored
```

- [ ] In `src/brigade/fleet_hub.py`, change line 126 to `SCHEMA_VERSION = 19`, and after the existing `set_run_preference` facade (around line 769) add:

```python
def get_run_preference_meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Facade for the preference row's ``updated_at`` and ``updated_by``."""
    return fleet_hub_preference.get_run_preference_meta(conn)
```

- [ ] Update the comment near line 546 of `fleet_hub.py` that lists migrations by appending: `# v18 -> v19: research/security/scout role columns on run_preference (roster page).`
- [ ] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py tests/test_fleet_model_roster.py tests/test_fleet_command_deck.py" --capture brigade-work`
  Expect PASS. `tests/test_worklore_store.py:2721-2724` asserts the literal `18` twice (`fleet_hub.SCHEMA_VERSION == 18` and `PRAGMA user_version == 18`); change both literals to `19` in the same commit and include that file in the run:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_worklore_store.py -k schema" --capture brigade-work` - expect PASS.
- [ ] Commit: `git add -A && git commit -m "feat(fleet): store research, security, and scout roles on the hub (schema v19)"`

### Task 3: CLI flags and printing

**Files:**
- Modify: `src/brigade/cli/fleet.py:374-380` (parser), `:717-741` (`_preference_payload`, `_print_preference`), `:763-771` (`_dispatch_preference_set`)
- Test: `tests/test_run_preference.py`

- [ ] Append this test:

```python
def test_fleet_preference_cli_sets_and_prints_roles(tmp_path, monkeypatch, capsys) -> None:
    from brigade import cli

    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    stored: dict[str, str] = {}

    def fake_put(preference, *, hub_url=None):
        stored.clear()
        stored.update(preference)
        return dict(stored)

    monkeypatch.setattr("brigade.fleet_client.fetch_run_preference", lambda: dict(stored))
    monkeypatch.setattr("brigade.fleet_client.put_run_preference", fake_put)
    assert (
        cli.main(
            [
                "fleet", "preference", "set",
                "--research", "researcher", "--security", "daybreak", "--scout", "cursor_scout",
            ]
        )
        == 0
    )
    assert stored == {"research": "researcher", "security": "daybreak", "scout": "cursor_scout"}
    out = capsys.readouterr().out
    assert "  security: daybreak" in out
    assert "  scout: cursor_scout" in out
    assert cli.main(["fleet", "preference", "get"]) == 0
    assert "  research: researcher" in capsys.readouterr().out
```

- [ ] Run, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py -k cli_sets_and_prints_roles" --capture brigade-work`
  Expect FAIL: argparse `error: unrecognized arguments: --research` (exit code 2 raised as SystemExit).
- [ ] In the parser block (after the `--chef` argument, before `--notes`) add:

```python
    p_pref_set.add_argument("--research", default=None, help="Default read-only research seat name.")
    p_pref_set.add_argument("--security", default=None, help="Default security review seat name.")
    p_pref_set.add_argument("--scout", default=None, help="Default fast repository scout seat name.")
```

- [ ] Replace `_preference_payload` and `_print_preference` with:

```python
def _preference_payload(preference: object) -> dict[str, str | None]:
    from .. import run_preference

    if isinstance(preference, run_preference.RunPreference):
        return {field: getattr(preference, field) for field in run_preference.ALLOWED_FIELDS}
    if isinstance(preference, dict):
        return {field: preference.get(field) for field in run_preference.ALLOWED_FIELDS}
    return {field: None for field in run_preference.ALLOWED_FIELDS}


def _print_preference(payload: dict[str, str | None], *, source: str) -> None:
    from .. import run_preference

    print(f"run preference ({source})")
    for key in run_preference.ALLOWED_FIELDS:
        print(f"  {key}: {_safe_table_cell(payload.get(key) or '-')}")
```

- [ ] In `_dispatch_preference_set`, replace the `raw = ...` line and the error message with:

```python
    raw = {
        key: getattr(args, key)
        for key in run_preference.ALLOWED_FIELDS
        if getattr(args, key, None) is not None
    }
    if not raw:
        flags = ", ".join(f"--{key}" for key in run_preference.ALLOWED_FIELDS)
        print(f"error: set at least one of {flags}", file=sys.stderr)
        return 2
```

- [ ] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_run_preference.py" --capture brigade-work`
  Expect PASS.
- [ ] Commit: `git add -A && git commit -m "feat(fleet): preference set gains --research, --security, and --scout"`

### Task 4: roster page read view and `GET /deck/roster`

**Files:**
- Create: `src/brigade/fleet_hub_roster_page.py`
- Modify: `src/brigade/fleet_hub_http.py` (`do_GET`, new `_serve_roster`)
- Modify: `src/brigade/fleet_command_deck.py:975,1075` (nav), `:1182-1196` (`_document`), `_STYLE`
- Test: `tests/test_fleet_roster_page.py` (create)

- [ ] Create `tests/test_fleet_roster_page.py` with the fixtures and the GET tests:

```python
"""Hub-served roster page: GET rendering, auth, and POST apply (roster page spec)."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest

from brigade import fleet_hub, fleet_hub_roster_page

TOKEN = "test-admin-token-roster"
NODE_A = "11111111-1111-4111-8111-111111111111"
SEATS = {
    "agy_flash": {
        "provider": "google", "model": "gemini-3.8-flash-high", "reasoning": "none",
        "brigade_cli": "antigravity", "t3_instance_id": "", "limit": 4,
    },
    "coder": {
        "provider": "openai", "model": "gpt-5.6-terra", "reasoning": "high",
        "brigade_cli": "codex", "t3_instance_id": "codex", "limit": 1,
    },
    "daybreak": {
        "provider": "openai", "model": "gpt-daybreak-blue-latest", "reasoning": "high",
        "brigade_cli": "codex", "t3_instance_id": "", "limit": 1,
    },
    "cursor_grok": {
        "provider": "cursor", "model": "cursor-grok-4.6-high-fast", "reasoning": "none",
        "brigade_cli": "cursor", "t3_instance_id": "cursor", "limit": 8,
    },
}


@contextmanager
def _hub(tmp_path, *, trust_tailscale: bool = False):
    db = tmp_path / "hub" / "fleet.db"
    server = fleet_hub.make_server(
        "127.0.0.1", 0, db, TOKEN, trust_tailscale_identity=trust_tailscale
    )  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_address[1]), db  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None):
    host, port = hub
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    text = response.read().decode("utf-8")
    result = (response.status, {k.lower(): v for k, v in response.getheaders()}, text)
    conn.close()
    return result


def _bearer() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _json(hub, method: str, path: str, body: dict, *, token: str = TOKEN):
    status, _headers, text = _request(
        hub, method, path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        body=json.dumps(body).encode("utf-8"),
    )
    return status, json.loads(text)


def _revision(hub) -> int:
    status, _headers, text = _request(hub, "GET", "/models", headers=_bearer())
    assert status == 200
    return int(json.loads(text)["revision"])


def _seed(hub) -> None:
    for seat, fields in SEATS.items():
        status, payload = _json(
            hub, "POST", "/models",
            {"action": "set", "seat": seat, "enabled": True, "expected_revision": _revision(hub), **fields},
        )
        assert status == 200, payload
    status, payload = _json(
        hub, "PUT", "/preference",
        {"impl": "coder", "review": "coder", "chef": "coder", "notes": "seeded"},
    )
    assert status == 200, payload


def _login_cookie(hub) -> str:
    status, headers, _text = _request(hub, "GET", f"/deck/roster?token={TOKEN}")
    assert status == 303
    assert headers["location"] == "/deck/roster"
    return headers["set-cookie"].split(";")[0]


def _enroll_node(db) -> str:
    conn = fleet_hub.open_db(db)
    try:
        _node, node_token = fleet_hub.add_node(conn, NODE_A, "node-a")
    finally:
        conn.close()
    return node_token


def _form(hub, fields: dict, *, cookie: str | None = None, extra: dict | None = None) -> tuple:
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}
    if cookie:
        headers["Cookie"] = cookie
    headers.update(extra or {})
    return _request(hub, "POST", "/deck/roster", headers=headers, body=urlencode(fields, doseq=True).encode())


def _current_form(hub, cookie: str) -> dict:
    """The field set a browser would submit from the freshly rendered page."""
    status, _headers, page = _request(hub, "GET", "/deck/roster", headers={"Cookie": cookie})
    assert status == 200
    fields = {"csrf": fleet_hub_roster_page.csrf_value(TOKEN)}
    fields["expected_revision"] = re.search(r'name="expected_revision" value="(\d+)"', page).group(1)
    fields["expected_preference_updated_at"] = re.search(
        r'name="expected_preference_updated_at" value="([^"]*)"', page
    ).group(1)
    for seat in re.findall(r'name="seat\.([a-z0-9._-]+)" value="1" checked', page):
        fields[f"seat.{seat}"] = "1"
    for provider in re.findall(r'name="cloud\.([a-z0-9-]+)" value="1" checked', page):
        fields[f"cloud.{provider}"] = "1"
    for role in fleet_hub_roster_page.ROLES:
        match = re.search(rf'name="role\.{role}".*?<option value="([^"]*)" selected', page, re.S)
        fields[f"role.{role}"] = match.group(1) if match else ""
    for consumer in fleet_hub_roster_page.CONSUMERS:
        match = re.search(rf'name="default\.{consumer}".*?<option value="([^"]*)" selected', page, re.S)
        fields[f"default.{consumer}"] = match.group(1) if match else ""
    notes = re.search(r'<textarea name="notes"[^>]*>([^<]*)</textarea>', page)
    fields["notes"] = notes.group(1) if notes else ""
    return fields


def _tables(db) -> str:
    conn = sqlite3.connect(db)
    try:
        return "\n".join(
            line
            for line in conn.iterdump()
            if any(
                table in line
                for table in ("model_policy", "model_consumer_defaults", "model_roster_meta", "run_preference", "cloud_provider_state")
            )
        )
    finally:
        conn.close()


# --- GET -------------------------------------------------------------------


def test_roster_page_requires_auth_and_renders_every_block(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        _seed(hub)
        assert _request(hub, "GET", "/deck/roster")[0] == 401
        cookie = _login_cookie(hub)
        status, headers, page = _request(hub, "GET", "/deck/roster", headers={"Cookie": cookie})
        assert status == 200
        assert headers["cache-control"] == "no-store"
        assert 'http-equiv="refresh"' not in page
        assert "Roster" in page and 'href="/deck/roster"' in page
        for heading in ("Roles", "Seats", "Cloud lanes", "Consumer defaults", "Retired families"):
            assert heading in page
        assert 'name="role.security"' in page and 'name="role.scout"' in page
        assert 'name="seat.agy_flash" value="1" checked' in page
        assert 'name="cloud.jules" value="1" checked' in page
        assert 'name="cloud.claude" value="1"' in page and 'name="cloud.claude" value="1" checked' not in page
        assert 'name="default.brigade-run"' in page and 'name="default.t3-fleet"' in page
        assert "gpt-5.4" in page and "permanent" in page
        assert f'name="expected_revision" value="{_revision(hub)}"' in page
        assert 'name="csrf" value=' in page
        assert '<button type="submit">Save</button>' in page
        assert TOKEN not in page and cookie.split("=", 1)[1] not in page
        # bearer works too
        assert _request(hub, "GET", "/deck/roster", headers=_bearer())[0] == 200


def test_roster_page_deck_nav_links_to_it(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        deck = _request(hub, "GET", "/deck", headers=_bearer())[2]
        assert '<a href="/deck/roster">roster</a>' in deck


def test_roster_page_read_only_under_tailscale_identity(tmp_path):
    with _hub(tmp_path, trust_tailscale=True) as (hub, _db):
        _seed(hub)
        status, _headers, page = _request(
            hub, "GET", "/deck/roster", headers={"Tailscale-User-Login": "operator@example.test"}
        )
        assert status == 200
        assert "read-only" in page
        assert "<button" not in page
        assert page.count("disabled") >= 10
        assert "operator@example.test" not in page


def test_roster_page_escapes_hostile_notes(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        _seed(hub)
        status, payload = _json(hub, "PUT", "/preference", {"notes": "<script>alert(1)</script>"})
        assert status == 200, payload
        page = _request(hub, "GET", "/deck/roster", headers=_bearer())[2]
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
```

- [ ] Run, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_fleet_roster_page.py -k 'renders_every_block or nav_links or read_only or hostile'" --capture brigade-work`
  Expect FAIL: `ModuleNotFoundError: No module named 'brigade.fleet_hub_roster_page'`.
- [ ] Create `src/brigade/fleet_hub_roster_page.py`:

```python
"""Hub-served roster page (``/deck/roster``): projection, HTML, form parse, apply.

Everything here is a pure function of a hub connection and a startup-frozen
deck config, so tests render without a socket. ``fleet_hub_http`` owns
authentication, the CSRF and same-origin checks, and body limits; this
module owns what the page shows and the one transaction a Save performs.
No token, cookie value, or identity is ever rendered.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qs

from . import fleet_command_deck, fleet_hub, fleet_hub_model_roster, fleet_hub_preference, fleet_model_roster
from . import run_preference

CSRF_PURPOSE = b"brigade.fleet-roster-form.v1"
MAX_FORM_BYTES = 64 * 1024
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
ROLES = run_preference.ROLE_FIELDS
CONSUMERS = ("brigade-run", "t3-fleet")
UPDATED_BY = "deck-form"
_MAX_FIELDS = 512


class FormError(ValueError):
    """The submitted form is malformed (not a policy failure)."""


def csrf_value(token: str) -> str:
    """Hidden form token derived from the admin token; distinct from the cookie."""
    return hmac.new(token.encode("utf-8"), CSRF_PURPOSE, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SeatRow:
    seat: str
    provider: str
    model: str
    reasoning: str
    limit: int | None
    enabled: bool
    brigade_cli: str
    t3_instance_id: str
    t3_service_tier: str
    notes: str | None
    retired: bool


@dataclass(frozen=True)
class CloudRow:
    provider: str
    enabled: bool
    limit: int
    hosted: bool
    circuit_state: str
    policy: dict[str, Any]


@dataclass(frozen=True)
class RosterView:
    revision: int
    revision_updated_at: str
    updated_by: str
    seats: tuple[SeatRow, ...]
    cloud: tuple[CloudRow, ...]
    defaults: dict[str, str | None]
    retired: tuple[dict[str, Any], ...]
    preference: dict[str, str | None]
    preference_updated_at: str


@dataclass(frozen=True)
class Submission:
    expected_revision: int
    expected_preference_updated_at: str
    csrf: str
    roles: dict[str, str]
    notes: str
    seats_on: frozenset[str]
    cloud_on: frozenset[str]
    defaults: dict[str, str]


@dataclass(frozen=True)
class ApplyResult:
    status: str  # "saved" | "conflict" | "invalid"
    message: str
    revision: int


# --- projection --------------------------------------------------------------


def load_view(conn: sqlite3.Connection, config: fleet_command_deck.DeckConfig) -> RosterView:
    meta = conn.execute("SELECT revision, updated_at, updated_by FROM model_roster_meta WHERE singleton=1").fetchone()
    if meta is None:
        raise fleet_hub.FleetHubError("model roster revision metadata is missing")
    retired_rows = fleet_hub_model_roster._retired_rows(conn)
    seats: list[SeatRow] = []
    for row in conn.execute(
        "SELECT seat, provider, model, reasoning, enabled, limit_count, brigade_cli, t3_instance_id, "
        "t3_service_tier, notes FROM model_policy ORDER BY seat"
    ).fetchall():
        seats.append(
            SeatRow(
                seat=str(row[0]),
                provider=str(row[1]),
                model=str(row[2]),
                reasoning=str(row[3] or "none"),
                limit=None if row[5] is None else int(row[5]),
                enabled=bool(row[4]),
                brigade_cli=str(row[6] or ""),
                t3_instance_id=str(row[7] or ""),
                t3_service_tier=str(row[8] or ""),
                notes=row[9],
                retired=fleet_model_roster.retired_reason(str(row[1]), str(row[2]), retired_rows) is not None,
            )
        )
    providers = fleet_hub._cloud_policy(conn, config)["providers"]
    cloud = tuple(
        CloudRow(
            provider=name,
            enabled=bool(policy.get("enabled")),
            limit=int(policy.get("limit", 0)),
            hosted=bool(policy.get("hosted", True)),
            circuit_state=str(policy.get("circuit_state", "closed")),
            policy=dict(policy),
        )
        for name, policy in sorted(providers.items())
    )
    pref_meta = fleet_hub_preference.get_run_preference_meta(conn)
    return RosterView(
        revision=int(meta[0]),
        revision_updated_at=str(meta[1]),
        updated_by=str(meta[2] or ""),
        seats=tuple(seats),
        cloud=cloud,
        defaults=fleet_hub_model_roster._consumer_defaults(conn),
        retired=tuple(retired_rows),
        preference=fleet_hub_preference.get_run_preference(conn),
        preference_updated_at=str(pref_meta["updated_at"] or ""),
    )


# --- render ------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _select(name: str, current: str, seats: tuple[SeatRow, ...], *, editable: bool, binding: str | None = None) -> str:
    """One ``<select>``: ``(unset)`` first, usable seats, then an optgroup of the rest."""
    usable: list[str] = []
    rest: list[str] = []
    for row in seats:
        bound = True if binding is None else bool(getattr(row, binding))
        (usable if row.enabled and not row.retired and bound else rest).append(row.seat)
    disabled = "" if editable else " disabled"

    def option(seat: str) -> str:
        selected = " selected" if seat == current else ""
        return f'<option value="{_esc(seat)}"{selected}>{_esc(seat)}</option>'

    parts = [f'<select name="{_esc(name)}"{disabled}>']
    parts.append(f'<option value=""{" selected" if not current else ""}>(unset)</option>')
    parts.extend(option(seat) for seat in usable)
    if rest:
        label = "disabled" if binding is None else "no binding"
        parts.append(f'<optgroup label="{label}">' + "".join(option(seat) for seat in rest) + "</optgroup>")
    parts.append("</select>")
    return "".join(parts)


def _checkbox(name: str, checked: bool, *, editable: bool) -> str:
    return (
        f'<input type="checkbox" name="{_esc(name)}" value="1"'
        f'{" checked" if checked else ""}{"" if editable else " disabled"}>'
    )


def render(
    view: RosterView,
    *,
    nonce: str,
    now: datetime,
    csrf: str,
    editable: bool,
    banner: str | None = None,
    error: str | None = None,
    submission: Submission | None = None,
) -> str:
    """The roster page. ``submission`` re-selects what the operator sent on a 409 or 422."""
    roles = dict(view.preference)
    notes = view.preference.get("notes") or ""
    seats_on = {row.seat for row in view.seats if row.enabled}
    cloud_on = {row.provider for row in view.cloud if row.enabled}
    defaults = dict(view.defaults)
    if submission is not None:
        roles.update(submission.roles)
        notes = submission.notes
        seats_on = set(submission.seats_on)
        cloud_on = set(submission.cloud_on)
        defaults.update(submission.defaults)
    d = "" if editable else " disabled"
    parts: list[str] = ['<main class="deck-shell">']
    parts.append(
        '<header class="masthead"><div><p class="eyebrow">Fleet operations</p><h1>Command Deck &middot; Roster</h1>'
        f'<p class="station-meta">revision {view.revision}, updated {_esc(view.revision_updated_at)} by '
        f'{_esc(view.updated_by or "unknown")}</p></div>'
        f'<p class="header-meta">{_esc(fleet_command_deck._stamp(now))}</p></header>'
    )
    parts.append(
        '<nav aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/view/machines">machines board</a></nav>'
    )
    if banner:
        parts.append(f'<p class="banner">{_esc(banner)}</p>')
    if error:
        parts.append(f'<p class="banner banner--error">{_esc(error)}</p>')
    if not editable:
        parts.append('<p class="banner">read-only: enroll with the fleet token to edit</p>')
    parts.append('<form method="post" action="/deck/roster" class="roster-form">')
    parts.append(f'<input type="hidden" name="expected_revision" value="{view.revision}">')
    parts.append(
        f'<input type="hidden" name="expected_preference_updated_at" value="{_esc(view.preference_updated_at)}">'
    )
    if editable:
        parts.append(f'<input type="hidden" name="csrf" value="{_esc(csrf)}">')
    # 1. roles
    role_cells = "".join(
        f'<label>{_esc(role)}{_select(f"role.{role}", roles.get(role) or "", view.seats, editable=editable)}</label>'
        for role in ROLES
    )
    parts.append(
        '<section class="panel" aria-labelledby="roles"><header><h2 id="roles">Roles</h2></header>'
        f'<div class="roster-grid">{role_cells}</div>'
        f'<label>notes<textarea name="notes" maxlength="240" rows="2"{d}>{_esc(notes)}</textarea></label></section>'
    )
    # 2. seats
    seat_rows = []
    for row in view.seats:
        cls = ' class="seat--off"' if row.seat not in seats_on else ""
        flag = ' <span class="flag">retired</span>' if row.retired else ""
        box = _checkbox(f"seat.{row.seat}", row.seat in seats_on and not row.retired, editable=editable and not row.retired)
        seat_rows.append(
            f"<tr{cls}><td>{_esc(row.seat)}{flag}</td><td>{_esc(row.provider)}/{_esc(row.model)}</td>"
            f"<td>{_esc(row.reasoning)}</td><td>{_esc('-' if row.limit is None else row.limit)}</td>"
            f"<td>{_esc(row.brigade_cli or '-')}</td><td>{_esc(row.t3_instance_id or '-')}</td><td>{box}</td></tr>"
        )
    parts.append(
        '<section class="panel" aria-labelledby="seats"><header><h2 id="seats">Seats</h2>'
        f'<p class="panel-count">{len(view.seats)} seat(s)</p></header><div class="table-wrap"><table class="roster-table">'
        "<thead><tr><th>Seat</th><th>Provider/model</th><th>Reasoning</th><th>Limit</th><th>Brigade CLI</th>"
        f'<th>T3 instance</th><th>On</th></tr></thead><tbody>{"".join(seat_rows)}</tbody></table></div></section>'
    )
    # 3. cloud lanes
    cloud_rows = "".join(
        f"<tr><td>{_esc(row.provider)}</td><td>{_checkbox(f'cloud.{row.provider}', row.provider in cloud_on, editable=editable)}</td>"
        f"<td>{row.limit}</td><td>{'yes' if row.hosted else 'no'}</td><td>{_esc(row.circuit_state)}</td></tr>"
        for row in view.cloud
    )
    parts.append(
        '<section class="panel" aria-labelledby="cloud"><header><h2 id="cloud">Cloud lanes</h2></header>'
        '<div class="table-wrap"><table class="roster-table"><thead><tr><th>Provider</th><th>On</th><th>Limit</th>'
        f"<th>Hosted</th><th>Circuit</th></tr></thead><tbody>{cloud_rows}</tbody></table></div></section>"
    )
    # 4. consumer defaults
    default_cells = "".join(
        f'<label>{_esc(consumer)}{_select(f"default.{consumer}", defaults.get(consumer) or "", view.seats, editable=editable, binding="brigade_cli" if consumer == "brigade-run" else "t3_instance_id")}</label>'
        for consumer in CONSUMERS
    )
    parts.append(
        '<section class="panel" aria-labelledby="defaults"><header><h2 id="defaults">Consumer defaults</h2></header>'
        f'<div class="roster-grid">{default_cells}</div></section>'
    )
    # 5. retired
    retired_items = "".join(
        f"<li>{_esc(item['provider'])}/{_esc(item['family'])} &middot; "
        f"{'permanent' if item.get('permanent') else 'operator'} &middot; {_esc(item.get('reason_code'))}</li>"
        for item in view.retired
    )
    parts.append(
        '<section class="panel" aria-labelledby="retired"><header><h2 id="retired">Retired families</h2></header>'
        + (f'<ul class="observer-list">{retired_items}</ul>' if retired_items else '<p class="empty">None.</p>')
        + "</section>"
    )
    if editable:
        parts.append('<p class="roster-actions"><button type="submit">Save</button></p>')
    parts.append("</form></main>")
    return fleet_command_deck._document("\n".join(parts), nonce=nonce, now=now, title="Roster", refresh=False)
```

  (The parse and apply halves are added in Task 5; the module stays one
  file, well under the ceiling.)

- [ ] In `src/brigade/fleet_command_deck.py`, change `_document` to:

```python
def _document(body: str, *, nonce: str, now: datetime, title: str = "Command Deck", refresh: bool = True) -> str:
    refresh_tag = '<meta http-equiv="refresh" content="10">' if refresh else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{refresh_tag}"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#111617">'
        '<meta name="application-name" content="Fleet Hub">'
        '<meta name="apple-mobile-web-app-title" content="Fleet Hub">'
        '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
        '<link rel="manifest" href="/site.webmanifest">'
        f"<title>{_esc(title)}</title>"
        f'<style nonce="{_esc(nonce)}">{_STYLE}</style>'
        f'<script nonce="{_esc(nonce)}">{_SCRIPT}</script>'
        f'</head><body class="deck" data-as-of="{_esc(_stamp(now))}">{body}</body></html>'
    )
```

- [ ] In both nav strings (`render_deck` line 975 and `render_repos` line 1075) insert `<a href="/deck/roster">roster</a> ` before `<a href="/view/machines">machines board</a>`.
- [ ] Append to `_STYLE` (before the `@media` block):

```css
.roster-form label { display: grid; gap: 4px; color: var(--muted); font-size: 12px; }
.roster-form select, .roster-form textarea { width: 100%; padding: 6px; border: 1px solid var(--line); background: var(--surface-raised); color: var(--ink); font: inherit; }
.roster-form textarea { margin-top: 10px; }
.roster-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; }
.roster-table th:nth-child(n) { width: auto; }
.seat--off td { color: var(--faint); }
.banner { margin: 12px 0 0; padding: 10px; border: 1px solid var(--signal); color: var(--ink); font-size: 12px; }
.banner--error { border-color: #b0553a; }
.roster-actions button { padding: 8px 18px; border: 1px solid var(--signal); background: var(--signal-quiet); color: var(--ink); font: inherit; font-weight: 700; cursor: pointer; }
.panel + .panel { margin-top: 12px; }
```

- [ ] In `src/brigade/fleet_hub_http.py`, import the module (`from . import fleet_hub_roster_page`) and add this method to the handler class after `_serve_deck`:

```python
        def _roster_auth(self) -> tuple[bool, bool]:
            """``(authorized, editable)``: bearer/cookie edit; Tailscale identity reads."""
            if self._authorized() or self._cookie_authorized():
                return True, True
            if self._tailscale_identity_authorized():
                return True, False
            return False, False

        def _render_roster(
            self,
            *,
            status: int,
            editable: bool,
            saved_revision: int | None = None,
            error: str | None = None,
            submission: Any = None,
        ) -> None:
            plain = "text/plain; charset=utf-8"
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                view = fleet_hub_roster_page.load_view(conn, frozen_deck)
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            nonce = secrets.token_urlsafe(16)
            banner = f"saved as revision {view.revision}" if saved_revision == view.revision else None
            page = fleet_hub_roster_page.render(
                view,
                nonce=nonce,
                now=datetime.now(timezone.utc),
                csrf=fleet_hub_roster_page.csrf_value(token),
                editable=editable,
                banner=banner,
                error=error,
                submission=submission,
            )
            self._send_html(status, page, nonce=nonce)

        def _serve_roster(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            params = parse_qs(query, keep_blank_values=False)
            presented = params.pop("token", [""])[0]
            if presented:
                if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    self._send_html(401, "Unauthorized.\n", content_type=plain)
                    return
                cookie = (
                    f"{DASHBOARD_COOKIE}={dashboard_cookie_value(token)}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={DASHBOARD_COOKIE_MAX_AGE}"
                )
                self._send_html(
                    303, "", content_type=plain, extra_headers={"Location": "/deck/roster", "Set-Cookie": cookie}
                )
                return
            authorized, editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401,
                    "Unauthorized: send the fleet bearer token, or open this page once with "
                    "?token=<fleet token> to set the dashboard cookie.\n",
                    content_type=plain,
                )
                return
            saved = params.get("saved", [""])[0]
            saved_revision = int(saved) if saved.isdigit() and len(saved) <= 12 else None
            self._render_roster(status=200, editable=editable, saved_revision=saved_revision)
```

- [ ] In `do_GET`, insert before the `if path == "/" or path in ("/deck", "/deck/repos") ...` line:

```python
            if path == "/deck/roster":
                self._serve_roster(query)
                return
```

- [ ] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_fleet_roster_page.py tests/test_fleet_command_deck.py" --capture brigade-work`
  Expect PASS.
- [ ] Commit: `git add -A && git commit -m "feat(fleet): hub roster page at /deck/roster (read view)"`

### Task 5: form parse, apply transaction, and `POST /deck/roster`

**Files:**
- Modify: `src/brigade/fleet_hub_roster_page.py` (append)
- Modify: `src/brigade/fleet_hub_http.py` (`do_POST`, new `_post_roster`)
- Test: `tests/test_fleet_roster_page.py` (append)

- [ ] Append these tests to `tests/test_fleet_roster_page.py`:

```python
# --- POST ------------------------------------------------------------------


def test_roster_post_auth_csrf_origin_and_body_rules(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        good = _current_form(hub, cookie)
        assert _form(hub, good)[0] == 401
        node_token = _enroll_node(db)
        assert _form(hub, good, extra={"Authorization": f"Bearer {node_token}"})[0] == 403
        bad_csrf = {**good, "csrf": "0" * 64}
        assert _form(hub, bad_csrf, cookie=cookie)[0] == 403
        assert _form(hub, good, cookie=cookie, extra={"Sec-Fetch-Site": "cross-site"})[0] == 403
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
        assert _request(hub, "POST", "/deck/roster", headers=headers, body=b"{}")[0] == 415
        big = urlencode({**good, "notes": "x" * (64 * 1024)}).encode()
        headers = {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"}
        # The hub answers 413 from Content-Length without reading the body; the
        # peer may see the response or a reset socket, never a write.
        try:
            assert _request(hub, "POST", "/deck/roster", headers=headers, body=big)[0] == 413
        except (BrokenPipeError, ConnectionResetError):
            pass
        assert _form(hub, {**good, "expected_revision": "x"}, cookie=cookie)[0] == 400


def test_roster_post_tailscale_identity_cannot_write(tmp_path):
    with _hub(tmp_path, trust_tailscale=True) as (hub, _db):
        _seed(hub)
        cookie = _login_cookie(hub)
        good = _current_form(hub, cookie)
        before = _tables(_db)
        status, _headers, _text = _form(hub, good, extra={"Tailscale-User-Login": "operator@example.test"})
        assert status == 403
        assert _tables(_db) == before


def test_roster_post_stale_revision_and_stale_preference_write_nothing(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        # A CLI mutation lands between load and save.
        status, payload = _json(
            hub, "POST", "/models",
            {"action": "set", "seat": "coder", "enabled": True, "expected_revision": _revision(hub), **SEATS["coder"]},
        )
        assert status == 200, payload
        before = _tables(db)
        status, _headers, page = _form(hub, {**form, "role.security": "daybreak"}, cookie=cookie)
        assert status == 409
        assert "changed underneath you" in page
        assert _tables(db) == before
        form = _current_form(hub, cookie)
        status, payload = _json(hub, "PUT", "/preference", {"impl": "agy_flash"})
        assert status == 200, payload
        before = _tables(db)
        status, _headers, page = _form(hub, {**form, "role.security": "daybreak"}, cookie=cookie)
        assert status == 409
        assert "run preference changed" in page
        assert _tables(db) == before


def test_roster_post_applies_everything_in_one_revision(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        start = _revision(hub)
        form.pop("seat.cursor_grok")
        form.pop("seat.coder")
        form.pop("cloud.codex", None)
        form["cloud.claude"] = "1"
        form["role.impl"] = "agy_flash"
        form["role.review"] = ""
        form["role.chef"] = ""
        form["role.security"] = "daybreak"
        form["default.brigade-run"] = "agy_flash"
        form["notes"] = "cursor via Other Models only"
        status, headers, _text = _form(hub, form, cookie=cookie)
        assert status == 303
        assert headers["location"] == f"/deck/roster?saved={start + 1}"
        assert _revision(hub) == start + 1
        _status, _headers, models = _request(hub, "GET", "/models", headers=_bearer())
        roster = json.loads(models)
        enabled = {row["seat"]: row["enabled"] for row in roster["seats"]}
        assert enabled == {"agy_flash": True, "coder": False, "daybreak": True, "cursor_grok": False}
        assert roster["consumer_defaults"]["brigade-run"] == "agy_flash"
        _status, _headers, pref = _request(hub, "GET", "/preference", headers=_bearer())
        preference = json.loads(pref)["preference"]
        assert preference["impl"] == "agy_flash" and preference["security"] == "daybreak"
        assert preference["review"] is None and preference["notes"] == "cursor via Other Models only"
        _status, _headers, cloud = _request(hub, "GET", "/cloud", headers=_bearer())
        providers = {row["provider"]: row["enabled"] for row in json.loads(cloud)["policy"]["providers"]}
        assert providers["claude"] is True and providers["codex"] is False
        page = _request(hub, "GET", f"/deck/roster?saved={start + 1}", headers={"Cookie": cookie})[2]
        assert f"saved as revision {start + 1}" in page
        assert "by deck-form" in page
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT updated_by FROM run_preference WHERE id=1").fetchone()[0] == "deck-form"
        conn.close()
        # A no-op save leaves the revision alone.
        again = _current_form(hub, cookie)
        status, headers, _text = _form(hub, again, cookie=cookie)
        assert status == 303 and headers["location"] == f"/deck/roster?saved={start + 1}"
        assert _revision(hub) == start + 1


def test_roster_post_rejects_role_on_seat_disabled_in_same_save(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed(hub)
        cookie = _login_cookie(hub)
        form = _current_form(hub, cookie)
        form.pop("seat.daybreak")
        form["role.security"] = "daybreak"
        before = _tables(db)
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422
        assert "role security names seat daybreak" in page
        assert 'name="role.security"' in page and '<option value="daybreak" selected' in page
        assert _tables(db) == before
        # A consumer default needs the consumer's binding.
        form = _current_form(hub, cookie)
        form["default.t3-fleet"] = "agy_flash"
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422 and "no t3-fleet binding" in page
        # Notes still go through the secret regexes.
        form = _current_form(hub, cookie)
        form["notes"] = "see /home/operator/roster.toml"
        status, _headers, page = _form(hub, form, cookie=cookie)
        assert status == 422 and "home paths" in page
        assert "/home/operator" in page  # echoed back, escaped, so the operator can fix it
        assert _tables(db) == before
```

- [ ] Run, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_fleet_roster_page.py -k post" --capture brigade-work`
  Expect FAIL: `assert 404 == 401` (the POST route does not exist yet).
- [ ] Append to `src/brigade/fleet_hub_roster_page.py`:

```python
# --- form ------------------------------------------------------------------


def parse_form(raw: bytes) -> Submission:
    """Decode a form body. Size and content-type are enforced by the HTTP layer."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormError("form body is not UTF-8") from exc
    try:
        fields = parse_qs(text, keep_blank_values=True, max_num_fields=_MAX_FIELDS)
    except ValueError as exc:
        raise FormError("too many form fields") from exc
    first = {key: values[0] for key, values in fields.items() if values}
    try:
        expected = int(first.get("expected_revision", ""))
    except ValueError as exc:
        raise FormError("expected_revision must be an integer") from exc
    return Submission(
        expected_revision=expected,
        expected_preference_updated_at=first.get("expected_preference_updated_at", ""),
        csrf=first.get("csrf", ""),
        roles={role: first.get(f"role.{role}", "").strip() for role in ROLES},
        notes=first.get("notes", "").strip(),
        seats_on=frozenset(key[len("seat.") :] for key in fields if key.startswith("seat.")),
        cloud_on=frozenset(key[len("cloud.") :] for key in fields if key.startswith("cloud.")),
        defaults={consumer: first.get(f"default.{consumer}", "").strip() for consumer in CONSUMERS},
    )


# --- apply -----------------------------------------------------------------


def _utc_now() -> str:
    return fleet_hub._utc_now()


def _validate(view: RosterView, submission: Submission) -> tuple[str | None, dict[str, bool]]:
    """``(error, target_enabled)``; ``error`` is ``None`` when the save is admissible."""
    known = {row.seat: row for row in view.seats}
    target = {name: (name in submission.seats_on) and not row.retired for name, row in known.items()}
    for role, seat in submission.roles.items():
        if not seat:
            continue
        if seat not in known:
            return f"role {role} names unknown seat {seat}", target
        if not target[seat]:
            return f"role {role} names seat {seat}, which is disabled or retired in this save", target
    for consumer, seat in submission.defaults.items():
        if not seat:
            continue
        if seat not in known:
            return f"default {consumer} names unknown seat {seat}", target
        if not target[seat]:
            return f"default {consumer} names seat {seat}, which is disabled or retired in this save", target
        row = known[seat]
        bound = row.brigade_cli if consumer == "brigade-run" else row.t3_instance_id
        if not bound:
            return f"default {consumer} names seat {seat}, which has no {consumer} binding", target
    raw = {role: seat for role, seat in submission.roles.items() if seat}
    if submission.notes:
        raw["notes"] = submission.notes
    try:
        run_preference.parse_preference(raw)
    except run_preference.RunPreferenceError as exc:
        return str(exc), target
    return None, target


def _write_cloud_enabled(conn: sqlite3.Connection, row: CloudRow, enabled: bool) -> None:
    """Same upsert as ``fleet_hub._set_cloud_policy`` minus its commit; only ``enabled`` differs."""
    policy = row.policy
    conn.execute(
        "INSERT INTO cloud_provider_state (provider, enabled, limit_count, hosted, circuit_state, reason, "
        "subscription_pool, reset_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled, limit_count=excluded.limit_count, "
        "hosted=excluded.hosted, circuit_state=excluded.circuit_state, reason=excluded.reason, "
        "subscription_pool=excluded.subscription_pool, reset_at=excluded.reset_at, expires_at=excluded.expires_at, "
        "updated_at=excluded.updated_at",
        (
            row.provider,
            int(enabled),
            int(row.limit),
            int(row.hosted),
            row.circuit_state,
            policy.get("reason"),
            policy.get("subscription_pool"),
            policy.get("reset_at"),
            policy.get("expires_at"),
            _utc_now(),
        ),
    )


def apply(conn: sqlite3.Connection, config: fleet_command_deck.DeckConfig, submission: Submission) -> ApplyResult:
    """One Save: fences, validation, then only the differences, in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        view = load_view(conn, config)
        if view.revision != submission.expected_revision:
            conn.rollback()
            return ApplyResult(
                "conflict",
                f"roster changed underneath you: revision {submission.expected_revision} is now {view.revision}. "
                "Reload before saving.",
                view.revision,
            )
        if view.preference_updated_at != submission.expected_preference_updated_at:
            conn.rollback()
            return ApplyResult("conflict", "the run preference changed underneath you. Reload before saving.", view.revision)
        error, target = _validate(view, submission)
        if error is not None:
            conn.rollback()
            return ApplyResult("invalid", error, view.revision)
        roster_changed = False
        for row in view.seats:
            if row.retired or target[row.seat] == row.enabled:
                continue
            fleet_hub_model_roster._write_set(
                conn,
                {
                    "seat": row.seat,
                    "provider": row.provider,
                    "model": row.model,
                    "reasoning": row.reasoning,
                    "enabled": target[row.seat],
                    "limit": row.limit,
                    "brigade_cli": row.brigade_cli,
                    "t3_instance_id": row.t3_instance_id,
                    "t3_service_tier": row.t3_service_tier,
                    "notes": row.notes,
                },
            )
            roster_changed = True
        now = _utc_now()
        for consumer, seat in submission.defaults.items():
            if seat == (view.defaults.get(consumer) or ""):
                continue
            conn.execute(
                "INSERT INTO model_consumer_defaults (consumer, seat, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(consumer) DO UPDATE SET seat=excluded.seat, updated_at=excluded.updated_at",
                (consumer, seat, now),
            )
            roster_changed = True
        for row in view.cloud:
            want = row.provider in submission.cloud_on
            if want != row.enabled:
                _write_cloud_enabled(conn, row, want)
        wanted_pref = {role: seat for role, seat in submission.roles.items() if seat}
        if submission.notes:
            wanted_pref["notes"] = submission.notes
        current_pref = {key: value for key, value in view.preference.items() if value}
        if wanted_pref != current_pref:
            fleet_hub_preference.upsert_run_preference(conn, wanted_pref, updated_by=UPDATED_BY)
        revision = view.revision
        if roster_changed:
            revision += 1
            conn.execute(
                "UPDATE model_roster_meta SET revision=?, updated_at=?, updated_by=? WHERE singleton=1",
                (revision, now, UPDATED_BY),
            )
        conn.commit()
        return ApplyResult("saved", f"saved as revision {revision}", revision)
    except BaseException:
        conn.rollback()
        raise
```

- [ ] In `fleet_hub_http.py`, add to the handler class after `_serve_roster`:

```python
        def _same_origin(self) -> bool:
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None:
                return site.strip().lower() == "same-origin"
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            host = self.headers.get("Host", "")
            origin_host = origin.partition("://")[2].partition("/")[0]
            return bool(host) and hmac.compare_digest(origin_host.encode("utf-8"), host.encode("utf-8"))

        def _post_roster(self) -> None:
            plain = "text/plain; charset=utf-8"
            content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != fleet_hub_roster_page.FORM_CONTENT_TYPE:
                self._send_html(415, "Unsupported Media Type: send a form body.\n", content_type=plain)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_html(400, "bad Content-Length\n", content_type=plain)
                return
            if length <= 0:
                self._send_html(400, "missing body\n", content_type=plain)
                return
            if length > fleet_hub_roster_page.MAX_FORM_BYTES:
                self._send_html(413, "form body too large\n", content_type=plain)
                return
            presented = self._bearer()
            if presented is not None:
                # A bearer is checked against the database (node tokens) before the
                # body is read, exactly like the JSON routes.
                try:
                    conn = open_db(Path(db_path))
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                    return
                try:
                    if self._authorized():
                        is_admin = True
                    else:
                        node_id, _revoked = lookup_node_token(conn, presented)
                        is_admin = False
                        if node_id is None:
                            self._send_html(401, "Unauthorized.\n", content_type=plain)
                            return
                finally:
                    conn.close()
                if not is_admin:
                    self._send_html(403, "the admin token is required to edit the roster\n", content_type=plain)
                    return
            elif self._cookie_authorized():
                pass
            elif self._tailscale_identity_authorized():
                self._send_html(403, "read-only: enroll with the fleet token to edit\n", content_type=plain)
                return
            else:
                self._send_html(401, "Unauthorized.\n", content_type=plain)
                return
            if not self._same_origin():
                self._send_html(403, "cross-origin form post refused\n", content_type=plain)
                return
            raw = self.rfile.read(length)
            try:
                submission = fleet_hub_roster_page.parse_form(raw)
            except fleet_hub_roster_page.FormError as exc:
                self._send_html(400, f"{exc}\n", content_type=plain)
                return
            expected = fleet_hub_roster_page.csrf_value(token)
            if not hmac.compare_digest(submission.csrf.encode("utf-8"), expected.encode("utf-8")):
                self._send_html(403, "form token mismatch: reload the page\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                result = fleet_hub_roster_page.apply(conn, frozen_deck, submission)
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            if result.status == "saved":
                self._send_html(
                    303, "", content_type=plain, extra_headers={"Location": f"/deck/roster?saved={result.revision}"}
                )
                return
            status = 409 if result.status == "conflict" else 422
            self._render_roster(status=status, editable=True, error=result.message, submission=submission)
```

- [ ] In `do_POST`, insert as the first statement after `path = ...`:

```python
            if path == "/deck/roster":
                self._post_roster()
                return
```

- [ ] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_fleet_roster_page.py tests/test_fleet_command_deck.py tests/test_fleet_model_roster.py" --capture brigade-work`
  Expect PASS.
- [ ] Run the size ratchet:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_module_size_ratchet.py tests/test_size_ratchet_baseline.py" --capture brigade-work`
  Expect PASS. `fleet_hub_http.py` grows from 810 to roughly 970 lines and is not a legacy entry, so the ratchet stays green.
- [ ] Commit: `git add -A && git commit -m "feat(fleet): save the roster from /deck/roster in one revisioned transaction"`

### Task 6: `fleet_routing` block in `brigade work brief`

**Files:**
- Create: `src/brigade/work_cmd/session/fleet_routing.py`
- Modify: `src/brigade/work_cmd/session/briefing.py:553` (payload), `:702` (print)
- Test: `tests/test_work_cmd_session.py`

- [ ] Append these tests to `tests/test_work_cmd_session.py`:

```python
def test_work_brief_fleet_routing_from_hub(tmp_path, monkeypatch, capsys):
    from brigade import run_preference
    from brigade.work_cmd.session import fleet_routing

    _init_git_repo(tmp_path)
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(
        "brigade.run_preference.refresh_cache",
        lambda: run_preference.RunPreference(impl="agy_flash", security="daybreak", notes="cursor via Other Models only"),
    )
    monkeypatch.setattr(
        "brigade.fleet_client_cloud.load_model_policy_snapshot",
        lambda: {
            "state": "authoritative",
            "source": "hub",
            "revision": 16,
            "seats": [
                {"seat": "agy_flash", "enabled": True},
                {"seat": "cursor_grok", "enabled": False},
                {"seat": "coder", "enabled": False},
            ],
        },
    )
    monkeypatch.setattr(
        "brigade.fleet_client_cloud.fetch_cloud",
        lambda: {"policy": {"providers": [{"provider": "jules", "enabled": True}, {"provider": "codex", "enabled": False}]}},
    )
    payload = fleet_routing.fleet_routing_for_brief()
    assert payload["roles"] == {"impl": "agy_flash", "security": "daybreak"}
    assert payload["disabled_seats"] == ["coder", "cursor_grok"]
    assert payload["cloud_lanes"] == {"codex": False, "jules": True}
    assert payload["source"] == "hub" and payload["revision"] == 16
    assert work_cmd.brief(target=tmp_path, limit=2) == 0
    out = capsys.readouterr().out
    assert "fleet_routing: impl=agy_flash security=daybreak" in out
    assert "fleet_routing_notes: cursor via Other Models only" in out
    assert "fleet_disabled_seats: coder, cursor_grok" in out
    assert "fleet_cloud_lanes: codex=off jules=on" in out
    assert "fleet_routing_source: hub revision 16" in out
    assert out.index("latest_run:") < out.index("fleet_routing:") < out.index("next_source:")


def test_work_brief_fleet_routing_hub_down_uses_cache(tmp_path, monkeypatch, capsys):
    from brigade import run_preference
    from brigade.work_cmd.session import fleet_routing

    _init_git_repo(tmp_path)
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.test")
    monkeypatch.setattr("brigade.run_preference.refresh_cache", lambda: run_preference.RunPreference(scout="cursor_scout"))
    monkeypatch.setattr(
        "brigade.fleet_client_cloud.load_model_policy_snapshot",
        lambda: {"state": "authoritative", "source": "lkg", "revision": 15, "seats": [{"seat": "coder", "enabled": False}]},
    )

    def down():
        raise fleet_client.FleetClientError("hub down")

    monkeypatch.setattr("brigade.fleet_client_cloud.fetch_cloud", down)
    monkeypatch.setattr("brigade.fleet_model_admission._load_lkg_record", lambda: {"cached_at": "2026-09-02T00:00:00Z"})
    payload = fleet_routing.fleet_routing_for_brief()
    assert payload["source"] == "lkg" and payload["cloud_lanes"] == {}
    assert payload["cached_at"] == "2026-09-02T00:00:00Z"
    assert work_cmd.brief(target=tmp_path, limit=2) == 0
    out = capsys.readouterr().out
    assert "fleet_routing: scout=cursor_scout" in out
    assert "fleet_cloud_lanes: unknown (hub unreachable)" in out
    assert "fleet_routing_source: cache revision 15 (hub unreachable, cached 2026-09-02T00:00:00Z)" in out


def test_work_brief_fleet_routing_budget_and_unconfigured(tmp_path, monkeypatch, capsys):
    import time

    from brigade import run_preference
    from brigade.work_cmd.session import fleet_routing

    _init_git_repo(tmp_path)
    assert fleet_routing.fleet_routing_for_brief() is None  # conftest strips the hub
    assert work_cmd.brief(target=tmp_path, limit=2) == 0
    assert "fleet_routing" not in capsys.readouterr().out
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.test")
    monkeypatch.setattr(fleet_routing, "BUDGET_SECONDS", 0.2)

    def slow():
        time.sleep(2)
        return {"state": "authoritative", "source": "hub", "revision": 1, "seats": []}

    monkeypatch.setattr("brigade.run_preference.refresh_cache", lambda: run_preference.RunPreference())
    monkeypatch.setattr("brigade.fleet_client_cloud.load_model_policy_snapshot", slow)
    monkeypatch.setattr("brigade.fleet_client_cloud.fetch_cloud", lambda: {"policy": {"providers": []}})
    started = time.monotonic()
    payload = fleet_routing.fleet_routing_for_brief()
    assert time.monotonic() - started < 1.5
    assert payload["source"] == "unavailable"
```

- [ ] Run, watch it fail:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_work_cmd_session.py -k fleet_routing" --capture brigade-work`
  Expect FAIL: `ImportError: cannot import name 'fleet_routing' from 'brigade.work_cmd.session'`.
- [ ] Create `src/brigade/work_cmd/session/fleet_routing.py`:

```python
"""``fleet_routing`` block for ``brigade work brief`` (hub roster page spec).

The Claude SessionStart hook injects the brief, so this is how every
interactive session learns the pinned roles, disabled seats, and cloud lanes
without being told. Bounded to ``BUDGET_SECONDS`` and never raises: a down
hub prints the cache, an unconfigured fleet prints nothing.
"""

from __future__ import annotations

from typing import Any

BUDGET_SECONDS = 3.0
_KEYS = ("roles", "notes", "disabled_seats", "cloud_lanes", "source", "revision", "cached_at")


def _pull() -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
    from ... import fleet_client_cloud, run_preference

    preference = run_preference.refresh_cache()
    snapshot = fleet_client_cloud.load_model_policy_snapshot()
    try:
        cloud = fleet_client_cloud.fetch_cloud()
    except Exception:  # noqa: BLE001 - lanes are optional under a down hub
        cloud = None
    return preference, snapshot, cloud


def fleet_routing_for_brief() -> dict[str, Any] | None:
    """The block, or ``None`` when no fleet hub is configured."""
    from ... import fleet_client, run_preference

    if not fleet_client.load_fleet_settings()["hub_url"]:
        return None
    result: dict[str, Any] = {
        "roles": {},
        "notes": None,
        "disabled_seats": [],
        "cloud_lanes": {},
        "source": "unavailable",
        "revision": None,
        "cached_at": None,
    }
    try:
        preference, snapshot, cloud = fleet_client._run_with_deadline(_pull, timeout=BUDGET_SECONDS)
    except Exception:  # noqa: BLE001 - brief stays fail-open
        preference, snapshot, cloud = run_preference.load_cached(), {"state": "unavailable"}, None
    result["roles"] = {role: value for role in run_preference.ROLE_FIELDS if (value := getattr(preference, role))}
    result["notes"] = preference.notes
    if snapshot.get("state") == "authoritative":
        result["source"] = str(snapshot.get("source") or "hub")
        result["revision"] = snapshot.get("revision")
        result["disabled_seats"] = sorted(
            str(row.get("seat"))
            for row in snapshot.get("seats") or []
            if isinstance(row, dict) and row.get("seat") and row.get("enabled") is not True
        )
        if result["source"] == "lkg":
            try:
                from ... import fleet_model_admission

                result["cached_at"] = fleet_model_admission._load_lkg_record().get("cached_at")
            except Exception:  # noqa: BLE001
                result["cached_at"] = None
    if isinstance(cloud, dict):
        providers = (cloud.get("policy") or {}).get("providers") or []
        result["cloud_lanes"] = {
            str(row["provider"]): bool(row.get("enabled"))
            for row in providers
            if isinstance(row, dict) and row.get("provider")
        }
    return result


def print_fleet_routing(block: dict[str, Any]) -> None:
    roles = block.get("roles") or {}
    rendered = " ".join(f"{role}={seat}" for role, seat in roles.items()) or "(unset)"
    print(f"fleet_routing: {rendered}")
    print(f"fleet_routing_notes: {block.get('notes') or '(none)'}")
    disabled = block.get("disabled_seats") or []
    print(f"fleet_disabled_seats: {', '.join(disabled) if disabled else '(none)'}")
    lanes = block.get("cloud_lanes") or {}
    if lanes:
        print("fleet_cloud_lanes: " + " ".join(f"{name}={'on' if on else 'off'}" for name, on in sorted(lanes.items())))
    else:
        print("fleet_cloud_lanes: unknown (hub unreachable)")
    source = block.get("source")
    revision = block.get("revision")
    if source == "hub":
        print(f"fleet_routing_source: hub revision {revision}")
    elif source == "lkg":
        print(f"fleet_routing_source: cache revision {revision} (hub unreachable, cached {block.get('cached_at') or 'unknown'})")
    else:
        print("fleet_routing_source: unavailable (hub unreachable, no cache)")
```

- [ ] In `briefing.py`, add the payload key. At line 553 (inside the `_brief_payload` return dict, next to `"cloud_tracker": ...`) add:

```python
        "fleet_routing": _fleet_routing_for_brief(),
```

  and near `_cloud_tracker_for_brief` add:

```python
def _fleet_routing_for_brief() -> dict[str, Any] | None:
    try:
        from . import fleet_routing

        return fleet_routing.fleet_routing_for_brief()
    except Exception:  # noqa: BLE001 - brief stays fail-open
        return None
```

- [ ] In `brief()`, immediately after the `else: print("latest_run: none")` block (line 702), add:

```python
    routing = payload.get("fleet_routing")
    if isinstance(routing, dict):
        from . import fleet_routing

        fleet_routing.print_fleet_routing(routing)
```

- [ ] Run to green:
  `brigade work verify run --target . --command "python -m pytest -q tests/test_work_cmd_session.py" --capture brigade-work`
  Expect PASS. `test_work_brief_json_compacts_heavy_report_and_fleet_health` must still pass; `fleet_routing` is `None` there because conftest strips the hub, and `json.dumps` handles `None`.
- [ ] Commit: `git add -A && git commit -m "feat(work): print the fleet routing pin in the work brief"`

### Task 7: docs, full verification, changelog

**Files:**
- Modify: `docs/fleet-sync.md:102-106` (preference bullets), append a roster page section
- Modify: `docs/runbooks/fleet-hub-proxmox.md:18-27` (endpoint list)
- Modify: `CHANGELOG.md` (Unreleased section, follow its existing format)

- [ ] In `docs/fleet-sync.md`, change the `GET /preference` bullet to list `impl`, `review`, `chef`, `research`, `security`, `scout`, `notes`, and add after the `PUT /preference` bullet:

```markdown
- `GET /deck/roster`, `POST /deck/roster` — the hub roster page (spec:
  `docs/phase-fleet-roster-deck-page.md`). Read with the admin bearer, the
  dashboard cookie, or a trusted Tailscale identity (read-only). Save with
  the admin bearer or the dashboard cookie only; the form carries a CSRF
  token derived from the admin token, the roster revision, and the
  preference row's `updated_at`. One Save is one `BEGIN IMMEDIATE`
  transaction: seat on/off and consumer defaults bump the roster revision
  once; roles, notes, and cloud lane on/off do not touch the revision.
  Schema v19 adds the three role columns.
```

  Update the `PRAGMA user_version` bullet to mention v19.
- [ ] In `docs/runbooks/fleet-hub-proxmox.md`, add to the endpoint list:

```markdown
- `GET /deck/roster` and `POST /deck/roster`, the roster page (admin token or the dashboard cookie to save; see `docs/fleet-sync.md`)
```

  and in the upgrade section note that v19 adds columns to `run_preference` and the section 8 backup precedes the rollout.
- [ ] In `CHANGELOG.md`, add this as the first bullet under `## [Unreleased]` / `### Added` (Keep a Changelog style, one bullet): "Fleet hub roster page at `/deck/roster`: roles (impl, review, chef, research, security, scout), seat and cloud lane toggles, consumer defaults, and notes saved in one revisioned transaction; `brigade work brief` prints the `fleet_routing` block; `fleet preference set` gains `--research`, `--security`, `--scout`; hub schema v19."
- [ ] Full verification:
  `brigade work verify run --target . --command "python -m pytest -q -x tests/test_run_preference.py tests/test_fleet_roster_page.py tests/test_fleet_command_deck.py tests/test_fleet_model_roster.py tests/test_fleet_cloud.py tests/test_work_cmd_session.py tests/test_module_size_ratchet.py tests/test_size_ratchet_baseline.py tests/test_fleet_model_admission.py" --capture brigade-work`
  Expect PASS.
- [ ] Lint and types, through Brigade: `brigade work verify run --target . --command "ruff check src tests" --capture brigade-work` and `brigade work verify run --target . --command ".venv/bin/mypy src/brigade/fleet_hub_roster_page.py src/brigade/work_cmd/session/fleet_routing.py src/brigade/fleet_hub_preference.py src/brigade/run_preference.py" --capture brigade-work` (mypy lives in the repo `.venv`). Expect exit 0 for both.
- [ ] Commit: `git add -A && git commit -m "docs(fleet): roster page, schema v19, and fleet_routing brief block"`
- [ ] Write the Memory Handoff for the session (`.claude/memory-handoffs/`), then open the PR with `skillet:pass`.

## Rollout (after merge)

1. `pipx install --force` from a detached `origin/main` worktree on the workstation (see memory: pipx refresh from origin/main worktree).
2. Hub: back up `/var/lib/brigade/fleet-hub.db` (runbook section 8), refresh the CT's pipx install, `systemctl restart brigade-fleet.service`.
3. Open `https://<hub>/deck/roster?token=<admin token>` once to set the cookie, set roles and toggles to the spoken policy, Save.
4. Start a fresh Claude session in a wired repo and confirm the brief shows `fleet_routing_source: hub revision 16` or later.
