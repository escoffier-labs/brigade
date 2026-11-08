use super::embedding::LEASE_MS;
use super::tests::*;
use super::*;
use base64::Engine;
use serde_json::json;
use std::sync::Arc;

pub(super) fn setup(h: &mut Hub) -> String {
    setup_at(h, "http://127.0.0.1:1/api/embed")
}
fn setup_at(h: &mut Hub, endpoint: &str) -> String {
    setup_with(h, Some(endpoint), json!({"pull": true}))
}
/// Configure hybrid retrieval. `extra` overrides keys such as `pull` and
/// `pull_private`. No endpoint means pull mode without a query embedder.
fn setup_with(h: &mut Hub, endpoint: Option<&str>, extra: Json) -> String {
    let token = h.dir.join("embed.token");
    std::fs::write(&token, "fake-embed-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    let mut embedder = json!({"backend": "remote", "shape": "api/embed", "name": "fake-model", "model_digest": "fake-digest", "dimensions": 2, "input_chars": 6000, "query_prefix_version": 1});
    if let Some(endpoint) = endpoint {
        embedder["endpoint"] = json!(endpoint);
        embedder["allowlist"] = json!([endpoint]);
        embedder["token_file"] = json!(token);
    }
    for (key, value) in extra.as_object().unwrap() {
        embedder[key] = value.clone();
    }
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"retrieval": "hybrid", "embedder": embedder}))
        .unwrap();
    h.store
        .as_ref()
        .unwrap()
        .retrieval
        .as_ref()
        .unwrap()
        .config
        .id
        .key()
}
async fn command(h: &Hub, raw: Json) -> Reply {
    let mut raw = raw;
    raw["client_event_id"] = json!(h.id());
    h.call(ADMIN_TOKEN, raw).await
}
pub(super) fn encoded(v: &[f32]) -> String {
    base64::engine::general_purpose::STANDARD
        .encode(v.iter().flat_map(|f| f.to_le_bytes()).collect::<Vec<_>>())
}
async fn fill(h: &Hub, id: &str) {
    assert_eq!(command(h, json!({"action": "reindex"})).await.status, 200);
    let lease = command(
        h,
        json!({"action": "embed_lease", "embedder_id": id, "max": 4}),
    )
    .await;
    assert_eq!(lease.status, 200, "{:?}", lease);
    let vectors: Vec<_> = lease.body["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|j| json!({"content_hash": j["content_hash"], "vector": encoded(&[1.0, 0.0])}))
        .collect();
    if vectors.is_empty() {
        return;
    }
    let result = command(h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await;
    assert_eq!(result.status, 200, "{:?}", result);
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
}
#[tokio::test]
async fn reindex_is_idempotent_shared_vectors_and_exact_retirement_lineage() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let a = h
        .put(ADMIN_TOKEN, "repo:a/r", "one", None, "shared body")
        .await;
    let b = h
        .put(ADMIN_TOKEN, "repo:b/r", "two", None, "shared body")
        .await;
    assert_eq!(a.status, 201);
    assert_eq!(b.status, 201);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    // `put` queued the shared text once, inside its own transaction.
    assert_eq!(
        db.query_row("SELECT count(*) FROM memory_embed_jobs", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    fill(&h, &id).await;
    let count = |table: &str| {
        db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| {
            r.get::<_, i64>(0)
        })
        .unwrap()
    };
    assert_eq!(count("memory_vectors"), 1);
    assert_eq!(count("memory_lineage"), 2);
    for (scope, card, reply, remaining) in [("repo:a/r", "one", a, 1), ("repo:b/r", "two", b, 0)] {
        let result = command(&h, json!({"action": "retire", "scope": scope, "card_id": card, "base_revision": rev(&reply), "base_epoch": h.epoch().await})).await;
        assert_eq!(result.status, 200, "{result:?}");
        assert_eq!(count("memory_lineage"), remaining);
        assert_eq!(count("memory_vectors"), remaining.min(1));
    }
}
#[tokio::test]
async fn switching_keeps_vectors_reuses_matching_and_falls_back_when_endpoint_stops() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "one", None, "searchable apple")
        .await;
    let switching = h.call(ADMIN_TOKEN, json!({"action": "status"})).await;
    assert_eq!(switching.body["retrieval"]["state"], "switching");
    let first = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(first.status, 200);
    assert_eq!(first.body["retrieval"]["mode"], "lexical");
    fill(&h, &id).await;
    assert_eq!(h.store.as_ref().unwrap().retrieval_status()["state"], "ok");
    let stopped = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(stopped.status, 200);
    assert_eq!(stopped.body["retrieval"]["mode"], "lexical-fallback");
    assert_eq!(stopped.body["retrieval"]["reason"], "embedder-unavailable");
    assert_eq!(
        h.store.as_ref().unwrap().retrieval_status()["state"],
        "degraded"
    );
    // Restart in lexical mode reads neither vectors nor the leftover secret file.
    drop(h.store.take());
    h.open(scan::bundled(), None);
    assert!(h.store.as_ref().unwrap().dense.current(&id).is_none());
    assert_eq!(
        h.call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
            .await
            .body["retrieval"]["mode"],
        "lexical"
    );
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM memory_vectors", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    setup(&mut h);
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    h.put(ADMIN_TOKEN, "fleet", "two", None, "missing pear")
        .await;
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    assert_eq!(
        h.store.as_ref().unwrap().retrieval_status()["state"],
        "switching"
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_embed_jobs WHERE embedder_id=?1 AND state='pending'",
            [&id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    let old = h.store.as_ref().unwrap().dense.current(&id).unwrap();
    // A different fingerprint backfills all of its own missing rows and retains the old matrix.
    let runtime = h.store.as_mut().unwrap().retrieval.take().unwrap();
    let mut c = runtime.config.clone();
    c.id.model_digest = "second-digest".into();
    let second = c.id.key();
    h.store.as_mut().unwrap().retrieval = Some(embedder::Runtime::new(c, scan::bundled()).unwrap());
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        2
    );
    assert!(Arc::ptr_eq(
        &old,
        &h.store.as_ref().unwrap().dense.current(&id).unwrap()
    ));
    // The active embedder cannot be dropped while hybrid is configured.
    let refused = command(&h, json!({"action": "reindex", "drop": second})).await;
    assert_eq!(refused.status, 409, "{refused:?}");
    assert_eq!(refused.body["code"], "memory_embedder_active");
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_vectors WHERE embedder_id=?1",
            [&id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}
#[tokio::test]
async fn scan_pending_digest_lease_expiry_and_principal_are_enforced() {
    let mut h = Hub::new();
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "pending", None, "pending body")
        .await;
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    drop(h.store.take());
    h.open(scan::bundled(), None);
    setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean body")
        .await;
    command(&h, json!({"action": "reindex"})).await;
    let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    assert_eq!(lease.body["jobs"].as_array().unwrap().len(), 1);
    let vectors = json!([{"content_hash": lease.body["jobs"][0]["content_hash"], "vector": encoded(&[1.0, 0.0])}]);
    let wrong = command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "vectors": vectors, "model_digest": "other"})).await;
    assert_eq!(wrong.status, 422);
    h.set_clock(h.now() + 60_001);
    let expired = command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "vectors": vectors, "model_digest": "fake-digest"})).await;
    assert_eq!(expired.status, 409);
    let node = h
        .call(
            NODE_TOKEN,
            json!({"action": "embed_lease", "embedder_id": id, "client_event_id": h.id()}),
        )
        .await;
    assert_eq!(node.status, 403);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM memory_vectors", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
}
async fn rows(h: &Hub, sql: &str) -> i64 {
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.query_row(sql, [], |r| r.get(0)).unwrap()
}
fn raw_card(db: &rusqlite::Connection, scope: &str, card: &str, hash: &str, scan: &str) {
    db.execute(
        "INSERT OR IGNORE INTO memory_blobs VALUES(?1,?2,4,'test')",
        rusqlite::params![hash, b"body"],
    )
    .unwrap();
    db.execute("INSERT INTO memory_cards VALUES(?1,?2,1,?3,'live','reviewed','hub:admin','admin','','','[]','test','test')", rusqlite::params![scope, card, hash]).unwrap();
    db.execute("INSERT INTO memory_card_revisions VALUES(?1,?2,1,?3,'reviewed','live','hub:admin',1,NULL,?4,'test')", rusqlite::params![scope, card, hash, scan]).unwrap();
}

#[tokio::test]
async fn lease_reply_is_never_stored_or_replayed_after_retirement() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let put = h
        .put(
            ADMIN_TOKEN,
            "fleet",
            "one",
            None,
            "remembered orchard notes",
        )
        .await;
    let before = rows(&h, "SELECT count(*) FROM memory_idempotency").await;
    let event = h.id();
    let raw =
        json!({"action": "embed_lease", "embedder_id": id, "max": 4, "client_event_id": event});
    let lease = h.call(ADMIN_TOKEN, raw.clone()).await;
    assert!(
        lease.body["jobs"][0]["text"]
            .as_str()
            .unwrap()
            .contains("orchard")
    );
    // Neither the lease nor the derived reindex turns store a reply.
    assert_eq!(command(&h, json!({"action": "reindex"})).await.status, 200);
    assert_eq!(
        rows(&h, "SELECT count(*) FROM memory_idempotency").await,
        before
    );
    assert_eq!(
        rows(
            &h,
            "SELECT count(*) FROM memory_idempotency WHERE response LIKE '%orchard%'"
        )
        .await,
        0
    );
    let retire = command(&h, json!({"action": "retire", "scope": "fleet", "card_id": "one", "base_revision": rev(&put), "base_epoch": h.epoch().await})).await;
    assert_eq!(retire.status, 200);
    h.set_clock(h.now() + LEASE_MS + 1);
    let replay = h.call(ADMIN_TOKEN, raw).await;
    assert_eq!(replay.status, 200, "{replay:?}");
    assert_eq!(replay.body["jobs"], json!([]));
    assert!(!replay.body.to_string().contains("orchard"));
}

/// A test endpoint that refuses any batch holding the word "poison" with 400.
async fn picky() -> (
    String,
    Arc<std::sync::Mutex<usize>>,
    tokio::task::JoinHandle<()>,
) {
    use http_body_util::{BodyExt, Full};
    use hyper::body::Bytes;
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let calls = Arc::new(std::sync::Mutex::new(0));
    let seen = calls.clone();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let seen = seen.clone();
            tokio::spawn(async move {
                let service = hyper::service::service_fn(
                    move |req: hyper::Request<hyper::body::Incoming>| {
                        let seen = seen.clone();
                        async move {
                            let body = req.into_body().collect().await?.to_bytes();
                            let value: Json = serde_json::from_slice(&body).unwrap();
                            *seen.lock().unwrap() += 1;
                            let inputs = value["input"].as_array().unwrap();
                            if inputs
                                .iter()
                                .any(|t| t.as_str().unwrap().contains("poison"))
                            {
                                return Ok::<_, hyper::Error>(
                                    hyper::Response::builder()
                                        .status(400)
                                        .body(Full::new(Bytes::new()))
                                        .unwrap(),
                                );
                            }
                            let n = inputs.len();
                            Ok(hyper::Response::new(Full::new(Bytes::from(
                                json!({"model_digest": "fake-digest", "embeddings": vec![vec![3.0, 4.0]; n]}).to_string(),
                            ))))
                        }
                    },
                );
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(hyper_util::rt::TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (format!("http://{address}/api/embed"), calls, task)
}

#[tokio::test]
async fn poison_document_is_retried_alone_then_parked_without_stalling_the_backfill() {
    let (endpoint, calls, task) = picky().await;
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let runtime = h.store.as_mut().unwrap().retrieval.take().unwrap();
    let mut config = runtime.config.clone();
    config.endpoint = endpoint.clone();
    config.allowlist = vec![endpoint];
    config.pull = false;
    h.store.as_mut().unwrap().retrieval =
        Some(embedder::Runtime::new(config, scan::bundled()).unwrap());
    h.store.as_mut().unwrap().pull_embeddings = false;
    // Startup loads the matrix before any write, as `start_retrieval` does.
    h.store.as_ref().unwrap().load_dense().await;
    for (card, text) in [
        ("a", "apple notes"),
        ("b", "poison pill"),
        ("c", "pear notes"),
    ] {
        assert_eq!(
            h.put(ADMIN_TOKEN, "fleet", card, None, text).await.status,
            201
        );
    }
    for _ in 0..12 {
        h.store
            .as_ref()
            .unwrap()
            .retrieval
            .as_ref()
            .unwrap()
            .reset_backoff();
        h.set_clock(h.now() + 3_600_001);
        h.store.as_ref().unwrap().embedding_tick().await;
    }
    task.abort();
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_vectors").await, 2);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let (state, attempts, error): (String, i64, String) = db
        .query_row(
            "SELECT j.state,j.attempts,j.last_error FROM memory_embed_jobs j JOIN memory_cards c ON c.content_hash=j.content_hash WHERE c.card_id='b' AND j.embedder_id=?1",
            [&id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        (state.as_str(), attempts, error.as_str()),
        ("failed", embedding::MAX_ATTEMPTS, "embedder-rejected")
    );
    // One batch, then each document alone until the poison one is parked.
    assert!(*calls.lock().unwrap() <= 2 + embedding::MAX_ATTEMPTS as usize);
    // Further ticks send nothing.
    let sent = *calls.lock().unwrap();
    h.store.as_ref().unwrap().embedding_tick().await;
    assert_eq!(*calls.lock().unwrap(), sent);
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(status["state"], "ok", "{status}");
    assert_eq!(status["coverage"]["unembeddable"], 1);
}

#[tokio::test]
async fn embed_failed_over_http_backs_off_parks_and_never_reopens_done_jobs() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "bad", None, "refused text")
        .await;
    async fn lease_of(h: &Hub, id: &str) -> Reply {
        command(h, json!({"action": "embed_lease", "embedder_id": id})).await
    }
    async fn fail(h: &Hub, lease: Json, reason: &str) -> Reply {
        command(h, json!({"action": "embed_failed", "lease_id": lease["lease_id"], "failures": [{"content_hash": lease["jobs"][0]["content_hash"], "reason": reason}]})).await
    }
    // An endpoint fault releases the job without counting an attempt.
    let first = lease_of(&h, &id).await.body;
    let released = fail(&h, first, "embedder-unavailable").await;
    assert_eq!(released.status, 200, "{released:?}");
    assert_eq!(released.body["released"], 1);
    assert_eq!(rows(&h, "SELECT attempts FROM memory_embed_jobs").await, 0);
    for attempt in 1..=embedding::MAX_ATTEMPTS {
        h.set_clock(h.now() + 3_600_001);
        let leased = lease_of(&h, &id).await.body;
        assert_eq!(
            leased["jobs"].as_array().unwrap().len(),
            1,
            "attempt {attempt}"
        );
        let reply = fail(&h, leased, "embedder-rejected").await;
        assert_eq!(reply.status, 200);
        if attempt < embedding::MAX_ATTEMPTS {
            // Backing off: the job is not offered again yet.
            assert_eq!(lease_of(&h, &id).await.body["jobs"], json!([]));
        } else {
            assert_eq!(reply.body["parked"], 1);
        }
    }
    h.set_clock(h.now() + 3_600_001);
    assert_eq!(lease_of(&h, &id).await.body["jobs"], json!([]));
    assert_eq!(
        rows(&h, "SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='embedder-rejected'").await,
        1
    );
    // A late failure report never reopens a finished job.
    h.put(ADMIN_TOKEN, "fleet", "good", None, "accepted text")
        .await;
    let leased = lease_of(&h, &id).await.body;
    let vectors = json!([{"content_hash": leased["jobs"][0]["content_hash"], "vector": encoded(&[1.0, 0.0])}]);
    assert_eq!(command(&h, json!({"action": "embed_submit", "lease_id": leased["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await.status, 200);
    let late = fail(&h, leased, "embedder-rejected").await;
    assert_eq!(late.body["released"], 0);
    assert_eq!(
        rows(
            &h,
            "SELECT count(*) FROM memory_embed_jobs WHERE state='done'"
        )
        .await,
        1
    );
    // The operator's reindex gives the parked document another chance.
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        1
    );
    let bad = command(&h, json!({"action": "embed_failed", "lease_id": "x", "failures": [{"content_hash": "short", "reason": "embedder-rejected"}]})).await;
    assert_eq!(bad.status, 422);
}

/// Clean unless the text holds a word that the current rules flag.
struct Rules(std::sync::atomic::AtomicBool);
impl scan::Scanner for Rules {
    fn scan(&self, text: &str) -> scan::Scan {
        let flagged = text.contains("flagme")
            || self.0.load(std::sync::atomic::Ordering::SeqCst) && text.contains("changed");
        if flagged {
            scan::Scan::Flagged {
                ruleset: "r1".into(),
                rules: vec!["role-override".into()],
            }
        } else {
            scan::Scan::Clean {
                ruleset: "r1".into(),
            }
        }
    }
    fn describe(&self) -> (bool, Option<String>) {
        (true, Some("r1".into()))
    }
}

#[tokio::test]
async fn pending_and_flagged_cards_are_unembeddable_and_do_not_hold_hybrid() {
    let mut h = Hub::new();
    h.put(ADMIN_TOKEN, "fleet", "pending", None, "pending body")
        .await;
    drop(h.store.take());
    let rules = Arc::new(Rules(std::sync::atomic::AtomicBool::new(false)));
    h.open(rules.clone(), None);
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean apple")
        .await;
    h.put(ADMIN_TOKEN, "fleet", "stored", None, "flagme at write")
        .await;
    h.put(ADMIN_TOKEN, "fleet", "later", None, "changed rules later")
        .await;
    // Only clean receipts are queued. Flagged text never leaves the host.
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 2);
    rules.0.store(true, std::sync::atomic::Ordering::SeqCst);
    let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    let jobs = lease.body["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1, "{lease:?}");
    assert!(jobs[0]["text"].as_str().unwrap().contains("apple"));
    let vectors =
        json!([{"content_hash": jobs[0]["content_hash"], "vector": encoded(&[1.0, 0.0])}]);
    assert_eq!(command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await.status, 200);
    assert_eq!(
        rows(&h, "SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='content-flagged'").await,
        1
    );
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(status["effective"], "hybrid", "{status}");
    assert_eq!(status["state"], "ok");
    assert_eq!(
        status["coverage"],
        json!({"cards": 1, "eligible": 1, "unembeddable": 3, "total": 4})
    );
}

#[tokio::test]
async fn lease_that_parks_the_last_job_refreshes_coverage_without_a_later_write() {
    let mut h = Hub::new();
    drop(h.store.take());
    let rules = Arc::new(Rules(std::sync::atomic::AtomicBool::new(false)));
    h.open(rules.clone(), None);
    let id = setup(&mut h);
    // Startup loads the matrix before any write, as `start_retrieval` does.
    h.store.as_ref().unwrap().load_dense().await;
    h.put(ADMIN_TOKEN, "fleet", "later", None, "changed rules later")
        .await;
    let before = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(before["state"], "switching", "{before}");
    // The ruleset now flags the only queued text. The lease parks it and
    // offers nothing. No submit follows and nothing calls refresh_dense.
    rules.0.store(true, std::sync::atomic::Ordering::SeqCst);
    let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    assert_eq!(lease.body["jobs"], json!([]), "{lease:?}");
    assert_eq!(lease.body["parked"], 1);
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(status["state"], "ok", "{status}");
    // The only card is unembeddable, so the matrix has no row to rank with.
    assert_eq!(status["effective"], "lexical");
    assert_eq!(
        status["coverage"],
        json!({"cards": 0, "eligible": 0, "unembeddable": 1, "total": 1})
    );
    // A lease that parks nothing reports zero and leaves coverage alone.
    let quiet = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    assert_eq!(quiet.body["parked"], 0);
}

#[tokio::test]
async fn a_corpus_parked_by_embedding_failures_is_degraded_and_searches_lexically() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    // Startup loads the matrix before any write, as `start_retrieval` does.
    h.store.as_ref().unwrap().load_dense().await;
    h.put(ADMIN_TOKEN, "fleet", "one", None, "searchable apple")
        .await;
    h.put(ADMIN_TOKEN, "fleet", "two", None, "searchable pear")
        .await;
    // An endpoint that always answers the wrong row count: every document
    // fails with `vector-invalid` until it parks.
    for _ in 0..(2 * embedding::MAX_ATTEMPTS) {
        h.set_clock(h.now() + 3_600_001);
        let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
        let jobs = lease.body["jobs"].as_array().unwrap();
        if jobs.is_empty() {
            continue;
        }
        let failures: Vec<_> = jobs
            .iter()
            .map(|j| json!({"content_hash": j["content_hash"], "reason": "vector-invalid", "alone": jobs.len() == 1}))
            .collect();
        let reply = command(&h, json!({"action": "embed_failed", "lease_id": lease.body["lease_id"], "failures": failures})).await;
        assert_eq!(reply.status, 200, "{reply:?}");
    }
    assert_eq!(
        rows(&h, "SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='vector-invalid'").await,
        2
    );
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(status["state"], "degraded", "{status}");
    assert_eq!(status["effective"], "lexical-fallback");
    assert_eq!(status["reason"], "all-embeddings-failed");
    assert_eq!(status["coverage"]["cards"], 0);
    let found = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(found.status, 200);
    assert_eq!(
        found.body["retrieval"]["mode"], "lexical-fallback",
        "{found:?}"
    );
    assert_eq!(found.body["retrieval"]["degraded"], true);
    assert_eq!(found.body["retrieval"]["reason"], "all-embeddings-failed");
    assert_eq!(found.body["matches"].as_array().unwrap().len(), 1);
    // The operator's reindex requeues the parked documents. Once one has a
    // vector the corpus is hybrid again, with the other left to BM25.
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        2
    );
    let lease = command(
        &h,
        json!({"action": "embed_lease", "embedder_id": id, "max": 1}),
    )
    .await;
    let jobs = lease.body["jobs"].as_array().unwrap();
    let vectors =
        json!([{"content_hash": jobs[0]["content_hash"], "vector": encoded(&[1.0, 0.0])}]);
    assert_eq!(command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await.status, 200);
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_ne!(status["reason"], "all-embeddings-failed", "{status}");
    assert_ne!(status["state"], "degraded", "{status}");
}

#[tokio::test]
async fn a_reported_batch_backs_off_each_document_the_worker_already_tried_alone() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    async fn leased_pair(h: &Hub, id: &str) -> Json {
        let lease = command(
            h,
            json!({"action": "embed_lease", "embedder_id": id, "max": 4}),
        )
        .await;
        assert_eq!(lease.body["jobs"].as_array().unwrap().len(), 2, "{lease:?}");
        lease.body
    }
    for (card, text) in [("a", "first refused"), ("b", "second refused")] {
        h.put(ADMIN_TOKEN, "fleet", card, None, text).await;
    }
    let lease = leased_pair(&h, &id).await;
    let rows_of = |alone: Option<bool>| -> Vec<Json> {
        lease["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|j| {
                let mut row =
                    json!({"content_hash": j["content_hash"], "reason": "embedder-rejected"});
                if let Some(alone) = alone {
                    row["alone"] = json!(alone);
                }
                row
            })
            .collect()
    };
    // The worker retried both alone and reports two refusals: each backs off.
    let reply = command(&h, json!({"action": "embed_failed", "lease_id": lease["lease_id"], "failures": rows_of(Some(true))})).await;
    assert_eq!(reply.body["released"], 2, "{reply:?}");
    let waits = |h: &Hub, lease: &Json| {
        let db = rusqlite::Connection::open(h.memory()).unwrap();
        lease["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|j| {
                db.query_row(
                    "SELECT not_before-?1 FROM memory_embed_jobs WHERE content_hash=?2",
                    rusqlite::params![h.now(), j["content_hash"].as_str().unwrap()],
                    |r| r.get::<_, i64>(0),
                )
                .unwrap()
            })
            .collect::<Vec<_>>()
    };
    assert_eq!(waits(&h, &lease), vec![embedding::BACKOFF_MS; 2]);
    assert_eq!(
        command(&h, json!({"action": "embed_lease", "embedder_id": id}))
            .await
            .body["jobs"],
        json!([])
    );
    // A whole batch reported without the flag is still retried at once.
    for (card, text) in [("c", "third refused"), ("d", "fourth refused")] {
        h.put(ADMIN_TOKEN, "fleet", card, None, text).await;
    }
    let lease = leased_pair(&h, &id).await;
    let rows: Vec<Json> = lease["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|j| json!({"content_hash": j["content_hash"], "reason": "embedder-rejected"}))
        .collect();
    command(
        &h,
        json!({"action": "embed_failed", "lease_id": lease["lease_id"], "failures": rows}),
    )
    .await;
    assert_eq!(waits(&h, &lease), vec![0; 2]);
    // The flag must be a boolean.
    let bad = command(&h, json!({"action": "embed_failed", "lease_id": "x", "failures": [{"content_hash": "0".repeat(64), "reason": "embedder-rejected", "alone": "yes"}]})).await;
    assert_eq!(bad.status, 422);
}

#[tokio::test]
async fn lease_filters_scope_rights_and_duplicate_hashes_before_the_limit() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.execute_batch("BEGIN").unwrap();
    for i in 0..300 {
        raw_card(
            &db,
            "repo:a/r",
            &format!("a-{i}"),
            &format!("1{i:063x}"),
            "clean",
        );
        // Three hundred cards in one scope share one text.
        raw_card(&db, "fleet", &format!("same-{i}"), &"0".repeat(64), "clean");
    }
    raw_card(
        &db,
        "fleet",
        "other",
        &format!("2{}", "0".repeat(63)),
        "clean",
    );
    raw_card(&db, "repo:b/r", "mine", &"f".repeat(64), "clean");
    db.execute_batch("COMMIT").unwrap();
    assert_eq!(command(&h, json!({"action": "reindex"})).await.status, 200);
    let admin = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    let hashes: Vec<_> = admin.body["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|j| j["content_hash"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(hashes.len(), 4, "{hashes:?}");
    assert_eq!(hashes[0], "0".repeat(64));
    command(
        &h,
        json!({"action": "grant", "principal": "agent:dot", "scope": "repo:b/r", "rights": ["embed"]}),
    )
    .await;
    let agent = h
        .call(
            AGENT_TOKEN,
            json!({"action": "embed_lease", "embedder_id": id, "client_event_id": h.id()}),
        )
        .await;
    assert_eq!(agent.status, 200, "{agent:?}");
    assert_eq!(agent.body["jobs"].as_array().unwrap().len(), 1);
    assert_eq!(agent.body["jobs"][0]["content_hash"], "f".repeat(64));
}

#[tokio::test]
async fn scheduler_tick_scans_no_corpus_and_put_queues_its_own_job() {
    let mut h = Hub::with(scan::bundled(), None);
    setup(&mut h);
    h.store.as_mut().unwrap().pull_embeddings = false;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    raw_card(&db, "fleet", "raw", &"a".repeat(64), "clean");
    let before = rows(&h, "SELECT count(*) FROM memory_idempotency").await;
    for _ in 0..3 {
        assert!(!h.store.as_ref().unwrap().embedding_tick().await);
    }
    // The tick only leases. Finding unqueued cards is reindex's job.
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 0);
    assert_eq!(
        rows(&h, "SELECT count(*) FROM memory_idempotency").await,
        before
    );
    h.put(ADMIN_TOKEN, "fleet", "new", None, "fresh text").await;
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 1);
}

thread_local! {
    static TRACED: std::cell::RefCell<Vec<String>> = const { std::cell::RefCell::new(Vec::new()) };
}
fn traced(event: rusqlite::trace::TraceEvent<'_>) {
    if let rusqlite::trace::TraceEvent::Stmt(stmt, _) = event {
        TRACED.with(|t| t.borrow_mut().push(stmt.sql().into_owned()));
    }
}
fn take_traced() -> Vec<String> {
    TRACED.with(|t| std::mem::take(&mut *t.borrow_mut()))
}
#[test]
fn dense_refresh_reads_only_changed_vectors_through_the_log() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let plan: String = db
        .query_row("EXPLAIN QUERY PLAN SELECT content_hash,vector FROM memory_vectors WHERE embedder_id='id' AND normalizer_version=1 ORDER BY content_hash", [], |r| r.get(3))
        .unwrap();
    assert!(plan.contains("memory_vectors_embedder"), "{plan}");
    let index = Arc::new(dense::Index::default());
    let bytes: Vec<_> = [1.0f32, 0.0].iter().flat_map(|f| f.to_le_bytes()).collect();
    for hash in ["one", "two"] {
        db.execute(
            "INSERT INTO memory_vectors VALUES(?1,'id',1,?2)",
            rusqlite::params![hash, &bytes],
        )
        .unwrap();
    }
    let first = index.refresh(&db, "id", 2).unwrap();
    assert_eq!(first.rows, 2);
    db.trace_v2(
        rusqlite::trace::TraceEventCodes::SQLITE_TRACE_STMT,
        Some(traced),
    );
    take_traced();
    // A commit that touches no vector reads no vector row.
    db.execute("INSERT INTO memory_meta VALUES('unrelated','x')", [])
        .unwrap();
    take_traced();
    let same = index.refresh(&db, "id", 2).unwrap();
    assert!(Arc::ptr_eq(&first, &same));
    let reads = take_traced();
    assert!(
        reads.iter().all(|sql| !sql.contains("FROM memory_vectors")),
        "{reads:?}"
    );
    // Another embedder's vector advances the log and still reads no vector.
    db.execute(
        "INSERT INTO memory_vectors VALUES('three','other',1,?1)",
        [&bytes],
    )
    .unwrap();
    take_traced();
    assert_eq!(index.refresh(&db, "id", 2).unwrap().rows, 2);
    let reads = take_traced();
    assert!(
        reads.iter().all(|sql| !sql.contains("FROM memory_vectors")),
        "{reads:?}"
    );
    db.execute(
        "INSERT INTO memory_vectors VALUES('three','id',1,?1)",
        [&bytes],
    )
    .unwrap();
    take_traced();
    let added = index.refresh(&db, "id", 2).unwrap();
    assert_eq!(added.hashes().len(), 3);
    let reads = take_traced();
    let vector_reads: Vec<_> = reads
        .iter()
        .filter(|sql| sql.contains("FROM memory_vectors"))
        .collect();
    assert_eq!(vector_reads.len(), 1, "{reads:?}");
    assert!(vector_reads[0].contains("content_hash="), "{reads:?}");
    db.execute("DELETE FROM memory_vectors WHERE content_hash='one'", [])
        .unwrap();
    let removed = index.refresh(&db, "id", 2).unwrap();
    assert_eq!(removed.hashes().len(), 2);
    assert!(!removed.hashes().contains("one"));
    db.trace_v2(rusqlite::trace::TraceEventCodes::empty(), None);
    // A matrix behind a trimmed log reloads in full instead of missing rows.
    db.execute("DELETE FROM memory_vector_log", []).unwrap();
    db.execute("DELETE FROM memory_vectors WHERE content_hash='two'", [])
        .unwrap();
    db.execute("DELETE FROM memory_vector_log", []).unwrap();
    let reloaded = index.refresh(&db, "id", 2).unwrap();
    assert_eq!(reloaded.hashes().len(), 1);
}

#[test]
fn dense_segments_preserve_retained_snapshots_and_score_with_scope_mask() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let index = Arc::new(dense::Index::default());
    let bytes: Vec<_> = [1.0f32, 0.0].iter().flat_map(|f| f.to_le_bytes()).collect();
    for (i, hash) in ["one", "two"].iter().enumerate() {
        db.execute(
            "INSERT INTO memory_vectors VALUES(?1,'id',1,?2)",
            rusqlite::params![hash, &bytes],
        )
        .unwrap();
        let m = index.refresh(&db, "id", 2).unwrap();
        assert_eq!(m.rows, i + 1);
    }
    let retained = index.current("id").unwrap();
    db.execute("DELETE FROM memory_vectors WHERE content_hash='one'", [])
        .unwrap();
    let next = index.refresh(&db, "id", 2).unwrap();
    assert_eq!(next.hashes().len(), 1);
    assert_eq!(retained.rows, 2);
    assert_eq!(
        retained
            .segments
            .iter()
            .map(|s| s.matrix.len())
            .sum::<usize>(),
        4
    );
}
#[test]
#[ignore = "explicit peak RSS measurement in a fresh process"]
fn dense_rebuild_peak_memory() {
    let dims: usize = std::env::var("S15_DIMS").unwrap().parse().unwrap();
    let rows = 20_000;
    let mut parts = Vec::new();
    for batch in 0..20 {
        let hashes = (0..1000)
            .map(|n| format!("{:064x}", batch * 1000 + n))
            .collect();
        let matrix = vec![1.0 / (dims as f32).sqrt(); 1000 * dims];
        parts.push(Arc::new(dense::Segment { hashes, matrix }));
    }
    let old = dense::Matrix {
        dimensions: dims,
        segments: parts,
        rows,
        ..dense::Matrix::default()
    };
    let start = std::time::Instant::now();
    let new = old.compact();
    assert_eq!(old.rows, new.rows);
    let rss = std::fs::read_to_string("/proc/self/status").unwrap();
    eprintln!(
        "S15 dense rebuild rows={rows} dimensions={dims} seconds={} {} retained_old_rows={}",
        start.elapsed().as_secs_f64(),
        rss.lines().find(|l| l.starts_with("VmHWM:")).unwrap(),
        old.rows
    );
    std::hint::black_box((old, new));
}

#[tokio::test]
async fn scheduler_and_hybrid_use_real_http_and_respect_read_grants() {
    let (endpoint, seen, task) = super::embedder_tests::fake("fake-digest").await;
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let runtime = h.store.as_mut().unwrap().retrieval.take().unwrap();
    let mut config = runtime.config.clone();
    config.endpoint = endpoint.clone();
    config.allowlist = vec![endpoint];
    h.store.as_mut().unwrap().retrieval =
        Some(embedder::Runtime::new(config, scan::bundled()).unwrap());
    h.store.as_mut().unwrap().pull_embeddings = false;
    // Startup loads the matrix before any write, as `start_retrieval` does.
    h.store.as_ref().unwrap().load_dense().await;
    h.put(ADMIN_TOKEN, "fleet", "one", None, "apple fruit")
        .await;
    h.put(ADMIN_TOKEN, "repo:hidden/r", "two", None, "secret banana")
        .await;
    h.store.as_ref().unwrap().embedding_tick().await;
    assert_eq!(seen.lock().unwrap().len(), 1);
    assert_eq!(
        h.store.as_ref().unwrap().dense.current(&id).unwrap().rows,
        2
    );
    let search = h
        .call(
            ADMIN_TOKEN,
            json!({"action": "search", "query": "unrelated"}),
        )
        .await;
    assert_eq!(search.body["retrieval"]["mode"], "hybrid");
    assert_eq!(search.body["matches"].as_array().unwrap().len(), 2);
    // `match_count` keeps its lexical meaning: nothing matched the words.
    assert_eq!(search.body["match_count"], 0);
    assert_eq!(search.body["matches"][0]["ranks"]["dense"], 1);
    command(
        &h,
        json!({"action": "grant", "principal": "agent:dot", "scope": "fleet", "rights": ["read"]}),
    )
    .await;
    let node = h
        .call(
            AGENT_TOKEN,
            json!({"action": "search", "query": "unrelated"}),
        )
        .await;
    assert_eq!(node.body["matches"].as_array().unwrap().len(), 1);
    assert_eq!(node.body["matches"][0]["scope"], "fleet");
    let before = seen.lock().unwrap().len();
    assert_eq!(
        h.call(
            AGENT_TOKEN,
            json!({"action": "search", "query": "private query", "scopes": ["repo:hidden/r"]})
        )
        .await
        .status,
        403
    );
    assert_eq!(seen.lock().unwrap().len(), before);
    task.abort();
}

#[test]
fn document_normalizer_is_content_addressed_and_excludes_unrelated_metadata() {
    let body = "---\ntitle: Fruit\nsummary: Food\ntags: [hidden-tag]\nwriter: hidden-writer\n---\nApple body";
    assert_eq!(
        embedding::document_text(body, 6000),
        "Fruit\nFood\nApple body"
    );
    assert_eq!(embedding::document_text("body", 6000), "\n\nbody");
    assert!(embedding::document_text(&"界".repeat(40000), 40000).len() <= 32768);
}
#[tokio::test]
async fn lexical_starts_no_scheduler_and_revival_requeues_collected_vector() {
    let mut h = Hub::with(scan::bundled(), None);
    let store = Arc::new(h.store.take().unwrap());
    store.start_embeddings();
    assert!(store.embedding_task.lock().unwrap().is_none());
    drop(store);
    h.open(scan::bundled(), None);
    let id = setup(&mut h);
    let put = h
        .put(ADMIN_TOKEN, "fleet", "one", None, "shared body")
        .await;
    fill(&h, &id).await;
    let retire = command(&h, json!({"action": "retire", "scope": "fleet", "card_id": "one", "base_revision": rev(&put), "base_epoch": h.epoch().await})).await;
    assert_eq!(retire.status, 200);
    assert_eq!(
        h.put(
            ADMIN_TOKEN,
            "fleet",
            "one",
            Some(rev(&retire)),
            "shared body"
        )
        .await
        .status,
        201
    );
    // The revival queued the collected text again inside its own transaction.
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT state FROM memory_embed_jobs WHERE embedder_id=?1",
            [&id],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "pending"
    );
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
}

#[tokio::test]
async fn pull_without_query_endpoint_never_reads_a_model_secret_or_sends_a_query() {
    let mut h = Hub::with(scan::bundled(), None);
    h.store.as_mut().unwrap().configure_retrieval(&json!({"retrieval": "hybrid", "embedder": {"backend": "remote", "pull": true, "shape": "api/embed", "name": "fake-model", "model_digest": "fake-digest", "dimensions": 2, "input_chars": 6000, "query_prefix_version": 1}})).unwrap();
    assert_eq!(
        h.store.as_ref().unwrap().retrieval_status()["reason"],
        "embedder-unavailable"
    );
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    let search = h
        .call(
            ADMIN_TOKEN,
            json!({"action": "search", "query": "ordinary"}),
        )
        .await;
    assert_eq!(search.status, 200);
    assert_eq!(search.body["retrieval"]["mode"], "lexical-fallback");
}

#[tokio::test]
async fn reindex_drains_multiple_pages_and_drop_preserves_other_embedder_rows() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.execute_batch("BEGIN").unwrap();
    for i in 0..270 {
        let hash = format!("{i:064x}");
        db.execute(
            "INSERT INTO memory_blobs VALUES(?1,?2,4,'test')",
            rusqlite::params![&hash, b"body"],
        )
        .unwrap();
        db.execute("INSERT INTO memory_cards VALUES('fleet',?1,?2,?3,'live','reviewed','hub:admin','admin','','','[]','test','test')", rusqlite::params![format!("card-{i}"),i+1,&hash]).unwrap();
        db.execute("INSERT INTO memory_card_revisions VALUES('fleet',?1,?2,?3,'reviewed','live','hub:admin',?2,NULL,'clean','test')", rusqlite::params![format!("card-{i}"),i+1,&hash]).unwrap();
    }
    db.execute_batch("COMMIT").unwrap();
    assert_eq!(command(&h, json!({"action": "reindex"})).await.status, 200);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_embed_jobs WHERE embedder_id=?1",
            [&id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        270
    );
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    let other = "b".repeat(64);
    let gone = "c".repeat(64);
    db.execute_batch("BEGIN").unwrap();
    db.execute(
        "INSERT INTO memory_vectors VALUES('other-hash',?1,1,?2)",
        rusqlite::params![&other, vec![0u8; 8]],
    )
    .unwrap();
    // More rows than one writer turn deletes.
    for i in 0..700 {
        db.execute(
            "INSERT INTO memory_vectors VALUES(?1,?2,1,?3)",
            rusqlite::params![format!("{i:064x}"), &gone, vec![0u8; 8]],
        )
        .unwrap();
    }
    db.execute_batch("COMMIT").unwrap();
    for bad in [
        json!(""),
        json!("not-an-id"),
        json!("B".repeat(64)),
        json!(7),
    ] {
        let refused = command(&h, json!({"action": "reindex", "drop": bad})).await;
        assert_eq!(refused.status, 422, "{refused:?}");
    }
    let active = command(&h, json!({"action": "reindex", "drop": id})).await;
    assert_eq!(active.status, 409);
    let dropped = command(&h, json!({"action": "reindex", "drop": gone})).await;
    assert_eq!(dropped.status, 200, "{dropped:?}");
    assert_eq!(dropped.body["vectors"], 700);
    assert_eq!(dropped.body["jobs"], 0);
    let count = |embedder: &str| {
        db.query_row(
            "SELECT count(*) FROM memory_vectors WHERE embedder_id=?1",
            [embedder],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
    };
    assert_eq!(count(&gone), 0);
    assert_eq!(count(&other), 1);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_embed_jobs WHERE embedder_id=?1",
            [&id],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        270
    );
}
#[test]
fn schema_one_upgrades_and_finished_jobs_expire_in_bounded_sweep() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch("DROP TABLE memory_retrieval_evals; DROP TABLE memory_evidence; DROP TABLE memory_proposals; DROP TABLE memory_vectors; DROP TABLE memory_vector_log; DROP TABLE memory_lineage; DROP TABLE memory_embed_jobs; DROP INDEX memory_cards_hash; PRAGMA user_version=1").unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        schema::USER_VERSION
    );
    db.execute("INSERT INTO memory_embed_jobs(content_hash,embedder_id,state,finished_ms) VALUES('old','id','done',0)", []).unwrap();
    db.execute("INSERT INTO memory_embed_jobs(content_hash,embedder_id,state,finished_ms) VALUES('pending','id','pending',0)", []).unwrap();
    writer::sweep(&db, 1_800_000_000_000).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM memory_embed_jobs", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[tokio::test]
async fn stopped_endpoint_during_initial_backfill_is_degraded_and_findable() {
    let mut h = Hub::with(scan::bundled(), None);
    setup(&mut h);
    h.store.as_mut().unwrap().pull_embeddings = false;
    h.put(ADMIN_TOKEN, "fleet", "one", None, "apple fruit")
        .await;
    h.store.as_ref().unwrap().embedding_tick().await;
    let search = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(search.status, 200);
    assert_eq!(search.body["matches"][0]["card_id"], "one");
    assert_eq!(search.body["retrieval"]["mode"], "lexical-fallback");
    assert_eq!(
        h.store.as_ref().unwrap().retrieval_status()["state"],
        "degraded"
    );
}

#[tokio::test]
async fn dense_writer_replays_tail_after_background_watermark_without_blocking_writes() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "one", None, "apple").await;
    fill(&h, &id).await;
    let dense = h.store.as_ref().unwrap().dense.clone();
    let gate = dense.pause_next();
    h.put(ADMIN_TOKEN, "fleet", "two", None, "pear").await;
    fill(&h, &id).await;
    gate.started.wait();
    struct Release(Arc<dense::Gate>);
    impl Drop for Release {
        fn drop(&mut self) {
            self.0.release.wait();
        }
    }
    let release = Release(gate);
    let retained = dense.current(&id).unwrap();
    // The compactor is paused. Both the card and its vector still commit.
    h.put(ADMIN_TOKEN, "fleet", "three", None, "banana").await;
    fill(&h, &id).await;
    drop(release);
    dense.wait_rebuild().await;
    let installed = dense.current(&id).unwrap();
    assert_eq!(installed.hashes().len(), 3);
    assert_eq!(retained.hashes().len(), 2);
    assert_eq!(
        installed.indexed_seq,
        h.store.as_ref().unwrap().index.current().indexed_seq
    );
    assert_eq!(installed.segments[0].hashes.len(), 2);
    assert_eq!(installed.segments.len(), 2);
}

#[tokio::test]
async fn dropping_embedder_during_rebuild_cannot_resurrect_its_matrix() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    h.put(ADMIN_TOKEN, "fleet", "one", None, "apple").await;
    fill(&h, &id).await;
    let dense = h.store.as_ref().unwrap().dense.clone();
    let gate = dense.pause_next();
    h.put(ADMIN_TOKEN, "fleet", "two", None, "pear").await;
    fill(&h, &id).await;
    gate.started.wait();
    struct Release(Arc<dense::Gate>);
    impl Drop for Release {
        fn drop(&mut self) {
            self.0.release.wait();
        }
    }
    let release = Release(gate);
    assert_eq!(
        command(&h, json!({"action": "reindex", "drop": id}))
            .await
            .status,
        409
    );
    // Switching to lexical is the way to drop it. The paused rebuild was
    // captured before the switch and cannot publish the matrix again.
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"retrieval": "lexical"}))
        .unwrap();
    let dropped = command(&h, json!({"action": "reindex", "drop": id})).await;
    drop(release);
    dense.wait_rebuild().await;
    assert_eq!(dropped.status, 200, "{dropped:?}");
    assert_eq!(dropped.body["vectors"], 2);
    assert!(dense.current(&id).is_none());
}

/// Fills one vector, then reopens the store the way a restart does.
async fn restarted_with_one_vector(h: &mut Hub) -> String {
    let id = setup(h);
    h.put(ADMIN_TOKEN, "fleet", "one", None, "searchable apple")
        .await;
    fill(h, &id).await;
    drop(h.store.take());
    h.open(scan::bundled(), None);
    setup(h)
}

#[tokio::test]
async fn slow_initial_dense_load_is_not_cut_off_by_the_reader_deadline() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = restarted_with_one_vector(&mut h).await;
    let store = h.store.as_ref().unwrap();
    // The reader pool rejects a load that outlasts READ_LIMIT. This is what
    // startup used to run, and it would have aborted `start_and_serve`.
    store
        .dense
        .delay_next_load(READ_LIMIT + Duration::from_millis(300));
    assert!(store.refresh_dense().await.is_err());
    store.dense.drop_id(&id);
    // The startup load runs on its own connection and has no such limit.
    store
        .dense
        .delay_next_load(READ_LIMIT + Duration::from_millis(300));
    assert!(store.load_dense().await);
    assert_eq!(store.dense.current(&id).unwrap().rows, 1);
    assert_eq!(store.retrieval_status()["state"], "ok");
}

#[tokio::test]
async fn failed_initial_dense_load_degrades_to_lexical_and_the_scheduler_retries() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = restarted_with_one_vector(&mut h).await;
    // A vector row of the wrong length, as a corrupt file would hold.
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.execute("UPDATE memory_vectors SET vector=x'0102'", [])
        .unwrap();
    // `start_retrieval` is what main calls. It returns instead of failing.
    let store_arc = Arc::new(h.store.take().unwrap());
    store_arc.start_retrieval().await;
    assert!(store_arc.dense.current(&id).is_none());
    let status = store_arc.retrieval_status();
    assert_eq!(status["state"], "degraded", "{status}");
    assert_eq!(status["effective"], "lexical-fallback", "{status}");
    assert_eq!(status["reason"], "dense-load-failed", "{status}");
    h.store = Arc::into_inner(store_arc);
    // Search keeps serving, lexically, and names the reason.
    let search = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(search.status, 200, "{search:?}");
    assert_eq!(search.body["retrieval"]["mode"], "lexical-fallback");
    assert_eq!(search.body["retrieval"]["reason"], "dense-load-failed");
    assert!(
        !search.body["matches"].as_array().unwrap().is_empty(),
        "{search:?}"
    );
    // The row is repaired. The next scheduler pass loads the matrix.
    db.execute("DELETE FROM memory_vectors", []).unwrap();
    let store = h.store.as_ref().unwrap();
    store.retry_dense().await;
    assert!(store.dense.current(&id).is_some());
    let status = store.retrieval_status();
    assert_ne!(status["state"], "degraded", "{status}");
    assert_ne!(status["reason"], "dense-load-failed", "{status}");
}

#[tokio::test]
async fn status_is_not_hybrid_while_no_matrix_was_ever_installed() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let store = h.store.as_ref().unwrap();
    // Nothing was written and nothing loaded, so search has no matrix.
    assert!(store.dense.current(&id).is_none());
    let status = store.retrieval_status();
    assert_eq!(status["effective"], "lexical", "{status}");
    assert_eq!(status["state"], "switching", "{status}");
    assert_eq!(status["reason"], "dense-not-loaded", "{status}");
    let search = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(search.body["retrieval"]["mode"], "lexical", "{search:?}");
    // The first load installs an empty matrix. Nothing can rank, so status
    // says lexical, as search does, and is not a fault.
    let store = h.store.as_ref().unwrap();
    assert!(store.load_dense().await);
    assert!(store.dense.current(&id).is_some());
    let status = store.retrieval_status();
    assert_eq!(status["effective"], "lexical", "{status}");
    assert_eq!(status["state"], "ok", "{status}");
}

#[tokio::test]
async fn a_slow_dense_retry_does_not_stall_a_write_or_its_reply() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = restarted_with_one_vector(&mut h).await;
    let store = h.store.as_ref().unwrap();
    assert!(store.dense.current(&id).is_none());
    let slow = std::time::Duration::from_millis(2500);
    store.dense.delay_next_load(slow);
    let started = std::time::Instant::now();
    let write = async {
        // Let the retry begin its scan before the write commits.
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let reply = h
            .put(ADMIN_TOKEN, "fleet", "two", None, "written during a retry")
            .await;
        (reply, started.elapsed())
    };
    let (loaded, (reply, waited)) = tokio::join!(store.load_dense(), write);
    assert!(loaded);
    assert_eq!(reply.status, 201, "{reply:?}");
    assert!(
        waited < slow / 2,
        "the write waited {waited:?} behind a {slow:?} load"
    );
    // The commit that ran during the scan is replayed once the matrix exists.
    assert_eq!(store.dense.current(&id).unwrap().rows, 1);
}

#[tokio::test]
async fn an_empty_matrix_never_sends_query_text_to_the_endpoint() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let store = h.store.as_ref().unwrap();
    // The endpoint is `127.0.0.1:1`, which refuses every connection.
    assert!(store.load_dense().await);
    assert_eq!(store.dense.current(&id).unwrap().rows, 0);
    for _ in 0..2 {
        let search = h
            .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
            .await;
        assert_eq!(search.status, 200, "{search:?}");
        assert_ne!(
            search.body["retrieval"]["reason"], "embedder-unavailable",
            "{search:?}"
        );
        assert_ne!(
            search.body["retrieval"]["degraded"],
            json!(true),
            "{search:?}"
        );
    }
    let status = store.retrieval_status();
    assert_ne!(status["state"], "degraded", "{status}");
    assert_ne!(status["reason"], "embedder-unavailable", "{status}");
}

#[tokio::test]
async fn a_vector_submitted_during_the_first_load_joins_the_matrix_without_a_later_write() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = restarted_with_one_vector(&mut h).await;
    let store = h.store.as_ref().unwrap();
    assert!(store.dense.current(&id).is_none());
    // The scan holds one read snapshot taken before the sleep begins.
    store
        .dense
        .delay_next_load(std::time::Duration::from_millis(1200));
    let submit = async {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        let put = h
            .put(ADMIN_TOKEN, "fleet", "two", None, "submitted during a load")
            .await;
        assert_eq!(put.status, 201, "{put:?}");
        let lease = command(
            &h,
            json!({"action": "embed_lease", "embedder_id": id, "max": 4}),
        )
        .await;
        let jobs = lease.body["jobs"].as_array().unwrap();
        assert_eq!(jobs.len(), 1, "{lease:?}");
        let vectors =
            json!([{"content_hash": jobs[0]["content_hash"], "vector": encoded(&[0.0, 1.0])}]);
        let result = command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await;
        assert_eq!(result.status, 200, "{result:?}");
    };
    let (loaded, ()) = tokio::join!(store.load_dense(), submit);
    assert!(loaded);
    // No commit follows. The matrix already holds both vectors.
    let matrix = store.dense.current(&id).unwrap();
    assert_eq!(matrix.rows, 2);
    assert_eq!(matrix.hashes().len(), 2);
    let status = store.retrieval_status();
    assert_eq!(status["effective"], "hybrid", "{status}");
    assert_eq!(status["state"], "ok", "{status}");
}

#[tokio::test]
async fn a_matrix_whose_rows_are_all_collected_reports_lexical_and_skips_the_endpoint() {
    let mut h = Hub::with(scan::bundled(), None);
    let id = setup(&mut h);
    let put = h
        .put(ADMIN_TOKEN, "fleet", "one", None, "searchable apple")
        .await;
    fill(&h, &id).await;
    let store = h.store.as_ref().unwrap();
    assert!(store.retrieval_status()["effective"] == "hybrid");
    // Retiring the only card collects its vector. The matrix keeps the dead row
    // until a rebuild compacts it.
    let retire = command(&h, json!({"action": "retire", "scope": "fleet", "card_id": "one", "base_revision": rev(&put), "base_epoch": h.epoch().await})).await;
    assert_eq!(retire.status, 200, "{retire:?}");
    store.refresh_dense().await.unwrap();
    // A background compaction may already have emptied the matrix. Either
    // form has no live row.
    assert!(!store.dense.current(&id).unwrap().has_live_rows());
    let status = store.retrieval_status();
    assert_eq!(status["effective"], "lexical", "{status}");
    assert_eq!(status["state"], "ok", "{status}");
    // The endpoint is `127.0.0.1:1`, which refuses every connection. A query
    // that reached it would come back degraded.
    let search = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "apple"}))
        .await;
    assert_eq!(search.status, 200, "{search:?}");
    assert_ne!(
        search.body["retrieval"]["degraded"],
        json!(true),
        "{search:?}"
    );
    assert_eq!(search.body["retrieval"]["mode"], "lexical", "{search:?}");
}

#[tokio::test]
async fn a_corpus_of_only_unembeddable_cards_reports_lexical_and_skips_the_endpoint() {
    let mut h = Hub::with(tiers(), None);
    let id = setup(&mut h);
    // Private text on a non-loopback endpoint is never embedded.
    h.put(ADMIN_TOKEN, "fleet", "lab", None, "labhost notes")
        .await;
    let store = h.store.as_ref().unwrap();
    assert!(store.load_dense().await);
    assert_eq!(store.dense.current(&id).unwrap().rows, 0);
    let status = store.retrieval_status();
    assert_eq!(status["effective"], "lexical", "{status}");
    assert_eq!(status["state"], "ok", "{status}");
    assert_eq!(status["coverage"]["eligible"], 0, "{status}");
    let search = h
        .call(ADMIN_TOKEN, json!({"action": "search", "query": "labhost"}))
        .await;
    assert_ne!(
        search.body["retrieval"]["degraded"],
        json!(true),
        "{search:?}"
    );
}

/// Clean, except text with `labhost` scans private, and text with `shifted`
/// scans private once the rules change.
struct Tiers(std::sync::atomic::AtomicBool);
impl scan::Scanner for Tiers {
    fn scan(&self, text: &str) -> scan::Scan {
        let moved = text.contains("shifted") && self.0.load(std::sync::atomic::Ordering::SeqCst);
        if text.contains("labhost") || moved {
            scan::Scan::Private {
                ruleset: "r1".into(),
                rules: vec!["private-ipv4".into()],
            }
        } else {
            scan::Scan::Clean {
                ruleset: "r1".into(),
            }
        }
    }
    fn describe(&self) -> (bool, Option<String>) {
        (true, Some("r1".into()))
    }
}
fn tiers() -> Arc<Tiers> {
    Arc::new(Tiers(std::sync::atomic::AtomicBool::new(false)))
}

#[tokio::test]
async fn push_mode_loopback_endpoint_queues_and_leases_private_cards() {
    let mut h = Hub::with(tiers(), None);
    let id = setup_with(
        &mut h,
        Some("http://127.0.0.1:1/api/embed"),
        json!({"pull": false}),
    );
    h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean apple")
        .await;
    h.put(ADMIN_TOKEN, "fleet", "lab", None, "labhost notes")
        .await;
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 2);
    // Reindex finds nothing more to queue: both were queued by their puts.
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    let jobs = lease.body["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 2, "{lease:?}");
    let mut states: Vec<&str> = jobs
        .iter()
        .map(|j| j["scan_state"].as_str().unwrap())
        .collect();
    states.sort();
    assert_eq!(states, ["clean", "private"]);
    let vectors: Vec<_> = jobs
        .iter()
        .map(|j| json!({"content_hash": j["content_hash"], "vector": encoded(&[1.0, 0.0])}))
        .collect();
    assert_eq!(command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await.status, 200);
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    let status = h.store.as_ref().unwrap().retrieval_status();
    assert_eq!(status["state"], "ok", "{status}");
    assert_eq!(
        status["coverage"],
        json!({"cards": 2, "eligible": 2, "unembeddable": 0, "total": 2})
    );
}

#[tokio::test]
async fn push_mode_non_loopback_endpoint_never_queues_or_leases_private_cards() {
    let mut h = Hub::with(tiers(), None);
    // A private-network address is not loopback, so private text stays home.
    let id = setup_with(
        &mut h,
        Some("http://10.0.0.5:1/api/embed"), // <!-- content-guard: allow private-ipv4 -->
        json!({"pull": false}),
    ); // <!-- content-guard: allow private-ipv4 -->
    assert!(!embedding::private_ok(
        &h.store.as_ref().unwrap().retrieval.as_ref().unwrap().config
    ));
    h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean apple")
        .await;
    h.put(ADMIN_TOKEN, "fleet", "lab", None, "labhost notes")
        .await;
    assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 1);
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
    let jobs = lease.body["jobs"].as_array().unwrap();
    assert_eq!(jobs.len(), 1, "{lease:?}");
    assert!(jobs[0]["text"].as_str().unwrap().contains("apple"));
    let vectors =
        json!([{"content_hash": jobs[0]["content_hash"], "vector": encoded(&[1.0, 0.0])}]);
    assert_eq!(command(&h, json!({"action": "embed_submit", "lease_id": lease.body["lease_id"], "model_digest": "fake-digest", "vectors": vectors})).await.status, 200);
    h.store.as_ref().unwrap().refresh_dense().await.unwrap();
    let status = h.store.as_ref().unwrap().retrieval_status();
    // Unembeddable, not failed: hybrid stays healthy.
    assert_eq!(status["effective"], "hybrid", "{status}");
    assert_eq!(status["state"], "ok");
    assert_eq!(
        status["coverage"],
        json!({"cards": 1, "eligible": 1, "unembeddable": 1, "total": 2})
    );
    assert_eq!(
        rows(
            &h,
            "SELECT count(*) FROM memory_embed_jobs WHERE state='failed'"
        )
        .await,
        0
    );
}

#[tokio::test]
async fn push_mode_lease_rescan_treats_private_by_endpoint() {
    for (endpoint, loopback) in [
        ("http://127.0.0.1:1/api/embed", true),
        ("http://10.0.0.5:1/api/embed", false), // <!-- content-guard: allow private-ipv4 -->
    ] {
        let rules = tiers();
        let mut h = Hub::with(rules.clone(), None);
        let id = setup_with(&mut h, Some(endpoint), json!({"pull": false}));
        h.store.as_ref().unwrap().load_dense().await;
        // Clean when stored, so the job is queued. New rules call it private.
        h.put(ADMIN_TOKEN, "fleet", "later", None, "shifted body")
            .await;
        rules.0.store(true, std::sync::atomic::Ordering::SeqCst);
        let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
        if loopback {
            let jobs = lease.body["jobs"].as_array().unwrap();
            assert_eq!(jobs.len(), 1, "{lease:?}");
            assert_eq!(jobs[0]["scan_state"], "private");
            assert_eq!(lease.body["parked"], 0);
        } else {
            assert_eq!(lease.body["jobs"], json!([]), "{lease:?}");
            assert_eq!(lease.body["parked"], 1);
            assert_eq!(
                rows(&h, "SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='content-private'").await,
                1
            );
            let status = h.store.as_ref().unwrap().retrieval_status();
            // The parked card counts as unembeddable and does not degrade hybrid.
            assert_eq!(status["state"], "ok", "{status}");
            assert_eq!(
                status["coverage"],
                json!({"cards": 0, "eligible": 0, "unembeddable": 1, "total": 1})
            );
            // `content-private` is a verdict about one card, not an endpoint fault.
            assert!(!super::embedding::TRANSIENT.contains(&"content-private"));
        }
    }
}

/// Every pull-mode shape: no query endpoint, a loopback one, and a
/// private-network one. The query endpoint never decides where documents go.
const PULL_SHAPES: [Option<&str>; 3] = [
    None,
    Some("http://127.0.0.1:1/api/embed"),
    Some("http://192.0.2.7:1/api/embed"),
];

#[tokio::test]
async fn pull_mode_never_queues_or_leases_private_cards_unless_pull_private_is_set() {
    for endpoint in PULL_SHAPES {
        let rules = tiers();
        let mut h = Hub::with(rules.clone(), None);
        let id = setup_with(&mut h, endpoint, json!({"pull": true}));
        assert!(!embedding::private_ok(
            &h.store.as_ref().unwrap().retrieval.as_ref().unwrap().config
        ));
        h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean apple")
            .await;
        h.put(ADMIN_TOKEN, "fleet", "lab", None, "labhost notes")
            .await;
        // Clean when stored, private under newer rules: the lease rescan parks it.
        h.put(ADMIN_TOKEN, "fleet", "later", None, "shifted body")
            .await;
        assert_eq!(
            rows(&h, "SELECT count(*) FROM memory_embed_jobs").await,
            2,
            "{endpoint:?}"
        );
        assert_eq!(
            command(&h, json!({"action": "reindex"})).await.body["queued"],
            0
        );
        rules.0.store(true, std::sync::atomic::Ordering::SeqCst);
        let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
        let jobs = lease.body["jobs"].as_array().unwrap();
        assert_eq!(jobs.len(), 1, "{endpoint:?} {lease:?}");
        assert!(jobs[0]["text"].as_str().unwrap().contains("apple"));
        assert_eq!(lease.body["parked"], 1);
        assert_eq!(
            rows(&h, "SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='content-private'").await,
            1
        );
        h.store.as_ref().unwrap().refresh_coverage().await;
        let status = h.store.as_ref().unwrap().retrieval_status();
        assert_eq!(status["pull_private"], false, "{status}");
        assert_eq!(
            status["coverage"],
            json!({"cards": 0, "eligible": 1, "unembeddable": 2, "total": 3}),
            "{endpoint:?} {status}"
        );
    }
}

#[tokio::test]
async fn pull_private_leases_private_cards_to_workers() {
    for endpoint in PULL_SHAPES {
        let rules = tiers();
        let mut h = Hub::with(rules.clone(), None);
        let id = setup_with(
            &mut h,
            endpoint,
            json!({"pull": true, "pull_private": true}),
        );
        h.put(ADMIN_TOKEN, "fleet", "clean", None, "clean apple")
            .await;
        h.put(ADMIN_TOKEN, "fleet", "lab", None, "labhost notes")
            .await;
        h.put(ADMIN_TOKEN, "fleet", "later", None, "shifted body")
            .await;
        assert_eq!(rows(&h, "SELECT count(*) FROM memory_embed_jobs").await, 3);
        rules.0.store(true, std::sync::atomic::Ordering::SeqCst);
        let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
        let jobs = lease.body["jobs"].as_array().unwrap();
        assert_eq!(jobs.len(), 3, "{endpoint:?} {lease:?}");
        let mut states: Vec<&str> = jobs
            .iter()
            .map(|j| j["scan_state"].as_str().unwrap())
            .collect();
        states.sort();
        assert_eq!(states, ["clean", "private", "private"]);
        assert_eq!(lease.body["parked"], 0);
        h.store.as_ref().unwrap().refresh_coverage().await;
        let status = h.store.as_ref().unwrap().retrieval_status();
        assert_eq!(status["pull_private"], true, "{status}");
        assert_eq!(
            status["coverage"],
            json!({"cards": 0, "eligible": 3, "unembeddable": 0, "total": 3})
        );
    }
}

#[tokio::test]
async fn pull_private_must_be_a_bool_and_needs_pull() {
    let mut h = Hub::with(tiers(), None);
    let store = h.store.as_mut().unwrap();
    let embedder = |extra: Json| {
        let mut e = json!({"backend": "remote", "shape": "api/embed", "name": "m", "model_digest": "d", "dimensions": 2, "input_chars": 6000, "query_prefix_version": 1, "pull": true});
        for (k, v) in extra.as_object().unwrap() {
            e[k] = v.clone();
        }
        json!({"retrieval": "hybrid", "embedder": e})
    };
    for bad in [json!("yes"), json!(1), json!(null)] {
        let error = store
            .configure_retrieval(&embedder(json!({"pull_private": bad})))
            .unwrap_err();
        assert!(
            error.contains("pull_private must be true or false"),
            "{error}"
        );
    }
    let error = store
        .configure_retrieval(&embedder(json!({"pull": false, "pull_private": true})))
        .unwrap_err();
    assert!(error.contains("requires memory.embedder.pull"), "{error}");
}

#[tokio::test]
async fn a_worker_reported_content_verdict_parks_the_job_at_once() {
    for reason in ["content-private", "content-blocked"] {
        let mut h = Hub::with(tiers(), None);
        let id = setup(&mut h);
        h.put(ADMIN_TOKEN, "fleet", "doc", None, "clean apple")
            .await;
        let lease = command(&h, json!({"action": "embed_lease", "embedder_id": id}))
            .await
            .body;
        let reply = command(&h, json!({"action": "embed_failed", "lease_id": lease["lease_id"], "failures": [{"content_hash": lease["jobs"][0]["content_hash"], "reason": reason}]})).await;
        assert_eq!(reply.status, 200, "{reply:?}");
        assert_eq!(reply.body["parked"], 1, "{reason}");
        assert_eq!(reply.body["released"], 0, "{reason}");
        assert_eq!(
            rows(&h, &format!("SELECT count(*) FROM memory_embed_jobs WHERE state='failed' AND last_error='{reason}' AND lease_id IS NULL")).await,
            1
        );
        // Parked, so it is never leased again, however long the wait.
        h.set_clock(h.now() + 7_200_000);
        let again = command(&h, json!({"action": "embed_lease", "embedder_id": id})).await;
        assert_eq!(again.body["jobs"], json!([]), "{reason}");
    }
}
