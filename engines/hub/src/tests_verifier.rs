//! Native enrollment binds the memory verifier flag (phase-memory-verify D4).
use super::*;
use brigade_hub::memory::{Options, Reply as MemoryReply, Store, ids, scan};
use serde_json::{Value as Json, json};
use std::path::{Path, PathBuf};

const ADMIN: &str = "verifier-admin-token-0123456789";

fn hub_db(dir: &Path) -> PathBuf {
    let path = dir.join("fleet-hub.db");
    let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
    let status = std::process::Command::new(python)
        .args(["-c", "from pathlib import Path; import sys; from brigade.fleet_hub import init_db; init_db(Path(sys.argv[1])).close()"])
        .arg(&path)
        .status()
        .unwrap();
    assert!(status.success());
    path
}

struct Fixture {
    dir: PathBuf,
    hub: PathBuf,
    store: Arc<Store>,
    registration: registration::Registration,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}
fn fixture() -> Fixture {
    let dir = std::env::temp_dir().join(format!(
        "hub-verifier-{}-{}",
        std::process::id(),
        ids::mint(0).chars().rev().take(8).collect::<String>()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    let hub = hub_db(&dir);
    let arbiter = Arc::new(brigade_hub::writer::Arbiter::default());
    let readers = Arc::new(brigade_hub::read::ReaderPool::open(&hub, 1, 64).unwrap());
    let store = Arc::new(
        Store::open(
            Options {
                memory: dir.join("fleet-hub-memory.db"),
                hub: hub.clone(),
                admin_digest: registration::digest(ADMIN),
                clock: ids::Clock::default(),
                scanner: Arc::new(scan::Unavailable),
                arbiter: arbiter.clone(),
                hook: None,
            },
            readers.clone(),
        )
        .unwrap(),
    );
    let writer =
        brigade_hub::writer::Writer::start(brigade_hub::writer::Config::new(hub.clone()), arbiter)
            .unwrap();
    store.attach_generation(Arc::new(
        brigade_hub::memory_verifier_generation::HubGeneration::new(writer.clone()),
    ));
    let registration = registration::Registration {
        readers,
        writer,
        admin: registration::digest(ADMIN),
        clock: None,
        memory: Some(store.clone()),
    };
    Fixture {
        dir,
        hub,
        store,
        registration,
    }
}
impl Fixture {
    async fn post(&self, path: &str, body: Json) -> (u16, Json) {
        let bytes = body.to_string();
        let (parts, ()) = Request::builder()
            .method("POST")
            .uri(path)
            .header("authorization", format!("Bearer {ADMIN}"))
            .header("content-length", bytes.len())
            .body(())
            .unwrap()
            .into_parts();
        let response = self.registration.handle(&parts, bytes.as_bytes()).await;
        let status = response.status().as_u16();
        let body = axum::body::to_bytes(response.into_body(), 1 << 20)
            .await
            .unwrap();
        (status, serde_json::from_slice(&body).unwrap())
    }
    async fn memory(&self, token: &str, body: Json) -> MemoryReply {
        match self.store.authenticate(token).await {
            Ok(c) => self.store.handle(&c, body.to_string().as_bytes()).await,
            Err(r) => r,
        }
    }
    async fn admin(&self, body: Json) -> MemoryReply {
        let mut body = body;
        body["client_event_id"] = json!(ids::mint(ids::Clock::default().now_ms()));
        let r = self.memory(ADMIN, body).await;
        assert_eq!(r.status, 200, "{r:?}");
        r
    }
    fn node_row(&self, id: &str) -> (String, Option<String>) {
        rusqlite::Connection::open(&self.hub)
            .unwrap()
            .query_row(
                "SELECT token_sha256,revoked_at FROM nodes WHERE node_id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
    }
}
fn token(body: &Json) -> String {
    body["token"].as_str().unwrap().to_string()
}

#[tokio::test]
async fn node_rotation_carries_the_flag_and_an_unflagged_request_issues_nothing() {
    let f = fixture();
    let (status, added) = f
        .post("/nodes", json!({"action":"add","node_id":"verify-1"}))
        .await;
    assert_eq!(status, 200, "{added}");
    let first = token(&added);
    f.admin(json!({"action":"verifier","principal":"verify-1","verifier":true}))
        .await;
    assert_eq!(
        f.memory(&first, json!({"action":"status"})).await.body["verifier"],
        true
    );
    // Rotation is revoke, then add.
    let (status, _) = f
        .post("/nodes", json!({"action":"revoke","node_id":"verify-1"}))
        .await;
    assert_eq!(status, 200);
    let revoked = f.node_row("verify-1");
    let (status, refused) = f
        .post(
            "/nodes",
            json!({"action":"add","node_id":"verify-1","verifier":false}),
        )
        .await;
    assert_eq!(
        (status, refused["code"].as_str()),
        (409, Some("memory_verifier_credential_unflagged")),
        "{refused}"
    );
    assert!(refused.get("token").is_none());
    assert_eq!(f.node_row("verify-1"), revoked);
    let (status, bad) = f
        .post(
            "/nodes",
            json!({"action":"add","node_id":"verify-1","verifier":"no"}),
        )
        .await;
    assert_eq!(status, 400, "{bad}");
    assert_eq!(f.node_row("verify-1"), revoked);
    // The rotated credential carries the flag.
    let (status, rotated) = f
        .post("/nodes", json!({"action":"add","node_id":"verify-1"}))
        .await;
    assert_eq!(status, 200, "{rotated}");
    let second = token(&rotated);
    assert_ne!(first, second);
    assert_eq!(f.node_row("verify-1").1, None);
    let s = f.memory(&second, json!({"action":"status"})).await;
    assert_eq!(
        (s.status, &s.body["verifier"]),
        (200, &json!(true)),
        "{s:?}"
    );
    // An explicit request for the flag matches and is issued too.
    f.post("/nodes", json!({"action":"revoke","node_id":"verify-1"}))
        .await;
    let (status, third) = f
        .post(
            "/nodes",
            json!({"action":"add","node_id":"verify-1","verifier":true}),
        )
        .await;
    assert_eq!(status, 200, "{third}");
    assert_eq!(
        f.memory(&token(&third), json!({"action":"status"}))
            .await
            .body["verifier"],
        true
    );
}

#[tokio::test]
async fn message_principal_readds_are_flagged_and_unflagged_principals_get_no_flag() {
    let f = fixture();
    let (status, added) = f
        .post("/message-principals", json!({"name":"verify-agent"}))
        .await;
    assert_eq!(status, 201, "{added}");
    let id = "agent:verify-agent";
    f.admin(json!({"action":"grant","principal":id,"scope":"fleet","rights":["read"]}))
        .await;
    f.admin(json!({"action":"verifier","principal":id,"verifier":true}))
        .await;
    let mut principal = added["principal"].clone();
    for _ in 0..2 {
        let enrollment = principal["enrollment_id"].clone();
        let (status, _) = f
            .post(
                "/message-principals",
                json!({"action":"revoke","name":"verify-agent","expected_enrollment_id":enrollment}),
            )
            .await;
        assert_eq!(status, 200);
        let (status, refused) = f
            .post(
                "/message-principals",
                json!({"name":"verify-agent","expected_enrollment_id":enrollment,"verifier":false}),
            )
            .await;
        assert_eq!(
            (status, refused["code"].as_str()),
            (409, Some("memory_verifier_credential_unflagged")),
            "{refused}"
        );
        let (status, readded) = f
            .post(
                "/message-principals",
                json!({"name":"verify-agent","expected_enrollment_id":enrollment}),
            )
            .await;
        assert_eq!(status, 201, "{readded}");
        principal = readded["principal"].clone();
        let s = f
            .memory(&token(&principal), json!({"action":"status"}))
            .await;
        assert_eq!(
            (s.status, &s.body["verifier"]),
            (200, &json!(true)),
            "{s:?}"
        );
    }
    // An unflagged principal cannot be enrolled with the flag.
    let (status, refused) = f
        .post(
            "/message-principals",
            json!({"name":"plain-agent","verifier":true}),
        )
        .await;
    assert_eq!(
        (status, refused["code"].as_str()),
        (409, Some("memory_verifier_flag_mismatch")),
        "{refused}"
    );
    let (status, missing) = f
        .post(
            "/message-principals",
            json!({"action":"get","name":"plain-agent"}),
        )
        .await;
    assert_eq!(status, 404, "{missing}");
}

/// Runs the Python policy writer's verifier check against `hub` and returns
/// the refusal code, or "accepted".
fn policy_check(hub: &Path, reflect: &str) -> String {
    let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
    let script = "import sqlite3, sys\nfrom brigade import fleet_hub_verifiers as v\nconn = sqlite3.connect(sys.argv[1])\ntry:\n    v.refuse_listed(conn, {'memory': {'reflect_principals': [sys.argv[2]]}})\n    print('accepted')\nexcept v.VerifierListed as exc:\n    print(exc.code)\n";
    let out = std::process::Command::new(python)
        .args(["-c", script])
        .arg(hub)
        .arg(reflect)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

#[tokio::test]
async fn a_failed_startup_republish_blocks_policy_writes() {
    let f = fixture();
    f.admin(json!({"action":"verifier","principal":"agent:held","verifier":true}))
        .await;
    assert_eq!(
        policy_check(&f.hub, "agent:held"),
        "memory_policy_verifier_listed"
    );
    assert_eq!(policy_check(&f.hub, "agent:other"), "accepted");
    let memory = f.dir.join("fleet-hub-memory.db");
    // A crash lost the change's list: the old, valid list is back on disk.
    brigade_hub::memory::verifier::publish(&memory, 0, &[]).unwrap();
    // The next start cannot republish it.
    let sidecar = brigade_hub::memory::verifier::sidecar(&memory);
    let temp = sidecar.with_extension(format!("json.{}.tmp", std::process::id()));
    std::fs::create_dir(&temp).unwrap();
    std::fs::write(temp.join("blocker"), b"x").unwrap();
    let readers = Arc::new(brigade_hub::read::ReaderPool::open(&f.hub, 1, 64).unwrap());
    let restarted = Store::open(
        Options {
            memory: memory.clone(),
            hub: f.hub.clone(),
            admin_digest: registration::digest(ADMIN),
            clock: ids::Clock::default(),
            scanner: Arc::new(scan::Unavailable),
            arbiter: Arc::new(brigade_hub::writer::Arbiter::default()),
            hook: None,
        },
        readers,
    );
    assert!(restarted.is_err());
    // Every policy that lists a reflect principal is held, flagged or not.
    for reflect in ["agent:held", "agent:other"] {
        assert_eq!(
            policy_check(&f.hub, reflect),
            "memory_policy_verifier_list_stale"
        );
    }
}
