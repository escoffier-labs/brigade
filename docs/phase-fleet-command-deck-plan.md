# Fleet Command Deck implementation plan

## Goal

Add the read-only, server-rendered Command Deck at `/deck` and `/deck/repos` without changing schema, dependencies, `/`, legacy boards, or the rollout runbook. Execute the three tasks in order, completing every checkbox before beginning the next task.

## Architecture

`fleet_command_deck.py` is independent of `fleet_hub.py`: it owns immutable config, bounded SQLite reads, pure projections, and HTML. `fleet_hub.py` imports it, loads config exactly once in `make_server`, and only supplies authentication, connections, routing, and headers. Result caps bound response size, while the whole-history latest-state projection remains issue #1146.

## File map

| File | Responsibility |
| --- | --- |
| Create `src/brigade/fleet_command_deck.py` | Config, projections, capped reads, pure HTML. |
| Modify `src/brigade/fleet_hub.py` | Startup config load and authenticated deck routing. |
| Modify `src/brigade/cli/fleet.py` | `--deck-config` parser and dispatch wiring. |
| Create `tests/test_fleet_command_deck.py` | Pure, SQLite, HTTP, CLI, header, secret, responsive, and legacy regressions. |

### Task 1: independent deck module and its red-to-green tests

**Files:**

- Create: `src/brigade/fleet_command_deck.py`
- Create: `tests/test_fleet_command_deck.py`

- [x] Write the failing pure and SQLite tests first. Use fake IDs only and one `now` value.

```python
def test_load_config_validates_and_strips_controls(tmp_path):
    path = tmp_path / "deck.json"
    path.write_text(json.dumps({"stations": [{"node_id": NODE_A, "name": "Alpha\x00", "capacity": 6}]}))
    assert deck.load_config(path).stations == (deck.StationConfig(NODE_A, "Alpha", 6),)
    for raw in ({}, {"stations": []}, {"stations": [{"node_id": "unknown", "name": "A", "capacity": 1}]}):
        path.write_text(json.dumps(raw))
        with pytest.raises(deck.DeckConfigError):
            deck.load_config(path)

def test_bucket_and_collision_rules():
    assert deck.bucket_for("run.dispatch.failed", age_seconds=1, stale_after_seconds=1800) == "failed"
    assert deck.bucket_for("run.paused", age_seconds=3600, stale_after_seconds=1800) == "awaiting approval"
    assert deck.bucket_for("run.created", age_seconds=1, stale_after_seconds=1800) == "queued"
    rows = [deck.LiveRun(NODE_A, "a", "repo", "", "", "run.created", "queued", 1, 1),
            deck.LiveRun(NODE_B, "b", "repo", "", "", "run.created", "queued", 1, 1)]
    assert deck.collides("repo", rows, {"repo": deck.Claim("repo", NODE_A, "", 60)}) is True

def test_capped_queries_select_latest_then_limit(conn):
    seed_events(conn, terminal_then_older_live=True, extra_live=201, extra_terminal=101)
    assert len(deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)) == 200
    assert len(deck.fetch_outcomes(conn, outcome_window=100)) == 100
    assert all(row.state not in deck.TERMINAL_STATES for row in deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800))
```

- [x] Run red: `pytest -q tests/test_fleet_command_deck.py -k 'load_config or bucket or capped_queries'`. Expect collection failure: `ModuleNotFoundError: No module named 'brigade.fleet_command_deck'`.

- [x] Implement `fleet_command_deck.py` with only stdlib imports (`html`, `json`, `re`, `sqlite3`, dataclasses, datetime, pathlib, typing). It must not import `fleet_hub`.

```python
class DeckConfigError(ValueError):
    pass

@dataclass(frozen=True)
class StationConfig:
    node_id: str
    name: str
    capacity: int

@dataclass(frozen=True)
class DeckConfig:
    stations: Sequence[StationConfig] = ()
    stale_after_seconds: int = 1800
    outcome_window: int = 20
    failed_lookback_seconds: int = 86400

def resolve_config_path(flag_value: str | Path | None, environ: Mapping[str, str]) -> Path | None:
    value = flag_value if flag_value is not None else environ.get("BRIGADE_FLEET_DECK_CONFIG")
    return Path(value).expanduser() if isinstance(value, (str, Path)) and str(value).strip() else None

def load_config(path: Path) -> DeckConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeckConfigError(f"invalid deck config: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("stations"), list):
        raise DeckConfigError("deck config requires a stations array")
    stations = tuple(_station(item) for item in raw["stations"])
    if not 1 <= len(stations) <= 16 or len({item.node_id for item in stations}) != len(stations):
        raise DeckConfigError("deck config requires 1..16 unique stations")
    return DeckConfig(
        stations=stations,
        stale_after_seconds=_bounded_int(raw, "stale_after_seconds", 1800, 300, 21600),
        outcome_window=_bounded_int(raw, "outcome_window", 20, 1, 100),
        failed_lookback_seconds=_bounded_int(raw, "failed_lookback_seconds", 86400, 1, 2_592_000),
    )
```

Add `_station(raw: object) -> StationConfig` and `_bounded_int(raw: Mapping[str, object], key: str, default: int, low: int, high: int) -> int`. `_station` rejects non-dicts, validates `node_id` with `CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")`, rejects `unknown`, strips a supplied `name` with `re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value).strip()`, and requires its supplied value to be 1..64 characters. An absent `name` becomes `""` so enrollment-label fallback is possible. Require `type(capacity) is int` in 1..64. `_bounded_int` uses its default only for a missing key and otherwise requires `type(value) is int` in the stated inclusive range. Each rejection raises `DeckConfigError` with the field name.

- [x] Add these exact constants and pure shapes. They make all render input immutable after construction.

```python
TERMINAL_STATES = frozenset({"run.completed", "run.failed", "run.interrupted"})
AWAITING_STATES = frozenset({"run.paused", "approval.requested", "approval.held"})
ACTIVE_LIMIT, OBSERVER_LIMIT, RAIL_FAILURE_LIMIT = 200, 8, 10
BUCKET_RANK = {"failed": 0, "awaiting approval": 1, "stale": 2, "running": 3, "queued": 4}

@dataclass(frozen=True)
class LiveRun:
    node_id: str; run_id: str; repo: str; seat: str; harness: str; state: str; bucket: str; age_seconds: int | None; elapsed_seconds: int | None

@dataclass(frozen=True)
class Claim:
    target: str; owner_node: str; owner_conductor: str; ttl_remaining: int
```

`bucket_for(state: str, *, age_seconds: int | None, stale_after_seconds: int) -> str`

`fetch_live_runs(conn: sqlite3.Connection, *, now: datetime, stale_after_seconds: int) -> list[LiveRun]`

`fetch_started_at(conn: sqlite3.Connection, keys: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]`

`fetch_outcomes(conn: sqlite3.Connection, *, outcome_window: int) -> list[LiveRun]`

`fetch_failed_outcomes(conn: sqlite3.Connection, *, now: datetime, lookback_seconds: int) -> list[LiveRun]`

`fetch_last_heard(conn: sqlite3.Connection, node_ids: Sequence[str]) -> dict[str, str]`

`fetch_observers(conn: sqlite3.Connection, configured: frozenset[str]) -> list[tuple[str, str]]`

`collides(target: str, live_runs: Sequence[LiveRun], claims: Mapping[str, Claim]) -> bool`

- [x] Implement bounded reads exactly. The latest-row CTE partitions by `(node_id, run_id)` ordered `sequence DESC, received_at DESC, digest DESC`. Only after `rn = 1` exclude exact terminal states. Use `ORDER BY` bucket rank, `ts DESC`, node, run and `LIMIT 200`. Fetch starts only for selected `(node_id, run_id)` keys in chunks of at most 400 pairs. Fetch terminal outcomes as latest rows ordered `ts DESC LIMIT ?`. Failures use exact `state = 'run.failed'`, `ts >= cutoff`, newest first, `LIMIT 10`. Restrict last-heard aggregation to configured IDs. Observers are unconfigured observed IDs ordered by `MAX(received_at) DESC LIMIT 8`. Do not claim issue #1146 is solved.

- [x] Implement projections and renderers with these public entry points. Escape every inserted value with `html.escape(value, quote=True)`, strip controls from config names and enrollment labels before escaping, and never include token, cookie, authorization, or holder fields.

```python
`build_view(config: DeckConfig, *, live_runs: Sequence[LiveRun], claims: Sequence[Claim], enrolled_labels: Mapping[str, str], last_heard: Mapping[str, str], outcomes: Sequence[LiveRun], failed_outcomes: Sequence[LiveRun], observers: Sequence[tuple[str, str]], now: datetime) -> DeckView`

`render_deck(view: DeckView, *, nonce: str, now: datetime) -> str`

`render_repos(view: DeckView, *, nonce: str, now: datetime) -> str`
```

`build_view` preserves `config.stations` order, renders a grey `not enrolled` station when an ID is absent from enrolled labels, counts only configured stations, assigns tiles by station, excludes exact terminal tiles, sorts tiles by `BUCKET_RANK`, and builds collision-first repo rows from the union of active claim targets and live repos. Its rail order is live failed, live awaiting approval, live stale, then collisions, followed by terminal failures. `render_deck` emits a nonce-bearing style and elapsed-only script, refresh meta tag, `main`, labelled station/rail/timeline sections, native `details`, both navigation links, classic-board links, zero-station hint, and the required empty strings. `render_repos` emits the same document shell and collision-first table.

- [x] Run green: `pytest -q tests/test_fleet_command_deck.py -k 'load_config or bucket or capped_queries'`. Expect `3 passed`.
- [x] Commit: `git add src/brigade/fleet_command_deck.py tests/test_fleet_command_deck.py && git commit -m "feat: add fleet command deck projections"`.

### Task 2: startup-frozen config, authenticated routes, and integrations

**Files:**

- Modify: `src/brigade/fleet_hub.py`
- Modify: `src/brigade/cli/fleet.py`
- Modify: `tests/test_fleet_command_deck.py`

- [x] Add one test helper, and use it for every test configuration. Each caller writes the selected JSON before entering this manager. Do not mutate a config file while a server is running.

```python
@contextmanager
def _start_hub(tmp_path, config: dict | None):
    db = tmp_path / "hub" / "fleet.db"
    config_path = None
    if config is not None:
        config_path = tmp_path / "deck.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
    server = fleet_hub.make_server("127.0.0.1", 0, db, TOKEN, allow_admin_writes=True, deck_config_path=config_path)  # content-guard: allow loopback-ipv4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", server.server_address[1]), db  # content-guard: allow loopback-ipv4
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
```

- [x] Add and run these failing HTTP, security, secret, config, CLI, and legacy tests. Each call of the `_start_hub` context manager receives a distinct `dict` and therefore creates a separately started server.

```python
def test_deck_requires_bearer_or_cookie(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _):
        assert _request(hub, "GET", "/deck")[0] == 401
        assert _request(hub, "GET", "/deck/repos", headers=_bearer())[0] == 200
        cookie = _login_cookie(hub, "/deck")
        assert _request(hub, "GET", "/deck", headers={"Cookie": cookie})[0] == 200
        assert _request(hub, "GET", "/status", headers={"Cookie": cookie})[0] == 401

def test_config_is_loaded_once_and_routes_keep_legacy_pages(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _):
        assert "Alpha" in _request(hub, "GET", "/deck", headers=_bearer())[2]
        for path, title in (("/", "Fleet: Machines"), ("/view/machines", "Fleet: Machines"), ("/view/repos", "Fleet: Repos")):
            assert title in _request(hub, "GET", path, headers=_bearer())[2]

def test_headers_secrets_and_responsive_css(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, _):
        status, headers, body = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200 and headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert all(secret not in body for secret in (TOKEN, HOLDER_TOKEN, _login_cookie(hub).split("=", 1)[1]))
        assert "@media (max-width: 700px)" in body and "grid-template-columns: 1fr" in body
```

- [x] Run red: `pytest -q tests/test_fleet_command_deck.py -k 'deck_requires or loaded_once or headers_secrets'`. Expect failures because `make_server()` lacks `deck_config_path` and `/deck` is not routed.

- [x] In `fleet_hub.py`, add `from . import fleet_command_deck` beside the dashboard import. Extend exactly these signatures and capture immutable config in the handler closure:

```python
`make_handler(token: str, db_path: Path, *, allow_admin_writes: bool = False, deck_config: fleet_command_deck.DeckConfig | None = None) -> type[BaseHTTPRequestHandler]`

`make_server(host: str, port: int, db_path: Path, token: str, *, allow_admin_writes: bool = False, deck_config_path: Path | None = None) -> ThreadingHTTPServer`

`run(*, host: str | None, port: int, db_path: Path, token_file: Path | None, allow_admin_writes: bool = False, deck_config_path: Path | None = None) -> int`
```

Inside `make_server`, call `fleet_command_deck.load_config(deck_config_path)` when path is present, else create `DeckConfig()`, before binding the socket. Pass that object to `make_handler`. Never reread the path. In `run`, include `fleet_command_deck.DeckConfigError` in the startup `except`, print its existing error form, and return 2.

- [x] Add `_login_cookie(hub, path: str = "/")` in the test file so it sends `GET f"{path}?token={TOKEN}"`, then use it to cover `/deck` enrollment and cookie-only rendering.

- [x] Add `_serve_deck(path, query)` adjacent to `_serve_dashboard`. Copy the legacy `?token=` enrollment, redirect, bearer-or-cookie authorization, and `_send_html` path byte-for-byte except that it accepts only `/deck` and `/deck/repos, opens a request-local database connection, invokes the deck capped helpers plus `list_claims()` and `list_nodes()`, then renders `render_deck` or `render_repos`. Pass only unrevoked rows from `list_nodes()` into the enrollment-label mapping. Ignore all non-token deck query parameters instead of reflecting them. Route `/health`, then exact deck paths, then legacy `/` and `/view/*`. Leave every legacy route unchanged. Do not add POST handling.

- [x] In `cli/fleet.py`, import `os` and `Mapping`, add `p_serve.add_argument("--deck-config", type=Path, default=None, help="Command Deck JSON config (else BRIGADE_FLEET_DECK_CONFIG).")`, and dispatch `deck_config_path=fleet_command_deck.resolve_config_path(args.deck_config, os.environ)`. Add CLI tests asserting flag precedence, nonempty environment fallback, blank environment fallback to `None`, and forwarding to `fleet_hub.run`.

- [x] Add coverage for invalid config refusal at `make_server`, fixed station order, enrollment label fallback, capacity and over marker, terminal-only timeline, observers excluded from totals, bounded timeline, collision marker in tile/rail/repo-first row, escaped hostile labels, no secrets for bearer and cookie on both deck routes, and no-config zero-station hint. Seed claims through `/claims`. Assert expired claims are absent.

- [x] Run green: `pytest -q tests/test_fleet_command_deck.py tests/test_fleet_dashboard.py tests/test_fleet_sync.py tests/test_fleet_nodes.py`. Expect all selected tests passed.
- [ ] Commit: `git add src/brigade/fleet_hub.py src/brigade/cli/fleet.py tests/test_fleet_command_deck.py && git commit -m "feat: serve fleet command deck"`.

### Task 3: responsive assertions, verification, and private CT operations

**Files:**

- Modify: `src/brigade/fleet_command_deck.py`
- Modify: `tests/test_fleet_command_deck.py`

- [ ] Add the final red assertion to the HTML test:

```python
assert "overflow-wrap: anywhere" in body
assert "table-layout: fixed" in body
assert "@media (max-width: 700px)" in body
assert ".stations { grid-template-columns: 1fr; }" in body
```

- [ ] Run red: `pytest -q tests/test_fleet_command_deck.py -k responsive`. Expect `AssertionError` until all four CSS guarantees are present.
- [ ] Add these literal rules to the one shared stylesheet used by `render_deck` and `render_repos`: `box-sizing: border-box`, `.deck { max-width: 100%; overflow-wrap: anywhere; }`, `.stations { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }`, `.tile { min-width: 0; }`, `table { width: 100%; table-layout: fixed; }`, and `@media (max-width: 700px) { .stations { grid-template-columns: 1fr; } .tile { width: 100%; } }`. Do not introduce static assets, JavaScript fetches, dependencies, schema edits, or runbook edits.
- [ ] Run green through Brigade: `brigade work verify run --target . --command "pytest -q tests/test_fleet_command_deck.py tests/test_fleet_dashboard.py tests/test_fleet_sync.py tests/test_fleet_nodes.py" --capture brigade-work`. Expect exit 0 and a recorded passed receipt.
- [ ] Run the completion gate through Brigade with a 3600-second command timeout: `brigade work verify run --target . --command "timeout 3600 ./scripts/verify" --capture brigade-work`. Expect exit 0 and `verification passed`.
- [ ] Commit: `git add src/brigade/fleet_command_deck.py tests/test_fleet_command_deck.py && git commit -m "test: cover command deck responsive layout"`.

## Private CT deploy, canary, and rollback commands

Run these only after the implementation commits are reviewed. Substitute only the uppercase placeholder values. Do not put any private values in this repository.

```bash
ssh <PRIVATE_CT_SSH_TARGET> 'sudo install -d -m 0700 /etc/brigade /etc/systemd/system/brigade-fleet.service.d'
scp <LOCAL_COMMAND_DECK_CONFIG_JSON> <PRIVATE_CT_SSH_TARGET>:/tmp/command-deck.json
ssh <PRIVATE_CT_SSH_TARGET> 'sudo install -m 0600 /tmp/command-deck.json /etc/brigade/command-deck.json && rm /tmp/command-deck.json'
printf '%s\n' '[Service]' 'Environment=BRIGADE_FLEET_DECK_CONFIG=/etc/brigade/command-deck.json' | ssh <PRIVATE_CT_SSH_TARGET> 'sudo tee /etc/systemd/system/brigade-fleet.service.d/10-command-deck.conf >/dev/null'
ssh <PRIVATE_CT_SSH_TARGET> 'sudo systemctl daemon-reload && sudo systemctl restart brigade-fleet.service && sudo systemctl is-active --quiet brigade-fleet.service'
curl --fail --silent --show-error http://<PRIVATE_TAILNET_HOST>:<PRIVATE_PORT>/health
curl --fail --silent --show-error --header 'Authorization: Bearer <PRIVATE_FLEET_TOKEN>' http://<PRIVATE_TAILNET_HOST>:<PRIVATE_PORT>/deck >/dev/null
ssh <PRIVATE_CT_SSH_TARGET> 'sudo journalctl -u brigade-fleet.service --since "24 hours ago" --no-pager'
```

Use `/deck` beside the legacy boards for one normal working day. Verify three configured station names and capacities, no phone overflow, collision and attention entries, and no 500 lines in the service journal. Roll back by restoring the previously reviewed private ref using the existing private ref-swap procedure, remove only the deck-specific drop-in, then run:

```bash
ssh <PRIVATE_CT_SSH_TARGET> 'sudo rm /etc/systemd/system/brigade-fleet.service.d/10-command-deck.conf && sudo systemctl daemon-reload && sudo systemctl restart brigade-fleet.service && sudo systemctl is-active --quiet brigade-fleet.service'
curl --fail --silent --show-error http://<PRIVATE_TAILNET_HOST>:<PRIVATE_PORT>/health
```
