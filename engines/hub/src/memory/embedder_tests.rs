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
async fn fake(
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
