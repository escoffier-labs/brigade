use super::*;
#[tokio::test]
async fn injected_mismatch_without_commit_is_definite_and_python_answers() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let mut app = App::new(
        format!("http://{}", listener.local_addr().unwrap()),
        Client::builder(TokioExecutor::new()).build_http(),
        false,
        false,
    );
    app.modes = brigade_hub::modes::Modes::parse(&["health=shadow".into()]).unwrap();
    let path = std::env::temp_dir().join(format!(
        "brigade-shadow-{}-{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch("CREATE TABLE sample(x);")
        .unwrap();
    app.shadow_db = Some(path.clone());
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut bytes = [0; 4096];
        let n = socket.read(&mut bytes).await.unwrap();
        assert!(n > 0);
        let body = r#"{"ok":false,"service":"brigade-fleet-hub"}"#;
        socket
            .write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                )
                .as_bytes(),
            )
            .await
            .unwrap();
    });
    let response = handle(
        State(app.clone()),
        Request::builder()
            .uri("/health")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(response.status(), 200);
    let body = axum::body::to_bytes(response.into_body(), 1024)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&body).unwrap()["ok"],
        false
    );
    assert!(app.telemetry.render().contains("result=\"definite\"} 1"));
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}
#[tokio::test]
async fn metrics_require_admin_and_keep_health_and_ready_shapes() {
    use sha2::Digest;
    let mut app = App::new(
        "http://localhost:1".into(),
        Client::builder(TokioExecutor::new()).build_http(),
        false,
        false,
    );
    app.admin_digest = Some(sha2::Sha256::digest(b"fake-admin").into());
    for authorization in [None, Some("Bearer wrong"), Some("bearer fake-admin")] {
        let mut request = Request::builder().uri("/metrics");
        if let Some(value) = authorization {
            request = request.header("authorization", value);
        }
        assert_eq!(
            handle(State(app.clone()), request.body(Body::empty()).unwrap())
                .await
                .status(),
            401
        );
    }
    let response = handle(
        State(app.clone()),
        Request::builder()
            .uri("/metrics")
            .header("authorization", "Bearer fake-admin")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    assert_eq!(response.status(), 200);
    assert!(
        response.headers()["content-type"]
            .to_str()
            .unwrap()
            .starts_with("text/plain; version=0.0.4")
    );
    let body = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let text = String::from_utf8_lossy(&body);
    assert!(text.contains("brigade_hub_writer_queue_depth NaN"));
    assert!(text.contains("No writer in this process"));
    assert!(!text.contains("Not yet wired"));
    assert!(!text.contains("fake-admin"));
}
#[tokio::test]
async fn metrics_report_attached_pools() {
    use sha2::Digest;
    let mut app = App::new(
        "http://localhost:1".into(),
        Client::builder(TokioExecutor::new()).build_http(),
        false,
        false,
    );
    app.admin_digest = Some(sha2::Sha256::digest(b"fake-admin").into());
    app.telemetry.attach_readers(Box::new(|| 4));
    app.telemetry.attach_cpu(Box::new(|| 1));
    let response = handle(
        State(app),
        Request::builder()
            .uri("/metrics")
            .header("authorization", "Bearer fake-admin")
            .body(Body::empty())
            .unwrap(),
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let text = String::from_utf8_lossy(&body);
    assert!(text.contains("\nbrigade_hub_reader_queue_depth 4\n"));
    assert!(text.contains("\nbrigade_hub_cpu_queue_depth 1\n"));
    assert!(text.contains("\nbrigade_hub_writer_batch_size NaN\n"));
}
