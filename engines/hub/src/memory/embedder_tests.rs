use super::{embedder::*, scan, tests::*};
use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use serde_json::json;
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

fn config(endpoint: String) -> Config {
    let dir = temp_dir();
    let token = dir.join("token");
    std::fs::write(&token, "fake-embed-token").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
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
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let seen = requests.clone();
    let digest = digest.to_string();
    let task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
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
    (format!("http://{address}/api/embed"), requests, task)
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
