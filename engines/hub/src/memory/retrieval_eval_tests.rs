//! S19e-2 uses the real admin record action and the production writer.
use super::{attestation_tests::*, proposal_tests::*, tests::*, *};
use rusqlite::Connection;

pub(super) async fn gate_report(h: &Hub) -> Json {
    let search = h.call(ADMIN_TOKEN, json!({"action":"search","query":"gate","mode":"lexical","eval":true,"scopes":["fleet"],"min_seq":h.events()})).await;
    assert_eq!(search.status, 200, "{search:?}");
    let mut binding = search.body.clone();
    binding["mode"] = json!("lexical");
    binding["scope"] = json!("fleet");
    binding["corpus_identity"] = search.body["corpus_identity"]["fleet"].clone();
    binding["ranking"] = json!("raw");
    binding["embedder_id"] = Json::Null;
    let metrics =
        json!({"query_count":1,"hit_rate":0.5,"mrr":0.5,"precision_at_k":0.5,"recall_at_k":0.5});
    let baseline = json!({"available":true,"overall":metrics,"by_category":{"paraphrase":metrics},"ranking_sha256":ids::sha256_hex(b"[[\"a\"]]"),"queries":[{"id":"q","ranked_candidates":["a"]}]});
    let mut candidate = baseline.clone();
    candidate["binding"] = binding;
    json!({"evaluation_id":h.id(),"k":5,"fixture_digest":ids::sha256_hex(b"fixture"),
        "gate":{"name":"R0","passed":true,"failures":[],"private_qualification":"not_evaluated"},
        "adapters":{"bm25":baseline,"hub-lexical":candidate}})
}
pub(super) async fn record(h: &Hub, report: &Json) -> Reply {
    let raw = ids::canonical(report);
    h.call(ADMIN_TOKEN,json!({"action":"retrieval_eval.record","report_json":raw,"report_sha256":ids::sha256_hex(raw.as_bytes()),"client_event_id":h.id()})).await
}
pub(super) async fn record_current(h: &Hub) {
    h.set_clock(h.now() + 1);
    let r = record(h, &gate_report(h).await).await;
    assert_eq!(r.status, 201, "{r:?}");
}
#[tokio::test]
async fn retrieval_eval_real_record_opens_the_last_gate() {
    let (h, id) = ready().await;
    record_current(&h).await;
    let admin_report = report(&h, &id).await;
    assert!(!admin_report.body["retrieval_eval_record"].is_null());
    let reader = h
        .call(NODE_TOKEN, json!({"action":"preflight","proposal_id":id}))
        .await;
    assert_eq!(reader.status, 200);
    assert!(reader.body["retrieval_eval_record"].is_null());
    let applied = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_eq!(card(&h, "fleet", "a").await["trust"], "reviewed");
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM memory_retrieval_evals", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[tokio::test]
async fn retrieval_eval_admin_digest_append_only_and_latest_failure() {
    let (h, id) = ready().await;
    let report = gate_report(&h).await;
    let raw = ids::canonical(&report);
    let request = json!({"action":"retrieval_eval.record","report_json":raw,"report_sha256":ids::sha256_hex(raw.as_bytes()),"client_event_id":h.id()});
    let before = h.events();
    for token in [NODE_TOKEN, AGENT_TOKEN] {
        assert_eq!(h.call(token, request.clone()).await.status, 403);
    }
    let mut bad = request.clone();
    bad["report_sha256"] = json!(ids::sha256_hex(b"wrong"));
    assert_eq!(
        code(&h.call(ADMIN_TOKEN, bad).await),
        "memory_retrieval_eval_digest_mismatch"
    );
    assert_eq!(h.events(), before);
    let first = h.call(ADMIN_TOKEN, request.clone()).await;
    assert_eq!(first.status, 201, "{first:?}");
    assert_eq!(h.call(ADMIN_TOKEN, request).await, first);
    let db = Connection::open(h.memory()).unwrap();
    assert!(
        db.execute("UPDATE memory_retrieval_evals SET mode='hybrid'", [])
            .is_err()
    );
    assert!(
        db.execute("DELETE FROM memory_retrieval_evals", [])
            .is_err()
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_events WHERE kind='retrieval_eval.record'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    h.set_clock(h.now() + 1);
    let mut failed = report.clone();
    failed["evaluation_id"] = json!(h.id());
    failed["gate"]["passed"] = json!(false);
    failed["gate"]["failures"] = json!(["lexical order differs"]);
    failed["adapters"]["hub-lexical"]["binding"]["ranking"] = json!("default");
    assert_eq!(record(&h, &failed).await.status, 201);
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_failed")
    );
    assert_eq!(r.body["state"], "pending");
    let stored = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    assert_eq!(stored["state"], r.body["state"]);
}

#[tokio::test]
async fn retrieval_eval_bindings_fail_closed_and_are_rechecked_after_preparation() {
    for (field, value, expected) in [
        (
            "index_format",
            json!("other"),
            "retrieval_eval_index_format_mismatch",
        ),
        (
            "scorer_version",
            json!("other"),
            "retrieval_eval_scorer_mismatch",
        ),
        (
            "policy_revision",
            json!(2),
            "retrieval_eval_policy_revision_mismatch",
        ),
        (
            "policy_digest",
            json!(format!("sha256:{}", ids::sha256_hex(b"other"))),
            "retrieval_eval_policy_digest_mismatch",
        ),
        ("indexed_seq", json!(0), "retrieval_eval_seq_stale"),
        ("indexed_seq", json!(999999), "retrieval_eval_seq_future"),
        (
            "log_epoch",
            json!(ids::mint(1000)),
            "retrieval_eval_epoch_mismatch",
        ),
        (
            "scope",
            json!("collection:other"),
            "retrieval_eval_scope_mismatch",
        ),
        (
            "rrf_weights",
            json!({"lexical":2.0,"dense":1.0}),
            "retrieval_eval_weights_mismatch",
        ),
        (
            "corpus_identity",
            json!({"live_count":1,"digest":ids::sha256_hex(b"wrong")}),
            "retrieval_eval_corpus_mismatch",
        ),
    ] {
        let (h, id) = ready().await;
        let mut report = gate_report(&h).await;
        report["adapters"]["hub-lexical"]["binding"][field] = value;
        assert_eq!(record(&h, &report).await.status, 201, "{field}");
        let before = bytes_snapshot(&h);
        let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert!(
            r.body["preflight"]["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["code"] == expected),
            "{field}: {r:?}"
        );
        assert_eq!(bytes_snapshot(&h), before);
    }
    let (h, id) = ready().await;
    record_current(&h).await;
    let store = h.store.as_ref().unwrap();
    let who = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let request = auto_request(&h, &id).await;
    let db = Connection::open(h.memory()).unwrap();
    let mut p = store
        .prepare(&who, "auto_apply", request.as_object().unwrap(), &request)
        .unwrap();
    p.action = ops::Action::Proposal(
        proposals::prepare(
            &crate::writer::worker::Tx(&db),
            &who,
            request,
            clean().as_ref(),
        )
        .unwrap(),
    );
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET digest=?1",
            [ids::sha256_hex(b"changed")],
        )
        .unwrap();
    let r = store.write(p).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_policy_digest_mismatch")
    );
}

#[tokio::test]
async fn retrieval_eval_rejects_malformed_or_inconsistent_gate_reports() {
    let (h, _) = ready().await;
    for (field, value) in [
        ("indexed_seq", json!(true)),
        ("policy_revision", json!(0)),
        ("policy_digest", json!("bad")),
        ("mode", json!("auto")),
        ("index_format", Json::Null),
        ("scorer_version", json!("")),
        ("embedder_id", json!("unused")),
        ("log_epoch", json!("bad")),
        ("scope", json!("*")),
        ("corpus_identity", json!({})),
        ("rrf_weights", json!({})),
        ("ranking", json!("default")),
    ] {
        let mut report = gate_report(&h).await;
        report["adapters"]["hub-lexical"]["binding"][field] = value;
        assert_eq!(record(&h, &report).await.status, 422, "{field}");
    }
    for field in ["fixture_digest", "evaluation_id"] {
        let mut report = gate_report(&h).await;
        report[field] = Json::Null;
        assert_eq!(record(&h, &report).await.status, 422, "{field}");
    }
    let mut report = gate_report(&h).await;
    report["gate"]["failures"] = json!(["failed"]);
    assert_eq!(record(&h, &report).await.status, 422);
}

#[tokio::test]
async fn retrieval_eval_expiry_cannot_be_refreshed_by_recording_old_bytes_and_replay_needs_new_id()
{
    let (h, id) = ready().await;
    let report = gate_report(&h).await;
    record(&h, &report).await;
    let request = auto_request(&h, &id).await;
    h.set_clock(h.now() + retrieval_eval::MAX_AGE_MS + 1);
    let refused = record(&h, &report).await;
    assert_eq!(refused.status, 409);
    assert_eq!(code(&refused), "memory_retrieval_eval_superseded");
    let held = h.call(ADMIN_TOKEN, request.clone()).await;
    assert!(
        held.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_expired")
    );
    record_current(&h).await;
    assert_eq!(h.call(ADMIN_TOKEN, request).await, held);
    // Evidence expired too, so refresh it using new ids rather than changing immutable rows.
    assert_eq!(
        h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await.body["state"],
        "pending"
    );
}

#[tokio::test]
async fn retrieval_eval_changed_corpus_and_active_hybrid_cannot_use_lexical_record() {
    let (mut h, id) = ready().await;
    record_current(&h).await;
    h.put(ADMIN_TOKEN, "fleet", "b", None, "new corpus member")
        .await;
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_seq_stale")
    );
    // Configure hybrid through the production runtime, with no endpoint access.
    h.store.as_mut().unwrap().configure_retrieval(&json!({"retrieval":"hybrid","embedder":{"backend":"remote","pull":true,"name":"fixture","shape":"api/embed","input_chars":1000,"query_prefix_version":1,"model_digest":ids::sha256_hex(b"model"),"dimensions":4}})).unwrap();
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_unbound")
    );
}

#[tokio::test]
async fn retrieval_eval_hybrid_identity_is_checked_and_records_survive_restart() {
    let (mut h, id) = ready().await;
    let mut r = gate_report(&h).await;
    let config = json!({"retrieval":"hybrid","embedder":{"backend":"remote","pull":true,"shape":"api/embed","name":"fixture","model_digest":ids::sha256_hex(b"model"),"dimensions":4,"input_chars":1000,"query_prefix_version":1}});
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&config)
        .unwrap();
    let embedder = h.store.as_ref().unwrap().dense.active_id().unwrap();
    let mut adapter = r["adapters"]["hub-lexical"].clone();
    for block in ["overall", "by_category"] {
        let metrics = json!({"query_count":1,"hit_rate":0.75,"mrr":0.75,"precision_at_k":0.75,"recall_at_k":0.75});
        adapter[block] = if block == "overall" {
            metrics.clone()
        } else {
            json!({"paraphrase":metrics})
        };
    }
    adapter["binding"]["mode"] = json!("hybrid");
    adapter["binding"]["ranking"] = json!("default");
    adapter["binding"]["embedder_id"] = json!(ids::sha256_hex(b"wrong-embedder"));
    r["adapters"].as_object_mut().unwrap().remove("hub-lexical");
    r["adapters"]["hub-hybrid"] = adapter;
    r["gate"]["name"] = json!("R1-public");
    assert_eq!(record(&h, &r).await.status, 201);
    let attempt = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        attempt.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_embedder_mismatch")
    );
    h.set_clock(h.now() + 1);
    r["evaluation_id"] = json!(h.id());
    r["adapters"]["hub-hybrid"]["binding"]["embedder_id"] = json!(embedder);
    assert_eq!(record(&h, &r).await.status, 201);
    h.store.take();
    h.open(clean(), None);
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&config)
        .unwrap();
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(r.body["state"], "applied", "{r:?}");
}

#[tokio::test]
async fn retrieval_eval_late_output_scan_hold_reports_only_persisted_state() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct LateScan {
        after: String,
        count: AtomicUsize,
    }
    impl Scanner for LateScan {
        fn scan(&self, text: &str) -> Scan {
            if text == self.after && self.count.fetch_add(1, Ordering::SeqCst) >= 3 {
                Scan::Unavailable
            } else {
                Scan::Clean {
                    ruleset: "test-v7".into(),
                }
            }
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some("test-v7".into()))
        }
    }
    let (mut h, id) = ready().await;
    record_current(&h).await;
    let db = Connection::open(h.memory()).unwrap();
    let before = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    let after = before["cards"][0]["after_text"]
        .as_str()
        .unwrap()
        .to_string();
    h.store.take();
    h.open(
        Arc::new(LateScan {
            after,
            count: AtomicUsize::new(0),
        }),
        None,
    );
    let request = auto_request(&h, &id).await;
    let r = h.call(ADMIN_TOKEN, request.clone()).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(r.body["reason"], "scan_not_clean", "{r:?}");
    assert!(
        preflight::all_satisfied(&r.body["preflight"]),
        "late scan must occur after gates"
    );
    let stored = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    assert_eq!(stored, before);
    assert_eq!(r.body["state"], stored["state"]);
    assert_eq!(h.call(ADMIN_TOKEN, request).await, r);
}

#[tokio::test]
async fn retrieval_eval_late_stale_reply_never_claims_an_unpersisted_transition() {
    use crate::writer::{ReadTx, WriteTx, worker::Tx};
    use rusqlite::types::Value;
    use std::cell::Cell;
    // Real snapshots cannot move here. Inject an epoch change only at the
    // redundant final check to cover the defensive stale return in #2369.
    struct LateEpoch<'a> {
        inner: Tx<'a>,
        reads: Cell<usize>,
    }
    impl ReadTx for LateEpoch<'_> {
        fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
            if sql == "SELECT value FROM memory_meta WHERE key='log_epoch'" {
                self.reads.set(self.reads.get() + 1);
                if self.reads.get() == 4 {
                    return Ok(vec![vec![Value::Text("restored".into())]]);
                }
            }
            self.inner.query(sql, params)
        }
    }
    impl WriteTx for LateEpoch<'_> {
        fn execute(&self, sql: &str, params: &[Value]) -> rusqlite::Result<usize> {
            self.inner.execute(sql, params)
        }
    }
    let (h, id) = ready().await;
    record_current(&h).await;
    let store = h.store.as_ref().unwrap();
    let request = auto_request(&h, &id).await;
    let who = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let db = Connection::open(h.memory()).unwrap();
    let mut p = store
        .prepare(&who, "auto_apply", request.as_object().unwrap(), &request)
        .unwrap();
    p.action = ops::Action::Proposal(
        proposals::prepare(&Tx(&db), &who, request, clean().as_ref()).unwrap(),
    );
    let hub = Connection::open(&h.hub).unwrap();
    db.execute_batch("BEGIN IMMEDIATE").unwrap();
    hub.execute_batch("BEGIN IMMEDIATE").unwrap();
    let mut a = ops::Authority::with_credentials(&Tx(&hub), &digest(ADMIN_TOKEN)).unwrap();
    a.retrieval = Some(retrieval_eval::Live {
        indexed_seq: store.index.current().indexed_seq,
        embedder_id: None,
        weights: store.rrf.json(),
    });
    let tx = LateEpoch {
        inner: Tx(&db),
        reads: Cell::new(0),
    };
    let ops::Action::Proposal(cmd) = &p.action else {
        unreachable!()
    };
    let r = proposals::run(&tx, &p, cmd, &a, clean().as_ref()).unwrap();
    assert_eq!(tx.reads.get(), 4);
    assert!(preflight::all_satisfied(&r.body["preflight"]), "{r:?}");
    assert_eq!(r.body["reason"], "memory_epoch_changed");
    assert_eq!(r.body["state"], "pending");
    let stored = proposals::load(&Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    assert_eq!(r.body["state"], stored["state"]);
    db.execute_batch("ROLLBACK").unwrap();
    hub.execute_batch("ROLLBACK").unwrap();
}

#[tokio::test]
async fn retrieval_eval_review_fix_old_pass_cannot_supersede_new_failure() {
    let (h, id) = ready().await;
    let pass = gate_report(&h).await;
    assert_eq!(record(&h, &pass).await.status, 201);
    h.set_clock(h.now() + 1);
    let mut failed = gate_report(&h).await;
    failed["gate"]["passed"] = json!(false);
    failed["gate"]["failures"] = json!(["lexical order differs"]);
    assert_eq!(record(&h, &failed).await.status, 201);
    for older in [&pass, &failed] {
        let before = h.events();
        let refused = record(&h, older).await;
        assert_eq!(refused.status, 409, "{refused:?}");
        assert_eq!(code(&refused), "memory_retrieval_eval_superseded");
        assert_eq!(h.events(), before);
    }
    let attempt = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        attempt.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_failed")
    );
    assert_eq!(attempt.body["state"], "pending");
}

#[tokio::test]
async fn retrieval_eval_review_fix_admission_time_bounds() {
    for (offset, status, code_expected) in [
        (-86_400_001, 422, "memory_retrieval_eval_expired"),
        (-86_400_000, 201, ""),
        (300_001, 422, "memory_retrieval_eval_future"),
        (300_000, 201, ""),
    ] {
        let (h, id) = ready().await;
        let mut r = gate_report(&h).await;
        r["evaluation_id"] = json!(ids::mint(h.now() + offset));
        let before = h.events();
        let reply = record(&h, &r).await;
        assert_eq!(reply.status, status, "offset {offset}: {reply:?}");
        assert_eq!(code(&reply), code_expected);
        if status == 422 {
            assert_eq!(h.events(), before);
        }
        if offset > 0 && status == 201 {
            let held = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
            assert!(
                held.body["preflight"]["checks"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["code"] == "retrieval_eval_future")
            );
        }
    }
}

#[tokio::test]
async fn retrieval_eval_review_fix_rust_rechecks_r0_metrics() {
    for mutation in [
        "order",
        "score",
        "missing_metric",
        "missing_digest",
        "missing_queries",
        "bad_rows",
        "count",
        "category",
    ] {
        let (h, id) = ready().await;
        let mut r = gate_report(&h).await;
        let c = &mut r["adapters"]["hub-lexical"];
        match mutation {
            "order" => c["ranking_sha256"] = json!(ids::sha256_hex(b"different order")),
            "score" => c["overall"]["mrr"] = json!(0.4),
            "missing_metric" => {
                c["overall"].as_object_mut().unwrap().remove("mrr");
            }
            "missing_digest" => {
                c.as_object_mut().unwrap().remove("ranking_sha256");
            }
            "missing_queries" => {
                c["queries"] = json!([]);
            }
            "bad_rows" => c["queries"][0]["ranked_candidates"] = json!(["b"]),
            "count" => c["overall"]["query_count"] = json!(0),
            "category" => c["by_category"] = json!({}),
            _ => unreachable!(),
        }
        let reply = record(&h, &r).await;
        assert_eq!(reply.status, 201, "{mutation}: {reply:?}");
        assert_eq!(reply.body["passed"], false, "{mutation}: {reply:?}");
        let held = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert!(
            held.body["preflight"]["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["code"] == "retrieval_eval_failed")
        );
    }
}

#[tokio::test]
async fn retrieval_eval_review_fix_rust_rechecks_r1_thresholds() {
    for mutation in [
        "pass",
        "k",
        "overall",
        "category",
        "equal",
        "missing_category",
        "negative",
        "non_numeric",
        "over_one",
    ] {
        let (h, _) = ready().await;
        let mut r = gate_report(&h).await;
        let mut c = r["adapters"]["hub-lexical"].clone();
        c["binding"]["mode"] = json!("hybrid");
        c["binding"]["ranking"] = json!("default");
        c["binding"]["embedder_id"] = json!(ids::sha256_hex(b"model"));
        let metrics = json!({"query_count":1,"hit_rate":0.75,"mrr":0.75,"precision_at_k":0.75,"recall_at_k":0.75});
        c["overall"] = metrics.clone();
        c["by_category"] = json!({"paraphrase":metrics});
        r["adapters"].as_object_mut().unwrap().remove("hub-lexical");
        r["adapters"]["hub-hybrid"] = c;
        r["gate"]["name"] = json!("R1-public");
        let c = &mut r["adapters"]["hub-hybrid"];
        match mutation {
            "pass" => {}
            "k" => r["k"] = json!(10),
            "overall" => c["overall"]["mrr"] = json!(0.4),
            "category" => c["by_category"]["paraphrase"]["recall_at_k"] = json!(0.4),
            "equal" => {
                let metrics = json!({"query_count":1,"hit_rate":0.5,"mrr":0.5,"precision_at_k":0.5,"recall_at_k":0.5});
                c["overall"] = metrics.clone();
                c["by_category"] = json!({"paraphrase":metrics});
            }
            "missing_category" => c["by_category"] = json!({}),
            "negative" => c["overall"]["hit_rate"] = json!(-1),
            "non_numeric" => c["overall"]["hit_rate"] = json!(true),
            "over_one" => c["overall"]["hit_rate"] = json!(1.1),
            _ => unreachable!(),
        }
        let reply = record(&h, &r).await;
        assert_eq!(reply.status, 201, "{mutation}: {reply:?}");
        assert_eq!(
            reply.body["passed"],
            mutation == "pass",
            "{mutation}: {reply:?}"
        );
    }
}

#[tokio::test]
async fn retrieval_eval_review_fix_fixture_pin_matches_live_policy() {
    let (h, id) = ready().await;
    record_current(&h).await;
    change_policy(
        &h,
        "retrieval_eval",
        json!({"fixture_digest":ids::sha256_hex(b"other fixture")}),
    );
    let held = report(&h, &id).await;
    assert!(
        held.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_fixture_mismatch")
    );
    change_policy(
        &h,
        "retrieval_eval",
        json!({"fixture_digest":ids::sha256_hex(b"fixture")}),
    );
    assert!(
        report(&h, &id).await.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_satisfied")
    );
    for bad in [
        Json::Null,
        json!({"fixture_digest":"bad"}),
        json!({"unknown":true}),
    ] {
        change_policy(&h, "retrieval_eval", bad);
        assert!(
            report(&h, &id).await.body["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["code"] == "retrieval_eval_policy_invalid")
        );
    }
}

#[tokio::test]
async fn retrieval_eval_zero_head_tolerance_is_global_and_consumed_by_apply() {
    let (h, id) = ready().await;
    record_current(&h).await;
    h.put(ADMIN_TOKEN, "repo:other", "b", None, "other scope")
        .await;
    let held = report(&h, &id).await;
    assert!(
        held.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_seq_stale")
    );
    let db = Connection::open(h.memory()).unwrap();
    let body = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    let another=h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":body["ops"],"client_event_id":h.id()})).await;
    assert_eq!(another.status, 201);
    record_current(&h).await;
    let applied = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(applied.body["state"], "applied");
    let next = report(&h, &another.body["proposal_id"]).await;
    assert!(
        next.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_seq_stale")
    );
}

#[tokio::test]
async fn retrieval_eval_newest_record_per_mode_binds_only_its_scope() {
    let (h, id) = ready().await;
    h.put(ADMIN_TOKEN, "repo:other", "b", None, "other scope")
        .await;
    record_current(&h).await;
    assert!(
        report(&h, &id).await.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_satisfied")
    );
    h.set_clock(h.now() + 1);
    let mut other = gate_report(&h).await;
    let search=h.call(ADMIN_TOKEN,json!({"action":"search","query":"other","mode":"lexical","eval":true,"scopes":["repo:other"],"min_seq":h.events()})).await;
    other["adapters"]["hub-lexical"]["binding"]["scope"] = json!("repo:other");
    other["adapters"]["hub-lexical"]["binding"]["corpus_identity"] =
        search.body["corpus_identity"]["repo:other"].clone();
    assert_eq!(record(&h, &other).await.status, 201);
    assert!(
        report(&h, &id).await.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_scope_mismatch")
    );
}
