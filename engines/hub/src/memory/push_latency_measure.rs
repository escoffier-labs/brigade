//! Opt-in HTTP measurement; timings are evidence, never a CI threshold.
use super::response;
use crate::memory::scan;
use crate::memory::tests::{ADMIN_TOKEN, Hub, NODE_TOKEN};
use axum::{Router, body::Bytes, routing::post};
use http_body_util::{BodyExt, Full};
use hyper::{Request, client::conn::http1};
use hyper_util::rt::TokioIo;
use serde_json::json;
use std::{net::Ipv4Addr, sync::Arc, time::Instant};

#[tokio::test]
#[ignore = "host-dependent HTTP latency measurement"]
async fn push_http_latency_measurement() {
    let mut h = Hub::with(scan::bundled(), None);
    for (id, copies) in [
        ("eligible", 1),
        ("one", 1),
        ("five", 5),
        ("sixty_four", 64),
        ("many", 70),
    ] {
        for n in 0..copies {
            let r = h.call(ADMIN_TOKEN, json!({"action":"import", "scope":format!("repo:example/copy{n:02}"), "card_id":id, "text":"release checklist", "client_event_id":h.id()})).await;
            assert_eq!(r.status, 201);
        }
    }
    let conn = rusqlite::Connection::open(h.memory()).unwrap();
    conn.execute(
        "UPDATE memory_card_revisions SET scan_state='pending' WHERE card_id!='eligible'",
        [],
    )
    .unwrap();
    drop(conn);
    let store = Arc::new(h.store.take().unwrap());
    let app = Router::new().route(
        "/memory",
        post(move |headers: axum::http::HeaderMap, bytes: Bytes| {
            let store = store.clone();
            async move {
                let token = headers["authorization"]
                    .to_str()
                    .unwrap()
                    .strip_prefix("Bearer ")
                    .unwrap();
                let r = match store.authenticate(token).await {
                    Ok(c) => store.handle(&c, &bytes).await,
                    Err(r) => r,
                };
                response::respond(r.status, r.body)
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let stream = tokio::net::TcpStream::connect(addr).await.unwrap();
    stream.set_nodelay(true).unwrap();
    let (mut client, connection) = http1::handshake(TokioIo::new(stream)).await.unwrap();
    let driver = tokio::spawn(connection);
    let ids = ["eligible", "absent", "one", "five", "sixty_four", "many"];
    for run in 1..=2 {
        let mut samples = vec![Vec::new(); ids.len()];
        // Interleave cases to avoid assigning a load trend to one copy count.
        for round in 0..550 {
            for j in 0..ids.len() {
                let i = (round + j) % ids.len();
                let id = ids[i];
                let body = json!({"action":"card", "card_id":id, "push":true}).to_string();
                let request = Request::post("/memory")
                    .header("host", "example.invalid")
                    .header("authorization", format!("Bearer {NODE_TOKEN}"))
                    .header("content-length", body.len())
                    .body(Full::new(Bytes::from(body)))
                    .unwrap();
                let start = Instant::now();
                let r = client.send_request(request).await.unwrap();
                assert_eq!(
                    r.status().as_u16(),
                    if id == "eligible" { 200 } else { 404 }
                );
                r.into_body().collect().await.unwrap();
                if round >= 50 {
                    samples[i].push(start.elapsed().as_nanos());
                }
            }
        }
        for (id, mut values) in ids.into_iter().zip(samples) {
            values.sort_unstable();
            println!(
                "HTTP run={run} id={id} samples={} p50_us={:.3} p99_us={:.3} p99_minus_p50_us={:.3}",
                values.len(),
                values[249] as f64 / 1000.,
                values[494] as f64 / 1000.,
                (values[494] - values[249]) as f64 / 1000.
            );
        }
    }
    drop(client);
    driver.abort();
    server.abort();
}
