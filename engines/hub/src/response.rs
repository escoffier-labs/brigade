//! Buffer bounded JSON before exposing headers. Browser responses stream.
use crate::{BODY_LIMIT, upstream_error};
use axum::{
    body::{Body, Bytes},
    http::{Response, StatusCode},
};
use futures_util::StreamExt;
use http_body_util::BodyExt;
use std::{
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::{sync::Semaphore, time::Instant};

pub async fn prepare(
    response: Response<hyper::body::Incoming>,
    browser: bool,
    buffers: Arc<Semaphore>,
    fallbacks: Arc<AtomicU64>,
    total: Instant,
) -> Response<Body> {
    let (mut parts, body) = response.into_parts();
    crate::strip_hop(&mut parts.headers);
    let length = parts
        .headers
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok());
    let is_json = !browser
        && parts
            .headers
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .is_some_and(|v| {
                v.split(';')
                    .next()
                    .is_some_and(|v| v == "application/json" || v.ends_with("+json"))
            });
    let reserve = length.unwrap_or(BODY_LIMIT).min(BODY_LIMIT);
    let permit = if is_json && length.is_none_or(|n| n <= BODY_LIMIT) {
        match tokio::time::timeout(
            Duration::from_secs(1),
            buffers.acquire_many_owned(reserve as u32),
        )
        .await
        {
            Ok(Ok(permit)) => Some(permit),
            _ => {
                fallbacks.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    } else {
        None
    };
    let buffering = permit.is_some();
    let mut stream = body.into_data_stream();
    let mut prefix: Vec<Result<Bytes, std::io::Error>> = vec![];
    let mut size = 0;
    // Even a streamed response waits for its first frame, as in S3b.
    loop {
        let deadline = total.min(Instant::now() + Duration::from_secs(30));
        match tokio::time::timeout_at(deadline, stream.next()).await {
            Ok(Some(Ok(bytes))) => {
                size += bytes.len();
                prefix.push(Ok(bytes));
            }
            Ok(None) => break,
            Ok(Some(Err(_))) => {
                return upstream_error(
                    StatusCode::BAD_GATEWAY,
                    "upstream-outcome-unknown",
                    browser,
                );
            }
            Err(_) => {
                return upstream_error(
                    StatusCode::GATEWAY_TIMEOUT,
                    "upstream-body-timeout",
                    browser,
                );
            }
        }
        if !buffering || size > BODY_LIMIT {
            break;
        }
    }
    let stream = futures_util::stream::iter(prefix)
        .chain(stream.map(|item| item.map_err(std::io::Error::other)));
    let output = futures_util::stream::unfold(
        (stream, permit, false),
        move |(mut stream, permit, done)| async move {
            if done {
                return None;
            }
            let deadline = total.min(Instant::now() + Duration::from_secs(30));
            match tokio::time::timeout_at(deadline, stream.next()).await {
                Ok(Some(item)) => Some((item, (stream, permit, false))),
                Ok(None) => None,
                Err(_) => Some((
                    Err(std::io::Error::new(
                        std::io::ErrorKind::TimedOut,
                        "upstream body timeout",
                    )),
                    (stream, permit, true),
                )),
            }
        },
    );
    Response::from_parts(parts, Body::from_stream(output))
}
