//! S3 transport front. Python remains the authority for route semantics and storage.
mod admission;
mod claims_route;
mod memory_route;
mod registration;
mod response;

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
    collections::HashMap,
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
    /// Repeatable route override. Unimplemented handlers are refused.
    #[arg(long)]
    route_mode: Vec<String>,
    #[arg(long)]
    host: String,
    /// Acceptance-only clock, shared with the Python upstream.
    #[arg(long, hide = true)]
    test_clock_file: Option<String>,
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
/// Match Python's unnamed-user expanduser for native CLI file reads.
fn expand_user(path: &str) -> String {
    let Some(suffix) = path.strip_prefix('~') else {
        return path.into();
    };
    if !suffix.is_empty() && !suffix.starts_with(std::path::is_separator) {
        return path.into();
    }
    #[cfg(not(windows))]
    #[allow(deprecated)]
    let home = std::env::home_dir().and_then(|p| p.into_os_string().into_string().ok());
    #[cfg(windows)]
    let home = std::env::var("USERPROFILE").ok().or_else(|| {
        std::env::var("HOMEPATH")
            .ok()
            .map(|p| format!("{}{p}", std::env::var("HOMEDRIVE").unwrap_or_default()))
    });
    match home {
        Some(home) => {
            #[cfg(windows)]
            let expanded = format!("{home}{suffix}");
            #[cfg(not(windows))]
            let expanded = format!("{}{suffix}", home.trim_end_matches('/'));
            if expanded.is_empty() {
                std::path::MAIN_SEPARATOR.to_string()
            } else {
                expanded
            }
        }
        None => path.into(),
    }
}

impl Config {
    fn validate(&self) -> Result<(), String> {
        brigade_hub::modes::Modes::parse(&self.route_mode)?;
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
    modes: brigade_hub::modes::Modes,
    telemetry: Arc<brigade_hub::telemetry::Telemetry>,
    admin_digest: Option<[u8; 32]>,
    shadow_db: Option<std::path::PathBuf>,
    upstream: String,
    client: HttpClient,
    budgets: admission::Budgets,
    lookup: Option<admission::Lookup>,
    registration: Option<registration::Registration>,
    claims: Option<claims_route::Claims>,
    /// None: memory is not configured and `/memory` is proxied like any unknown path.
    memory: Option<memory_route::MemoryRoute>,
    /// Shared by every writer of the hub file, the memory writer included (D23).
    arbiter: Arc<brigade_hub::writer::Arbiter>,
    trust_identity: bool,
    trust_proto: bool,
    storage: Option<Arc<brigade_hub::maintenance::Maintenance>>,
    buffers: Arc<Semaphore>,
    buffer_fallbacks: Arc<std::sync::atomic::AtomicU64>,
    upstream_key: axum::http::HeaderValue,
    front_bind: bool,
    ready: Arc<Mutex<Option<tokio::time::Instant>>>,
}
impl App {
    fn new(upstream: String, client: HttpClient, trust_identity: bool, trust_proto: bool) -> Self {
        App {
            modes: Default::default(),
            telemetry: Arc::default(),
            admin_digest: None,
            shadow_db: None,
            upstream,
            client,
            budgets: admission::Budgets::default(),
            lookup: None,
            registration: None,
            claims: None,
            memory: None,
            arbiter: Arc::default(),
            trust_identity,
            trust_proto,
            storage: None,
            buffers: Arc::new(Semaphore::new(BODY_BUDGET)),
            buffer_fallbacks: Arc::default(),
            upstream_key: axum::http::HeaderValue::from_static(""),
            front_bind: false,
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
/// Matches Python's `"token" in parse_qs(query, keep_blank_values=True)`:
/// names are split on `&` and percent- and plus-decoded.
fn query_names_token(query: Option<&str>) -> bool {
    query
        .is_some_and(|q| url::form_urlencoded::parse(q.as_bytes()).any(|(name, _)| name == "token"))
}
/// The family a request is counted under once its body shows the action.
#[derive(Clone, Default)]
struct FamilySlot(Arc<std::sync::OnceLock<&'static str>>);
fn message_action(bytes: &[u8]) -> String {
    serde_json::from_slice::<serde_json::Value>(bytes)
        .ok()
        .and_then(|v| v.get("action").and_then(|a| a.as_str()).map(str::to_owned))
        .unwrap_or_default()
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
                    return Duration::from_millis(wait.min(20_000) + 10_000);
                }
            }
        }
    }
    Duration::from_secs(30)
}
fn admin_authorized(app: &App, req: &Request<Body>) -> bool {
    use sha2::Digest;
    use subtle::ConstantTimeEq;
    let Some(token) = req
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|v| !v.is_empty())
    else {
        return false;
    };
    let digest: [u8; 32] = sha2::Sha256::digest(token.as_bytes()).into();
    app.admin_digest
        .is_some_and(|admin| bool::from(admin.ct_eq(&digest)))
}
async fn handle(State(app): State<App>, req: Request<Body>) -> Response<Body> {
    use brigade_hub::{
        modes::{Mode, family},
        shadow,
        telemetry::RequestLog,
    };
    let started = std::time::Instant::now();
    let f = family(req.method().as_str(), req.uri().path(), "");
    // Create the log first so a request cancelled during the lookup is still counted.
    let mut log = RequestLog {
        telemetry: app.telemetry.clone(),
        id: app.telemetry.id(),
        family: f,
        status: 500,
        credential_kind: "anonymous",
        request_bytes: Arc::default(),
        response_bytes: 0,
        start: started,
    };
    log.credential_kind = if admin_authorized(&app, &req) {
        "admin"
    } else if f == "health" {
        // Liveness and readiness never wait on the reader pool for a log label.
        "unchecked"
    } else if let Some(lookup) = &app.lookup {
        lookup
            .kind(
                req.headers()
                    .get("authorization")
                    .and_then(|v| v.to_str().ok()),
            )
            .await
    } else {
        "anonymous"
    };
    let request_bytes = log.request_bytes.clone();
    let (mut parts, body) = req.into_parts();
    // handle_inner fills this in for POST /messages once the body is buffered.
    let action_family = FamilySlot::default();
    parts.extensions.insert(action_family.clone());
    let body = body.into_data_stream().map(move |item| {
        if let Ok(bytes) = &item {
            request_bytes.fetch_add(bytes.len() as u64, std::sync::atomic::Ordering::Relaxed);
        }
        item
    });
    let req = Request::from_parts(parts, Body::from_stream(body));
    // Only /health has a native shadow handler in S9. /ready stays local.
    let shadowed = req.method() == "GET"
        && req.uri().path() == "/health"
        && req.uri().query().is_none()
        && !req.headers().contains_key("transfer-encoding")
        && req.headers().get_all("content-length").iter().count() <= 1
        && req.headers().get("content-length").is_none_or(|v| v == "0")
        && app.modes.get("health") == Mode::Shadow;
    let response = if shadowed {
        // Each comparison owns one dedicated watch connection across all samples.
        // Read-only + query_only means no Rust housekeeping can write here.
        let watch = match app.shadow_db.clone() {
            Some(path) => shadow::Watch::open(path).await,
            None => None,
        };
        let retry_headers = req.headers().clone();
        let peer = req.extensions().get::<SocketAddr>().copied();
        let mut original = Some(req);
        let mut observations = Vec::new();
        let mut answer = None;
        for _ in 0..3 {
            let before = match &watch {
                Some(w) => w.sample().await,
                None => None,
            };
            let request = original.take().unwrap_or_else(|| {
                let mut request = Request::builder()
                    .uri("/health")
                    .body(Body::empty())
                    .unwrap();
                *request.headers_mut() = retry_headers.clone();
                if let Some(peer) = peer {
                    request.extensions_mut().insert(peer);
                }
                request
            });
            let python = handle_inner(State(app.clone()), request).await;
            let status = python.status();
            let (parts, body) = python.into_parts();
            let bytes = match axum::body::to_bytes(body, BODY_LIMIT).await {
                Ok(bytes) => bytes,
                Err(_) => {
                    log.status = StatusCode::BAD_GATEWAY.as_u16();
                    return upstream_error(StatusCode::BAD_GATEWAY, "upstream-body-error", false);
                }
            };
            let native = shadow::health_shape();
            let (comparison, path) = serde_json::from_slice(&bytes)
                .map(|p| shadow::compare("health", &p, &native, log.start.elapsed().as_secs_f64()))
                .unwrap_or((shadow::Class::Definite, String::new()));
            let after = match &watch {
                Some(w) => w.sample().await,
                None => None,
            };
            let equal = status == 200 && comparison == shadow::Class::Match;
            observations.push((equal, before.is_none() || before != after));
            let class = shadow::classify(&observations);
            answer = Some(Response::from_parts(parts, Body::from(bytes.clone())));
            if class != shadow::Class::Inconclusive || observations.len() == 3 {
                app.telemetry.shadow(class.label());
                if class != shadow::Class::Match {
                    eprintln!(
                        "{}",
                        shadow::record(
                            "/health",
                            "",
                            (status.as_u16(), 200),
                            class,
                            &path,
                            (&bytes, shadow::HEALTH_BODY.as_bytes())
                        )
                    );
                }
                break;
            }
        }
        answer.unwrap()
    } else {
        handle_inner(State(app.clone()), req).await
    };
    if let Some(family) = action_family.0.get() {
        log.family = family;
    }
    log.status = response.status().as_u16();
    let (parts, body) = response.into_parts();
    // Hold the logger until completion or disconnect and count actual streamed bytes.
    let stream = futures_util::stream::unfold(
        (body.into_data_stream(), log),
        |(mut stream, mut log)| async move {
            match stream.next().await {
                Some(item) => {
                    if let Ok(bytes) = &item {
                        log.response_bytes += bytes.len() as u64;
                    }
                    Some((item, (stream, log)))
                }
                None => None,
            }
        },
    );
    Response::from_parts(parts, Body::from_stream(stream))
}
async fn handle_inner(State(app): State<App>, req: Request<Body>) -> Response<Body> {
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
    if req.method() == "GET"
        && req.uri().path() == "/health"
        && app.modes.get("health") == brigade_hub::modes::Mode::Native
    {
        return Response::builder()
            .header("content-type", "application/json")
            .header("server", "brigade-fleet-hub/1")
            .header("cache-control", "no-store")
            .header("referrer-policy", "no-referrer")
            .body(Body::from(brigade_hub::shadow::HEALTH_BODY))
            .unwrap();
    }
    if req.method() == "GET" && req.uri().path() == "/metrics" {
        if !admin_authorized(&app, &req) {
            return json(
                StatusCode::UNAUTHORIZED,
                serde_json::json!({"error":"unauthorized"}),
            );
        }
        let mut text = app.telemetry.render();
        text += &format!(
            "brigade_hub_proxy_buffer_fallback_total {}\nbrigade_hub_upstream_up {}\n",
            app.buffer_fallbacks
                .load(std::sync::atomic::Ordering::Relaxed),
            u8::from(
                app.ready
                    .lock()
                    .unwrap()
                    .is_some_and(|last| last.elapsed() < Duration::from_secs(5))
            )
        );
        if let Some(storage) = &app.storage {
            let m = storage.metrics.lock().unwrap();
            text += &format!(
                "brigade_hub_checkpoint_busy_total {}\nbrigade_hub_checkpoint_failures_total {}\nbrigade_hub_checkpoint_wal_frames {}\nbrigade_hub_checkpointed_frames {}\nbrigade_hub_wal_bytes {}\nbrigade_hub_checkpoint_stall_frames {}\n",
                m.busy,
                m.failures,
                m.log_frames,
                m.checkpointed_frames,
                m.wal_bytes,
                (m.log_frames - m.checkpointed_frames).max(0)
            );
        } else {
            text += "brigade_hub_checkpoint_stall_frames NaN\n";
        }
        if let Some(memory_route::MemoryRoute::Store(store)) = &app.memory {
            let m = store.metrics();
            text += &format!(
                "brigade_hub_memory_batches_total {}\nbrigade_hub_memory_hub_lock_residence_seconds_last {}\nbrigade_hub_memory_hub_lock_residence_seconds_max {}\nbrigade_hub_memory_hub_lock_residence_seconds_sum {}\nbrigade_hub_memory_hub_lock_wait_seconds_max {}\nbrigade_hub_memory_authority_timeouts_total {}\nbrigade_hub_memory_commit_failures_total {}\nbrigade_hub_memory_residence_bucket_seconds {}\n",
                m.batches,
                m.residence_last,
                m.residence_max,
                m.residence_sum,
                m.wait_max,
                m.authority_timeouts,
                m.commit_failures,
                store.bucket()
            );
        }
        return Response::builder()
            .header("content-type", "text/plain; version=0.0.4; charset=utf-8")
            .header("cache-control", "no-store")
            .body(Body::from(text))
            .unwrap();
    }
    if req.method() == "GET" && req.uri().path() == "/ready" {
        if app.storage.as_ref().is_some_and(|s| !s.schema.ready()) {
            return busy("schema-changed");
        }
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
    // Native memory answers framing and size refusals itself, each with its
    // memory error code (D21), in `MemoryRoute::admit`.
    let memory_request = app.memory.is_some() && req.uri().path() == "/memory";
    if !memory_request
        && (req.headers().contains_key("transfer-encoding")
            || req.headers().get_all("content-length").iter().count() > 1)
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
    if length > BODY_LIMIT as u64 && !memory_request {
        return body_too_large();
    }
    if let Some(storage) = &app.storage {
        if req.method() != "GET" && !storage.schema.ready() {
            return busy("schema-changed");
        }
        // S6's event/session writes are non-durable admission classes. Other
        // families adopt the gate with their writer commands as they go native.
        // Memory reads stay available under pressure; memory writes are
        // refused after the body shows the action.
        if req.method() == "POST" && matches!(req.uri().path(), "/events" | "/sessions") {
            if let Err(reason) = storage.disk.admit(false) {
                return busy(reason);
            }
        }
    }
    let browser = admission::browser(req.uri().path());
    let credential = match &app.lookup {
        Some(lookup) => match lookup
            .credential(
                req.headers()
                    .get("authorization")
                    .and_then(|v| v.to_str().ok()),
            )
            .await
        {
            Ok(value) => value,
            Err(()) => return busy("credential-lookup"),
        },
        None => None,
    };
    let source = req
        .extensions()
        .get::<SocketAddr>()
        .map(|p| {
            if app.front_bind && p.ip().is_loopback() {
                "front".into()
            } else {
                p.ip().to_string()
            }
        })
        .unwrap_or_default();
    let login = if app.trust_identity {
        req.headers()
            .get("tailscale-user-login")
            .and_then(|v| v.to_str().ok())
    } else {
        None
    };
    let Some(credential) = app
        .budgets
        .admit(credential, req.uri().path(), &source, login)
    else {
        return busy("credential-inflight-full");
    };
    let headers_only = credential.lane != admission::Lane::Credential && length > 256 * 1024;
    let (parts, body) = req.into_parts();
    let native = app
        .registration
        .as_ref()
        .filter(|_| registration::Registration::route(parts.method.as_str(), parts.uri.path()))
        // Python refuses any `token` query parameter with 401 before routing. Leave
        // those requests to the proxy so the refusal and its bytes stay Python's.
        .filter(|_| !query_names_token(parts.uri.query()));
    if let Some(native) = native {
        if let Some(refusal) = native.auth(&parts).await {
            return refusal;
        }
    }
    let claims = app
        .claims
        .as_ref()
        .filter(|_| claims_route::Claims::route(parts.method.as_str(), parts.uri.path()))
        .filter(|_| !query_names_token(parts.uri.query()));
    let claims = match claims {
        Some(claims) => match claims.auth(&parts).await {
            Ok(node) => Some((claims, node)),
            Err(refusal) => return *refusal,
        },
        None => None,
    };
    // Native memory authenticates and checks size before any body byte is read.
    let memory = match app
        .memory
        .as_ref()
        .filter(|_| parts.uri.path() == "/memory")
    {
        Some(route) => match route.admit(&parts).await {
            Ok(credential) => Some((route, credential)),
            Err(refusal) => return *refusal,
        },
        None => None,
    };
    let _budget = if headers_only {
        vec![]
    } else {
        match credential.reserve(length as u32).await {
            Ok(permits) => permits,
            Err(()) => return busy("body-budget-full"),
        }
    };
    // Large unvalidated bodies are never read or reserved. Python refuses them
    // from headers. Dropping the incoming stream closes the client connection.
    let body = if headers_only { Body::empty() } else { body };
    let collected = http_body_util::Limited::new(body, BODY_LIMIT).collect();
    let body = match tokio::time::timeout(Duration::from_secs(30), collected).await {
        Ok(Ok(collected)) => collected.to_bytes(),
        Ok(Err(e)) if memory.is_some() => {
            return memory_route::body_refusal(e.is::<http_body_util::LengthLimitError>());
        }
        Err(_) if memory.is_some() => return memory_route::timeout(),
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
    if let Some((route, credential)) = memory {
        if let Some(reason) = app
            .storage
            .as_ref()
            .and_then(|s| brigade_hub::memory::pressure_refusal(&s.disk, &body))
        {
            return busy(reason);
        }
        return route.handle(&credential, &body).await;
    }
    if let Some(native) = native {
        // serde_json rejects NaN, Infinity, 1e400 and lone surrogate escapes that
        // Python's json.loads accepts. Python answers those bodies itself.
        if body.is_empty() || serde_json::from_slice::<serde_json::Value>(&body).is_ok() {
            return native.handle(&parts, &body).await;
        }
    }
    if let Some((claims, node)) = claims {
        // The same serde_json gap as registration: Python answers those bodies.
        if body.is_empty() || serde_json::from_slice::<serde_json::Value>(&body).is_ok() {
            if let Some(response) = claims.handle(&parts, node, &body).await {
                return response;
            }
        }
    }
    let header_timeout = long_poll_deadline(req_path(&parts), &body);
    if let Some(slot) = parts.extensions.get::<FamilySlot>() {
        if parts.method == axum::http::Method::POST && req_path(&parts) == "/messages" {
            let _ = slot.0.set(brigade_hub::modes::family(
                "POST",
                "/messages",
                &message_action(&body),
            ));
        }
    }
    let bodyless = body.is_empty();
    let outgoing = if headers_only {
        Body::from_stream(futures_util::stream::pending::<
            Result<axum::body::Bytes, std::io::Error>,
        >())
    } else {
        Body::from(body)
    };
    let mut req = Request::from_parts(parts, outgoing);
    let inflight = match credential.proxy() {
        Ok(slot) => slot,
        Err(reason) => return busy(reason),
    };
    if !app.trust_identity || !browser {
        req.headers_mut().remove("tailscale-user-login");
    }
    // Enrollment stays anonymous for admission, but its origin and Secure
    // cookie checks need the trusted terminator's scheme.
    let trusted_scheme_route = browser || req.uri().path() == "/enroll";
    if !app.trust_proto || !trusted_scheme_route {
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
            .insert("x-brigade-upstream-key", app.upstream_key.clone());
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
            drop(_budget);
            let response = response::prepare(
                response,
                browser,
                app.buffers.clone(),
                app.buffer_fallbacks.clone(),
                total_deadline,
            )
            .await;
            let (mut parts, body) = response.into_parts();
            if headers_only {
                parts.headers.insert("connection", "close".parse().unwrap());
            }
            let body = body.into_data_stream();
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
    let upstream_args = if let Some(clock) = &config.test_clock_file {
        let mut args = vec![
            "-c".to_owned(),
            include_str!("../../../registry/skills/verify-brigade/hub_clock.py").to_owned(),
            "--clock-file".into(),
            clock.clone(),
            "--".into(),
        ];
        args.extend(config.python_args(0).into_iter().skip(2));
        args
    } else {
        config.python_args(0)
    };
    let mut child =
        Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
            .args(upstream_args)
            .env("BRIGADE_HUB_MANAGED_LOOPBACK", key)
            .env("BRIGADE_HUB_UPSTREAM_KEY", key)
            .env("BRIGADE_HUB_CHECKPOINT_OWNER", "rust")
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
fn parse_upstream_key(key: &str, managed: bool) -> Result<axum::http::HeaderValue, String> {
    if managed && key.is_empty() {
        return Err("managed upstream key must not be empty".into());
    }
    axum::http::HeaderValue::from_str(key).map_err(|_| "invalid upstream key header".to_owned())
}

async fn run(mut config: Config) -> Result<(), String> {
    config.validate()?;
    // Resolve defaults and user expansion through the matching Python runtime.
    let output =
        Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
            .args(["-c", include_str!("../resolve_db.py")])
            .args(config.db.iter())
            .output()
            .await
            .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err("Python database path resolution failed".into());
    }
    let database: String = serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())?;
    config.db = Some(database.clone());
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
    let parsed_key = parse_upstream_key(&key, config.upstream.is_none())?;
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
    let path = std::path::PathBuf::from(&database);
    let modes = brigade_hub::modes::Modes::parse(&config.route_mode)?;
    let admin = std::env::var("BRIGADE_FLEET_TOKEN")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(|v| v.trim().to_owned())
        .or_else(|| {
            config
                .token_file
                .as_ref()
                .and_then(|p| std::fs::read_to_string(expand_user(p)).ok())
                .map(|v| v.trim().to_owned())
        })
        .ok_or("no fleet hub token")?;
    let readers = Arc::new(brigade_hub::read::ReaderPool::open(
        &path,
        brigade_hub::read::hub_threads(),
        brigade_hub::read::HUB_QUEUE,
    )?);
    let arbiter = Arc::new(brigade_hub::writer::Arbiter::default());
    let memory_path = path.with_file_name("fleet-hub-memory.db");
    let memory =
        match modes.configured("memory") {
            None => None,
            Some(brigade_hub::modes::Mode::Native) => Some(match brigade_hub::memory::Store::open(
                brigade_hub::memory::Options {
                    memory: memory_path.clone(),
                    hub: path.clone(),
                    admin_digest: registration::digest(&admin),
                    clock: brigade_hub::memory::ids::Clock {
                        file: config.test_clock_file.clone().map(Into::into),
                    },
                    scanner: Arc::new(brigade_hub::memory::scan::Unavailable),
                    arbiter: arbiter.clone(),
                    hook: None,
                },
                readers.clone(),
            ) {
                Ok(store) => memory_route::MemoryRoute::Store(Arc::new(store)),
                Err(e) => {
                    eprintln!("memory unavailable: {e}");
                    memory_route::MemoryRoute::Unavailable
                }
            }),
            Some(_) => Some(memory_route::MemoryRoute::Unavailable),
        };
    let storage = if config.upstream.is_none() {
        let sweeps: Arc<dyn brigade_hub::maintenance::WriterCommands> = match &memory {
            Some(memory_route::MemoryRoute::Store(store)) => Arc::new(MemorySweeps(store.clone())),
            _ => Arc::new(brigade_hub::maintenance::NoSweeps),
        };
        Some(brigade_hub::maintenance::Maintenance::start(
            &path,
            &memory_path,
            sweeps,
        )?)
    } else {
        None
    };
    let cpu = brigade_hub::read::CpuPool::default();
    let mut state = App::new(
        upstream,
        client,
        config.trust_tailscale_identity,
        config.trust_forwarded_proto,
    );
    state.telemetry.attach_readers(readers.queue_probe());
    if let Some(memory_route::MemoryRoute::Store(store)) = &memory {
        state.telemetry.attach_readers(store.reader_probe());
    }
    state.telemetry.attach_cpu(cpu.queue_probe());
    // Only a native family's writer commits here. Without one the writer
    // metrics stay NaN, because the Python upstream commits every write.
    state.modes = modes;
    state.memory = memory;
    state.arbiter = arbiter.clone();
    state.shadow_db = Some(path.clone());
    state.storage = storage.map(Arc::new);
    state.upstream_key = parsed_key;
    state.front_bind = address.ip().is_loopback();
    {
        use sha2::Digest;
        state.admin_digest = Some(sha2::Sha256::digest(admin.as_bytes()).into());
    }
    state.lookup = Some(admission::Lookup::new(readers.clone(), &admin));
    let native = |family| state.modes.get(family) == brigade_hub::modes::Mode::Native;
    // One S4a writer per file, shared by every native family. It owns the writer metrics.
    let writer = if native("registration") || native("claims") {
        let writer = brigade_hub::writer::Writer::start(
            brigade_hub::writer::Config::new(path.clone()),
            arbiter.clone(),
        )
        .map_err(|_| "native writer unavailable")?;
        state.telemetry.attach_writer(writer.stats());
        Some(writer)
    } else {
        None
    };
    if native("registration") {
        state.registration = Some(registration::Registration {
            readers: readers.clone(),
            writer: writer.clone().unwrap(),
            admin: registration::digest(&admin),
            clock: config.test_clock_file.clone(),
        });
    }
    if native("claims") {
        state.claims = Some(claims_route::Claims {
            readers,
            writer: writer.unwrap(),
            admin: registration::digest(&admin),
            clock: config.test_clock_file.clone(),
            allow_admin_writes: config.allow_admin_writes,
            events: brigade_hub::claims::events_enabled(),
        });
    }
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
                let source = if front_peer(address.ip(), peer.ip()) { None } else {
                    let Some(slot) = sources.acquire(peer.ip().to_string(), 256) else { continue; };
                    Some(slot)
                };
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
/// Maintenance asks for retention every second; only memory retention is native.
struct MemorySweeps(Arc<brigade_hub::memory::Store>);
impl brigade_hub::maintenance::WriterCommands for MemorySweeps {
    fn schedule(&self, sweep: brigade_hub::maintenance::Sweep) -> bool {
        sweep == brigade_hub::maintenance::Sweep::MemoryRetention && self.0.sweep()
    }
}
fn front_peer(bind: IpAddr, peer: IpAddr) -> bool {
    bind.is_loopback() && peer.is_loopback()
}
async fn serve(
    stream: tokio::net::TcpStream,
    service: TowerToHyperService<Router>,
    mut stopping: watch::Receiver<bool>,
    _global: OwnedSemaphorePermit,
    _source: Option<Slot>,
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
    fn upstream_keys_are_validated_once() {
        assert!(parse_upstream_key("", true).is_err());
        assert!(parse_upstream_key("", false).is_ok());
        for key in ["fake\nkey", "fake\rkey", "fake\u{7f}key"] {
            assert!(parse_upstream_key(key, true).is_err());
            assert!(parse_upstream_key(key, false).is_err());
        }
        assert_eq!(parse_upstream_key("fake-key", true).unwrap(), "fake-key");
    }

    #[test]
    fn token_query_names_match_python_parse_qs() {
        assert!(query_names_token(Some("token=x")));
        assert!(query_names_token(Some("a=1&token")));
        assert!(query_names_token(Some("%74oken=x")));
        assert!(!query_names_token(Some("tokens=x&a=token")));
        assert!(!query_names_token(None));
    }
    #[test]
    fn message_action_sets_the_request_family() {
        use brigade_hub::modes::family;
        let of = |body: &[u8]| family("POST", "/messages", &message_action(body));
        assert_eq!(of(br#"{"action":"events_list"}"#), "cold");
        assert_eq!(of(br#"{"action":"send"}"#), "messages");
        assert_eq!(of(b"not json"), "messages");
    }
    #[test]
    fn long_poll_uses_body_and_caps_wait() {
        assert_eq!(
            long_poll_deadline("/messages", br#"{"action":"list","wait_ms":12000}"#),
            Duration::from_secs(22)
        );
        assert_eq!(
            long_poll_deadline("/messages", br#"{"action":"list","wait_ms":999999}"#),
            Duration::from_secs(30)
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
        let _slots = app.budgets.lanes[2]
            .clone()
            .acquire_many_owned(4)
            .await
            .unwrap();
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
        let slots: Vec<_> = (0..8)
            .map(|_| app.budgets.admit(None, "/nodes", "", None).unwrap())
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
        let _slots = app.budgets.lanes[2]
            .clone()
            .acquire_many_owned(4)
            .await
            .unwrap();
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

        server.await.unwrap();
    }
}

#[cfg(test)]
mod tests_s3c;

#[cfg(test)]
mod tests_s9;
