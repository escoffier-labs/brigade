//! Deterministic work and wire parity regressions for #2443.
#![cfg(feature = "memory-scan")]
use super::tests::{ADMIN_TOKEN, Hub, NODE_TOKEN};
use super::{Conn, ReadTx, Value, scan};
use http_body_util::BodyExt;
use rusqlite::Connection;
use serde_json::json;
use std::cell::{Cell, RefCell};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
#[path = "../memory_response.rs"]
mod response;

struct CountRows<'a> {
    conn: Conn<'a>,
    joined: Cell<usize>,
    statements: Cell<usize>,
    instructions: Cell<usize>,
    plan: RefCell<Vec<String>>,
}
impl ReadTx for CountRows<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        if sql.contains("WITH RECURSIVE walk") {
            let mut stmt = self.conn.0.prepare(&format!("EXPLAIN QUERY PLAN {sql}"))?;
            let plan = stmt
                .query_map(rusqlite::params_from_iter(params), |r| {
                    r.get::<_, String>(3)
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            *self.plan.borrow_mut() = plan;
        }
        let steps = Arc::new(AtomicUsize::new(0));
        let counter = steps.clone();
        self.conn.0.progress_handler(
            1,
            Some(move || {
                counter.fetch_add(1, Ordering::Relaxed);
                false
            }),
        )?;
        let result = self.conn.query(sql, params);
        self.conn.0.progress_handler(0, None::<fn() -> bool>)?;
        let rows = result?;
        self.statements.set(self.statements.get() + 1);
        if sql.contains("LEFT JOIN memory_scans") {
            self.joined.set(self.joined.get() + rows.len());
            self.instructions.set(steps.load(Ordering::Relaxed));
        }
        Ok(rows)
    }
}

#[tokio::test]
async fn push_card_negative_copy_budget_and_body_parity() {
    let h = Hub::with(scan::bundled(), None);
    // Use the same store and decoy for all cases.
    for (id, copies) in [("one", 1), ("five", 5), ("sixty_four", 64), ("many", 70)] {
        for n in 0..copies {
            let r = h
                .call(
                    ADMIN_TOKEN,
                    json!({"action":"import",
                "scope":format!("repo:example/copy{n:02}"), "card_id":id,
                "text":"release checklist", "client_event_id":h.id()}),
                )
                .await;
            assert_eq!(r.status, 201, "{:?}", r.body);
        }
    }
    let conn = Connection::open(h.memory()).unwrap();
    conn.execute("UPDATE memory_card_revisions SET scan_state='pending'", [])
        .unwrap();
    let credential = h
        .store
        .as_ref()
        .unwrap()
        .authenticate(NODE_TOKEN)
        .await
        .unwrap();
    let ruleset = scan::bundled().describe().1.unwrap();
    let mut baseline = None;
    let mut joined = Vec::new();
    let mut instructions = Vec::new();
    for id in ["absent", "one", "five", "sixty_four", "many", "zzabsent"] {
        let tx = CountRows {
            conn: Conn(&conn),
            joined: Cell::new(0),
            statements: Cell::new(0),
            instructions: Cell::new(0),
            plan: RefCell::default(),
        };
        let body = json!({"action":"card", "card_id":id, "push":true});
        // Warm the prepared program before counting VM execution. Schema and
        // statement compilation are outside this discovery-work regression.
        for _ in 0..2 {
            super::reads(
                &tx,
                &credential,
                "card",
                body.as_object().unwrap(),
                h.now(),
                (true, Some(ruleset.clone())),
                0,
            )
            .unwrap();
        }
        let warm_instructions = tx.instructions.get();
        tx.joined.set(0);
        tx.statements.set(0);
        let reply = super::reads(
            &tx,
            &credential,
            "card",
            body.as_object().unwrap(),
            h.now(),
            (true, Some(ruleset.clone())),
            0,
        )
        .unwrap();
        assert_eq!(
            tx.instructions.get(),
            warm_instructions,
            "repeat VM count: {id}"
        );
        instructions.push(tx.instructions.get());
        assert!(
            tx.plan
                .borrow()
                .iter()
                .any(|s| s.contains("memory_cards_id") && s.contains("(card_id,scope)>(?,?)")),
            "{:?}",
            tx.plan.borrow()
        );
        assert_eq!(reply.status, 404);
        let response = response::respond(reply.status, reply.body);
        let headers = response.headers().clone();
        let bytes = response
            .into_body()
            .collect()
            .await
            .unwrap()
            .to_bytes()
            .to_vec();
        assert_eq!(
            bytes,
            br#"{"code":"memory_card_not_found","error":"card not found"}"#
        );
        joined.push(tx.joined.get());
        let observed = (tx.statements.get(), bytes, headers);
        if let Some(expected) = &baseline {
            assert_eq!(&observed, expected, "{id}");
        }
        baseline = Some(observed);
    }
    assert_eq!(
        joined,
        vec![65; 6],
        "absent, one, five, sixty_four, many, absent-after-index joined-head budgets"
    );
    println!("VM instructions absent/1/5/64/70/absent-after-index: {instructions:?}");
    let spread = instructions.iter().max().unwrap() - instructions.iter().min().unwrap();
    assert_eq!(
        spread, 0,
        "discovery VM instruction spread {spread}: {instructions:?}"
    );
}

#[tokio::test]
async fn push_card_budget_overflow_requires_scope_and_preserves_ambiguity() {
    let h = Hub::with(scan::bundled(), None);
    for n in 0..65 {
        let reply = h
            .call(
                ADMIN_TOKEN,
                json!({"action":"import",
            "scope":format!("repo:example/copy{n:02}"), "card_id":"shared",
            "text":"release checklist", "client_event_id":h.id()}),
            )
            .await;
        assert_eq!(reply.status, 201, "{:?}", reply.body);
        if n == 63 {
            let reply = h
                .call(
                    ADMIN_TOKEN,
                    json!({"action":"card", "card_id":"shared", "push":true}),
                )
                .await;
            assert_eq!(reply.status, 409, "{:?}", reply.body);
            assert_eq!(reply.body["scopes"].as_array().unwrap().len(), 64);
        }
    }
    let reply = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"card", "card_id":"shared", "push":true}),
        )
        .await;
    assert_eq!(reply.status, 404, "{:?}", reply.body);
    let reply = h.call(ADMIN_TOKEN, json!({"action":"card", "card_id":"shared", "scope":"repo:example/copy64", "push":true})).await;
    assert_eq!(reply.status, 200, "{:?}", reply.body);
}

#[path = "push_latency_measure.rs"]
mod latency;

#[tokio::test]
async fn push_card_discovery_survives_missing_index() {
    let h = Hub::with(scan::bundled(), None);
    let r = h.call(ADMIN_TOKEN, json!({"action":"import", "scope":"repo:example/card", "card_id":"available", "text":"release checklist", "client_event_id":h.id()})).await;
    assert_eq!(r.status, 201);
    let conn = Connection::open(h.memory()).unwrap();
    conn.execute("DROP INDEX memory_cards_id", []).unwrap();
    for (id, status) in [("available", 200), ("absent", 404)] {
        let r = h
            .call(
                NODE_TOKEN,
                json!({"action":"card", "card_id":id, "push":true}),
            )
            .await;
        assert_eq!(r.status, status, "{:?}", r.body);
    }
    conn.execute("UPDATE memory_card_revisions SET scan_state='pending'", [])
        .unwrap();
    let r = h
        .call(
            NODE_TOKEN,
            json!({"action":"card", "card_id":"available", "push":true}),
        )
        .await;
    assert_eq!(r.status, 404, "{:?}", r.body);
}

#[tokio::test]
async fn push_card_scope_successor_preserves_prefix_scopes() {
    let h = Hub::with(scan::bundled(), None);
    let scopes = ["repo:example/prefix", "repo:example/prefix/child"];
    for scope in scopes {
        let r = h.call(ADMIN_TOKEN, json!({"action":"import", "scope":scope, "card_id":"shared", "text":"release checklist", "client_event_id":h.id()})).await;
        assert_eq!(r.status, 201, "{:?}", r.body);
    }
    let conn = Connection::open(h.memory()).unwrap();
    for indexed in [true, false] {
        if !indexed {
            conn.execute("DROP INDEX memory_cards_id", []).unwrap();
        }
        let r = h
            .call(
                NODE_TOKEN,
                json!({"action":"card", "card_id":"shared", "push":true}),
            )
            .await;
        assert_eq!(r.status, 409, "{:?}", r.body);
        assert_eq!(r.body["scopes"], json!(scopes));
        for scope in scopes {
            let r = h
                .call(
                    NODE_TOKEN,
                    json!({"action":"card", "scope":scope, "card_id":"shared", "push":true}),
                )
                .await;
            assert_eq!(r.status, 200, "{:?}", r.body);
            assert_eq!(r.body["card"]["scope"], scope);
            assert_eq!(r.body["card"]["text"], "release checklist\n");
        }
    }
}
