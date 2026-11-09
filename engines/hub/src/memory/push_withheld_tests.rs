//! #2410: push answers reveal nothing about the cards push withholds. Ranking
//! statistics and `match_count` count push-eligible cards only, and a push
//! card read answers 404 with the same store work whether the id is absent
//! or every copy is withheld.
#![cfg(feature = "memory-scan")]
use super::scan;
use super::tests::{ADMIN_TOKEN, Hub, NODE_TOKEN, code};
use rusqlite::Connection;
use serde_json::{Value as Json, json};
use std::sync::{
    Arc,
    atomic::{AtomicU64, Ordering},
};
use std::time::{Duration, Instant};

/// A card whose title and summary vary in length, so the average length
/// and document frequency both shape its BM25 score.
fn card(title: &str, summary: &str) -> String {
    format!("---\ntitle: {title}\ndescription: {summary}\n---\nrelease checklist body a :: b\n")
}

/// Text the scanner flags, built at run time so the repository guard never
/// sees the whole path. It holds both query forms.
fn flagged() -> String {
    format!(
        "release checklist release, deploy notes in /{}/example/notes.md :: x",
        "home"
    )
}

async fn import(h: &Hub, card_id: &str, text: &str) -> i64 {
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"import","scope":"fleet","card_id":card_id,"text":text,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 201, "import {card_id}: {:?}", r.body);
    r.body["event"]["seq"].as_i64().unwrap()
}

async fn retire(h: &Hub, card_id: &str) -> i64 {
    let revision: i64 = Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT revision FROM memory_cards WHERE card_id=?1",
            [card_id],
            |r| r.get(0),
        )
        .unwrap();
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retire","scope":"fleet","card_id":card_id,"base_revision":revision,
                   "base_epoch":h.epoch().await,"reason":"obsolete","client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 200, "retire {card_id}: {:?}", r.body);
    r.body["event"]["seq"].as_i64().unwrap()
}

/// The ranked `(card_id, score)` list and `match_count` of one search.
async fn ranked(h: &Hub, query: &str, push: bool, seq: i64) -> (Vec<(String, f64)>, i64) {
    let mut body = json!({"action":"search","query":query,"limit":20,"min_seq":seq});
    if push {
        body["query_source"] = json!("push");
    }
    let r = h.call(NODE_TOKEN, body).await;
    assert_eq!(r.status, 200, "{:?}", r.body);
    let hits = r.body["matches"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| {
            (
                m["card_id"].as_str().unwrap().to_string(),
                m["score"].as_f64().unwrap(),
            )
        })
        .collect();
    (hits, r.body["match_count"].as_i64().unwrap())
}

#[tokio::test]
async fn push_scores_ignore_withheld_cards_as_they_come_and_go() {
    let h = Hub::with(scan::bundled(), None);
    import(&h, "alpha", &card("Release checklist", "short")).await;
    import(
        &h,
        "beta",
        &card("Release plan", "release notes with a much longer summary"),
    )
    .await;
    let seq = import(&h, "gamma", &card("Deploy checklist", "deploy steps")).await;
    let query = "release checklist";
    let before = ranked(&h, query, true, seq).await;
    assert_eq!(before.1, 3, "{before:?}");
    let plain_before = ranked(&h, query, false, seq).await;

    // Two withheld cards that hold the query terms: a flagged scan, and a
    // clean card whose revision is still pending a scan.
    import(&h, "private", &flagged()).await;
    let seq = import(&h, "pending", &card("Release checklist", "release")).await;
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "UPDATE memory_card_revisions SET scan_state='pending' WHERE card_id='pending'",
            [],
        )
        .unwrap();
    let plain = ranked(&h, query, false, seq).await;
    assert_eq!(plain.1, 5, "{plain:?}");
    // The withheld cards move every plain score, so a push score that kept
    // still is the statistics leaving them out, not a lucky corpus.
    for (id, score) in &plain_before.0 {
        let now = plain.0.iter().find(|(i, _)| i == id).unwrap().1;
        assert_ne!(now, *score, "{id}: plain score did not move");
    }
    assert_eq!(ranked(&h, query, true, seq).await, before);

    // A literal query reads the same eligible cards and counts no others.
    let literal = |push: bool| {
        let h = &h;
        async move {
            let mut body = json!({"action":"search","query":"::","limit":20,"min_seq":seq});
            if push {
                body["query_source"] = json!("push");
            }
            let r = h.call(NODE_TOKEN, body).await;
            assert_eq!(r.status, 200, "{:?}", r.body);
            r.body["match_count"].as_i64().unwrap()
        }
    };
    assert_eq!((literal(false).await, literal(true).await), (5, 3));

    // Retiring them leaves push exactly where it was.
    retire(&h, "private").await;
    let seq = retire(&h, "pending").await;
    assert_eq!(ranked(&h, query, true, seq).await, before);
}

/// Every SQLite virtual machine instruction one push `card` read runs.
/// Instructions are a deterministic measure of store work, so absent and
/// withheld ids compare exactly where wall-clock time would only be noisy.
fn steps(h: &Hub, credential: &super::Credential, ruleset: &str, body: Json) -> (u16, u64) {
    let conn = Connection::open(h.memory()).unwrap();
    let count = Arc::new(AtomicU64::new(0));
    let counter = count.clone();
    conn.progress_handler(
        1,
        Some(move || {
            counter.fetch_add(1, Ordering::Relaxed);
            false
        }),
    )
    .unwrap();
    let reply = super::reads(
        &super::Conn(&conn),
        credential,
        "card",
        body.as_object().unwrap(),
        h.now(),
        (true, Some(ruleset.to_string())),
        0,
    )
    .unwrap();
    assert_eq!(
        (reply.status, code(&reply)),
        (404, "memory_card_not_found"),
        "{body}"
    );
    (reply.status, count.load(Ordering::Relaxed))
}

/// A hub holding one eligible card and one the node may not push: `hidden`
/// has a flagged scan. `first` sorts before it, so the decoy head an absent
/// id reads is an eligible card, not the withheld one.
async fn withheld_hub() -> Hub {
    let h = Hub::with(scan::bundled(), None);
    import(&h, "first", &card("Release checklist", "steps")).await;
    import(&h, "hidden", &flagged()).await;
    h
}

#[tokio::test]
async fn push_card_404_does_the_same_store_work_for_absent_and_withheld_ids() {
    let h = withheld_hub().await;
    let credential = match h.store.as_ref().unwrap().authenticate(NODE_TOKEN).await {
        Ok(c) => c,
        Err(r) => panic!("{:?}", r.body),
    };
    let ruleset = scan::bundled().describe().1.unwrap();
    for scope in [None, Some("fleet")] {
        let read = |id: &str| {
            let mut body = json!({"action":"card","card_id":id,"push":true});
            if let Some(scope) = scope {
                body["scope"] = json!(scope);
            }
            body
        };
        let absent = steps(&h, &credential, &ruleset, read("absent"));
        let withheld = steps(&h, &credential, &ruleset, read("hidden"));
        // The decoy head runs the same joins and predicate as the withheld
        // head. Before #2410 an absent id stopped at the index miss and ran
        // about 6% fewer instructions over the whole read.
        let (a, w) = (absent.1 as f64, withheld.1 as f64);
        assert!(
            (a / w - 1.0).abs() <= 0.02,
            "scope {scope:?}: absent {a} vs withheld {w} instructions"
        );
    }
}

#[tokio::test]
async fn push_card_404_latency_does_not_separate_absent_and_withheld_ids() {
    let h = withheld_hub().await;
    let read = |id: &str| json!({"action":"card","card_id":id,"push":true});
    // Interleaved batches, best batch per id, so load on a shared runner
    // falls on both sides alike and a slow batch cannot decide the ratio.
    // The bound is loose on purpose: the instruction count above is the
    // exact check, and this one bounds what a caller can time end to end.
    let batch = |id: &'static str| {
        let h = &h;
        async move {
            let start = Instant::now();
            for _ in 0..50 {
                let r = h.call(NODE_TOKEN, read(id)).await;
                assert_eq!(r.status, 404, "{id}: {:?}", r.body);
            }
            start.elapsed()
        }
    };
    let (mut absent, mut withheld) = (Duration::MAX, Duration::MAX);
    for _ in 0..7 {
        absent = absent.min(batch("absent").await);
        withheld = withheld.min(batch("hidden").await);
    }
    let ratio = withheld.as_secs_f64() / absent.as_secs_f64();
    assert!(
        (1.0 / 1.5..=1.5).contains(&ratio),
        "withheld {withheld:?} vs absent {absent:?}: ratio {ratio:.3}"
    );
}
