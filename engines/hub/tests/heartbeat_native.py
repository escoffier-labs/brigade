"""Native heartbeat wire bytes, atomic batches and shared-file storage."""

import json
import sqlite3
import sys
import tempfile
import concurrent.futures
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claims_native import BINARY, START, enroll, launch, send
import hub_contract as hc


class Heartbeat(unittest.TestCase):
    def test_wire_and_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            hubs = []
            try:
                # launch() already selects registration and claims. Add heartbeat
                # and messages so every native family shares the same writer.
                import claims_native

                previous = claims_native.BINARY
                claims_native.BINARY = str(BINARY) + " --route-mode heartbeat=native --route-mode messages=native"
                try:
                    hubs.append(launch(work / "native", clock, True, events=True))
                finally:
                    claims_native.BINARY = previous
                hubs.insert(0, launch(work / "python", clock, False, events=True))
                for hub in hubs:
                    enroll(hub, "node-a")
                    enroll(hub, "node-b")
                    with sqlite3.connect(hub.db) as db:
                        db.execute(
                            "INSERT INTO fleet_event_subscriptions(id,principal_id,name,arguments,url,secret,created_at) VALUES('sub-run','node-a','run.failed','{}','https://events.example.test/hook','fake',?)",
                            (START,),
                        )

                def compare(method, path, body=None, who="node-a", raw=None):
                    answers = [send(h, method, path, h.tokens.get(who), body, raw) for h in hubs]
                    self.assertEqual(answers[0].status, answers[1].status, (path, body, answers))
                    self.assertEqual(answers[0].body, answers[1].body, (path, body))
                    for header in ("content-type", "cache-control", "referrer-policy"):
                        self.assertEqual(answers[0].headers.get(header), answers[1].headers.get(header))
                    return answers[1].body

                event = dict(
                    node_id="node-a",
                    run_id="run-one",
                    sequence=1,
                    digest="digest-one",
                    state="run.created",
                    ts="2026-01-01T00:00:00Z",
                    repo="café",
                    seat="worker",
                )
                for path in ("/status", "/sessions"):
                    compare("GET", path)
                    compare("GET", path, who="bogus")
                    compare("GET", path, who=None)
                for body in (
                    None,
                    [],
                    3,
                    [3],
                    {},
                    dict(event, sequence=True),
                    dict(event, sequence=-1),
                    dict(event, state="x\x7f"),
                    dict(event, repo="x" * 1025),
                    dict(event, node_id="node-b"),
                    dict(event, harness="grokbot"),
                    dict(event, exit_status=2**63),
                    dict(event, context={"schema": "wrong"}),
                ):
                    compare("POST", "/events", body)
                # The wire limit is inclusive. Writer reply accounting must
                # not turn a valid 8 MiB body into writer-budget shedding.
                maximum = dict(event, run_id="maximum-body", padding="")
                overhead = len(json.dumps(maximum).encode("utf-8"))
                maximum["padding"] = "x" * (8 * 1024 * 1024 - overhead)
                raw_maximum = json.dumps(maximum).encode("utf-8")
                self.assertEqual(len(raw_maximum), 8 * 1024 * 1024)
                self.assertEqual(compare("POST", "/events", raw=raw_maximum), b'{"accepted": 1, "duplicate": 0}')
                compare("POST", "/events", event)
                compare("POST", "/events", event)
                compare("POST", "/events", [dict(event, sequence=2, digest="two"), dict(event, node_id="node-b")])
                compare("POST", "/events", [dict(event, sequence=i, digest=f"batch-{i}") for i in range(3, 1003)])
                compare(
                    "POST", "/events", dict(event, sequence=1003, digest="terminal", state="run.completed", seat=None)
                )
                compare("GET", "/status")
                compare("POST", "/events", dict(event, run_id="failed", state="run.failed", exit_status=7))
                compare("GET", "/status?all=1")
                session = dict(
                    action="upsert",
                    harness="codex",
                    session_id="session-one",
                    repo_identity="example/repo",
                    identity_scope="fleet",
                    repo_label="repo",
                    checkout_path="/workspace/repo",
                    branch=" main ",
                    dirty_paths=["café.py", "src\\main.rs"],
                    dirty_truncated=False,
                )
                for body in (
                    [],
                    {},
                    dict(session, node_id="node-a"),
                    dict(session, ttl_seconds=True),
                    dict(session, dirty_paths=["../secret"]),
                    dict(session, branch="\x7f"),
                    dict(session, repo_identity="https://example.test/repo"),
                    session,
                ):
                    compare("POST", "/sessions", body)
                compare("POST", "/sessions", dict(session, **{"bad\nkey": 1}))
                compare("POST", "/sessions", {"action": []})
                compare("GET", "/sessions")
                clock.write_text(str(START + 60), encoding="utf-8")
                compare("POST", "/sessions", session)
                compare("POST", "/sessions", dict(session, repo_identity="example/moved"))
                link = dict(
                    session,
                    harness="t3",
                    session_id="thread-one",
                    provider_link={"harness": "codex", "session_id": "session-one"},
                    repo_identity="example/moved",
                )
                compare("POST", "/sessions", link)
                compare("POST", "/sessions", {k: v for k, v in link.items() if k != "provider_link"})
                compare("GET", "/sessions?all=1")
                compare(
                    "POST",
                    "/sessions",
                    dict(action="end", harness="codex", session_id="session-one", repo_identity="example/moved"),
                )
                compare("GET", "/sessions")
                clock.write_text(str(START + 100000), encoding="utf-8")
                compare("GET", "/status?all=1")
                compare("GET", "/sessions?all=yes")
                # End checks the exact repository identity. Cloud linkage on
                # another repository must not turn a missing local row into an error.
                for hub in hubs:
                    with sqlite3.connect(hub.db) as db:
                        db.execute(
                            "UPDATE interactive_sessions SET cloud_context_json=? WHERE harness='codex' AND repo_identity='example/repo'",
                            (json.dumps({"version": 1}),),
                        )
                compare(
                    "POST",
                    "/sessions",
                    dict(action="end", harness="codex", session_id="session-one", repo_identity="example/missing"),
                )

                compare("GET", "/status?token=forbidden")
                for raw in (b"{", b'{"sequence": -0}', b'{"note": NaN}'):
                    compare("POST", "/events", raw=raw)
                for table in ("events", "interactive_sessions"):
                    rows = []
                    for hub in hubs:
                        with sqlite3.connect(hub.db) as db:
                            rows.append(db.execute(f"SELECT * FROM {table} ORDER BY 1,2,3,4").fetchall())
                    self.assertEqual(*rows, table)
                outboxes = []
                for hub in hubs:
                    with sqlite3.connect(hub.db) as db:
                        outboxes.append(
                            db.execute(
                                "SELECT name,subject,producer,data,created_at FROM fleet_events ORDER BY name,subject"
                            ).fetchall()
                        )
                self.assertEqual(*outboxes)
                self.assertEqual(len(outboxes[0]), 1)
            finally:
                for hub in hubs:
                    hc.stop_hub(hub)

    def test_writer_rechecks_schema_and_credential(self):
        import claims_native

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            clock = work / "clock"
            clock.write_text(str(START), encoding="utf-8")
            previous = claims_native.BINARY
            claims_native.BINARY = str(BINARY) + " --route-mode heartbeat=native"
            try:
                hub = launch(work / "hub", clock, True, events=True)
            finally:
                claims_native.BINARY = previous
            try:
                enroll(hub, "node-a")
                event = dict(
                    node_id="node-a",
                    run_id="atomic",
                    sequence=1,
                    digest="one",
                    state="run.created",
                    ts="2026-01-01T00:00:00Z",
                )
                for revoke in (False, True):
                    with sqlite3.connect(hub.db) as db, concurrent.futures.ThreadPoolExecutor() as pool:
                        db.execute("BEGIN IMMEDIATE")
                        pending = pool.submit(send, hub, "POST", "/events", hub.tokens["node-a"], event)
                        time.sleep(0.2)
                        if revoke:
                            db.execute("UPDATE nodes SET revoked_at='2026-01-01' WHERE node_id='node-a'")
                        else:
                            version = db.execute("PRAGMA user_version").fetchone()[0]
                            db.execute(f"PRAGMA user_version={version + 1}")
                        db.commit()
                        response = pending.result(timeout=10)
                        self.assertEqual(response.status, 401 if revoke else 503, response.body)
                        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                        if not revoke:
                            db.execute(f"PRAGMA user_version={version}")
                            db.commit()
                            # Give the schema watcher its normal interval to recover.
                            time.sleep(1)
            finally:
                hc.stop_hub(hub)


if __name__ == "__main__":
    unittest.main()
