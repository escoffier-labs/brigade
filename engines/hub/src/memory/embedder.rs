//! HTTP embedding boundary. Credentials and text never appear in errors.
use super::scan::{Scan, Scanner};
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Bytes;
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::{
    client::legacy::{Client, connect::HttpConnector},
    rt::TokioExecutor,
};
use serde_json::{Value, json};
use std::{
    future::Future,
    net::IpAddr,
    path::PathBuf,
    pin::Pin,
    sync::{Arc, Mutex, Weak},
    time::Duration,
};
use tokio::time::Instant;
use url::Url;

pub const QUERY_DEADLINE: Duration = Duration::from_millis(250);
pub const DOCUMENT_DEADLINE: Duration = Duration::from_secs(30);
pub const PROBE_DEADLINE: Duration = Duration::from_secs(10);
pub const BREAKER_WINDOW: Duration = Duration::from_secs(30);
const QUIET: Duration = Duration::from_secs(1);
const POOL_IDLE: Duration = Duration::from_secs(30);
const POOL_IDLE_PER_HOST: usize = 2;
pub const BATCH_BYTES: usize = 32 * 1024;
pub const BATCH_CARDS: usize = 4;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EmbedderId {
    pub name: String,
    pub model_digest: String,
    pub dimensions: usize,
    pub normalizer_version: u32,
    pub query_prefix_version: u32,
}
impl EmbedderId {
    pub fn key(&self) -> String {
        super::ids::sha256_hex(
            json!([
                self.name,
                self.model_digest,
                self.dimensions,
                self.normalizer_version,
                self.query_prefix_version
            ])
            .to_string()
            .as_bytes(),
        )
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Query,
    Document,
    Probe,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EmbedError {
    pub reason: &'static str,
    pub retry_after: Option<Duration>,
}
impl EmbedError {
    fn new(reason: &'static str) -> Self {
        Self {
            reason,
            retry_after: None,
        }
    }
}
pub type Embedding = Pin<Box<dyn Future<Output = Result<Vec<Vec<f32>>, EmbedError>> + Send>>;
pub trait Embedder: Send + Sync {
    fn id(&self) -> &EmbedderId;
    fn embed(&self, kind: Kind, texts: Vec<String>, deadline: Duration) -> Embedding;
}

#[derive(Clone)]
pub struct Config {
    pub backend: String,
    pub endpoint: String,
    pub allowlist: Vec<String>,
    pub token_file: PathBuf,
    pub shape: String,
    pub id: EmbedderId,
    pub query_prefix: String,
    pub input_chars: usize,
}
impl Config {
    pub fn parse(v: &Value) -> Result<Self, String> {
        let s = |k: &str| {
            v[k].as_str()
                .filter(|s| !s.is_empty())
                .map(String::from)
                .ok_or_else(|| format!("memory.embedder.{k} is required"))
        };
        let n = |k: &str| {
            v[k].as_u64()
                .filter(|n| *n > 0 && *n <= 1_000_000)
                .map(|n| n as usize)
                .ok_or_else(|| format!("invalid memory.embedder.{k}"))
        };
        Ok(Self {
            backend: s("backend")?,
            endpoint: s("endpoint")?,
            token_file: s("token_file")?.into(),
            allowlist: v["allowlist"]
                .as_array()
                .ok_or("embedder allowlist is required")?
                .iter()
                .map(|x| {
                    x.as_str()
                        .map(String::from)
                        .ok_or("invalid allowlist entry".into())
                })
                .collect::<Result<_, String>>()?,
            shape: s("shape")?,
            query_prefix: v["query_prefix"].as_str().unwrap_or("").into(),
            input_chars: n("input_chars")?,
            id: EmbedderId {
                name: s("name")?,
                model_digest: s("model_digest")?,
                dimensions: n("dimensions")?,
                normalizer_version: 1,
                query_prefix_version: n("query_prefix_version")? as u32,
            },
        })
    }
}

pub fn normalize_vector(mut v: Vec<f32>, dimensions: usize) -> Result<Vec<f32>, EmbedError> {
    let norm: f64 = v.iter().map(|x| (*x as f64).powi(2)).sum::<f64>().sqrt();
    if v.len() != dimensions || !norm.is_finite() || norm == 0.0 {
        return Err(EmbedError::new("vector-invalid"));
    }
    for x in &mut v {
        *x = (*x as f64 / norm) as f32;
    }
    Ok(v)
}

pub struct HttpEmbedder {
    config: Config,
    url: Url,
    token: hyper::header::HeaderValue,
    client: Client<HttpsConnector<HttpConnector>, Full<Bytes>>,
    scanner: Arc<dyn Scanner>,
}
fn loopback_literal(url: &Url) -> bool {
    url.host_str().is_some_and(|h| {
        h.trim_matches(['[', ']'])
            .parse::<IpAddr>()
            .is_ok_and(|i| i.is_loopback())
    })
}
/// Whether text the scanner could not check may go to this endpoint. Only a
/// loopback address qualifies, never a name. Plain HTTP decides on the pinned
/// address after resolution (`pinned`), so `localhost` mapped to a LAN host
/// gets nothing. HTTPS connects through its own resolver, so only a loopback
/// IP literal qualifies there. Before resolution (`pinned` is `None`) a plain
/// HTTP name may still pass, and the pinned check runs before connecting.
pub(crate) fn unscanned_permitted(url: &Url, pinned: Option<IpAddr>) -> bool {
    match (url.scheme(), pinned) {
        ("http", Some(ip)) => ip.is_loopback(),
        ("http", None) => loopback_literal(url) || url.host_str() == Some("localhost"),
        ("https", _) => loopback_literal(url),
        _ => false,
    }
}
/// The token file belongs to the hub's effective user and nobody else can
/// read or write it.
pub(crate) fn token_file_permitted(mode: u32, owner: u32, euid: u32) -> bool {
    owner == euid && matches!(mode & 0o777, 0o600 | 0o400)
}
fn private(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            v.is_loopback() || v.is_private() || (u32::from(v) & 0xffc0_0000 == 0x6440_0000)
        }
        IpAddr::V6(v) => v.is_loopback() || (v.segments()[0] & 0xfe00 == 0xfc00),
    }
}
impl HttpEmbedder {
    pub fn new(config: Config, scanner: Arc<dyn Scanner>) -> Result<Arc<Self>, String> {
        let url = Url::parse(&config.endpoint).map_err(|_| "invalid embedder endpoint")?;
        if !matches!(config.backend.as_str(), "local" | "remote")
            || !matches!(config.shape.as_str(), "api/embed" | "v1/embeddings")
            || config.id.dimensions > 16384
        {
            return Err("invalid embedder backend, shape or dimensions".into());
        }
        if !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || !matches!(url.scheme(), "http" | "https")
            || !config.allowlist.contains(&config.endpoint)
        {
            return Err(
                "embedder endpoint is outside the allowlist or has forbidden URL fields".into(),
            );
        }
        let file = std::fs::File::open(&config.token_file)
            .map_err(|_| "embedder token file unavailable")?;
        let meta = file
            .metadata()
            .map_err(|_| "embedder token file unavailable")?;
        if !meta.is_file() || meta.len() > 8192 {
            return Err("invalid embedder token file".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            // SAFETY: geteuid has no preconditions and cannot fail.
            let euid = unsafe { libc::geteuid() };
            if !token_file_permitted(meta.permissions().mode(), meta.uid(), euid) {
                return Err(
                    "embedder token file must be owned by the hub user with mode 0600 or 0400"
                        .into(),
                );
            }
        }
        use std::io::Read;
        let mut token = String::new();
        file.take(8193)
            .read_to_string(&mut token)
            .map_err(|_| "invalid embedder token")?;
        let token = token.trim();
        if token.is_empty() {
            return Err("empty embedder token".into());
        }
        let mut token = hyper::header::HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|_| "invalid embedder token")?;
        token.set_sensitive(true);
        let https = HttpsConnectorBuilder::new()
            .with_webpki_roots()
            .https_or_http()
            .enable_http1()
            .build();
        // A small idle pool keeps a TCP and TLS handshake out of most 250 ms
        // query budgets. The pool keys on the request URI's scheme and
        // authority. Plain HTTP rewrites that authority to the pinned address,
        // so a pooled connection is reused only for the same checked address.
        // A request on a pooled connection the server already closed is
        // retried on a new one, inside the same deadline.
        let client = Client::builder(TokioExecutor::new())
            .pool_timer(hyper_util::rt::TokioTimer::new())
            .pool_idle_timeout(POOL_IDLE)
            .pool_max_idle_per_host(POOL_IDLE_PER_HOST)
            .build(https);
        Ok(Arc::new(Self {
            config,
            url,
            token,
            client,
            scanner,
        }))
    }
    pub fn host(&self) -> &str {
        self.url.host_str().unwrap_or("")
    }
    async fn send(&self, kind: Kind, texts: Vec<String>) -> Result<Vec<Vec<f32>>, EmbedError> {
        if texts.is_empty()
            || texts.len()
                > if kind == Kind::Document {
                    BATCH_CARDS
                } else {
                    1
                }
            || texts.iter().map(String::len).sum::<usize>() > BATCH_BYTES
        {
            return Err(EmbedError::new("embedder-batch-invalid"));
        }
        // Scan the exact prefixed/truncated payload, not an earlier representation.
        // Flagged text is never pushed (S18), so only a clean scan leaves the host.
        let mut unscanned = false;
        for text in &texts {
            match self.scanner.scan(text) {
                Scan::Clean { .. } => {}
                Scan::Unavailable if kind != Kind::Document => unscanned = true,
                Scan::Unavailable => return Err(EmbedError::new("scan-unavailable")),
                Scan::Flagged { .. } | Scan::Blocked { .. } | Scan::Failed => {
                    return Err(EmbedError::new("content-blocked"));
                }
            }
        }
        if unscanned && !unscanned_permitted(&self.url, None) {
            return Err(EmbedError::new("scan-unavailable"));
        }
        let mut url = self.url.clone();
        if url.scheme() == "http" {
            // Resolve once and pin the connect address. No DNS rebinding between policy and connect.
            let addresses: Vec<_> = tokio::net::lookup_host((
                self.host().trim_matches(['[', ']']),
                url.port_or_known_default().unwrap(),
            ))
            .await
            .map_err(|_| EmbedError::new("embedder-unavailable"))?
            .collect();
            if addresses.is_empty() || addresses.iter().any(|a| !private(a.ip())) {
                return Err(EmbedError::new("endpoint-refused"));
            }
            if unscanned && !unscanned_permitted(&url, Some(addresses[0].ip())) {
                return Err(EmbedError::new("scan-unavailable"));
            }
            url.set_ip_host(addresses[0].ip())
                .map_err(|_| EmbedError::new("endpoint-refused"))?;
        }
        let payload = if self.config.shape == "api/embed" {
            json!({"model": self.config.id.name, "input": texts, "truncate": false})
        } else {
            json!({"model": self.config.id.name, "input": texts, "encoding_format": "float"})
        };
        let req = hyper::Request::post(url.as_str())
            .header("Host", self.url.authority())
            .header("Authorization", self.token.clone())
            .header("Content-Type", "application/json")
            .body(Full::new(Bytes::from(payload.to_string())))
            .map_err(|_| EmbedError::new("endpoint-refused"))?;
        let response = self
            .client
            .request(req)
            .await
            .map_err(|_| EmbedError::new("embedder-unavailable"))?;
        let status = response.status().as_u16();
        if status != 200 {
            let mut err = EmbedError::new(if status == 401 || status == 403 {
                "embedder-refused"
            } else {
                "embedder-unavailable"
            });
            if status == 429 || status == 503 {
                err.retry_after = response
                    .headers()
                    .get("Retry-After")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| s.parse::<u64>().ok())
                    .map(|s| Duration::from_secs(s.clamp(1, 60)));
            }
            return Err(err);
        }
        let header_digest = response
            .headers()
            .get("x-model-digest")
            .and_then(|s| s.to_str().ok())
            .map(String::from);
        let bytes = Limited::new(
            response.into_body(),
            (self.config.id.dimensions * texts.len() * 32 + 8192).min(4 << 20),
        )
        .collect()
        .await
        .map_err(|_| EmbedError::new("embedder-response-invalid"))?
        .to_bytes();
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| EmbedError::new("embedder-response-invalid"))?;
        let digest = value["model_digest"].as_str().or(header_digest.as_deref());
        if digest != Some(self.config.id.model_digest.as_str())
            || header_digest
                .as_ref()
                .is_some_and(|d| d != &self.config.id.model_digest)
        {
            return Err(EmbedError::new("digest-mismatch"));
        }
        let vectors: Vec<Vec<f32>> = if self.config.shape == "api/embed" {
            serde_json::from_value(value["embeddings"].clone())
                .map_err(|_| EmbedError::new("vector-invalid"))?
        } else {
            let data = value["data"]
                .as_array()
                .ok_or_else(|| EmbedError::new("vector-invalid"))?;
            let mut rows = vec![None; texts.len()];
            for row in data {
                let i = row["index"]
                    .as_u64()
                    .filter(|i| *i < rows.len() as u64)
                    .ok_or_else(|| EmbedError::new("vector-invalid"))?
                    as usize;
                if rows[i].is_some() {
                    return Err(EmbedError::new("vector-invalid"));
                }
                rows[i] = Some(
                    serde_json::from_value(row["embedding"].clone())
                        .map_err(|_| EmbedError::new("vector-invalid"))?,
                );
            }
            rows.into_iter()
                .collect::<Option<Vec<_>>>()
                .ok_or_else(|| EmbedError::new("vector-invalid"))?
        };
        if vectors.len() != texts.len() {
            return Err(EmbedError::new("vector-invalid"));
        }
        vectors
            .into_iter()
            .map(|v| normalize_vector(v, self.config.id.dimensions))
            .collect()
    }
}
impl Embedder for Arc<HttpEmbedder> {
    fn id(&self) -> &EmbedderId {
        &self.config.id
    }
    fn embed(&self, kind: Kind, texts: Vec<String>, deadline: Duration) -> Embedding {
        let this = self.clone();
        Box::pin(async move {
            tokio::time::timeout(deadline, this.send(kind, texts))
                .await
                .unwrap_or_else(|_| Err(EmbedError::new("embedder-timeout")))
        })
    }
}

struct State {
    queries: usize,
    document: bool,
    quiet_until: Instant,
    open_until: Option<Instant>,
    reason: Option<&'static str>,
    probe: bool,
    retry_at: Instant,
    backoff: u64,
}
pub struct Runtime {
    pub config: Config,
    embedder: Arc<dyn Embedder>,
    state: Mutex<State>,
}
struct QueryGuard(Arc<Runtime>);
impl Drop for QueryGuard {
    fn drop(&mut self) {
        let mut s = self.0.state.lock().unwrap();
        s.queries -= 1;
        s.quiet_until = Instant::now() + QUIET;
    }
}
struct ProbeGuard(Weak<Runtime>);
impl Drop for ProbeGuard {
    fn drop(&mut self) {
        if let Some(runtime) = self.0.upgrade() {
            if let Ok(mut s) = runtime.state.lock() {
                s.probe = false;
            }
        }
    }
}
struct DocumentGuard(Arc<Runtime>);
impl Drop for DocumentGuard {
    fn drop(&mut self) {
        self.0.state.lock().unwrap().document = false;
    }
}
impl Runtime {
    pub fn new(config: Config, scanner: Arc<dyn Scanner>) -> Result<Arc<Self>, String> {
        let http = HttpEmbedder::new(config.clone(), scanner)?;
        Ok(Self::with(config, Arc::new(http)))
    }
    pub fn with(config: Config, embedder: Arc<dyn Embedder>) -> Arc<Self> {
        Arc::new(Self {
            config,
            embedder,
            state: Mutex::new(State {
                queries: 0,
                document: false,
                quiet_until: Instant::now(),
                open_until: None,
                reason: None,
                probe: false,
                retry_at: Instant::now(),
                backoff: 1,
            }),
        })
    }
    pub fn reason(&self) -> Option<&'static str> {
        self.state.lock().unwrap().reason
    }
    /// An expired window counts as closed, whatever the probe did.
    pub fn breaker_open(&self) -> bool {
        self.state
            .lock()
            .unwrap()
            .open_until
            .is_some_and(|t| Instant::now() < t)
    }
    pub async fn query(self: &Arc<Self>, query: &str) -> Result<Vec<f32>, EmbedError> {
        let contention;
        {
            let mut s = self.state.lock().unwrap();
            if s.open_until.is_some_and(|t| Instant::now() < t) {
                return Err(EmbedError::new(s.reason.unwrap_or("embedder-unavailable")));
            }
            if s.queries >= 4 {
                return Err(EmbedError::new("embedder-busy"));
            }
            s.queries += 1;
            contention = s.document;
        }
        let _guard = QueryGuard(self.clone());
        let text = format!("{}{}", self.config.query_prefix, query);
        let result = self
            .embedder
            .embed(Kind::Query, vec![text], QUERY_DEADLINE)
            .await;
        match result {
            Ok(mut rows) => {
                // A healthy answer ends any earlier fault, even if its probe failed.
                let mut s = self.state.lock().unwrap();
                s.open_until = None;
                s.reason = None;
                Ok(rows.remove(0))
            }
            Err(e) => {
                if !(matches!(e.reason, "content-blocked" | "scan-unavailable")
                    || e.reason == "embedder-timeout" && contention)
                {
                    self.fail(e.reason);
                }
                Err(e)
            }
        }
    }
    fn fail(self: &Arc<Self>, reason: &'static str) {
        let mut s = self.state.lock().unwrap();
        s.reason = Some(reason);
        s.open_until = Some(Instant::now() + BREAKER_WINDOW);
        if s.probe {
            return;
        }
        s.probe = true;
        let weak: Weak<Self> = Arc::downgrade(self);
        tokio::spawn(async move {
            // Only the embedder is held during the HTTP call. Dropping a store does not keep a runtime alive.
            let Some(runtime) = weak.upgrade() else {
                return;
            };
            let embedder = runtime.embedder.clone();
            let text = format!("{}health probe", runtime.config.query_prefix);
            drop(runtime);
            // Resets the flag even if the embedder panics, so a later failure probes again.
            let _reset = ProbeGuard(weak.clone());
            let result = embedder
                .embed(Kind::Probe, vec![text], PROBE_DEADLINE)
                .await;
            if let (Ok(_), Some(runtime)) = (result, weak.upgrade()) {
                let mut s = runtime.state.lock().unwrap();
                s.open_until = None;
                s.reason = None;
            }
        });
    }
    pub async fn documents(
        self: &Arc<Self>,
        texts: Vec<(String, String)>,
    ) -> Result<Vec<Vec<f32>>, EmbedError> {
        // The stored version's receipt is required even when a fresh scan would pass.
        // Only `clean` qualifies: S18 never pushes a `flagged` version.
        if texts.iter().any(|(_, state)| state != "clean") {
            return Err(EmbedError::new("scan-pending"));
        }
        {
            let mut s = self.state.lock().unwrap();
            if s.document
                || s.queries > 0
                || Instant::now() < s.quiet_until
                || Instant::now() < s.retry_at
            {
                return Err(EmbedError::new("embedder-busy"));
            }
            s.document = true;
        }
        let _guard = DocumentGuard(self.clone());
        let result = self
            .embedder
            .embed(
                Kind::Document,
                texts
                    .into_iter()
                    .map(|(t, _)| t.chars().take(self.config.input_chars).collect())
                    .collect(),
                DOCUMENT_DEADLINE,
            )
            .await;
        let mut s = self.state.lock().unwrap();
        if let Err(e) = &result {
            s.retry_at = Instant::now() + e.retry_after.unwrap_or(Duration::from_secs(s.backoff));
            s.backoff = (s.backoff * 2).min(60);
        } else {
            s.backoff = 1;
        }
        result
    }
}
