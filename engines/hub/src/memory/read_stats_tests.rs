//! Slice 2 of the memory verification plan: the `read_stats` right, its
//! cursor, rate limit and audit rows (#2387).
use super::{proposal_tests::clean, read_counts::day, read_stats::*, tests::*, *};
use crate::{
    memory_audit::{HubAudit, IN_FLIGHT},
    writer::{self as hub_writer, ApplyError, Command, WriteTx},
};
use rusqlite::{Connection, params};
use serde_json::json;
use std::collections::BTreeSet;

const OTHER_TOKEN: &str = "fake-other-agent-token";

/// A hub with the audit table, a writer attached, a second agent principal and
/// cards in `fleet`, a repository scope and a collection.
async fn hub() -> Hub {
    hub_and_writer().await.0
}
/// The same hub and the hub writer its audit log uses, so a test can submit
/// another family's work to that writer.
async fn hub_and_writer() -> (Hub, hub_writer::Writer) {
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
        .attach_audit(Arc::new(HubAudit::new(writer.clone())));
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
    (h, writer)
}
/// Another writer family's command: one row in a table memory never touches.
struct Probe;
impl Command for Probe {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        tx.execute(
            "INSERT INTO claims_probe(value) VALUES('other family')",
            &[],
        )?;
        Ok(json!({}))
    }
}
async fn other_family(writer: &hub_writer::Writer) -> bool {
    let budget = hub_writer::Budget {
        events: 1,
        bytes: 64,
        durable: false,
        deadline: std::time::Instant::now() + std::time::Duration::from_secs(5),
    };
    match writer.submit(Probe, budget).await {
        Ok(answer) => matches!(answer.await, Ok(Ok(_))),
        Err(_) => false,
    }
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
    // Exact `read_stats` with `read` from a node default, `*` or `repo:*`:
    // both rights must sit on one grant naming the scope.
    grant(&h, "node-1", "fleet", &["read_stats"]).await;
    assert!(denied(&stats(&h, NODE_TOKEN, ask("fleet")).await));
    grant(&h, "agent:dot", "*", &["read"]).await;
    assert!(denied(&stats(&h, AGENT_TOKEN, ask("fleet")).await));
    grant(&h, "agent:dot", "repo:*", &["read"]).await;
    grant(&h, "agent:dot", "repo:example/one", &["read_stats"]).await;
    assert!(denied(
        &stats(&h, AGENT_TOKEN, ask("repo:example/one")).await
    ));
    grant(&h, "agent:dot", "*", &[]).await;
    grant(&h, "agent:dot", "repo:*", &[]).await;
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

const START: i64 = 1_800_000_000_000 - 1_800_000_000_000 % read_counts::DAY_MS;
fn admitted(gate: &Gate, principal: &str, now_ms: i64) -> bool {
    gate.decide(principal, now_ms) == Decision::Admit
}

#[test]
fn the_daily_limit_resets_at_the_utc_day_boundary() {
    let gate = Gate::default();
    for n in 0..DAILY_MAX as i64 {
        assert!(admitted(&gate, "agent:dot", START + n * MIN_GAP_MS), "{n}");
    }
    let over = START + DAILY_MAX as i64 * MIN_GAP_MS;
    assert!(!admitted(&gate, "agent:dot", over));
    assert!(admitted(&gate, "agent:other", over));
    assert!(!admitted(
        &gate,
        "agent:dot",
        START + read_counts::DAY_MS - 1
    ));
    assert!(admitted(&gate, "agent:dot", START + read_counts::DAY_MS));
}

#[test]
fn refusals_merge_into_one_summary_per_principal_per_window() {
    let gate = Gate::default();
    assert!(admitted(&gate, "agent:dot", START));
    // The first refusal is summarized at once, the rest of the window waits.
    assert_eq!(
        gate.decide("agent:dot", START),
        Decision::Refuse(Some(("agent:dot".into(), 1)))
    );
    for _ in 0..10_000 {
        assert_eq!(gate.decide("agent:dot", START), Decision::Refuse(None));
    }
    // Another principal's refusals are its own.
    assert!(admitted(&gate, "agent:other", START));
    assert_eq!(
        gate.decide("agent:other", START),
        Decision::Refuse(Some(("agent:other".into(), 1)))
    );
    // A summary that was not written goes back into the next one.
    gate.unsent("agent:other", 1);
    let later = START + SUMMARY_WINDOW_MS;
    assert!(admitted(&gate, "agent:dot", later));
    assert_eq!(
        gate.decide("agent:dot", later),
        Decision::Refuse(Some(("agent:dot".into(), 10_001)))
    );
    assert!(admitted(&gate, "agent:other", later));
    assert_eq!(
        gate.decide("agent:other", later),
        Decision::Refuse(Some(("agent:other".into(), 2)))
    );
}

#[test]
fn the_hub_wide_row_budget_caps_audit_rows_per_day() {
    let gate = Gate::default();
    let mut rows = 0u32;
    let mut last = START;
    // Forty principals each try their whole daily quota: 24,000 requests.
    for p in 0..40 {
        for n in 0..DAILY_MAX as i64 {
            last = START + n * MIN_GAP_MS;
            match gate.decide(&format!("agent:p{p}"), last) {
                Decision::Admit | Decision::Refuse(Some(_)) => rows += 1,
                Decision::Refuse(None) => {}
            }
        }
    }
    assert_eq!(rows, AUDIT_DAILY_ROWS);
    // Spent: a fresh principal is refused and adds no row, not even a summary.
    assert_eq!(gate.decide("agent:fresh", last + 1), Decision::Refuse(None));
    // The next UTC day starts a new budget.
    assert!(admitted(&gate, "agent:fresh", START + read_counts::DAY_MS));
}

#[test]
fn principals_past_the_table_share_one_untracked_summary() {
    let gate = Gate::default();
    for p in 0..PRINCIPALS {
        assert!(admitted(&gate, &format!("agent:p{p}"), START));
    }
    assert_eq!(
        gate.decide("agent:late", START),
        Decision::Refuse(Some((UNTRACKED_ACTOR.into(), 1)))
    );
    for _ in 0..100 {
        assert_eq!(gate.decide("agent:later", START), Decision::Refuse(None));
    }
    // Next day the old entries go and the late principal is tracked again.
    assert!(admitted(&gate, "agent:late", START + read_counts::DAY_MS));
}

/// The gate holds `PRINCIPALS` principals of `prefix` admitted at `now`.
fn fill(gate: &Gate, prefix: &str, now: i64) {
    for p in 0..PRINCIPALS {
        assert!(admitted(gate, &format!("agent:{prefix}{p}"), now));
    }
}

#[test]
fn a_summary_that_fails_across_the_day_boundary_is_never_lost() {
    // With room after the eviction: the principal gets a new entry.
    let gate = Gate::default();
    fill(&gate, "p", START);
    // agent:p0's summary for one refusal is in flight when the day rolls over.
    assert_eq!(
        gate.decide("agent:p0", START),
        Decision::Refuse(Some(("agent:p0".into(), 1)))
    );
    let tomorrow = START + read_counts::DAY_MS;
    // A new principal finds the table full and evicts every earlier entry.
    assert!(admitted(&gate, "agent:new", tomorrow));
    // The insert fails. The count must survive into agent:p0's next summary.
    gate.unsent("agent:p0", 1);
    assert!(admitted(&gate, "agent:p0", tomorrow));
    assert_eq!(
        gate.decide("agent:p0", tomorrow),
        Decision::Refuse(Some(("agent:p0".into(), 2)))
    );

    // Without room: the table filled again on the new day, so the count joins
    // the untracked summary.
    let gate = Gate::default();
    fill(&gate, "p", START);
    assert_eq!(
        gate.decide("agent:p0", START),
        Decision::Refuse(Some(("agent:p0".into(), 1)))
    );
    fill(&gate, "q", tomorrow);
    gate.unsent("agent:p0", 1);
    assert_eq!(
        gate.decide("agent:late", tomorrow),
        Decision::Refuse(Some((UNTRACKED_ACTOR.into(), 2)))
    );
}

#[test]
fn a_summary_never_stores_more_than_the_reader_accepts() {
    let gate = Gate::default();
    // The day's quota is spent, so every later request is refused.
    for n in 0..DAILY_MAX as i64 {
        assert!(admitted(&gate, "agent:dot", START + n * MIN_GAP_MS));
    }
    gate.unsent("agent:dot", 2_500_000);
    let mut counts = Vec::new();
    for hour in 0..4 {
        let now = START + DAILY_MAX as i64 * MIN_GAP_MS + hour * SUMMARY_WINDOW_MS;
        match gate.decide("agent:dot", now) {
            Decision::Refuse(Some((_, n))) => counts.push(n),
            other => panic!("{other:?}"),
        }
    }
    // Capped at the reader's bound, and nothing lost: 2,500,000 plus 4 refusals.
    assert_eq!(counts, [SUMMARY_COUNT_MAX, SUMMARY_COUNT_MAX, 500_003, 1]);
}

/// Pages the hub file's audit log through `brigade.fleet_hub_audit.page`,
/// the reader behind `GET /audit`, and returns its rows as JSON.
fn python_audit_page(hub: &std::path::Path) -> Json {
    let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
    let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let out = std::process::Command::new(python)
        .env("PYTHONPATH", src)
        .args([
            "-c",
            "import json, sqlite3, sys\n\
             from brigade import fleet_hub_audit\n\
             rows = fleet_hub_audit.page(sqlite3.connect(sys.argv[1]), limit=500)\n\
             print(json.dumps(rows))",
        ])
        .arg(hub)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}

#[tokio::test]
async fn a_summary_past_a_million_refusals_still_pages_through_the_audit_reader() {
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    assert_eq!(stats(&h, AGENT_TOKEN, ask("fleet")).await.status, 200);
    // A flood of 1,500,000 refusals already merged, then one more.
    h.store
        .as_ref()
        .unwrap()
        .read_stats_gate
        .unsent("agent:dot", 1_500_000);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 429);
    h.set_clock(h.now() + SUMMARY_WINDOW_MS);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 200);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 429);
    let rows = python_audit_page(&h.hub);
    let summaries: Vec<i64> = rows
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["action"] == REFUSED_ACTION)
        .map(|r| r["detail"]["count"].as_i64().unwrap())
        .collect();
    assert_eq!(summaries, [1_000_000, 500_002]);
}

#[tokio::test]
async fn every_admitted_request_writes_one_audit_row_and_no_memory_event() {
    let h = hub().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    seed(&h, "fleet", "alpha", &[(0, 1, 0)]);
    seed(&h, "fleet", "beta", &[(0, 1, 0)]);
    let events = h.events();
    let before = audit_rows(&h).len();
    let mut statuses = Vec::new();
    // Served, refused for grants, invalid, rate limited (its summary), and an admin read.
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
        ("agent:dot", json!({"count":1,"status":429})),
        ("node-1", json!({"scope":"fleet","count":0,"status":403})),
        ("admin", json!({"scope":"fleet","count":2,"status":200})),
    ];
    for (n, ((actor, action, subject, detail), (who, want))) in
        rows[before..].iter().zip(expected).enumerate()
    {
        let kind = if n == 3 { REFUSED_ACTION } else { AUDIT_ACTION };
        assert_eq!(
            (actor.as_str(), action.as_str(), subject.as_str()),
            (who, kind, who)
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

#[tokio::test]
async fn a_refusal_flood_keeps_audit_rows_bounded_and_other_writers_working() {
    let (h, writer) = hub_and_writer().await;
    grant(&h, "agent:dot", "fleet", &["read", "read_stats"]).await;
    // A principal without read_stats floods too.
    let before = audit_rows(&h).len();
    assert_eq!(stats(&h, AGENT_TOKEN, ask("fleet")).await.status, 200);
    assert_eq!(h.call(NODE_TOKEN, ask("fleet")).await.status, 403);
    for n in 0..2000 {
        assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 429);
        assert_eq!(h.call(NODE_TOKEN, ask("fleet")).await.status, 429);
        if n % 250 == 0 {
            assert!(other_family(&writer).await, "other family refused at {n}");
        }
    }
    // One row per admitted request and one summary per principal.
    let rows = audit_rows(&h);
    let added: Vec<_> = rows[before..]
        .iter()
        .map(|(actor, action, _, detail)| {
            (actor.as_str(), action.as_str(), detail["count"].clone())
        })
        .collect();
    assert_eq!(
        added,
        [
            ("agent:dot", AUDIT_ACTION, json!(0)),
            ("node-1", AUDIT_ACTION, json!(0)),
            ("agent:dot", REFUSED_ACTION, json!(1)),
            ("node-1", REFUSED_ACTION, json!(1)),
        ]
    );
    // An hour on, the merged count arrives in the next summary.
    h.set_clock(h.now() + SUMMARY_WINDOW_MS);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 200);
    assert_eq!(h.call(AGENT_TOKEN, ask("fleet")).await.status, 429);
    let last = audit_rows(&h).pop().unwrap();
    assert_eq!(
        (last.1.as_str(), last.3),
        (REFUSED_ACTION, json!({"count":2000,"status":429}))
    );
    assert!(other_family(&writer).await);
}

#[tokio::test]
async fn audit_rows_never_hold_more_than_their_writer_slots() {
    let (h, writer) = hub_and_writer().await;
    let audit = Arc::new(HubAudit::new(writer.clone()));
    // Hold the hub file's write lock so queued audit rows cannot finish.
    let lock = Connection::open(&h.hub).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    let now = h.now();
    let joined: Vec<_> = (0..IN_FLIGHT + 8)
        .map(|n| {
            let audit = audit.clone();
            tokio::spawn(async move {
                audit
                    .append(Audit::refused(format!("agent:p{n}"), 1, now))
                    .await
            })
        })
        .collect();
    tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    // Every row past the slots was refused at once, without queueing.
    assert_eq!(audit.available(), 0);
    let refused = joined.iter().filter(|h| h.is_finished()).count();
    assert_eq!(refused, 8);
    lock.execute_batch("COMMIT").unwrap();
    let mut written = 0;
    for handle in joined {
        written += handle.await.unwrap() as usize;
    }
    assert_eq!(written, IN_FLIGHT);
    assert_eq!(audit.available(), IN_FLIGHT);
    assert!(other_family(&writer).await);
}
