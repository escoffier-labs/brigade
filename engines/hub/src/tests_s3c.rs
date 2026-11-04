use super::*;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

async fn short_response(
    path: &str,
    kind: &str,
    declared: usize,
    sent: usize,
    full_budget: bool,
) -> (Response<Body>, App, Duration) {
    let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let upstream = format!("http://{}", listener.local_addr().unwrap());
    let header = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {declared}\r\nConnection: close\r\n\r\n"
    );
    tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut request = [0u8; 4096];
        let n = socket.read(&mut request).await.unwrap();
        assert!(n > 0);
        socket.write_all(header.as_bytes()).await.unwrap();
        if sent > 0 {
            let _ = socket.write_all(&vec![b'x'; sent]).await;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    });
    let app = App::new(
        upstream,
        Client::builder(TokioExecutor::new()).build_http(),
        false,
        false,
    );
    let permit = if full_budget {
        Some(
            app.buffers
                .clone()
                .acquire_many_owned(BODY_BUDGET as u32)
                .await
                .unwrap(),
        )
    } else {
        None
    };
    let start = tokio::time::Instant::now();
    let response = handle(
        State(app.clone()),
        Request::builder().uri(path).body(Body::empty()).unwrap(),
    )
    .await;
    drop(permit);
    (response, app, start.elapsed())
}
#[tokio::test]
async fn json_reset_is_502_before_headers_and_preserves_browser_stream() {
    let (response, _, _) = short_response("/nodes", "application/json", 100, 10, false).await;
    assert_eq!(response.status(), 502);
    assert!(
        axum::body::to_bytes(response.into_body(), 1024)
            .await
            .unwrap()
            .windows(24)
            .any(|v| v == b"upstream-outcome-unknown")
    );
    let (response, _, _) = short_response("/deck", "text/html", 100, 10, false).await;
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["content-length"], "100");
    assert!(
        axum::body::to_bytes(response.into_body(), 1024)
            .await
            .is_err()
    );
}
#[tokio::test]
async fn full_buffer_budget_waits_then_streams_and_counts() {
    let (response, app, elapsed) =
        short_response("/nodes", "application/json", 100, 10, true).await;
    assert_eq!(response.status(), 200);
    assert!(elapsed >= Duration::from_millis(900));
    assert_eq!(
        app.buffer_fallbacks
            .load(std::sync::atomic::Ordering::Relaxed),
        1
    );
    assert!(
        axum::body::to_bytes(response.into_body(), 1024)
            .await
            .is_err()
    );
}
#[tokio::test]
async fn oversized_json_keeps_length_and_closes_short_stream() {
    let (response, _, _) = short_response(
        "/nodes",
        "application/json",
        BODY_LIMIT + 1000,
        BODY_LIMIT + 10,
        false,
    )
    .await;
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.headers()["content-length"],
        (BODY_LIMIT + 1000).to_string()
    );
    assert!(
        axum::body::to_bytes(response.into_body(), BODY_LIMIT + 2000)
            .await
            .is_err()
    );
}

#[test]
fn loopback_is_front_only_on_loopback_bind() {
    let peer = IpAddr::V4(Ipv4Addr::LOCALHOST);
    assert!(front_peer(peer, peer));
    assert!(!front_peer(Ipv4Addr::UNSPECIFIED.into(), peer));
}

#[test]
fn enrollment_stays_anonymous_and_home_paths_expand() {
    assert!(!admission::browser("/enroll"));
    assert_eq!(expand_user("relative/hub.db"), "relative/hub.db");
    assert_eq!(expand_user("~another/hub.db"), "~another/hub.db");
    let home = expand_user("~");
    assert_ne!(home, "~");
    assert_eq!(
        std::path::Path::new(&expand_user("~/hub.db")),
        std::path::Path::new(&home).join("hub.db")
    );
}
