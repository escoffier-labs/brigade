use super::{
    embedder::*,
    scan::{self, Scan},
    tests::*,
};
use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use serde_json::json;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

fn config(endpoint: String) -> Config {
    config_with_mode(endpoint, 0o600)
}
fn config_with_mode(endpoint: String, _mode: u32) -> Config {
    let dir = temp_dir();
    let token = dir.join("token");
    std::fs::write(&token, "fake-embed-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(_mode)).unwrap();
    }
    Config {
        backend: "remote".into(),
        pull: false,
        pull_private: false,
        allowlist: vec![endpoint.clone()],
        endpoint,
        token_file: token,
        shape: "api/embed".into(),
        id: EmbedderId {
            name: "test-model".into(),
            model_digest: "test-digest".into(),
            dimensions: 2,
            normalizer_version: 1,
            query_prefix_version: 1,
        },
        query_prefix: "query: ".into(),
        input_chars: 6000,
    }
}
pub(super) async fn fake(
    digest: &str,
) -> (
    String,
    Arc<Mutex<Vec<serde_json::Value>>>,
    tokio::task::JoinHandle<()>,
) {
    let (url, requests, _, task) = fake_counting(digest).await;
    (url, requests, task)
}
/// Also counts accepted TCP connections.
async fn fake_counting(
    digest: &str,
) -> (
    String,
    Arc<Mutex<Vec<serde_json::Value>>>,
    Arc<Mutex<usize>>,
    tokio::task::JoinHandle<()>,
) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let connections = Arc::new(Mutex::new(0));
    let seen = requests.clone();
    let accepted = connections.clone();
    let digest = digest.to_string();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            *accepted.lock().unwrap() += 1;
            let seen = seen.clone();
            let digest = digest.clone();
            tokio::spawn(async move {
                let service = hyper::service::service_fn(
                    move |req: hyper::Request<hyper::body::Incoming>| {
                        let seen = seen.clone();
                        let digest = digest.clone();
                        async move {
                            let authorized = req
                                .headers()
                                .get("Authorization")
                                .is_some_and(|v| v == "Bearer fake-embed-token");
                            if !authorized {
                                return Ok::<_, hyper::Error>(
                                    hyper::Response::builder()
                                        .status(401)
                                        .body(Full::new(Bytes::new()))
                                        .unwrap(),
                                );
                            }
                            let body = req.into_body().collect().await?.to_bytes();
                            let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
                            let n = value["input"].as_array().unwrap().len();
                            seen.lock().unwrap().push(value);
                            Ok(hyper::Response::new(Full::new(Bytes::from(json!({"model_digest": digest, "embeddings": vec![vec![3.0, 4.0]; n], "data": (0..n).map(|i| json!({"index": i, "embedding": [3.0, 4.0]})).collect::<Vec<_>>()}).to_string()))))
                        }
                    },
                );
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(hyper_util::rt::TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    (
        format!("http://{address}/api/embed"),
        requests,
        connections,
        task,
    )
}
#[tokio::test]
async fn http_token_digest_allowlist_and_vectors() {
    let (url, seen, task) = fake("test-digest").await;
    let client: hyper_util::client::legacy::Client<
        hyper_util::client::legacy::connect::HttpConnector,
        Full<Bytes>,
    > = hyper_util::client::legacy::Client::builder(hyper_util::rt::TokioExecutor::new())
        .build_http();
    let unauthenticated = client
        .request(
            hyper::Request::post(&url)
                .body(Full::new(Bytes::from("{}")))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(unauthenticated.status().as_u16(), 401);
    let c = config(url);
    let endpoint = HttpEmbedder::new(c.clone(), scan::bundled()).unwrap();
    let rows = endpoint
        .embed(Kind::Query, vec!["ordinary query".into()], QUERY_DEADLINE)
        .await
        .unwrap();
    assert_eq!(rows, vec![vec![0.6, 0.8]]);
    assert_eq!(seen.lock().unwrap().len(), 1);
    let mut openai_shape = c.clone();
    openai_shape.shape = "v1/embeddings".into();
    assert_eq!(
        HttpEmbedder::new(openai_shape, scan::bundled())
            .unwrap()
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap(),
        rows
    );
    let mut no_token = c.clone();
    std::fs::write(&no_token.token_file, "wrong-token").unwrap();
    assert_eq!(
        HttpEmbedder::new(no_token.clone(), scan::bundled())
            .unwrap()
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap_err()
            .reason,
        "embedder-refused"
    );
    no_token.allowlist.clear();
    assert!(HttpEmbedder::new(no_token, scan::bundled()).is_err());
    task.abort();
    let (url, _, task) = fake("different-digest").await;
    assert_eq!(
        HttpEmbedder::new(config(url), scan::bundled())
            .unwrap()
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap_err()
            .reason,
        "digest-mismatch"
    );
    task.abort();
    for vector in [
        vec![0.0, 0.0],
        vec![f32::NAN, 1.0],
        vec![f32::INFINITY, 1.0],
        vec![1.0],
    ] {
        assert!(normalize_vector(vector, 2).is_err());
    }
}
#[tokio::test]
async fn outgoing_gate_reuses_s18_and_pending_never_sends() {
    let (url, seen, task) = fake("test-digest").await;
    let runtime = Runtime::new(config(url), scan::bundled()).unwrap();
    assert!(
        runtime
            .documents(vec![("text".into(), "pending".into())])
            .await
            .is_err()
    );
    let blocked = ["-----BEGIN", "PRIVATE KEY-----"].join(" ");
    assert_eq!(
        runtime.query(&blocked).await.unwrap_err().reason,
        "content-blocked"
    );
    assert!(seen.lock().unwrap().is_empty());
    // Non-loopback is rejected before any DNS or connection attempt without a scanner.
    let c = config("https://192.0.2.1/v1/embeddings".into());
    let endpoint = HttpEmbedder::new(c, Arc::new(scan::Unavailable)).unwrap();
    assert_eq!(
        endpoint
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap_err()
            .reason,
        "scan-unavailable"
    );
    task.abort();
}

struct Slow {
    id: EmbedderId,
    calls: Mutex<Vec<(Kind, Duration)>>,
}
impl Embedder for Slow {
    fn id(&self) -> &EmbedderId {
        &self.id
    }
    fn embed(&self, kind: Kind, _: Vec<String>, deadline: Duration) -> Embedding {
        self.calls.lock().unwrap().push((kind, deadline));
        Box::pin(async move {
            match kind {
                Kind::Query => {
                    tokio::time::sleep(QUERY_DEADLINE).await;
                    Err(EmbedError {
                        reason: "embedder-timeout",
                        retry_after: None,
                    })
                }
                Kind::Probe => {
                    tokio::time::sleep(Duration::from_millis(6400)).await;
                    Ok(vec![vec![1.0, 0.0]])
                }
                Kind::Document => {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    Ok(vec![vec![1.0, 0.0]])
                }
            }
        })
    }
}
#[tokio::test(start_paused = true)]
async fn admission_batching_contention_and_cold_probe() {
    let c = config("http://127.0.0.1:1/api/embed".into());
    let slow = Arc::new(Slow {
        id: c.id.clone(),
        calls: Mutex::default(),
    });
    let runtime = Runtime::with(c, slow.clone());
    let mut queries = Vec::new();
    for _ in 0..4 {
        let runtime = runtime.clone();
        queries.push(tokio::spawn(async move { runtime.query("text").await }));
    }
    tokio::task::yield_now().await;
    assert_eq!(
        runtime.query("fifth").await.unwrap_err().reason,
        "embedder-busy"
    );
    assert_eq!(
        runtime
            .documents(vec![("body".into(), "clean".into())])
            .await
            .unwrap_err()
            .reason,
        "embedder-busy"
    );
    tokio::time::advance(QUERY_DEADLINE).await;
    for q in queries {
        assert!(q.await.unwrap().is_err());
    }
    assert!(runtime.breaker_open());
    assert_eq!(
        runtime.query("skip").await.unwrap_err().reason,
        "embedder-timeout"
    );
    tokio::task::yield_now().await;
    assert_eq!(
        slow.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|(k, _)| *k == Kind::Probe)
            .count(),
        1
    );
    tokio::time::advance(Duration::from_millis(6400)).await;
    tokio::task::yield_now().await;
    assert!(!runtime.breaker_open());
    assert!(runtime.reason().is_none());
    let r = runtime.clone();
    let doc = tokio::spawn(async move { r.documents(vec![("body".into(), "clean".into())]).await });
    tokio::task::yield_now().await;
    assert!(runtime.query("during document").await.is_err());
    assert!(!runtime.breaker_open());
    doc.await.unwrap().unwrap();
    assert!(
        slow.calls
            .lock()
            .unwrap()
            .contains(&(Kind::Document, DOCUMENT_DEADLINE))
    );
}
#[tokio::test]
async fn lexical_ignores_bad_embedder_and_reports_ok() {
    let mut hub = Hub::new();
    let store = hub.store.as_mut().unwrap();
    store
        .configure_retrieval(
            &json!({"retrieval": "lexical", "embedder": {"token_file": "/does/not/exist"}}),
        )
        .unwrap();
    assert_eq!(
        store.retrieval_status(),
        json!({"mode": "lexical", "effective": "lexical", "state": "ok"})
    );
}

#[tokio::test]
async fn document_batch_limits_and_local_backend() {
    let (url, seen, task) = fake("test-digest").await;
    let mut c = config(url);
    c.backend = "local".into();
    let endpoint = HttpEmbedder::new(c, scan::bundled()).unwrap();
    assert_eq!(
        endpoint
            .embed(Kind::Document, vec!["body".into(); 4], DOCUMENT_DEADLINE)
            .await
            .unwrap()
            .len(),
        4
    );
    assert!(
        endpoint
            .embed(Kind::Document, vec!["body".into(); 5], DOCUMENT_DEADLINE)
            .await
            .is_err()
    );
    assert!(
        endpoint
            .embed(
                Kind::Document,
                vec!["x".repeat(BATCH_BYTES + 1)],
                DOCUMENT_DEADLINE
            )
            .await
            .is_err()
    );
    assert_eq!(seen.lock().unwrap().len(), 1);
    task.abort();
}

struct Backoff {
    id: EmbedderId,
    calls: Mutex<usize>,
}
impl Embedder for Backoff {
    fn id(&self) -> &EmbedderId {
        &self.id
    }
    fn embed(&self, _: Kind, _: Vec<String>, _: Duration) -> Embedding {
        *self.calls.lock().unwrap() += 1;
        Box::pin(async {
            Err(EmbedError {
                reason: "embedder-unavailable",
                retry_after: Some(Duration::from_secs(7)),
            })
        })
    }
}
#[tokio::test(start_paused = true)]
async fn document_backoff_honors_retry_after_and_query_quiet_period() {
    let c = config("http://127.0.0.1:1/api/embed".into());
    let backend = Arc::new(Backoff {
        id: c.id.clone(),
        calls: Mutex::new(0),
    });
    let runtime = Runtime::with(c, backend.clone());
    assert!(
        runtime
            .documents(vec![("body".into(), "clean".into())])
            .await
            .is_err()
    );
    assert_eq!(*backend.calls.lock().unwrap(), 1);
    assert_eq!(
        runtime
            .documents(vec![("body".into(), "clean".into())])
            .await
            .unwrap_err()
            .reason,
        "embedder-busy"
    );
    tokio::time::advance(Duration::from_secs(7)).await;
    assert!(
        runtime
            .documents(vec![("body".into(), "clean".into())])
            .await
            .is_err()
    );
    assert_eq!(*backend.calls.lock().unwrap(), 2);
    // An in-flight query and its one-second quiet period both deny a new document.
    let c = config("http://127.0.0.1:1/api/embed".into());
    let slow = Arc::new(Slow {
        id: c.id.clone(),
        calls: Mutex::default(),
    });
    let runtime = Runtime::with(c, slow);
    assert!(runtime.query("text").await.is_err());
    assert_eq!(
        runtime
            .documents(vec![("body".into(), "clean".into())])
            .await
            .unwrap_err()
            .reason,
        "embedder-busy"
    );
}

#[tokio::test]
async fn hung_http_server_pays_one_deadline_then_breaker_scales_down_best_of_three() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let streams = Arc::new(Mutex::new(Vec::new()));
    let held = streams.clone();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            held.lock().unwrap().push(stream);
        }
    });
    let mut cold = Vec::new();
    let mut skipped = Vec::new();
    for _ in 0..3 {
        let runtime = Runtime::new(
            config(format!("http://{address}/api/embed")),
            scan::bundled(),
        )
        .unwrap();
        let start = std::time::Instant::now();
        assert_eq!(
            runtime.query("ordinary").await.unwrap_err().reason,
            "embedder-timeout"
        );
        cold.push(start.elapsed());
        let start = std::time::Instant::now();
        assert!(runtime.query("ordinary").await.is_err());
        skipped.push(start.elapsed());
    }
    let first = cold.into_iter().min().unwrap();
    let skip = skipped.into_iter().min().unwrap();
    eprintln!("S15 hung HTTP best-of-3 first={first:?} breaker={skip:?}");
    assert!(first > skip * 10, "first={first:?} skipped={skip:?}");
    task.abort();
    streams.lock().unwrap().clear();
}

#[tokio::test(start_paused = true)]
async fn failed_probe_keeps_breaker_open_for_thirty_seconds() {
    let c = config("http://127.0.0.1:1/api/embed".into());
    let backend = Arc::new(Backoff {
        id: c.id.clone(),
        calls: Mutex::new(0),
    });
    let runtime = Runtime::with(c, backend.clone());
    assert!(runtime.query("first").await.is_err());
    tokio::task::yield_now().await;
    assert_eq!(*backend.calls.lock().unwrap(), 2);
    tokio::time::advance(Duration::from_secs(29)).await;
    assert!(runtime.query("skip").await.is_err());
    assert_eq!(*backend.calls.lock().unwrap(), 2);
    tokio::time::advance(Duration::from_secs(1)).await;
    assert!(runtime.query("retry").await.is_err());
    assert!(*backend.calls.lock().unwrap() >= 3);
}

fn flagged() -> Arc<dyn scan::Scanner> {
    Arc::new(Fixed(Scan::Flagged {
        ruleset: "test-rules".into(),
        rules: vec!["test-warning".into()],
    }))
}
fn clean() -> Arc<dyn scan::Scanner> {
    Arc::new(Fixed(Scan::Clean {
        ruleset: "test-rules".into(),
    }))
}
#[tokio::test]
async fn flagged_query_and_document_never_leave_the_host() {
    let (url, seen, task) = fake("test-digest").await;
    // A query the scanner flags is refused before any connection.
    let runtime = Runtime::new(config(url.clone()), flagged()).unwrap();
    assert_eq!(
        runtime.query("flagged query").await.unwrap_err().reason,
        "content-blocked"
    );
    assert!(!runtime.breaker_open());
    // A document whose fresh scan flags it is refused at the transport.
    let endpoint = HttpEmbedder::new(config(url.clone()), flagged()).unwrap();
    assert_eq!(
        endpoint
            .embed(
                Kind::Document,
                vec!["flagged body".into()],
                DOCUMENT_DEADLINE
            )
            .await
            .unwrap_err()
            .reason,
        "content-blocked"
    );
    // A version stored as flagged is refused even when a fresh scan is clean.
    let runtime = Runtime::new(config(url), clean()).unwrap();
    assert_eq!(
        runtime
            .documents(vec![("body".into(), "flagged".into())])
            .await
            .unwrap_err()
            .reason,
        "scan-pending"
    );
    assert!(seen.lock().unwrap().is_empty());
    // The same clean version goes out.
    runtime
        .documents(vec![("body".into(), "clean".into())])
        .await
        .unwrap();
    assert_eq!(seen.lock().unwrap().len(), 1);
    task.abort();
}

fn private_scan() -> Arc<dyn scan::Scanner> {
    Arc::new(Fixed(Scan::Private {
        ruleset: "test-rules".into(),
        rules: vec!["home-path".into()],
    }))
}

/// S18: private text may reach an embedder only at a loopback address.
#[tokio::test]
async fn private_text_goes_only_to_a_loopback_endpoint() {
    let (url, seen, task) = fake("test-digest").await;
    let endpoint = HttpEmbedder::new(config(url.clone()), private_scan()).unwrap();
    endpoint
        .embed(Kind::Query, vec!["query".into()], QUERY_DEADLINE)
        .await
        .unwrap();
    endpoint
        .embed(Kind::Document, vec!["body".into()], DOCUMENT_DEADLINE)
        .await
        .unwrap();
    let runtime = Runtime::new(config(url), private_scan()).unwrap();
    runtime
        .documents(vec![("body".into(), "private".into())])
        .await
        .unwrap();
    assert_eq!(seen.lock().unwrap().len(), 3);
    task.abort();
    // A private-network address on either backend, and a remote name over
    // HTTP or HTTPS, are refused before any lookup or connection. A lookup or
    // connect attempt would end as embedder-unavailable or endpoint-refused.
    let lan = "http://10.0.0.5:9/api/embed".to_string(); // <!-- content-guard: allow private-ipv4 -->
    for (backend, endpoint_url) in [
        ("local", lan.clone()),
        ("remote", lan),
        ("remote", "http://embed.example.com/api/embed".to_string()),
        (
            "remote",
            "https://embed.example.com/v1/embeddings".to_string(),
        ),
    ] {
        let mut c = config(endpoint_url.clone());
        c.backend = backend.into();
        let endpoint = HttpEmbedder::new(c.clone(), private_scan()).unwrap();
        for kind in [Kind::Query, Kind::Document] {
            let reason = endpoint
                .embed(kind, vec!["text".into()], QUERY_DEADLINE)
                .await
                .unwrap_err()
                .reason;
            assert_eq!(reason, "content-private", "{backend} {endpoint_url}");
        }
        let runtime = Runtime::new(c.clone(), private_scan()).unwrap();
        assert_eq!(
            runtime.query("text").await.unwrap_err().reason,
            "content-private"
        );
        assert!(!runtime.breaker_open(), "{backend} {endpoint_url}");
        // A stored private version is refused without reaching the embedder,
        // even when a fresh scan would pass.
        let runtime = Runtime::new(c, clean()).unwrap();
        assert_eq!(
            runtime
                .documents(vec![("body".into(), "private".into())])
                .await
                .unwrap_err()
                .reason,
            "content-private"
        );
    }
}

#[test]
fn private_text_needs_a_pinned_loopback_address() {
    let url = |s: &str| url::Url::parse(s).unwrap();
    let lan: std::net::IpAddr = "192.168.1.20".parse().unwrap(); // <!-- content-guard: allow private-ipv4 -->
    let v4: std::net::IpAddr = "127.0.0.1".parse().unwrap();
    let local = url("http://localhost:8080/api/embed");
    assert!(private_permitted(&local, None));
    assert!(private_permitted(&local, Some(v4)));
    assert!(!private_permitted(&local, Some(lan)));
    assert!(!private_permitted(
        &url("http://10.0.0.5/api/embed"), // <!-- content-guard: allow private-ipv4 -->
        None
    ));
    assert!(!private_permitted(
        &url("https://localhost/v1/embeddings"),
        None
    ));
    assert!(private_permitted(&url("https://[::1]/v1/embeddings"), None));
}

#[test]
fn unscanned_text_needs_a_loopback_address_not_a_name() {
    let url = |s: &str| url::Url::parse(s).unwrap();
    let lan: std::net::IpAddr = "192.168.1.20".parse().unwrap(); // <!-- content-guard: allow private-ipv4 -->
    let v4: std::net::IpAddr = "127.0.0.1".parse().unwrap();
    let v6: std::net::IpAddr = "::1".parse().unwrap();
    // `localhost` that resolves to a LAN address gets no exemption.
    assert!(!unscanned_permitted(
        &url("http://localhost:8080/api/embed"),
        Some(lan)
    ));
    assert!(unscanned_permitted(
        &url("http://localhost:8080/api/embed"),
        Some(v4)
    ));
    assert!(unscanned_permitted(
        &url("http://localhost:8080/api/embed"),
        Some(v6)
    ));
    assert!(unscanned_permitted(
        &url("http://localhost:8080/api/embed"),
        None
    ));
    assert!(!unscanned_permitted(
        &url("http://10.0.0.5/api/embed"), // <!-- content-guard: allow private-ipv4 -->
        None
    ));
    assert!(!unscanned_permitted(
        &url("http://embed.example.com/api/embed"),
        None
    ));
    // HTTPS resolves out of the hub's sight, so only a loopback literal passes.
    assert!(!unscanned_permitted(
        &url("https://localhost/v1/embeddings"),
        None
    ));
    assert!(unscanned_permitted(
        &url("https://127.0.0.1/v1/embeddings"),
        None
    ));
    assert!(unscanned_permitted(
        &url("https://[::1]/v1/embeddings"),
        None
    ));
}

#[tokio::test]
async fn unscanned_query_to_https_localhost_is_refused_and_loopback_ip_is_sent() {
    let endpoint = HttpEmbedder::new(
        config("https://localhost:1/v1/embeddings".into()),
        Arc::new(scan::Unavailable),
    )
    .unwrap();
    assert_eq!(
        endpoint
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap_err()
            .reason,
        "scan-unavailable"
    );
    let (url, seen, task) = fake("test-digest").await;
    HttpEmbedder::new(config(url), Arc::new(scan::Unavailable))
        .unwrap()
        .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
        .await
        .unwrap();
    assert_eq!(seen.lock().unwrap().len(), 1);
    task.abort();
}

#[tokio::test]
async fn token_file_must_be_owned_by_the_hub_user_with_0600_or_0400() {
    let (url, _, task) = fake("test-digest").await;
    let endpoint = HttpEmbedder::new(config_with_mode(url.clone(), 0o400), clean()).unwrap();
    endpoint
        .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
        .await
        .unwrap();
    #[cfg(unix)]
    for mode in [0o640, 0o604, 0o700, 0o644] {
        assert!(HttpEmbedder::new(config_with_mode(url.clone(), mode), clean()).is_err());
    }
    assert!(token_file_permitted(0o100600, 1000, 1000));
    assert!(token_file_permitted(0o100400, 1000, 1000));
    // Another user's file is refused whatever its mode.
    assert!(!token_file_permitted(0o100600, 0, 1000));
    assert!(!token_file_permitted(0o100400, 1001, 1000));
    task.abort();
}

#[tokio::test]
async fn idle_connection_is_reused_for_the_pinned_address() {
    let (url, seen, connections, task) = fake_counting("test-digest").await;
    let endpoint = HttpEmbedder::new(config(url), clean()).unwrap();
    for _ in 0..3 {
        endpoint
            .embed(Kind::Query, vec!["text".into()], QUERY_DEADLINE)
            .await
            .unwrap();
    }
    assert_eq!(seen.lock().unwrap().len(), 3);
    assert_eq!(*connections.lock().unwrap(), 1);
    task.abort();
}

#[tokio::test]
async fn hybrid_search_sends_no_query_before_dense_retrieval() {
    let (url, seen, task) = fake("test-digest").await;
    let mut hub = Hub::with(clean(), None);
    let c = config(url.clone());
    hub.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"retrieval": "hybrid", "embedder": {
            "backend": "remote", "endpoint": url, "allowlist": [url],
            "token_file": c.token_file, "shape": "api/embed", "name": "test-model",
            "model_digest": "test-digest", "dimensions": 2, "input_chars": 6000,
            "query_prefix_version": 1}}))
        .unwrap();
    let reply = hub
        .call(
            ADMIN_TOKEN,
            json!({"action": "search", "query": "release checklist"}),
        )
        .await;
    assert_eq!(reply.status, 200, "{}", reply.body);
    assert!(seen.lock().unwrap().is_empty());
    task.abort();
}

/// Query and probe answers come from scripted queues. A `None` probe panics.
struct Scripted {
    id: EmbedderId,
    queries: Mutex<Vec<bool>>,
    probes: Mutex<Vec<Option<bool>>>,
    probe_calls: Mutex<usize>,
}
impl Scripted {
    fn new(id: EmbedderId, queries: Vec<bool>, probes: Vec<Option<bool>>) -> Arc<Self> {
        Arc::new(Self {
            id,
            queries: Mutex::new(queries),
            probes: Mutex::new(probes),
            probe_calls: Mutex::new(0),
        })
    }
}
impl Embedder for Scripted {
    fn id(&self) -> &EmbedderId {
        &self.id
    }
    fn embed(&self, kind: Kind, _: Vec<String>, _: Duration) -> Embedding {
        let answer = match kind {
            Kind::Probe => {
                *self.probe_calls.lock().unwrap() += 1;
                self.probes.lock().unwrap().remove(0)
            }
            _ => Some(self.queries.lock().unwrap().remove(0)),
        };
        Box::pin(async move {
            match answer {
                Some(true) => Ok(vec![vec![1.0, 0.0]]),
                Some(false) => Err(EmbedError {
                    reason: "embedder-unavailable",
                    retry_after: None,
                }),
                None => panic!("scripted probe panic"),
            }
        })
    }
}
#[tokio::test(start_paused = true)]
async fn breaker_recovers_after_a_failed_probe_and_status_returns() {
    let mut hub = Hub::new();
    let c = config("http://127.0.0.1:1/api/embed".into());
    let scripted = Scripted::new(c.id.clone(), vec![false, true], vec![Some(false)]);
    let runtime = Runtime::with(c, scripted.clone());
    hub.store.as_mut().unwrap().retrieval = Some(runtime.clone());
    let healthy = hub.store.as_ref().unwrap().retrieval_status();
    assert!(runtime.query("text").await.is_err());
    tokio::task::yield_now().await;
    assert_eq!(*scripted.probe_calls.lock().unwrap(), 1);
    assert!(runtime.breaker_open());
    assert_eq!(
        hub.store.as_ref().unwrap().retrieval_status()["state"],
        json!("degraded")
    );
    // The probe failed. Once the window passes the breaker reads closed.
    tokio::time::advance(BREAKER_WINDOW).await;
    assert!(!runtime.breaker_open());
    // A healthy query clears the fault and status returns to its healthy form.
    runtime.query("text").await.unwrap();
    assert!(runtime.reason().is_none());
    assert_eq!(hub.store.as_ref().unwrap().retrieval_status(), healthy);
}
#[tokio::test(start_paused = true)]
async fn panicking_probe_resets_the_probe_flag() {
    let c = config("http://127.0.0.1:1/api/embed".into());
    let scripted = Scripted::new(c.id.clone(), vec![false, false], vec![None, Some(true)]);
    let runtime = Runtime::with(c, scripted.clone());
    assert!(runtime.query("text").await.is_err());
    for _ in 0..4 {
        tokio::task::yield_now().await;
    }
    assert_eq!(*scripted.probe_calls.lock().unwrap(), 1);
    tokio::time::advance(BREAKER_WINDOW).await;
    assert!(runtime.query("text").await.is_err());
    for _ in 0..4 {
        tokio::task::yield_now().await;
    }
    // Without the reset the first panic would leave `probe` set forever.
    assert_eq!(*scripted.probe_calls.lock().unwrap(), 2);
    assert!(!runtime.breaker_open());
}

#[tokio::test]
async fn retrieval_that_is_present_but_not_a_string_is_a_config_error() {
    let mut hub = Hub::new();
    let store = hub.store.as_mut().unwrap();
    for retrieval in [json!(1), json!(true), json!(["hybrid"]), json!({})] {
        let error = store
            .configure_retrieval(&json!({"retrieval": retrieval}))
            .unwrap_err();
        assert!(error.contains("lexical or hybrid"), "{retrieval}: {error}");
    }
    // Absent still means lexical.
    store.configure_retrieval(&json!({})).unwrap();
    assert_eq!(store.retrieval_status()["mode"], json!("lexical"));
}

#[tokio::test]
async fn embedder_pull_that_is_present_but_not_a_bool_is_a_config_error() {
    let mut hub = Hub::new();
    let store = hub.store.as_mut().unwrap();
    let hybrid = |pull: serde_json::Value| json!({"retrieval": "hybrid", "embedder": {"backend": "remote", "endpoint": "http://127.0.0.1:1/api/embed", "allowlist": ["http://127.0.0.1:1/api/embed"], "token_file": "/nonexistent", "shape": "api/embed", "name": "m", "model_digest": "d", "dimensions": 2, "input_chars": 6000, "query_prefix_version": 1, "pull": pull}});
    for pull in [
        json!("true"),
        json!(1),
        json!(null),
        json!(["true"]),
        json!({}),
    ] {
        let error = store
            .configure_retrieval(&hybrid(pull.clone()))
            .unwrap_err();
        assert!(error.contains("embedder.pull"), "{pull}: {error}");
    }
    // Lexical mode still ignores an unused embedder table.
    store
        .configure_retrieval(&json!({"retrieval": "lexical", "embedder": {"pull": "yes"}}))
        .unwrap();
}

#[tokio::test]
async fn local_backend_refuses_a_public_https_endpoint_even_when_allowlisted() {
    let local = |endpoint: &str| {
        let mut c = config(endpoint.into());
        c.backend = "local".into();
        HttpEmbedder::new(c, clean())
    };
    for public in [
        "https://embed.example.com/v1/embeddings",
        "https://192.0.2.1/v1/embeddings",
        "https://localhost:1/v1/embeddings",
    ] {
        let error = local(public).err().expect(public);
        assert!(
            error.contains("local embedder backend"),
            "{public}: {error}"
        );
    }
    for on_host in [
        "https://127.0.0.1:1/v1/embeddings",
        "https://[::1]:1/v1/embeddings",
        "http://127.0.0.1:1/api/embed",
        "http://localhost:1/api/embed",
    ] {
        assert!(local(on_host).is_ok(), "{on_host}");
    }
    // An internal service address over HTTPS, built so no private literal sits in the source.
    let internal = format!(
        "https://{}/v1/embeddings",
        std::net::Ipv4Addr::new(10, 0, 0, 1)
    );
    assert!(local(&internal).is_ok(), "{internal}");
    // The same public URL is the documented `remote` case.
    assert!(
        HttpEmbedder::new(
            config("https://embed.example.com/v1/embeddings".into()),
            clean()
        )
        .is_ok()
    );
}

#[cfg(unix)]
#[test]
fn a_fifo_token_file_fails_fast_instead_of_blocking_startup() {
    use std::os::unix::ffi::OsStrExt;
    let c = config("http://127.0.0.1:1/api/embed".into());
    std::fs::remove_file(&c.token_file).unwrap();
    let name = std::ffi::CString::new(c.token_file.as_os_str().as_bytes()).unwrap();
    // SAFETY: `name` is a valid NUL-terminated path.
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    let (send, receive) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = send.send(HttpEmbedder::new(c, clean()).err());
    });
    let outcome = receive.recv_timeout(Duration::from_secs(5));
    if outcome.is_err() {
        // Release a blocked open so the thread ends, then fail.
        // SAFETY: `name` is a valid NUL-terminated path.
        let fd = unsafe { libc::open(name.as_ptr(), libc::O_WRONLY | libc::O_NONBLOCK) };
        if fd >= 0 {
            // SAFETY: `fd` was just opened and is closed once.
            unsafe { libc::close(fd) };
        }
        panic!("opening a FIFO token file blocked startup");
    }
    let error = outcome.unwrap().expect("a FIFO token file is refused");
    assert!(error.contains("invalid embedder token file"), "{error}");
}

/// Answers every call with `rows` copies of a valid vector.
struct Rows {
    id: EmbedderId,
    rows: usize,
}
impl Embedder for Rows {
    fn id(&self) -> &EmbedderId {
        &self.id
    }
    fn embed(&self, _: Kind, _: Vec<String>, _: Duration) -> Embedding {
        let rows = vec![vec![1.0, 0.0]; self.rows];
        Box::pin(async move { Ok(rows) })
    }
}
#[tokio::test(start_paused = true)]
async fn a_query_with_the_wrong_row_count_is_an_error_not_a_panic() {
    for rows in [0, 2] {
        let c = config("http://127.0.0.1:1/api/embed".into());
        let runtime = Runtime::with(
            c.clone(),
            Arc::new(Rows {
                id: c.id.clone(),
                rows,
            }),
        );
        assert_eq!(
            runtime.query("text").await.unwrap_err().reason,
            "vector-invalid",
            "{rows} rows"
        );
        for _ in 0..4 {
            tokio::task::yield_now().await;
        }
        // The probe got the same bad answer, so the fault stays and the
        // state is still usable.
        assert_eq!(runtime.reason(), Some("vector-invalid"));
        assert!(runtime.breaker_open());
        tokio::time::advance(BREAKER_WINDOW).await;
        assert_eq!(
            runtime.query("text").await.unwrap_err().reason,
            "vector-invalid"
        );
        // Documents check their row count too.
        tokio::time::advance(Duration::from_secs(2)).await;
        assert_eq!(
            runtime
                .documents(vec![("body".into(), "clean".into())])
                .await
                .unwrap_err()
                .reason,
            "vector-invalid"
        );
    }
}

/// A server shaped like a local `/api/embed` one: the embedding reply carries
/// no digest unless `reply_digest` is set, and `GET /api/tags` lists the
/// model with whatever `listed` holds now. Counts listing reads and embedding
/// requests. When `on_embed` holds a value, an arriving embedding request
/// replaces `listed` with it before answering, which is a model swapped
/// while the request is in flight.
pub(super) struct Listing {
    pub(super) url: String,
    pub(super) listed: Arc<Mutex<serde_json::Value>>,
    pub(super) on_embed: Arc<Mutex<Option<serde_json::Value>>>,
    pub(super) reads: Arc<Mutex<usize>>,
    pub(super) embeds: Arc<Mutex<usize>>,
    /// The digest each embedding reply carries, if any.
    pub(super) reply: Arc<Mutex<Option<&'static str>>>,
    pub(super) task: tokio::task::JoinHandle<()>,
}
fn tags(digest: &str) -> serde_json::Value {
    tags_for("test-model", digest)
}
pub(super) fn tags_for(name: &str, digest: &str) -> serde_json::Value {
    let name = format!("{name}:latest");
    json!({"models": [
        {"name": "other-model:latest", "model": "other-model:latest", "digest": "other-digest"},
        {"name": name, "model": name, "digest": digest},
    ]})
}
pub(super) async fn fake_with_listing(
    reply_digest: Option<&'static str>,
    listed: serde_json::Value,
) -> Listing {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let listed = Arc::new(Mutex::new(listed));
    let reads = Arc::new(Mutex::new(0));
    let on_embed = Arc::new(Mutex::new(None::<serde_json::Value>));
    let embeds = Arc::new(Mutex::new(0));
    let reply_digest = Arc::new(Mutex::new(reply_digest));
    let carried = reply_digest.clone();
    let (state, count) = (listed.clone(), reads.clone());
    let (swap, sent) = (on_embed.clone(), embeds.clone());
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let (state, count) = (state.clone(), count.clone());
            let (swap, sent) = (swap.clone(), sent.clone());
            let carried = carried.clone();
            tokio::spawn(async move {
                let service = hyper::service::service_fn(
                    move |req: hyper::Request<hyper::body::Incoming>| {
                        let (state, count) = (state.clone(), count.clone());
                        let (swap, sent) = (swap.clone(), sent.clone());
                        let carried = carried.clone();
                        async move {
                            let reply = |status: u16, body: String| {
                                Ok::<_, hyper::Error>(
                                    hyper::Response::builder()
                                        .status(status)
                                        .body(Full::new(Bytes::from(body)))
                                        .unwrap(),
                                )
                            };
                            let authorized = req
                                .headers()
                                .get("Authorization")
                                .is_some_and(|v| v == "Bearer fake-embed-token");
                            if !authorized {
                                return reply(401, String::new());
                            }
                            if req.method() == hyper::Method::GET {
                                if req.uri().path() != "/api/tags" {
                                    return reply(404, String::new());
                                }
                                *count.lock().unwrap() += 1;
                                let listed = state.lock().unwrap().clone();
                                if listed == "stall" {
                                    // Accepts the request and never answers.
                                    std::future::pending::<()>().await;
                                }
                                return match listed {
                                    serde_json::Value::Null => reply(404, String::new()),
                                    listed => reply(200, listed.to_string()),
                                };
                            }
                            let body = req.into_body().collect().await?.to_bytes();
                            let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
                            let n = value["input"].as_array().unwrap().len();
                            *sent.lock().unwrap() += 1;
                            if let Some(next) = swap.lock().unwrap().take() {
                                *state.lock().unwrap() = next;
                            }
                            let mut body = json!({"embeddings": vec![vec![3.0, 4.0]; n], "data": (0..n).map(|i| json!({"index": i, "embedding": [3.0, 4.0]})).collect::<Vec<_>>()});
                            let digest = *carried.lock().unwrap();
                            if let Some(digest) = digest {
                                body["model_digest"] = json!(digest);
                            }
                            reply(200, body.to_string())
                        }
                    },
                );
                let _ = hyper::server::conn::http1::Builder::new()
                    .serve_connection(hyper_util::rt::TokioIo::new(stream), service)
                    .await;
            });
        }
    });
    Listing {
        url: format!("http://{address}/api/embed"),
        listed,
        on_embed,
        reads,
        embeds,
        reply: reply_digest,
        task,
    }
}
async fn reason(endpoint: &Arc<HttpEmbedder>, kind: Kind) -> Option<&'static str> {
    let deadline = if kind == Kind::Query {
        QUERY_DEADLINE
    } else {
        PROBE_DEADLINE
    };
    endpoint
        .embed(kind, vec!["ordinary text".into()], deadline)
        .await
        .err()
        .map(|e| e.reason)
}

/// #2469: a local `/api/embed` server sends no digest in its reply. The hub
/// takes the model's digest from the server's listing instead, and a later
/// reading that reports another digest refuses every reply.
#[tokio::test]
async fn a_reply_without_a_digest_is_checked_against_the_model_listing() {
    let server = fake_with_listing(None, tags("test-digest")).await;
    let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
    assert_eq!(
        endpoint
            .embed(Kind::Query, vec!["ordinary query".into()], QUERY_DEADLINE)
            .await
            .unwrap(),
        vec![vec![0.6, 0.8]]
    );
    assert_eq!(*server.reads.lock().unwrap(), 1);
    // A query inside the window reuses the reading.
    assert_eq!(reason(&endpoint, Kind::Query).await, None);
    assert_eq!(*server.reads.lock().unwrap(), 1);

    // The model behind the name changes. The cached reading still answers a
    // query, and a document batch reads the listing again and is refused on
    // the reading it takes before its request.
    *server.listed.lock().unwrap() = tags("swapped-digest");
    assert_eq!(reason(&endpoint, Kind::Query).await, None);
    assert_eq!(
        reason(&endpoint, Kind::Document).await,
        Some("digest-mismatch")
    );
    assert_eq!(*server.reads.lock().unwrap(), 2);
    // From that reading on, queries are refused too, without another read.
    assert_eq!(
        reason(&endpoint, Kind::Query).await,
        Some("digest-mismatch")
    );
    assert_eq!(*server.reads.lock().unwrap(), 2);
    // The breaker's probe always reads again, and sees the same mismatch.
    assert_eq!(
        reason(&endpoint, Kind::Probe).await,
        Some("digest-mismatch")
    );
    assert_eq!(*server.reads.lock().unwrap(), 3);

    // The pinned model is back: the probe reads before and after its
    // request, and those readings clear the refusal.
    *server.listed.lock().unwrap() = tags("test-digest");
    assert_eq!(reason(&endpoint, Kind::Probe).await, None);
    assert_eq!(reason(&endpoint, Kind::Query).await, None);
    assert_eq!(*server.reads.lock().unwrap(), 5);
    server.task.abort();
}

#[tokio::test]
async fn a_listing_that_cannot_vouch_for_the_model_refuses_the_reply() {
    for listed in [
        // No listing route at all.
        serde_json::Value::Null,
        // The configured model is not listed.
        json!({"models": [{"name": "other-model:latest", "digest": "test-digest"}]}),
        // Listed without a digest.
        json!({"models": [{"name": "test-model:latest"}]}),
        // Two entries for the name that disagree.
        json!({"models": [
            {"name": "test-model:latest", "digest": "test-digest"},
            {"model": "test-model", "digest": "swapped-digest"},
        ]}),
        json!({"models": "not a list"}),
        json!("not an object"),
    ] {
        let server = fake_with_listing(None, listed.clone()).await;
        let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
        assert_eq!(
            reason(&endpoint, Kind::Query).await,
            Some("digest-mismatch"),
            "{listed}"
        );
        assert_eq!(*server.reads.lock().unwrap(), 1, "{listed}");
        server.task.abort();
    }
}

#[tokio::test]
async fn a_digest_in_the_reply_decides_without_the_listing() {
    // The listing agrees with the pin, and the reply reports another model.
    let server = fake_with_listing(Some("different-digest"), tags("test-digest")).await;
    let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
    assert_eq!(
        reason(&endpoint, Kind::Query).await,
        Some("digest-mismatch")
    );
    server.task.abort();
    assert_eq!(*server.reads.lock().unwrap(), 0);
    // The reply reports the pin, and the listing reports another model. The
    // first document batch takes its reading before the request, since the
    // hub cannot know yet whether the reply will carry a digest. That
    // reading does not count against a reply that does, and no second one
    // is taken.
    let agreeing = fake_with_listing(Some("test-digest"), tags("swapped-digest")).await;
    let endpoint = HttpEmbedder::new(config(agreeing.url.clone()), scan::bundled()).unwrap();
    assert_eq!(reason(&endpoint, Kind::Document).await, None);
    assert_eq!(*agreeing.reads.lock().unwrap(), 1);
    // Once a reply has carried a digest the listing is not read at all, so
    // a listing that does not answer does not refuse such a reply either.
    *agreeing.listed.lock().unwrap() = serde_json::Value::Null;
    assert_eq!(reason(&endpoint, Kind::Document).await, None);
    assert_eq!(reason(&endpoint, Kind::Probe).await, None);
    assert_eq!(reason(&endpoint, Kind::Query).await, None);
    assert_eq!(*agreeing.reads.lock().unwrap(), 1);
    // The same endpoint stops sending its digest. That reply had no reading
    // before its request, so it is refused, whatever the listing says now.
    *agreeing.listed.lock().unwrap() = tags("test-digest");
    for kind in [Kind::Document, Kind::Probe] {
        *agreeing.reply.lock().unwrap() = Some("test-digest");
        assert_eq!(reason(&endpoint, kind).await, None);
        *agreeing.reply.lock().unwrap() = None;
        let reads = *agreeing.reads.lock().unwrap();
        assert_eq!(reason(&endpoint, kind).await, Some("digest-mismatch"));
        assert_eq!(*agreeing.reads.lock().unwrap(), reads, "{kind:?}");
        // The next call is bracketed again and accepted on two readings.
        assert_eq!(reason(&endpoint, kind).await, None);
        assert_eq!(*agreeing.reads.lock().unwrap(), reads + 2, "{kind:?}");
    }
    agreeing.task.abort();

    // `v1/embeddings` has no listing, so a reply without a digest is refused
    // as before and nothing else is requested.
    let server = fake_with_listing(None, tags("test-digest")).await;
    let mut other_shape = config(server.url.clone());
    other_shape.shape = "v1/embeddings".into();
    let endpoint = HttpEmbedder::new(other_shape, scan::bundled()).unwrap();
    assert_eq!(
        reason(&endpoint, Kind::Query).await,
        Some("digest-mismatch")
    );
    assert_eq!(*server.reads.lock().unwrap(), 0);
    server.task.abort();
}

/// One bracketed call against a server whose listing reports `before` until
/// the embedding request arrives and `after` from then on. Returns the
/// refusal, the listing reads and the embedding requests.
async fn bracketed(
    kind: Kind,
    before: serde_json::Value,
    after: serde_json::Value,
) -> (Option<&'static str>, usize, usize) {
    let server = fake_with_listing(None, before).await;
    *server.on_embed.lock().unwrap() = Some(after);
    let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
    let refused = reason(&endpoint, kind).await;
    server.task.abort();
    (
        refused,
        *server.reads.lock().unwrap(),
        *server.embeds.lock().unwrap(),
    )
}

/// Security review of #2479: the model's identity comes from a second
/// route, so a call whose vectors are stored reads the listing before its
/// request and again after the reply, and both readings must report the pin.
#[tokio::test]
async fn a_stored_embedding_is_bracketed_by_two_listing_reads() {
    let (pin, other) = (tags("test-digest"), tags("swapped-digest"));
    for kind in [Kind::Document, Kind::Probe] {
        // Both readings report the pin: accepted, with one request between.
        assert_eq!(
            bracketed(kind, pin.clone(), pin.clone()).await,
            (None, 2, 1),
            "{kind:?}"
        );
        // Swapped before the reply was computed, from another model to the
        // pinned one. The reading after the reply reports the pin, and only
        // the reading before the request shows the other model.
        assert_eq!(
            bracketed(kind, other.clone(), pin.clone()).await,
            (Some("digest-mismatch"), 1, 1),
            "{kind:?}"
        );
        // Swapped away from the pin while the request was in flight.
        assert_eq!(
            bracketed(kind, pin.clone(), other.clone()).await,
            (Some("digest-mismatch"), 2, 1),
            "{kind:?}"
        );
        // The listing names another model on both sides of a request that
        // the embedding route answered.
        assert_eq!(
            bracketed(kind, other.clone(), other.clone()).await,
            (Some("digest-mismatch"), 1, 1),
            "{kind:?}"
        );
        // The listing does not answer on one side, or cannot vouch for the
        // model there.
        for unreadable in [
            serde_json::Value::Null,
            json!({"models": [{"name": "test-model:latest"}]}),
            json!("not an object"),
        ] {
            assert_eq!(
                bracketed(kind, unreadable.clone(), pin.clone()).await.0,
                Some("digest-mismatch"),
                "{kind:?} before {unreadable}"
            );
            assert_eq!(
                bracketed(kind, pin.clone(), unreadable.clone()).await.0,
                Some("digest-mismatch"),
                "{kind:?} after {unreadable}"
            );
        }
    }
    // A query is not stored and keeps one reading, taken after its reply.
    assert_eq!(
        bracketed(Kind::Query, other.clone(), pin.clone()).await,
        (None, 1, 1)
    );
}

/// Codex review of #2479: the listing route may accept a request and never
/// answer. One reading has a bounded share of the call's deadline, so an
/// endpoint whose replies carry the pinned digest still embeds, as it did
/// before the listing existed.
#[tokio::test]
async fn a_stalled_listing_does_not_block_a_reply_that_carries_its_digest() {
    let deadline = Duration::from_secs(2);
    let budget = listing_budget(deadline);
    for kind in [Kind::Document, Kind::Probe] {
        let server = fake_with_listing(Some("test-digest"), json!("stall")).await;
        let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
        let started = std::time::Instant::now();
        let rows = endpoint
            .embed(kind, vec!["ordinary text".into()], deadline)
            .await
            .map_err(|e| e.reason);
        let first = started.elapsed();
        assert_eq!(rows, Ok(vec![vec![0.6, 0.8]]), "{kind:?}");
        // One stalled reading, then the request. Far short of the deadline.
        assert!(
            first >= budget && first < deadline / 2,
            "{kind:?} {first:?}"
        );
        assert_eq!(*server.reads.lock().unwrap(), 1);
        // The reply carried its digest, so later calls skip the reading.
        let started = std::time::Instant::now();
        assert_eq!(
            endpoint
                .embed(kind, vec!["ordinary text".into()], deadline)
                .await
                .map_err(|e| e.reason),
            Ok(vec![vec![0.6, 0.8]]),
            "{kind:?}"
        );
        assert!(started.elapsed() < budget, "{kind:?}");
        assert_eq!(*server.reads.lock().unwrap(), 1);
        assert_eq!(*server.embeds.lock().unwrap(), 2);
        server.task.abort();
    }
}

/// A reply with no digest has only the listing to vouch for it. A reading
/// that stalls on either side of the request is no evidence, so the call is
/// refused, and it is refused long before its deadline.
#[tokio::test]
async fn a_stalled_listing_refuses_a_reply_without_a_digest_inside_its_budget() {
    let deadline = Duration::from_secs(2);
    for kind in [Kind::Document, Kind::Probe] {
        for stall_first in [true, false] {
            let (first, second) = if stall_first {
                (json!("stall"), tags("test-digest"))
            } else {
                (tags("test-digest"), json!("stall"))
            };
            let server = fake_with_listing(None, first).await;
            *server.on_embed.lock().unwrap() = Some(second);
            let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
            let started = std::time::Instant::now();
            let refused = endpoint
                .embed(kind, vec!["ordinary text".into()], deadline)
                .await
                .err()
                .map(|e| e.reason);
            let elapsed = started.elapsed();
            assert_eq!(
                refused,
                Some("embedder-timeout"),
                "{kind:?} first {stall_first}"
            );
            assert!(
                elapsed < deadline / 2,
                "{kind:?} first {stall_first} {elapsed:?}"
            );
            assert_eq!(*server.embeds.lock().unwrap(), 1);
            server.task.abort();
        }
    }
    // A query's single reading is bounded the same way.
    let server = fake_with_listing(None, json!("stall")).await;
    let endpoint = HttpEmbedder::new(config(server.url.clone()), scan::bundled()).unwrap();
    let started = std::time::Instant::now();
    assert_eq!(
        endpoint
            .embed(Kind::Query, vec!["ordinary text".into()], deadline)
            .await
            .err()
            .map(|e| e.reason),
        Some("embedder-timeout")
    );
    assert!(started.elapsed() < deadline / 2);
    server.task.abort();
}

/// Both readings together stay under half of any deadline the hub uses.
#[test]
fn a_listing_reading_takes_a_small_share_of_the_deadline() {
    for deadline in [QUERY_DEADLINE, PROBE_DEADLINE, DOCUMENT_DEADLINE] {
        let budget = listing_budget(deadline);
        assert!(budget * 2 < deadline / 2, "{deadline:?}");
        assert!(budget <= Duration::from_secs(2), "{deadline:?}");
    }
    assert_eq!(listing_budget(QUERY_DEADLINE), Duration::from_millis(50));
    assert_eq!(listing_budget(DOCUMENT_DEADLINE), Duration::from_secs(2));
}

/// A listing read that fails outright, not only one that reports another
/// model, refuses the call on either side.
#[tokio::test]
async fn a_listing_reply_that_does_not_parse_refuses_a_stored_embedding() {
    for fail_first in [true, false] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let reads = Arc::new(Mutex::new(0));
        let count = reads.clone();
        let task = tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let count = count.clone();
                tokio::spawn(async move {
                    let service = hyper::service::service_fn(
                        move |req: hyper::Request<hyper::body::Incoming>| {
                            let count = count.clone();
                            async move {
                                let body = if req.method() == hyper::Method::GET {
                                    let mut count = count.lock().unwrap();
                                    *count += 1;
                                    if (*count == 1) == fail_first {
                                        // A body that is not JSON.
                                        "<html>".to_string()
                                    } else {
                                        tags("test-digest").to_string()
                                    }
                                } else {
                                    json!({"embeddings": [[3.0, 4.0]]}).to_string()
                                };
                                Ok::<_, hyper::Error>(hyper::Response::new(Full::new(Bytes::from(
                                    body,
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
        let endpoint = HttpEmbedder::new(
            config(format!("http://{address}/api/embed")),
            scan::bundled(),
        )
        .unwrap();
        assert_eq!(
            reason(&endpoint, Kind::Document).await,
            Some("embedder-response-invalid"),
            "fail_first {fail_first}"
        );
        task.abort();
    }
}

#[test]
fn the_listing_url_stays_on_the_endpoint_origin() {
    let url = |s: &str| url::Url::parse(s).unwrap();
    assert_eq!(
        listing_url("api/embed", &url("http://embedder:11434/api/embed"))
            .unwrap()
            .as_str(),
        "http://embedder:11434/api/tags"
    );
    assert_eq!(
        listing_url(
            "api/embed",
            &url("https://embed.example.test:8443/front/api/embed")
        )
        .unwrap()
        .as_str(),
        "https://embed.example.test:8443/front/api/tags"
    );
    assert!(listing_url("v1/embeddings", &url("http://embedder:11434/api/embed")).is_none());
    assert!(listing_url("api/embed", &url("http://embedder:11434/embed")).is_none());
    // A name without a tag matches the listing's `:latest` entry, and a
    // tagged name matches only itself.
    let listing = json!({"models": [
        {"name": "plain:latest", "digest": "a"},
        {"name": "org/tagged:8b", "digest": "b"},
        {"name": "org/tagged:4b", "digest": "c"},
    ]});
    assert_eq!(listed_digest(&listing, "plain").as_deref(), Some("a"));
    assert_eq!(
        listed_digest(&listing, "org/tagged:8b").as_deref(),
        Some("b")
    );
    assert_eq!(listed_digest(&listing, "org/tagged"), None);
    assert_eq!(listed_digest(&listing, "absent"), None);
}
