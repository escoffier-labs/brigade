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
    collections::HashMap,
    net::{IpAddr, Ipv4Addr, SocketAddr},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    net::{TcpListener, TcpSocket},
    process::{Child, Command},
    sync::{OwnedSemaphorePermit, Semaphore},
};

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
    json(
        StatusCode::SERVICE_UNAVAILABLE,
        serde_json::json!({"error":"hub busy", "reason":reason}),
    )
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
async fn handle(State(app): State<App>, mut req: Request<Body>) -> Response<Body> {
    // Query credentials are refused before routing, including on native health.
    if req
        .uri()
        .query()
        .is_some_and(|q| url::form_urlencoded::parse(q.as_bytes()).any(|(k, _)| k == "token"))
    {
        return Response::builder()
            .status(401)
            .header("content-type", "text/plain; charset=utf-8")
            .header("cache-control", "no-store")
            .header("referrer-policy", "no-referrer")
            .body(Body::from("Dashboard request refused.\n"))
            .unwrap();
    }
    if req.method() == "GET" && req.uri().path() == "/health" {
        return json(
            StatusCode::OK,
            serde_json::json!({"ok":true,"service":"brigade-fleet-hub"}),
        );
    }
    let key = req
        .headers()
        .get("authorization")
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
        .unwrap_or_default();
    let Some(credential) = app.credentials.acquire(key, 64) else {
        return busy("credential-inflight-full");
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
    *req.uri_mut() = format!(
        "{}{}",
        app.upstream,
        req.uri()
            .path_and_query()
            .map(|v| v.as_str())
            .unwrap_or("/")
    )
    .parse()
    .unwrap();
    let (parts, body) = req.into_parts();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    let stream = futures_util::stream::unfold(
        (body.into_data_stream(), 0usize),
        move |(mut stream, total)| async move {
            match tokio::time::timeout_at(deadline, stream.next()).await {
                Ok(Some(Ok(bytes))) if total + bytes.len() <= 8 * 1024 * 1024 => {
                    let n = total + bytes.len();
                    Some((Ok(bytes), (stream, n)))
                }
                Ok(None) => None,
                _ => Some((
                    Err(std::io::Error::other("request body limit or timeout")),
                    (stream, usize::MAX / 2),
                )),
            }
        },
    );
    let request = Request::from_parts(parts, Body::from_stream(stream));
    // Header wait includes the upstream's bounded long poll. Never retry writes.
    match tokio::time::timeout(Duration::from_secs(35), app.client.request(request)).await {
        Ok(Ok(response)) => {
            let (mut parts, body) = response.into_parts();
            strip_hop(&mut parts.headers);
            let stream = body.into_data_stream();
            // Hold credential admission through the last response byte or disconnect.
            let stream = futures_util::stream::unfold(
                (stream, credential),
                |(mut stream, slot)| async move {
                    stream.next().await.map(|item| (item, (stream, slot)))
                },
            );
            Response::from_parts(parts, Body::from_stream(stream))
        }
        _ => busy("upstream-unavailable"),
    }
}
async fn upstream(config: &Config, client: &HttpClient) -> Result<(String, Option<Child>), String> {
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
    let probe = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .map_err(|e| e.to_string())?;
    let port = probe.local_addr().unwrap().port();
    drop(probe);
    let mut child =
        Command::new(std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into()))
            .args(config.python_args(port))
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| e.to_string())?;
    let url = format!("http://{}:{port}", Ipv4Addr::LOCALHOST);
    for _ in 0..300 {
        if child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("Python upstream refused startup configuration".into());
        }
        let req = Request::builder()
            .uri(format!("{url}/health"))
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
    let client = Client::builder(TokioExecutor::new())
        .retry_canceled_requests(false)
        .pool_max_idle_per_host(32)
        .build_http();
    let (upstream, mut child) = upstream(&config, &client).await?;
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
    let app = Router::new().fallback(any(handle)).with_state(App {
        upstream,
        client,
        credentials: Arc::default(),
        trust_identity: config.trust_tailscale_identity,
        trust_proto: config.trust_forwarded_proto,
    });
    let connections = Arc::new(Semaphore::new(config.max_connections as usize));
    let sources = Arc::<Counts>::default();
    let mut tasks = tokio::task::JoinSet::new();
    let mut signal = Box::pin(async {
        let mut term =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
        tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term.recv() => {} }
    });
    loop {
        tokio::select! {
            _ = &mut signal => break,
            _ = tasks.join_next(), if !tasks.is_empty() => {},
            accepted = listener.accept() => {
                let (stream, peer) = accepted.map_err(|e|e.to_string())?;
                let Ok(global) = connections.clone().try_acquire_owned() else { continue; };
                let Some(source) = sources.acquire(peer.ip().to_string(), 256) else { continue; };
                let service = TowerToHyperService::new(app.clone());
                tasks.spawn(async move { serve(stream, peer, service, global, source).await; });
            }
        }
    }
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    if let Some(ref mut child) = child {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    Ok(())
}
async fn serve(
    stream: tokio::net::TcpStream,
    _peer: SocketAddr,
    service: TowerToHyperService<Router>,
    _global: OwnedSemaphorePermit,
    _source: Slot,
) {
    let _ = hyper::server::conn::http1::Builder::new()
        .timer(TokioTimer::new())
        .header_read_timeout(Duration::from_secs(10))
        .max_headers(100)
        .serve_connection(TokioIo::new(stream), service)
        .await;
}
#[tokio::main]
async fn main() {
    if let Err(e) = run(Config::parse()).await {
        eprintln!("error: {e}");
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let app = App {
            upstream: format!("http://{}:1", Ipv4Addr::LOCALHOST),
            client: Client::builder(TokioExecutor::new()).build_http(),
            credentials: Arc::default(),
            trust_identity: false,
            trust_proto: false,
        };
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
        assert_eq!(down.status(), 503);
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
        let app = App {
            upstream,
            client: Client::builder(TokioExecutor::new()).build_http(),
            credentials: Arc::default(),
            trust_identity: false,
            trust_proto: false,
        };
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
