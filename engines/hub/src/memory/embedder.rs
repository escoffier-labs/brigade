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

#[derive(Clone, Debug)]
pub struct Config {
    pub backend: String,
    pub pull: bool,
    /// Pull mode only: the operator asserts that every worker holding the
    /// embed grant runs on their own machines, so the hub may lease private
    /// text to it. The query endpoint says nothing about where documents go.
    pub pull_private: bool,
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
        // Like `retrieval`, a value of the wrong type is a typo, never a default.
        let pull = match v.get("pull") {
            None => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => return Err("memory.embedder.pull must be true or false".into()),
        };
        let pull_private = match v.get("pull_private") {
            None => false,
            Some(Value::Bool(b)) => *b,
            Some(_) => return Err("memory.embedder.pull_private must be true or false".into()),
        };
        if pull_private && !pull {
            return Err("memory.embedder.pull_private requires memory.embedder.pull = true".into());
        }
        let endpoint = v["query_endpoint"]
            .as_str()
            .or_else(|| v["endpoint"].as_str())
            .unwrap_or("")
            .to_string();
        if endpoint.is_empty() && !pull {
            return Err("memory.embedder.endpoint is required".into());
        }
        let empty = Vec::new();
        Ok(Self {
            backend: s("backend")?,
            pull,
            pull_private,
            endpoint: endpoint.clone(),
            token_file: if endpoint.is_empty() {
                PathBuf::new()
            } else {
                s("token_file")?.into()
            },
            allowlist: v
                .get("allowlist")
                .and_then(Value::as_array)
                .or({
                    if endpoint.is_empty() {
                        Some(&empty)
                    } else {
                        None
                    }
                })
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
/// Whether text that scans private (S18, D24 amendment) may go to this
/// endpoint. The private class guards connector audiences, and an embedder
/// counts as one unless its pinned address is loopback. The rule matches
/// [`unscanned_permitted`]: a loopback literal, or plain HTTP `localhost`
/// until the pinned address is known. A private-network address, a public
/// name and HTTPS to any name are refused.
pub(crate) fn private_permitted(url: &Url, pinned: Option<IpAddr>) -> bool {
    unscanned_permitted(url, pinned)
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
/// Where the `local` backend may send text: the hub host or an internal
/// service beside it. Plain HTTP passes here and is held to a private or
/// loopback address by the pinned check before every connect. HTTPS connects
/// through its own resolver, so only a loopback or private IP literal passes.
/// A name over HTTPS, an allowlisted public one included, is refused.
pub(crate) fn local_destination_permitted(url: &Url) -> bool {
    match url.scheme() {
        "http" => true,
        "https" => url.host_str().is_some_and(|h| {
            h.trim_matches(['[', ']'])
                .parse::<IpAddr>()
                .is_ok_and(private)
        }),
        _ => false,
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
        if config.backend == "local" && !local_destination_permitted(&url) {
            return Err("the local embedder backend needs a loopback or internal service endpoint: plain HTTP to a private address, or HTTPS to a loopback or private IP literal".into());
        }
        let mut open = std::fs::OpenOptions::new();
        open.read(true);
        // Nonblocking, so a FIFO with no writer fails the regular-file check
        // below instead of holding startup in open(2).
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            open.custom_flags(libc::O_NONBLOCK);
        }
        let file = open
            .open(&config.token_file)
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
        // Private text goes only to a loopback endpoint.
        let mut unscanned = false;
        let mut private_text = false;
        for text in &texts {
            match self.scanner.scan(text) {
                Scan::Clean { .. } => {}
                Scan::Private { .. } => private_text = true,
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
        // Before any lookup or connection.
        if private_text && !private_permitted(&self.url, None) {
            return Err(EmbedError::new("content-private"));
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
            // `localhost` mapped to a LAN host gets no private text.
            if private_text && !private_permitted(&url, Some(addresses[0].ip())) {
                return Err(EmbedError::new("content-private"));
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
            // 400, 413 and 422 refuse this input, not the endpoint. The job
            // layer retries such a batch one document at a time.
            let mut err = EmbedError::new(match status {
                401 | 403 => "embedder-refused",
                400 | 413 | 422 => "embedder-rejected",
                _ => "embedder-unavailable",
            });
            if status == 429 || status == 503 {
                err.retry_after = response
                    .headers()
                    .get("Retry-After")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| {
                        s.parse::<u64>().ok().map(Duration::from_secs).or_else(|| {
                            httpdate::parse_http_date(s)
                                .ok()
                                .and_then(|t| t.duration_since(std::time::SystemTime::now()).ok())
                        })
                    })
                    .map(|d| d.clamp(Duration::from_secs(1), Duration::from_secs(60)));
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
        let mut s = self.0.lock();
        s.queries -= 1;
        s.quiet_until = Instant::now() + QUIET;
    }
}
struct ProbeGuard(Weak<Runtime>);
impl Drop for ProbeGuard {
    fn drop(&mut self) {
        if let Some(runtime) = self.0.upgrade() {
            runtime.lock().probe = false;
        }
    }
}
struct DocumentGuard(Arc<Runtime>);
impl Drop for DocumentGuard {
    fn drop(&mut self) {
        self.0.lock().document = false;
    }
}
impl Runtime {
    pub fn new(config: Config, scanner: Arc<dyn Scanner>) -> Result<Arc<Self>, String> {
        if config.pull && config.endpoint.is_empty() {
            struct Disabled(EmbedderId);
            impl Embedder for Disabled {
                fn id(&self) -> &EmbedderId {
                    &self.0
                }
                fn embed(&self, _: Kind, _: Vec<String>, _: Duration) -> Embedding {
                    Box::pin(async { Err(EmbedError::new("embedder-unavailable")) })
                }
            }
            let disabled = Arc::new(Disabled(config.id.clone()));
            return Ok(Self::with(config, disabled));
        }
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
    /// The state holds counters and timestamps only, and nothing panics while
    /// holding it, so a poisoned lock still holds consistent state.
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }
    pub fn reason(&self) -> Option<&'static str> {
        if self.config.endpoint.is_empty() {
            return Some("embedder-unavailable");
        }
        self.lock().reason
    }
    /// An expired window counts as closed, whatever the probe did.
    pub fn breaker_open(&self) -> bool {
        self.lock().open_until.is_some_and(|t| Instant::now() < t)
    }
    pub async fn query(self: &Arc<Self>, query: &str) -> Result<Vec<f32>, EmbedError> {
        let contention;
        {
            let mut s = self.lock();
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
        // An embedder supplied through `with` may answer any number of rows.
        // Only exactly one row counts as a healthy answer, checked before the
        // state lock is taken.
        let result = self
            .embedder
            .embed(Kind::Query, vec![text], QUERY_DEADLINE)
            .await
            .and_then(|mut rows| match (rows.pop(), rows.is_empty()) {
                (Some(row), true) => Ok(row),
                _ => Err(EmbedError::new("vector-invalid")),
            });
        match result {
            Ok(row) => {
                // A healthy answer ends any earlier fault, even if its probe failed.
                let mut s = self.lock();
                s.open_until = None;
                s.reason = None;
                Ok(row)
            }
            Err(e) => {
                if !(matches!(
                    e.reason,
                    "content-blocked" | "content-private" | "scan-unavailable"
                ) || e.reason == "embedder-timeout" && contention)
                {
                    self.fail(e.reason);
                }
                Err(e)
            }
        }
    }
    fn fail(self: &Arc<Self>, reason: &'static str) {
        let mut s = self.lock();
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
            if let (Ok(1), Some(runtime)) = (result.map(|rows| rows.len()), weak.upgrade()) {
                let mut s = runtime.lock();
                s.open_until = None;
                s.reason = None;
            }
        });
    }
    #[cfg(test)]
    pub fn reset_backoff(&self) {
        let mut s = self.lock();
        s.retry_at = Instant::now();
        s.quiet_until = Instant::now();
    }
    /// Whether a document request could start now. The scheduler checks
    /// this before it leases, so no lease waits out a backoff.
    pub fn documents_ready(&self) -> bool {
        let s = self.lock();
        let now = Instant::now();
        !(s.document || s.probe || s.queries > 0 || now < s.quiet_until || now < s.retry_at)
    }
    pub async fn documents(
        self: &Arc<Self>,
        texts: Vec<(String, String)>,
    ) -> Result<Vec<Vec<f32>>, EmbedError> {
        // The stored version's receipt is required even when a fresh scan would pass.
        // `clean` qualifies anywhere: S18 never pushes a `flagged` version.
        // `private` qualifies only for a loopback endpoint, and `send` checks
        // the pinned address again before it connects.
        if texts
            .iter()
            .any(|(_, state)| !matches!(state.as_str(), "clean" | "private"))
        {
            return Err(EmbedError::new("scan-pending"));
        }
        if texts.iter().any(|(_, state)| state == "private")
            && !Url::parse(&self.config.endpoint).is_ok_and(|url| private_permitted(&url, None))
        {
            return Err(EmbedError::new("content-private"));
        }
        {
            let mut s = self.lock();
            if s.document
                || s.probe
                || s.queries > 0
                || Instant::now() < s.quiet_until
                || Instant::now() < s.retry_at
            {
                return Err(EmbedError::new("embedder-busy"));
            }
            s.document = true;
        }
        let _guard = DocumentGuard(self.clone());
        let count = texts.len();
        let result = match self
            .embedder
            .embed(
                Kind::Document,
                texts
                    .into_iter()
                    .map(|(t, _)| t.chars().take(self.config.input_chars).collect())
                    .collect(),
                DOCUMENT_DEADLINE,
            )
            .await
        {
            Ok(rows) if rows.len() != count => Err(EmbedError::new("vector-invalid")),
            other => other,
        };
        let mut s = self.lock();
        if let Err(e) = &result {
            if matches!(
                e.reason,
                "embedder-unavailable"
                    | "embedder-timeout"
                    | "embedder-refused"
                    | "digest-mismatch"
                    | "endpoint-refused"
            ) {
                s.reason = Some(e.reason);
            }
            s.retry_at = Instant::now() + e.retry_after.unwrap_or(Duration::from_secs(s.backoff));
            s.backoff = (s.backoff * 2).min(60);
        } else {
            s.backoff = 1;
            if s.open_until.is_none() {
                s.reason = None;
            }
        }
        result
    }
}
