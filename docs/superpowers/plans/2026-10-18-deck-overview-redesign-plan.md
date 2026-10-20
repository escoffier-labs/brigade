# Command Deck overview redesign implementation plan

Spec: `docs/superpowers/specs/2026-10-18-deck-overview-redesign.md` (approved 2026-10-18).

**Goal:** turn `/deck` into a one-screen "what is the fleet doing, and does anything need me" overview (sidebar machine tree, four summary cards, alerts, working and queued lists, machines table), with a light theme that follows the system.

**Architecture:** two new modules, `fleet_deck_overview.py` (pure render functions over `DeckView`, no database access) and `fleet_deck_shell.py` (the two-column page shell), sit beside `fleet_command_deck.py`, which only loses code. `ui_theme.py` gains light tokens, the shared shell CSS, and the overview CSS. The routing snapshot gains an additive `node_id` per machine so stations join to machines by node id, never by label.

**Tech:** Python 3.10+ standard library, `ui_theme.document`, pytest, ruff (line length 120), mypy, headless Chrome for captures.

**Commit trailers:** the commit commands below omit the trailer line. Every commit message ends with the repository's approved agent co-author trailer.

**For agentic workers:** execute task by task and tick the `- [ ]` checkboxes. Commit at each task's last step. Run every check through `brigade work verify run --target . --command "<check>" --capture brigade-work`, then read the result from the newest `.brigade/work/verify-runs/*/command-1-stdout.log`. Never run pytest, ruff, or mypy raw. Line ranges below refer to base commit `da9af41e8`; earlier tasks shift them, so locate each edit by the quoted anchor text. Content guard: no real host names, personal names, or home paths in any tracked file; fixtures use `linux-a`, `linux-b`, `win-a`.

**Reading a verify result:** after each wrapper call run

```bash
tail -n 40 "$(ls -dt .brigade/work/verify-runs/*/ | head -1)command-1-stdout.log"
```

## Spec deviations

Each item picks the closest faithful option where the code contradicts the spec or leaves a gap the plan had to close.

1. **Stale "since" has no TTL.** The spec derives the stale-telemetry alert time as `last_observed_at` plus the policy TTL. The routing snapshot does not carry `telemetry_ttl_seconds` (`src/brigade/fleet_hub_routing.py:1692-1703` emits only the machine fields, and `ControlPlane.policy` holds version, digest, and update time at `src/brigade/fleet_command_deck.py:272-278`). Adding it would widen the snapshot beyond the one allowed `node_id` key. The alert therefore reads `last telemetry <age> ago` from `last_observed_at`, and says `no telemetry received` when that is unreadable.
2. **Attention alerts need an age the rail does not carry.** `RailEntry` has no time (`fleet_command_deck.py:184-190`) and failed outcomes are built with `age_seconds=None` (`fleet_command_deck.py:936-948`). Task 3 adds an optional `RailEntry.age_seconds` and fills failed rows from `received_at`, which is the spec's "event's `received_at`". Collisions, integration items, and blocked queue items have no observation time and read `first observed unknown`.
3. **No `offline` state exists.** The spec counts machines that "report state `offline`". The telemetry vocabulary is `available, busy, draining, unavailable, unknown` (`fleet_hub_routing.py:34`). The plan implements the literal `state == "offline"` check, which is inert today, and the stale and not-enrolled rules do the real work.
4. **Queued rows are blocked rows.** The spec says queued rows come from `QueueState`, which holds counts and a blocked list only (`fleet_command_deck.py:320-325`). The Queued panel shows the eligible and queued counts plus one row per blocked item, with the spec's `Nothing queued.` empty state.
5. **Sidebar tree only where a view exists.** `/deck/roster`, `/deck/policy`, and `/deck/work` never build a `DeckView` (`src/brigade/fleet_hub_http.py:1028-1065`, `1152-1200`, `1102-1133`). They get the new shell with the navigation only; `/deck` and `/deck/repos` (which build one) also get the machine and cloud trees.
6. **`deck_page` takes keyword extras.** The spec signature is `deck_page(title, active, sidebar_html, body_html, nonce)`. The shell also needs `as_of`, `refresh_seconds`, and `extra_css`, so they are keyword-only arguments after `nonce`.
7. **Center stays dark.** The spec says every `ui_theme.document()` page gets the light scheme and that Center only gains it. Center view modules embed their own `<style>` blocks and SVG attributes with literal dark palettes (for example `src/brigade/center_cmd/dashboard/views/status_grid.py:22-28`, `work.py:28-34`, `research.py:49-55`, `code_graph.py:34-42`, `memory_operations.py:32-41`, `agent_activity.py:42`), outside the `extra_css` the spec audits. Light canvas under those palettes would leave low-contrast text. `ui_theme.document` gains `scheme="dark"`, Center passes it, and removing that argument is the follow-up once the Center views use tokens.
8. **Brand accent rules are exempt from the literal-color test.** `fleet_command_deck.py:1605-1609` generates `.badge-color-<hex>` rules from the fixed vendor brand table, and `tests/test_ui_theme_integration.py` asserts the literal accent. Vendor marks must not change with the scheme, so the test strips those rules and checks everything else.
9. **Working rows carry the harness badge only.** This follows the spec (`harness badge · seat · machine`); the old tile also showed a provider badge and model column. The model moves to a tooltip on the row. Tests that pinned the old tile are listed in Task 12.
10. **Verdict counts stale machines.** The pill is `deck_verdict` unchanged unless attention items plus stale-telemetry machines exist, in which case it reads `NEEDS ATTENTION (n)` with the same `n` as the alert cards. Without this the pill could read `ALL CLEAR` above a stale-telemetry alert. `deck_verdict` and `attention_items` are not modified.

## File map

| Path | Change | Responsibility |
|---|---|---|
| `src/brigade/ui_theme.py` | modify | Light and dark token sets (`TOKENS`, `DARK_TOKENS`), `document(scheme=...)`, shell and sidebar CSS in `BASE_CSS`, `OVERVIEW_CSS` constant |
| `src/brigade/fleet_deck_shell.py` | create | `deck_page` two-column shell and the constant machine-filter script |
| `src/brigade/fleet_deck_overview.py` | create | `format_age`, `collapse_outcomes`, join helpers, summary, alerts, header, work lists, machines table, below-the-fold sections, sidebar, `render_overview` |
| `src/brigade/fleet_command_deck.py` | modify (shrinks to 1763 lines) | `MachineState.node_id`, `RailEntry.age_seconds`, thin `render_deck`, `_document` over the shell; station cards, tile and session helpers, old script and CSS deleted |
| `src/brigade/fleet_hub_routing.py` | modify | `snapshot()` emits `node_id` per machine |
| `src/brigade/fleet_hub_roster_page.py` | modify | Drop inline nav, render in shell with `active="roster"` |
| `src/brigade/fleet_policy_page.py` | modify | Drop inline nav, render in shell with `active="policy"` |
| `src/brigade/fleet_repo_policy_page.py` | modify | Drop inline nav, render in shell with `active="repos"` and the view |
| `src/brigade/fleet_work_page.py` | modify | Drop inline navs, render in shell with `active="work"` |
| `src/brigade/center_cmd/dashboard/render.py` | modify | Center page passes `scheme="dark"` |
| `tests/test_fleet_deck_overview.py` | create | Renderer, shell, sidebar, escaping, class-coverage, and size-budget tests |
| `tests/test_ui_theme.py` | modify | Token, scheme, and base-CSS tests |
| `tests/test_ui_theme_integration.py` | modify | Canvas count, badge test, layout-order test, scheme and literal-color tests |
| `tests/test_fleet_command_deck.py` | modify | node_id, age, HTTP join tests; pinned-markup assertions updated |
| `tests/test_fleet_routing.py` | modify | `node_id` in the snapshot |
| `tests/test_fleet_deck_badges.py` | modify | Tile tests move to working-row tests |
| `tests/test_fleet_dashboard.py` | modify | One station-capacity assertion |
| `tests/test_fleet_work_page.py` | modify | One slots-busy assertion (two lines) |
| `CHANGELOG.md` | modify | Unreleased entry |
| `docs/assets/deck-overview-20261018/` | create | Capture script, PNGs, README |

## Task 1: Light theme tokens and dark-only Center

**Files:**
- Modify: `src/brigade/ui_theme.py` (lines 18-36 `TOKENS`, lines 83-87 literals, `document()` at 148-173)
- Modify: `src/brigade/center_cmd/dashboard/render.py` (lines 111-117)
- Modify: `tests/test_ui_theme.py` (lines 8-26 `test_tokens_define_every_color_once`)
- Modify: `tests/test_ui_theme_integration.py` (line 176, add helpers and a test at the end)

- [x] **Step 1: Write the failing tests.** In `tests/test_ui_theme.py` change the import block to

```python
import re

import pytest

from brigade import ui_theme
```

then replace the whole `test_tokens_define_every_color_once` function with:

```python
COLOR_TOKENS = (
    "--canvas",
    "--surface",
    "--surface-raised",
    "--line",
    "--line-quiet",
    "--ink",
    "--muted",
    "--faint",
    "--signal",
    "--signal-quiet",
    "--ok",
    "--bad",
    "--working",
    "--idle",
    "--offline",
    "--warn-surface",
    "--warn-line",
    "--bar-track",
    "--surface-inset",
    "--error-line",
    "--on-accent",
)


def test_tokens_define_every_color_for_dark_and_light():
    dark, light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")
    for name in COLOR_TOKENS:
        assert dark.count(f"{name}:") == 1, name
        assert light.count(f"{name}:") == 1, name
    assert "color-scheme: light dark;" in dark
    assert "--space-1:" in dark and "--space-1:" not in light


def test_light_palette_follows_the_design():
    light = ui_theme.TOKENS.split("@media (prefers-color-scheme: light)")[1]
    for declaration in (
        "--canvas: #f6f7f8;",
        "--surface: #ffffff;",
        "--line: #e3e6e8;",
        "--ink: #1d2327;",
        "--muted: #5f6b72;",
        "--working: #3b6fd8;",
        "--idle: #3f9a5d;",
        "--warn-surface: #fbf3e6;",
        "--warn-line: #e8cfa4;",
    ):
        assert declaration in light, declaration


def test_dark_tokens_pin_a_page_to_dark():
    assert "color-scheme: dark;" in ui_theme.DARK_TOKENS
    assert "prefers-color-scheme" not in ui_theme.DARK_TOKENS
    for name in COLOR_TOKENS:
        assert ui_theme.DARK_TOKENS.count(f"{name}:") == 1, name
    assert "--canvas: #111617;" in ui_theme.DARK_TOKENS


def test_document_scheme_selects_the_token_sheet():
    auto = ui_theme.document("t", "n", "b", as_of="x")
    assert ui_theme.TOKENS in auto
    dark = ui_theme.document("t", "n", "b", as_of="x", scheme="dark")
    assert ui_theme.DARK_TOKENS in dark and "prefers-color-scheme" not in dark
    with pytest.raises(ValueError):
        ui_theme.document("t", "n", "b", as_of="x", scheme="sepia")


def test_base_css_takes_inset_and_error_colors_from_tokens():
    assert ".empty { margin: 0; padding: var(--space-3); color: var(--muted); background: var(--surface-inset);" in (
        ui_theme.BASE_CSS
    )
    assert ".callout--error { border-color: var(--error-line); }" in ui_theme.BASE_CSS
    assert ".banner--error { border-color: var(--error-line); }" in ui_theme.BASE_CSS
```

In `tests/test_ui_theme_integration.py` change `assert rendered.count("--canvas:") == 1, route` (in `test_all_fleet_renderers_use_one_shared_shell_and_navigation`) to:

```python
        assert rendered.count("--canvas:") == 2, route  # dark :root plus the light media override
```

and append at the end of the file:

```python
def _document_css(rendered: str) -> str:
    """The document stylesheet: the first style element, which is the one in the head."""
    match = re.search(r'<style nonce="[^"]*">(.*?)</style>', rendered, re.DOTALL)
    assert match
    return match.group(1)


def _center_status_page() -> str:
    from brigade.center_cmd.dashboard import render
    from brigade.center_cmd.dashboard.views import render_nav, status_grid

    return render.page(
        "Status",
        "integration-nonce",
        render_nav("status"),
        status_grid.render({"pending_tasks": []}, nonce="integration-nonce"),
    )


def test_boards_and_deck_pages_follow_the_system_scheme_and_center_stays_dark(fleet_pages):
    for route, rendered in fleet_pages.items():
        css = _document_css(rendered)
        assert "color-scheme: light dark;" in css, route
        assert "@media (prefers-color-scheme: light)" in css, route
    center = _document_css(_center_status_page())
    assert "color-scheme: dark;" in center and "prefers-color-scheme" not in center
    assert "--canvas: #111617;" in center
```

- [x] **Step 2: Run them and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_ui_theme.py tests/test_ui_theme_integration.py" --capture brigade-work
```

Expected: failures with `AttributeError: module 'brigade.ui_theme' has no attribute 'DARK_TOKENS'`, `TypeError: document() got an unexpected keyword argument 'scheme'`, the canvas count `assert 1 == 2`, and `assert 'color-scheme: light dark;' in ...`.

- [x] **Step 3: Implement.** In `src/brigade/ui_theme.py` replace the `TOKENS = ( ... )` block (everything from the line `TOKENS = (` through its closing `)`, immediately before `BASE_CSS = """`) with:

```python
_DARK_COLORS = """  --canvas: #111617;
  --surface: #182022;
  --surface-raised: #202a2c;
  --line: #3a4849;
  --line-quiet: #293536;
  --ink: #e5ece8;
  --muted: #a4b1ad;
  --faint: #75837f;
  --signal: #d5ab69;
  --signal-quiet: #382f22;
  --ok: #6fbf8e;
  --bad: #d67c6a;
  --working: #6a9bf0;
  --idle: var(--ok);
  --offline: #75837f;
  --warn-surface: #2a2419;
  --warn-line: #6d5635;
  --bar-track: #293536;
  --surface-inset: #141b1c;
  --error-line: #b0553a;
  --on-accent: #ffffff;
"""

_LIGHT_COLORS = """    --canvas: #f6f7f8;
    --surface: #ffffff;
    --surface-raised: #f0f2f3;
    --line: #e3e6e8;
    --line-quiet: #eceef0;
    --ink: #1d2327;
    --muted: #5f6b72;
    --faint: #8a959b;
    --signal: #8a5d14;
    --signal-quiet: #fbf3e6;
    --ok: #2f7d4a;
    --bad: #b3402c;
    --working: #3b6fd8;
    --idle: #3f9a5d;
    --offline: #8a959b;
    --warn-surface: #fbf3e6;
    --warn-line: #e8cfa4;
    --bar-track: #e3e6e8;
    --surface-inset: #eef1f2;
    --error-line: #c0472a;
    --on-accent: #ffffff;
"""

_SPACE_TOKENS = "".join(f"  --space-{index}: {pixels}px;\n" for index, pixels in enumerate(SPACING, 1))

# The default sheet follows the system: dark values on :root, light values
# under prefers-color-scheme. DARK_TOKENS pins a page to dark.
TOKENS = (
    ":root {\n  color-scheme: light dark;\n"
    + _DARK_COLORS
    + _SPACE_TOKENS
    + "}\n@media (prefers-color-scheme: light) {\n  :root {\n"
    + _LIGHT_COLORS
    + "  }\n}\n"
)
DARK_TOKENS = ":root {\n  color-scheme: dark;\n" + _DARK_COLORS + _SPACE_TOKENS + "}\n"
```

Replace the three literal-color rules in `BASE_CSS`:

- `.empty { ... background: #141b1c; ... }` becomes `.empty { margin: 0; padding: var(--space-3); color: var(--muted); background: var(--surface-inset); font-size: 12px; }`
- `.callout--error { border-color: #b0553a; }` becomes `.callout--error { border-color: var(--error-line); }`
- `.banner--error { border-color: #b0553a; }` becomes `.banner--error { border-color: var(--error-line); }`

Replace the `document()` signature and the first lines of its body so it reads:

```python
def document(
    title: str,
    nonce: str,
    body: str,
    *,
    as_of: str,
    refresh_seconds: int | None = None,
    extra_css: str = "",
    script: str = "",
    scheme: str = "auto",
) -> str:
    """Wrap an already-escaped *body* in the shared HTML shell.

    *title* and *as_of* are escaped here. *nonce* is stamped on the style and
    script tags so hub CSP headers keep working. *refresh_seconds* adds the
    meta refresh the Deck and the boards rely on; ``None`` omits it. *scheme*
    is ``"auto"`` (dark, with light under ``prefers-color-scheme: light``) or
    ``"dark"`` (dark only, for pages whose own CSS still carries literal colors).
    """
    if scheme not in ("auto", "dark"):
        raise ValueError(f"unknown color scheme {scheme!r}: use 'auto' or 'dark'")
    tokens = DARK_TOKENS if scheme == "dark" else TOKENS
    refresh_tag = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh_seconds else ""
```

and in the returned f-string replace `{TOKENS}{BASE_CSS}{extra_css}` with `{tokens}{BASE_CSS}{extra_css}`.

In `src/brigade/center_cmd/dashboard/render.py` add `scheme="dark",` as the last argument of the `ui_theme.document(` call in `page()`:

```python
    return ui_theme.document(
        html.unescape(title),
        nonce,
        shell,
        as_of=stamp,
        extra_css=_CENTER_CSS,
        script=_CENTER_SCRIPT % reload_delay,
        scheme="dark",
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_ui_theme.py tests/test_ui_theme_integration.py tests/test_center_dashboard.py tests/test_fleet_dashboard.py" --capture brigade-work
```

Expected: every test passes (`0 failed`). `tests/test_center_dashboard.py` still finds `--canvas: #111617;` because Center uses the dark sheet.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/ui_theme.py src/brigade/center_cmd/dashboard/render.py tests/test_ui_theme.py tests/test_ui_theme_integration.py
git commit -m "$(cat <<'EOF'
feat(ui): add a light token set that follows the system scheme

TOKENS now carries dark values on :root and light values under
prefers-color-scheme, plus the semantic tokens the Deck overview needs.
DARK_TOKENS and document(scheme="dark") keep Center dark until its views use tokens.
EOF
)"
```

## Task 2: `node_id` in the routing snapshot and on `MachineState`

**Files:**
- Modify: `src/brigade/fleet_hub_routing.py` (the `machines.append({...})` dict, lines 1690-1702)
- Modify: `src/brigade/fleet_command_deck.py` (`MachineState` 289-301, parser 1286-1299)
- Modify: `tests/test_fleet_routing.py` (append), `tests/test_fleet_command_deck.py` (append)

- [x] **Step 1: Write the failing tests.** Append to `tests/test_fleet_routing.py`:

```python
def test_snapshot_emits_the_policy_node_id_for_every_machine(conn):
    from brigade import fleet_command_deck

    snap = fleet_hub_routing.snapshot(conn)
    expected = {
        "worker-linux-1": NODE_LINUX,
        "worker-linux-2": NODE_LINUX_2,
        "worker-windows-1": NODE_WINDOWS,
        "worker-cloud-1": NODE_CLOUD,
        "origin-windows-1": CONTROLLER,
    }
    assert {row["id"]: row["node_id"] for row in snap["machines"]} == expected
    plane = fleet_command_deck.control_plane_from_snapshot(snap)
    assert {machine.id: machine.node_id for machine in plane.machines} == expected
```

In `tests/test_fleet_command_deck.py` add `import re` to the import block (after `import json`) and append:

```python
def test_control_plane_adapter_reads_node_id_and_tolerates_snapshots_without_it():
    snapshot = _control_plane_snapshot()
    legacy = deck.control_plane_from_snapshot(snapshot)
    assert legacy.machines[0].node_id == deck.UNKNOWN
    snapshot["machines"][0]["node_id"] = NODE_A
    assert deck.control_plane_from_snapshot(snapshot).machines[0].node_id == NODE_A
    snapshot["machines"][0]["node_id"] = None
    assert deck.control_plane_from_snapshot(snapshot).machines[0].node_id == deck.UNKNOWN
    snapshot["machines"][0]["node_id"] = "<script>alert(1)</script>"
    html = deck.render_deck(_empty_view(deck.control_plane_from_snapshot(snapshot)), nonce="nonce", now=NOW)
    assert "<script>alert(1)</script>" not in html
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_routing.py::test_snapshot_emits_the_policy_node_id_for_every_machine tests/test_fleet_command_deck.py::test_control_plane_adapter_reads_node_id_and_tolerates_snapshots_without_it" --capture brigade-work
```

Expected: `KeyError: 'node_id'` and `AttributeError: 'MachineState' object has no attribute 'node_id'`.

- [x] **Step 3: Implement.** In `fleet_hub_routing.py` change the dict so it starts:

```python
            {
                "id": name,
                "node_id": node_id,
                "os": record.get("os"),
```

In `fleet_command_deck.py` extend `MachineState` (anchor: `eligible_queued_work: str = UNKNOWN` then `unused_reason: str = UNKNOWN`) with a final field:

```python
    eligible_queued_work: str = UNKNOWN
    unused_reason: str = UNKNOWN
    # The enrolled node this policy machine runs on. Additive: snapshots from
    # before the key existed parse with ``unknown`` and join to no station.
    node_id: str = UNKNOWN
```

and in `control_plane_from_snapshot` add the key to the `MachineState(...)` call (anchor `unused_reason=_cp_text(item.get("unused_reason")),`):

```python
                unused_reason=_cp_text(item.get("unused_reason")),
                node_id=_cp_text(item.get("node_id")),
            )
        )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_routing.py tests/test_fleet_command_deck.py" --capture brigade-work
```

Expected: all pass.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_hub_routing.py src/brigade/fleet_command_deck.py tests/test_fleet_routing.py tests/test_fleet_command_deck.py
git commit -m "$(cat <<'EOF'
feat(fleet): emit node_id per machine in the routing snapshot

Stations are keyed by node id while machines are keyed by policy name. The
additive key lets the Deck join them exactly; older snapshots without it parse
as unknown and join to nothing.
EOF
)"
```

## Task 3: Observation age on rail entries

**Files:**
- Modify: `src/brigade/fleet_command_deck.py` (`RailEntry` 184-190, `_live_run_from_row` 936-948, `fetch_failed_outcomes` return at ~849, `build_view` rail at 1158-1167)
- Modify: `tests/test_fleet_command_deck.py` (append)

- [x] **Step 1: Write the failing test.** Append to `tests/test_fleet_command_deck.py`:

```python
def test_failed_outcomes_and_rail_entries_carry_the_receipt_age(conn):
    _insert(conn, NODE_A, "failure.new", 1, "run.dispatch.failed", ts=_ts(60), repo="same-repo")
    (failed,) = _failed(conn)
    assert failed.age_seconds == 60
    stale = deck.LiveRun(
        node_id=NODE_A,
        run_id="ghost",
        repo="stale-only",
        seat="seat",
        harness="claude",
        state="run.started",
        bucket="stale",
        age_seconds=3600,
        elapsed_seconds=3600,
    )
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE_A, "Alpha", 2),)),
        live_runs=[stale],
        claims=[],
        enrolled_labels={NODE_A: "Alpha"},
        last_heard={},
        outcomes=[],
        failed_outcomes=[failed],
        observers=[],
        now=NOW,
    )
    assert [(entry.kind, entry.age_seconds) for entry in view.rail] == [("failed", 60), ("stale", 3600)]
```

- [x] **Step 2: Run and see it fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_command_deck.py::test_failed_outcomes_and_rail_entries_carry_the_receipt_age" --capture brigade-work
```

Expected: `assert None == 60` (failed outcomes carry no age).

- [x] **Step 3: Implement.** In `fleet_command_deck.py`:

`RailEntry` gains a defaulted last field:

```python
@dataclass(frozen=True)
class RailEntry:
    kind: str
    node_id: str
    repo: str
    run_id: str
    detail: str
    age_seconds: int | None = None
```

`_live_run_from_row` takes `now` and fills the age from the receipt time (column 7):

```python
def _live_run_from_row(row: tuple, *, bucket: str | None = None, now: datetime | None = None) -> LiveRun:
    return LiveRun(
        node_id=row[0],
        run_id=row[1],
        repo=row[2] or "",
        seat=row[3] or "",
        harness=row[4] or "",
        state=row[5],
        bucket=row[5] if bucket is None else bucket,
        age_seconds=_age_seconds(row[7] or "", now) if now is not None and len(row) > 7 else None,
        elapsed_seconds=None,
        repo_identity=(row[8] or "") if len(row) > 8 else "",
    )
```

In `fetch_failed_outcomes` change the last line to `return _enrich_runs(conn, [_live_run_from_row(row, bucket="failed", now=now) for row in rows])`.

In `build_view` replace the two `RailEntry(` constructions for failed outcomes and for awaiting/stale/unknown live runs with:

```python
    rail: list[RailEntry] = [
        RailEntry(
            kind="failed",
            node_id=run.node_id,
            repo=run.repo,
            run_id=run.run_id,
            detail=run.state,
            age_seconds=run.age_seconds,
        )
        for run in failed_outcomes
    ]
    rail.extend(
        RailEntry(
            kind=run.bucket,
            node_id=run.node_id,
            repo=run.repo,
            run_id=run.run_id,
            detail=run.state,
            age_seconds=run.age_seconds,
        )
        for run in live_runs
        if run.bucket in ("awaiting approval", "stale", "unknown")
    )
```

(The collision `RailEntry` below stays as it is; its age stays `None`.)

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_command_deck.py tests/test_fleet_deck_badges.py" --capture brigade-work
```

Expected: all pass.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_command_deck.py tests/test_fleet_command_deck.py
git commit -m "$(cat <<'EOF'
feat(fleet): carry the receipt age on Deck attention entries

Failed outcomes and stale, unknown, and awaiting rows now keep how long ago the
hub received their event, so alerts can say when they were first observed.
EOF
)"
```

## Task 4: Overview module, helpers, and the station join

**Files:**
- Create: `src/brigade/fleet_deck_overview.py`
- Create: `tests/test_fleet_deck_overview.py`

- [x] **Step 1: Write the failing tests.** Create `tests/test_fleet_deck_overview.py` with this header (later tasks append sections and extend the import block) and the two test sections that follow.

```python
"""Command Deck overview renderers over synthetic DeckView objects."""

from datetime import datetime, timedelta, timezone

from brigade import fleet_command_deck as deck
from brigade import fleet_deck_overview as overview

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
```

```python
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
```

```python
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
    assert overview.station_status(idle, None) == "idle"
    assert overview.station_status(idle, stale) == "stale"
    assert overview.station_status(busy, never) == "stale"
    assert overview.station_status(idle, gone) == "offline"
    assert overview.station_status(unenrolled, stale) == "offline"
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: collection error `ImportError: cannot import name 'fleet_deck_overview' from 'brigade'`.

- [x] **Step 3: Implement.** Create `src/brigade/fleet_deck_overview.py` containing exactly:

```python
"""Overview page renderers for the Command Deck.

Pure functions over ``DeckView``: no database access and no I/O. Every value
that came from a run, station, session, provider, or snapshot goes through
``_esc`` before it reaches markup or an attribute. The page is built from data
the hub already stores, and a missing value renders as ``unknown``, never as
zero or as an all-clear.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from . import fleet_command_deck as deck
from .fleet_command_deck import UNKNOWN, DeckView, LiveRun, MachineState, StationView

_esc = deck._esc
_UTC = timezone.utc

# Staleness is the hub's verdict (it compares telemetry age to the policy TTL),
# not a second threshold in the Deck.
STALE_REASONS = frozenset({"stale-telemetry", "unknown-telemetry"})


def format_age(seconds: int | None) -> str:
    """``11s``, ``4m``, ``1h 13m``, ``40d 2h``; ``unknown`` when there is no age."""
    if seconds is None:
        return UNKNOWN
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h {total % 3600 // 60}m"
    return f"{total // 86400}d {total % 86400 // 3600}h"


def seconds_since(stamp: str | None, now: datetime) -> int | None:
    """Whole seconds from an ISO stamp to *now*, or None when it cannot be read."""
    if not stamp or stamp == UNKNOWN:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return max(0, int((now - moment).total_seconds()))


def collapse_outcomes(outcomes: Sequence[LiveRun]) -> tuple[tuple[LiveRun, int], ...]:
    """Merge adjacent rows with the same state, repo, and node into one with a count."""
    rows: list[tuple[LiveRun, int]] = []
    for run in outcomes:
        if rows and _outcome_key(rows[-1][0]) == _outcome_key(run):
            rows[-1] = (rows[-1][0], rows[-1][1] + 1)
        else:
            rows.append((run, 1))
    return tuple(rows)


def _outcome_key(run: LiveRun) -> tuple[str, str, str]:
    return (run.state, run.repo, run.node_id)


def machine_for(view: DeckView, station: StationView) -> MachineState | None:
    """The policy machine for a station, joined on node id. Never matched by label."""
    plane = view.control_plane
    node_id = station.station.node_id
    if plane is None or not node_id:
        return None
    for machine in plane.machines:
        if machine.node_id != UNKNOWN and machine.node_id == node_id:
            return machine
    return None


def is_stale(station: StationView, machine: MachineState | None) -> bool:
    """An enrolled station whose hub-reported telemetry is stale or missing."""
    return station.enrolled and machine is not None and machine.unused_reason in STALE_REASONS


def station_status(station: StationView, machine: MachineState | None) -> str:
    """``offline``, ``stale``, ``working``, or ``idle``. Drives dots and card counts."""
    if not station.enrolled:
        return "offline"
    if is_stale(station, machine):
        return "stale"
    if machine is not None and machine.state == "offline":
        return "offline"
    return "working" if station.busy else "idle"
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `5 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add overview helpers for ages, outcomes, and the station join

format_age renders 11s, 4m, 1h 13m, and 40d 2h. collapse_outcomes merges only
adjacent identical rows. Stations join to machines on node id, never on label,
and staleness is the hub's stale-telemetry and unknown-telemetry verdict.
EOF
)"
```

## Task 5: Summary cards

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (import block, append a section)
- Modify: `tests/test_fleet_deck_overview.py` (import block, append a section)

- [x] **Step 1: Write the failing tests.** In `tests/test_fleet_deck_overview.py` add `import re` as the first import line (above `from datetime ...`), then append two blank lines and:

```python
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
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 5 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'render_summary'`.

- [x] **Step 3: Implement.** In `src/brigade/fleet_deck_overview.py` replace the import block with

```python
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from . import fleet_command_deck as deck
from .fleet_command_deck import UNKNOWN, DeckView, LiveRun, MachineState, StationView
```

then append two blank lines and:

```python
# --- summary cards ------------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    busy: int
    total: int
    idle: str
    offline: str
    queued: str


def summarize(view: DeckView) -> Summary:
    plane = view.control_plane
    busy = sum(station.busy for station in view.stations)
    total = sum(station.station.capacity for station in view.stations)
    idle = offline = UNKNOWN
    if plane is not None and "machines" in plane.present:
        idle_slots = offline_count = 0
        for station in view.stations:
            machine = machine_for(view, station)
            if station_status(station, machine) in {"offline", "stale"}:
                offline_count += 1
            elif machine is not None and machine.available_slots.isdigit():
                idle_slots += int(machine.available_slots)
        idle, offline = str(idle_slots), str(offline_count)
    queued = plane.queue.eligible_count if plane is not None and "queue" in plane.present else UNKNOWN
    return Summary(busy=busy, total=total, idle=idle, offline=offline, queued=queued)


def _card(name: str, title: str, value: str, sub: str, bar: str = "") -> str:
    return (
        f'<article class="card card--{name}" data-card="{name}"><h2 class="card-title">{title}</h2>'
        f'<p class="card-value">{_esc(value)}</p><p class="card-sub">{_esc(sub)}</p>{bar}</article>'
    )


def render_summary(view: DeckView) -> str:
    summary = summarize(view)
    working_bar = (
        f'<progress class="bar" value="{summary.busy}" max="{summary.total}" aria-label="Busy slots"></progress>'
        if summary.total > 0
        else ""
    )
    return (
        '<section class="cards" aria-label="Fleet summary">'
        + _card("working", "Working", str(summary.busy), f"of {summary.total} slots", working_bar)
        + _card("idle", "Idle", summary.idle, "free slots, fresh telemetry")
        + _card("offline", "Offline", summary.offline, "stale or offline")
        + _card("queued", "Queued", summary.queued, "eligible to run")
        + "</section>"
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `10 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): render the four overview summary cards

Working counts busy slots, Idle counts hub-reported free slots on fresh
machines, Offline counts stale, unenrolled, and offline machines, and Queued
shows the eligible count. Absent data renders as unknown, never zero.
EOF
)"
```

## Task 6: Active alerts

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (append a section)
- Modify: `tests/test_fleet_deck_overview.py` (append a section)

- [x] **Step 1: Write the failing tests.** Append two blank lines and:

```python
# --- alerts ----------------------------------------------------------------------------


def _alert_view() -> deck.DeckView:
    rail = (
        deck.RailEntry("failed", NODE_A, "skillet", "r1", "external.failed", 7200),
        deck.RailEntry("unknown", NODE_B, "canary-repo", "r2", "external.unknown", 86400),
        deck.RailEntry("collision", "linux-a", "shared", "", "held by linux-a, live on shared"),
    )
    plane = deck.ControlPlane(
        machines=(
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
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 7 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'page_alerts'` (or `render_alerts`).

- [x] **Step 3: Implement.** Append two blank lines and:

```python
# --- alerts ---------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    kind: str
    title: str
    detail: str
    since: str


def _first_observed(age_seconds: int | None) -> str:
    return "first observed unknown" if age_seconds is None else f"first observed {format_age(age_seconds)} ago"


def page_alerts(view: DeckView, now: datetime) -> tuple[Alert, ...]:
    """Attention items, then collisions, stale telemetry, and coverage gaps."""
    labels = {station.station.node_id: station.label for station in view.stations}
    attention: list[Alert] = []
    collisions: list[Alert] = []
    for entry in view.rail:
        since = _first_observed(entry.age_seconds)
        if entry.kind == "collision":
            collisions.append(Alert("collision", entry.repo or UNKNOWN, entry.detail, since))
            continue
        where = labels.get(entry.node_id) or entry.node_id[:12]
        attention.append(Alert(entry.kind, f"{entry.repo or UNKNOWN} on {where}", entry.detail, since))
    plane = view.control_plane
    if plane is not None:
        for item in plane.integrations:
            if item.routine_state == "needs-attention":
                detail = f"routine {item.routine_state}" + (f" · {item.detail}" if item.detail else "")
                attention.append(Alert("integration", item.id, detail, _first_observed(None)))
        for blocked in plane.queue.blocked:
            reason = deck._cp_text(blocked.get("reason"))
            attention.append(Alert("blocked", deck._cp_text(blocked.get("id")), reason, _first_observed(None)))
    stale: list[Alert] = []
    for station in view.stations:
        machine = machine_for(view, station)
        if machine is not None and is_stale(station, machine):
            seen = seconds_since(machine.last_observed_at, now)
            since = "no telemetry received" if seen is None else f"last telemetry {format_age(seen)} ago"
            stale.append(Alert("stale telemetry", station.label, machine.unused_reason, since))
    gaps = deck.coverage_gaps(plane)
    coverage: list[Alert] = []
    if gaps:
        title = "Control-plane snapshot missing" if plane is None else "Telemetry coverage gaps"
        coverage.append(Alert("coverage", title, "no telemetry for: " + ", ".join(gaps), "ongoing"))
    return tuple(attention + collisions + stale + coverage)


def _alert_html(alert: Alert) -> str:
    kind_class = alert.kind.replace(" ", "-")
    return (
        f'<li class="alert alert--{_esc(kind_class)}"><span class="alert-icon" aria-hidden="true">!</span>'
        f'<div class="alert-body"><p class="alert-title"><span class="alert-kind">{_esc(alert.kind)}</span> '
        f'{_esc(alert.title)}</p><p class="alert-detail">{_esc(alert.detail)}</p>'
        f'<p class="alert-since">{_esc(alert.since)}</p></div></li>'
    )


def render_alerts(view: DeckView, now: datetime) -> str:
    alerts = page_alerts(view, now)
    items = "".join(_alert_html(alert) for alert in alerts)
    body = f'<ul class="alerts">{items}</ul>' if items else '<p class="empty">No active alerts</p>'
    return (
        '<section class="panel" id="alerts" aria-labelledby="alerts-title"><header>'
        f'<h2 id="alerts-title">Active alerts ({len(alerts)})</h2></header>{body}</section>'
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `17 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add the overview alert list

Alerts follow the attention items, then collisions, stale telemetry, and
coverage gaps. Each carries a first-observed age where the hub has one, and a
missing control-plane snapshot is itself an alert.
EOF
)"
```

## Task 7: Page header and verdict pill

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (append a section)
- Modify: `tests/test_fleet_deck_overview.py` (append a section)

- [x] **Step 1: Write the failing tests.** Append two blank lines and:

```python
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
    assert 'verdict-pill--attention">NEEDS ATTENTION (2)' in overview.render_header(with_failure, NOW)


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
    assert "verdict-pill--clear" in overview.render_header(_view(plane=full), NOW)
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 3 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'render_header'` (or `verdict_text`).

- [x] **Step 3: Implement.** Append two blank lines and:

```python
# --- header and verdict ---------------------------------------------------------


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def verdict_text(view: DeckView, now: datetime) -> str:
    """The Deck verdict, counting stale telemetry alongside the attention items."""
    stale = sum(1 for alert in page_alerts(view, now) if alert.kind == "stale telemetry")
    count = len(deck.attention_items(view)) + stale
    return f"NEEDS ATTENTION ({count})" if count else deck.deck_verdict(view)


def render_header(view: DeckView, now: datetime) -> str:
    verdict = verdict_text(view, now)
    tone = "attention" if verdict.startswith("NEEDS") else "unknown" if verdict.startswith("COVERAGE") else "clear"
    slots = sum(station.station.capacity for station in view.stations)
    sub = " · ".join(
        (
            _plural(len(view.stations), "machine"),
            _plural(slots, "slot"),
            _plural(len(view.cloud_workers), "cloud provider"),
        )
    )
    return (
        '<header class="page-head"><div><p class="crumbs">Fleet &rsaquo; Fleet overview</p>'
        f'<h1>Fleet overview <span class="verdict-pill verdict-pill--{tone}">{_esc(verdict)}</span></h1>'
        f'<p class="page-sub">{_esc(sub)}</p></div>'
        f'<p class="header-meta">Updated {_esc(now.strftime("%H:%M UTC"))} '
        '<a class="refresh-link" href="/deck">Refresh</a></p></header>'
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `20 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add the overview header and verdict pill

The pill reads the Deck verdict, counting stale-telemetry machines with the
attention items so it always matches the alert cards.
EOF
)"
```

## Task 8: Working now and Queued

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (import block, append a section)
- Modify: `tests/test_fleet_deck_overview.py` (append a section)

- [x] **Step 1: Write the failing tests.** Append two blank lines and:

```python
# --- working now and queued -----------------------------------------------------------------


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
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 4 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'render_work_lists'`.

- [x] **Step 3: Implement.** Replace the import block in `src/brigade/fleet_deck_overview.py` with

```python
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlencode

from . import fleet_command_deck as deck
from .fleet_command_deck import UNKNOWN, DeckView, LiveRun, MachineState, StationView, Tile
```

then append two blank lines and:

```python
# --- working now and queued -------------------------------------------------------


def _working_row(station: StationView, tile: Tile) -> str:
    run = tile.run
    seat = run.seat or (tile.claim.owner_conductor if tile.claim is not None else "") or UNKNOWN
    href = "/view/machines?" + urlencode({"node": run.node_id, "repo": run.repo[:128]})
    flag = ' <span class="flag">! collision</span>' if tile.collision else ""
    state = "" if run.bucket == "running" else f' <span class="chip chip-muted">{_esc(run.bucket)}</span>'
    model = run.launch_model or run.model
    model_attr = f' title="{_esc(model)}"' if model else ""
    return (
        f'<li class="row" title="{_esc(run.run_id)}"><span class="dot dot--working" aria-hidden="true"></span>'
        f'<div class="row-body"><p class="row-title"><a href="{_esc(href)}">{_esc(run.repo or UNKNOWN)}</a>'
        f'{flag}{state}</p><p class="row-meta"{model_attr}>{deck.harness_badge_html(run.harness)} '
        f"{_esc(seat)} · {_esc(station.label)}</p></div>"
        f'<span class="row-time">{_esc(format_age(run.elapsed_seconds))}</span></li>'
    )


def _queue_body(plane: deck.ControlPlane | None) -> str:
    if plane is None or "queue" not in plane.present:
        return '<p class="empty">Queue unknown: the control-plane snapshot has no queue section.</p>'
    queue = plane.queue
    counts = f'<p class="station-meta">eligible {_esc(queue.eligible_count)} · queued {_esc(queue.queued_count)}</p>'
    rows = "".join(
        '<li class="row"><span class="dot dot--offline" aria-hidden="true"></span><div class="row-body">'
        f'<p class="row-title">{_esc(deck._cp_text(item.get("id")))}</p>'
        f'<p class="row-meta">blocked · {_esc(deck._cp_text(item.get("reason")))}</p></div></li>'
        for item in queue.blocked
    )
    if rows:
        return counts + f'<ul class="rows">{rows}</ul>'
    if queue.queued_count == "0":
        return counts + '<p class="empty">Nothing queued.</p>'
    if queue.queued_count == UNKNOWN:
        return counts + '<p class="empty">Queued items unknown.</p>'
    note = "none blocked" if queue.blocked_known else "blocked reasons unknown"
    return counts + f'<p class="empty">{_esc(queue.queued_count)} queued, {note}.</p>'


def render_work_lists(view: DeckView) -> str:
    rows = "".join(_working_row(station, tile) for station in view.stations for tile in station.tiles)
    count = sum(station.busy for station in view.stations)
    working = f'<ul class="rows">{rows}</ul>' if rows else '<p class="empty">Nothing is running.</p>'
    return (
        '<div class="split"><section class="panel" aria-labelledby="working-title"><header>'
        f'<h2 id="working-title">Working now ({count})</h2></header>{working}</section>'
        '<section class="panel" aria-labelledby="queued-title"><header><h2 id="queued-title">Queued</h2></header>'
        f"{_queue_body(view.control_plane)}</section></div>"
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `24 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add the Working now and Queued panels

Working rows show the repo (linked to its board rows), harness badge, seat,
machine, and elapsed time. Queued shows the hub counts and blocked reasons, and
says unknown when the queue section is absent.
EOF
)"
```

## Task 9: Machines table

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (import block, append a section)
- Modify: `tests/test_fleet_deck_overview.py` (append a section)

- [x] **Step 1: Write the failing tests.** Append two blank lines and:

```python
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
        html = overview.render_machine_table(_view([station]), NOW)
        idle = max(capacity - busy, 0)
        assert f"{busy} busy, {idle} idle" in html
        assert html.count("seg--busy") == min(busy, capacity)
        assert html.count("seg--free") == idle


def test_wide_machines_use_one_proportional_bar_instead_of_segments():
    station = _station(NODE_A, "linux-a", 17, [_run(NODE_A, "r1", "x"), _run(NODE_A, "r2", "x")])
    html = overview.render_machine_table(_view([station]), NOW)
    assert "seg--busy" not in html
    assert '<progress class="seats-wide" value="2" max="17"' in html
    assert "2 busy, 15 idle" in html


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
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 6 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'render_machine_table'`.

- [x] **Step 3: Implement.** Replace the import block with

```python
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence
from urllib.parse import urlencode

from . import fleet_command_deck as deck
from .fleet_command_deck import UNKNOWN, DeckView, LiveRun, MachineState, StationView, Tile
```

then append two blank lines and:

```python
# --- machines table ---------------------------------------------------------------


SEGMENT_LIMIT = 16


def _seats_html(station: StationView) -> str:
    capacity = station.station.capacity
    busy = station.busy
    idle = max(capacity - busy, 0)
    text = f"{busy} busy, {idle} idle"
    if capacity <= SEGMENT_LIMIT:
        busy_segments = '<span class="seg seg--busy"></span>' * min(busy, capacity)
        free_segments = '<span class="seg seg--free"></span>' * idle
        bar = f'<span class="seats" role="img" aria-label="{_esc(text)}">{busy_segments}{free_segments}</span>'
    else:
        bar = (
            f'<progress class="seats-wide" value="{min(busy, capacity)}" max="{capacity}" '
            f'aria-label="{_esc(text)}"></progress>'
        )
    return f'{bar}<span class="seat-text">{_esc(text)}</span>'


def _load_cell(machine: MachineState | None) -> str:
    if machine is None:
        return UNKNOWN
    try:
        value = float(machine.load)
    except ValueError:
        return UNKNOWN
    if not math.isfinite(value):
        return UNKNOWN
    percent = round(min(max(value, 0.0), 1.0) * 100)
    return f'{percent}%<progress class="bar bar--thin" value="{percent}" max="100" aria-label="Load"></progress>'


def _machine_row(view: DeckView, station: StationView, now: datetime) -> str:
    machine = machine_for(view, station)
    status = station_status(station, machine)
    enrol = "" if station.enrolled else ' <span class="chip chip-muted">not enrolled</span>'
    os_name = machine.os if machine is not None else UNKNOWN
    claims = machine.active_claims if machine is not None else UNKNOWN
    state = machine.state if machine is not None else UNKNOWN
    reason = machine.unused_reason if machine is not None else UNKNOWN
    reason_html = f'<span class="sub">{_esc(reason)}</span>' if reason != UNKNOWN else ""
    telemetry = format_age(seconds_since(machine.last_observed_at, now)) if machine is not None else UNKNOWN
    last_run = "never" if station.last_heard is None else format_age(seconds_since(station.last_heard, now))
    return (
        f'<tr data-status="{status}"><td><span class="dot dot--{status}" aria-hidden="true"></span> '
        f'<span class="machine-name" title="{_esc(station.station.node_id)}">{_esc(station.label)}</span>{enrol}'
        f'<span class="sub">{_esc(os_name)}</span></td>'
        f"<td>{_seats_html(station)}</td><td>{_load_cell(machine)}</td><td>{_esc(claims)}</td>"
        f"<td>{_esc(state)}{reason_html}</td>"
        f'<td>{_esc(telemetry)}<span class="sub">last run {_esc(last_run)}</span></td></tr>'
    )


def render_machine_table(view: DeckView, now: datetime) -> str:
    if not view.stations:
        return (
            '<section class="panel" aria-labelledby="machines-title"><header>'
            '<h2 id="machines-title">Machines (0)</h2></header>'
            '<p class="empty">No stations configured. Start the hub with --deck-config to add them.</p></section>'
        )
    rows = "".join(_machine_row(view, station, now) for station in view.stations)
    return (
        '<section class="panel" id="machines-table" aria-labelledby="machines-title"><header>'
        f'<h2 id="machines-title">Machines ({len(view.stations)})</h2></header>'
        '<div class="table-wrap"><table class="machine-table"><thead><tr><th>Machine</th><th>Seats</th>'
        "<th>Load</th><th>Claims</th><th>State</th><th>Telemetry</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></section>"
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `30 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add the machines table with seat bars

One row per station joined to its machine by node id. Seats render as one
segment per slot up to 16 and a proportional bar beyond, load as a percentage
with a bar, and telemetry age apart from last-run age. Missing joins show unknown.
EOF
)"
```

## Task 10: Below-the-fold sections and the overview body

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py` (append sections)
- Modify: `tests/test_fleet_deck_overview.py` (append a section)

- [x] **Step 1: Write the failing tests.** Append two blank lines and:

```python
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
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: 6 failures with `AttributeError: module 'brigade.fleet_deck_overview' has no attribute 'render_cloud_workers'` (and the same for the other new functions).

- [x] **Step 3: Implement.** Append two blank lines and the first block, then two more blank lines and the second:

```python
# --- below the fold ---------------------------------------------------------------


def _cloud_worker_html(worker: deck.CloudWorker) -> str:
    lease_items = "".join(
        "<li>"
        f"<strong>{_esc(lease.label or lease.provider_task_id or 'active lease')}</strong><br>"
        f"{_esc(lease.repo)} &middot; {_esc(lease.state)}"
        + (f'<br><span title="task id">{_esc(lease.provider_task_id)}</span>' if lease.provider_task_id else "")
        + (f'<br><span title="artifact">{_esc(lease.artifact_ref)}</span>' if lease.artifact_ref else "")
        + "</li>"
        for lease in worker.leases
    )
    return (
        '<article class="cloud-worker-card"><header>'
        f"<h3>{deck.provider_badge_html(worker.provider)} {_esc(worker.provider)}</h3>"
        f'<p class="cloud-capacity">{worker.used}/{worker.limit}</p>'
        f'</header><p class="cloud-state">circuit: {_esc(worker.circuit_state)}</p>'
        + (
            f'<ul class="cloud-lease-list">{lease_items}</ul>'
            if lease_items
            else '<p class="empty">No active leases.</p>'
        )
        + "</article>"
    )


def render_cloud_workers(view: DeckView) -> str:
    cards = "".join(_cloud_worker_html(worker) for worker in view.cloud_workers)
    return (
        '<section class="panel" aria-labelledby="cloud-workers"><header><h2 id="cloud-workers">Cloud workers</h2>'
        f'<p class="panel-count">{len(view.cloud_workers)} provider(s)</p></header>'
        + (
            f'<div class="cloud-workers">{cards}</div>'
            if cards
            else '<p class="empty">No cloud providers configured.</p>'
        )
        + "</section>"
    )


def _session_row(session: deck.InteractiveSession) -> str:
    dirty = f"{len(session.dirty_paths)}+" if session.dirty_truncated else str(len(session.dirty_paths))
    return (
        f'<tr><td title="{_esc(session.checkout_path)}">{_esc(session.repo_label or session.repo_identity)}</td>'
        f"<td>{deck.harness_badge_html(session.harness)} "
        f"{_esc(session.harness or UNKNOWN)}/{_esc(session.session_id)}</td>"
        f"<td>{_esc(session.branch or '-')}</td><td>{_esc(dirty)}</td></tr>"
    )


def render_sessions(view: DeckView) -> str:
    rows = "".join(_session_row(session) for session in view.interactive_sessions)
    body = (
        '<div class="table-wrap"><table class="session-table"><thead><tr><th>Repo</th><th>Session</th>'
        f"<th>Branch</th><th>Dirty</th></tr></thead><tbody>{rows}</tbody></table></div>"
        if rows
        else '<p class="empty">No interactive sessions.</p>'
    )
    return (
        '<section class="panel" aria-labelledby="interactive-sessions"><header>'
        '<h2 id="interactive-sessions">Interactive sessions</h2>'
        f'<p class="panel-count">{len(view.interactive_sessions)} session(s)</p></header>{body}</section>'
    )


def render_outcomes(view: DeckView) -> str:
    labels = {station.station.node_id: station.label for station in view.stations}
    items = "".join(
        '<li><span class="timeline-state">'
        f"{_esc(run.state)}</span><span>{_esc(run.repo)} · {_esc(labels.get(run.node_id) or run.node_id[:12])}</span>"
        + (
            f"<span>×{count}</span>"
            if count > 1
            else f'<span title="{_esc(run.run_id)}">{_esc(run.run_id[:12])}</span>'
        )
        + "</li>"
        for run, count in collapse_outcomes(view.outcomes)
    )
    return (
        '<section class="panel" aria-labelledby="timeline"><header><h2 id="timeline">Recent outcomes</h2>'
        f'<p class="panel-count">{len(view.outcomes)} shown</p></header>'
        + (f'<ul class="timeline">{items}</ul>' if items else '<p class="empty">No recent outcomes.</p>')
        + "</section>"
    )


def render_observed_groups(view: DeckView, now: datetime) -> str:
    """Operator-configured metadata, folded away because it is not live activity."""
    cards = ""
    for group in view.observed_work_groups:
        stamp, freshness = deck.snapshot_freshness(
            group.snapshot_observed_at, now=now, stale_after_seconds=view.stale_after_seconds
        )
        cards += (
            '<article class="panel"><h3><a href="/deck/observed/'
            + _esc(group.key)
            + '">'
            + _esc(group.label)
            + "</a></h3><p>"
            + str(len(group.work_ids))
            + " configured observation records</p><p>Coverage: "
            + _esc(group.coverage)
            + "</p><p>Configured source: "
            + _esc(group.source_ref)
            + "<br>Configured proxy: "
            + _esc(group.proxy_ref)
            + "<br>Configured parent: "
            + _esc(group.parent_ref)
            + "</p><p>Snapshot observation time (operator-configured metadata): "
            + _esc(stamp)
            + f"<br>Snapshot freshness: {freshness}. Stale after {view.stale_after_seconds} seconds.</p></article>"
        )
    return (
        f"<details><summary>Observed work groups ({len(view.observed_work_groups)})</summary>"
        "<p>Retained task observations. Counts describe configured records. "
        "Recent observation metadata does not establish live activity.</p>" + cards + "</details>"
    )


def render_observers(view: DeckView, now: datetime) -> str:
    items = "".join(
        f'<li><span title="{_esc(node)}">{_esc(node[:12])}</span> &middot; '
        f"heard {_esc(format_age(seconds_since(received, now)))} ago</li>"
        for node, received in view.observers
    )
    return (
        f"<details><summary>Other observers ({len(view.observers)})</summary>"
        f'<ul class="observer-list">{items}</ul></details>'
    )
```

```python
def render_overview(view: DeckView, now: datetime, *, worklore_enabled: bool) -> str:
    """The whole main column for ``/deck``."""
    parts = [
        '<main class="deck-shell overview">',
        render_header(view, now),
        render_summary(view),
        render_alerts(view, now),
        render_work_lists(view),
        render_machine_table(view, now),
        render_cloud_workers(view),
        render_sessions(view),
        render_outcomes(view),
        "<details><summary>Control plane details</summary>"
        + deck.render_control_plane(view.control_plane)
        + "</details>",
    ]
    if worklore_enabled and view.observed_work_groups:
        parts.append(render_observed_groups(view, now))
    parts.append(render_observers(view, now))
    parts.append('<footer><a href="/view/machines">machines board</a> <a href="/view/repos">repos board</a></footer>')
    parts.append("</main>")
    return "\n".join(parts)
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py" --capture brigade-work
```

Expected: `36 passed`.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_overview.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
feat(deck): add compact cloud, session, outcome, and folded sections

Sessions become a table with the checkout path only in a tooltip, outcomes
collapse adjacent duplicates into a count, and control-plane detail, observed
work groups, and observers fold into closed details. render_overview assembles
the main column.
EOF
)"
```

## Task 11: Shell, sidebar, and the redesign CSS

**Files:**
- Create: `src/brigade/fleet_deck_shell.py`
- Modify: `src/brigade/fleet_deck_overview.py` (append the sidebar section)
- Modify: `src/brigade/ui_theme.py` (shell CSS inside `BASE_CSS`, `OVERVIEW_CSS` constant)
- Modify: `tests/test_fleet_deck_overview.py` (import block, append a section)
- Modify: `tests/test_ui_theme.py` (append)

- [x] **Step 1: Write the failing tests.** In `tests/test_fleet_deck_overview.py` add `from brigade import fleet_deck_shell` directly below the `from brigade import fleet_deck_overview as overview` line, then append two blank lines and:

```python
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
    refreshed = fleet_deck_shell.deck_page("t", "overview", "", "", "n", as_of="x")
    assert '<meta http-equiv="refresh" content="10">' in refreshed
    hostile = fleet_deck_shell.deck_page("t", '"><x>', "", "", "n", as_of="x")
    assert 'data-active="&quot;&gt;&lt;x&gt;"' in hostile
```

Append to `tests/test_ui_theme.py`:

```python
def test_shell_css_lives_in_base_css_and_overview_css_stays_separate():
    assert ".shell-sidebar" in ui_theme.BASE_CSS and ".tree-row[hidden]" in ui_theme.BASE_CSS
    assert "@media (max-width: 900px)" in ui_theme.BASE_CSS
    assert ".cards" in ui_theme.OVERVIEW_CSS and ".cards" not in ui_theme.BASE_CSS
    declared = set(re.findall(r"(--[\w-]+)\s*:", ui_theme.TOKENS))
    referenced = set(re.findall(r"var\(\s*(--[\w-]+)", ui_theme.BASE_CSS + ui_theme.OVERVIEW_CSS))
    assert referenced <= declared, referenced - declared
```

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py tests/test_ui_theme.py" --capture brigade-work
```

Expected: collection error `ImportError: cannot import name 'fleet_deck_shell' from 'brigade'`, and after that exists, `AttributeError: ... 'render_sidebar'` and `AttributeError: module 'brigade.ui_theme' has no attribute 'OVERVIEW_CSS'`.

- [x] **Step 3: Implement.** Create `src/brigade/fleet_deck_shell.py`:

```python
"""Two-column page shell for the Command Deck pages.

``deck_page`` wraps an already-escaped sidebar and body in the shared
``ui_theme.document`` and attaches the machine filter script. The script is a
constant: it builds the filter input with DOM calls, reads ``textContent`` and
the input value only, and never writes markup. Without JavaScript the input is
simply absent and the machine tree stays fully visible.
"""

from __future__ import annotations

import html

from . import ui_theme

FILTER_SCRIPT = (
    "(function () {"
    "var host = document.querySelector('[data-tree-filter]');"
    "if (!host) { return; }"
    "var input = document.createElement('input');"
    "input.type = 'search';"
    "input.placeholder = 'Filter machines';"
    "input.setAttribute('aria-label', 'Filter machines and cloud providers');"
    "host.appendChild(input);"
    "function apply() {"
    "var query = input.value.toLowerCase();"
    "document.querySelectorAll('[data-filter-row]').forEach(function (row) {"
    "row.hidden = query !== '' && (row.textContent || '').toLowerCase().indexOf(query) === -1;"
    "});}"
    "try { input.value = sessionStorage.getItem('deck-filter') || ''; } catch (error) { input.value = ''; }"
    "input.addEventListener('input', function () {"
    "try { sessionStorage.setItem('deck-filter', input.value); } catch (error) { /* storage blocked */ }"
    "apply();});"
    "apply();"
    "})();"
)


def deck_page(
    title: str,
    active: str,
    sidebar_html: str,
    body_html: str,
    nonce: str,
    *,
    as_of: str,
    refresh_seconds: int | None = 10,
    extra_css: str = "",
) -> str:
    """The shared shell. *sidebar_html* and *body_html* must already be escaped."""
    shell = (
        f'<div class="shell" data-active="{html.escape(active, quote=True)}">'
        f"{sidebar_html}"
        f'<div class="shell-main">{body_html}</div></div>'
    )
    return ui_theme.document(
        title,
        nonce,
        shell,
        as_of=as_of,
        refresh_seconds=refresh_seconds,
        extra_css=extra_css,
        script=FILTER_SCRIPT,
    )
```

Append two blank lines and this section to `src/brigade/fleet_deck_overview.py`:

```python
# --- sidebar ----------------------------------------------------------------------


NAV_ITEMS = (
    ("overview", "/deck", "overview"),
    ("repos", "/deck/repos", "repos"),
    ("work", "/deck/work", "work"),
    ("roster", "/deck/roster", "roster"),
    ("policy", "/deck/policy", "policy"),
    ("boards", "/view/machines", "boards"),
)


def _tree_summary(station: StationView, status: str) -> str:
    if status == "offline":
        return "not enrolled" if not station.enrolled else "offline"
    if status == "stale":
        return "stale"
    return f"{station.busy} busy" if station.busy else "idle"


def _tree_run(tile: Tile) -> str:
    run = tile.run
    return (
        f'<li class="tree-run"><span class="tree-run-name">{_esc(run.repo or UNKNOWN)} · {_esc(run.seat or UNKNOWN)}'
        f'</span><span class="tree-time">{_esc(format_age(run.elapsed_seconds))}</span></li>'
    )


def _machine_tree(view: DeckView) -> str:
    rows = []
    for station in view.stations:
        status = station_status(station, machine_for(view, station))
        runs = "".join(_tree_run(tile) for tile in station.tiles)
        rows.append(
            f'<li class="tree-row" data-filter-row title="{_esc(station.station.node_id)}"><div class="tree-line">'
            f'<span class="dot dot--{status}" aria-hidden="true"></span>'
            f'<span class="tree-label">{_esc(station.label)}</span>'
            f'<span class="tree-meta">{_esc(_tree_summary(station, status))}</span></div>'
            + (f'<ul class="tree-children">{runs}</ul>' if runs else "")
            + "</li>"
        )
    body = f'<ul class="tree-list">{"".join(rows)}</ul>' if rows else '<p class="tree-empty">No stations.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-machines"><h2 id="tree-machines" class="tree-heading">'
        f'Machines <span class="nav-count">{len(view.stations)}</span></h2>{body}</section>'
    )


def _cloud_tree(view: DeckView) -> str:
    rows = "".join(
        '<li class="tree-row" data-filter-row><div class="tree-line">'
        f'<span class="tree-label">{_esc(worker.provider)}</span>'
        f'<span class="tree-meta">{worker.used}/{worker.limit}'
        + ("" if worker.circuit_state == "closed" else f" · {_esc(worker.circuit_state)}")
        + "</span></div></li>"
        for worker in view.cloud_workers
    )
    body = f'<ul class="tree-list">{rows}</ul>' if rows else '<p class="tree-empty">None configured.</p>'
    return (
        '<section class="tree" aria-labelledby="tree-cloud"><h2 id="tree-cloud" class="tree-heading">'
        f'Cloud <span class="nav-count">{len(view.cloud_workers)}</span></h2>{body}</section>'
    )


def _sidebar_foot(view: DeckView, now: datetime) -> str:
    plane = view.control_plane
    ages = [] if plane is None else [seconds_since(machine.last_observed_at, now) for machine in plane.machines]
    known = [age for age in ages if age is not None]
    text = f"Hub telemetry {format_age(min(known))} ago" if known else "Hub telemetry unknown"
    return f'<p class="sidebar-foot">{_esc(text)}</p>'


def render_sidebar(view: DeckView | None, active: str, now: datetime, *, worklore_enabled: bool) -> str:
    """Brand, page nav, and (when a view is available) the machine and cloud trees.

    Pages that do not build a ``DeckView`` pass ``None`` and get the nav only.
    """
    links = []
    for key, href, label in NAV_ITEMS:
        if key == "work" and not worklore_enabled:
            continue
        current = ' aria-current="page"' if key == active else ""
        count = (
            f' <span class="nav-count">{len(view.stations)}</span>' if key == "overview" and view is not None else ""
        )
        links.append(f'<a href="{href}"{current}>{label}{count}</a>')
    parts = [
        '<aside class="shell-sidebar" aria-label="Fleet navigation"><div class="brand">'
        '<p class="brand-name">Brigade Fleet Hub</p><p class="brand-sub">self-hosted</p></div>',
        f'<nav class="deck-nav" aria-label="Command Deck">{"".join(links)}</nav>',
    ]
    if view is not None:
        parts += ['<div class="tree-filter" data-tree-filter></div>', _machine_tree(view), _cloud_tree(view)]
        parts.append(_sidebar_foot(view, now))
    parts.append("</aside>")
    return "".join(parts)
```

In `src/brigade/ui_theme.py` insert the shell rules into the `BASE_CSS` string directly above the line `@media (max-width: 700px) {` (the block that starts `.deck-shell { padding: var(--space-3); }`):

```css
.shell { display: grid; grid-template-columns: 248px minmax(0, 1fr); min-height: 100vh; }
.shell-main { min-width: 0; }
.shell-sidebar { position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto; padding: var(--space-4); border-right: 1px solid var(--line-quiet); background: var(--surface); }
.brand { padding: var(--space-1) 0; }
.brand-name { margin: 0; font-size: 15px; font-weight: 750; }
.brand-sub { margin: 0; color: var(--muted); font-size: 12px; }
.shell-sidebar .deck-nav { flex-direction: column; flex-wrap: nowrap; gap: var(--space-1); margin: var(--space-4) 0; }
.shell-sidebar .deck-nav a { justify-content: space-between; border-color: transparent; text-transform: capitalize; }
.shell-sidebar .deck-nav a[aria-current="page"] { border-color: var(--line); background: var(--surface-raised); color: var(--ink); }
.nav-count { margin-left: var(--space-2); color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tree-filter input { width: 100%; min-height: 36px; padding: var(--space-2) 10px; border: 1px solid var(--line); background: var(--surface-raised); color: var(--ink); font: inherit; }
.tree-heading { display: flex; justify-content: space-between; margin: var(--space-4) 0 var(--space-2); color: var(--muted); font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
.tree-list, .tree-children { margin: 0; padding: 0; list-style: none; }
.tree-row[hidden] { display: none; }
.tree-line { display: flex; align-items: center; gap: var(--space-2); min-height: 28px; }
.tree-label { flex: 1; min-width: 0; font-weight: 650; }
.tree-meta { color: var(--muted); font-size: 12px; white-space: nowrap; }
.tree-children { margin: 0 0 var(--space-1) var(--space-1); padding-left: var(--space-3); border-left: 1px solid var(--line-quiet); }
.tree-run { display: flex; justify-content: space-between; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.tree-run-name { min-width: 0; }
.tree-time { white-space: nowrap; font-variant-numeric: tabular-nums; }
.tree-empty { margin: 0; color: var(--faint); font-size: 12px; }
.sidebar-foot { margin: var(--space-4) 0 0; color: var(--faint); font-size: 12px; }
.dot { display: inline-block; flex: 0 0 auto; width: 8px; height: 8px; border-radius: 999px; background: var(--offline); }
.dot--working { background: var(--working); }
.dot--idle { background: var(--idle); }
.dot--stale { background: var(--signal); }
.dot--offline { background: var(--offline); }
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .shell-sidebar { position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line-quiet); }
  .shell-sidebar .deck-nav { flex-direction: row; flex-wrap: wrap; margin: var(--space-3) 0 0; }
  .shell-sidebar .tree, .tree-filter, .sidebar-foot { display: none; }
}
```

and add the overview stylesheet as a module constant directly above `_HEAD_LINKS = (`:

```python
# Main-column rules for the Fleet overview page (/deck) only.
OVERVIEW_CSS = """
.overview > * + * { margin-top: var(--space-4); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
.crumbs { margin: 0 0 var(--space-1); color: var(--muted); font-size: 12px; }
.page-head h1 { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-3); margin: 0; }
.page-sub { margin: var(--space-1) 0 0; color: var(--muted); }
.verdict-pill { padding: 2px var(--space-3); border: 1px solid var(--line); border-radius: 999px; font-size: 11px; font-weight: 800; letter-spacing: .06em; }
.verdict-pill--attention { border-color: var(--warn-line); background: var(--warn-surface); color: var(--signal); }
.verdict-pill--unknown { color: var(--muted); }
.verdict-pill--clear { border-color: var(--ok); color: var(--ok); }
.refresh-link { margin-left: var(--space-2); color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-4); }
.card { min-width: 0; padding: var(--space-4); border: 1px solid var(--line-quiet); background: var(--surface); }
.card-title { margin: 0; color: var(--muted); font-size: 12px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.card-value { margin: var(--space-1) 0 0; font-size: 28px; font-weight: 750; font-variant-numeric: tabular-nums; }
.card-sub { margin: 0 0 var(--space-2); color: var(--muted); font-size: 12px; }
.card--working .card-value { color: var(--working); }
.card--idle .card-value { color: var(--idle); }
.card--offline .card-value { color: var(--offline); }
progress { appearance: none; -webkit-appearance: none; display: block; width: 100%; height: 6px; border: 0; border-radius: 999px; overflow: hidden; background: var(--bar-track); color: var(--working); }
progress::-webkit-progress-bar { background: var(--bar-track); }
progress::-webkit-progress-value { background: var(--working); }
progress::-moz-progress-bar { background: var(--working); }
.bar { margin-top: var(--space-2); }
.bar--thin { height: 4px; margin-top: var(--space-1); }
.alerts { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
.alert { display: flex; gap: var(--space-3); padding: var(--space-3); border: 1px solid var(--warn-line); background: var(--warn-surface); }
.alert-icon { flex: 0 0 auto; width: 20px; height: 20px; border-radius: 999px; background: var(--signal); color: var(--on-accent); font-weight: 800; line-height: 20px; text-align: center; }
.alert-body { min-width: 0; }
.alert-title, .alert-detail, .alert-since { margin: 0; }
.alert-kind { color: var(--signal); font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
.alert-detail { color: var(--muted); font-size: 12px; }
.alert-since { color: var(--faint); font-size: 12px; }
.split { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-4); }
.split > .panel + .panel { margin-top: 0; }
.rows { display: grid; margin: 0; padding: 0; list-style: none; }
.row { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3) 0; border-top: 1px solid var(--line-quiet); }
.row:first-child { padding-top: 0; border-top: 0; }
.row .dot { margin-top: 6px; }
.row-body { flex: 1; min-width: 0; }
.row-title, .row-meta { margin: 0; }
.row-meta { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); color: var(--muted); font-size: 12px; }
.row-time { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.machine-table { min-width: 640px; }
.session-table { min-width: 480px; }
.machine-table th:nth-child(1) { width: 24%; }
.machine-table th:nth-child(2) { width: 26%; }
.machine-table th:nth-child(3) { width: 12%; }
.machine-table th:nth-child(4) { width: 8%; }
.machine-table th:nth-child(5) { width: 15%; }
.machine-table th:nth-child(6) { width: 15%; }
.session-table th:nth-child(n) { width: auto; }
.machine-name { color: var(--ink); font-weight: 650; }
.sub { display: block; color: var(--faint); font-size: 11px; }
.seats { display: inline-flex; gap: 2px; margin-right: var(--space-2); vertical-align: middle; }
.seg { width: 8px; height: 12px; border-radius: 2px; }
.seg--busy { background: var(--working); }
.seg--free { background: var(--idle); }
.seats-wide { background: var(--idle); }
.seats-wide::-webkit-progress-bar { background: var(--idle); }
.seat-text { color: var(--muted); }
@media (max-width: 900px) {
  .page-head { flex-direction: column; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .split { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
  .cards { grid-template-columns: 1fr; }
}
"""
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py tests/test_ui_theme.py tests/test_ui_theme_integration.py" --capture brigade-work
```

Expected: all pass (`test_fleet_deck_overview.py` reports 41 tests).

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_deck_shell.py src/brigade/fleet_deck_overview.py src/brigade/ui_theme.py tests/test_fleet_deck_overview.py tests/test_ui_theme.py
git commit -m "$(cat <<'EOF'
feat(deck): add the two-column shell, sidebar, and overview styles

The sidebar carries the page nav, a machine tree with a no-JS-safe filter, a
cloud list, and hub telemetry age. The filter script is a constant that reads
textContent only. Light and dark both come from tokens.
EOF
)"
```

## Task 12: Render `/deck` and every Deck page through the shell

**Files:**
- Modify: `src/brigade/fleet_command_deck.py` (imports at line 27, `render_deck` 1688-1811, `_document` 1944-1953)
- Modify: `src/brigade/fleet_hub_roster_page.py` (433-437, 550), `src/brigade/fleet_policy_page.py` (1872-1876, 1899), `src/brigade/fleet_repo_policy_page.py` (379-381, 394), `src/brigade/fleet_work_page.py` (152-154, 167, 208-209, 226)
- Modify tests: `tests/test_fleet_deck_overview.py`, `tests/test_fleet_command_deck.py`, `tests/test_ui_theme_integration.py`, `tests/test_fleet_deck_badges.py`, `tests/test_fleet_dashboard.py`, `tests/test_fleet_work_page.py`

- [x] **Step 1: Write the failing tests and update every assertion that pinned the old markup.** Append two blank lines and this section to `tests/test_fleet_deck_overview.py`:

```python
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
```

Append to `tests/test_fleet_command_deck.py`:

```python
def test_deck_http_joins_stations_to_telemetry_by_node_id(tmp_path):
    with _start_hub(tmp_path, CONFIG) as (hub, db):
        for node_id, label in ((NODE_A, "Alpha"), (NODE_B, "Beta")):
            body = {"action": "add", "node_id": node_id, "label": label}
            assert _request(hub, "POST", "/nodes", headers=_bearer(), body=body)[0] == 200
        conn = fleet_hub.open_db(db)
        try:
            fleet_hub_policy.save_policy(
                conn, _policy_machines(), expected_version=1, actor="operator", reason="seed machines"
            )
            conn.commit()
        finally:
            conn.close()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        _insert_telemetry(db, NODE_A, observed_at=now.isoformat())
        _insert_telemetry(db, NODE_B, observed_at=(now - timedelta(hours=2)).isoformat())
        status, _headers, page = _request(hub, "GET", "/deck", headers=_bearer())
        assert status == 200, page

        def card(name: str) -> str:
            found = re.search(rf'data-card="{name}">.*?class="card-value">([^<]*)<', page, re.S)
            assert found, name
            return found.group(1)

        assert card("working") == "0"
        assert card("idle") == "4"
        assert card("offline") == "1"
        rows = re.findall(r"<tr data-status=.*?</tr>", page, re.S)
        alpha = next(row for row in rows if ">Alpha<" in row)
        beta = next(row for row in rows if ">Beta<" in row)
        assert "idle-capacity" in alpha and "stale-telemetry" not in alpha
        assert "stale-telemetry" in beta and "dot--stale" in beta
        assert 'class="alert-kind">stale telemetry<' in page
```

Intentional assertion changes (the PR lists these):

`tests/test_fleet_command_deck.py`

| Test | Old assertion | New assertion |
|---|---|---|
| `test_command_deck_is_root_and_legacy_boards_stay_under_view` | `assert '<a href="/">deck</a>' in root`, under a comment saying `href="/"` is the valid self-link | `assert '<a href="/deck" aria-current="page">overview <span class="nav-count">' in root`, comment reworded to "the sidebar marks Overview current". The `/view/machines` and `/view/repos` link assertions stay because the footer keeps both board links. |
| `test_headers_secrets_and_responsive_css` | `assert ".stations { grid-template-columns: 1fr; }" in body` | two lines: `assert "@media (max-width: 900px)" in body` and `assert ".shell-sidebar" in body` |
| `test_station_order_labels_capacity_and_timeline_bounds` | `assert "1/1 busy" in body and "1/2 busy" in body` | `assert "1 busy, 0 idle" in body and "1 busy, 1 idle" in body` |
| `test_collision_markers_and_observers_and_expired_claims` | three uses of `deck_body.split('id="rail"')` | `deck_body.split('id="alerts"')` in all three. `deck_body.count("! collision") >= 1` stays true through the working-row flag. |
| `test_cloud_workers_render_safe_active_leases_without_using_station_capacity` | `assert "1/4 busy" in body` | `assert "1 busy, 3 idle" in body` |

`tests/test_fleet_dashboard.py`, `test_interactive_sessions_stay_off_legacy_boards_and_station_capacity`: `assert "0/10 busy" in deck_body` becomes `assert "0 busy, 10 idle" in deck_body`.

`tests/test_fleet_work_page.py`, `test_observed_home_shows_snapshot_freshness_without_changing_active_slots`: `assert "1/1 slots busy" in body` becomes `assert "of 1 slots" in body and "1 busy, 0 idle" in body`, and `assert "1/1 slots busy" in without_group` becomes `assert "of 1 slots" in without_group and "1 busy, 0 idle" in without_group`.

`tests/test_fleet_work_page.py`, `test_observed_group_http_discovery_auth_and_read_only`: in `normal_home_routes`, `"/"` becomes `"/deck"`. The sidebar links the canonical `/deck` route, which serves the same page as `/`.

`tests/test_ui_theme_integration.py`:

- In `test_badge_palette_uses_nonce_stylesheet_without_inline_styles`: change the import to `from brigade.fleet_deck_brands import harness_brand`; change `assert len(badges) == 2 * len(names)` to `assert len(badges) == len(names)`; replace the three-line `for attrs, brand in zip(badges, [brand for name in names for brand in (harness_brand(name), provider_brand(name))], strict=True):` header with `for attrs, brand in zip(badges, [harness_brand(name) for name in names], strict=True):`; change `assert [attrs["title"] for attrs in badges[-2:]] == [hostile, hostile]` to `assert [attrs["title"] for attrs in badges[-1:]] == [hostile]`. Working rows carry the harness badge only.
- Replace `test_populated_deck_separates_station_grid_from_following_panel` (the old `.stations` grid check) with:

```python
def test_populated_deck_orders_summary_alerts_work_and_machines():
    station = deck.StationView(
        deck.StationConfig("fixture-node", "Example", 2), "Example", True, 0, NOW.isoformat(), ()
    )
    view = deck.DeckView(stations=(station,), rail=(), repos=(), outcomes=(), observers=())
    rendered = deck.render_deck(view, nonce="spacing-nonce", now=NOW)
    markers = [
        'aria-label="Fleet summary"',
        'id="alerts"',
        'aria-labelledby="working-title"',
        'id="machines-table"',
    ]
    positions = [rendered.index(marker) for marker in markers]
    assert positions == sorted(positions)
    stylesheet = rendered.split('<style nonce="spacing-nonce">', 1)[1].split("</style>", 1)[0]
    # The shared 16px step still separates the sections of the main column.
    assert ".overview > * + * { margin-top: var(--space-4); }" in stylesheet
```

`tests/test_fleet_deck_badges.py`: add `from brigade import fleet_deck_overview as overview` below the `fleet_deck_brands` import, then replace every test from `test_genuinely_absent_fields_stay_unknown` to the end of the file (the tile, card, snapshot, cloud, and session tests that called `deck._tile_html`, `deck._cloud_worker_html`, and `deck._interactive_session_html`) with:

```python
def _station_view(*tiles: deck.Tile) -> deck.DeckView:
    station = deck.StationView(
        station=deck.StationConfig(NODE_A, "Example", 4),
        label="Example",
        enrolled=True,
        busy=len(tiles),
        last_heard=None,
        tiles=tiles,
    )
    return deck.DeckView(stations=(station,), rail=(), repos=(), outcomes=(), observers=())


def _working_row(tile: deck.Tile) -> str:
    html = overview.render_work_lists(_station_view(tile))
    match = re.search(r'<li class="row".*?</li>', html, re.DOTALL)
    assert match
    return match.group(0)


def test_genuinely_absent_fields_stay_unknown(conn: sqlite3.Connection) -> None:
    _event(conn, "ghost-1", 1, "run.started")
    (run,) = deck.fetch_live_runs(conn, now=NOW, stale_after_seconds=1800)
    assert run.seat == ""
    assert run.harness == ""
    assert run.provider == ""
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    assert "unknown" in html
    assert ">/" not in html


def test_row_falls_back_to_claim_conductor_for_seat() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="worker-9",
        repo="repo",
        seat="",
        harness="",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
    )
    claim = deck.Claim(target="repo", owner_node="node-label", owner_conductor="muse13", ttl_remaining=887)
    html = _working_row(deck.Tile(run=run, claim=claim, collision=False))
    assert "muse13" in html
    assert "seat /" not in html


def test_row_renders_the_harness_badge_and_keeps_the_model_in_a_tooltip() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="run-claude-1",
        repo="repo",
        seat="claude_standby",
        harness="claude",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
        provider="anthropic",
        model="opus",
        launch_model="opus",
    )
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    assert html.count("<svg") == 1
    assert 'title="claude"' in html and 'aria-label="Claude"' in html
    assert 'title="anthropic"' not in html
    assert '<p class="row-meta" title="opus">' in html
    assert "claude_standby" in html


def test_row_snapshot() -> None:
    run = deck.LiveRun(
        node_id=NODE_A,
        run_id="run-muse13-1",
        repo="repo",
        seat="muse13",
        harness="opencode",
        state="run.started",
        bucket="running",
        age_seconds=10,
        elapsed_seconds=10,
        provider="opencode",
        model="opencode-muse-spark-1.3-contributor-free",
        launch_model="opencode/muse-spark-1.3-contributor-free",
    )
    html = _working_row(deck.Tile(run=run, claim=None, collision=False))
    oc = deck.harness_brand("opencode")
    assert oc.svg != ""
    assert html == (
        '<li class="row" title="run-muse13-1"><span class="dot dot--working" aria-hidden="true"></span>'
        '<div class="row-body"><p class="row-title">'
        f'<a href="/view/machines?node={NODE_A}&amp;repo=repo">repo</a></p>'
        '<p class="row-meta" title="opencode/muse-spark-1.3-contributor-free">'
        '<span class="badge badge-color-2b6cb0" title="opencode" aria-label="OpenCode">' + oc.svg + "</span> "
        "muse13 · Example</p></div>"
        '<span class="row-time">10s</span></li>'
    )


def test_cloud_and_session_badges() -> None:
    worker = deck.CloudWorker(provider="cursor", used=1, limit=3, circuit_state="closed", leases=())
    view = deck.DeckView(stations=(), rail=(), repos=(), outcomes=(), observers=(), cloud_workers=(worker,))
    html = overview.render_cloud_workers(view)
    assert 'title="cursor"' in html and 'aria-label="Cursor"' in html and "<svg" in html
    session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="claude",
        session_id="sess-1",
        repo_identity="github.com/example/project",
        identity_scope="fleet",
        repo_label="project",
        checkout_path="/tmp/project",
        branch="main",
        dirty_paths=("src/a.py",),
        dirty_truncated=False,
    )
    unknown_session = deck.InteractiveSession(
        node_id=NODE_A,
        harness="",
        session_id="sess-2",
        repo_identity="github.com/example/other",
        identity_scope="fleet",
        repo_label="other",
        checkout_path="/tmp/other",
        branch=None,
        dirty_paths=(),
        dirty_truncated=False,
    )
    sessions = deck.DeckView(
        stations=(), rail=(), repos=(), outcomes=(), observers=(), interactive_sessions=(session, unknown_session)
    )
    session_html = overview.render_sessions(sessions)
    assert "<svg" in session_html and 'aria-label="Claude"' in session_html
    assert ">??<" in session_html
```

The old `test_card_renders_both_badges_and_model_text` expected two badges and a `tile-model` paragraph; the new row test expects one harness badge and the model in the row tooltip. `test_card_snapshot` becomes `test_row_snapshot`.

- [x] **Step 2: Run and see them fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py tests/test_fleet_command_deck.py tests/test_ui_theme_integration.py tests/test_fleet_deck_badges.py tests/test_fleet_dashboard.py tests/test_fleet_work_page.py" --capture brigade-work
```

Expected failures include `test_old_station_cards_and_script_are_gone` (`station-card` still rendered in the body), `test_deck_http_joins_stations_to_telemetry_by_node_id` (no `data-card` in the old page), the updated assertions above, The row tests in `tests/test_fleet_deck_badges.py` already pass because they call the overview module directly.

- [x] **Step 3: Implement.** In `src/brigade/fleet_command_deck.py` change `from . import ui_theme` to `from . import fleet_deck_shell, ui_theme`.

Replace the whole `render_deck` function (from `def render_deck(` through the line before `def repo_coordination_table`) with:

```python
def render_deck(view: DeckView, *, nonce: str, now: datetime, worklore_enabled: bool = True) -> str:
    # Imported here because fleet_deck_overview imports this module for its types.
    from . import fleet_deck_overview

    body = fleet_deck_overview.render_overview(view, now, worklore_enabled=worklore_enabled)
    return _document(
        body,
        nonce=nonce,
        now=now,
        active="overview",
        view=view,
        worklore_enabled=worklore_enabled,
        extra_css=ui_theme.OVERVIEW_CSS,
    )
```

Replace the whole `_document` function at the end of the file with:

```python
def _document(
    body: str,
    *,
    nonce: str,
    now: datetime,
    title: str = "Command Deck",
    refresh: bool = True,
    active: str = "overview",
    view: DeckView | None = None,
    worklore_enabled: bool | None = None,
    extra_css: str = "",
) -> str:
    """Every Deck page: its body inside the shared two-column shell.

    *view* feeds the sidebar machine tree; pages that never build a
    ``DeckView`` pass ``None`` and get the navigation only.
    """
    # Imported here because fleet_deck_overview imports this module for its types.
    from . import fleet_deck_overview

    if worklore_enabled is None:
        from . import worklore_http

        worklore_enabled = worklore_http.enabled()
    sidebar = fleet_deck_overview.render_sidebar(view, active, now, worklore_enabled=worklore_enabled)
    return fleet_deck_shell.deck_page(
        title,
        active,
        sidebar,
        body,
        nonce,
        as_of=_stamp(now),
        refresh_seconds=10 if refresh else None,
        extra_css=_STYLE + extra_css,
    )
```

In `src/brigade/fleet_hub_roster_page.py` delete the inline nav block

```python
    parts.append(
        '<nav class="deck-nav" aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/view/machines">machines board</a></nav>'
    )
```

and change the last line to:

```python
    return fleet_command_deck._document(
        "\n".join(parts), nonce=nonce, now=now, title="Roster", refresh=False, active="roster"
    )
```

In `src/brigade/fleet_policy_page.py` delete the identical inline nav block and change the last line to:

```python
    return fleet_command_deck._document(
        "\n".join(parts), nonce=nonce, now=now, title="Policy", refresh=False, active="policy"
    )
```

In `src/brigade/fleet_repo_policy_page.py` delete these three lines from the `body` string (the nav) and change the last line to the call below them:

```python
        '<nav class="deck-nav" aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/view/machines">machines board</a></nav>'
```

```python
    return fleet_command_deck._document(
        body, nonce=nonce, now=now, title="Repos", refresh=False, active="repos", view=view
    )
```

In `src/brigade/fleet_work_page.py` delete the three-line nav in `render`

```python
        '<nav class="deck-nav" aria-label="Command Deck"><a href="/">deck</a> <a href="/deck/repos">repos</a> '
        '<a href="/deck/roster">roster</a> <a href="/deck/policy">policy</a> '
        '<a href="/deck/work" aria-current="page">work</a> <a href="/view/machines">machines board</a></nav>'
```

(keep the `pagination` navs). In `render_observed_group` replace the two lines

```python
        '<nav class="deck-nav" aria-label="Command Deck"><a href="/deck">deck</a>'
        '<a href="/deck/work">work</a></nav><section class="panel"><h2>Retained task observations</h2>'
```

with the single line

```python
        '<section class="panel"><h2>Retained task observations</h2>'
```

and change the two returns to:

```python
    return deck._document(
        body, nonce=nonce, now=now, title="Worklore", refresh=False, active="work", worklore_enabled=True
    )
```

```python
    return deck._document(
        body, nonce=nonce, now=now, title=group.label, refresh=False, active="work", worklore_enabled=True
    )
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py tests/test_fleet_command_deck.py tests/test_ui_theme_integration.py tests/test_fleet_deck_badges.py tests/test_fleet_dashboard.py tests/test_fleet_work_page.py tests/test_fleet_repo_policy_page.py tests/test_fleet_roster_page.py tests/test_fleet_policy_page.py tests/test_fleet_enrollment.py tests/test_fleet_claim_generations.py" --capture brigade-work
```

Expected: every test passes. If an assertion outside the table above fails because the sidebar now contributes text, stop and report the test name and line instead of loosening it.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_command_deck.py src/brigade/fleet_hub_roster_page.py src/brigade/fleet_policy_page.py src/brigade/fleet_repo_policy_page.py src/brigade/fleet_work_page.py tests/test_fleet_deck_overview.py tests/test_fleet_command_deck.py tests/test_ui_theme_integration.py tests/test_fleet_deck_badges.py tests/test_fleet_dashboard.py tests/test_fleet_work_page.py
git commit -m "$(cat <<'EOF'
feat(deck): render the overview and every Deck page in the two-column shell

/deck becomes the Fleet overview. Repos, work, roster, and policy keep their
bodies inside the shell with the navigation moved into the sidebar. Station
cards stop rendering; assertions that pinned the old markup are updated.
EOF
)"
```

## Task 13: Delete the dead station-card code

**Files:**
- Modify: `src/brigade/fleet_command_deck.py`
- Modify: `tests/test_fleet_deck_overview.py` (import block, append a section)

- [x] **Step 1: Write the failing test.** Add `from pathlib import Path` to the import block of `tests/test_fleet_deck_overview.py` (below the `datetime` import, above the `brigade` imports), then append two blank lines and:

```python
def test_deck_modules_stay_inside_their_size_budgets():
    source = Path(deck.__file__).parent

    def lines(name: str) -> int:
        return len((source / name).read_text(encoding="utf-8").splitlines())

    assert lines("fleet_command_deck.py") < 1953
    assert lines("fleet_deck_overview.py") < 700
    assert lines("fleet_deck_shell.py") < 250


def test_the_station_card_code_is_gone_from_the_command_deck_module():
    for name in ("_tile_html", "_interactive_session_html", "_cloud_worker_html", "_empty_rail_text", "_SCRIPT"):
        assert not hasattr(deck, name), name
    assert not hasattr(deck, "heard_age")
    css = deck._STYLE
    for selector in (".station-card", ".stations", ".run-stack", ".dashboard-grid", ".attention-panel", ".capacity"):
        assert selector not in css, selector
```

- [x] **Step 2: Run and see it fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py::test_the_station_card_code_is_gone_from_the_command_deck_module tests/test_fleet_deck_overview.py::test_deck_modules_stay_inside_their_size_budgets" --capture brigade-work
```

Expected: `test_the_station_card_code_is_gone_from_the_command_deck_module` fails with `AssertionError: _tile_html`; the size test already passes.

- [x] **Step 3: Implement.** In `src/brigade/fleet_command_deck.py` delete exactly these definitions (locate each by its first line; each runs up to, not including, the next `def` or the next listed item):

1. `_SCRIPT = (` through the end of `def _empty_rail_text(view: DeckView) -> str:` (both sit directly above the new `render_deck`).
2. `def _tile_html(tile: Tile) -> str:` and `def _interactive_session_html(session: InteractiveSession) -> str:` (directly below `repo_coordination_table`, above `def _repo_flags`).
3. `def _cloud_worker_html(worker: CloudWorker) -> str:` and `def heard_age(received_at: str | None) -> str:` (directly above `def _owner_text`).

In `_STYLE` delete these rule lines (whole line each; identify by the leading text):

```
.stations { display: grid; ...
.stations + .panel { ...
.station-card { ...
.station-card > header { ...
.capacity { ...
.not-enrolled { ...
.run-stack { ...
.repo-name { ...
.tile-side { ...
.tile-badges { ...
.tile-model { ...
.dashboard-grid { ...
.attention-panel { border-color: #6d5635; }
```

Then replace the final media block in `_STYLE`

```
@media (max-width: 700px) {
  .stations { grid-template-columns: 1fr; }
  .tile { width: 100%; }
  .station-card > header { align-items: start; flex-direction: column; }
  .capacity { text-align: left; }
  .dashboard-grid { grid-template-columns: 1fr; }
  .timeline li { grid-template-columns: 1fr; gap: 2px; }
}
```

with

```
@media (max-width: 700px) {
  .timeline li { grid-template-columns: 1fr; gap: 2px; }
}
```

The rules for `.badge*`, `.cloud-*`, `.tile-facts`, `.work-*`, `.claim`, `.collision`, `.attention-list`, `.attention-kind`, `.timeline*`, `.observer-list`, `th:nth-child`, `.roster-table`, and `.seat--off` stay: other pages and the new sections still use them.

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fleet_deck_overview.py tests/test_fleet_command_deck.py tests/test_ui_theme_integration.py tests/test_fleet_deck_badges.py tests/test_module_size_ratchet.py" --capture brigade-work
```

Expected: all pass. `wc -l src/brigade/fleet_command_deck.py` reports 1763.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_command_deck.py tests/test_fleet_deck_overview.py
git commit -m "$(cat <<'EOF'
refactor(deck): remove the station-card renderers and their styles

The overview module replaced them, so fleet_command_deck.py drops from 1953 to
1763 lines.
EOF
)"
```

## Task 14: No literal colors outside the token block

Literals found in page CSS before this task (the document stylesheet in each page head):

| Location | Literal | Resolution |
|---|---|---|
| `src/brigade/ui_theme.py:83` `.empty` | `#141b1c` | `var(--surface-inset)` (Task 1) |
| `src/brigade/ui_theme.py:85` `.callout--error` | `#b0553a` | `var(--error-line)` (Task 1) |
| `src/brigade/ui_theme.py:87` `.banner--error` | `#b0553a` | `var(--error-line)` (Task 1) |
| `src/brigade/fleet_command_deck.py:1641` `.attention-panel` | `#6d5635` | rule deleted with the old section (Task 13); alert cards use `var(--warn-line)` |
| `src/brigade/fleet_command_deck.py:1620` `.badge` | `#fff` | `var(--on-accent)` (this task) |
| `src/brigade/fleet_command_deck.py:1605-1609` generated `.badge-color-<hex>` | brand accents | exempt by design (deviation 8) |
| `src/brigade/ui_theme.py:134` `<meta name="theme-color">` | `#111617` | an attribute, not CSS; unchanged |

Center view modules keep their own palettes (deviation 7); the test inspects only the document stylesheet in each page head.

**Files:**
- Modify: `src/brigade/fleet_command_deck.py` (the `.badge` rule in `_STYLE`)
- Modify: `tests/test_ui_theme_integration.py` (append)

- [x] **Step 1: Write the failing test.** Append to `tests/test_ui_theme_integration.py`:

```python
_LITERAL_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(", re.IGNORECASE)
# Vendor brand fills are fixed marks that must not change with the scheme.
_BRAND_RULE = re.compile(r"\.badge-color-[0-9a-f]+\s*\{[^}]*\}")


def test_page_css_has_no_literal_colors_outside_the_token_block(fleet_pages):
    pages = {**fleet_pages, "/center/status": _center_status_page()}
    for route, rendered in pages.items():
        css = _document_css(rendered).replace(ui_theme.TOKENS, "").replace(ui_theme.DARK_TOKENS, "")
        css = _BRAND_RULE.sub("", css)
        assert _LITERAL_COLOR.findall(css) == [], route


def test_token_block_is_where_the_literals_live():
    assert _LITERAL_COLOR.search(ui_theme.TOKENS)
    assert _LITERAL_COLOR.search(ui_theme.DARK_TOKENS)
    assert not _LITERAL_COLOR.search(ui_theme.BASE_CSS)
    assert not _LITERAL_COLOR.search(ui_theme.OVERVIEW_CSS)
```

- [x] **Step 2: Run and see it fail.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_ui_theme_integration.py::test_page_css_has_no_literal_colors_outside_the_token_block tests/test_ui_theme_integration.py::test_token_block_is_where_the_literals_live" --capture brigade-work
```

Expected: the first fails with `assert ['#fff'] == []` for `/deck` (and the other Deck routes); the second passes.

- [x] **Step 3: Implement.** In `_STYLE` change the `.badge` rule's `color: #fff;` to `color: var(--on-accent);`, so the rule reads:

```
.badge { display: inline-flex; align-items: center; justify-content: center; min-width: 26px; min-height: 22px; padding: 2px 6px; border-radius: 999px; color: var(--on-accent); font-size: 11px; font-weight: 800; letter-spacing: .04em; text-align: center; white-space: nowrap; }
```

- [x] **Step 4: Run to green.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_ui_theme.py tests/test_ui_theme_integration.py tests/test_fleet_deck_badges.py" --capture brigade-work
```

Expected: all pass.

- [x] **Step 5: Commit.**

```bash
git add src/brigade/fleet_command_deck.py tests/test_ui_theme_integration.py
git commit -m "$(cat <<'EOF'
test(ui): forbid literal colors in page CSS outside the token block

Badge text uses the on-accent token. Vendor brand fills stay literal on purpose.
EOF
)"
```

## Task 15: Changelog and the focused gate

**Files:**
- Modify: `CHANGELOG.md` (the `## [Unreleased]` list)

- [x] **Step 1: Add the changelog entry.** Insert as the first bullet under `## [Unreleased]`:

```markdown
- The Command Deck opens on a Fleet overview: a left sidebar with the page nav, a machine tree, and cloud providers; four summary cards; an alerts strip with first-observed times; working and queued lists; and a machines table with seat and load bars. Deck pages and the fleet boards follow the system light or dark scheme. The routing snapshot gains an additive `node_id` per machine so stations join to machines by node id. Center stays dark for now.
```

- [x] **Step 2: Run every focused test file.**

```bash
brigade work verify run --target . --command ".venv/bin/python -m pytest -q -p no:cacheprovider tests/test_module_size_ratchet.py tests/test_ui_theme.py tests/test_ui_theme_integration.py tests/test_fleet_command_deck.py tests/test_fleet_deck_overview.py tests/test_fleet_deck_badges.py tests/test_fleet_dashboard.py tests/test_center_dashboard.py tests/test_center_page_load.py tests/test_fleet_roster_page.py tests/test_fleet_policy_page.py tests/test_fleet_repo_policy_page.py tests/test_fleet_work_page.py tests/test_fleet_routing.py tests/test_fleet_enrollment.py tests/test_fleet_claim_generations.py" --capture brigade-work
```

Expected: all pass, 0 failed. Read the log with the recipe above and record the pass count in the PR.

- [x] **Step 3: Lint, format, and types.**

```bash
brigade work verify run --target . --command ".venv/bin/ruff check ." --capture brigade-work
brigade work verify run --target . --command ".venv/bin/ruff format --check ." --capture brigade-work
brigade work verify run --target . --command ".venv/bin/mypy" --capture brigade-work
```

Expected: `All checks passed!`, no files would be reformatted, and `Success: no issues found`.

- [x] **Step 4: Content guard on the new tracked files.**

```bash
brigade guard scan src/brigade/fleet_deck_overview.py --policy src/brigade/guard/policies/public-repo.json
brigade guard scan src/brigade/fleet_deck_shell.py --policy src/brigade/guard/policies/public-repo.json
brigade guard scan tests/test_fleet_deck_overview.py --policy src/brigade/guard/policies/public-repo.json
brigade guard scan docs/superpowers/plans/2026-10-18-deck-overview-redesign-plan.md --policy src/brigade/guard/policies/in-house-repo.json
```

The plan itself uses the in-house policy because it quotes the approved agent co-author trailer in its commit commands. Expected: each reports no findings. Fixture node ids are the generic `1111...`, `2222...`, `3333...` forms.

- [ ] **Step 5: Run the full gate once, as a transient unit** (it exceeds the 10-minute foreground cap, and a plain background launch ignores SIGINT and fails the claim and cancel tests):

```bash
systemd-run --user --unit deck-overview-verify --collect --working-directory "$PWD" brigade work verify run --target . --command ./scripts/verify --capture brigade-work
systemctl --user is-active deck-overview-verify
```

Poll `systemctl --user is-active deck-overview-verify` until it prints `inactive`, then read the newest `summary.md` under `.brigade/work/verify-runs/` and require status `passed`. If the full gate fails on a test outside the files listed in this plan, record the exact failure in the PR and stop; do not edit unrelated tests.

- [x] **Step 6: Commit.**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs: note the Command Deck overview redesign in the changelog
EOF
)"
```

## Task 16: Captures

Method follows `docs/assets/ui-1495/acceptance-20261017/README.md`: the real Python renderers over synthetic data, a frozen document served by an ephemeral local server with the nonce CSP still enforced, the refresh tag removed, headless Chromium. The script starts a real fixture hub (three generic stations, telemetry fresh for `linux-a` and `win-a` and two hours old for `linux-b`, two running runs, one failed run, one interactive session), fetches `/deck` and `/deck/repos` over HTTP, freezes them, and photographs them. Chrome's `preferredColorScheme=0` renders the dark scheme and `1` the light scheme.

**Files:**
- Create: `docs/assets/deck-overview-20261018/capture_fixture.py`
- Create: `docs/assets/deck-overview-20261018/README.md`
- Create: five PNGs in `docs/assets/deck-overview-20261018/`

- [x] **Step 1: Create the capture script** `docs/assets/deck-overview-20261018/capture_fixture.py`:

```python
"""Capture the Command Deck from a local fixture hub with synthetic data.

Starts a real hub on a free port with a temporary database, seeds three
generic stations, renders ``/deck`` and ``/deck/repos`` over HTTP, freezes the
HTML (the meta refresh is removed so a capture does not reload), serves the
frozen pages with the same nonce CSP the hub sends, and photographs them with
headless Chrome in both color schemes. No live fleet data is involved.

Usage: .venv/bin/python docs/assets/deck-overview-20261018/capture_fixture.py [--out DIR]
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brigade import fleet_hub, fleet_hub_policy, fleet_hub_sessions, fleet_policy

HOST = "127.0.0.1"  # content-guard: allow loopback-ipv4
BEARER = "capture-fixture-bearer"
NODE_A = "11111111-1111-4111-8111-111111111111"
NODE_B = "22222222-2222-4222-8222-222222222222"
NODE_C = "33333333-3333-4333-8333-333333333333"
STATIONS = ((NODE_A, "linux-a", 10), (NODE_B, "linux-b", 8), (NODE_C, "win-a", 4))
# Chrome's blink setting: 0 renders the dark scheme, 1 the light scheme.
SCHEMES = {"dark": 0, "light": 1}
SHOTS = (
    ("deck", "deck-1440-dark", 1440, 1500, "dark"),
    ("deck", "deck-1440-light", 1440, 1500, "light"),
    ("deck", "deck-390-dark", 390, 2600, "dark"),
    ("deck", "deck-390-light", 390, 2600, "light"),
    ("deck-repos", "deck-repos-1440-light", 1440, 1100, "light"),
)


def request(port: int, method: str, path: str, body: object = None) -> tuple[int, str]:
    connection = http.client.HTTPConnection(HOST, port, timeout=10)
    headers = {"Authorization": f"Bearer {BEARER}", "Content-Type": "application/json"}
    connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    connection.close()
    return response.status, text


def seed(port: int, db: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for node_id, label in ((NODE_A, "linux-a"), (NODE_B, "linux-b"), (NODE_C, "win-a")):
        status, text = request(port, "POST", "/nodes", {"action": "add", "node_id": node_id, "label": label})
        assert status == 200, text
    events = [
        (NODE_A, "run-a1", "example-api", "run.started", 12),
        (NODE_A, "run-a2", "example-web", "run.started", 7),
        (NODE_B, "run-b1", "example-cli", "run.failed", 0),
    ]
    for node_id, run_id, repo, state, minutes in events:
        for sequence, stamp in ((1, now - timedelta(minutes=minutes)), (2, now)):
            event = {
                "node_id": node_id,
                "run_id": run_id,
                "repo": repo,
                "seat": "seat-a",
                "harness": "codex",
                "state": "run.created" if sequence == 1 else state,
                "ts": stamp.isoformat(),
                "sequence": sequence,
                "digest": f"d-{run_id}-{sequence}",
            }
            status, text = request(port, "POST", "/events", [event])
            assert status == 200, text
    conn = fleet_hub.open_db(db)
    try:
        document = {
            "schema": fleet_policy.POLICY_SCHEMA,
            "defaults": {},
            "machines": {
                "linux-a": {"os": "linux", "node_id": NODE_A, "concurrency": 10},
                "linux-b": {"os": "linux", "node_id": NODE_B, "concurrency": 8},
                "win-a": {"os": "windows", "node_id": NODE_C, "concurrency": 4},
            },
            "seats": {"seat-a": {"provider": "provider-a", "model": "model-a-1"}},
            "consumers": {},
            "repositories": {},
        }
        fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="operator", reason="capture fixture")
        telemetry = ((NODE_A, now, 0.44), (NODE_B, now - timedelta(hours=2), 0.12), (NODE_C, now, 0.0))
        for node_id, observed, load in telemetry:
            conn.execute(
                "INSERT INTO fleet_machine_telemetry (node_id, machine, observed_at, ttl_seconds, status, load, "
                "running, active_claims, usable_seats, credential_state, observed_id, updated_at) "
                "VALUES (?, ?, ?, 300, 'available', ?, ?, '[]', '[\"seat-a\"]', 'ok', ?, ?)",
                (
                    node_id,
                    node_id,
                    observed.isoformat(),
                    load,
                    json.dumps({"session_ids": [], "run_ids": []}),
                    f"obs-{node_id[:8]}",
                    observed.isoformat(),
                ),
            )
        conn.commit()
        status, _payload = fleet_hub_sessions.handle_session(
            conn,
            {
                "action": "upsert",
                "node_id": NODE_A,
                "harness": "claude",
                "session_id": "sess-capture",
                "repo_identity": "github.com/example/example-api",
                "identity_scope": "fleet",
                "repo_label": "example-api",
                "checkout_path": "/work/example-api",
                "branch": "main",
                "dirty_paths": ["src/app.py"],
                "dirty_truncated": False,
                "ttl_seconds": 900,
            },
            caller_node=None,
        )
        assert status == 200
    finally:
        conn.close()


def freeze(page: str) -> str:
    return re.sub(r'<meta http-equiv="refresh"[^>]*>', "", page)


def serve(pages: dict[str, str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            page = pages.get(self.path.strip("/"))
            if page is None:
                self.send_error(404)
                return
            nonce = re.search(r'<style nonce="([^"]+)"', page).group(1)  # type: ignore[union-attr]
            csp = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
                f"style-src 'nonce-{nonce}'; img-src 'self'; connect-src 'none'; base-uri 'none'"
            )
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((HOST, 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    assert chrome, "google-chrome or chromium is required"
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        db = Path(scratch) / "hub" / "fleet.db"
        config = Path(scratch) / "deck.json"
        config.write_text(
            json.dumps(
                {"stations": [{"node_id": node, "name": name, "capacity": size} for node, name, size in STATIONS]}
            )
        )
        hub = fleet_hub.make_server(HOST, 0, db, BEARER, allow_admin_writes=True, deck_config_path=config)
        threading.Thread(target=hub.serve_forever, daemon=True).start()
        try:
            port = hub.server_address[1]
            seed(port, db)
            pages = {}
            for key, path in (("deck", "/deck"), ("deck-repos", "/deck/repos")):
                status, text = request(port, "GET", path)
                assert status == 200, (path, status)
                pages[key] = freeze(text)
        finally:
            hub.shutdown()
            hub.server_close()
    frozen = serve(pages)
    try:
        port = frozen.server_address[1]
        for key, name, width, height, scheme in SHOTS:
            target = args.out / f"{name}.png"
            subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    f"--blink-settings=preferredColorScheme={SCHEMES[scheme]}",
                    f"--window-size={width},{height}",
                    f"--screenshot={target}",
                    f"http://{HOST}:{port}/{key}",  # content-guard: allow loopback-ipv4
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            print(f"wrote {target}")
    finally:
        frozen.shutdown()
        frozen.server_close()


if __name__ == "__main__":
    main()
```

- [x] **Step 2: Run it.**

```bash
brigade work verify run --target . --command ".venv/bin/python docs/assets/deck-overview-20261018/capture_fixture.py" --capture brigade-work
```

Expected stdout lists five lines `wrote .../deck-1440-dark.png`, `deck-1440-light.png`, `deck-390-dark.png`, `deck-390-light.png`, `deck-repos-1440-light.png`.

- [x] **Step 3: Look at every image with the image viewer.** Confirm: the dark captures show the dark canvas and the light captures show the light canvas; 1440 shows the sidebar beside the main column; 390 shows the sidebar collapsed to the brand and nav bar above single-column cards, with the machines table scrolling inside its own box; the alerts show a stale-telemetry card for `linux-b` and a failed card for `example-cli`; no text is clipped or overlaps. If a capture is wrong, fix the cause in the source and rerun Step 2; do not edit images.

- [x] **Step 4: Write `docs/assets/deck-overview-20261018/README.md`:**

```markdown
# Deck overview redesign captures

The captures show `/deck` at 1440 and 390 pixels wide in the dark and light color schemes, and `/deck/repos` in the light scheme at 1440.

They come from `capture_fixture.py`: a real fixture hub with synthetic data (stations `linux-a`, `linux-b`, `win-a`, generic repository names, no live fleet data or credentials), the pages fetched over HTTP, the meta refresh removed so a capture does not reload, and the frozen pages served locally with the same nonce CSP the hub sends. Headless Chrome renders them; `preferredColorScheme` selects dark (0) or light (1). Screenshots are viewport captures, not full-page images.

| Page | Width | Scheme | Image |
| --- | --- | --- | --- |
| Overview | 1440 | dark | [Image](deck-1440-dark.png) |
| Overview | 1440 | light | [Image](deck-1440-light.png) |
| Overview | 390 | dark | [Image](deck-390-dark.png) |
| Overview | 390 | light | [Image](deck-390-light.png) |
| Repos | 1440 | light | [Image](deck-repos-1440-light.png) |

The layout draws on the Runner Dashboard in `aarondfrancis/fleet`, which has no license. Only layout and behavior ideas were taken; no code, CSS, scripts, or assets were copied.
```

- [x] **Step 5: Commit.**

```bash
git add docs/assets/deck-overview-20261018
git commit -m "$(cat <<'EOF'
docs(deck): add overview redesign captures and the fixture script
EOF
)"
```
