//! Slice 2 of the memory verification plan: the `read_stats` right, its
//! cursor, rate limit and audit rows (#2387).
use super::{proposal_tests::clean, read_counts::day, read_stats::*, tests::*, *};
use crate::writer as hub_writer;
use rusqlite::{Connection, params};
use serde_json::json;
use std::collections::BTreeSet;

const OTHER_TOKEN: &str = "fake-other-agent-token";

/// A hub with the audit table, a writer attached, a second agent principal and
/// cards in `fleet`, a repository scope and a collection.
async fn hub() -> Hub {
    let h = Hub::with(clean(), None);
    let db = Connection::open(&h.hub).unwrap();
    db.execute_batch(
        "CREATE TABLE fleet_audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
           actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);",
    )
    .unwrap();
    db.execute(
        "INSERT INTO fleet_message_principals VALUES('agent:other','other','Other',?1,'2026-01-01',NULL,NULL)",
        [digest(OTHER_TOKEN)],
    )
    .unwrap();
    let writer = hub_writer::Writer::start_unchecked(
        hub_writer::Config::new(h.hub.clone()),
        h.arbiter.clone(),
    )
    .unwrap();
    h.store
        .as_ref()
        .unwrap()
        .attach_audit(Arc::new(crate::memory_audit::HubAudit(writer)));
    for (scope, card) in [
        ("fleet", "alpha"),
        ("fleet", "beta"),
        ("fleet", "gamma"),
        ("repo:example/one", "alpha"),
        ("collection:vault", "alpha"),
    ] {
        let put = if scope.starts_with("collection:") {
            h.call(
                ADMIN_TOKEN,
                json!({"action":"import","scope":scope,"card_id":card,"base_revision":null,
                       "text":"collection card","client_event_id":h.id()}),
            )
            .await
        } else {
            h.put(ADMIN_TOKEN, scope, card, None, "a card").await
        };
        assert_eq!(put.status, 201, "{put:?}");
    }
    h
}
async fn grant(h: &Hub, principal: &str, scope: &str, rights: &[&str]) {
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"grant","principal":principal,"scope":scope,"rights":rights,
                   "client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 200, "{r:?}");
}
/// One `read_stats` call, a second after the previous one so the rate limit
/// never bites unless a test means it to.
async fn stats(h: &Hub, token: &str, body: Json) -> Reply {
    h.set_clock(h.now() + MIN_GAP_MS);
    h.call(token, body).await
}
fn ask(scope: &str) -> Json {
    json!({"action":"read_stats","scope":scope})
}
fn audit_rows(h: &Hub) -> Vec<(String, String, String, Json)> {
    let db = Connection::open(&h.hub).unwrap();
    let mut statement = db
        .prepare("SELECT actor,action,subject,detail FROM fleet_audit_log ORDER BY id")
        .unwrap();
    statement
        .query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                serde_json::from_str(&r.get::<_, String>(3)?).unwrap(),
            ))
        })
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}
/// Day rows and totals written straight to the memory file, as a flush would.
fn seed(h: &Hub, scope: &str, card: &str, days: &[(i64, i64, i64)]) {
    let db = Connection::open(h.memory()).unwrap();
    let today = day(h.now());
    let mut total = 0;
    for (ago, hits, reads) in days {
        db.execute(
            "INSERT INTO memory_read_days VALUES(?1,?2,?3,?4,?5)",
            params![scope, card, today - ago, hits, reads],
        )
        .unwrap();
        total += hits + reads;
    }
    let first = today - days.iter().map(|d| d.0).max().unwrap();
    let last = today - days.iter().map(|d| d.0).min().unwrap();
    db.execute(
        "INSERT INTO memory_read_totals VALUES(?1,?2,?3,?4,?5)",
        params![scope, card, first, last, total],
    )
    .unwrap();
}

#[tokio::test]
async fn read_stats_needs_read_and_read_stats_on_the_exact_scope() {
    let h = hub().await;
    seed(&h, "fleet", "alpha", &[(0, 1, 1)]);
    seed(&h, "collection:vault", "alpha", &[(0, 1, 0)]);
    let denied = |r: &Reply| r.status == 403 && code(r) == "memory_grant_missing";

    // A node reads `fleet` by default. That never opens the counts.
    assert!(denied(&stats(&h, NODE_TOKEN, ask("fleet")).await));
    // `read` alone.
    grant(&h, "agent:dot", "fleet", &["read"]).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    // `reflect` with `read`: reflect does not imply read_stats.
    grant(&h, "agent:dot", "fleet", &["read", "reflect", "review"]).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    // `read_stats` without `read`.
    grant(&h, "agent:dot", "fleet", &["read_stats"]).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    // Both rights on another scope.
    grant(&h, "agent:dot", "fleet", &[]).await;
    grant(&h, "agent:dot", "repo:example/one", &["read", "read_stats"]).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    // Wildcards carrying every right never open the counts.
    let all: Vec<&str> = auth::RIGHTS.to_vec();
    grant(&h, "agent:dot", "*", &all).await;
    grant(&h, "agent:dot", "repo:*", &all).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    // Both rights on the exact scope return rows.
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    let served = stats(&h, AGENT_TOKEN, ask("fleet")).await;
    assert_eq!(served.status, 200, "{served:?}");
    assert_eq!(served.body["rows"][0]["card_id"], "alpha");
    // A collection needs its own exact grant: `*` does not cover it.
    assert!(denied(
        &stats(&h, AGENT_TOKEN, ask("collection:vault")).await
    ));
    grant(&h, "agent:dot", "collection:vault", &["read", "read_stats"]).await;
    let collection = stats(&h, AGENT_TOKEN, ask("collection:vault")).await;
    assert_eq!(collection.status, 200, "{collection:?}");
    assert_eq!(collection.body["rows"][0]["total_reads"], 1);
    // Another principal's private scope never opens, whatever is granted.
    assert!(denied(
        &stats(&h, AGENT_TOKEN, ask("principal:agent:other")).await
    ));
}

#[tokio::test]
async fn rows_hold_only_card_ids_and_the_documented_counts() {
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    // Today, 3 days ago, 10 days ago and 40 days ago.
    seed(
        &h,
        "fleet",
        "alpha",
        &[(0, 2, 1), (3, 4, 0), (10, 0, 5), (40, 7, 0)],
    );
    seed(&h, "fleet", "beta", &[(20, 1, 1)]);
    // Read in another scope, and a retired card: neither appears.
    seed(&h, "repo:example/one", "alpha", &[(0, 9, 9)]);
    seed(&h, "fleet", "gone", &[(0, 3, 0)]);
    let reply = stats(&h, AGENT_TOKEN, ask("fleet")).await;
    assert_eq!(reply.status, 200, "{reply:?}");
    let keys: BTreeSet<&str> = reply
        .body
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        BTreeSet::from(["next_after", "read_counts_since_day", "rows", "scope"])
    );
    assert_eq!(reply.body["scope"], "fleet");
    assert!(reply.body["read_counts_since_day"].is_i64());
    assert_eq!(reply.body["next_after"], Json::Null);
    let today = day(h.now());
    assert_eq!(
        reply.body["rows"],
        json!([
            {"card_id":"alpha","reads_7d":7,"reads_30d":12,"last_read_day":today,"total_reads":19},
            {"card_id":"beta","reads_7d":0,"reads_30d":2,"last_read_day":today - 20,"total_reads":2},
        ])
    );
    for row in reply.body["rows"].as_array().unwrap() {
        let row = row.as_object().unwrap();
        assert_eq!(
            row.keys().map(String::as_str).collect::<BTreeSet<_>>(),
            BTreeSet::from([
                "card_id",
                "last_read_day",
                "reads_30d",
                "reads_7d",
                "total_reads"
            ])
        );
        assert!(row.iter().all(|(k, v)| if k == "card_id" {
            v.is_string()
        } else {
            v.is_i64()
        }));
    }
    // Unknown fields and out-of-range limits are refused.
    let extra = stats(
        &h,
        AGENT_TOKEN,
        json!({"action":"read_stats","scope":"fleet","query":"x"}),
    )
    .await;
    assert_eq!(
        (extra.status, code(&extra)),
        (400, "memory_request_invalid")
    );
    for limit in [0, PAGE_MAX + 1] {
        let r = stats(
            &h,
            AGENT_TOKEN,
            json!({"action":"read_stats","scope":"fleet","limit":limit}),
        )
        .await;
        assert_eq!(r.status, 400, "{limit}");
    }
}

#[tokio::test]
async fn pages_continue_only_through_an_untampered_live_cursor_for_the_same_caller_and_scope() {
    let h = hub().await;
    for principal in ["agent:dot", "agent:other"] {
        grant(&h, principal, "fleet", &["read", "read_stats"]).await;
        grant(&h, principal, "repo:example/one", &["read", "read_stats"]).await;
    }
    for card in ["alpha", "beta", "gamma"] {
        seed(&h, "fleet", card, &[(0, 1, 0)]);
    }
    let page = |after: Option<&str>, scope: &str| {
        let mut body = json!({"action":"read_stats","scope":scope,"limit":2});
        if let Some(after) = after {
            body["after"] = json!(after);
        }
        body
    };
    let first = stats(&h, AGENT_TOKEN, page(None, "fleet")).await;
    assert_eq!(first.body["rows"].as_array().unwrap().len(), 2);
    let handle = first.body["next_after"].as_str().unwrap().to_string();
    assert!(handle.starts_with("s1.") && handle.len() == 67);
    // A page never holds more than PAGE_MAX rows.
    let big = stats(
        &h,
        AGENT_TOKEN,
        json!({"action":"read_stats","scope":"fleet","limit":PAGE_MAX}),
    )
    .await;
    assert_eq!(big.body["rows"].as_array().unwrap().len(), 3);

    let refused = |r: &Reply| r.status == 400 && code(r) == "memory_request_invalid";
    // Altered: one changed character.
    let mut altered = handle.clone().into_bytes();
    let last = altered.len() - 1;
    altered[last] = if altered[last] == b'0' { b'1' } else { b'0' };
    let altered = String::from_utf8(altered).unwrap();
    assert!(refused(
        &stats(&h, AGENT_TOKEN, page(Some(&altered), "fleet")).await
    ));
    // A proposal cursor's shape is not a read_stats cursor.
    let foreign = format!("p1.{}", &handle[3..]);
    assert!(refused(
        &stats(&h, AGENT_TOKEN, page(Some(&foreign), "fleet")).await
    ));
    // Another principal.
    assert!(refused(
        &stats(&h, OTHER_TOKEN, page(Some(&handle), "fleet")).await
    ));
    // Another scope.
    assert!(refused(
        &stats(&h, AGENT_TOKEN, page(Some(&handle), "repo:example/one")).await
    ));
    // The owner continues on the same scope.
    let second = stats(&h, AGENT_TOKEN, page(Some(&handle), "fleet")).await;
    assert_eq!(second.status, 200, "{second:?}");
    assert_eq!(
        second.body["rows"],
        json!([{"card_id":"gamma","reads_7d":1,"reads_30d":1,"last_read_day":day(h.now()),"total_reads":1}])
    );
    assert_eq!(second.body["next_after"], Json::Null);
    // Expired: past the lifetime, the same handle is refused.
    let fresh = stats(&h, AGENT_TOKEN, page(None, "fleet")).await;
    let fresh = fresh.body["next_after"].as_str().unwrap().to_string();
    h.set_clock(h.now() + CURSOR_TTL_MS);
    assert!(refused(
        &stats(&h, AGENT_TOKEN, page(Some(&fresh), "fleet")).await
    ));
}

#[tokio::test]
async fn the_rate_limit_refuses_over_the_limit_and_recovers() {
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    grant(&h, "agent:other", "fleet", &["read", "read_stats"]).await;
    let first = stats(&h, AGENT_TOKEN, ask("fleet")).await;
    assert_eq!(first.status, 200);
    // Same second: refused, and the refusal does not move the window.
    for _ in 0..3 {
        let again = h.call(AGENT_TOKEN, ask("fleet")).await;
        assert_eq!(
            (again.status, code(&again)),
            (429, "memory_read_stats_rate_limited")
        );
    }
    // Another principal has its own limit.
    assert_eq!(h.call(OTHER_TOKEN, ask("fleet")).await.status, 200);
    h.set_clock(h.now() + MIN_GAP_MS);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 200);
}

#[test]
fn the_daily_limit_resets_at_the_utc_day_boundary() {
    let limiter = Limiter::default();
    let start = 1_800_000_000_000 - 1_800_000_000_000 % read_counts::DAY_MS;
    for n in 0..DAILY_MAX as i64 {
        assert!(limiter.admit("agent:dot", start + n * MIN_GAP_MS), "{n}");
    }
    let over = start + DAILY_MAX as i64 * MIN_GAP_MS;
    assert!(!limiter.admit("agent:dot", over));
    assert!(limiter.admit("agent:other", over));
    assert!(!limiter.admit("agent:dot", start + read_counts::DAY_MS - 1));
    assert!(limiter.admit("agent:dot", start + read_counts::DAY_MS));
}

#[tokio::test]
async fn every_request_writes_one_audit_row_and_no_memory_event() {
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    seed(&h, "fleet", "alpha", &[(0, 1, 0)]);
    seed(&h, "fleet", "beta", &[(0, 1, 0)]);
    let events = h.events();
    let before = audit_rows(&h).len();
    let mut statuses = Vec::new();
    // Served, refused for grants, invalid, rate limited, and an admin read.
    statuses.push(stats(&h, AGENT_TOKEN, ask("fleet")).await.status);
    statuses.push(stats(&h, AGENT_TOKEN, ask("repo:example/one")).await.status);
    statuses.push(stats(&h, AGENT_TOKEN, ask("not a scope")).await.status);
    statuses.push(h.call(AGENT_TOKEN, ask("fleet")).await.status);
    statuses.push(stats(&h, NODE_TOKEN, ask("fleet")).await.status);
    statuses.push(stats(&h, ADMIN_TOKEN, ask("fleet")).await.status);
    assert_eq!(statuses, [200, 403, 400, 429, 403, 200]);
    let rows = audit_rows(&h);
    assert_eq!(rows.len() - before, statuses.len());
    let expected = [
        ("agent:dot", json!({"scope":"fleet","count":2,"status":200})),
        (
            "agent:dot",
            json!({"scope":"repo:example/one","count":0,"status":403}),
        ),
        ("agent:dot", json!({"count":0,"status":400})),
        ("agent:dot", json!({"scope":"fleet","count":0,"status":429})),
        ("node-1", json!({"scope":"fleet","count":0,"status":403})),
        ("admin", json!({"scope":"fleet","count":2,"status":200})),
    ];
    for ((actor, action, subject, detail), (who, want)) in rows[before..].iter().zip(expected) {
        assert_eq!(
            (actor.as_str(), action.as_str(), subject.as_str()),
            (who, AUDIT_ACTION, who)
        );
        assert_eq!(detail, &want);
    }
    // The memory event log is untouched, so nothing replicates to readers.
    assert_eq!(h.events(), events);
}

#[tokio::test]
async fn without_an_audit_writer_nothing_is_served() {
    let h = Hub::with(clean(), None);
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    let r = h.call(AGENT_TOKEN, ask("fleet")).await;
    assert_eq!((r.status, code(&r)), (503, "memory_action_unavailable"));
    assert!(r.body.get("rows").is_none());
    // An audit table that refuses the insert also withholds the rows.
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    seed(&h, "fleet", "alpha", &[(0, 1, 0)]);
    Connection::open(&h.hub)
        .unwrap()
        .execute_batch(
            "CREATE TRIGGER no_audit BEFORE INSERT ON fleet_audit_log BEGIN SELECT RAISE(ABORT,'closed'); END;",
        )
        .unwrap();
    let r = stats(&h, AGENT_TOKEN, ask("fleet")).await;
    assert_eq!((r.status, code(&r)), (503, "memory_action_unavailable"));
    assert!(r.body.get("rows").is_none());
}

#[test]
fn read_stats_is_not_a_pressure_exempt_read() {
    // It writes an audit row, so disk pressure refuses it like a write.
    assert!(!read_only(br#"{"action":"read_stats","scope":"fleet"}"#));
}
