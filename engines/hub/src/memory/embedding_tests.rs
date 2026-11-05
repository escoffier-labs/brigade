use super::tests::*;
use super::*;
use base64::Engine;
use serde_json::json;
use std::sync::Arc;

fn setup(h: &mut Hub) -> String {
    let token = h.dir.join("embed.token");
    std::fs::write(&token, "fake-embed-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    h.store.as_mut().unwrap().configure_retrieval(&json!({"retrieval": "hybrid", "embedder": {"backend": "remote", "endpoint": "http://127.0.0.1:1/api/embed", "allowlist": ["http://127.0.0.1:1/api/embed"], "token_file": token, "shape": "api/embed", "name": "fake-model", "model_digest": "fake-digest", "dimensions": 2, "input_chars": 6000, "query_prefix_version": 1, "pull": true}})).unwrap();
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
fn encoded(v: &[f32]) -> String {
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
    let first = command(&h, json!({"action": "reindex"})).await;
    assert_eq!(first.body["queued"], 1);
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        0
    );
    fill(&h, &id).await;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
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
        command(&h, json!({"action": "reindex"})).await.body["queued"],
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
    assert_eq!(
        command(&h, json!({"action": "reindex", "drop": second}))
            .await
            .status,
        200
    );
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
#[test]
fn dense_segments_preserve_retained_snapshots_and_score_with_scope_mask() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let index = dense::Index::default();
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
    assert_eq!(next.rows, 1);
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
    assert_eq!(
        command(&h, json!({"action": "reindex"})).await.body["queued"],
        1
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
    db.execute(
        "INSERT INTO memory_vectors VALUES('other-hash','other-id',1,?1)",
        [vec![0u8; 8]],
    )
    .unwrap();
    assert_eq!(
        command(&h, json!({"action": "reindex", "drop": id}))
            .await
            .status,
        200
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_vectors WHERE embedder_id='other-id'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}
#[test]
fn schema_one_upgrades_and_finished_jobs_expire_in_bounded_sweep() {
    let db = rusqlite::Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch("DROP TABLE memory_vectors; DROP TABLE memory_lineage; DROP TABLE memory_embed_jobs; DROP INDEX memory_cards_hash; PRAGMA user_version=1").unwrap();
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
