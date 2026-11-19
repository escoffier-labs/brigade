//! A principal whose reflect-controlled command exhausts its budget is refused
//! before the writer for `auto_proposal_ms`, whichever of the five budgeted
//! commands it sent.
use super::attestation_tests::{auto_request, ready};
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{hardening::*, proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const COOLDOWN: &str = "memory_auto_apply_cooldown";
const BUDGET: &str = "memory_auto_apply_budget_exceeded";
const BURN: &str = "CREATE TRIGGER cooldown_burn BEFORE INSERT ON memory_idempotency BEGIN SELECT (WITH RECURSIVE work(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM work WHERE n<100000000) SELECT sum(n) FROM work); END;";

fn count(db: &Connection, table: &str) -> i64 {
    db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
        .unwrap()
}
struct Fixture {
    h: Hub,
    /// The pending proposal `auto_apply` runs on, or the applied one `rollback` reverses.
    proposal: Json,
    /// Revision of card `b`, which `propose` targets.
    b: i64,
}
/// `rollback` needs an applied proposal, so it applies the ready one first with
/// the default limits, before any budget is tightened.
async fn fixture(applied: bool) -> Fixture {
    let (h, proposal) = ready().await;
    let b = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, OLD).await;
    if applied {
        super::retrieval_eval_tests::record_current(&h).await;
        let r = h.call(ADMIN_TOKEN, auto_request(&h, &proposal).await).await;
        assert_eq!(r.body["state"], "applied", "{r:?}");
        h.set_clock(h.now() + 1);
    }
    Fixture {
        h,
        proposal,
        b: rev(&b),
    }
}
impl Fixture {
    fn db(&self) -> Connection {
        Connection::open(self.h.memory()).unwrap()
    }
    fn limits(&mut self, limits: Json) {
        self.h
            .store
            .as_mut()
            .unwrap()
            .configure_retrieval(&json!({ "proposal_limits": limits }))
            .unwrap();
    }
    /// A fresh request of `kind`, so each call has its own event id and epoch.
    async fn request(&self, kind: &str) -> Json {
        let h = &self.h;
        let epoch = h.epoch().await;
        match kind {
            "auto_apply" => auto_request(h, &self.proposal).await,
            "rollback" => {
                json!({"action":"rollback","proposal_id":self.proposal,"base_epoch":epoch,"client_event_id":h.id()})
            }
            "propose" => {
                json!({"action":"propose","base_epoch":epoch,"ops":[op(FIXTURE_SCOPE, "b", self.b)],"client_event_id":h.id()})
            }
            "compensation_watch" => {
                json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":epoch,"client_event_id":h.id()})
            }
            _ => unreachable!(),
        }
    }
    async fn send(&self, token: &str, kind: &str) -> Reply {
        self.h.call(token, self.request(kind).await).await
    }
    /// Holds the memory writer lock, so a command that took the writer would
    /// wait on it and could not answer with the cooldown refusal.
    async fn refused_without_the_writer(&self, token: &str, kind: &str) -> Reply {
        let held = self.db();
        held.execute_batch("BEGIN IMMEDIATE").unwrap();
        let reply = self.send(token, kind).await;
        held.execute_batch("ROLLBACK").unwrap();
        reply
    }
}

/// Drives one kind through exhaustion, cooldown, a second principal, ordinary
/// writes and expiry. `exhaust` tightens the budget so the first command fails.
async fn cools_down(kind: &str, mut f: Fixture, exhaust: Json) {
    f.limits(exhaust);
    f.db().execute_batch(BURN).unwrap();
    let t0 = f.h.now();
    let events = (f.h.events(), count(&f.db(), "memory_idempotency"));
    let first = f.send(ADMIN_TOKEN, kind).await;
    assert_eq!(
        (first.status, code(&first)),
        (503, BUDGET),
        "{kind}: {first:?}"
    );

    // The next request is refused before the writer, writes nothing and spends
    // no rate slot, whichever kind it is.
    for other in ["auto_apply", "propose", "compensation_watch", "rollback"] {
        let r = f.refused_without_the_writer(ADMIN_TOKEN, other).await;
        assert_eq!((r.status, code(&r)), (429, COOLDOWN), "{kind} then {other}");
    }
    assert_eq!(
        events,
        (f.h.events(), count(&f.db(), "memory_idempotency")),
        "{kind}"
    );

    // Another principal is not affected: it reaches the work, not the cooldown.
    let node = f.send(NODE_TOKEN, kind).await;
    assert_ne!(code(&node), COOLDOWN, "{kind}: {node:?}");

    // Work outside the five commands stays open to the cooled-down principal.
    f.db().execute_batch("DROP TRIGGER cooldown_burn").unwrap();
    let put =
        f.h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "other", None, OLD)
            .await;
    assert!(rev(&put) > 0, "{kind}");
    f.db().execute_batch(BURN).unwrap();

    // The cooldown is `auto_proposal_ms`: still refused one millisecond early.
    f.h.set_clock(t0 + 59_999);
    let early = f.refused_without_the_writer(ADMIN_TOKEN, kind).await;
    assert_eq!((early.status, code(&early)), (429, COOLDOWN), "{kind}");
    f.h.set_clock(t0 + 60_000);
    let after = f.send(ADMIN_TOKEN, kind).await;
    assert_eq!(
        (after.status, code(&after)),
        (503, BUDGET),
        "{kind}: {after:?}"
    );
}

#[tokio::test]
async fn auto_apply_writer_budget_exhaustion_starts_a_cooldown() {
    let f = fixture(false).await;
    super::retrieval_eval_tests::record_current(&f.h).await;
    cools_down("auto_apply", f, json!({"lock_ms": 1})).await;
}
#[tokio::test]
async fn auto_apply_diagnostic_exhaustion_starts_a_cooldown_ahead_of_the_rate_gate() {
    let mut f = fixture(false).await;
    super::retrieval_eval_tests::record_current(&f.h).await;
    f.limits(json!({"diagnostic_rows": 1}));
    let first = f.send(ADMIN_TOKEN, "auto_apply").await;
    assert_eq!((first.status, code(&first)), (503, BUDGET), "{first:?}");
    // That attempt spent the principal's rate slot, so a rate gate that ran
    // first would answer `memory_auto_apply_rate_limited`.
    let second = f
        .refused_without_the_writer(ADMIN_TOKEN, "auto_apply")
        .await;
    assert_eq!(
        (second.status, code(&second)),
        (429, COOLDOWN),
        "{second:?}"
    );
    f.h.set_clock(f.h.now() + 60_000);
    let third = f.send(ADMIN_TOKEN, "auto_apply").await;
    assert_eq!((third.status, code(&third)), (503, BUDGET), "{third:?}");
}
#[tokio::test]
async fn propose_budget_exhaustion_starts_a_cooldown() {
    cools_down("propose", fixture(false).await, json!({"lock_ms": 1})).await;
}
#[tokio::test]
async fn compensation_watch_budget_exhaustion_starts_a_cooldown() {
    cools_down(
        "compensation_watch",
        fixture(false).await,
        json!({"lock_ms": 1}),
    )
    .await;
}
#[tokio::test]
async fn rollback_budget_exhaustion_starts_a_cooldown() {
    cools_down("rollback", fixture(true).await, json!({"lock_ms": 1})).await;
}

#[tokio::test]
async fn work_inside_its_budget_never_starts_a_cooldown() {
    let f = fixture(false).await;
    for _ in 0..3 {
        // Each proposal is the same operation, so only the first can open; the
        // refusals after it are ordinary ones, never the cooldown.
        let r = f.send(ADMIN_TOKEN, "propose").await;
        assert_ne!(code(&r), COOLDOWN, "{r:?}");
        let r = f.send(ADMIN_TOKEN, "compensation_watch").await;
        assert_ne!(code(&r), COOLDOWN, "{r:?}");
    }
    let first = f.send(ADMIN_TOKEN, "auto_apply").await;
    assert_eq!(first.status, 200, "{first:?}");
    // A second attempt inside the interval is rate limited, not cooled down.
    let again = f.send(ADMIN_TOKEN, "auto_apply").await;
    assert_eq!(
        (again.status, code(&again)),
        (429, "memory_auto_apply_rate_limited")
    );
}

#[test]
fn cooldown_expires_and_fails_closed_when_its_map_is_full() {
    let limits = Limits::default();
    let mut cooldown = Cooldown::default();
    assert!(!cooldown.active("one", 1_000, &limits));
    cooldown.start("one", 1_000);
    assert!(cooldown.active("one", 1_000, &limits));
    assert!(!cooldown.active("two", 1_000, &limits));
    assert!(cooldown.active("one", 60_999, &limits));
    assert!(!cooldown.active("one", 61_000, &limits));
    // A full map refuses a principal it cannot record, then recovers on expiry.
    for i in 0..4096 {
        cooldown.start(&format!("p{i}"), 100_000);
    }
    assert!(cooldown.active("new", 100_000, &limits));
    assert!(!cooldown.active("new", 160_000, &limits));
}
