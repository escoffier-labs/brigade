//! S3 transport front. Python remains the authority for route semantics and storage.
use axum::{
    Router,
    body::Body,
    extract::State,
    http::{Request, Response, StatusCode},
    routing::any,
};
use clap::Parser;
use futures_util::StreamExt;
use http_body_util::BodyExt;
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::{TokioExecutor, TokioIo, TokioTimer},
    service::TowerToHyperService,
};
use std::{
    collections::{HashMap, hash_map::RandomState},
    hash::BuildHasher,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    net::TcpSocket,
    process::{Child, Command},
    sync::{OwnedSemaphorePermit, Semaphore, watch},
};

const BODY_LIMIT: usize = 8 * 1024 * 1024;
/// Bytes of request bodies the front may buffer at once (S3c's budget).
const BODY_BUDGET: usize = 64 * 1024 * 1024;
/// Shutdown drain. A container stop grace period must exceed this.
const DRAIN: Duration = Duration::from_secs(25);

#[derive(Parser, Debug)]
#[command(
    name = "brigade-hub",
    infer_long_args = true,
    args_override_self = true
)]
struct Config {
    #[arg(long)]
    host: String,
    #[arg(long, default_value_t = 3774, allow_hyphen_values = true)]
    port: i64,
    #[arg(long)]
    db: Option<String>,
    #[arg(long)]
    token_file: Option<String>,
    #[arg(long)]
    allow_admin_writes: bool,
    #[arg(long)]
    require_sealed: bool,
    #[arg(long)]
    deck_config: Option<String>,
    #[arg(long)]
    trust_tailscale_identity: bool,
    #[arg(long)]
    trust_forwarded_proto: bool,
    /// Existing loopback Python hub. Without this flag, start and supervise Python.
    #[arg(long)]
    upstream: Option<String>,
    #[arg(long, default_value_t = 4096, value_parser = clap::value_parser!(u32).range(1..))]
    max_connections: u32,
}
fn loopback(host: &str) -> bool {
    host.parse::<IpAddr>()
        .map(|ip| match ip {
            IpAddr::V4(v) => v.is_loopback(),
            IpAddr::V6(v) => v.is_loopback() || v.to_ipv4_mapped().is_some_and(|v| v.is_loopback()),
        })
        .unwrap_or(false)
}
impl Config {
    fn validate(&self) -> Result<(), String> {
        u16::try_from(self.port).map_err(|_| "port must be 0-65535")?;
        if self.host.is_empty() {
            return Err(
                "--host is required (the hub never binds all interfaces by default)".into(),
            );
        }
        if self.trust_forwarded_proto && !loopback(&self.host) {
            return Err("--trust-forwarded-proto requires a numeric loopback --host".into());
        }
        if self.trust_tailscale_identity && !loopback(&self.host) {
            return Err("--trust-tailscale-identity requires a loopback --host".into());
        }
        if let Some(upstream) = &self.upstream {
            let u = url::Url::parse(upstream).map_err(|_| "invalid --upstream")?;
            if u.scheme() != "http"
                || !u
                    .host_str()
                    .is_some_and(|h| loopback(h.trim_matches(['[', ']'])))
                || !u.username().is_empty()
                || u.password().is_some()
                || u.path() != "/"
                || u.query().is_some()
                || u.fragment().is_some()
            {
                return Err("--upstream requires a plain HTTP numeric loopback origin".into());
            }
        }
        Ok(())
    }
    fn python_args(&self, port: u16) -> Vec<String> {
        let mut args = vec![
            "-m".into(),
            "brigade".into(),
            "fleet".into(),
            "serve".into(),
            "--host".into(),
            Ipv4Addr::LOCALHOST.to_string(),
            "--port".into(),
            port.to_string(),
        ];
        for (flag, value) in [
            ("--db", &self.db),
            ("--token-file", &self.token_file),
            ("--deck-config", &self.deck_config),
        ] {
            if let Some(v) = value {
                args.extend([flag.into(), v.clone()]);
            }
        }
        for (flag, enabled) in [
            ("--allow-admin-writes", self.allow_admin_writes),
            ("--require-sealed", self.require_sealed),
            ("--trust-tailscale-identity", self.trust_tailscale_identity),
            ("--trust-forwarded-proto", self.trust_forwarded_proto),
        ] {
            if enabled {
                args.push(flag.into());
            }
        }
        args
    }
}
#[derive(Default)]
struct Counts(Mutex<HashMap<String, usize>>);
struct Slot {
    counts: Arc<Counts>,
    key: String,
}
impl Counts {
    fn acquire(self: &Arc<Self>, key: String, cap: usize) -> Option<Slot> {
        let mut counts = self.0.lock().unwrap();
        let count = counts.entry(key.clone()).or_default();
        if *count >= cap {
            return None;
        }
        *count += 1;
        Some(Slot {
            counts: self.clone(),
            key,
        })
    }
}
impl Drop for Slot {
    fn drop(&mut self) {
        let mut counts = self.counts.0.lock().unwrap();
        if let Some(n) = counts.get_mut(&self.key) {
            *n -= 1;
            if *n == 0 {
                counts.remove(&self.key);
            }
        }
    }
}
type HttpClient = Client<HttpConnector, Body>;
#[derive(Clone)]
struct App {
    upstream: String,
    client: HttpClient,
    credentials: Arc<Counts>,
    trust_identity: bool,
    trust_proto: bool,
    inflight: Arc<Semaphore>,
    bodies: Arc<Semaphore>,
    hashes: RandomState,
    upstream_key: String,
    ready: Arc<Mutex<Option<tokio::time::Instant>>>,
}
impl App {
    fn new(upstream: String, client: HttpClient, trust_identity: bool, trust_proto: bool) -> Self {
        App {
            upstream,
            client,
            credentials: Arc::default(),
            trust_identity,
            trust_proto,
            inflight: Arc::new(Semaphore::new(24)),
            bodies: Arc::new(Semaphore::new(BODY_BUDGET)),
            hashes: RandomState::new(),
            upstream_key: String::new(),
            ready: Arc::default(),
        }
    }
}
fn json(status: StatusCode, value: serde_json::Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .header("cache-control", "no-store")
        .header("referrer-policy", "no-referrer")
        .body(Body::from(value.to_string()))
        .unwrap()
}
fn busy(reason: &str) -> Response<Body> {
    let mut response = json(
        StatusCode::SERVICE_UNAVAILABLE,
        serde_json::json!({"error":"hub busy", "reason":reason}),
    );
    response
        .headers_mut()
        .insert("retry-after", "1".parse().unwrap());
    response
}
fn strip_hop(headers: &mut axum::http::HeaderMap) {
    let nominated: Vec<String> = headers
        .get_all("connection")
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(',').map(|v| v.trim().to_owned()))
        .collect();
    for name in nominated {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        headers.remove(name);
    }
}
fn req_path(parts: &axum::http::request::Parts) -> &str {
    parts.uri.path()
}
fn long_poll_deadline(path: &str, bytes: &[u8]) -> Duration {
    if path == "/messages" && bytes.len() <= BODY_LIMIT {
        if let Ok(serde_json::Value::Object(body)) = serde_json::from_slice(bytes) {
            if body.get("action").and_then(|v| v.as_str()) == Some("list") {
                if let Some(wait) = body
                    .get("wait_ms")
                    .and_then(|v| v.as_u64())
                    .filter(|v| *v > 0)
                {
                    return Duration::from_millis(wait.min(20_000) + 5_000);
                }
            }
        }
    }
    Duration::from_secs(30)
}
async fn handle(State(app): State<App>, req: Request<Body>) -> Response<Body> {
    if req
        .uri()
        .path_and_query()
        .is_none_or(|p| !p.as_str().starts_with('/'))
    {
        return json(
            StatusCode::BAD_REQUEST,
            serde_json::json!({"error":"invalid request target"}),
        );
    }
    if matches!(req.method().as_str(), "GET" | "POST")
        && req
            .uri()
            .query()
            .is_some_and(|q| url::form_urlencoded::parse(q.as_bytes()).any(|(k, _)| k == "token"))
    {
        return Response::builder()
            .status(401)
            .header("content-type", "text/plain; charset=utf-8")
            .header("cache-control", "no-store")
            .header("referrer-policy", "no-referrer")
            .header(
                "content-security-policy",
                "default-src 'none'; frame-ancestors 'none'",
            )
            .header("x-content-type-options", "nosniff")
            .header("vary", "Cookie")
            .body(Body::from("Dashboard request refused.\n"))
            .unwrap();
    }
    if req.method() == "GET" && req.uri().path() == "/health" {
        return Response::builder()
            .header("content-type", "application/json")
            .header("server", "brigade-fleet-hub/1")
            .header("cache-control", "no-store")
            .header("referrer-policy", "no-referrer")
            .body(Body::from(
                r#"{"ok": true, "service": "brigade-fleet-hub"}"#,
            ))
            .unwrap();
    }
    if req.method() == "GET" && req.uri().path() == "/ready" {
        let ready = app
            .ready
            .lock()
            .unwrap()
            .is_some_and(|last| last.elapsed() < Duration::from_secs(5));
        return json(
            if ready {
                StatusCode::OK
            } else {
                StatusCode::SERVICE_UNAVAILABLE
            },
            serde_json::json!({"ready":ready}),
        );
    }
    if req.headers().contains_key("transfer-encoding")
        || req.headers().get_all("content-length").iter().count() > 1
    {
        return json(
            StatusCode::BAD_REQUEST,
            serde_json::json!({"error":"bad Content-Length or Transfer-Encoding"}),
        );
    }
    let length = req
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0);
    if length > BODY_LIMIT as u64 {
        return body_too_large();
    }
    // Buffer the whole body before admission. A slow upload then holds only
    // its connection and its share of the byte budget, never a proxied slot,
    // so unvalidated Authorization values cannot occupy Python's threads.
    let (parts, body) = req.into_parts();
    let _budget = if length == 0 {
        None
    } else {
        let wanted = app.bodies.clone().acquire_many_owned(length as u32);
        match tokio::time::timeout(Duration::from_secs(1), wanted).await {
            Ok(Ok(permit)) => Some(permit),
            _ => return busy("body-budget-full"),
        }
    };
    let collected = http_body_util::Limited::new(body, BODY_LIMIT).collect();
    let body = match tokio::time::timeout(Duration::from_secs(30), collected).await {
        Ok(Ok(collected)) => collected.to_bytes(),
        Ok(Err(e)) if e.is::<http_body_util::LengthLimitError>() => return body_too_large(),
        Ok(Err(_)) => {
            return json(
                StatusCode::BAD_REQUEST,
                serde_json::json!({"error":"incomplete request body"}),
            );
        }
        Err(_) => {
            return json(
                StatusCode::REQUEST_TIMEOUT,
                serde_json::json!({"error":"request body timeout"}),
            );
        }
    };
    let header_timeout = long_poll_deadline(req_path(&parts), &body);
    let bodyless = body.is_empty();
    let mut req = Request::from_parts(parts, Body::from(body));
    let (key, cap) = if let Some(value) = req.headers().get("authorization") {
        (
            format!("credential:{}", app.hashes.hash_one(value.as_bytes())),
            64,
        )
    } else {
        let peer = req
            .extensions()
            .get::<SocketAddr>()
            .map(|p| p.ip().to_string())
            .unwrap_or_default();
        (format!("source:{peer}"), 8)
    };
    let Some(credential) = app.credentials.acquire(key, cap) else {
        return busy("credential-inflight-full");
    };
    let Ok(inflight) = app.inflight.clone().try_acquire_owned() else {
        return busy("proxy-inflight-full");
    };
    let browser = req.uri().path() == "/"
        || ["/deck", "/view/", "/enroll", "/dashboard/"]
            .iter()
            .any(|p| req.uri().path().starts_with(p));
    if !app.trust_identity || !browser {
        req.headers_mut().remove("tailscale-user-login");
    }
    if !app.trust_proto || !browser {
        req.headers_mut().remove("x-forwarded-proto");
    }
    // Preserve framing for Python's exact route-specific refusals. Hyper rejects
    // ambiguous HTTP framing before dispatch; valid Content-Length is unchanged.
    strip_hop(req.headers_mut());
    req.headers_mut().remove("x-brigade-client-address");
    req.headers_mut().remove("x-brigade-upstream-key");
    if !app.upstream_key.is_empty() {
        let peer = req
            .extensions()
            .get::<SocketAddr>()
            .map(|p| p.ip())
            .unwrap_or(Ipv4Addr::LOCALHOST.into());
        req.headers_mut().insert(
            "x-brigade-client-address",
            peer.to_string().parse().unwrap(),
        );
        req.headers_mut()
            .insert("x-brigade-upstream-key", app.upstream_key.parse().unwrap());
    }
    let origin = match app.upstream.parse::<axum::http::Uri>() {
        Ok(uri) => uri,
        Err(_) => return upstream_error(StatusCode::BAD_GATEWAY, "upstream-down", browser),
    };
    let Some(authority) = origin.authority().cloned() else {
        return upstream_error(StatusCode::BAD_GATEWAY, "upstream-down", browser);
    };
    let uri = axum::http::Uri::builder()
        .scheme("http")
        .authority(authority)
        .path_and_query(req.uri().path_and_query().cloned().unwrap())
        .build();
    match uri {
        Ok(uri) => *req.uri_mut() = uri,
        Err(_) => {
            return json(
                StatusCode::BAD_REQUEST,
                serde_json::json!({"error":"invalid request target"}),
            );
        }
    }
    // Python reads wait_ms from the JSON body only, and its bounded wait plus
    // processing fits this deadline. The body is already buffered, so the
    // deadline covers only Python's handling.

    let retry = req.method() == "GET" && bodyless;
    let retry_head = (
        req.method().clone(),
        req.uri().clone(),
        req.headers().clone(),
    );
    let total_deadline = tokio::time::Instant::now() + Duration::from_secs(120);
    let mut result = tokio::time::timeout(header_timeout, app.client.request(req)).await;
    if retry && matches!(&result, Ok(Err(e)) if e.is_connect()) {
        let mut request = Request::builder()
            .method(retry_head.0)
            .uri(retry_head.1)
            .body(Body::empty())
            .unwrap();
        *request.headers_mut() = retry_head.2;
        result = tokio::time::timeout(header_timeout, app.client.request(request)).await;
    }
    match result {
        Ok(Ok(response)) => {
            let (mut parts, body) = response.into_parts();
            strip_hop(&mut parts.headers);
            let mut body = body.into_data_stream();
            let first_deadline =
                total_deadline.min(tokio::time::Instant::now() + Duration::from_secs(30));
            let first = match tokio::time::timeout_at(first_deadline, body.next()).await {
                Ok(Some(Ok(bytes))) => Some(Ok(bytes)),
                Ok(None) => None,
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
            };
            let body = futures_util::stream::iter(first).chain(body);
            // Once headers have been sent, a body failure terminates the stream.
            // HTTP cannot replace an already sent status with a 504.
            let stream = futures_util::stream::unfold(
                (body, credential, inflight, false),
                move |(mut stream, slot, inflight, done)| async move {
                    if done {
                        return None;
                    }
                    let deadline =
                        total_deadline.min(tokio::time::Instant::now() + Duration::from_secs(30));
                    match tokio::time::timeout_at(deadline, stream.next()).await {
                        Ok(Some(item)) => Some((
                            item.map_err(std::io::Error::other),
                            (stream, slot, inflight, false),
                        )),
                        Ok(None) => None,
                        Err(_) => Some((
                            Err(std::io::Error::new(
                                std::io::ErrorKind::TimedOut,
                                "upstream body timeout",
                            )),
                            (stream, slot, inflight, true),
                        )),
                    }
                },
            );
            Response::from_parts(parts, Body::from_stream(stream))
        }
        Err(_) => upstream_error(
            StatusCode::GATEWAY_TIMEOUT,
            "upstream-header-timeout",
            browser,
        ),
        Ok(Err(e)) => upstream_error(
            StatusCode::BAD_GATEWAY,
            if e.is_connect() {
                "upstream-down"
            } else {
                "upstream-outcome-unknown"
            },
            browser,
        ),
    }
}
fn upstream_error(status: StatusCode, reason: &str, browser: bool) -> Response<Body> {
    if browser {
        return Response::builder()
            .status(status)
            .header("content-type", "text/html; charset=utf-8")
            .header("cache-control", "no-store")
            .header("x-content-type-options", "nosniff")
            .body(Body::from(format!(
                "<!doctype html><title>Hub unavailable</title><p>{reason}</p>"
            )))
            .unwrap();
    }
    json(
        status,
        serde_json::json!({"error":if status == StatusCode::GATEWAY_TIMEOUT { "upstream timeout" } else { "upstream unavailable" }, "reason":reason}),
    )
}
fn body_too_large() -> Response<Body> {
    Response::builder()
        .status(413)
        .header("content-type", "application/json")
        .header("cache-control", "no-store")
        .header("referrer-policy", "no-referrer")
        .body(Body::from(
            r#"{"error": "request body exceeds 8388608 bytes"}"#,
        ))
        .unwrap()
}

async fn upstream(
    config: &Config,
    client: &HttpClient,
    key: &str,
) -> Result<(String, Option<Child>), String> {
    if let Some(url) = &config.upstream {
        let status =
            Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
                .arg("-c")
                .arg(include_str!("../validate.py"))
                .args(config.python_args(0).into_iter().skip(2))
                .kill_on_drop(true)
                .status()
                .await
                .map_err(|e| e.to_string())?;
        if !status.success() {
            return Err("Python upstream refused startup configuration".into());
        }
        return Ok((url.trim_end_matches('/').into(), None));
    }
    let mut child =
        Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
            .args(config.python_args(0))
            .env("BRIGADE_HUB_MANAGED_LOOPBACK", key)
            .env("BRIGADE_HUB_UPSTREAM_KEY", key)
            .stdout(std::process::Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| e.to_string())?;
    use tokio::io::{AsyncBufReadExt, BufReader};
    let stdout = child.stdout.take().ok_or("missing upstream stdout")?;
    let mut lines = BufReader::new(stdout).lines();
    let line = tokio::time::timeout(Duration::from_secs(30), lines.next_line())
        .await
        .map_err(|_| "Python upstream startup timed out")?
        .map_err(|e| e.to_string())?
        .ok_or("Python upstream refused startup configuration")?;
    let port: u16 = line
        .strip_prefix("BRIGADE_UPSTREAM_PORT=")
        .ok_or("invalid upstream port announcement")?
        .parse()
        .map_err(|_| "invalid upstream port")?;
    // Keep stdout drained after the one private startup announcement.
    tokio::spawn(async move { while matches!(lines.next_line().await, Ok(Some(_))) {} });
    let url = format!("http://{}:{port}", Ipv4Addr::LOCALHOST);
    for _ in 0..300 {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("Python upstream refused startup configuration".into());
        }
        let req = Request::builder()
            .uri(format!("{url}/health"))
            .header("x-brigade-upstream-key", key)
            .header("x-brigade-client-address", Ipv4Addr::LOCALHOST.to_string())
            .body(Body::empty())
            .unwrap();
        if matches!(client.request(req).await, Ok(r) if r.status() == StatusCode::OK) {
            return Ok((url, Some(child)));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    Err("Python upstream startup timed out".into())
}
async fn run(config: Config) -> Result<(), String> {
    config.validate()?;
    let mut connector = HttpConnector::new();
    connector.set_connect_timeout(Some(Duration::from_secs(1)));
    let client = Client::builder(TokioExecutor::new())
        .retry_canceled_requests(false)
        .pool_max_idle_per_host(0)
        .build(connector);
    // Generate in the existing Python runtime, without a shell or secret output.
    let key = if config.upstream.is_some() {
        std::env::var("BRIGADE_HUB_UPSTREAM_KEY").unwrap_or_default()
    } else {
        let output =
            Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
                .args(["-c", "import secrets; print(secrets.token_hex(32))"])
                .output()
                .await
                .map_err(|e| e.to_string())?;
        if !output.status.success() {
            return Err("per-start key generation failed".into());
        }
        String::from_utf8(output.stdout)
            .map_err(|_| "invalid per-start key")?
            .trim()
            .to_owned()
    };
    let (upstream, mut child) = upstream(&config, &client, &key).await?;
    let port = u16::try_from(config.port).map_err(|_| "port must be 0-65535")?;
    let address = tokio::net::lookup_host((config.host.as_str(), port))
        .await
        .map_err(|e| e.to_string())?
        .next()
        .ok_or("host did not resolve")?;
    let socket = if address.is_ipv4() {
        TcpSocket::new_v4()
    } else {
        TcpSocket::new_v6()
    }
    .map_err(|e| e.to_string())?;
    socket.set_reuseaddr(true).map_err(|e| e.to_string())?;
    socket.bind(address).map_err(|e| e.to_string())?;
    let listener = socket.listen(1024).map_err(|e| e.to_string())?;
    eprintln!(
        "listening on {}",
        listener.local_addr().map_err(|e| e.to_string())?
    );
    let mut state = App::new(
        upstream,
        client,
        config.trust_tailscale_identity,
        config.trust_forwarded_proto,
    );
    state.upstream_key = key;
    let probe = state.clone();
    tokio::spawn(async move {
        loop {
            let request = Request::builder()
                .uri(format!("{}/health", probe.upstream))
                .header("x-brigade-upstream-key", &probe.upstream_key)
                .header("x-brigade-client-address", Ipv4Addr::LOCALHOST.to_string())
                .body(Body::empty())
                .unwrap();
            if matches!(tokio::time::timeout(Duration::from_secs(1), probe.client.request(request)).await,
                Ok(Ok(response)) if response.status() == StatusCode::OK)
            {
                *probe.ready.lock().unwrap() = Some(tokio::time::Instant::now());
            } else {
                *probe.ready.lock().unwrap() = None;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    });
    let app = Router::new().fallback(any(handle)).with_state(state);
    let connections = Arc::new(Semaphore::new(config.max_connections as usize));
    let sources = Arc::<Counts>::default();
    let mut tasks = tokio::task::JoinSet::new();
    let (stop, stopping) = watch::channel(false);
    let mut signal = Box::pin(async {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    });
    loop {
        tokio::select! {
            _ = &mut signal => break,
            status = async { match child.as_mut() { Some(c) => c.wait().await, None => std::future::pending().await } } => {
                return Err(format!("managed upstream exited: {status:?}"));
            },
            _ = tasks.join_next(), if !tasks.is_empty() => {},
            accepted = listener.accept() => {
                let (stream, peer) = match accepted {
                    Ok(pair) => pair,
                    Err(e) => {
                        eprintln!("accept failed: {e}");
                        if matches!(e.raw_os_error(), Some(23 | 24)) { tokio::time::sleep(Duration::from_millis(100)).await; }
                        continue;
                    }
                };
                let Ok(global) = connections.clone().try_acquire_owned() else { continue; };
                let Some(source) = sources.acquire(peer.ip().to_string(), 256) else { continue; };
                let mut app = app.clone();
                app = app.layer(axum::Extension(peer));
                let service = TowerToHyperService::new(app);
                let stopping = stopping.clone();
                tasks.spawn(async move { serve(stream, service, stopping, global, source).await; });
            }
        }
    }
    // Idle keep-alive connections close now. In-flight requests finish
    // within the drain deadline.
    let _ = stop.send(true);
    let drain = async { while tasks.join_next().await.is_some() {} };
    if tokio::time::timeout(DRAIN, drain).await.is_err() {
        tasks.abort_all();
    }

    if let Some(ref mut child) = child {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    Ok(())
}
async fn serve(
    stream: tokio::net::TcpStream,
    service: TowerToHyperService<Router>,
    mut stopping: watch::Receiver<bool>,
    _global: OwnedSemaphorePermit,
    _source: Slot,
) {
    let mut builder = hyper::server::conn::http1::Builder::new();
    builder
        .timer(TokioTimer::new())
        .header_read_timeout(Duration::from_secs(10))
        .max_headers(100);
    let mut conn = std::pin::pin!(builder.serve_connection(TokioIo::new(stream), service));
    tokio::select! {
        _ = conn.as_mut() => return,
        _ = stopping.wait_for(|stop| *stop) => {}
    }
    conn.as_mut().graceful_shutdown();
    let _ = conn.await;
}
#[tokio::main]
async fn main() {
    let config = Config::parse();
    if let Err(e) = config.validate() {
        eprintln!("error: {e}");
        std::process::exit(2);
    }
    // Python is already required by this transport. Re-exec before starting
    // children so its safe resource API can raise our inherited descriptor limit.
    if std::env::var("BRIGADE_HUB_LIMIT_PID").ok() != Some(std::process::id().to_string()) {
        use std::os::unix::process::CommandExt;
        let error = std::process::Command::new(
            std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()),
        )
        .arg("-c")
        .arg(include_str!("../limits.py"))
        .arg((config.max_connections as u64 + 24 + 64).to_string())
        .arg(std::env::current_exe().expect("executable path"))
        .args(std::env::args_os().skip(1))
        .exec();
        eprintln!("error: descriptor limit setup failed: {error}");
        std::process::exit(2);
    }
    if let Err(e) = run(config).await {
        eprintln!("error: {e}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;
    #[test]
    fn long_poll_uses_body_and_caps_wait() {
        assert_eq!(
            long_poll_deadline("/messages", br#"{"action":"list","wait_ms":12000}"#),
            Duration::from_secs(17)
        );
        assert_eq!(
            long_poll_deadline("/messages", br#"{"action":"list","wait_ms":999999}"#),
            Duration::from_secs(25)
        );
        assert_eq!(
            long_poll_deadline("/messages", br#"{"action":"send","wait_ms":1}"#),
            Duration::from_secs(30)
        );
        assert_eq!(
            long_poll_deadline("/messages", b"invalid"),
            Duration::from_secs(30)
        );
    }
    #[tokio::test]
    async fn ready_ignores_full_proxy_pool_and_expires() {
        let app = App::new(
            "http://localhost:1".into(),
            Client::builder(TokioExecutor::new()).build_http(),
            false,
            false,
        );
        let _slots = app.inflight.clone().acquire_many_owned(24).await.unwrap();
        *app.ready.lock().unwrap() = Some(tokio::time::Instant::now());
        let request = || {
            Request::builder()
                .uri("/ready")
                .body(Body::empty())
                .unwrap()
        };
        assert_eq!(handle(State(app.clone()), request()).await.status(), 200);
        *app.ready.lock().unwrap() = Some(tokio::time::Instant::now() - Duration::from_secs(6));
        assert_eq!(handle(State(app), request()).await.status(), 503);
    }
    #[test]
    fn required_and_typed_flags() {
        assert!(Config::try_parse_from(["hub"]).is_err());
        assert!(Config::try_parse_from(["hub", "--host", "localhost", "--port", "no"]).is_err());
        assert!(Config::try_parse_from(["hub", "--host", "localhost", "--unknown"]).is_err());
        let c = Config::try_parse_from(["hub", "--host", "localhost", "--port", "-1"]).unwrap();
        assert_eq!(c.port, -1);
    }
    #[test]
    fn trust_requires_numeric_loopback() {
        let c = Config::try_parse_from(["hub", "--host", "localhost", "--trust-forwarded-proto"])
            .unwrap();
        assert_eq!(
            c.validate().unwrap_err(),
            "--trust-forwarded-proto requires a numeric loopback --host"
        );
    }
    #[test]
    fn caps_release_and_remove_keys() {
        let counts = Arc::<Counts>::default();
        let slot = counts.acquire("fake".into(), 1).unwrap();
        assert!(counts.acquire("fake".into(), 1).is_none());
        drop(slot);
        assert!(counts.0.lock().unwrap().is_empty());
    }
    #[tokio::test]
    async fn native_health_and_upstream_down() {
        let app = App::new(
            format!("http://{}:1", Ipv4Addr::LOCALHOST),
            Client::builder(TokioExecutor::new()).build_http(),
            false,
            false,
        );
        let health = handle(
            State(app.clone()),
            Request::builder()
                .uri("/health")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(health.status(), 200);
        let down = handle(
            State(app),
            Request::builder()
                .uri("/nodes")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(down.status(), 502);
    }
    #[tokio::test]
    async fn admission_returns_retry_after() {
        let app = App::new(
            format!("http://{}:1", Ipv4Addr::LOCALHOST),
            Client::builder(TokioExecutor::new()).build_http(),
            false,
            false,
        );
        let key = format!(
            "credential:{}",
            app.hashes.hash_one(b"Bearer fake".as_slice())
        );
        let slots: Vec<_> = (0..64)
            .map(|_| app.credentials.acquire(key.clone(), 64).unwrap())
            .collect();
        let reply = handle(
            State(app.clone()),
            Request::builder()
                .uri("/nodes")
                .header("authorization", "Bearer fake")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(reply.status(), 503);
        assert_eq!(reply.headers()["retry-after"], "1");
        assert!(
            String::from_utf8(
                axum::body::to_bytes(reply.into_body(), 1024)
                    .await
                    .unwrap()
                    .to_vec()
            )
            .unwrap()
            .contains("credential-inflight-full")
        );
        drop(slots);
        let _slots = app.inflight.clone().acquire_many_owned(24).await.unwrap();
        let reply = handle(
            State(app),
            Request::builder()
                .uri("/nodes")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(reply.status(), 503);
        assert_eq!(reply.headers()["retry-after"], "1");
    }
    #[test]
    fn forwards_every_python_flag() {
        let c = Config::try_parse_from([
            "hub",
            "--host",
            "localhost",
            "--db",
            "fake.db",
            "--token-file",
            "fake-token",
            "--deck-config",
            "fake.json",
            "--allow-admin-writes",
            "--require-sealed",
            "--trust-tailscale-identity",
            "--trust-forwarded-proto",
        ])
        .unwrap();
        let args = c.python_args(1234);
        for flag in [
            "--db",
            "--token-file",
            "--deck-config",
            "--allow-admin-writes",
            "--require-sealed",
            "--trust-tailscale-identity",
            "--trust-forwarded-proto",
        ] {
            assert!(args.iter().any(|a| a == flag));
        }
    }
    #[tokio::test]
    async fn proxy_preserves_error_bytes_and_authorization() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let upstream = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut bytes = [0u8; 4096];
            let n = socket.read(&mut bytes).await.unwrap();
            let request = String::from_utf8_lossy(&bytes[..n]).to_lowercase();
            assert!(request.contains("authorization: bearer fake-token"));
            assert!(!request.contains("tailscale-user-login"));
            socket.write_all(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 9\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nexact err").await.unwrap();
        });
        let app = App::new(
            upstream,
            Client::builder(TokioExecutor::new()).build_http(),
            false,
            false,
        );
        let response = handle(
            State(app.clone()),
            Request::builder()
                .uri("/fake")
                .header("authorization", "Bearer fake-token")
                .header("tailscale-user-login", "fake")
                .body(Body::empty())
                .unwrap(),
        )
        .await;
        assert_eq!(response.status(), 403);
        assert_eq!(response.headers()["content-type"], "text/plain");
        assert_eq!(
            axum::body::to_bytes(response.into_body(), 100)
                .await
                .unwrap(),
            "exact err"
        );
        assert!(app.credentials.0.lock().unwrap().is_empty());
        server.await.unwrap();
    }
}
