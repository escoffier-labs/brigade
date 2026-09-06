"""Loopback fixture server for eyeballing the Fleet policy, roster, and deck pages.

Development aid only. It builds a throwaway hub database in a temp directory
from obviously fake seats, machines, repositories, and sessions, then serves it
on 127.0.0.1 with a fixture token printed to stdout. No production data, no
operator credential, no live provider probe: the provider inventory is a static
injected snapshot so the page's ``available`` / ``retired`` / ``policy-blocked``
/ ``missing`` / ``unavailable`` states can all be seen without a login.

    python tests/fixtures/fleet_policy_ui_server.py --port PORT

Then open the printed URL. ``--inventory unavailable`` shows the honest
degraded path where no snapshot could be read.

Only the policy document is seeded, so the roster page's legacy seat table is
empty here. That is the fixture's scope, not a defect: the legacy roster tables
have their own coverage in ``tests/test_fleet_roster_page.py``.

The Command Deck reads ``fleet_hub_routing.control_plane_status`` on the
request connection. This fixture seeds no telemetry, so machine slots and
quota stay unknown rather than idle or unlimited.

``/deck/repos`` shows the repository policy projection joined to claims and
runs. The fixture seeds one claim on a linked repository and one live run on an
unlinked checkout, so both rendering paths are visible. Their repo and state
text carries ordinary punctuation (``&``, angle brackets, quotes) to show that
server-provided text renders as text; it is sample copy, not a payload.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from http.server import ThreadingHTTPServer  # noqa: E402

from brigade import fleet_command_deck, fleet_hub, fleet_hub_http, fleet_hub_policy  # noqa: E402
from brigade import fleet_policy, fleet_policy_page  # noqa: E402

FIXTURE_TOKEN = "fixture-token-not-a-real-credential"  # content-guard: allow api-key-assignment
LOOPBACK = "127.0.0.1"  # content-guard: allow loopback-ipv4
NODE_A = "11111111-1111-4111-8111-111111111111"

DOCUMENT = {
    "schema": fleet_policy.POLICY_SCHEMA,
    "defaults": {
        "roles": {"impl": "seat-alpha", "review": "seat-beta", "chef": "seat-alpha"},
        "data": {"allow_training": False, "allow_free": False, "retention": "30d"},
        "execution": {"concurrency": 2},
    },
    "machines": {
        "worker-linux-1": {"os": "linux", "concurrency": 4, "capabilities": ["docker"]},
        "worker-windows-1": {"os": "windows", "concurrency": 1, "draining": True},
    },
    "seats": {
        "seat-alpha": {
            "provider": "provider-a",
            "model": "model-a-1",
            "effort": "high",
            "cost_class": "paid",
            "eligible_machines": ["worker-linux-1"],
            "concurrency": 2,
            "quota_pool": "pool-one",
            "bindings": {"brigade": {"cli": "cli-a"}},
            "notes": "example worker seat",
        },
        "seat-beta": {
            "provider": "provider-b",
            "model": "model-b-1",
            "cost_class": "subscription",
            "retention": "7d",
        },
        "seat-gone": {"provider": "provider-a", "model": "model-a-ghost"},
        "seat-retired": {"provider": "provider-b", "model": "model-b-0"},
        "seat-blocked": {"provider": "provider-b", "model": "model-b-x"},
        "seat-pinned": {"provider": "provider-a", "model": "model-a-2", "pinned": True, "notes": "held on purpose"},
    },
    "consumers": {
        "brigade-run": {"reload": "refreshable", "coverage": "verified"},
        "t3-fleet": {"reload": "restart-required", "default_patches": {"execution": {"concurrency": 1}}},
    },
    "repositories": {
        "example/public-tool": {
            "privacy": "public",
            "owner": "team-example",
            "patches": {"data": {"allow_free": True}},
        },
        "example/private-tool": {"privacy": "private", "patches": {"data": {"allow_training": True}}},
    },
}

INVENTORY = fleet_policy_page.Inventory(
    providers={
        "provider-a": {
            "state": "fresh",
            "available": ["model-a-1", "model-a-2", "model-a-3"],
            "retired": [],
            "blocked": [],
        },
        "provider-b": {
            "state": "fresh",
            "available": ["model-b-1"],
            "retired": ["model-b-0"],
            "blocked": ["model-b-x"],
        },
    },
    source="fixture-snapshot",
    captured_at="2026-09-05T11:00:00Z",
)
# Scope and CLI auth are optional snapshot metadata. ``provider-b`` omits them
# on purpose so the unknown path is visible next to the populated one.
INVENTORY.providers["provider-a"]["scope"] = "native CLI listing"
INVENTORY.providers["provider-a"]["observed_at"] = "2026-09-05T10:15:00Z"
INVENTORY.providers["provider-a"]["cli_auth_state"] = "signed-in"

# Ordinary punctuation in operator-visible text, not an exploit payload.
TEXTY_REPO = 'notes & drafts <wip> "v2"'
TEXTY_STATE = "run.started <phase 2> & waiting"


def seed(db_path: Path) -> None:
    conn = fleet_hub.init_db(db_path)
    try:
        current = fleet_hub_policy.current_policy(conn)
        saved = fleet_hub_policy.save_policy(
            conn,
            DOCUMENT,
            expected_version=current["revision"],
            actor="fixture-operator",
            reason="fixture seed",
        )
        # session-one: acknowledged snapshot
        resolved_one = fleet_policy.resolve_policy(
            saved["document"],
            consumer="brigade-run",
            repo_identity="example/public-tool",
        )
        chash_one = "sha256:" + hashlib.sha256(b"session-one-fixture-hash").hexdigest()
        pending_one = fleet_hub_policy.record_pending_policy(
            conn,
            session_id="session-one",
            consumer="brigade-run",
            node_id=NODE_A,
            repo_identity="example/public-tool",
            origin="fixture",
            revision=saved["revision"],
            digest=saved["digest"],
            overrides={},
            override_reason=None,
            effective=resolved_one["effective"],
            sources=resolved_one["sources"],
            context_hash=chash_one,
        )
        fleet_hub_policy.record_acknowledged_policy(
            conn,
            pending_one,
            node_id=NODE_A,
            expected_context_hash=chash_one,
            loaded_at="2026-09-05T11:30:00Z",
        )
        # session-two: legacy session policy
        fleet_hub_policy.record_session_policy(
            conn,
            node_id=NODE_A,
            session_id="session-two",
            consumer="t3-fleet",
            repo_identity="example/public-tool",
            revision=saved["revision"],
            digest=saved["digest"],
            source="fixture",
            loaded_at="2026-09-05T11:30:00Z",
        )
        # session-three: pending snapshot (prepared, not yet acknowledged)
        resolved_three = fleet_policy.resolve_policy(
            saved["document"],
            consumer="brigade-run",
            repo_identity="example/public-tool",
        )
        chash_three = "sha256:" + hashlib.sha256(b"session-three-fixture-hash").hexdigest()
        fleet_hub_policy.record_pending_policy(
            conn,
            session_id="session-three",
            consumer="brigade-run",
            node_id=NODE_A,
            repo_identity="example/public-tool",
            origin="fixture",
            revision=saved["revision"],
            digest=saved["digest"],
            overrides={},
            override_reason=None,
            effective=resolved_three["effective"],
            sources=resolved_three["sources"],
            context_hash=chash_three,
        )
        fleet_hub.handle_claim(
            conn,
            {
                "action": "acquire",
                "target": "example/public-tool",
                "node_id": NODE_A,
                "holder": "fixture-holder",
                "conductor": "fixture-conductor",
                "ttl_seconds": 600,
            },
        )
        # One live run on a checkout with no canonical identity, so the repos
        # page shows the unlinked path beside the linked one.
        fleet_hub.store_events(
            conn,
            [
                {
                    "node_id": NODE_A,
                    "run_id": "fixture-run-1",
                    "state": "run.started",
                    "ts": "2026-09-05T11:45:00Z",
                    "sequence": 1,
                    "digest": "fixture-digest-1",
                    "repo": TEXTY_REPO,
                    "seat": "seat-alpha",
                    "harness": "fixture",
                },
                {
                    "node_id": NODE_A,
                    "run_id": "fixture-run-2",
                    "state": TEXTY_STATE,
                    "ts": "2026-09-05T11:46:00Z",
                    "sequence": 1,
                    "digest": "fixture-digest-2",
                    "repo": "example/public-tool",
                    "seat": "seat-beta",
                    "harness": "fixture",
                    "repo_identity": "example/public-tool",
                },
            ],
        )
        # A second revision so the audit history and rollback control have
        # something real to show.
        bumped = {**DOCUMENT, "defaults": {**DOCUMENT["defaults"], "execution": {"concurrency": 3}}}
        fleet_hub_policy.save_policy(
            conn,
            bumped,
            expected_version=saved["revision"],
            actor="fixture-operator",
            reason="raise default concurrency",
        )
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--inventory", choices=("fresh", "unavailable"), default="fresh")
    args = parser.parse_args(argv)

    workspace = Path(tempfile.mkdtemp(prefix="brigade-policy-ui-fixture-"))
    db_path = workspace / "fleet.db"
    seed(db_path)

    snapshot = (
        INVENTORY
        if args.inventory == "fresh"
        else fleet_policy_page.unavailable_inventory("fixture: provider inventory deliberately not read")
    )
    # Built here rather than through ``fleet_hub.make_server`` only so the
    # fixture can inject its static inventory snapshot without the hub server
    # entry point growing a development-only argument.
    server = ThreadingHTTPServer(
        (LOOPBACK, args.port),
        fleet_hub_http.make_handler(
            FIXTURE_TOKEN,
            db_path,
            deck_config=fleet_command_deck.DeckConfig(),
            policy_inventory=lambda: snapshot,
        ),
    )
    port = server.server_address[1]
    print(f"fixture hub database: {db_path}")
    print(f"open:  http://{LOOPBACK}:{port}/deck/policy?token={FIXTURE_TOKEN}")
    print(f"       http://{LOOPBACK}:{port}/deck/repos?token={FIXTURE_TOKEN}")
    print(f"       http://{LOOPBACK}:{port}/deck/roster?token={FIXTURE_TOKEN}")
    print(f"       http://{LOOPBACK}:{port}/?token={FIXTURE_TOKEN}")
    print("ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
