# Fleet Ownership Map: Implementation Plan

> **For agentic workers:** execute this plan task by task, in order. Tick each `- [ ]` checkbox as you finish it. Every code block is complete: paste it exactly. Do not skip a failing-test step. The source of truth for scope is `docs/phase-fleet-ownership.md` sections 1 to 9 (slice 1).

**Goal:** Give the Fleet Hub an ownership map: declared charters in a new `owners` policy section, shown beside each owner's live holdings on `/deck/owners` and as JSON at `GET /owners`, with an Unassigned list for live work no charter matches.

**Architecture:** A new pure-validation module `fleet_policy_owners.py` adds the `owners` section to `brigade.fleet_policy.v1`, so charters inherit revisions, compare-and-swap saves, history, and rollback. A new read-only module `fleet_owners.py` loads the current charters and four live sources (claims, Grok Bot jobs, interactive sessions, delegations) and attributes each live item with a pure matcher. A new `fleet_owners_page.py` renders it, `fleet_hub_http.py` gains one JSON branch and one HTML route, and a `brigade fleet owners show` CLI prints the JSON.

**Tech:** Python 3.10+, standard library only, SQLite, pytest, ruff, mypy. No new dependency, no schema change, no new table.

**Scope:** slice 1 only (spec sections 1 to 9). Section 10 of the spec (OpenClaw presence reporting, schema v33) is a separate subsystem and needs its own plan. Charter text for real owners is never committed here: tests use generic names.

**Verification command form (pinned).** Every run in this plan uses the form below, from the worktree root. `python` resolves to an interpreter with pytest, and `tests/conftest.py` puts this checkout's `src` first on `sys.path`, so no virtualenv is needed until Task 8.

```bash
brigade work verify run --target . --command "python -m pytest tests/<file>.py -q" --capture brigade-work
```

The verify runner rejects shell metacharacters, so never add `|`, `&&`, or redirects inside `--command`. A run on an unchanged tree with the same arguments returns the cached result, so every red and green step below follows a file change.

## Pinned decisions

- **Digest stability.** `parse_document` emits the `owners` key only when at least one owner exists. Every revision saved before this change re-parses to the same canonical JSON and the same `sha256:` digest, so routing decisions, delegations, and session receipts that compare digests keep working.
- **`match` normalization.** A parsed owner always carries all seven `match` keys: six lists (empty by default) and the boolean `grokbot_jobs` (false by default). Absent `match` and an all-empty `match` are the same value, so parsing stays idempotent. A charter is "charter only" when every one of the seven keys is empty or false, `principals` included.
- **List items.** `owns`, `never`, and `intake` hold non-empty text items (at most `MAX_TEXT` characters each, at most `MAX_LIST_ITEMS` items). `match` lists hold names that satisfy `NAME_PATTERN`; wildcards are rejected by that pattern. `contact` must satisfy `IDENTITY_PATTERN`. An empty `summary` normalizes to `null`.
- **Selectors.** `nodes` matches raw node ids (not labels). A claim carries its `owner_node`; a Grok Bot job carries its `claimant_node` (a queued job with no claimant carries no node); a session carries its `node_id`; a delegation carries both `origin_node` and `target_node`. `principals` never match holdings: they only report whether a named message principal is enrolled (unrevoked).
- **Live holdings.** Claims are the unexpired rows from `fleet_hub_claims.list_claims` (it uses the hub clock). Grok Bot jobs are rows whose state is in `fleet_hub_grokbot.WORK_STATES` (`queued`, `claimed`, `running`). Sessions come from `fleet_hub_sessions.list_sessions(conn, now_epoch=...)`. A delegation is live when its reservation is not `released` and its `expires_at` is after `now_epoch`. Each source is capped at 500 rows.
- **Failure handling.** A policy read failure leaves the view with no charters, `policy_error` set to `policy read failed: <ExceptionClassName>` (never the exception text), and every live item unassigned. A live source that raises `FleetHubError` or `sqlite3.Error` is listed in `unknown_sources` and the rest still load. Node labels are cosmetic: a failed label read falls back to the first eight characters of the node id.
- **JSON route.** `GET /owners` joins the existing bearer-authenticated JSON GET block in `fleet_hub_http.py` (admin token or enrolled node token, revoked node tokens refused, browser cookies not accepted). The branch sets `PRAGMA query_only=ON` before loading. The body is `brigade.fleet_owners.v1`: `schema`, `policy_version`, `policy_error`, `owners`, `unassigned`, `overlaps`, `unknown_sources`.
- **HTML route.** `GET /deck/owners` uses `_roster_auth()` like `/deck/work` (admin bearer, dashboard cookie, or trusted Tailscale identity), is available whether or not Worklore is enabled, rejects any query string with 400, and shows at most 50 rows per table with a visible note.
- **Navigation.** The six hard-coded deck nav strings in the spec have since been unified. The sidebar of every `fleet_command_deck._document` page (overview, repos, work, roster, policy, repo policy, owners) comes from `NAV_ITEMS` in `fleet_deck_overview.py`; the two boards come from `_nav` in `fleet_dashboard.py`. Two one-line edits cover all pages, and `fleet_command_deck.py` is not touched.
- **CLI placement.** `src/brigade/cli/fleet.py` is exactly at the 2000-line ceiling and is not in the legacy allowlist, so it cannot take another line. `brigade fleet owners show` lives in a new `cli/fleet_owners.py`, registered at the end of `cli/fleet_policy.register` (charters are part of the policy document). It always prints JSON and takes no flags.
- **Out of scope.** No enforcement, no `owners set` command, no `/deck/policy` editor panel, no schema or table change, no Worklore or events attribution, no OpenClaw reporting (slice 2). Editing charters uses `brigade fleet policy save --file` with the whole document, as for every other section.

## File map

| File | Action | Responsibility |
|---|---|---|
| `src/brigade/fleet_policy_owners.py` | Create | Validate and normalize the `owners` section (`parse_owners`). |
| `src/brigade/fleet_policy.py` | Modify (3 small edits) | Allow the top-level `owners` key and call `parse_owners`. |
| `src/brigade/fleet_owners.py` | Create | `Holding`, `attribute` matcher, `load_view` over live hub rows, `view_payload` JSON. |
| `src/brigade/fleet_owners_page.py` | Create | Render the read-only `/deck/owners` page. |
| `src/brigade/fleet_hub_http.py` | Modify (about 34 lines) | `GET /owners` JSON branch, `/deck/owners` route and `_serve_owners_page`. |
| `src/brigade/fleet_deck_overview.py` | Modify (1 line) | Add `owners` to `NAV_ITEMS`. |
| `src/brigade/fleet_dashboard.py` | Modify (1 line) | Add `owners` to the boards' `_nav`. |
| `src/brigade/fleet_client_policy.py` | Modify (5 lines) | `show_owners()` client call for `GET /owners`. |
| `src/brigade/cli/fleet_owners.py` | Create | `brigade fleet owners show`. |
| `src/brigade/cli/fleet_policy.py` | Modify (5 lines) | Register the owners group. |
| `docs/command-inventory.md` | Regenerate | Generated list of CLI paths. |
| `CHANGELOG.md` | Modify | One Unreleased entry. |
| `tests/test_fleet_policy_owners.py` | Create | Policy section: parse, bounds, digest stability, save and rollback, form edits. |
| `tests/test_fleet_owners.py` | Create | Matcher on plain data. |
| `tests/test_fleet_owners_view.py` | Create | `load_view` and `view_payload` against a real hub database. |
| `tests/test_fleet_owners_page.py` | Create | HTTP: `/owners` JSON, `/deck/owners` page, rendering, nav links. |
| `tests/test_fleet_owners_cli.py` | Create | `brigade fleet owners show` and `show_owners()`. |

Size ratchet headroom (`tests/test_module_size_ratchet.py`, ceiling 2000): `fleet_hub_http.py` grows from 1838 to about 1872, `fleet_policy.py` from 1211 to 1218. `cli/fleet.py` stays at 2000 and is never edited.

## Task 1: The `owners` section of the policy document

**Files:**
- Create: `src/brigade/fleet_policy_owners.py`
- Modify: `src/brigade/fleet_policy.py` (import near line 32, `TOP_LEVEL_FIELDS` near line 62, `parse_document` near line 827)
- Test: `tests/test_fleet_policy_owners.py`

- [x] Write the failing test

**Create** `tests/test_fleet_policy_owners.py`:

```python
"""The ``owners`` section of the fleet policy document (charters)."""

from __future__ import annotations

import copy

import pytest

from brigade import fleet_hub, fleet_hub_policy, fleet_policy, fleet_policy_form
from brigade.fleet_policy import FleetPolicyError


def _owners() -> dict:
    return {
        "grokbot": {
            "label": "Grok Bot",
            "summary": "Builds approved issues.",
            "owns": ["Issues with the approval label"],
            "never": ["Deploys", "Releases"],
            "intake": ["Approval label"],
            "contact": "agent:grokbot",
            "match": {"grokbot_jobs": True, "harnesses": ["grokbot"]},
        },
        "miller": {
            "label": "Miller",
            "match": {"session_harnesses": ["dot"], "principals": ["miller"]},
        },
        "openclaw": {"label": "OpenClaw gateway"},
    }


def _document(owners: object | None = None) -> dict:
    document = fleet_policy.empty_document()
    if owners is not None:
        document["owners"] = owners
    return document


def test_full_record_parses_and_fills_defaults():
    parsed = fleet_policy.parse_document(_document(_owners()))
    grokbot = parsed["owners"]["grokbot"]
    assert grokbot["label"] == "Grok Bot"
    assert grokbot["never"] == ["Deploys", "Releases"]
    assert grokbot["contact"] == "agent:grokbot"
    assert grokbot["match"] == {
        "nodes": [],
        "harnesses": ["grokbot"],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": True,
    }
    openclaw = parsed["owners"]["openclaw"]
    assert openclaw["summary"] is None and openclaw["contact"] is None
    assert openclaw["owns"] == [] and openclaw["never"] == [] and openclaw["intake"] == []
    assert openclaw["match"]["grokbot_jobs"] is False
    assert list(parsed["owners"]) == ["grokbot", "miller", "openclaw"]


def test_parse_is_idempotent():
    once = fleet_policy.parse_document(_document(_owners()))
    assert fleet_policy.parse_document(once) == once


def test_missing_or_empty_section_is_omitted_so_old_digests_hold():
    base = fleet_policy.parse_document(fleet_policy.empty_document())
    assert "owners" not in base
    assert fleet_policy.parse_document(_document({})) == base
    assert fleet_policy.document_digest(_document({})) == fleet_policy.document_digest(base)
    assert "owners" not in fleet_policy.canonical_json(base)


@pytest.mark.parametrize(
    "owners, message",
    [
        ({"a": {"label": "A", "colour": "red"}}, "unknown owner 'a' field(s): colour"),
        ({"a": {"label": "A", "match": {"regex": ["x"]}}}, "unknown owner 'a' match field(s): regex"),
        ({"a": {}}, "owner 'a' field 'label' is required"),
        ({"a": {"label": ""}}, "owner 'a' field 'label' must not be empty"),
        ({"a": {"label": "x" * 257}}, "must be at most 256 characters"),
        ({"a": {"label": "A", "owns": "everything"}}, "owner 'a' field 'owns' must be a list"),
        ({"a": {"label": "A", "owns": [""]}}, "owner 'a' field 'owns'[0] must not be empty"),
        ({"a": {"label": "A", "owns": [7]}}, "owner 'a' field 'owns'[0] must be a string"),
        ({"a": {"label": "A", "never": ["x"] * 65}}, "must hold at most 64 items"),
        ({"a": {"label": "A", "contact": "has space"}}, "owner 'a' field 'contact' must match"),
        ({"a": {"label": "A", "match": {"nodes": ["*"]}}}, "owner 'a' match field 'nodes'[0] must match"),
        ({"a": {"label": "A", "match": {"nodes": "node-a"}}}, "owner 'a' match field 'nodes' must be a list"),
        ({"a": {"label": "A", "match": {"grokbot_jobs": "yes"}}}, "field 'grokbot_jobs' must be a boolean"),
        ({"a": {"label": "A", "match": ["nodes"]}}, "owner 'a' match must be a JSON object"),
        ({"bad name": {"label": "A"}}, "owner name must match"),
        ({"a": "text"}, "owner 'a' must be a JSON object"),
    ],
)
def test_invalid_owners_are_rejected_with_a_named_error(owners, message):
    with pytest.raises(FleetPolicyError) as raised:
        fleet_policy.parse_document(_document(owners))
    assert message in str(raised.value)


def test_section_must_be_an_object_and_bounded():
    with pytest.raises(FleetPolicyError, match="owners must be a JSON object"):
        fleet_policy.parse_document(_document(["grokbot"]))
    too_many = {f"owner-{index}": {"label": "x"} for index in range(fleet_policy.MAX_RECORDS + 1)}
    with pytest.raises(FleetPolicyError, match="'owners' must hold at most 256 records"):
        fleet_policy.parse_document(_document(too_many))


def test_owners_round_trip_through_save_and_rollback(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        saved = fleet_hub_policy.save_policy(
            conn, _document(_owners()), expected_version=1, actor="admin", reason="seed owners"
        )
        assert saved["revision"] == 2
        stored = fleet_hub_policy.current_policy(conn)["document"]
        assert stored["owners"]["miller"]["match"]["principals"] == ["miller"]
        cleared = fleet_hub_policy.save_policy(
            conn, fleet_policy.empty_document(), expected_version=2, actor="admin", reason="clear owners"
        )
        assert "owners" not in cleared["document"]
        restored = fleet_hub_policy.rollback_policy(
            conn, to_revision=2, expected_version=3, actor="admin", reason="restore owners"
        )
        assert restored["document"]["owners"] == stored["owners"]
        assert restored["digest"] == saved["digest"]
    finally:
        conn.close()


def test_a_scoped_policy_form_edit_keeps_the_owners_section():
    current = fleet_policy.parse_document(_document(_owners()))
    submission = fleet_policy_form.parse_form(
        b"scope=defaults&action=preview&expected_version=1&field.data_allow_free=true"
    )
    edited = fleet_policy_form.build_document(copy.deepcopy(current), submission)
    assert edited["owners"] == current["owners"]
    assert edited["defaults"]["data"]["allow_free"] is True
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_policy_owners.py -q" --capture brigade-work` - expect FAIL (document rejects the new key); 22 failed, every failure reads `FleetPolicyError: unknown document field(s): owners`. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Implement the validator module

**Create** `src/brigade/fleet_policy_owners.py`:

```python
"""Charter section of the fleet policy document (``owners``).

Each owner is an agent or surface in the fleet (a bot, a bridge, a gateway)
with a declared charter: what it owns, what it must never touch, how work
reaches it, and the selectors that attribute live hub rows to it. The section
rides the versioned policy document, so it inherits revisions, compare-and-swap
saves, history, and rollback. Nothing here enforces a charter.

This module imports ``fleet_policy`` lazily, inside the parse functions,
because ``fleet_policy`` imports this module to validate the section.
"""

from __future__ import annotations

from typing import Any

OWNER_FIELDS = ("label", "summary", "owns", "never", "intake", "contact", "match")
MATCH_LIST_KEYS = ("nodes", "harnesses", "conductors", "session_harnesses", "adapters", "principals")
MATCH_KEYS = (*MATCH_LIST_KEYS, "grokbot_jobs")


def parse_owners(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate and normalize the ``owners`` mapping. ``None`` means no charters."""
    from . import fleet_policy as policy

    records = policy._records({} if raw is None else raw, "owners")
    return {
        policy._required_name(name, "owner name"): _parse_owner(str(name), records[name]) for name in sorted(records)
    }


def _parse_owner(name: str, raw: Any) -> dict[str, Any]:
    from . import fleet_policy as policy

    where = f"owner '{name}'"
    body = policy._mapping(raw, where)
    policy._reject_unknown(body, OWNER_FIELDS, where)
    label = policy._text(body.get("label"), f"{where} field 'label'", optional=False)
    if not label:
        raise policy.FleetPolicyError(f"fleet policy {where} field 'label' must not be empty")
    contact = body.get("contact")
    return {
        "label": label,
        "summary": policy._text(body.get("summary"), f"{where} field 'summary'") or None,
        "owns": _text_list(body.get("owns"), f"{where} field 'owns'"),
        "never": _text_list(body.get("never"), f"{where} field 'never'"),
        "intake": _text_list(body.get("intake"), f"{where} field 'intake'"),
        "contact": None if contact is None else policy._identity(contact, f"{where} field 'contact'"),
        "match": _parse_match(body.get("match"), f"{where} match"),
    }


def _text_list(raw: Any, where: str) -> list[str]:
    from . import fleet_policy as policy

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise policy.FleetPolicyError(f"fleet policy {where} must be a list")
    if len(raw) > policy.MAX_LIST_ITEMS:
        raise policy.FleetPolicyError(f"fleet policy {where} must hold at most {policy.MAX_LIST_ITEMS} items")
    items: list[str] = []
    for index, item in enumerate(raw):
        text = policy._text(item, f"{where}[{index}]", optional=False)
        if not text:
            raise policy.FleetPolicyError(f"fleet policy {where}[{index}] must not be empty")
        items.append(text)
    return items


def _parse_match(raw: Any, where: str) -> dict[str, Any]:
    from . import fleet_policy as policy

    body = {} if raw is None else policy._mapping(raw, where)
    policy._reject_unknown(body, MATCH_KEYS, where)
    parsed: dict[str, Any] = {
        key: policy._name_list(body.get(key), f"{where} field '{key}'") for key in MATCH_LIST_KEYS
    }
    parsed["grokbot_jobs"] = policy._bool(body.get("grokbot_jobs"), f"{where} field 'grokbot_jobs'", default=False)
    return parsed
```

`fleet_policy` imports this module, so this module imports `fleet_policy` lazily inside its functions to avoid a cycle.

- [x] Wire it into `parse_document`: three edits to `src/brigade/fleet_policy.py`

**Edit** `src/brigade/fleet_policy.py`, replace this:

```python
from .fleet_model_inventory import DEFAULT_ALLOWED_SOURCES
```

with this:

```python
from . import fleet_policy_owners
from .fleet_model_inventory import DEFAULT_ALLOWED_SOURCES
```

**Edit** `src/brigade/fleet_policy.py`, replace this:

```python
TOP_LEVEL_FIELDS = frozenset({"schema", "defaults", "machines", "seats", "consumers", "repositories", "routing"})
```

with this:

```python
TOP_LEVEL_FIELDS = frozenset(
    {"schema", "defaults", "machines", "seats", "consumers", "repositories", "routing", "owners"}
)
```

**Edit** `src/brigade/fleet_policy.py`, replace this:

```python
        "routing": parse_routing(body.get("routing")),
    }
    rendered = canonical_json(document)
```

with this:

```python
        "routing": parse_routing(body.get("routing")),
    }
    # Omitted when empty so every pre-owners revision keeps its stored digest.
    owners = fleet_policy_owners.parse_owners(body.get("owners"))
    if owners:
        document["owners"] = owners
    rendered = canonical_json(document)
```

- [x] Run to green: same command - expect PASS, 22 passed.

- [x] Run the neighbouring policy suites to prove nothing else moved: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_policy.py tests/test_fleet_hub_policy.py tests/test_fleet_policy_cli.py tests/test_fleet_policy_page.py tests/test_fleet_roster_page.py tests/test_fleet_policy_core_completion.py -q" --capture brigade-work` - expect PASS, 242 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_policy_owners.py src/brigade/fleet_policy.py tests/test_fleet_policy_owners.py
git commit -m "feat(fleet): add the owners charter section to the policy document" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 2: The attribution matcher

**Files:**
- Create: `src/brigade/fleet_owners.py` (matcher half; Task 3 completes it)
- Test: `tests/test_fleet_owners.py`

- [x] Write the failing test

**Create** `tests/test_fleet_owners.py`:

```python
"""Ownership matcher: pure attribution of live items to charters."""

from __future__ import annotations

from brigade.fleet_owners import Holding, attribute


def _match(**selectors):
    record = {
        "nodes": [],
        "harnesses": [],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": False,
    }
    record.update(selectors)
    return record


def _item(kind="claim", ref="item-1", **fields):
    return Holding(
        kind=kind,
        ref=ref,
        repo="example/repo",
        node_label="node-a",
        detail="",
        since="2026-01-01T00:00:00+00:00",
        **fields,
    )


def test_single_selector_attributes_an_item_to_one_owner():
    charters = {
        "alpha": {"match": _match(harnesses=["claude"])},
        "beta": {"match": _match(harnesses=["codex"])},
    }
    item = _item(harness="claude")
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"alpha": [item], "beta": []}
    assert unassigned == [] and overlaps == []


def test_selectors_are_ored_within_a_record():
    charters = {"alpha": {"match": _match(nodes=["node-a"], adapters=["t3-fleet"])}}
    by_node = _item(ref="by-node", nodes=("node-a",))
    by_adapter = _item(kind="delegation", ref="by-adapter", nodes=("node-z",), adapter="t3-fleet")
    neither = _item(ref="neither", nodes=("node-z",))
    by_owner, unassigned, _ = attribute(charters, [by_node, by_adapter, neither])
    assert by_owner["alpha"] == [by_node, by_adapter]
    assert unassigned == [neither]


def test_each_selector_key_matches_its_own_field():
    charters = {
        "conductors": {"match": _match(conductors=["orch"])},
        "sessions": {"match": _match(session_harnesses=["dot"])},
    }
    claim = _item(ref="c", conductor="orch")
    session = _item(kind="session", ref="s", session_harness="dot")
    other = _item(ref="o", harness="dot")
    by_owner, unassigned, _ = attribute(charters, [claim, session, other])
    assert by_owner == {"conductors": [claim], "sessions": [session]}
    assert unassigned == [other]


def test_grokbot_jobs_flag_matches_only_job_items():
    charters = {"grokbot": {"match": _match(grokbot_jobs=True)}}
    job = _item(kind="grokbot-job", ref="job-1")
    claim = _item(kind="claim", ref="claim-1")
    by_owner, unassigned, _ = attribute(charters, [job, claim])
    assert by_owner["grokbot"] == [job]
    assert unassigned == [claim]


def test_a_node_selector_matches_either_end_of_a_delegation():
    charters = {"origin": {"match": _match(nodes=["node-a"])}, "target": {"match": _match(nodes=["node-b"])}}
    delegation = _item(kind="delegation", ref="del-1", nodes=("node-a", "node-b"))
    by_owner, _, overlaps = attribute(charters, [delegation])
    assert by_owner == {"origin": [delegation], "target": [delegation]}
    assert [(overlap.item.ref, overlap.owners) for overlap in overlaps] == [("del-1", ("origin", "target"))]


def test_overlap_lists_the_item_under_both_owners_once_in_overlaps():
    charters = {
        "beta": {"match": _match(harnesses=["claude"])},
        "alpha": {"match": _match(nodes=["node-a"])},
    }
    item = _item(harness="claude", nodes=("node-a",))
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"beta": [item], "alpha": [item]}
    assert unassigned == []
    assert len(overlaps) == 1 and overlaps[0].owners == ("alpha", "beta")


def test_unmatched_items_are_unassigned_and_empty_fields_never_match():
    charters = {"alpha": {"match": _match(harnesses=["claude"])}}
    blank = _item(ref="blank")
    other = _item(ref="other", harness="codex")
    by_owner, unassigned, overlaps = attribute(charters, [blank, other])
    assert by_owner == {"alpha": []}
    assert unassigned == [blank, other]
    assert overlaps == []


def test_a_charter_without_selectors_stays_listed_and_matches_nothing():
    charters = {"quiet": {"label": "Quiet"}, "empty": {"match": _match()}}
    item = _item(harness="claude", nodes=("node-a",))
    by_owner, unassigned, overlaps = attribute(charters, [item])
    assert by_owner == {"quiet": [], "empty": []}
    assert unassigned == [item] and overlaps == []


def test_public_shape_omits_selector_fields():
    item = _item(harness="claude", nodes=("node-a",), conductor="orch")
    assert item.public() == {
        "kind": "claim",
        "ref": "item-1",
        "repo": "example/repo",
        "node_label": "node-a",
        "detail": "",
        "since": "2026-01-01T00:00:00+00:00",
    }
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners.py -q" --capture brigade-work` - expect FAIL (module missing); 1 error at collection, `ModuleNotFoundError: No module named 'brigade.fleet_owners'`. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Implement the dataclasses and the matcher

**Create** `src/brigade/fleet_owners.py`:

```python
"""Hub ownership map: policy charters joined to what each owner holds right now.

Read-only. ``attribute`` is a pure matcher over plain dataclasses, so it is
tested without a database. Nothing here enforces a charter: an item no charter
matches lands in ``unassigned``, and an item two charters match is listed under
both and reported in ``overlaps``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA = "brigade.fleet_owners.v1"
SOURCE_LIMIT = 500


@dataclass(frozen=True)
class Holding:
    """One live item, normalized. Selector fields are not part of the public shape."""

    kind: str
    ref: str
    repo: str
    node_label: str
    detail: str
    since: str
    nodes: tuple[str, ...] = ()
    harness: str = ""
    conductor: str = ""
    session_harness: str = ""
    adapter: str = ""

    def public(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "repo": self.repo,
            "node_label": self.node_label,
            "detail": self.detail,
            "since": self.since,
        }


@dataclass(frozen=True)
class Overlap:
    item: Holding
    owners: tuple[str, ...]


def _matches(match: Mapping[str, Any], item: Holding) -> bool:
    if match.get("grokbot_jobs") and item.kind == "grokbot-job":
        return True
    if set(match.get("nodes") or ()).intersection(item.nodes):
        return True
    for key, value in (
        ("harnesses", item.harness),
        ("conductors", item.conductor),
        ("session_harnesses", item.session_harness),
        ("adapters", item.adapter),
    ):
        if value and value in (match.get(key) or ()):
            return True
    return False


def attribute(
    charters: Mapping[str, Mapping[str, Any]], items: Sequence[Holding]
) -> tuple[dict[str, list[Holding]], list[Holding], list[Overlap]]:
    """Split *items* into per-owner lists, an unassigned list, and overlaps.

    An item matches an owner when any one selector in that owner's ``match``
    record matches (OR). A charter with no selectors matches nothing. An item
    two charters match is listed under both and once in the overlaps.
    """
    by_owner: dict[str, list[Holding]] = {name: [] for name in charters}
    unassigned: list[Holding] = []
    overlaps: list[Overlap] = []
    for item in items:
        owners = [name for name in sorted(charters) if _matches(charters[name].get("match") or {}, item)]
        if not owners:
            unassigned.append(item)
            continue
        for name in owners:
            by_owner[name].append(item)
        if len(owners) > 1:
            overlaps.append(Overlap(item=item, owners=tuple(owners)))
    return by_owner, unassigned, overlaps
```

- [x] Run to green: same command - expect PASS, 9 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_owners.py tests/test_fleet_owners.py
git commit -m "feat(fleet): attribute live items to owner charters" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 3: Live holdings and the JSON payload

**Files:**
- Modify: `src/brigade/fleet_owners.py` (full replacement below; the matcher section is the Task 2 code, unchanged)
- Test: `tests/test_fleet_owners_view.py`

- [x] Write the failing test

**Create** `tests/test_fleet_owners_view.py`:

```python
"""Ownership view: charters from policy joined to live hub rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_claims, fleet_hub_messages, fleet_hub_policy, fleet_owners, fleet_policy

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NODE_A = "node-a-0001"
NODE_B = "node-b-0002"


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        yield connection
    finally:
        connection.close()


def _save_owners(conn, owners):
    document = fleet_policy.empty_document()
    document["owners"] = owners
    revision = fleet_hub_policy.current_policy(conn)["revision"]
    fleet_hub_policy.save_policy(conn, document, expected_version=revision, actor="admin", reason="seed owners")


def _claim(conn, target="example/repo", node=NODE_A, **labels):
    request = {"action": "acquire", "target": target, "node_id": node, "holder": "holder-1", "conductor": "orch"}
    status, _ = fleet_hub.handle_claim(conn, {**request, **labels})
    assert status == 200


def _job(conn, job_id, state, claimant=NODE_A):
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, sequence, "
        "created_at, updated_at, queued_at, timeout_seconds, artifact_kind, owner_node, claimant_node"
        ") VALUES (?, 'implementation-worker', 'example/repo', 'Job', ?, ?, ?, 1, 1, ?, ?, ?, 900, 'draft-pr', "
        "'queue-owner', ?)",
        (job_id, "d" * 64, job_id.ljust(64, "f"), state, stamp, stamp, stamp, claimant),
    )


def _session(conn, session_id, harness="claude", node=NODE_A, expires=None):
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO interactive_sessions ("
        "node_id, harness, session_id, repo_identity, identity_scope, repo_label, checkout_path, branch, "
        "dirty_paths_json, dirty_truncated, state, started_at, heartbeat_at, ended_at, ttl_seconds, expires_at"
        ") VALUES (?, ?, ?, 'example/repo', 'fleet', 'repo', '/work/repo', 'main', '[]', 0, 'active', ?, ?, NULL, "
        "900, ?)",
        (node, harness, session_id, stamp, stamp, expires or NOW.timestamp() + 900),
    )


def _delegation(conn, delegation_id, *, released=False, expires=None, origin=NODE_A, target=NODE_B):
    stamp = NOW.isoformat()
    expires_at = (expires or NOW + timedelta(minutes=5)).isoformat()
    conn.execute(
        "INSERT INTO fleet_reservations (reservation_id, decision_id, session_id, machine, seat, node_id, "
        "origin_node, state, accepted, expires_at, created_at, updated_at) "
        "VALUES (?, ?, 'sess', 'machine-1', 'seat-1', ?, ?, ?, 1, ?, ?, ?)",
        (
            f"res-{delegation_id}",
            f"dec-{delegation_id}",
            target,
            origin,
            "released" if released else "active",
            expires_at,
            stamp,
            stamp,
        ),
    )
    conn.execute(
        "INSERT INTO fleet_policy_delegations (delegation_id, parent_request_id, origin_node, decision_id, "
        "reservation_id, session_id, consumer, adapter, repo_identity, seat, workload, policy_version, "
        "policy_digest, target_machine, target_node, source_revision, parent_metadata, created_at, expires_at, "
        "document) VALUES (?, ?, ?, ?, ?, 'sess', 'brigade-run', 't3-fleet', 'example/repo', 'seat-1', 'build', 1, "
        "'sha256:x', 'machine-1', ?, ?, NULL, ?, ?, '{}')",
        (
            delegation_id,
            f"parent-{delegation_id}",
            origin,
            f"dec-{delegation_id}",
            f"res-{delegation_id}",
            target,
            "a" * 40,
            stamp,
            expires_at,
        ),
    )


def _refs(items):
    return sorted((item.kind, item.ref) for item in items)


def test_without_charters_every_live_item_is_unassigned(conn):
    _claim(conn, harness="claude")
    _job(conn, "grokbot-1", "queued")
    _session(conn, "sess-1")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_version == 1 and view.policy_error is None
    assert view.charters == {} and view.unknown_sources == ()
    assert _refs(view.unassigned) == [("claim", "example/repo"), ("grokbot-job", "grokbot-1"), ("session", "sess-1")]


def test_live_rows_from_all_four_sources_reach_their_owners(conn):
    fleet_hub.add_node(conn, NODE_A, "Node A")
    _save_owners(
        conn,
        {
            "grokbot": {"label": "Grok Bot", "match": {"grokbot_jobs": True}},
            "builders": {"label": "Builders", "match": {"harnesses": ["claude"]}},
            "dot": {"label": "Dot", "match": {"session_harnesses": ["dot"]}},
            "t3": {"label": "T3", "match": {"adapters": ["t3-fleet"]}},
        },
    )
    _claim(conn, harness="claude", role="impl")
    _job(conn, "grokbot-1", "running")
    _session(conn, "sess-dot", harness="dot")
    _session(conn, "sess-other", harness="codex")
    _delegation(conn, "del-1")
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.by_owner["grokbot"]) == [("grokbot-job", "grokbot-1")]
    assert _refs(view.by_owner["builders"]) == [("claim", "example/repo")]
    assert _refs(view.by_owner["dot"]) == [("session", "sess-dot")]
    assert _refs(view.by_owner["t3"]) == [("delegation", "del-1")]
    assert _refs(view.unassigned) == [("session", "sess-other")]
    claim = view.by_owner["builders"][0]
    assert claim.node_label == "Node A" and claim.detail == "impl claude"
    job = view.by_owner["grokbot"][0]
    assert job.node_label == "Node A" and job.detail == "implementation-worker running"
    assert view.by_owner["t3"][0].node_label == NODE_B[:8]
    assert view.by_owner["t3"][0].detail == "seat-1 build"


def test_finished_and_expired_rows_are_not_holdings(conn):
    _claim(conn, target="expired/repo")
    conn.execute("UPDATE claims SET expires_at = 0 WHERE target = 'expired/repo'")
    _job(conn, "grokbot-done", "completed")
    _job(conn, "grokbot-queued", "queued", claimant=None)
    _session(conn, "sess-old", expires=NOW.timestamp() - 1)
    _delegation(conn, "del-released", released=True)
    _delegation(conn, "del-expired", expires=NOW - timedelta(seconds=1))
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert _refs(view.unassigned) == [("grokbot-job", "grokbot-queued")]
    assert view.unassigned[0].nodes == ()


def test_enrolled_principals_exclude_revoked_ones(conn):
    fleet_hub_messages.add_principal(conn, {"name": "miller"})
    fleet_hub_messages.add_principal(conn, {"name": "retired"})
    conn.execute("UPDATE fleet_message_principals SET revoked_at = ? WHERE name = 'retired'", (NOW.isoformat(),))
    conn.commit()
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.enrolled_principals == frozenset({"miller"})


def test_policy_read_failure_keeps_live_items_and_names_the_failure(conn, monkeypatch):
    _claim(conn)

    def broken(_conn):
        raise fleet_hub.FleetHubError("revision metadata is missing")

    monkeypatch.setattr(fleet_hub_policy, "current_policy", broken)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.policy_version is None
    assert view.policy_error == "policy read failed: FleetHubError"
    assert view.charters == {}
    assert _refs(view.unassigned) == [("claim", "example/repo")]


def test_a_failing_source_is_marked_unknown_and_the_others_still_load(conn, monkeypatch):
    _job(conn, "grokbot-1", "queued")

    def broken(_conn):
        raise sqlite3.OperationalError("no such table: claims")

    monkeypatch.setattr(fleet_hub_claims, "list_claims", broken)
    view = fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert view.unknown_sources == ("claims",)
    assert _refs(view.unassigned) == [("grokbot-job", "grokbot-1")]


def test_loading_the_view_writes_nothing(conn):
    _save_owners(conn, {"alpha": {"label": "Alpha", "match": {"harnesses": ["claude"]}}})
    _claim(conn, harness="claude")
    _job(conn, "grokbot-1", "queued")
    _session(conn, "sess-1")
    conn.commit()
    before = list(conn.iterdump())
    conn.execute("PRAGMA query_only=ON")
    fleet_owners.load_view(conn, now_epoch=NOW.timestamp())
    assert not conn.in_transaction
    assert list(conn.iterdump()) == before


def test_payload_is_the_v1_json_shape(conn):
    fleet_hub_messages.add_principal(conn, {"name": "miller"})
    _save_owners(
        conn,
        {
            "zeta": {"label": "Zeta", "summary": "Quiet.", "owns": ["Notes"], "never": ["Deploys"]},
            "alpha": {
                "label": "Alpha",
                "contact": "agent:alpha",
                "match": {"harnesses": ["claude"], "principals": ["miller", "ghost"]},
            },
            "beta": {"label": "Beta", "match": {"nodes": [NODE_A]}},
        },
    )
    _claim(conn, harness="claude")
    payload = fleet_owners.view_payload(fleet_owners.load_view(conn, now_epoch=NOW.timestamp()))
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema"] == "brigade.fleet_owners.v1"
    assert payload["policy_version"] == 2 and payload["policy_error"] is None
    assert [owner["name"] for owner in payload["owners"]] == ["alpha", "beta", "zeta"]
    alpha, beta, zeta = payload["owners"]
    assert alpha["contact"] == "agent:alpha" and alpha["charter_only"] is False
    assert alpha["principals"] == [{"name": "miller", "enrolled": True}, {"name": "ghost", "enrolled": False}]
    assert zeta["charter_only"] is True and zeta["summary"] == "Quiet." and zeta["never"] == ["Deploys"]
    assert alpha["holdings"][0]["ref"] == beta["holdings"][0]["ref"] == "example/repo"
    assert payload["unassigned"] == []
    assert payload["overlaps"] == [
        {**alpha["holdings"][0], "owners": ["alpha", "beta"]},
    ]
    assert payload["unknown_sources"] == []
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners_view.py -q" --capture brigade-work` - expect FAIL (`load_view` and `view_payload` are not defined); 8 failed, each `AttributeError: module 'brigade.fleet_owners' has no attribute 'load_view'` (or `view_payload`). The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Replace `src/brigade/fleet_owners.py` with the complete module

**Create** `src/brigade/fleet_owners.py`:

```python
"""Hub ownership map: policy charters joined to what each owner holds right now.

Read-only. ``load_view`` reads the current policy revision's ``owners`` section
and four live sources the hub already keeps (repo claims, Grok Bot jobs,
interactive sessions, policy delegations). ``attribute`` is a pure matcher over
plain dataclasses, so it is tested without a database. Nothing here enforces a
charter: an item no charter matches lands in ``unassigned``, and an item two
charters match is listed under both and reported in ``overlaps``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import fleet_hub_claims, fleet_hub_grokbot, fleet_hub_policy, fleet_hub_sessions, fleet_policy_owners
from .fleet_hub import FleetHubError

SCHEMA = "brigade.fleet_owners.v1"
SOURCE_LIMIT = 500


@dataclass(frozen=True)
class Holding:
    """One live item, normalized. Selector fields are not part of the public shape."""

    kind: str
    ref: str
    repo: str
    node_label: str
    detail: str
    since: str
    nodes: tuple[str, ...] = ()
    harness: str = ""
    conductor: str = ""
    session_harness: str = ""
    adapter: str = ""

    def public(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "repo": self.repo,
            "node_label": self.node_label,
            "detail": self.detail,
            "since": self.since,
        }


@dataclass(frozen=True)
class Overlap:
    item: Holding
    owners: tuple[str, ...]


@dataclass(frozen=True)
class OwnersView:
    policy_version: int | None
    policy_error: str | None
    charters: Mapping[str, Mapping[str, Any]]
    by_owner: Mapping[str, Sequence[Holding]]
    unassigned: Sequence[Holding]
    overlaps: Sequence[Overlap]
    enrolled_principals: frozenset[str]
    unknown_sources: tuple[str, ...]


def _matches(match: Mapping[str, Any], item: Holding) -> bool:
    if match.get("grokbot_jobs") and item.kind == "grokbot-job":
        return True
    if set(match.get("nodes") or ()).intersection(item.nodes):
        return True
    for key, value in (
        ("harnesses", item.harness),
        ("conductors", item.conductor),
        ("session_harnesses", item.session_harness),
        ("adapters", item.adapter),
    ):
        if value and value in (match.get(key) or ()):
            return True
    return False


def attribute(
    charters: Mapping[str, Mapping[str, Any]], items: Sequence[Holding]
) -> tuple[dict[str, list[Holding]], list[Holding], list[Overlap]]:
    """Split *items* into per-owner lists, an unassigned list, and overlaps.

    An item matches an owner when any one selector in that owner's ``match``
    record matches (OR). A charter with no selectors matches nothing. An item
    two charters match is listed under both and once in the overlaps.
    """
    by_owner: dict[str, list[Holding]] = {name: [] for name in charters}
    unassigned: list[Holding] = []
    overlaps: list[Overlap] = []
    for item in items:
        owners = [name for name in sorted(charters) if _matches(charters[name].get("match") or {}, item)]
        if not owners:
            unassigned.append(item)
            continue
        for name in owners:
            by_owner[name].append(item)
        if len(owners) > 1:
            overlaps.append(Overlap(item=item, owners=tuple(owners)))
    return by_owner, unassigned, overlaps


def charter_only(match: Mapping[str, Any]) -> bool:
    """True when a charter carries no selector of any kind (principals included)."""
    return not any(match.get(key) for key in fleet_policy_owners.MATCH_KEYS)


def _label(labels: Mapping[str, str], node_id: str | None) -> str:
    if not node_id:
        return ""
    return labels.get(node_id) or node_id[:8]


def _node_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """Cosmetic only: a failed read falls back to short node ids."""
    try:
        rows = conn.execute("SELECT node_id, label FROM nodes WHERE label IS NOT NULL AND label != ''").fetchall()
    except sqlite3.Error:
        return {}
    return {str(node_id): str(label) for node_id, label in rows}


def _claims(conn: sqlite3.Connection, labels: Mapping[str, str]) -> list[Holding]:
    return [
        Holding(
            kind="claim",
            ref=claim["target"],
            repo=claim["target"],
            node_label=_label(labels, claim["owner_node"]),
            detail=" ".join(part for part in (claim.get("role"), claim.get("harness")) if part) or "claim",
            since=claim["acquired_at"],
            nodes=(claim["owner_node"],),
            harness=claim.get("harness") or "",
            conductor=claim.get("owner_conductor") or "",
        )
        for claim in fleet_hub_claims.list_claims(conn)
        if not claim["expired"]
    ][:SOURCE_LIMIT]


def _grokbot_jobs(conn: sqlite3.Connection, labels: Mapping[str, str]) -> list[Holding]:
    states = sorted(fleet_hub_grokbot.WORK_STATES)
    rows = conn.execute(
        "SELECT job_id, repository, role, state, claimant_node, queued_at, claimed_at FROM grokbot_jobs "
        f"WHERE state IN ({', '.join('?' for _ in states)}) ORDER BY updated_at DESC, job_id LIMIT ?",
        (*states, SOURCE_LIMIT),
    ).fetchall()
    return [
        Holding(
            kind="grokbot-job",
            ref=job_id,
            repo=repository,
            node_label=_label(labels, claimant),
            detail=f"{role} {state}",
            since=claimed_at or queued_at,
            nodes=(claimant,) if claimant else (),
        )
        for job_id, repository, role, state, claimant, queued_at, claimed_at in rows
    ]


def _sessions(conn: sqlite3.Connection, labels: Mapping[str, str], now_epoch: float) -> list[Holding]:
    return [
        Holding(
            kind="session",
            ref=str(session["session_id"]),
            repo=str(session.get("repo_identity") or session.get("repo_label") or ""),
            node_label=_label(labels, str(session["node_id"])),
            detail=" ".join(part for part in (str(session["harness"]), str(session.get("branch") or "")) if part),
            since=str(session["started_at"]),
            nodes=(str(session["node_id"]),),
            session_harness=str(session["harness"]),
        )
        for session in fleet_hub_sessions.list_sessions(conn, now_epoch=now_epoch)
    ][:SOURCE_LIMIT]


def _parse_instant(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _delegations(conn: sqlite3.Connection, labels: Mapping[str, str], now_epoch: float) -> list[Holding]:
    """Delegations whose reservation is unreleased and whose expiry is in the future."""
    rows = conn.execute(
        "SELECT d.delegation_id, d.origin_node, d.target_node, d.adapter, d.repo_identity, d.seat, d.workload, "
        "d.created_at, d.expires_at FROM fleet_policy_delegations d "
        "JOIN fleet_reservations r ON r.reservation_id = d.reservation_id "
        "WHERE r.state != 'released' ORDER BY d.created_at DESC, d.delegation_id LIMIT ?",
        (SOURCE_LIMIT,),
    ).fetchall()
    holdings = []
    for delegation_id, origin, target, adapter, repo, seat, workload, created_at, expires_at in rows:
        expiry = _parse_instant(str(expires_at))
        if expiry is None or expiry.timestamp() <= now_epoch:
            continue
        holdings.append(
            Holding(
                kind="delegation",
                ref=delegation_id,
                repo=repo,
                node_label=_label(labels, target),
                detail=f"{seat} {workload}".strip(),
                since=created_at,
                nodes=(origin, target),
                adapter=adapter,
            )
        )
    return holdings


def _enrolled_principals(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM fleet_message_principals WHERE revoked_at IS NULL").fetchall()
    return frozenset(str(name) for (name,) in rows)


def load_view(conn: sqlite3.Connection, *, now_epoch: float) -> OwnersView:
    """Read charters and live holdings. A failing source degrades, it never raises."""
    policy_version: int | None = None
    policy_error: str | None = None
    charters: Mapping[str, Mapping[str, Any]] = {}
    try:
        current = fleet_hub_policy.current_policy(conn)
        policy_version = int(current["revision"])
        charters = current["document"].get("owners") or {}
    except (FleetHubError, sqlite3.Error) as exc:
        policy_error = f"policy read failed: {type(exc).__name__}"
    labels = _node_labels(conn)
    unknown: list[str] = []
    items: list[Holding] = []
    loaders: tuple[tuple[str, Callable[[], list[Holding]]], ...] = (
        ("claims", lambda: _claims(conn, labels)),
        ("grokbot-jobs", lambda: _grokbot_jobs(conn, labels)),
        ("sessions", lambda: _sessions(conn, labels, now_epoch)),
        ("delegations", lambda: _delegations(conn, labels, now_epoch)),
    )
    for name, loader in loaders:
        try:
            items.extend(loader())
        except (FleetHubError, sqlite3.Error):
            unknown.append(name)
    enrolled: frozenset[str] = frozenset()
    try:
        enrolled = _enrolled_principals(conn)
    except sqlite3.Error:
        unknown.append("principals")
    by_owner, unassigned, overlaps = attribute(charters, items)
    return OwnersView(
        policy_version=policy_version,
        policy_error=policy_error,
        charters=charters,
        by_owner=by_owner,
        unassigned=unassigned,
        overlaps=overlaps,
        enrolled_principals=enrolled,
        unknown_sources=tuple(unknown),
    )


def view_payload(view: OwnersView) -> dict[str, Any]:
    """The ``brigade.fleet_owners.v1`` JSON body served by ``GET /owners``."""
    owners = []
    for name in sorted(view.charters):
        charter = view.charters[name]
        match = charter.get("match") or {}
        owners.append(
            {
                "name": name,
                "label": charter.get("label"),
                "summary": charter.get("summary"),
                "owns": list(charter.get("owns") or []),
                "never": list(charter.get("never") or []),
                "intake": list(charter.get("intake") or []),
                "contact": charter.get("contact"),
                "match": dict(match),
                "charter_only": charter_only(match),
                "principals": [
                    {"name": principal, "enrolled": principal in view.enrolled_principals}
                    for principal in match.get("principals") or []
                ],
                "holdings": [item.public() for item in view.by_owner.get(name, ())],
            }
        )
    return {
        "schema": SCHEMA,
        "policy_version": view.policy_version,
        "policy_error": view.policy_error,
        "owners": owners,
        "unassigned": [item.public() for item in view.unassigned],
        "overlaps": [{**overlap.item.public(), "owners": list(overlap.owners)} for overlap in view.overlaps],
        "unknown_sources": list(view.unknown_sources),
    }
```

- [x] Run to green: same command - expect PASS, 8 passed.

- [x] Run the suites that share this module, so the replaced matcher is checked against the Task 2 tests: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners.py -q" --capture brigade-work` - expect PASS, 9 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_owners.py tests/test_fleet_owners_view.py
git commit -m "feat(fleet): load live holdings for the ownership view" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 4: `GET /owners` JSON route

**Files:**
- Modify: `src/brigade/fleet_hub_http.py` (import block near line 41, the bearer GET path tuple near line 1457, the `elif` chain near line 1520)
- Test: `tests/test_fleet_owners_page.py`

- [x] Write the failing test (HTTP helpers and the three JSON tests; Task 5 and Task 6 append to this file)

**Create** `tests/test_fleet_owners_page.py`:

```python
"""Hub HTTP contracts for the ownership map: ``GET /owners`` and ``/deck/owners``."""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager

from brigade import fleet_hub, fleet_hub_policy, fleet_policy

TOKEN = "test-admin-token-owners"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4


@contextmanager
def _hub(tmp_path, **kwargs):
    db = tmp_path / "fleet.db"
    server = fleet_hub.make_server(LOOPBACK, 0, db, TOKEN, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (LOOPBACK, server.server_address[1]), db
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(hub, path, *, headers=None, method="GET", body=None):
    conn = http.client.HTTPConnection(*hub, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def _bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _cookie(hub):
    status, _, body = _request(
        hub,
        "/dashboard/enrollment",
        method="POST",
        headers={**_bearer(), "Content-Type": "application/json"},
        body=b"{}",
    )
    assert status == 201
    code = json.loads(body)["code"]
    status, headers, _ = _request(
        hub,
        "/enroll",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Sec-Fetch-Site": "same-origin"},
        body=f"code={code}".encode(),
    )
    assert status == 303
    return {"Cookie": headers["Set-Cookie"].split(";")[0]}


def _seed_owners(db, owners):
    conn = fleet_hub.open_db(db)
    try:
        document = fleet_policy.empty_document()
        document["owners"] = owners
        revision = fleet_hub_policy.current_policy(conn)["revision"]
        fleet_hub_policy.save_policy(conn, document, expected_version=revision, actor="admin", reason="seed owners")
    finally:
        conn.close()


def _node_token(db, node_id="node-a-0001"):
    conn = fleet_hub.open_db(db)
    try:
        return fleet_hub.add_node(conn, node_id, "Node A")[1]
    finally:
        conn.close()


def test_owners_json_requires_a_bearer_token(tmp_path):
    with _hub(tmp_path) as (hub, _db):
        status, _, body = _request(hub, "/owners")
        assert status == 401 and json.loads(body) == {"error": "unauthorized"}
        assert _request(hub, "/owners", headers=_bearer("wrong-token"))[0] == 401
        assert _request(hub, "/owners", headers=_cookie(hub))[0] == 401


def test_owners_json_serves_the_v1_shape_to_admin_and_node_tokens(tmp_path):
    with _hub(tmp_path) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "owns": ["Notes"], "match": {"harnesses": ["claude"]}}})
        for headers in (_bearer(), _bearer(_node_token(db))):
            status, response_headers, body = _request(hub, "/owners", headers=headers)
            assert status == 200
            assert response_headers["Content-Type"].startswith("application/json")
            payload = json.loads(body)
            assert payload["schema"] == "brigade.fleet_owners.v1"
            assert payload["policy_version"] == 2
            assert [owner["name"] for owner in payload["owners"]] == ["alpha"]
            assert payload["owners"][0]["owns"] == ["Notes"]
            assert payload["unassigned"] == [] and payload["overlaps"] == []


def test_owners_json_lists_live_claims_and_reads_without_writing(tmp_path):
    with _hub(tmp_path, allow_admin_writes=True) as (hub, db):
        _seed_owners(db, {"alpha": {"label": "Alpha", "match": {"harnesses": ["claude"]}}})
        claim = {
            "action": "acquire",
            "target": "example/repo",
            "node_id": "node-a-0001",
            "holder": "holder-1",
            "harness": "claude",
        }
        status, _, text = _request(
            hub,
            "/claims",
            method="POST",
            headers={**_bearer(), "Content-Type": "application/json"},
            body=json.dumps(claim),
        )
        assert status == 200, text
        conn = fleet_hub.open_db(db)
        try:
            before = list(conn.iterdump())
        finally:
            conn.close()
        status, _, body = _request(hub, "/owners", headers=_bearer())
        assert status == 200
        holdings = json.loads(body)["owners"][0]["holdings"]
        assert [(item["kind"], item["ref"]) for item in holdings] == [("claim", "example/repo")]
        conn = fleet_hub.open_db(db)
        try:
            assert list(conn.iterdump()) == before
        finally:
            conn.close()
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners_page.py -q" --capture brigade-work` - expect FAIL (route answers 404); 3 failed, the route answers 404 where 401 or 200 is expected (`assert 404 == 200`). The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Add the route: three edits to `src/brigade/fleet_hub_http.py`

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
from . import fleet_model_inventory
from . import fleet_policy_page
```

with this:

```python
from . import fleet_model_inventory
from . import fleet_owners
from . import fleet_policy_page
```

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
                "/preference",
                "/sessions",
                "/policy",
```

with this:

```python
                "/preference",
                "/sessions",
                "/owners",
                "/policy",
```

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
                    elif path == "/sessions":
                        payload = {"sessions": list_sessions(conn, include_all=include_all)}
```

with this:

```python
                    elif path == "/sessions":
                        payload = {"sessions": list_sessions(conn, include_all=include_all)}
                    elif path == "/owners":
                        conn.execute("PRAGMA query_only=ON")
                        payload = fleet_owners.view_payload(fleet_owners.load_view(conn, now_epoch=_hub._now_epoch()))
```

- [x] Run to green: same command - expect PASS, 3 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_hub_http.py tests/test_fleet_owners_page.py
git commit -m "feat(fleet): serve the ownership map as JSON at GET /owners" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 5: The `/deck/owners` page

**Files:**
- Create: `src/brigade/fleet_owners_page.py`
- Modify: `src/brigade/fleet_hub_http.py` (import, new `_serve_owners_page` method before `_serve_work`, route after the `/deck/work` route in `do_GET`)
- Test: `tests/test_fleet_owners_page.py`

- [x] Write the failing tests: first widen the imports at the top of the test file

**Edit** `tests/test_fleet_owners_page.py`, replace this:

```python
from contextlib import contextmanager

from brigade import fleet_hub, fleet_hub_policy, fleet_policy
```

with this:

```python
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser

from brigade import fleet_hub, fleet_hub_policy, fleet_owners_page, fleet_policy
from brigade.fleet_owners import Holding, Overlap, OwnersView
```

Then append the page tests:

**Append** to `tests/test_fleet_owners_page.py` (after two blank lines):

```python
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ATTACK = '<img src=x onerror="alert(1)">'


def _match(**selectors):
    record = {
        "nodes": [],
        "harnesses": [],
        "conductors": [],
        "session_harnesses": [],
        "adapters": [],
        "principals": [],
        "grokbot_jobs": False,
    }
    record.update(selectors)
    return record


def _holding(ref="example/repo", kind="claim", **fields):
    return Holding(
        kind=kind,
        ref=ref,
        repo="example/repo",
        node_label="Node A",
        detail="impl claude",
        since="2026-01-01T00:00:00+00:00",
        **fields,
    )


def _view(**fields):
    base = {
        "policy_version": 3,
        "policy_error": None,
        "charters": {},
        "by_owner": {},
        "unassigned": [],
        "overlaps": [],
        "enrolled_principals": frozenset(),
        "unknown_sources": (),
    }
    base.update(fields)
    return OwnersView(**base)


def _render(view):
    return fleet_owners_page.render(view, nonce="test-nonce", now=NOW)


class _Tags(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.tags = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)


def test_page_requires_dashboard_access_and_is_read_only(tmp_path):
    with _hub(tmp_path, trust_tailscale_identity=False) as (hub, db):
        status, _, body = _request(hub, "/deck/owners")
        assert status == 401 and "brigade fleet enroll" in body
        assert _request(hub, "/deck/owners", headers=_bearer(_node_token(db)))[0] == 401
        for headers in (_bearer(), _cookie(hub)):
            status, response_headers, body = _request(hub, "/deck/owners", headers=headers)
            assert status == 200
            assert "Content-Security-Policy" in response_headers
            assert response_headers["Cache-Control"] == "no-store"
            assert "No charters declared" in body
            assert not {"form", "input", "button"} & set(_Tags(body).tags)
        assert _request(hub, "/deck/owners?cursor=1", headers=_bearer())[0] == 400


def test_page_shows_charters_beside_live_holdings_and_unassigned_items(tmp_path):
    with _hub(tmp_path, allow_admin_writes=True) as (hub, db):
        _seed_owners(
            db,
            {
                "alpha": {
                    "label": "Alpha Agent",
                    "summary": "Builds things.",
                    "owns": ["Approved issues"],
                    "never": ["Deploys"],
                    "intake": ["Approval label"],
                    "contact": "agent:alpha",
                    "match": {"harnesses": ["claude"]},
                },
                "quiet": {"label": "Quiet Gateway"},
            },
        )
        for target, harness in (("example/held", "claude"), ("example/loose", "codex")):
            claim = {
                "action": "acquire",
                "target": target,
                "node_id": "node-a-0001",
                "holder": "h1",
                "harness": harness,
            }
            status, _, text = _request(
                hub,
                "/claims",
                method="POST",
                headers={**_bearer(), "Content-Type": "application/json"},
                body=json.dumps(claim),
            )
            assert status == 200, text
        status, _, body = _request(hub, "/deck/owners", headers=_bearer())
        assert status == 200
        for text in ("Alpha Agent", "Builds things.", "Approved issues", "Deploys", "Approval label", "agent:alpha"):
            assert text in body
        assert "Quiet Gateway" in body and "Charter only, no live attribution." in body
        alpha_card, _, rest = body.partition("Quiet Gateway")
        assert "example/held" in alpha_card and "example/loose" not in alpha_card
        unassigned = rest.partition("<h2>Unassigned</h2>")[2]
        assert "example/loose" in unassigned and "example/held" not in unassigned
        assert "No overlaps." in body


def test_page_lists_overlaps_and_principal_state():
    item = _holding()
    view = _view(
        charters={
            "alpha": {"label": "Alpha", "match": _match(harnesses=["claude"], principals=["miller", "ghost"])},
            "beta": {"label": "Beta", "match": _match(nodes=["node-a"])},
        },
        by_owner={"alpha": [item], "beta": [item]},
        overlaps=[Overlap(item=item, owners=("alpha", "beta"))],
        enrolled_principals=frozenset({"miller"}),
    )
    body = _render(view)
    assert "claim example/repo: alpha, beta" in body
    assert "miller: enrolled" in body and "ghost: not enrolled" in body
    assert "revision 3" in body


def test_policy_failure_banner_still_lists_live_items_as_unassigned():
    view = _view(policy_version=None, policy_error="policy read failed: FleetHubError", unassigned=[_holding()])
    body = _render(view)
    assert "Policy unavailable" in body and "policy read failed: FleetHubError" in body
    assert "revision unknown" in body
    assert "No charters declared" not in body
    assert "Unassigned" in body and "example/repo" in body


def test_unknown_source_is_called_out_instead_of_shown_as_empty():
    view = _view(
        charters={"alpha": {"label": "Alpha", "match": _match(harnesses=["claude"])}},
        by_owner={"alpha": []},
        unknown_sources=("claims", "sessions"),
    )
    body = _render(view)
    assert "Holdings unknown: could not read claims, sessions." in body
    assert "Could not read: claims, sessions." in body
    assert "Nothing held right now." not in body


def test_stored_text_is_escaped_everywhere():
    evil = _holding(ref=ATTACK)
    view = _view(
        charters={
            "alpha": {
                "label": ATTACK,
                "summary": ATTACK,
                "owns": [ATTACK],
                "never": [ATTACK],
                "intake": [ATTACK],
                "contact": ATTACK,
                "match": _match(harnesses=["claude"], principals=[ATTACK]),
            }
        },
        by_owner={"alpha": [evil]},
        unassigned=[evil],
        overlaps=[Overlap(item=evil, owners=("alpha", ATTACK))],
    )
    body = _render(view)
    assert ATTACK not in body
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in body


def test_long_holding_lists_are_capped_with_a_note():
    items = [_holding(ref=f"example/repo-{index:03}") for index in range(fleet_owners_page.DISPLAY_LIMIT + 1)]
    view = _view(unassigned=items)
    body = _render(view)
    assert f"Showing {fleet_owners_page.DISPLAY_LIMIT} of {len(items)} items." in body
    assert "example/repo-049" in body and "example/repo-050" not in body
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners_page.py -q" --capture brigade-work` - expect FAIL (`fleet_owners_page` does not exist yet); 1 error at collection, `ImportError: cannot import name 'fleet_owners_page' from 'brigade'`. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Implement the page module

**Create** `src/brigade/fleet_owners_page.py`:

```python
"""Read-only Command Deck page for the ownership map (``/deck/owners``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import fleet_command_deck as deck
from .fleet_owners import Holding, Overlap, OwnersView, charter_only

DISPLAY_LIMIT = 50


def _items(values: Sequence[str]) -> str:
    if not values:
        return '<span class="station-meta">none declared</span>'
    return '<ul class="observer-list">' + "".join(f"<li>{deck._esc(value)}</li>" for value in values) + "</ul>"


def _table(items: Sequence[Holding], *, empty: str) -> str:
    if not items:
        return f'<p class="empty">{deck._esc(empty)}</p>'
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{deck._esc(value)}</td>"
            for value in (item.kind, item.ref, item.repo, item.node_label, item.detail, item.since)
        )
        + "</tr>"
        for item in items[:DISPLAY_LIMIT]
    )
    note = (
        f'<p class="station-meta">Showing {DISPLAY_LIMIT} of {len(items)} items.</p>'
        if len(items) > DISPLAY_LIMIT
        else ""
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>Kind</th><th>Item</th><th>Repository</th><th>Node</th>'
        f"<th>Detail</th><th>Since</th></tr></thead><tbody>{rows}</tbody></table></div>{note}"
    )


def _principals(match: Mapping[str, Any], enrolled: frozenset[str]) -> str:
    names = match.get("principals") or []
    if not names:
        return ""
    entries = "".join(
        f"<li>{deck._esc(name)}: {'enrolled' if name in enrolled else 'not enrolled'}</li>" for name in names
    )
    return f'<dt>Message principals</dt><dd><ul class="observer-list">{entries}</ul></dd>'


def _holdings(view: OwnersView, name: str, match: Mapping[str, Any]) -> str:
    if charter_only(match):
        return '<p class="station-meta">Charter only, no live attribution.</p>'
    if view.unknown_sources:
        sources = ", ".join(view.unknown_sources)
        return f'<p class="station-meta">Holdings unknown: could not read {deck._esc(sources)}.</p>' + _table(
            view.by_owner.get(name, ()), empty="No holdings from the sources that could be read."
        )
    return _table(view.by_owner.get(name, ()), empty="Nothing held right now.")


def _card(view: OwnersView, name: str) -> str:
    charter = view.charters[name]
    match = charter.get("match") or {}
    summary = charter.get("summary")
    contact = charter.get("contact")
    return (
        '<article class="panel owner-card"><h2>'
        + deck._esc(charter.get("label") or name)
        + f' <span class="station-meta">{deck._esc(name)}</span></h2>'
        + (f"<p>{deck._esc(summary)}</p>" if summary else "")
        + '<dl class="tile-facts">'
        + f"<dt>Owns</dt><dd>{_items(charter.get('owns') or [])}</dd>"
        + f"<dt>Never</dt><dd>{_items(charter.get('never') or [])}</dd>"
        + f"<dt>Intake</dt><dd>{_items(charter.get('intake') or [])}</dd>"
        + f"<dt>Contact</dt><dd>{deck._esc(contact) if contact else '(none declared)'}</dd>"
        + _principals(match, view.enrolled_principals)
        + "</dl><h3>Holds now</h3>"
        + _holdings(view, name, match)
        + "</article>"
    )


def _overlaps(overlaps: Sequence[Overlap]) -> str:
    if not overlaps:
        return '<p class="empty">No overlaps.</p>'
    entries = "".join(
        f"<li>{deck._esc(overlap.item.kind)} {deck._esc(overlap.item.ref)}: "
        + deck._esc(", ".join(overlap.owners))
        + "</li>"
        for overlap in overlaps[:DISPLAY_LIMIT]
    )
    return f'<ul class="observer-list">{entries}</ul>'


def render(view: OwnersView, *, nonce: str, now: datetime) -> str:
    """Charters beside live holdings, then Unassigned and Overlaps. No forms, no actions."""
    banners = ""
    if view.policy_error:
        banners += (
            '<section class="panel"><h2>Policy unavailable</h2><p>'
            + deck._esc(view.policy_error)
            + ". Charters are not shown; every live item is listed as unassigned.</p></section>"
        )
    revision = "unknown" if view.policy_version is None else str(view.policy_version)
    cards = "".join(_card(view, name) for name in sorted(view.charters))
    if not cards and not view.policy_error:
        cards = (
            '<section class="panel"><p class="empty">No charters declared. Save an owners section in the fleet '
            "policy document with <code>brigade fleet policy save</code> to populate this page.</p></section>"
        )
    unassigned_note = (
        f'<p class="station-meta">Could not read: {deck._esc(", ".join(view.unknown_sources))}.</p>'
        if view.unknown_sources
        else ""
    )
    body = (
        '<main class="deck-shell"><header class="masthead"><div><p class="eyebrow">Fleet operations</p>'
        "<h1>Command Deck &middot; Owners</h1></div>"
        f'<p class="header-meta">{deck._esc(deck._stamp(now))}</p></header>'
        + banners
        + '<section class="panel"><h2>Charters</h2><p>Read-only. Each charter is declared in the fleet policy '
        f"document (revision {deck._esc(revision)}) and describes responsibility. It does not block claims, "
        "dispatch, or merges.</p></section>"
        + cards
        + '<section class="panel"><h2>Unassigned</h2><p>Live items that no charter matches.</p>'
        + unassigned_note
        + _table(view.unassigned, empty="Nothing unassigned.")
        + "</section>"
        + '<section class="panel"><h2>Overlaps</h2><p>Items that more than one charter matches. '
        "Edit a charter to resolve one.</p>" + _overlaps(view.overlaps) + "</section></main>"
    )
    return deck._document(body, nonce=nonce, now=now, title="Owners", refresh=False, active="owners")
```

- [x] Add the route: three edits to `src/brigade/fleet_hub_http.py`

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
from . import fleet_owners
from . import fleet_policy_page
```

with this:

```python
from . import fleet_owners
from . import fleet_owners_page
from . import fleet_policy_page
```

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
        def _serve_work(self, query: str) -> None:
```

with this:

```python
        def _serve_owners_page(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            authorized, _editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=plain
                )
                return
            if query:
                self._send_html(400, "Invalid owners page query.\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
                try:
                    conn.execute("PRAGMA query_only=ON")
                    view = fleet_owners.load_view(conn, now_epoch=_hub._now_epoch())
                finally:
                    conn.close()
            except (FleetHubError, sqlite3.Error):
                self._send_html(500, "Ownership view unavailable.\n", content_type=plain)
                return
            nonce = secrets.token_urlsafe(16)
            page = fleet_owners_page.render(view, nonce=nonce, now=datetime.now(timezone.utc))
            self._send_html(200, page, nonce=nonce)

        def _serve_work(self, query: str) -> None:
```

**Edit** `src/brigade/fleet_hub_http.py`, replace this:

```python
            if path == "/deck/work":
                self._serve_work(query)
                return
```

with this:

```python
            if path == "/deck/work":
                self._serve_work(query)
                return
            if path == "/deck/owners":
                self._serve_owners_page(query)
                return
```

- [x] Run to green: same command - expect PASS, 10 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_owners_page.py src/brigade/fleet_hub_http.py tests/test_fleet_owners_page.py
git commit -m "feat(fleet): add the read-only /deck/owners page" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 6: Navigation links

**Files:**
- Modify: `src/brigade/fleet_deck_overview.py:609-616` (`NAV_ITEMS`)
- Modify: `src/brigade/fleet_dashboard.py:436-445` (`_nav`)
- Test: `tests/test_fleet_owners_page.py`

- [x] Write the failing test

**Append** to `tests/test_fleet_owners_page.py` (after two blank lines):

```python
def test_owners_link_is_in_the_navigation_of_every_deck_page_and_board(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_WORKLORE_ENABLED", "1")
    with _hub(tmp_path) as (hub, _db):
        for path in (
            "/deck",
            "/deck/repos",
            "/deck/roster",
            "/deck/policy",
            "/deck/work",
            "/deck/owners",
            "/view/machines",
            "/view/repos",
        ):
            status, _, body = _request(hub, path, headers=_bearer())
            assert status == 200, path
            assert 'href="/deck/owners"' in body, path
        _, _, body = _request(hub, "/deck/owners", headers=_bearer())
        assert '<a href="/deck/owners" aria-current="page">owners</a>' in body
        assert '<a href="/deck" aria-current="page">' not in body
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners_page.py -q" --capture brigade-work` - expect FAIL (the `/deck` page has no owners link); 1 failed, 10 passed; the failure is `AssertionError: /deck` on the missing `href="/deck/owners"`. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Add the link to both nav definitions

**Edit** `src/brigade/fleet_deck_overview.py`, replace this:

```python
    ("policy", "/deck/policy", "policy"),
```

with this:

```python
    ("policy", "/deck/policy", "policy"),
    ("owners", "/deck/owners", "owners"),
```

**Edit** `src/brigade/fleet_dashboard.py`, replace this:

```python
        ("/deck/policy", "policy"),
```

with this:

```python
        ("/deck/policy", "policy"),
        ("/deck/owners", "owners"),
```

- [x] Run to green: same command - expect PASS, 11 passed.

- [x] Run the suites that assert on deck navigation: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_deck_overview.py tests/test_fleet_dashboard.py tests/test_fleet_command_deck.py tests/test_fleet_work_page.py tests/test_fleet_repo_policy_page.py -q" --capture brigade-work` - expect PASS, 305 passed.

- [x] Commit:

```bash
git add src/brigade/fleet_deck_overview.py src/brigade/fleet_dashboard.py tests/test_fleet_owners_page.py
git commit -m "feat(fleet): link the owners page from deck navigation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 7: `brigade fleet owners show`

**Files:**
- Create: `src/brigade/cli/fleet_owners.py`
- Modify: `src/brigade/fleet_client_policy.py` (insert `show_owners` before `history_policy`, near line 156)
- Modify: `src/brigade/cli/fleet_policy.py` (end of `register`, after the `p_ack.set_defaults` line near line 175)
- Regenerate: `docs/command-inventory.md`
- Test: `tests/test_fleet_owners_cli.py`

- [x] Write the failing test

**Create** `tests/test_fleet_owners_cli.py`:

```python
"""CLI transport for `brigade fleet owners`."""

from __future__ import annotations

import json
from io import StringIO

from brigade import cli, fleet_client_policy
from brigade.fleet_client_policy import FleetPolicyClientError


def _run(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    try:
        rc = cli.main(["fleet", "owners", *argv])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def test_help_lists_show(monkeypatch):
    rc, out, err = _run(monkeypatch, ["--help"])
    assert rc == 0
    assert "show" in out + err


def test_show_prints_the_hub_payload_as_json(monkeypatch):
    payload = {"schema": "brigade.fleet_owners.v1", "policy_version": 4, "owners": [], "unassigned": [], "overlaps": []}
    monkeypatch.setattr(fleet_client_policy, "show_owners", lambda: payload)
    rc, out, err = _run(monkeypatch, ["show"])
    assert rc == 0 and err == ""
    assert json.loads(out) == payload


def test_show_reports_a_structured_error_without_a_token(monkeypatch):
    def refused():
        raise FleetPolicyClientError("auth-failed", "hub returned HTTP 401")

    monkeypatch.setattr(fleet_client_policy, "show_owners", refused)
    rc, out, err = _run(monkeypatch, ["show"])
    assert rc == 1
    body = json.loads(out or err)
    assert body["error"]["code"] == "auth-failed"
    assert "token" not in json.dumps(body).lower()


def test_show_owners_reads_get_owners_with_the_node_token(monkeypatch):
    seen = {}

    def fake_request(method, path, *, token, hub_url, **kwargs):
        seen.update(method=method, path=path, token=token, hub_url=hub_url)
        return 200, {"schema": "brigade.fleet_owners.v1"}

    monkeypatch.setattr(fleet_client_policy, "_hub_and_token", lambda *, admin: ("https://hub.example", "node-secret"))
    monkeypatch.setattr(fleet_client_policy, "_request", fake_request)
    assert fleet_client_policy.show_owners() == {"schema": "brigade.fleet_owners.v1"}
    assert seen == {"method": "GET", "path": "/owners", "token": "node-secret", "hub_url": "https://hub.example"}


def test_an_older_hub_without_the_route_maps_to_policy_unsupported(monkeypatch):
    monkeypatch.setattr(fleet_client_policy, "_hub_and_token", lambda *, admin: ("https://hub.example", "node-secret"))
    monkeypatch.setattr(fleet_client_policy, "_request", lambda *args, **kwargs: (404, {"error": "not found"}))
    try:
        fleet_client_policy.show_owners()
    except FleetPolicyClientError as exc:
        assert exc.code == "policy-unsupported"
    else:
        raise AssertionError("expected FleetPolicyClientError")
```

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_fleet_owners_cli.py -q" --capture brigade-work` - expect FAIL (`fleet owners` is not a command and `show_owners` is missing); 5 failed: `--help` exits 2 because `owners` is not a command, and the other four raise `AttributeError` for the missing `show_owners`. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Add the client call

**Edit** `src/brigade/fleet_client_policy.py`, replace this:

```python
def history_policy(*, limit: int = 50) -> dict[str, Any]:
```

with this:

```python
def show_owners() -> dict[str, Any]:
    hub, token = _hub_and_token(admin=False)
    status, payload = _request("GET", "/owners", token=token, hub_url=hub)
    return _checked(status, payload)


def history_policy(*, limit: int = 50) -> dict[str, Any]:
```

- [x] Add the command group

**Create** `src/brigade/cli/fleet_owners.py`:

```python
"""brigade fleet owners command group (hub ownership map)."""

from __future__ import annotations

import argparse

from .. import fleet_client_policy
from .fleet_policy import _emit, _fail


def register(fleet_sub: argparse._SubParsersAction) -> None:
    p_owners = fleet_sub.add_parser("owners", help="Read the fleet ownership map: charters and what each owner holds.")
    owners_sub = p_owners.add_subparsers(dest="owners_command", metavar="<owners-command>")
    owners_sub.required = True

    p_show = owners_sub.add_parser("show", help="Print the hub ownership map as JSON (brigade.fleet_owners.v1).")
    p_show.set_defaults(func=_dispatch_show)


def _dispatch_show(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.show_owners()
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)
```

- [x] Register it from the policy group (not from `cli/fleet.py`, which is at the size ceiling)

**Edit** `src/brigade/cli/fleet_policy.py`, replace this:

```python
    p_ack.set_defaults(func=_dispatch_acknowledge)
```

with this:

```python
    p_ack.set_defaults(func=_dispatch_acknowledge)

    # Charters live in the policy document, so the owners group registers with it.
    # cli/fleet.py sits at the module size ceiling and cannot take another import.
    from .fleet_owners import register as register_owners

    register_owners(fleet_sub)
```

- [x] Run to green: same command - expect PASS, 5 passed.

- [x] Run it, watch it fail: `brigade work verify run --target . --command "python -m pytest tests/test_cli_inventory_contract.py tests/test_roadmap_inventory_cmd.py -q" --capture brigade-work` - expect FAIL (the generated inventory does not list the new path); 1 failed, 11 passed; the failure is `test_parser_inventory_and_missing_cli_agree_after_alias_normalization`, with `brigade fleet owners show` listed as an extra parser path. The pytest text is in `command-1-stdout.log` inside the receipt directory named on the `receipt:` line.

- [x] Regenerate the inventory (extras must be on for the generator; `XDG_CONFIG_HOME` points at an empty directory so no local config leaks in): `mkdir -p /tmp/brigade-empty-xdg && BRIGADE_EXTRAS=1 XDG_CONFIG_HOME=/tmp/brigade-empty-xdg PYTHONPATH=src python -m brigade roadmap commands --write`. The only diff in `docs/command-inventory.md` must be the `brigade fleet` count going from 72 to 73 command paths and one new line, `` - `brigade fleet owners show` ``. Check with `git diff --stat docs/command-inventory.md` (expect 2 insertions, 1 deletion, so 3 lines changed) before continuing.

- [x] Run to green: same command - expect PASS, 12 passed.

- [x] Commit:

```bash
git add src/brigade/cli/fleet_owners.py src/brigade/cli/fleet_policy.py src/brigade/fleet_client_policy.py docs/command-inventory.md tests/test_fleet_owners_cli.py
git commit -m "feat(fleet): add brigade fleet owners show" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

## Task 8: Changelog and the focused gate

**Files:**
- Modify: `CHANGELOG.md` (the `### Added` list under `## [Unreleased]`)

- [x] Add the changelog entry

**Edit** `CHANGELOG.md`, replace this:

```markdown
## [Unreleased]

### Added
```

with this:

```markdown
## [Unreleased]

### Added

- A fleet ownership map. The policy document gains an optional `owners` section of charters that say what each agent or surface owns, must never touch, and how work reaches it. The Command Deck page `/deck/owners` shows each charter beside its live claims, Grok Bot jobs, sessions, and delegations, with an Unassigned list for work no charter matches. `GET /owners` and `brigade fleet owners show` return the same view as JSON. Charters describe responsibility and do not block any claim, dispatch, or merge.
```

- [x] Set up the virtualenv the focused gate needs (skip if `.venv/` already exists; it is ignored by git): `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`. `scripts/verify_env.py` refuses an interpreter that imports Brigade from another checkout, which is why the borrowed repository virtualenv cannot be used here.

- [x] Run the focused gate over every test file this plan added or relies on (ruff lint, ruff format check, version sync, managed snapshot, template-profile snapshot, mypy, then the named tests):


```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_fleet_policy_owners.py","tests/test_fleet_owners.py","tests/test_fleet_owners_view.py","tests/test_fleet_owners_page.py","tests/test_fleet_owners_cli.py","tests/test_fleet_policy.py","tests/test_fleet_hub_policy.py","tests/test_fleet_policy_cli.py","tests/test_fleet_policy_page.py","tests/test_fleet_roster_page.py","tests/test_fleet_repo_policy_page.py","tests/test_fleet_deck_overview.py","tests/test_fleet_dashboard.py","tests/test_fleet_command_deck.py","tests/test_fleet_work_page.py","tests/test_fleet_enrollment.py","tests/test_cli_inventory_contract.py","tests/test_roadmap_inventory_cmd.py","tests/test_module_size_ratchet.py"]' --capture brigade-work
```

Expect `status: completed` and exit 0. If ruff format reports a file, run `.venv/bin/ruff format <file>` and re-run. A failure in a test this plan did not touch is a real regression from a plan edit: fix the cause, never the test.

- [x] Scan the changed markdown for leaks (one path per invocation): `brigade guard scan CHANGELOG.md --policy src/brigade/guard/policies/public-repo.json` - expect exit 0 with `blocked=false` and exactly three WARN findings that already exist in the file (two loopback addresses and one port reference, none on the new bullet).

- [x] Commit:

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): record the fleet ownership map" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"  # content-guard: allow pii/email
```

Do not run the full coverage gate here. Per `AGENTS.md`, `./scripts/verify` runs once at pull-request time through `brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work`; status 75 is never retried with a larger timeout.

## Spec coverage

| Spec requirement | Task |
|---|---|
| 4: `owners` section, field table, closed `match` key set, unknown keys rejected, bounded sizes | 1 |
| 4: missing section parses as empty; old documents parse unchanged; round trip through save and rollback | 1 |
| 5: `attribute`, pure, single match, OR across selectors, overlaps under both owners, unassigned, charter without `match` | 2 |
| 5: `load_view` over claims, Grok Bot jobs, sessions, delegations; node labels; policy-failure and source-failure degradation | 3 |
| 5: normalized holding shape (`kind`, `ref`, `repo`, `node_label`, `detail`, `since`) | 2, 3 |
| 6: `GET /owners` JSON, schema `brigade.fleet_owners.v1`, node or admin token | 4 |
| 6: `/deck/owners` read-only page, cards, Unassigned, Overlaps, charter-only note, auth like `/deck/work` | 5 |
| 6: nav link on every deck page | 6 |
| 6: `brigade fleet owners show` | 7 |
| 7: T3 attributed by `adapters` and node; OpenClaw renders as a charter with no holdings | 3, 5 (matcher and charter-only rendering; no new reporting) |
| 8: testing list, size ratchet | 1 to 7, 8 |
| 9: rollout (deploy hub first, then save the first revision) | Operational, after merge; not code |

Rollout order after merge (spec section 9): deploy the new hub first, then save the first `owners` revision with `brigade fleet policy save --file <document> --expected-version <N> --reason "seed owners"`. A hub older than this change rejects any document containing `owners`.
