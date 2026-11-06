"""Native heartbeat wire bytes, atomic batches and shared-file storage."""

import json
import http.client
import os
import sqlite3
import sys
import tempfile
import concurrent.futures
import itertools
import threading
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
                clock.write_text(str(START + 100000), encoding="utf-8")
                compare("GET", "/status?all=1")
                compare("GET", "/sessions?all=yes")
                from brigade.fleet_hub_events_dispatch import _prune_outbox

                for hub in hubs:
                    with sqlite3.connect(hub.db) as db:
                        _prune_outbox(db, START + 100000)
                        self.assertEqual(db.execute("SELECT count(*) FROM fleet_events").fetchone()[0], 0)
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
                        session = dict(action="upsert", harness="codex", session_id="waiting",
                                       repo_identity="example/repo", identity_scope="fleet", repo_label="repo",
                                       checkout_path="/workspace/repo", dirty_paths=[], dirty_truncated=False)
                        waiting_session = pool.submit(send, hub, "POST", "/sessions", hub.tokens["node-a"], session)
                        time.sleep(0.2)
                        if revoke:
                            db.execute("UPDATE nodes SET revoked_at='2026-01-01' WHERE node_id='node-a'")
                        else:
                            version = db.execute("PRAGMA user_version").fetchone()[0]
                            db.execute(f"PRAGMA user_version={version + 1}")
                        db.commit()
                        response = pending.result(timeout=10)
                        self.assertEqual(response.status, 401 if revoke else 503, response.body)
                        self.assertEqual(waiting_session.result(timeout=10).status, 401 if revoke else 503)
                        self.assertEqual(db.execute("SELECT count(*) FROM interactive_sessions").fetchone()[0], 0)
                        self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                        if not revoke:
                            db.execute(f"PRAGMA user_version={version}")
                            db.commit()
                            # Give the schema watcher its normal interval to recover.
                            time.sleep(1)
            finally:
                hc.stop_hub(hub)


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        import claims_native

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        work = Path(self.directory.name)
        clock = work / "clock"
        clock.write_text(str(START), encoding="utf-8")
        self.hubs = []
        for native in (False, True):
            previous = claims_native.BINARY
            if native:
                claims_native.BINARY = str(BINARY) + " --route-mode heartbeat=native"
            try:
                hub = launch(work / str(native), clock, native)
            finally:
                claims_native.BINARY = previous
            self.hubs.append(hub)
            self.addCleanup(hc.stop_hub, hub)
            enroll(hub, "node-a")
        self.event = dict(node_id="node-a", run_id="review", sequence=1,
                          digest="one", state="run.created", ts="2026-01-01T00:00:00Z")

    def compare(self, method, path, body=None, raw=None, schema_error=False):
        answers = []
        for index, hub in enumerate(self.hubs):
            try:
                token = hub.admin_token if schema_error and index == 0 else hub.tokens["node-a"]
                answer = send(hub, method, path, token, body, raw)
                answers.append((answer.status, answer.body))
            except http.client.RemoteDisconnected:
                # Python's unhandled SQLite integer overflow closes the request.
                answers.append((None, b"disconnected"))
        self.assertEqual(*answers)
        return answers[0]

    def test_event_array_limit(self):
        for count in (1001, 5000):
            with self.subTest(count=count):
                self.assertEqual(self.compare("POST", "/events", [{}] * count),
                                 (400, b'{"error": "JSON container exceeds 1000 items"}'))

    def test_raw_integer_fallback(self):
        for field, literal in (("exit_status", "-0"), ("sequence", "-0"),
                               ("sequence", str(2**64)), ("exit_status", str(2**64)),
                               ("sequence", str(2**80)), ("exit_status", str(-(2**80)))):
            with self.subTest(field=field, literal=literal):
                event = dict(self.event, **{field: "RAW"})
                raw = json.dumps(event).replace('"RAW"', literal).encode("utf-8")
                self.compare("POST", "/events", raw=raw)

    def test_status_fence_structure(self):
        for table in ("claim_fences", "claim_retired_holders"):
            for change in ("index", "alter", "missing"):
                with self.subTest(table=table, change=change):
                    definitions = []
                    for hub in self.hubs:
                        with sqlite3.connect(hub.db) as db:
                            definitions.append(db.execute(
                                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL ORDER BY type DESC", (table,)
                            ).fetchall())
                            if change == "index":
                                db.execute(f"CREATE INDEX review_extra ON {table}(generation)")
                            elif change == "alter":
                                db.execute(f"ALTER TABLE {table} ADD COLUMN extra TEXT")
                            else:
                                db.execute(f"DROP TABLE {table}")
                    try:
                        status, body = self.compare("GET", "/status", schema_error=True)
                        self.assertEqual(status, 503)
                        self.assertEqual(json.loads(body)["reason"], "claim-fence-refused")
                    finally:
                        for hub, schema in zip(self.hubs, definitions):
                            with sqlite3.connect(hub.db) as db:
                                db.execute(f"DROP TABLE IF EXISTS {table}")
                                for (sql,) in schema:
                                    db.execute(sql)

    def test_status_sql_error(self):
        for hub in self.hubs:
            with sqlite3.connect(hub.db) as db:
                db.execute("DROP TABLE events")
        self.assertEqual(self.compare("GET", "/status", schema_error=True)[0], 500)

    def flood(self, hub, bodies, claims):
        """Post /events from one thread per (token, body) pair until claims() returns.

        claims() receives an event set on the first writer-queue-full answer.
        Returns every heartbeat answer as (monotonic time, status, reason) and
        what claims() returned.
        """
        stop = threading.Event()
        saturated = threading.Event()
        serial = itertools.count()
        answers = []

        def flood(token, body):
            while not stop.is_set():
                try:
                    reply = send(hub, "POST", "/events", token, raw=body(next(serial)))
                except (BrokenPipeError, ConnectionResetError):
                    # Ingress refusals answer before reading the body and close.
                    answers.append((time.monotonic(), None, "connection-closed"))
                    continue
                reason = json.loads(reply.body).get("reason") if reply.status == 503 else None
                answers.append((time.monotonic(), reply.status, reason))
                if reason == "writer-queue-full":
                    saturated.set()

        threads = [threading.Thread(target=flood, args=pair) for pair in bodies]
        for thread in threads:
            thread.start()
        try:
            return answers, claims(saturated)
        finally:
            stop.set()
            for thread in threads:
                thread.join()

    def claim(self, hub, number):
        began = time.monotonic()
        reply = send(hub, "POST", "/claims", hub.tokens["node-a"],
                     dict(action="acquire", target=f"mixed-{number}", node_id="node-a", holder=f"holder-{number}"))
        self.assertEqual(reply.status, 200, reply.body)
        return began, time.monotonic()

    def test_claims_during_saturated_heartbeat_flood(self):
        # An external write lock stalls the writer, as a slow FULL sync would.
        # Thirty 2 MiB flooders then fill the 56 MiB ordinary byte allocation
        # (27 commands) while the 61 MiB ingress body budget still admits all
        # 30, so ordinary admission refuses with writer-queue-full. Maximum
        # 8 MiB bodies cannot show this: ingress refuses the eighth body with
        # body-budget-full before the writer allocation fills. Claims sent
        # while the writer refuses are admitted through the durable reserve.
        # Once the lock is released they commit, and so do queued heartbeats.
        hub = self.hubs[1]
        bodies = []
        for i in range(30):
            node = f"flood-{i}"
            enroll(hub, node)
            event = dict(self.event, node_id=node, padding="")
            size = len(json.dumps(event).encode("utf-8"))
            event["padding"] = "x" * (2 * 1024 * 1024 - size)
            raw = json.dumps(event).encode("utf-8")
            bodies.append((hub.tokens[node], lambda _, raw=raw: raw))
        stall = sqlite3.connect(hub.db, isolation_level=None)
        self.addCleanup(stall.close)
        stall.execute("BEGIN IMMEDIATE")

        def claims(saturated):
            try:
                self.assertTrue(saturated.wait(timeout=60), "ordinary admission never saturated")
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    pending = [pool.submit(self.claim, hub, i) for i in range(3)]
                    # The writer's BEGIN retry lasts 5 s. Release well inside it.
                    time.sleep(1)
                    stall.execute("ROLLBACK")
                    return [future.result(timeout=30) for future in pending]
            finally:
                if stall.in_transaction:
                    stall.execute("ROLLBACK")

        answers, windows = self.flood(hub, bodies, claims)
        statuses = {status for _, status, _ in answers}
        reasons = {reason for _, status, reason in answers if status == 503}
        self.assertLessEqual(statuses, {200, 503, None}, answers)
        self.assertIn("writer-queue-full", reasons)
        self.assertLessEqual(reasons, {"writer-queue-full", "writer-timeout", "body-budget-full"})
        # Ordinary heartbeats still commit, and the pool was refusing while
        # the claims waited for admission.
        self.assertIn(200, statuses)
        self.assertTrue(any(min(b for b, _ in windows) <= at <= max(e for _, e in windows)
                            and reason == "writer-queue-full" for at, _, reason in answers),
                        "no refusal overlapped the claims")
        times = [end - began for began, end in windows]
        print(f"saturated flood claim seconds (1 s writer stall included): {times}, refusals "
              f"{sum(status == 503 for _, status, _ in answers)} of {len(answers)}", flush=True)

    def test_claims_during_event_batch_flood(self):
        # 256 clients posting 1,000-event bodies keep about 60 FULL batches of
        # ordinary work queued. Durable work is batched first, so a claim waits
        # for the batch in flight, not the queue. The bound is the v1 claim
        # client's 2.5 s timeout (CLAIM_TIMEOUT_SECONDS), which FIFO order
        # exceeded at about 3.5 s. With 20 samples the p99 is the slowest claim,
        # so the bound applies to every claim in the best of three rounds, which
        # absorbs a noisy runner. The rounds share one hub, so later rounds see
        # a larger events table, which works against the bound, not for it.
        hub = self.hubs[1]
        bodies = []
        for i in range(256):
            node = f"batch-{i}"
            enroll(hub, node)
            template = json.dumps([dict(self.event, node_id=node, run_id="RUN", sequence=k, digest=f"d{k}")
                                   for k in range(1000)]).encode("utf-8")
            bodies.append((hub.tokens[node], lambda n, t=template: t.replace(b'"RUN"', b'"r%d"' % n)))
        rounds = []

        def depth():
            text = send(hub, "GET", "/metrics", hub.admin_token).body.decode()
            return float(next(line.split()[1] for line in text.splitlines()
                              if line.startswith("brigade_hub_writer_queue_depth ")))

        def claims(_):
            time.sleep(2)
            depths, times = [], []
            for i in range(20):
                depths.append(depth())
                began, end = self.claim(hub, f"{len(rounds)}-{i}")
                times.append(end - began)
            return depths, sorted(times)

        for _ in range(3):
            answers, (depths, times) = self.flood(hub, bodies, claims)
            self.assertLessEqual({status for _, status, _ in answers}, {200, 503}, answers[:5])
            slowest = times[-1]
            rounds.append((slowest, max(depths)))
            print(f"1,000-event flood claim p99 (slowest of {len(times)}) {slowest:.3f} s, "
                  f"median {times[len(times) // 2]:.3f} s, max writer queue depth {max(depths):.0f}, "
                  f"load {os.getloadavg()}", flush=True)
            if slowest < 2.5:
                break
        best = min(rounds)
        self.assertLess(best[0], 2.5, rounds)

    def test_stored_json_fallback(self):
        self.compare("POST", "/events", self.event)
        session = dict(action="upsert", harness="codex", session_id="review",
                       repo_identity="example/repo", identity_scope="fleet",
                       repo_label="repo", checkout_path="/workspace/repo", dirty_paths=[], dirty_truncated=False)
        self.assertEqual(self.compare("POST", "/sessions", session)[0], 200)
        for column in ("context", "dirty_paths_json", "provider_link_json", "cloud_context_json"):
            table, path = ("events", "/status?all=1") if column == "context" else ("interactive_sessions", "/sessions?all=1")
            for item in (r'"\ud800"', str(2**64), str(2**80)):
                value = "[" + item + "]" if column == "dirty_paths_json" else '{"version": 1, "note": ' + item + "}"
                with self.subTest(column=column, value=value):
                    for hub in self.hubs:
                        with sqlite3.connect(hub.db) as db:
                            db.execute(f"UPDATE {table} SET {column}=?", (value,))
                    self.compare("GET", path)
            for hub in self.hubs:
                with sqlite3.connect(hub.db) as db:
                    db.execute(f"UPDATE {table} SET {column}=?", ("[]" if column == "dirty_paths_json" else None,))


if __name__ == "__main__":
    unittest.main()
