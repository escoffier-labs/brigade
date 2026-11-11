//! Deterministic work and wire parity regressions for #2443.
#![cfg(feature = "memory-scan")]
use super::tests::{ADMIN_TOKEN, Hub, NODE_TOKEN};
use super::{Conn, ReadTx, Value, scan};
use http_body_util::BodyExt;
use rusqlite::Connection;
use serde_json::json;
use std::cell::Cell;
#[path = "../memory_response.rs"]
mod response;

struct CountRows<'a> {
    conn: Conn<'a>,
    joined: Cell<usize>,
    statements: Cell<usize>,
}
impl ReadTx for CountRows<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let rows = self.conn.query(sql, params)?;
        self.statements.set(self.statements.get() + 1);
        if sql.contains("LEFT JOIN memory_scans") {
            self.joined.set(self.joined.get() + rows.len());
        }
        Ok(rows)
    }
}

#[tokio::test]
async fn push_card_negative_copy_budget_and_body_parity() {
    let h = Hub::with(scan::bundled(), None);
    // Use the same store and decoy for all cases.
    for (id, copies) in [("one", 1), ("five", 5), ("many", 70)] {
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
    for id in ["absent", "one", "five", "many"] {
        let tx = CountRows {
            conn: Conn(&conn),
            joined: Cell::new(0),
            statements: Cell::new(0),
        };
        let body = json!({"action":"card", "card_id":id, "push":true});
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
        vec![65; 4],
        "absent, one, five, many joined-head budgets"
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
