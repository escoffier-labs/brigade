//! S19e-2 uses the real admin record action and the production writer.
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{attestation_tests::*, proposal_tests::*, tests::*, *};
use rusqlite::Connection;

pub(super) async fn gate_report(h: &Hub) -> Json {
    let search = h.call(ADMIN_TOKEN, json!({"action":"search","query":"gate","mode":"lexical","eval":true,"scopes":[FIXTURE_SCOPE],"min_seq":h.events()})).await;
    assert_eq!(search.status, 200, "{search:?}");
    let mut binding = search.body.clone();
    binding["mode"] = json!("lexical");
    binding["scope"] = json!(FIXTURE_SCOPE);
    binding["corpus_identity"] = search.body["corpus_identity"][FIXTURE_SCOPE].clone();
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
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["trust"], "reviewed");
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
        ("indexed_seq", json!(0), "retrieval_eval_target_drifted"),
        ("indexed_seq", json!(999999), "retrieval_eval_seq_future"),
        (
            "log_epoch",
            json!(ids::mint(1000)),
            "retrieval_eval_epoch_mismatch",
        ),
        (
            "rrf_weights",
            json!({"lexical":2.0,"dense":1.0}),
            "retrieval_eval_weights_mismatch",
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
    h.set_clock(h.now() + 60_001);
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
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, "new corpus member")
        .await;
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_drift_exceeded")
    );
    h.set_clock(h.now() + 60_001);
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
                if self.reads.get() == 3 {
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
        applier: "admin",
        principal: super::ADMIN.into(),
        snapshot: Some(store.index.current()),
        indexed_seq: store.index.current().indexed_seq,
        embedder_id: None,
        weights: store.rrf.json(),
    });
    if let ops::Action::Proposal(cmd) = &mut p.action {
        let body = cmd.draft.as_mut().unwrap();
        let report = preflight::evaluate(&Tx(&db), body, &a, clean().as_ref(), h.now()).unwrap();
        assert!(preflight::all_satisfied(&report));
        body["_auto_snapshot"] = json!({"report":report,"token":hardening::token(&Tx(&db)).unwrap(),"authority":hardening::authority_digest(&a),"proposal_sha256":ids::sha256_hex(ids::canonical(body).as_bytes()),"now_ms":h.now()});
    }
    let tx = LateEpoch {
        inner: Tx(&db),
        reads: Cell::new(0),
    };
    let ops::Action::Proposal(cmd) = &p.action else {
        unreachable!()
    };
    let r = proposals::run(&tx, &p, cmd, &a, clean().as_ref()).unwrap();
    assert_eq!(tx.reads.get(), 3);
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
async fn retrieval_eval_cross_scope_and_unrelated_trust_changes_spend_no_budget() {
    let (h, id) = ready().await;
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, OLD).await;
    record_current(&h).await;
    let before = gate_report(&h).await;
    h.put(ADMIN_TOKEN, "repo:other", "b", None, "other scope")
        .await;
    // Importing identical bytes changes only trust and revision.
    let b = card(&h, FIXTURE_SCOPE, "b").await;
    let imported = h.call(ADMIN_TOKEN, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"b","base_revision":b["revision"],"base_epoch":h.epoch().await,"text":b["text"],"client_event_id":h.id()})).await;
    assert_eq!(imported.status, 201);
    assert_ne!(imported.body["event"]["trust"], b["trust"]);
    let after = gate_report(&h).await;
    assert_eq!(
        before["adapters"]["hub-lexical"]["binding"]["corpus_identity"],
        after["adapters"]["hub-lexical"]["binding"]["corpus_identity"]
    );
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    assert_eq!(
        h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await.body["state"],
        "applied"
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

fn assert_check(reply: &Reply, expected: &str) {
    assert!(
        reply.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == expected),
        "missing {expected}"
    );
}
async fn pad(h: &Hub, count: usize) {
    for n in 0..count {
        assert_eq!(
            h.put(
                ADMIN_TOKEN,
                FIXTURE_SCOPE,
                &format!("padding-{n:03}"),
                None,
                "Padding."
            )
            .await
            .status,
            201
        );
    }
}
async fn retire(h: &Hub, id: &str) {
    let c = card(h, FIXTURE_SCOPE, id).await;
    assert_eq!(h.call(ADMIN_TOKEN, json!({"action":"retire","scope":FIXTURE_SCOPE,"card_id":id,"base_revision":c["revision"],"base_epoch":h.epoch().await,"reason":"obsolete","client_event_id":h.id()})).await.status, 200);
}

#[tokio::test]
async fn retrieval_eval_snapshot_must_match_at_record_and_remains_immutable() {
    for mutation in ["digest", "count", "scope", "late_write"] {
        let (h, _) = ready().await;
        let mut r = gate_report(&h).await;
        let b = &mut r["adapters"]["hub-lexical"]["binding"];
        match mutation {
            "digest" => b["corpus_identity"]["digest"] = json!(ids::sha256_hex(b"wrong")),
            "count" => b["corpus_identity"]["live_count"] = json!(2),
            "scope" => b["scope"] = json!("repo:other"),
            _ => {
                h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "late", None, "Late.")
                    .await;
            }
        }
        let before = h.events();
        let refused = record(&h, &r).await;
        assert_eq!(refused.status, 409);
        assert_eq!(code(&refused), "memory_retrieval_eval_corpus_mismatch");
        assert_eq!(h.events(), before);
        record_current(&h).await;
        let db = Connection::open(h.memory()).unwrap();
        assert!(
            db.execute("UPDATE memory_retrieval_evals SET corpus_members='[]'", [])
                .is_err()
        );
        assert!(
            db.execute("UPDATE memory_retrieval_evals SET evaluated_ms=0", [])
                .is_err()
        );
        let pairs: String = db
            .query_row(
                "SELECT corpus_members FROM memory_retrieval_evals",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let pairs: Vec<(String, String)> = serde_json::from_str(&pairs).unwrap();
        assert_eq!(pairs.len(), if mutation == "late_write" { 2 } else { 1 });
        let a = card(&h, FIXTURE_SCOPE, "a").await;
        assert_eq!(
            pairs[0],
            ("a".into(), a["content_hash"].as_str().unwrap().into())
        );
    }
}

#[tokio::test]
async fn retrieval_eval_budget_counts_added_removed_and_changed_once_with_live_floor_and_cap() {
    // Each case reaches its inclusive limit, then exceeds it by one card.
    for (initial, limit, kind) in [
        (50, 1, "add"),
        (51, 1, "remove"),
        (50, 1, "content"),
        (300, 5, "content"),
    ] {
        let (h, id) = ready().await;
        pad(&h, initial - 1).await;
        record_current(&h).await;
        for n in 0..=limit {
            let name = format!("padding-{n:03}");
            match kind {
                "add" => {
                    h.put(
                        ADMIN_TOKEN,
                        FIXTURE_SCOPE,
                        &format!("added-{n}"),
                        None,
                        "Added.",
                    )
                    .await;
                }
                "remove" => retire(&h, &name).await,
                _ => {
                    let old = card(&h, FIXTURE_SCOPE, &name).await;
                    assert_eq!(
                        h.put(
                            ADMIN_TOKEN,
                            FIXTURE_SCOPE,
                            &name,
                            old["revision"].as_i64(),
                            "Changed."
                        )
                        .await
                        .status,
                        201
                    );
                }
            }
            assert_check(
                &report(&h, &id).await,
                if n < limit {
                    "retrieval_eval_satisfied"
                } else {
                    "retrieval_eval_drift_exceeded"
                },
            );
        }
        let before = bytes_snapshot(&h);
        let held = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert_eq!(held.body["state"], "pending");
        assert_eq!(bytes_snapshot(&h), before);
    }
    // Fewer than 50 live cards allow zero changed members.
    let (h, id) = ready().await;
    pad(&h, 47).await;
    record_current(&h).await;
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "new", None, "New.").await;
    assert_check(&report(&h, &id).await, "retrieval_eval_drift_exceeded");
}

#[tokio::test]
async fn retrieval_eval_targets_hold_inside_budget_and_after_revert_or_trust_only_edit() {
    for kind in ["content", "retire", "revert", "trust"] {
        let (h, id) = ready().await;
        pad(&h, 49).await;
        record_current(&h).await;
        let a = card(&h, FIXTURE_SCOPE, "a").await;
        match kind {
            "retire" => retire(&h, "a").await,
            "trust" => {
                let imported = h.call(ADMIN_TOKEN, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"a","base_revision":a["revision"],"base_epoch":h.epoch().await,"text":a["text"],"client_event_id":h.id()})).await;
                assert_eq!(imported.status, 201);
                assert_eq!(imported.body["event"]["content_hash"], a["content_hash"]);
            }
            _ => {
                let edit = h
                    .put(
                        ADMIN_TOKEN,
                        FIXTURE_SCOPE,
                        "a",
                        a["revision"].as_i64(),
                        "Changed.",
                    )
                    .await;
                assert_eq!(edit.status, 201);
                if kind == "revert" {
                    h.put(
                        ADMIN_TOKEN,
                        FIXTURE_SCOPE,
                        "a",
                        Some(rev(&edit)),
                        a["text"].as_str().unwrap(),
                    )
                    .await;
                }
            }
        }
        assert_check(&report(&h, &id).await, "retrieval_eval_target_drifted");
        let before = bytes_snapshot(&h);
        let held = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert_eq!(held.body["state"], "pending");
        assert_eq!(bytes_snapshot(&h), before);
    }
}

#[tokio::test]
async fn retrieval_eval_apply_rechecks_drift_after_preparation() {
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
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "late", None, "Late.")
        .await;
    let r = store.write(p).await;
    assert_eq!(r.body["state"], "pending");
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_drift_exceeded")
    );
}

#[tokio::test]
async fn retrieval_eval_unreadable_latest_row_holds_but_newer_report_recovers_using_max_time() {
    let (h, id) = ready().await;
    record_current(&h).await;
    let db = Connection::open(h.memory()).unwrap();
    // Simulate bytes a later, stricter prepare cannot read. No trigger bypass.
    db.execute("INSERT INTO memory_retrieval_evals(record_seq,mode,report_sha256,report_json,principal,credential_id,created_ms,evaluated_ms) VALUES(?1,'lexical',?2,'{}','admin','fixture',?3,?4)", rusqlite::params![h.events()+1, ids::sha256_hex(b"{}"), h.now(), h.now()-1]).unwrap();
    assert_check(&report(&h, &id).await, "retrieval_eval_record_invalid");
    // The maximum is the earlier row, not the latest appended one.
    let equal = record(&h, &gate_report(&h).await).await;
    assert_eq!(code(&equal), "memory_retrieval_eval_superseded");
    // Reserve the synthetic record's sequence, otherwise the next audit event
    // would reuse it. This fixture does not alter existing append-only rows.
    db.execute(
        "UPDATE sqlite_sequence SET seq=seq+1 WHERE name='memory_events'",
        [],
    )
    .unwrap();
    record_current(&h).await;
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
}

#[tokio::test]
async fn retrieval_eval_future_skew_sets_mode_ordering_floor() {
    let (h, _) = ready().await;
    let now = h.now();
    let mut future = gate_report(&h).await;
    future["evaluation_id"] = json!(ids::mint(now + retrieval_eval::MAX_FUTURE_MS));
    assert_eq!(record(&h, &future).await.status, 201);
    for offset in [1, retrieval_eval::MAX_FUTURE_MS] {
        h.set_clock(now + offset);
        assert_eq!(
            code(&record(&h, &gate_report(&h).await).await),
            "memory_retrieval_eval_superseded"
        );
    }
    h.set_clock(now + retrieval_eval::MAX_FUTURE_MS + 1);
    record_current(&h).await;
}

#[tokio::test]
async fn retrieval_eval_schema_seven_upgrade_preserves_rows_epoch_sequence_and_ordering() {
    let (mut h, id) = ready().await;
    let r = gate_report(&h).await;
    let first = record(&h, &r).await;
    let epoch = h.epoch().await;
    let high = h.events();
    h.store.take();
    let db = Connection::open(h.memory()).unwrap();
    db.execute_batch("DROP TABLE memory_retrieval_evals")
        .unwrap();
    db.execute_batch(&retrieval_eval::legacy_ddl()).unwrap();
    let raw = ids::canonical(&r);
    db.execute(
        "INSERT INTO memory_retrieval_evals VALUES(?1,'lexical',?2,?3,'admin','fixture',?4)",
        rusqlite::params![
            first.body["record_seq"].as_i64().unwrap(),
            ids::sha256_hex(raw.as_bytes()),
            raw,
            h.now()
        ],
    )
    .unwrap();
    // An unreadable legacy row must not prevent the upgrade or future records.
    db.execute(
        "INSERT INTO memory_retrieval_evals VALUES(?1,'hybrid',?2,'broken','admin','fixture',?3)",
        rusqlite::params![high + 1, ids::sha256_hex(b"broken"), h.now()],
    )
    .unwrap();
    db.execute(
        "UPDATE sqlite_sequence SET seq=500 WHERE name='memory_events'",
        [],
    )
    .unwrap();
    db.pragma_update(None, "user_version", 7).unwrap();
    schema::migrate(&db, h.now()).unwrap();
    assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        schema::USER_VERSION
    );
    assert_eq!(
        db.query_row(
            "SELECT seq FROM sqlite_sequence WHERE name='memory_events'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        500
    );
    let stored: (String, i64, Option<String>) = db.query_row("SELECT report_json,evaluated_ms,corpus_members FROM memory_retrieval_evals WHERE mode='lexical'", [], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap();
    assert_eq!(stored, (raw, h.now(), None));
    assert_eq!(
        db.query_row(
            "SELECT evaluated_ms FROM memory_retrieval_evals WHERE mode='hybrid'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        h.now() + retrieval_eval::MAX_FUTURE_MS
    );
    assert!(
        db.execute("UPDATE memory_retrieval_evals SET evaluated_ms=0", [])
            .is_err()
    );
    assert!(
        db.execute("DELETE FROM memory_retrieval_evals", [])
            .is_err()
    );
    schema::migrate(&db, h.now()).unwrap();
    drop(db);
    h.open(clean(), None);
    assert_check(&report(&h, &id).await, "retrieval_eval_snapshot_missing");
    assert_eq!(
        code(&record(&h, &r).await),
        "memory_retrieval_eval_superseded"
    );
    record_current(&h).await;
    assert!(
        Connection::open(h.memory())
            .unwrap()
            .query_row("SELECT MAX(seq) FROM memory_events", [], |r| r
                .get::<_, i64>(0))
            .unwrap()
            > 500
    );
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
}

#[tokio::test]
async fn retrieval_eval_one_record_allows_two_independent_applies_within_budget() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    let (h, first) = ready().await;
    let b = h.call(ADMIN_TOKEN, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"b","text":OLD,"client_event_id":h.id()})).await;
    assert_eq!(b.status, 201);
    let refs = [
        "node-1/workspace-a/b-one",
        "node-1/workspace-a/b-two",
        "agent:dot/workspace-a/b-three",
    ];
    for (i, id) in refs.iter().enumerate() {
        assert_eq!(h.call(if i == 2 { AGENT_TOKEN } else { NODE_TOKEN }, json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,"card_id":"b","workspace_id":"workspace-a","evidence_id":id,"artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"fixture evidence"),"observed_at":h.now(),"artifact_base64":STANDARD.encode(b"fixture evidence"),"client_event_id":h.id()})).await.status, 201);
    }
    let mut o = op(FIXTURE_SCOPE, "b", rev(&b));
    o["type"] = json!("append_evidence");
    o.as_object_mut().unwrap().remove("text");
    o["evidence_ids"] = json!(refs);
    let second = h.call(ADMIN_TOKEN, json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[o],"client_event_id":h.id()})).await;
    assert_eq!(second.status, 201);
    pad(&h, 98).await;
    record_current(&h).await;
    for id in [first, second.body["proposal_id"].clone()] {
        h.set_clock(h.now() + 1_001);
        assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
        let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert_eq!(r.body["state"], "applied");
    }
    assert_eq!(
        Connection::open(h.memory())
            .unwrap()
            .query_row("SELECT count(*) FROM memory_retrieval_evals", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

/// Synthetic passing R1 metrics exercise the production hybrid record boundary
/// without a remote embedder. The live index still drives lineage assertions.
async fn hybrid_gate_report(h: &mut Hub) -> Json {
    let mut r = gate_report(h).await;
    let config = json!({"retrieval":"hybrid","embedder":{"backend":"remote","pull":true,"shape":"api/embed","name":"fixture","model_digest":ids::sha256_hex(b"model"),"dimensions":4,"input_chars":1000,"query_prefix_version":1}});
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&config)
        .unwrap();
    let mut candidate = r["adapters"]["hub-lexical"].clone();
    let metrics = json!({"query_count":1,"hit_rate":0.75,"mrr":0.75,"precision_at_k":0.75,"recall_at_k":0.75});
    candidate["overall"] = metrics.clone();
    candidate["by_category"] = json!({"paraphrase":metrics});
    candidate["binding"]["mode"] = json!("hybrid");
    candidate["binding"]["ranking"] = json!("default");
    candidate["binding"]["embedder_id"] =
        json!(h.store.as_ref().unwrap().dense.active_id().unwrap());
    r["adapters"].as_object_mut().unwrap().remove("hub-lexical");
    r["adapters"]["hub-hybrid"] = candidate;
    r["gate"]["name"] = json!("R1-public");
    r
}

async fn default_ranked_gate_search(h: &Hub) -> Reply {
    let r = h.call(ADMIN_TOKEN, json!({"action":"search","query":"gate","mode":"lexical","ranking":"default","scopes":[FIXTURE_SCOPE],"min_seq":h.events()})).await;
    assert_eq!(r.status, 200);
    r
}

async fn default_ranking_change_spends_budget(kind: &str) {
    let (mut h, id) = ready().await;
    let text = "---\nsupersedes: a\n---\nUse the scoped gate.";
    let b = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, text).await;
    assert_eq!(b.status, 201);
    pad(&h, 48).await;
    let r = hybrid_gate_report(&mut h).await;
    assert_eq!(record(&h, &r).await.status, 201);
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    let before = default_ranked_gate_search(&h).await;
    assert!(
        before.body["matches"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["card_id"] == "a")
    );
    // Even default ranking ignores head changes outside the bound scope.
    h.put(ADMIN_TOKEN, "repo:other", "b", None, text).await;
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    match kind {
        "trust" => {
            let imported = h.call(ADMIN_TOKEN, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"b","base_revision":rev(&b),"base_epoch":h.epoch().await,"text":text,"client_event_id":h.id()})).await;
            assert_eq!(imported.status, 201);
            assert_eq!(imported.body["event"]["trust"], "imported");
            // Same bytes now assert a trusted supersedes edge and hide target a.
            let after = default_ranked_gate_search(&h).await;
            assert!(
                !after.body["matches"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["card_id"] == "a")
            );
            assert!(
                after.body["matches"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["card_id"] == "b")
            );
        }
        "recreate" => {
            let retired = h.call(ADMIN_TOKEN, json!({"action":"retire","scope":FIXTURE_SCOPE,"card_id":"b","base_revision":rev(&b),"base_epoch":h.epoch().await,"reason":"obsolete","client_event_id":h.id()})).await;
            assert_eq!(retired.status, 200);
            assert_eq!(
                h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", Some(rev(&retired)), text)
                    .await
                    .status,
                201
            );
        }
        _ => {
            let edit = h
                .put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", Some(rev(&b)), "Changed.")
                .await;
            assert_eq!(edit.status, 201);
            if kind == "revert" {
                assert_eq!(
                    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", Some(rev(&edit)), text)
                        .await
                        .status,
                    201
                );
            }
        }
    }
    let current = card(&h, FIXTURE_SCOPE, "b").await;
    assert!(current["revision"].as_i64().unwrap() > rev(&b));
    if kind != "content" {
        assert_eq!(current["content_hash"], b.body["event"]["content_hash"]);
    }
    // One card spends exactly one slot, including multiple events or both a
    // content difference and an event. A second card must exceed the budget.
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    let other = card(&h, FIXTURE_SCOPE, "padding-000").await;
    assert_eq!(
        h.put(
            ADMIN_TOKEN,
            FIXTURE_SCOPE,
            "padding-000",
            other["revision"].as_i64(),
            "Changed."
        )
        .await
        .status,
        201
    );
    assert_check(&report(&h, &id).await, "retrieval_eval_drift_exceeded");
    let held = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(held.body["state"], "pending");
    assert!(
        held.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_drift_exceeded")
    );
}

#[tokio::test]
async fn retrieval_eval_default_ranking_charges_non_target_trust_flip_with_new_lineage() {
    default_ranking_change_spends_budget("trust").await;
}
#[tokio::test]
async fn retrieval_eval_default_ranking_charges_non_target_revert_once() {
    default_ranking_change_spends_budget("revert").await;
}
#[tokio::test]
async fn retrieval_eval_default_ranking_charges_non_target_same_bytes_recreate_once() {
    default_ranking_change_spends_budget("recreate").await;
}
#[tokio::test]
async fn retrieval_eval_default_ranking_unions_content_and_event_changes() {
    default_ranking_change_spends_budget("content").await;
}

#[tokio::test]
async fn retrieval_eval_adding_card_at_249_does_not_raise_budget_to_five() {
    let (h, id) = ready().await;
    pad(&h, 248).await;
    record_current(&h).await;
    for n in 0..4 {
        let name = format!("padding-{n:03}");
        let c = card(&h, FIXTURE_SCOPE, &name).await;
        assert_eq!(
            h.put(
                ADMIN_TOKEN,
                FIXTURE_SCOPE,
                &name,
                c["revision"].as_i64(),
                "Changed."
            )
            .await
            .status,
            201
        );
    }
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    assert_eq!(
        h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "new", None, "New.")
            .await
            .status,
        201
    );
    assert_check(&report(&h, &id).await, "retrieval_eval_drift_exceeded");
}

#[tokio::test]
async fn hybrid_vector_added_after_evaluation_holds_without_head_change() {
    let (mut h, id) = ready().await;
    let report_body = hybrid_gate_report(&mut h).await;
    assert_eq!(record(&h, &report_body).await.status, 201);
    assert_check(&report(&h, &id).await, "retrieval_eval_satisfied");
    let db = Connection::open(h.memory()).unwrap();
    let hash: String = db
        .query_row(
            "SELECT content_hash FROM memory_cards WHERE scope='repo:brigade/retrieval-eval-fixture' AND card_id='a'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let embedder = h.store.as_ref().unwrap().dense.active_id().unwrap();
    let before = h.events();
    db.execute(
        "INSERT INTO memory_vectors VALUES(?1,?2,1,?3)",
        rusqlite::params![hash, embedder, vec![0u8; 16]],
    )
    .unwrap();
    assert_eq!(h.events(), before);
    assert_check(&report(&h, &id).await, "retrieval_eval_vector_changed");
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(r.body["state"], "pending");
    assert_eq!(h.events(), before);
}

#[tokio::test]
async fn epoch_change_between_automatic_snapshot_and_lock_acquisition_is_rejected() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let (mut h, id) = ready().await;
    record_current(&h).await;
    let before = bytes_snapshot(&h);
    let events = h.events();
    let request = auto_request(&h, &id).await;
    let path = h.memory();
    let observed = Arc::new(AtomicBool::new(false));
    let flag = observed.clone();
    h.store.take();
    h.open(
        clean(),
        Some(Arc::new(move |step| {
            if step == writer::Step::AutoPrepared {
                Connection::open(&path)
                    .unwrap()
                    .execute(
                        "UPDATE memory_meta SET value='restored-epoch' WHERE key='log_epoch'",
                        [],
                    )
                    .unwrap();
                flag.store(true, Ordering::SeqCst);
            }
        })),
    );
    let r = h.call(ADMIN_TOKEN, request).await;
    assert!(observed.load(Ordering::SeqCst));
    assert_eq!((r.status, code(&r)), (409, "memory_epoch_changed"));
    assert_eq!(r.body["log_epoch"], h.epoch().await);
    assert_eq!(bytes_snapshot(&h), before);
    assert_eq!(h.events(), events);
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
            .unwrap()
            .unwrap()["state"],
        "pending"
    );
}

#[tokio::test]
async fn hybrid_vector_change_after_snapshot_is_rechecked_under_apply_lock() {
    let (mut h, id) = ready().await;
    let report_body = hybrid_gate_report(&mut h).await;
    assert_eq!(record(&h, &report_body).await.status, 201);
    let db = Connection::open(h.memory()).unwrap();
    let hash: String = db
        .query_row(
            "SELECT content_hash FROM memory_cards WHERE scope='repo:brigade/retrieval-eval-fixture' AND card_id='a'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let embedder = h.store.as_ref().unwrap().dense.active_id().unwrap();
    let path = h.memory();
    let before = bytes_snapshot(&h);
    let events = h.events();
    // Reopen with the same runtime and a seam outside both writer locks.
    let config = json!({"retrieval":"hybrid","embedder":{"backend":"remote","pull":true,"shape":"api/embed","name":"fixture","model_digest":ids::sha256_hex(b"model"),"dimensions":4,"input_chars":1000,"query_prefix_version":1}});
    h.store.take();
    h.open(
        clean(),
        Some(Arc::new(move |step| {
            if step == writer::Step::AutoPrepared {
                Connection::open(&path)
                    .unwrap()
                    .execute(
                        "INSERT INTO memory_vectors VALUES(?1,?2,1,?3)",
                        rusqlite::params![hash, embedder, vec![0u8; 16]],
                    )
                    .unwrap();
            }
        })),
    );
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&config)
        .unwrap();
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(r.body["state"], "pending");
    assert_check(
        &Reply::new(200, r.body["preflight"].clone()),
        "retrieval_eval_vector_changed",
    );
    let after = bytes_snapshot(&h);
    // Only the externally inserted vector changed. No card or revision changed.
    assert_eq!(&before[..2], &after[..2]);
    assert_eq!(h.events(), events);
}

#[tokio::test]
async fn live_scope_reports_never_authorize_even_with_manufactured_spread_history() {
    let (h, id) = ready().await;
    let before = card(&h, FIXTURE_SCOPE, "a").await;
    let shown = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    let reader = h
        .call(
            NODE_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert!(reader.body.get("retrieval_eval_targets").is_none());
    let targets = shown.body["retrieval_eval_targets"]
        .as_array()
        .expect("reviewer candidate texts");
    let mut safe_targets = targets.clone();
    for target in &mut safe_targets {
        target.as_object_mut().unwrap().remove("after_text");
    }
    let judged = seed_live_history(&h).await;
    let fixture = gate_report(&h).await;
    let metrics =
        json!({"query_count":20,"hit_rate":1.0,"mrr":1.0,"precision_at_k":0.2,"recall_at_k":1.0});
    let mut live = json!({"evaluation_id":h.id(),"k":5,"query_set_digest":ids::sha256_hex(b"private judgments"),
        "gate":{"name":"Live-R0","passed":true,"failures":[]},
        "live":{"proposal_id":id,"targets":safe_targets,"replay_matches":true,"binding":fixture["adapters"]["hub-lexical"]["binding"]},
        "adapters":{"before":{"available":true,"overall":metrics},"candidate":{"available":true,"overall":metrics}}});
    live["query_set_digest"] = judged["query_set_digest"].clone();
    live["live"]["history_seq"] = judged["history_seq"].clone();
    let mut forged = live.clone();
    forged["query_set_digest"] = json!(ids::sha256_hex(b"forged history"));
    assert_eq!(record(&h, &forged).await.status, 409);
    assert_eq!(record(&h, &live).await.status, 201);
    let preflight = report(&h, &id).await;
    assert!(!preflight::all_satisfied(&preflight.body), "{preflight:?}");
    // A caller cannot arm a regression by leaving the pass bit true.
    h.set_clock(h.now() + 1);
    live["evaluation_id"] = json!(h.id());
    live["adapters"]["candidate"]["overall"]["recall_at_k"] = json!(0.95);
    assert_eq!(record(&h, &live).await.status, 201);
    assert!(
        report(&h, &id).await.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "retrieval_eval_failed")
    );
    assert_eq!(
        h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await.body["state"],
        "pending"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, before);
    live["adapters"]["candidate"]["overall"]["recall_at_k"] = json!(1.0);
    for mutation in ["count", "target", "proposal", "parity"] {
        h.set_clock(h.now() + 1);
        let mut changed = live.clone();
        changed["evaluation_id"] = json!(h.id());
        match mutation {
            "count" => changed["adapters"]["before"]["overall"]["query_count"] = json!(19),
            "target" => {
                changed["live"]["targets"][0]["after_hash"] =
                    json!(ids::sha256_hex(b"wrong candidate"))
            }
            "proposal" => changed["live"]["proposal_id"] = json!(h.id()),
            _ => changed["live"]["replay_matches"] = json!(false),
        }
        let recorded = record(&h, &changed).await;
        if mutation == "count" {
            assert_eq!(recorded.status, 409, "{recorded:?}");
            continue;
        }
        assert_eq!(recorded.status, 201);
        let preflight = report(&h, &id).await;
        assert!(!preflight::all_satisfied(&preflight.body), "{preflight:?}");
    }
    h.set_clock(h.now() + 60_001);
    live["evaluation_id"] = json!(h.id());
    assert_eq!(record(&h, &live).await.status, 201);
    let applied = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(applied.body["state"], "pending", "{applied:?}");
}

#[tokio::test]
async fn retrieval_history_twenty_self_made_pairs_supply_only_vetoes() {
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "wrong", None, "needle")
        .await;
    for i in 0..20 {
        let search = h.call(NODE_TOKEN, json!({"action":"search","query":format!("needle {i}"),"scopes":[FIXTURE_SCOPE],"session_id":"one-session","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
        assert_eq!(search.status, 200);
        assert!(search.body["search_id"].is_i64(), "{search:?}");
        let used = h.call(NODE_TOKEN, json!({"action":"retrieval_use","session_id":"one-session","session_kind":"interactive","card_id":"wrong","content_hash":ids::sha256_hex(b"needle\n"),"client_event_id":h.id()})).await;
        assert_eq!(used.status, 201, "{used:?}");
    }
    let judged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
        )
        .await;
    assert_eq!(judged.status, 200, "{judged:?}");
    assert_eq!(judged.body["query_count"], 20);
}

async fn seed_live_history(h: &Hub) -> Json {
    let now = h.now();
    for i in 0..20 {
        h.set_clock(now - (2 - i / 7) * 86_400_000 + i);
        let id = format!("label-{i}");
        let text = format!("term{i}");
        h.put(ADMIN_TOKEN, FIXTURE_SCOPE, &id, None, &text).await;
        history_pair(
            h,
            &format!("session-{}", i / 7),
            &format!("term{i}"),
            &id,
            &text,
        )
        .await;
    }
    h.set_clock(now + 100);
    let judged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
        )
        .await;
    assert_eq!(judged.status, 200, "{judged:?}");
    judged.body
}

async fn history_pair(h: &Hub, session: &str, query: &str, card: &str, text: &str) {
    let search=h.call(NODE_TOKEN,json!({"action":"search","query":query,"scopes":[FIXTURE_SCOPE],"query_source":"explicit","session_id":session,"session_kind":"interactive","min_seq":h.events()})).await;
    assert_eq!(search.status, 200, "{search:?}");
    assert!(search.body["search_id"].is_i64(), "{search:?}");
    let used=h.call(NODE_TOKEN,json!({"action":"retrieval_use","session_id":session,"session_kind":"interactive","card_id":card,"content_hash":ids::sha256_hex(ids::normalize(text).as_bytes()),"client_event_id":h.id()})).await;
    assert_eq!(used.status, 201, "{used:?}");
}

#[tokio::test]
async fn retrieval_history_diverse_history_is_private_and_immutable() {
    let h = Hub::with(clean(), None);
    let judged = seed_live_history(&h).await;
    assert_eq!(judged["query_count"], 20);
    let refused = h
        .call(
            NODE_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
        )
        .await;
    assert_eq!(refused.status, 403);
    let db = Connection::open(h.memory()).unwrap();
    assert!(
        db.execute("UPDATE memory_retrieval_history SET session='forged'", [])
            .is_err()
    );
    assert!(
        db.execute("DELETE FROM memory_retrieval_history", [])
            .is_err()
    );
    assert_eq!(judged["queries"].as_array().unwrap().len(), 20);
}

#[tokio::test]
async fn retrieval_history_filters_jobs_and_binds_session_and_credential() {
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "one", None, "needle")
        .await;
    for kind in [Json::Null, json!("scheduled"), json!("worker")] {
        let r=h.call(NODE_TOKEN,json!({"action":"search","query":"needle","scopes":[FIXTURE_SCOPE],"session_id":"one","session_kind":kind,"query_source":"explicit","min_seq":h.events()})).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert!(r.body.get("search_id").is_none());
    }
    let before=h.call(NODE_TOKEN,json!({"action":"search","query":"person","scopes":[FIXTURE_SCOPE],"session_id":"person","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
    assert!(before.body["search_id"].is_i64());
    let search=h.call(NODE_TOKEN,json!({"action":"search","query":"needle","scopes":[FIXTURE_SCOPE],"session_id":"person","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
    let cron=h.call(NODE_TOKEN,json!({"action":"search","query":"needle cron","scopes":[FIXTURE_SCOPE],"session_id":"cron","session_kind":"scheduled","query_source":"explicit","min_seq":h.events()})).await;
    assert!(cron.body.get("search_id").is_none());
    let use_request = json!({"action":"retrieval_use","session_id":"other","session_kind":"interactive","card_id":"one","content_hash":ids::sha256_hex(b"needle\n"),"client_event_id":h.id()});
    assert_eq!(h.call(NODE_TOKEN, use_request.clone()).await.status, 409);
    let mut request = use_request;
    request["session_id"] = json!("person");
    let mut wrong_hash = request.clone();
    wrong_hash["content_hash"] = json!(ids::sha256_hex(b"different content"));
    assert_eq!(h.call(NODE_TOKEN, wrong_hash).await.status, 409);
    let now = h.now();
    h.set_clock(now + 600_001);
    let mut expired = request.clone();
    expired["client_event_id"] = json!(h.id());
    assert_eq!(h.call(NODE_TOKEN, expired).await.status, 409);
    h.set_clock(now);
    assert_eq!(h.call(AGENT_TOKEN, request.clone()).await.status, 403);
    assert_eq!(h.call(ADMIN_TOKEN, request.clone()).await.status, 409);
    assert_eq!(h.call(NODE_TOKEN, request.clone()).await.status, 201);
    assert_eq!(h.call(NODE_TOKEN, request).await.status, 201);
    let db = Connection::open(h.memory()).unwrap();
    let (count,seq):(i64,i64)=db.query_row("SELECT COUNT(*),json_extract(payload,'$.search_seq') FROM memory_retrieval_history WHERE kind='use'",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    assert_eq!(count, 1);
    assert_eq!(json!(seq), search.body["search_id"]);
}

#[tokio::test]
async fn retrieval_history_invalidates_age_revision_retirement_and_negative() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    for change in ["expiry", "revision", "retire", "negative", "supersede"] {
        let h = Hub::with(clean(), None);
        let judged = seed_live_history(&h).await;
        let current = card(&h, FIXTURE_SCOPE, "label-0").await;
        match change {
            "expiry" => h.set_clock(h.now() + retrieval_history::AGE_MS + 1),
            "revision" => {
                h.put(
                    ADMIN_TOKEN,
                    FIXTURE_SCOPE,
                    "label-0",
                    current["revision"].as_i64(),
                    "corrected",
                )
                .await;
            }
            "retire" => {
                let r=h.call(ADMIN_TOKEN,json!({"action":"retire","scope":FIXTURE_SCOPE,"card_id":"label-0","base_epoch":h.epoch().await,"base_revision":current["revision"],"client_event_id":h.id()})).await;
                assert_eq!(r.status, 200, "{r:?}");
            }
            "negative" => {
                let r=h.call(ADMIN_TOKEN,json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,"card_id":"label-0","workspace_id":"workspace-a","evidence_id":"hub:admin/workspace-a/negative","artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"contradicted"),"observed_at":h.now(),"artifact_base64":STANDARD.encode(b"contradicted"),"stance":"negative","client_event_id":h.id()})).await;
                assert_eq!(r.status, 201, "{r:?}");
            }
            _ => {
                let r = h.call(ADMIN_TOKEN,json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"new","text":"---\nsupersedes: label-0\n---\nreplacement","client_event_id":h.id()})).await;
                assert_eq!(r.status, 201, "{r:?}");
            }
        }
        let got=h.call(ADMIN_TOKEN,json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":judged["history_seq"]})).await;
        assert_eq!(got.status, 200, "{change}: {got:?}");
        assert_eq!(
            got.body["query_count"],
            if change == "expiry" { 0 } else { 19 }
        );
    }
}

#[tokio::test]
async fn retrieval_history_no_diversity_floor_can_hide_a_veto() {
    for kind in ["one-card", "one-day", "one-session"] {
        let h = Hub::with(clean(), None);
        let now = h.now();
        for i in 0..20 {
            let day = if kind == "one-day" { 0 } else { i / 7 };
            h.set_clock(now + day * 86_400_000 + i);
            let session = if kind == "one-session" {
                "only".into()
            } else {
                format!("s-{}", i / 7)
            };
            let id = if kind == "one-card" {
                "only".into()
            } else {
                format!("card-{i}")
            };
            if i == 0 || kind != "one-card" {
                h.put(ADMIN_TOKEN, FIXTURE_SCOPE, &id, None, "needle").await;
            }
            history_pair(&h, &session, &format!("needle {i}"), &id, "needle").await;
        }
        let judged = h
            .call(
                ADMIN_TOKEN,
                json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
            )
            .await;
        assert_eq!(judged.status, 200, "{kind}: {judged:?}");
    }
}

#[tokio::test]
async fn retrieval_history_query_normalization_and_watermark_are_enforced() {
    let h = Hub::with(clean(), None);
    let judged = seed_live_history(&h).await;
    history_pair(&h, "later", "  TERM0  ", "label-0", "term0").await;
    let fresh = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
        )
        .await;
    assert_eq!(fresh.status, 200, "{fresh:?}");
    assert_eq!(fresh.body["query_count"], 20);
    assert_eq!(fresh.body["query_set_digest"], judged["query_set_digest"]);
    let frozen=h.call(ADMIN_TOKEN,json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":judged["history_seq"]})).await;
    assert_eq!(frozen.body, judged);
    let forged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":999999}),
        )
        .await;
    assert_eq!(forged.status, 409);
}

#[tokio::test]
async fn retrieval_history_storage_and_derivation_budgets_fail_closed() {
    let h = Hub::with(clean(), None);
    let judged = seed_live_history(&h).await;
    let db = Connection::open(h.memory()).unwrap();
    // Simulate a busy authenticated history without spending wall clock on I/O.
    db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) SELECT principal,credential,session,kind,created_ms,payload FROM memory_retrieval_history WHERE kind='use'",[]).unwrap();
    for _ in 0..4 {
        db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) SELECT principal,credential,session,kind,created_ms,payload FROM memory_retrieval_history WHERE kind='use'",[]).unwrap();
    }
    let too_many = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE}),
        )
        .await;
    assert_eq!(code(&too_many), "memory_retrieval_history_budget");
    // A bounded frozen set stays usable when only later history grows.
    let frozen=h.call(ADMIN_TOKEN,json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":judged["history_seq"]})).await;
    assert_eq!(frozen.status, 200);
    db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('node-1','fake','s','search',?1,?2)",rusqlite::params![h.now()," ".repeat(16*1024*1024)]).unwrap();
    let used=h.call(NODE_TOKEN,json!({"action":"retrieval_use","session_id":"session-2","session_kind":"interactive","card_id":"label-19","content_hash":ids::sha256_hex(b"term19\n"),"client_event_id":h.id()})).await;
    assert_eq!(code(&used), "memory_retrieval_history_budget");
}

#[tokio::test]
async fn retrieval_history_rejects_client_supplied_search_results() {
    let h = Hub::with(clean(), None);
    let forged=h.call(NODE_TOKEN,json!({"action":"retrieval_search","session_id":"forged","session_kind":"interactive","query":"fake","results":[{"scope":FIXTURE_SCOPE,"card_id":"fake"}],"client_event_id":h.id()})).await;
    assert_eq!(forged.status, 400);
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM memory_retrieval_history", [], |r| r
            .get::<_, i64>(
            0
        ))
        .unwrap(),
        0
    );
}

#[tokio::test]
async fn retrieval_history_ambiguous_scopes_do_not_label_an_open() {
    let h = Hub::with(clean(), None);
    for scope in [FIXTURE_SCOPE, "repo:example/app"] {
        h.put(ADMIN_TOKEN, scope, "one", None, "needle").await;
    }
    let search=h.call(NODE_TOKEN,json!({"action":"search","query":"needle","scopes":[FIXTURE_SCOPE,"repo:example/app"],"session_id":"s","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
    assert_eq!(search.body["matches"].as_array().unwrap().len(), 2);
    let used=h.call(NODE_TOKEN,json!({"action":"retrieval_use","session_id":"s","session_kind":"interactive","card_id":"one","content_hash":ids::sha256_hex(b"needle\n"),"client_event_id":h.id()})).await;
    assert_eq!(used.status, 409);
}

#[tokio::test]
async fn retrieval_history_unreviewed_successor_cannot_erase_other_gold() {
    let h = Hub::with(clean(), None);
    let judged = seed_live_history(&h).await;
    h.put(
        NODE_TOKEN,
        FIXTURE_SCOPE,
        "unreviewed",
        None,
        "---\nsupersedes: label-0\n---\nunsupported successor",
    )
    .await;
    let current=h.call(ADMIN_TOKEN,json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":judged["history_seq"]})).await;
    assert_eq!(current.status, 200, "{current:?}");
    assert_eq!(current.body["query_set_digest"], judged["query_set_digest"]);
}

#[tokio::test]
async fn retrieval_history_admin_rotation_is_bounded_and_preserves_frozen_evidence() {
    let h = Hub::with(clean(), None);
    let judged = seed_live_history(&h).await;
    let db = Connection::open(h.memory()).unwrap();
    let old = h.now() - retrieval_history::AGE_MS - retrieval_eval::MAX_AGE_MS - 600_001;
    for _ in 0..3 {
        db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('old','old','s','search',?1,'{}')",[old]).unwrap();
    }
    let request = json!({"action":"retrieval_history_rotate","limit":2,"client_event_id":h.id()});
    assert_eq!(h.call(NODE_TOKEN, request.clone()).await.status, 403);
    assert_eq!(h.call(AGENT_TOKEN, request.clone()).await.status, 403);
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            json!({"action":"retrieval_history_rotate","limit":501,"client_event_id":h.id()})
        )
        .await
        .status,
        400
    );
    let rotated = h.call(ADMIN_TOKEN, request.clone()).await;
    assert_eq!(rotated.body["deleted"], 2, "{rotated:?}");
    assert_eq!(h.call(ADMIN_TOKEN, request).await, rotated);
    assert_eq!(
        db.query_row(
            "SELECT COUNT(*) FROM memory_retrieval_history WHERE principal='old'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    let frozen=h.call(ADMIN_TOKEN,json!({"action":"retrieval_judgments","scope":FIXTURE_SCOPE,"history_seq":judged["history_seq"]})).await;
    assert_eq!(frozen.body, judged);
    assert!(
        db.execute("DELETE FROM memory_retrieval_history", [])
            .is_err()
    );
}

#[tokio::test]
async fn retrieval_history_principal_and_credential_quotas_isolate_other_nodes() {
    let h = Hub::with(clean(), None);
    let db = Connection::open(h.memory()).unwrap();
    for _ in 0..1000 {
        db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('node-1','old-key','s','search',0,'{}')",[]).unwrap();
    }
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "one", None, "needle")
        .await;
    let search=h.call(NODE_TOKEN,json!({"action":"search","query":"needle","scopes":[FIXTURE_SCOPE],"session_id":"s","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
    assert_eq!(search.status, 200);
    assert!(search.body.get("search_id").is_none());
    // A different principal's budget is still available.
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "INSERT INTO nodes VALUES('node-2',?1,NULL,'2026-01-01',NULL)",
            [digest("fake-other-node")],
        )
        .unwrap();
    assert_eq!(h.call(ADMIN_TOKEN,json!({"action":"grant","principal":"node-2","scope":FIXTURE_SCOPE,"rights":["read"],"client_event_id":h.id()})).await.status,200);
    let other=h.call("fake-other-node",json!({"action":"search","query":"needle","scopes":[FIXTURE_SCOPE],"session_id":"s","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
    assert!(other.body["search_id"].is_i64(), "{other:?}");
}

#[tokio::test]
async fn retrieval_eval_fixture_report_cannot_authorize_real_scope() {
    let h = Hub::with(clean(), None);
    policy(&h);
    let put = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    // A claim rewrite changes what search reads, so only a report could admit it.
    let proposed = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&put))]).await;
    assert_eq!(proposed.status, 201, "{proposed:?}");
    let id = &proposed.body["proposal_id"];
    let before = card(&h, "fleet", "a").await;
    // A valid fixture report is admitted, but is scoped to synthetic data.
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "fixture", None, "gate")
        .await;
    record_current(&h).await;
    assert_check(&report(&h, id).await, "retrieval_eval_projection_changed");
    let reply = h.call(ADMIN_TOKEN, auto_request(&h, id).await).await;
    assert_eq!(reply.body["state"], "pending");
    assert_eq!(card(&h, "fleet", "a").await, before);
}

#[test]
fn retrieval_history_migration_13_and_legacy_14_to_15() {
    for (version, legacy) in [(13, false), (14, false), (14, true)] {
        let db = Connection::open_in_memory().unwrap();
        schema::migrate(&db, 1_800_000_000_000).unwrap();
        if !legacy {
            db.execute_batch("DROP TABLE memory_retrieval_history")
                .unwrap();
        } else {
            db.execute_batch("DROP TABLE memory_verify_evals").unwrap();
            db.execute_batch("DROP TRIGGER memory_retrieval_history_no_delete; CREATE TRIGGER memory_retrieval_history_no_delete BEFORE DELETE ON memory_retrieval_history BEGIN SELECT RAISE(ABORT,'retrieval history is append-only'); END;").unwrap();
            db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('one','one','s','search',0,'{}')",[]).unwrap();
        }
        db.pragma_update(None, "user_version", version).unwrap();
        let epoch = schema::log_epoch(&db).unwrap();
        schema::migrate(&db, 1_800_000_000_000).unwrap();
        assert_eq!(
            db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            schema::USER_VERSION
        );
        assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
        let trigger: String = db
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='memory_retrieval_history_no_delete'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(trigger.contains("retrieval_history_rotation"));
        assert!(
            db.execute("DELETE FROM memory_retrieval_history", [])
                .is_ok()
                != legacy
        );
        schema::migrate(&db, 1_800_000_000_000).unwrap();
    }
}

#[tokio::test]
async fn retrieval_history_single_new_label_vetoes_one_gold_despite_other_gold_gain() {
    use crate::writer::worker::Tx;
    let h = Hub::with(clean(), None);
    let scope = FIXTURE_SCOPE;
    h.put(ADMIN_TOKEN, scope, "a", None, "needle").await;
    let b = h
        .put(ADMIN_TOKEN, scope, "b", None, "needle needle needle")
        .await;
    let db = Connection::open(h.memory()).unwrap();
    let proposal = json!({"cards":[{"scope":scope,"card_id":"b","before_text":"needle needle needle\n","after_text":"needle\n","before_state":"live","after_state":"live","after_hash":ids::sha256_hex(b"needle\n")} ]});
    assert_eq!(
        retrieval_history::veto(&Tx(&db), &proposal, h.now()).unwrap(),
        None
    );
    // This label arrives after the earlier check and is the only query.
    history_pair(&h, "only", "needle", "b", "needle needle needle").await;
    assert_eq!(
        retrieval_history::veto(&Tx(&db), &proposal, h.now()).unwrap(),
        Some("retrieval_eval_query_veto")
    );
    // A second gold card's gain cannot cancel the first card's loss.
    history_pair(&h, "only", "needle", "a", "needle").await;
    assert_eq!(
        retrieval_history::veto(&Tx(&db), &proposal, h.now()).unwrap(),
        Some("retrieval_eval_query_veto")
    );
    assert_eq!(card(&h, scope, "b").await["revision"], json!(rev(&b)));
}

#[tokio::test]
async fn retrieval_eval_invariant_projection_passes_without_any_measurement() {
    use crate::writer::worker::Tx;
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, "needle").await;
    let c = card(&h, "fleet", "a").await;
    let proposal = json!({"tier":0,"ops":[{"type":"append_evidence"}],"cards":[{
        "scope":"fleet","card_id":"a","base_revision":c["revision"],
        "before_hash":c["content_hash"],"after_hash":c["content_hash"],
        "before_text":c["text"],"after_text":c["text"],"before_state":"live","after_state":"live"}]});
    let db = Connection::open(h.memory()).unwrap();
    let hub = Connection::open(&h.hub).unwrap();
    let mut authority = ops::Authority::with_credentials(&Tx(&hub), &digest(ADMIN_TOKEN)).unwrap();
    authority.retrieval = Some(retrieval_eval::Live {
        applier: "admin",
        principal: super::ADMIN.into(),
        snapshot: None,
        indexed_seq: h.events(),
        embedder_id: None,
        weights: json!({"lexical":1.0,"dense":1.0}),
    });
    assert_eq!(
        retrieval_eval::hold(&Tx(&db), &proposal, &authority, h.now()).unwrap(),
        None
    );
    let mut merge = proposal.clone();
    merge["ops"][0]["type"] = json!("merge_exact_duplicates");
    assert_eq!(
        retrieval_eval::hold(&Tx(&db), &merge, &authority, h.now()).unwrap(),
        Some("retrieval_eval_merge_shape")
    );
}

#[tokio::test]
async fn retrieval_history_credential_byte_and_rate_limits_fail_closed() {
    for kind in [
        "credential-rows",
        "credential-bytes",
        "principal-bytes",
        "rate",
    ] {
        let h = Hub::with(clean(), None);
        let scope = FIXTURE_SCOPE;
        h.put(ADMIN_TOKEN, scope, "one", None, "needle").await;
        let db = Connection::open(h.memory()).unwrap();
        let key = if kind == "principal-bytes" {
            "old-key".into()
        } else {
            digest(NODE_TOKEN)
        };
        let count = if kind == "credential-rows" {
            500
        } else if kind == "rate" {
            120
        } else {
            1
        };
        let size = if kind == "credential-bytes" {
            1024 * 1024
        } else if kind == "principal-bytes" {
            2 * 1024 * 1024
        } else {
            2
        };
        for _ in 0..count {
            db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('node-1',?1,'s','search',?2,?3)",rusqlite::params![key,if kind=="rate" {h.now()} else {0}," ".repeat(size)]).unwrap();
        }
        let search=h.call(NODE_TOKEN,json!({"action":"search","query":"needle","scopes":[scope],"session_id":"s","session_kind":"interactive","query_source":"explicit","min_seq":h.events()})).await;
        assert_eq!(search.status, 200, "{kind}: {search:?}");
        assert!(search.body.get("search_id").is_none(), "{kind}: {search:?}");
    }
}

#[tokio::test]
async fn retrieval_history_new_labels_invalidate_automatic_snapshot_token() {
    use crate::writer::worker::Tx;
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "one", None, "needle")
        .await;
    let db = Connection::open(h.memory()).unwrap();
    let token = hardening::token(&Tx(&db)).unwrap();
    let events = h.events();
    history_pair(&h, "only", "needle", "one", "needle").await;
    assert_eq!(h.events(), events);
    assert_ne!(hardening::token(&Tx(&db)).unwrap(), token);
}
