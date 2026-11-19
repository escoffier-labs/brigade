//! Reader-pool routing at the route boundary, and the request log's
//! credential label. Both are behaviours `handle` owns, so they are tested
//! through it with a hub database and native routes wired the way `run` wires
//! them.
use super::*;
use brigade_hub::read::{HubPools, ReaderPool};
use sha2::Digest;
use std::path::{Path, PathBuf};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
    mpsc as std_mpsc,
};

const ADMIN: &str = "pool-admin-token-0123456789";
const NODE_TOKEN: &str = "pool-node-token-0123456789";
const PRINCIPAL_TOKEN: &str = "pool-principal-token-0123456789";
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
    /// A hub database with one live node and one live message principal.
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
    path
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
    request_lines::credential_kind(Arc::as_ptr(&app.telemetry) as usize, family)
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

/// The request-log label: `anonymous` only when no bearer token was presented,
/// `unchecked` when one was but no lookup named it, and the named kind when a
/// lookup finished.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn the_request_log_names_or_marks_every_credential() {
    let fixture = Fixture::new("labels");
    let app = fixture.app();
    let cold = |authorization: Option<String>, uri: &str| {
        let mut request = Request::builder().method("GET").uri(uri);
        if let Some(value) = authorization {
            request = request.header("authorization", value);
        }
        request.body(Body::empty()).unwrap()
    };
    let mut wrong = Vec::new();
    fn check(wrong: &mut Vec<String>, name: &str, expected: &str, app: &App) {
        let seen = logged(app, "cold");
        if seen.as_deref() != Some(expected) {
            wrong.push(format!("{name}: expected {expected}, logged {seen:?}"));
        }
    }
    // No token.
    let _ = answer(&app, cold(None, "/fake")).await;
    check(&mut wrong, "no token", "anonymous", &app);
    // A node token and a principal token.
    let _ = answer(&app, cold(Some(bearer(NODE_TOKEN)), "/fake")).await;
    check(&mut wrong, "node token", "node", &app);
    let _ = answer(&app, cold(Some(bearer(PRINCIPAL_TOKEN)), "/fake")).await;
    check(&mut wrong, "principal token", "principal", &app);
    // Rejected before the lookup, with a token presented.
    let _ = answer(&app, cold(Some(bearer(NODE_TOKEN)), "/fake?token=x")).await;
    check(&mut wrong, "early rejection", "unchecked", &app);
    // The lookup itself times out: hold every lookup reader past its deadline.
    let held = hold_every(&fixture.lookups, brigade_hub::read::LOOKUP_THREADS);
    let (status, value) = answer(&app, cold(Some(bearer(NODE_TOKEN)), "/fake")).await;
    held.release().await;
    if value["reason"] != "credential-lookup" || status != 503 {
        wrong.push(format!(
            "lookup timeout: expected 503 credential-lookup, got {status} {value}"
        ));
    }
    check(&mut wrong, "lookup timeout", "unchecked", &app);
    assert!(wrong.is_empty(), "{wrong:#?}");
}
