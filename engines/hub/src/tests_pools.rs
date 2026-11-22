//! Reader-pool routing at the route boundary, and the request log's
//! credential label. Both are behaviours `handle` owns, so they are tested
//! through it with a hub database and native routes wired the way `run` wires
//! them.
use super::*;
use brigade_hub::read::{HubPools, ReaderPool};
use brigade_hub::telemetry::{RequestLog, Stage, StageSlot};
use sha2::Digest;
use std::ops::{Deref, DerefMut};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
    mpsc as std_mpsc,
};
use tokio::net::TcpListener;

static REQUEST_LINES: Mutex<Vec<(usize, &'static str, String)>> = Mutex::new(Vec::new());

/// Capture the same formatter stderr uses when the log is dropped, including
/// when a request is cancelled before producing a response.
pub(super) struct CapturedLog(pub RequestLog);

impl Deref for CapturedLog {
    type Target = RequestLog;

    fn deref(&self) -> &RequestLog {
        &self.0
    }
}

impl DerefMut for CapturedLog {
    fn deref_mut(&mut self) -> &mut RequestLog {
        &mut self.0
    }
}

impl Drop for CapturedLog {
    fn drop(&mut self) {
        let line = self.line().to_string();
        let key = Arc::as_ptr(&self.telemetry) as usize;
        REQUEST_LINES.lock().unwrap().push((key, self.family, line));
    }
}

fn take_request_line(app: &App, family: &str) -> Option<(serde_json::Value, String)> {
    let key = Arc::as_ptr(&app.telemetry) as usize;
    let mut lines = REQUEST_LINES.lock().unwrap();
    let index = lines
        .iter()
        .rposition(|(app, f, _)| *app == key && *f == family)?;
    let (_, _, text) = lines.remove(index);
    drop(lines);
    let fields = serde_json::from_str(&text).expect("valid request log JSON");
    Some((fields, text))
}

const ADMIN: &str = "pool-admin-token-0123456789";
const NODE_TOKEN: &str = "pool-node-token-0123456789";
const PRINCIPAL_TOKEN: &str = "pool-principal-token-0123456789";
const REVOKED_NODE_TOKEN: &str = "pool-revoked-node-token-0123456789";
const REVOKED_PRINCIPAL_TOKEN: &str = "pool-revoked-principal-token-0123456789";
const DUAL_TOKEN: &str = "pool-dual-token-0123456789";
const UNKNOWN_TOKEN: &str = "pool-unknown-token-0123456789";
const NODE_ID: &str = "pool-node-1";
const PRINCIPAL_ID: &str = "principal:pool-1";

/// A hub database the Python schema owner created, the pools `run` opens on
/// it, one writer, and an app with every native route this test needs.
struct Fixture {
    dir: PathBuf,
    db: PathBuf,
    readers: Arc<ReaderPool>,
    lookups: Arc<ReaderPool>,
    writer: brigade_hub::writer::Writer,
    arbiter: Arc<brigade_hub::writer::Arbiter>,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}
impl Fixture {
    fn new(name: &str) -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let dir = std::env::temp_dir().join(format!(
            "hub-pools-{name}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let db = hub_db(&dir);
        let pools = HubPools::open(&db).unwrap();
        let arbiter = Arc::new(brigade_hub::writer::Arbiter::default());
        let writer = brigade_hub::writer::Writer::start(
            brigade_hub::writer::Config::new(db.clone()),
            arbiter.clone(),
        )
        .unwrap();
        Self {
            dir,
            db,
            readers: Arc::new(pools.readers),
            lookups: Arc::new(pools.lookups),
            writer,
            arbiter,
        }
    }
    /// A hub database with one live node, one live message principal, one
    /// revoked node, one revoked principal, and one digest present in both
    /// tables.
    fn app(&self) -> App {
        let mut app = App::new(
            format!("http://{}:1", Ipv4Addr::LOCALHOST),
            Client::builder(TokioExecutor::new()).build_http(),
            false,
            false,
        );
        app.admin_digest = Some(sha2::Sha256::digest(ADMIN.as_bytes()).into());
        app.lookup = Some(admission::Lookup::new(self.lookups.clone(), ADMIN));
        app
    }
    fn admin(&self) -> String {
        registration::digest(ADMIN)
    }
}

fn hub_db(dir: &Path) -> PathBuf {
    let path = dir.join("fleet-hub.db");
    let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
    let status = std::process::Command::new(python)
        .args(["-c", "from pathlib import Path; import sys; from brigade.fleet_hub import init_db; init_db(Path(sys.argv[1])).close()"])
        .arg(&path)
        .status()
        .unwrap();
    assert!(status.success());
    let connection = rusqlite::Connection::open(&path).unwrap();
    let node = registration::digest(NODE_TOKEN);
    connection
        .execute(
            "INSERT INTO nodes (node_id,token_sha256,label,created_at,revoked_at) VALUES (?1,?2,NULL,'2026-01-01T00:00:00Z',NULL)",
            [NODE_ID, node.as_str()],
        )
        .unwrap();
    let principal = registration::digest(PRINCIPAL_TOKEN);
    connection
        .execute(
            "INSERT INTO fleet_message_principals (principal_id,name,label,token_hash,created_at,revoked_at,enrollment_id) VALUES (?1,?1,'test',?2,'2026-01-01T00:00:00Z',NULL,NULL)",
            [PRINCIPAL_ID, principal.as_str()],
        )
        .unwrap();
    let revoked_node = registration::digest(REVOKED_NODE_TOKEN);
    connection
        .execute(
            "INSERT INTO nodes (node_id,token_sha256,label,created_at,revoked_at) VALUES ('pool-node-2',?1,NULL,'2026-01-01T00:00:00Z','2026-01-02T00:00:00Z')",
            [revoked_node.as_str()],
        )
        .unwrap();
    let revoked_principal = registration::digest(REVOKED_PRINCIPAL_TOKEN);
    connection
        .execute(
            "INSERT INTO fleet_message_principals (principal_id,name,label,token_hash,created_at,revoked_at,enrollment_id) VALUES ('principal:pool-2','principal:pool-2','test',?1,'2026-01-01T00:00:00Z','2026-01-02T00:00:00Z',NULL)",
            [revoked_principal.as_str()],
        )
        .unwrap();
    // The same digest in both tables: the node row must name it, as on main,
    // and the principal row must not add a privilege.
    let dual = registration::digest(DUAL_TOKEN);
    connection
        .execute(
            "INSERT INTO nodes (node_id,token_sha256,label,created_at,revoked_at) VALUES ('pool-dual-node',?1,NULL,'2026-01-01T00:00:00Z',NULL)",
            [dual.as_str()],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO fleet_message_principals (principal_id,name,label,token_hash,created_at,revoked_at,enrollment_id) VALUES ('principal:pool-dual','principal:pool-dual','test',?1,'2026-01-01T00:00:00Z',NULL,NULL)",
            [dual.as_str()],
        )
        .unwrap();
    path
}

#[tokio::test]
async fn front_admission_credential_kind_and_key_for_principals() {
    let fixture = Fixture::new("principal-classification");
    let lookup = admission::Lookup::new(fixture.lookups.clone(), ADMIN);
    for (token, expected_kind, expected_key) in [
        (
            PRINCIPAL_TOKEN,
            "principal",
            Some(registration::digest(PRINCIPAL_TOKEN)),
        ),
        (REVOKED_PRINCIPAL_TOKEN, "anonymous", None),
        (UNKNOWN_TOKEN, "anonymous", None),
    ] {
        let credential = lookup.credential(Some(&bearer(token))).await.unwrap();
        assert_eq!(
            (credential.kind, credential.key),
            (expected_kind, expected_key)
        );
    }
}

fn bearer(token: &str) -> String {
    format!("Bearer {token}")
}

/// Every handler reader is held by one gated slow job. A request served while
/// this is in scope can only reach the handler pool by waiting for it.
struct Held {
    gates: Vec<std_mpsc::Sender<()>>,
    jobs: Vec<tokio::task::JoinHandle<()>>,
}
impl Held {
    async fn release(self) {
        for gate in self.gates {
            let _ = gate.send(());
        }
        for job in self.jobs {
            let _ = job.await;
        }
    }
}
fn hold_every(pool: &Arc<ReaderPool>, threads: usize) -> Held {
    let mut gates = Vec::new();
    let mut jobs = Vec::new();
    for _ in 0..threads {
        let (entered_tx, entered) = std_mpsc::channel::<()>();
        let (gate, released) = std_mpsc::channel::<()>();
        let pool = pool.clone();
        jobs.push(tokio::spawn(async move {
            let _ = pool
                .submit(move |_| {
                    let _ = entered_tx.send(());
                    let _ = released.recv();
                    Ok(())
                })
                .await;
        }));
        entered
            .recv_timeout(Duration::from_secs(10))
            .expect("a reader never picked up the held job");
        gates.push(gate);
    }
    Held { gates, jobs }
}

async fn answer(app: &App, request: Request<Body>) -> (u16, serde_json::Value) {
    let response = handle(State(app.clone()), request).await;
    let status = response.status().as_u16();
    let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or_default())
}

fn logged(app: &App, family: &str) -> Option<String> {
    take_request_line(app, family)
        .and_then(|(line, _)| line["credential_kind"].as_str().map(str::to_owned))
}

fn log_test_app(upstream: String) -> App {
    App::new(
        upstream,
        Client::builder(TokioExecutor::new()).build_http(),
        false,
        false,
    )
}

async fn log_request(
    app: &App,
    method: &str,
    uri: &str,
    family: &str,
) -> (serde_json::Value, String) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .body(Body::empty())
        .unwrap();
    let (status, _) = answer(app, request).await;
    let (line, text) = take_request_line(app, family).expect("request log line");
    assert_eq!(line["status"], status);
    (line, text)
}

#[tokio::test]
async fn request_log_labels_native_health_and_drops_query() {
    let app = log_test_app(String::new());
    let (line, text) =
        log_request(&app, "GET", "/health?fixture_query=fixture_value", "health").await;
    assert_eq!(line["status"], 200);
    assert_eq!(line["method"], "GET");
    assert_eq!(line["route"], "/health");
    assert!(!text.contains("fixture_query"));
    assert!(!text.contains("fixture_value"));
}

#[tokio::test]
async fn request_log_labels_proxied_cold_policy() {
    let listener = TcpListener::bind((Ipv4Addr::from([127, 0, 0, 1]), 0))
        .await
        .unwrap();
    let app = log_test_app(format!("http://{}", listener.local_addr().unwrap()));
    let upstream = Router::new().route(
        "/policy",
        axum::routing::post(|| async { StatusCode::NO_CONTENT }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });
    let (line, _) = log_request(&app, "POST", "/policy", "cold").await;
    server.abort();
    assert_eq!(line["status"], 204);
    assert_eq!(line["method"], "POST");
    assert_eq!(line["route"], "/policy");
}

#[tokio::test]
async fn request_log_labels_immediate_admission_refusal() {
    let app = log_test_app(String::new());
    let _slots: Vec<_> = (0..8)
        .map(|_| app.budgets.admit(None, "/policy", "", None).unwrap())
        .collect();
    let (line, _) = log_request(&app, "POST", "/policy", "cold").await;
    assert_eq!(line["status"], 503);
    assert_eq!(line["stage"], "replying");
    assert_eq!(line["method"], "POST");
    assert_eq!(line["route"], "/policy");
}

#[tokio::test]
async fn request_log_labels_refusals_before_admission() {
    let app = log_test_app(String::new());
    let (invalid, _) = log_request(&app, "OPTIONS", "*", "cold").await;
    assert_eq!(invalid["status"], 400);
    assert_eq!(invalid["method"], "OPTIONS");
    assert_eq!(invalid["route"], "other");

    let (refused, text) =
        log_request(&app, "GET", "/policy?token=fixture-query-value", "cold").await;
    assert_eq!(refused["status"], 401);
    assert_eq!(refused["method"], "GET");
    assert_eq!(refused["route"], "/policy");
    assert!(!text.contains("token="));
    assert!(!text.contains("fixture-query-value"));
}

#[tokio::test]
async fn request_log_labels_proxied_route_templates() {
    let app = log_test_app(String::new());
    for (path, family, label) in [
        (
            "/deck/actions/aa_fixture-id",
            "browser",
            "/deck/actions/:id",
        ),
        ("/deck/actions/aa_", "browser", "other"),
        (
            "/deck/observed/fixture-id",
            "browser",
            "/deck/observed/:group",
        ),
        ("/work/campaigns/fixture-id", "cold", "/work/campaigns/:key"),
    ] {
        let (line, text) = log_request(&app, "GET", path, family).await;
        assert_eq!(line["method"], "GET", "{path}");
        assert_eq!(line["route"], label, "{path}");
        assert!(!text.contains("fixture-id"));
    }
}

#[tokio::test]
async fn request_log_replaces_ids_with_placeholders() {
    let app = log_test_app(String::new());
    let (line, text) = log_request(
        &app,
        "DELETE",
        "/work/items/fixture-item/links/fixture-link",
        "cold",
    )
    .await;
    assert_eq!(line["method"], "DELETE");
    assert_eq!(line["route"], "/work/items/:id/links/:link");
    assert!(!text.contains("fixture-item"));
    assert!(!text.contains("fixture-link"));
}

#[tokio::test]
async fn request_log_masks_unknown_path_and_method() {
    let app = log_test_app(String::new());
    let (line, text) = log_request(
        &app,
        "FIXTURE-METHOD",
        "/work/items/fixture-item/fixture-unknown",
        "cold",
    )
    .await;
    assert_eq!(line["method"], "other");
    assert_eq!(line["route"], "other");
    assert!(!text.contains("FIXTURE-METHOD"));
    assert!(!text.contains("fixture-unknown"));
}

/// The read-only audit records the hundreds of `PRAGMA table_xinfo` and
/// `index_xinfo` statements a schema snapshot prepares as writes. A checked
/// pool verifies its schema when it opens, so the first job on each reader,
/// which is what a request reaches during the audit window, must not repeat
/// that snapshot. The lookup pool doubled the set of readers doing it.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_checked_pool_checks_its_schema_at_open_not_on_its_first_job() {
    let fixture = Fixture::new("schema-cookie");
    // Every reader handles one job while all of them are held: those are the
    // first jobs a reader ever runs.
    let held = hold_every(&fixture.readers, brigade_hub::read::hub_threads());
    let lookups = hold_every(&fixture.lookups, brigade_hub::read::hub_threads());
    held.release().await;
    lookups.release().await;
    assert_eq!(
        fixture.readers.schema_checks(),
        0,
        "a first handler job re-checked the schema"
    );
    assert_eq!(
        fixture.lookups.schema_checks(),
        0,
        "a first lookup job re-checked the schema"
    );
}

/// The review's second read: each native route authenticates again, and on the
/// handler pool that read is the one that answered `credential-lookup`.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn native_route_auth_never_waits_on_a_held_handler_pool() {
    let fixture = Fixture::new("native-route-auth");
    let mut app = fixture.app();
    app.registration = Some(registration::Registration {
        readers: fixture.readers.clone(),
        lookups: fixture.lookups.clone(),
        writer: fixture.writer.clone(),
        admin: fixture.admin(),
        clock: None,
        memory: None,
    });
    app.claims = Some(claims_route::Claims {
        readers: fixture.readers.clone(),
        lookups: fixture.lookups.clone(),
        writer: fixture.writer.clone(),
        admin: fixture.admin(),
        clock: None,
        allow_admin_writes: true,
        events: false,
    });
    app.heartbeat = Some(heartbeat::Heartbeat {
        readers: fixture.readers.clone(),
        lookups: fixture.lookups.clone(),
        writer: fixture.writer.clone(),
        admin: fixture.admin(),
        clock: None,
        allow_admin_writes: true,
        events: false,
        stale_after: 900,
    });
    app.messages = Some(messages_route::Messages::new(
        fixture.readers.clone(),
        fixture.lookups.clone(),
        fixture.writer.clone(),
        fixture.admin(),
        None,
        true,
        brigade_hub::messages::Policy::from_env(|_| None, false).unwrap(),
        Arc::new(brigade_hub::messages::wake::Wake::new(
            brigade_hub::messages::wake::Limits::for_connections(64),
        )),
    ));
    let held = hold_every(&fixture.readers, brigade_hub::read::hub_threads());
    let node = bearer(NODE_TOKEN);
    let requests = [
        (
            "claims",
            Request::builder()
                .method("GET")
                .uri("/claims")
                .header("authorization", &node)
                .body(Body::empty())
                .unwrap(),
        ),
        (
            "registration",
            Request::builder()
                .method("GET")
                .uri("/nodes")
                .header("authorization", &node)
                .body(Body::empty())
                .unwrap(),
        ),
        (
            "messages",
            Request::builder()
                .method("POST")
                .uri("/messages")
                .header("authorization", &node)
                .header("content-length", 2)
                .body(Body::from("{}"))
                .unwrap(),
        ),
        (
            "heartbeat",
            Request::builder()
                .method("POST")
                .uri("/events")
                .header("authorization", &node)
                .header("content-length", 2)
                .body(Body::from("{}"))
                .unwrap(),
        ),
    ];
    let mut refused = Vec::new();
    for (name, request) in requests {
        let (status, value) = answer(&app, request).await;
        eprintln!("{name}: status={status} body={value}");
        if value["reason"] == "credential-lookup" {
            refused.push(format!("{name} (status {status}, {value})"));
        }
    }
    held.release().await;
    assert!(
        refused.is_empty(),
        "route auth read a held handler pool: {refused:#?}"
    );
}

/// A request the client abandons while a native route's own credential lookup
/// is still queued keeps that stage. The line exists to name where a request
/// stalled, and the route's auth read is what the route itself waits on.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn abandoned_route_lookup_keeps_the_credential_lookup_stage() {
    let fixture = Fixture::new("stage-route-lookup");
    let mut app = fixture.app();
    // Skip the front's own lookup so the route's is the read that stalls.
    app.lookup = None;
    app.heartbeat = Some(heartbeat::Heartbeat {
        readers: fixture.readers.clone(),
        lookups: fixture.lookups.clone(),
        writer: fixture.writer.clone(),
        admin: fixture.admin(),
        clock: None,
        allow_admin_writes: true,
        events: false,
        stale_after: 900,
    });
    let depth = fixture.lookups.queue_probe();
    let held = hold_every(&fixture.lookups, brigade_hub::read::hub_threads());
    let stage = StageSlot::default();
    let mut request = Request::builder()
        .method("GET")
        .uri("/status")
        .header("authorization", bearer(NODE_TOKEN))
        .body(Body::empty())
        .unwrap();
    request.extensions_mut().insert(stage.clone());
    let task = tokio::spawn(handle(State(app.clone()), request));
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while depth() == 0 {
        assert!(
            std::time::Instant::now() < deadline,
            "the route lookup never queued"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // The client gives up: no reply is ever written, so the stage is the whole
    // record of where the request was.
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(stage.get(), Stage::CredentialLookup);
    held.release().await;
    let (line, _) =
        take_request_line(&app, "heartbeat").expect("request log line for the cancelled request");
    assert_eq!(line["stage"], "credential-lookup");
    assert_eq!(line["method"], "GET");
    assert_eq!(line["route"], "/status");
}

/// The memory route's credential read is `Store::authenticate`. Its harness
/// here is the route's own admission step, which is where that read sits.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn native_memory_auth_never_waits_on_a_held_handler_pool() {
    let fixture = Fixture::new("native-memory-auth");
    let store = Arc::new(
        brigade_hub::memory::Store::open(
            brigade_hub::memory::Options {
                memory: fixture.dir.join("fleet-hub-memory.db"),
                hub: fixture.db.clone(),
                admin_digest: fixture.admin(),
                clock: brigade_hub::memory::ids::Clock::default(),
                scanner: Arc::new(brigade_hub::memory::scan::Unavailable),
                arbiter: fixture.arbiter.clone(),
                hook: None,
            },
            fixture.readers.clone(),
            fixture.lookups.clone(),
        )
        .unwrap(),
    );
    let route = memory_route::MemoryRoute::Store(store);
    let (parts, _) = Request::builder()
        .method("POST")
        .uri("/memory")
        .header("authorization", bearer(ADMIN))
        .header("content-length", 2)
        .body(())
        .unwrap()
        .into_parts();
    let held = hold_every(&fixture.readers, brigade_hub::read::hub_threads());
    let admitted = route.admit(&parts).await;
    held.release().await;
    match admitted {
        Ok(_) => {}
        Err(response) => {
            let response = *response;
            let status = response.status().as_u16();
            let bytes = axum::body::to_bytes(response.into_body(), 1 << 20)
                .await
                .unwrap();
            let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap_or_default();
            panic!("memory auth read a held handler pool: status {status}, {value}");
        }
    }
}

/// The lookup pool is sized like the handler pool, so a flood of bearer values
/// is not a smaller choke point than it was when both shared one pool: a burst
/// larger than the old 64-job queue is accepted and answered rather than
/// refused, and a valid node token queued behind it still authenticates.
#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn a_lookup_burst_past_the_old_queue_depth_still_answers() {
    let fixture = Fixture::new("lookup-burst");
    let lookups = fixture.lookups.clone();
    let probe = lookups.queue_probe();
    let lookup = admission::Lookup::new(lookups.clone(), ADMIN);
    // Hold every lookup reader. The pool is at most `hub_threads()` wide, so
    // that many gated jobs leave every reader busy and any rest in the queue.
    let started = Arc::new(AtomicUsize::new(0));
    let mut gates = Vec::new();
    let mut holds = Vec::new();
    for _ in 0..brigade_hub::read::hub_threads() {
        let (gate, released) = std_mpsc::channel::<()>();
        let marked = started.clone();
        let pool = lookups.clone();
        holds.push(tokio::spawn(async move {
            let _ = pool
                .submit(move |_| {
                    marked.fetch_add(1, Ordering::SeqCst);
                    let _ = released.recv();
                    Ok(())
                })
                .await;
        }));
        gates.push(gate);
    }
    let hold_deadline = std::time::Instant::now() + Duration::from_secs(10);
    while started.load(Ordering::SeqCst) + probe() != brigade_hub::read::hub_threads() {
        assert!(
            std::time::Instant::now() < hold_deadline,
            "every lookup reader is busy"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let base = probe();
    // More than the old queue of 64 behind its two readers.
    const BURST: usize = 100;
    let mut burst = Vec::new();
    for i in 0..BURST {
        let lookup = lookup.clone();
        burst.push(tokio::spawn(async move {
            let token = format!("Bearer unknown-{i}");
            lookup.credential(Some(&token)).await
        }));
    }
    let queued_deadline = std::time::Instant::now() + Duration::from_secs(5);
    while probe() < base + BURST {
        assert!(
            std::time::Instant::now() < queued_deadline,
            "a burst of {BURST} bearer values is queued, not refused"
        );
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // Submitted behind the whole burst: it must still answer as a node.
    let node = {
        let lookup = lookup.clone();
        tokio::spawn(async move { lookup.credential(Some(&bearer(NODE_TOKEN))).await })
    };
    for gate in gates {
        let _ = gate.send(());
    }
    for hold in holds {
        let _ = hold.await;
    }
    for (i, task) in burst.into_iter().enumerate() {
        let found = task.await.unwrap().expect("a queued lookup is answered");
        assert_eq!(
            (found.kind, found.key),
            ("anonymous", None),
            "unknown token {i}"
        );
    }
    let found = node
        .await
        .unwrap()
        .expect("the node token behind the burst authenticates");
    assert_eq!(found.kind, "node");
    assert!(found.key.is_some());
}

/// The request-log label rule: `node`, `principal` or `admin` when a lookup
/// named the credential, `anonymous` when no token was presented or the
/// lookup completed with no live row, and `unchecked` when a token was
/// presented but no lookup completed for it. Asserted with the HTTP outcome.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_request_log_names_or_marks_every_credential() {
    let fixture = Fixture::new("labels");
    let mut app = fixture.app();
    app.registration = Some(registration::Registration {
        readers: fixture.readers.clone(),
        lookups: fixture.lookups.clone(),
        writer: fixture.writer.clone(),
        admin: fixture.admin(),
        clock: None,
        memory: None,
    });
    let request = |authorization: Option<String>, uri: &str| {
        let mut request = Request::builder().method("GET").uri(uri);
        if let Some(value) = authorization {
            request = request.header("authorization", value);
        }
        request.body(Body::empty()).unwrap()
    };
    struct Case {
        name: &'static str,
        family: &'static str,
        token: Option<&'static str>,
        uri: &'static str,
        status: u16,
        label: &'static str,
    }
    let cases = [
        // `GET /nodes` asks for a node token, so a live node is refused (403)
        // and a live principal is unauthorized (401) after its label is set.
        Case {
            name: "no token",
            family: "registration",
            token: None,
            uri: "/nodes",
            status: 401,
            label: "anonymous",
        },
        Case {
            name: "valid node token",
            family: "registration",
            token: Some(NODE_TOKEN),
            uri: "/nodes",
            status: 403,
            label: "node",
        },
        Case {
            name: "valid principal token",
            family: "registration",
            token: Some(PRINCIPAL_TOKEN),
            uri: "/nodes",
            status: 401,
            label: "principal",
        },
        // The lookup completed and found no live row: `anonymous`, as main
        // logged an unknown or revoked token.
        Case {
            name: "unknown token",
            family: "registration",
            token: Some(UNKNOWN_TOKEN),
            uri: "/nodes",
            status: 401,
            label: "anonymous",
        },
        Case {
            name: "revoked node token",
            family: "registration",
            token: Some(REVOKED_NODE_TOKEN),
            uri: "/nodes",
            status: 401,
            label: "anonymous",
        },
        Case {
            name: "revoked principal token",
            family: "registration",
            token: Some(REVOKED_PRINCIPAL_TOKEN),
            uri: "/nodes",
            status: 401,
            label: "anonymous",
        },
        // The digest is in both tables: the node row names it and gets only a
        // node's privilege (403 here), never the principal's or the admin's.
        Case {
            name: "digest in nodes and principals",
            family: "registration",
            token: Some(DUAL_TOKEN),
            uri: "/nodes",
            status: 403,
            label: "node",
        },
        Case {
            name: "admin token",
            family: "registration",
            token: Some(ADMIN),
            uri: "/nodes",
            status: 200,
            label: "admin",
        },
        // `/health` and `/ready` both belong to the health family. Main never
        // waited on a reader for their label, so they stay `unchecked` with
        // and without a token.
        Case {
            name: "health without token",
            family: "health",
            token: None,
            uri: "/health",
            status: 200,
            label: "unchecked",
        },
        Case {
            name: "health with token",
            family: "health",
            token: Some(NODE_TOKEN),
            uri: "/health",
            status: 200,
            label: "unchecked",
        },
        Case {
            name: "ready without token",
            family: "health",
            token: None,
            uri: "/ready",
            status: 503,
            label: "unchecked",
        },
        Case {
            name: "ready with token",
            family: "health",
            token: Some(NODE_TOKEN),
            uri: "/ready",
            status: 503,
            label: "unchecked",
        },
        // Rejected before the lookup, with a valid token presented.
        Case {
            name: "early rejection",
            family: "cold",
            token: Some(NODE_TOKEN),
            uri: "/fake?token=x",
            status: 401,
            label: "unchecked",
        },
    ];
    let mut wrong = Vec::new();
    for case in cases {
        let (status, value) = answer(&app, request(case.token.map(bearer), case.uri)).await;
        if status != case.status {
            wrong.push(format!(
                "{}: expected {case_status}, got {status} {value}",
                case.name,
                case_status = case.status
            ));
        }
        let seen = logged(&app, case.family);
        if seen.as_deref() != Some(case.label) {
            wrong.push(format!(
                "{}: expected {}, logged {seen:?}",
                case.name, case.label
            ));
        }
    }
    // The lookup itself times out: hold every lookup reader past its deadline.
    // It completed for no request, so the label stays `unchecked`.
    let held = hold_every(&fixture.lookups, brigade_hub::read::hub_threads());
    let (status, value) = answer(&app, request(Some(bearer(NODE_TOKEN)), "/nodes")).await;
    held.release().await;
    if value["reason"] != "credential-lookup" || status != 503 {
        wrong.push(format!(
            "lookup timeout: expected 503 credential-lookup, got {status} {value}"
        ));
    }
    let seen = logged(&app, "registration");
    if seen.as_deref() != Some("unchecked") {
        wrong.push(format!(
            "lookup timeout: expected unchecked, logged {seen:?}"
        ));
    }
    assert!(wrong.is_empty(), "{wrong:#?}");
}
