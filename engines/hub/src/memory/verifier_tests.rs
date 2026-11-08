//! phase-memory-verify slice 3: verifier principals and the Tier 2 floor (D4).
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const ROTATED_TOKEN: &str = "fake-agent-token-rotated";

fn evidence(card: &str, revision: i64) -> Json {
    let mut o = op("fleet", card, revision);
    o.as_object_mut().unwrap().remove("text");
    o["type"] = json!("append_evidence");
    o["evidence_ids"] = json!([
        "node-a/workspace-a/one",
        "node-b/workspace-b/two",
        "node-a/workspace-a/three"
    ]);
    o
}
fn policy(h: &Hub, memory: Json) {
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET document=?1 WHERE revision=1",
            [json!({ "memory": memory }).to_string()],
        )
        .unwrap();
}
/// A tier 0 policy for `append_evidence` that does not list `agent:dot`.
fn tier0(h: &Hub) {
    policy(
        h,
        json!({"auto_apply":true,"curation_owner":"hub","evidence_freshness_seconds":3600,
               "tier0_op_classes":["append_evidence"],"reflect_principals":["agent:other"]}),
    );
}
async fn flag(h: &Hub, principal: &str, verifier: bool) -> Reply {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":principal,"verifier":verifier,"client_event_id":h.id()}),
    )
    .await
}
async fn status(h: &Hub, token: &str) -> Reply {
    h.call(token, json!({"action":"status"})).await
}
async fn preflight(h: &Hub, id: &Json) -> Reply {
    h.call(ADMIN_TOKEN, json!({"action":"preflight","proposal_id":id}))
        .await
}
fn gate(report: &Reply, name: &str) -> String {
    report.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["gate"] == name)
        .unwrap()["code"]
        .as_str()
        .unwrap()
        .into()
}
fn stored(h: &Hub, id: &Json) -> Json {
    let body: String = Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    serde_json::from_str(&body).unwrap()
}
fn copies(h: &Hub, principal: &str) -> Vec<i64> {
    let db = Connection::open(h.memory()).unwrap();
    let mut statement = db
        .prepare("SELECT verifier FROM memory_verifier_credentials WHERE principal=?1 ORDER BY credential")
        .unwrap();
    statement
        .query_map([principal], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect()
}
fn principal_flag(h: &Hub, principal: &str) -> Option<i64> {
    Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT verifier FROM memory_verifiers WHERE principal=?1",
            [principal],
            |r| r.get(0),
        )
        .ok()
}
/// The hub side of a registration that the memory writer already bound:
/// `agent:dot` now holds `token` and nothing else.
fn issue(h: &Hub, token: &str) {
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "INSERT INTO fleet_message_principals VALUES('agent:dot','dot','Dot',?1,'2026-01-02',NULL,NULL)
             ON CONFLICT(principal_id) DO UPDATE SET token_hash=excluded.token_hash,revoked_at=NULL",
            [digest(token)],
        )
        .unwrap();
}
fn revoke(h: &Hub) {
    Connection::open(&h.hub)
        .unwrap()
        .execute_batch("UPDATE fleet_message_principals SET revoked_at='2026-01-03' WHERE principal_id='agent:dot'")
        .unwrap();
}
async fn bind(h: &Hub, token: &str, requested: Option<bool>) -> Reply {
    h.store
        .as_ref()
        .unwrap()
        .bind_credential("agent:dot", "principal", &digest(token), requested)
        .await
}
fn snapshot(h: &Hub) -> Vec<String> {
    let db = Connection::open(h.memory()).unwrap();
    let tables: Vec<String> = db
        .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    tables
        .iter()
        .map(|table| {
            let mut stmt = db.prepare(&format!("SELECT * FROM {table}")).unwrap();
            let columns = stmt.column_count();
            let mut rows: Vec<String> = stmt
                .query_map([], |r| {
                    Ok((0..columns)
                        .map(|i| format!("{:?}", r.get_ref(i).unwrap()))
                        .collect::<Vec<_>>()
                        .join("|"))
                })
                .unwrap()
                .map(Result::unwrap)
                .collect();
            rows.sort();
            format!("{table}:{rows:?}")
        })
        .collect()
}
/// `agent:dot` may propose on `fleet` and two cards exist there.
async fn ready(h: &Hub) -> (i64, i64) {
    tier0(h);
    grant(h, "fleet", &["read", "reflect"]).await;
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    (rev(&a), rev(&b))
}

#[tokio::test]
async fn verifier_append_evidence_stays_tier_two_under_a_tier0_policy() {
    let h = Hub::with(clean(), None);
    let (a, b) = ready(&h).await;
    // Control: the same op from the unflagged principal is a tier 0 candidate.
    let plain = submit(&h, AGENT_TOKEN, vec![evidence("a", a)]).await;
    assert_eq!(plain.status, 201, "{plain:?}");
    assert_eq!(plain.body["tier"], 0);
    assert!(
        stored(&h, &plain.body["proposal_id"])
            .get("verifier")
            .is_none()
    );
    let r = preflight(&h, &plain.body["proposal_id"]).await;
    assert_eq!(gate(&r, "verifier_floor"), "verifier_floor_satisfied");
    assert_eq!(gate(&r, "candidate"), "candidate_satisfied");

    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let opened = submit(&h, AGENT_TOKEN, vec![evidence("b", b)]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["tier"], 2);
    let id = &opened.body["proposal_id"];
    assert_eq!(stored(&h, id)["verifier"], true);
    assert_eq!(stored(&h, id)["opened_tier"], 2);
    let r = preflight(&h, id).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(gate(&r, "verifier_floor"), "verifier_floor");
    assert_eq!(gate(&r, "candidate"), "tier_requires_review");
}

#[tokio::test]
async fn clearing_the_flag_after_submit_keeps_the_open_proposal_tier_two() {
    let h = Hub::with(clean(), None);
    let (a, _) = ready(&h).await;
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let opened = submit(&h, AGENT_TOKEN, vec![evidence("a", a)]).await;
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    let id = &opened.body["proposal_id"];
    assert_eq!(flag(&h, "agent:dot", false).await.status, 200);
    assert_eq!(status(&h, AGENT_TOKEN).await.body["verifier"], false);
    let r = preflight(&h, id).await;
    assert_eq!(gate(&r, "verifier_floor"), "verifier_floor");
    assert_eq!(gate(&r, "candidate"), "tier_requires_review");
    let decided = decide(&h, ADMIN_TOKEN, id).await;
    assert_eq!(decided.status, 200, "{decided:?}");
    assert_eq!(decided.body["tier"], 2);
    assert_eq!(stored(&h, id)["verifier"], true);
    assert_eq!(stored(&h, id)["tier"], 2);
}

#[tokio::test]
async fn auto_apply_on_a_verifier_proposal_is_refused_and_changes_nothing() {
    let h = Hub::with(clean(), None);
    let (a, _) = ready(&h).await;
    let on = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(on.status, 200, "{on:?}");
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let opened = submit(&h, AGENT_TOKEN, vec![evidence("a", a)]).await;
    let id = opened.body["proposal_id"].clone();
    let before = snapshot(&h);
    let request = json!({"action":"auto_apply","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()});
    let r = h.call(AGENT_TOKEN, request.clone()).await;
    assert_eq!(
        (r.status, code(&r)),
        (409, "memory_verifier_floor"),
        "{r:?}"
    );
    // A replay of the same request is refused again, by the floor or by the
    // per-proposal attempt limit, and never stored as an answer.
    let again = h.call(AGENT_TOKEN, request).await;
    assert!(
        matches!(
            code(&again),
            "memory_verifier_floor" | "memory_auto_apply_rate_limited"
        ),
        "{again:?}"
    );
    assert_eq!(before, snapshot(&h));
    assert_eq!(card(&h, "fleet", "a").await["revision"], a);
}

#[tokio::test]
async fn policy_refuses_flagging_a_listed_reflect_principal_including_a_replay() {
    let h = Hub::with(clean(), None);
    policy(
        &h,
        json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["agent:dot"]}),
    );
    let events = h.events();
    let request = json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()});
    let r = h.call(ADMIN_TOKEN, request.clone()).await;
    assert_eq!(
        (r.status, code(&r)),
        (409, "memory_policy_verifier_listed"),
        "{r:?}"
    );
    assert_eq!(h.call(ADMIN_TOKEN, request).await, r);
    assert_eq!(principal_flag(&h, "agent:dot"), None);
    assert_eq!(h.events(), events);
    // Clearing a listed principal is allowed: it only removes a flag.
    assert_eq!(flag(&h, "agent:dot", false).await.status, 200);
    // Once the policy drops it, the same principal can be flagged.
    policy(
        &h,
        json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["agent:other"]}),
    );
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(principal_flag(&h, "agent:dot"), Some(1));
}

#[tokio::test]
async fn flag_changes_publish_the_verifier_list_for_the_policy_writer() {
    let h = Hub::with(clean(), None);
    let sidecar = verifier::sidecar(&h.memory());
    let read = || -> Json { serde_json::from_slice(&std::fs::read(&sidecar).unwrap()).unwrap() };
    // Startup publishes the current, empty list.
    assert_eq!(
        read(),
        json!({"schema":"brigade.memory-verifiers.v1","generation":0,"verifiers":[]})
    );
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(flag(&h, "node-1", true).await.status, 200);
    assert_eq!(read()["verifiers"], json!(["agent:dot", "node-1"]));
    assert_eq!((read()["generation"].clone(), announced(&h)), (json!(2), 2));
    assert_eq!(flag(&h, "agent:dot", false).await.status, 200);
    assert_eq!(read()["verifiers"], json!(["node-1"]));
    assert_eq!((read()["generation"].clone(), announced(&h)), (json!(3), 3));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&sidecar).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
    // A stale file is replaced at the next start.
    std::fs::write(&sidecar, b"{}").unwrap();
    let mut h = h;
    h.store.take();
    h.open(clean(), None);
    assert_eq!(read()["verifiers"], json!(["node-1"]));
}

#[tokio::test]
async fn status_reports_verifier_and_reflect_principal_for_the_caller() {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read"]).await;
    policy(
        &h,
        json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["node-1"]}),
    );
    let both = |r: &Reply| {
        (
            r.body["verifier"].clone(),
            r.body["reflect_principal"].clone(),
        )
    };
    let dot = status(&h, AGENT_TOKEN).await;
    assert_eq!(dot.status, 200, "{dot:?}");
    assert_eq!(both(&dot), (json!(false), json!(false)));
    assert_eq!(dot.body["capabilities"], json!(["verifier_floor"]));
    let node = status(&h, NODE_TOKEN).await;
    assert_eq!(both(&node), (json!(false), json!(true)));
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(
        both(&status(&h, AGENT_TOKEN).await),
        (json!(true), json!(false))
    );
    assert_eq!(
        both(&status(&h, NODE_TOKEN).await),
        (json!(false), json!(true))
    );
    assert_eq!(
        both(&status(&h, ADMIN_TOKEN).await),
        (json!(false), json!(false))
    );
}

#[tokio::test]
async fn a_principal_cannot_set_its_own_flag_through_any_memory_action() {
    let h = Hub::with(clean(), None);
    let all: Vec<&str> = auth::RIGHTS.to_vec();
    for scope in ["fleet", "*"] {
        let r = h.call(ADMIN_TOKEN, json!({"action":"grant","principal":"agent:dot","scope":scope,"rights":all,"client_event_id":h.id()})).await;
        assert_eq!(r.status, 200, "{r:?}");
    }
    for (token, principal) in [(AGENT_TOKEN, "agent:dot"), (NODE_TOKEN, "node-1")] {
        for verifier in [true, false] {
            let r = h.call(token, json!({"action":"verifier","principal":principal,"verifier":verifier,"client_event_id":h.id()})).await;
            assert_eq!((r.status, code(&r)), (403, "memory_grant_missing"), "{r:?}");
        }
        let r = h.call(token, json!({"action":"grant","principal":principal,"scope":"fleet","rights":["read"],"client_event_id":h.id()})).await;
        assert_eq!(r.status, 403, "{r:?}");
    }
    // No other action carries the flag: an extra field is refused outright.
    for body in [
        json!({"action":"put","scope":"fleet","card_id":"x","text":"t","verifier":true,"client_event_id":h.id()}),
        json!({"action":"propose","base_epoch":h.epoch().await,"ops":[],"verifier":false,"client_event_id":h.id()}),
        json!({"action":"status","verifier":true}),
    ] {
        let r = h.call(AGENT_TOKEN, body).await;
        assert_eq!(r.status, 400, "{r:?}");
    }
    assert_eq!(principal_flag(&h, "agent:dot"), None);
    // A flagged principal cannot clear its own flag either.
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let r = h.call(AGENT_TOKEN, json!({"action":"verifier","principal":"agent:dot","verifier":false,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 403, "{r:?}");
    assert_eq!(principal_flag(&h, "agent:dot"), Some(1));
    assert_eq!(status(&h, AGENT_TOKEN).await.body["verifier"], true);
}

#[tokio::test]
async fn rotation_carries_the_flag_and_an_unflagged_request_binds_nothing() {
    let h = Hub::with(clean(), None);
    let (a, _) = ready(&h).await;
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(copies(&h, "agent:dot"), vec![1]);
    let refused = bind(&h, ROTATED_TOKEN, Some(false)).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (409, "memory_verifier_credential_unflagged"),
        "{refused:?}"
    );
    assert_eq!(copies(&h, "agent:dot"), vec![1]);
    // An unflagged principal cannot be handed a flagged credential either.
    let r = h
        .store
        .as_ref()
        .unwrap()
        .bind_credential("node-1", "node", &digest("other"), Some(true))
        .await;
    assert_eq!((r.status, code(&r)), (409, "memory_verifier_flag_mismatch"));
    // The rotation itself: bound first, then issued by the hub.
    let bound = bind(&h, ROTATED_TOKEN, None).await;
    assert_eq!((bound.status, &bound.body["verifier"]), (200, &json!(true)));
    issue(&h, ROTATED_TOKEN);
    assert_eq!(status(&h, AGENT_TOKEN).await.status, 401);
    let s = status(&h, ROTATED_TOKEN).await;
    assert_eq!(
        (s.status, &s.body["verifier"]),
        (200, &json!(true)),
        "{s:?}"
    );
    let opened = submit(&h, ROTATED_TOKEN, vec![evidence("a", a)]).await;
    assert_eq!((opened.status, &opened.body["tier"]), (201, &json!(2)));
}

#[tokio::test]
async fn every_credential_a_verifier_is_issued_is_flagged_and_tier_two() {
    let h = Hub::with(clean(), None);
    let (a, b) = ready(&h).await;
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let c = rev(&h.put(ADMIN_TOKEN, "fleet", "c", None, OLD).await);
    // The hub holds one live credential per principal id, so the second and
    // third credentials are successive enrollments of the same principal.
    for (token, (card, revision)) in [
        "fake-agent-token-2",
        "fake-agent-token-3",
        "fake-agent-token-4",
    ]
    .into_iter()
    .zip([("a", a), ("b", b), ("c", c)])
    {
        revoke(&h);
        assert_eq!(bind(&h, token, None).await.body["verifier"], true);
        issue(&h, token);
        assert_eq!(status(&h, token).await.body["verifier"], true);
        let opened = submit(&h, token, vec![evidence(card, revision)]).await;
        assert_eq!(
            (opened.status, &opened.body["tier"]),
            (201, &json!(2)),
            "{opened:?}"
        );
        assert_eq!(stored(&h, &opened.body["proposal_id"])["verifier"], true);
    }
    assert_eq!(copies(&h, "agent:dot"), vec![1, 1, 1, 1]);
}

#[tokio::test]
async fn revoke_and_readd_and_principal_removal_both_come_back_flagged() {
    let h = Hub::with(clean(), None);
    let (a, _) = ready(&h).await;
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    // Revoke every credential, drop the grants, then re-add one.
    revoke(&h);
    let r = h.call(ADMIN_TOKEN, json!({"action":"grant","principal":"agent:dot","scope":"fleet","rights":[],"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200);
    assert_eq!(principal_flag(&h, "agent:dot"), Some(1));
    assert_eq!(
        bind(&h, "fake-agent-token-readd", None).await.body["verifier"],
        true
    );
    issue(&h, "fake-agent-token-readd");
    assert_eq!(status(&h, "fake-agent-token-readd").await.status, 403);
    grant(&h, "fleet", &["read", "reflect"]).await;
    assert_eq!(
        status(&h, "fake-agent-token-readd").await.body["verifier"],
        true
    );
    // Remove the principal's hub row entirely and re-add the same id.
    Connection::open(&h.hub)
        .unwrap()
        .execute_batch("DELETE FROM fleet_message_principals WHERE principal_id='agent:dot'")
        .unwrap();
    assert_eq!(principal_flag(&h, "agent:dot"), Some(1));
    assert_eq!(
        bind(&h, "fake-agent-token-back", None).await.body["verifier"],
        true
    );
    issue(&h, "fake-agent-token-back");
    assert_eq!(
        status(&h, "fake-agent-token-back").await.body["verifier"],
        true
    );
    let opened = submit(&h, "fake-agent-token-back", vec![evidence("a", a)]).await;
    assert_eq!((opened.status, &opened.body["tier"]), (201, &json!(2)));
}

#[tokio::test]
async fn a_credential_whose_copy_differs_is_refused_on_every_memory_action() {
    let h = Hub::with(clean(), None);
    let (a, _) = ready(&h).await;
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    let opened = submit(&h, AGENT_TOKEN, vec![evidence("a", a)]).await;
    let id = opened.body["proposal_id"].clone();
    Connection::open(h.memory())
        .unwrap()
        .execute_batch("UPDATE memory_verifier_credentials SET verifier=0")
        .unwrap();
    let epoch = h.epoch().await;
    let bodies = [
        json!({"action":"status"}),
        json!({"action":"card","scope":"fleet","card_id":"a"}),
        json!({"action":"log"}),
        json!({"action":"search","query":"focused gate"}),
        json!({"action":"proposal_list"}),
        json!({"action":"proposal_show","proposal_id":id}),
        json!({"action":"read_stats","scope":"fleet"}),
        json!({"action":"preflight","proposal_id":id}),
        json!({"action":"put","scope":"fleet","card_id":"z","text":"new card","client_event_id":h.id()}),
        json!({"action":"handoff","kind":"note","text":"a note","client_event_id":h.id()}),
        json!({"action":"propose","base_epoch":epoch,"ops":[evidence("b", a)],"client_event_id":h.id()}),
        json!({"action":"auto_apply","proposal_id":id,"base_epoch":epoch,"client_event_id":h.id()}),
        json!({"action":"verifier","principal":"agent:dot","verifier":false,"client_event_id":h.id()}),
        json!({"action":"embed_lease","client_event_id":h.id()}),
        json!({"action":"unknown"}),
    ];
    let before = snapshot(&h);
    for body in bodies {
        let r = h.call(AGENT_TOKEN, body.clone()).await;
        assert_eq!(
            (r.status, code(&r)),
            (403, "memory_verifier_flag_mismatch"),
            "{body} {r:?}"
        );
    }
    let push = h
        .store
        .as_ref()
        .unwrap()
        .push(
            &Credential::Principal {
                id: "agent:dot".into(),
                digest: digest(AGENT_TOKEN),
            },
            json!({"query":"focused gate"}).as_object().unwrap(),
        )
        .await;
    assert_eq!(code(&push), "memory_verifier_flag_mismatch");
    assert_eq!(before, snapshot(&h));
    // A credential issued on a path that bound no copy is refused the same
    // way, until the admin sets the flag again and binds every live one.
    issue(&h, ROTATED_TOKEN);
    assert_eq!(
        code(&status(&h, ROTATED_TOKEN).await),
        "memory_verifier_flag_mismatch"
    );
    let set = flag(&h, "agent:dot", true).await;
    assert_eq!(set.body["live_credentials"], 1, "{set:?}");
    assert_eq!(status(&h, ROTATED_TOKEN).await.body["verifier"], true);
    // The writer refuses under its own lock too, not only at admission.
    Connection::open(h.memory())
        .unwrap()
        .execute_batch("UPDATE memory_verifier_credentials SET verifier=0")
        .unwrap();
    let p = Prepared {
        credential: Credential::Principal {
            id: "agent:dot".into(),
            digest: digest(ROTATED_TOKEN),
        },
        action: ops::Action::Handoff {
            kind: "note".into(),
            text: "a note".into(),
            hash: ids::sha256_hex(b"a note"),
            scan: Scan::Unavailable,
        },
        client_event_id: h.id(),
        request_digest: String::new(),
        now_ms: h.now(),
    };
    let r = h.store.as_ref().unwrap().write(p).await;
    assert_eq!(code(&r), "memory_verifier_flag_mismatch", "{r:?}");
}

#[tokio::test]
async fn clearing_the_flag_updates_every_copy_at_once_with_no_mismatch_between() {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read"]).await;
    // Stale copies from earlier credentials are rewritten too.
    for token in ["fake-agent-token-2", "fake-agent-token-3"] {
        assert_eq!(bind(&h, token, None).await.status, 200);
    }
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(copies(&h, "agent:dot"), vec![1, 1, 1]);
    let h = Arc::new(h);
    let mut readers = Vec::new();
    for _ in 0..8 {
        let h = h.clone();
        readers.push(tokio::spawn(async move {
            let mut seen = Vec::new();
            for _ in 0..25 {
                let r = status(&h, AGENT_TOKEN).await;
                assert_eq!(r.status, 200, "{r:?}");
                seen.push(r.body["verifier"].as_bool().unwrap());
            }
            seen
        }));
    }
    let cleared = flag(&h, "agent:dot", false).await;
    assert_eq!(cleared.status, 200, "{cleared:?}");
    for reader in readers {
        let seen = reader.await.unwrap();
        // Each reader sees the flag set, then cleared, never in between.
        assert!(seen.windows(2).all(|w| w[0] || !w[1]), "{seen:?}");
    }
    assert_eq!(copies(&h, "agent:dot"), vec![0, 0, 0]);
    assert_eq!(status(&h, AGENT_TOKEN).await.body["verifier"], false);
}

#[test]
fn version_ten_migrates_to_eleven_with_no_principal_flagged() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = Connection::open(&path).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch(
        "DROP TABLE memory_verifiers; DROP TABLE memory_verifier_credentials; PRAGMA user_version=10;",
    )
    .unwrap();
    let epoch = schema::log_epoch(&db).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let version = |db: &Connection| -> i64 {
        db.query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap()
    };
    assert_eq!(version(&db), 11);
    assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
    let rows: i64 = db
        .query_row(
            "SELECT (SELECT count(*) FROM memory_verifiers)+(SELECT count(*) FROM memory_verifier_credentials)",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(rows, 0);
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    assert_eq!(version(&db), 11);
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

#[tokio::test]
async fn a_publish_failure_refuses_the_flag_change_and_leaves_it_unflagged() {
    let h = Hub::with(clean(), None);
    let sidecar = verifier::sidecar(&h.memory());
    // A non-empty directory where the list goes makes the rename fail.
    std::fs::remove_file(&sidecar).unwrap();
    std::fs::create_dir(&sidecar).unwrap();
    std::fs::write(sidecar.join("blocker"), b"x").unwrap();
    let events = h.events();
    let r = flag(&h, "agent:dot", true).await;
    assert_eq!(
        (r.status, code(&r)),
        (503, "memory_verifier_publish_failed"),
        "{r:?}"
    );
    assert_eq!(principal_flag(&h, "agent:dot"), None);
    assert!(copies(&h, "agent:dot").is_empty());
    assert_eq!(h.events(), events);
    grant(&h, "fleet", &["read"]).await;
    assert_eq!(status(&h, AGENT_TOKEN).await.body["verifier"], false);
    // Once the list can be written, the same change succeeds.
    std::fs::remove_dir_all(&sidecar).unwrap();
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(principal_flag(&h, "agent:dot"), Some(1));
}

#[tokio::test]
async fn a_clear_keeps_the_principal_published_until_it_commits() {
    let seen: Arc<std::sync::Mutex<Vec<Json>>> = Arc::default();
    let path: Arc<std::sync::Mutex<Option<std::path::PathBuf>>> = Arc::default();
    let (record, at) = (seen.clone(), path.clone());
    let hook: writer::Hook = Arc::new(move |step| {
        if step == writer::Step::Applied {
            if let Some(p) = at.lock().unwrap().as_ref() {
                let body = std::fs::read(p).unwrap();
                record
                    .lock()
                    .unwrap()
                    .push(serde_json::from_slice(&body).unwrap());
            }
        }
    });
    let h = Hub::with(clean(), Some(hook));
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    *path.lock().unwrap() = Some(verifier::sidecar(&h.memory()));
    assert_eq!(flag(&h, "agent:dot", false).await.status, 200);
    // Before the clear committed, the list still named the principal.
    let before_commit = seen.lock().unwrap().last().cloned().unwrap();
    assert_eq!(before_commit["verifiers"], json!(["agent:dot"]));
    let after: Json =
        serde_json::from_slice(&std::fs::read(verifier::sidecar(&h.memory())).unwrap()).unwrap();
    assert_eq!(after["verifiers"], json!([]));
}

#[tokio::test]
async fn a_list_that_over_reports_after_a_lost_commit_is_repaired_on_restart() {
    let mut h = Hub::with(clean(), None);
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    h.store.take();
    // The state a failed commit leaves: the list was published before the
    // commit, but the memory file never recorded the change.
    Connection::open(h.memory())
        .unwrap()
        .execute_batch(
            "DELETE FROM memory_verifiers; DELETE FROM memory_verifier_credentials;
             DELETE FROM memory_meta WHERE key='verifier_generation';",
        )
        .unwrap();
    let sidecar = verifier::sidecar(&h.memory());
    let read = || -> Json { serde_json::from_slice(&std::fs::read(&sidecar).unwrap()).unwrap() };
    assert_eq!(read()["verifiers"], json!(["agent:dot"]));
    h.open(clean(), None);
    // The committed set at the memory file's own generation, not relabeled
    // to the one the lost change announced, so policy writes stay held.
    assert_eq!(read()["verifiers"], json!([]));
    assert_eq!((read()["generation"].clone(), announced(&h)), (json!(0), 1));
    assert_eq!(resync(&h).await.status, 200);
    assert_eq!((read()["generation"].clone(), announced(&h)), (json!(2), 2));
}

async fn resync(h: &Hub) -> Reply {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier_resync","client_event_id":h.id()}),
    )
    .await
}
fn set_announced(h: &Hub, generation: i64) {
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "INSERT INTO hub_meta VALUES('memory_verifier_generation',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [generation.to_string()],
        )
        .unwrap();
}
async fn generation_state(h: &Hub) -> Json {
    status(h, ADMIN_TOKEN).await.body["verifier_generation"].clone()
}

#[tokio::test]
async fn a_memory_file_restored_below_the_hub_is_not_relabeled() {
    let mut h = Hub::with(clean(), None);
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(flag(&h, "node-1", true).await.status, 200);
    assert_eq!(
        generation_state(&h).await,
        json!({"memory":2,"hub":2,"current":true})
    );
    h.store.take();
    // Restore the memory file to its state at generation 1.
    Connection::open(h.memory())
        .unwrap()
        .execute_batch(
            "DELETE FROM memory_verifiers WHERE principal='node-1';
             DELETE FROM memory_verifier_credentials WHERE principal='node-1';
             UPDATE memory_meta SET value='1' WHERE key='verifier_generation';",
        )
        .unwrap();
    h.open(clean(), None);
    let list: Json =
        serde_json::from_slice(&std::fs::read(verifier::sidecar(&h.memory())).unwrap()).unwrap();
    assert_eq!(list["generation"], json!(1));
    assert_eq!(list["verifiers"], json!(["agent:dot"]));
    assert_eq!(announced(&h), 2);
    assert_eq!(
        generation_state(&h).await,
        json!({"memory":1,"hub":2,"current":false})
    );
    // The repair: an admin action announces 3 and memory stores it.
    let r = resync(&h).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(r.body["verifiers"], json!(["agent:dot"]));
    assert_eq!((listed_generation(&h), announced(&h)), (json!(3), 3));
    assert_eq!(
        generation_state(&h).await,
        json!({"memory":3,"hub":3,"current":true})
    );
}

#[tokio::test]
async fn a_hub_restored_below_memory_stays_mismatched_until_a_flag_change() {
    let mut h = Hub::with(clean(), None);
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!(flag(&h, "node-1", true).await.status, 200);
    h.store.take();
    // Restore the hub file, and the list beside it, to generation 1.
    set_announced(&h, 1);
    verifier::publish(&h.memory(), 1, &["agent:dot".to_string()]).unwrap();
    h.open(clean(), None);
    // Start republishes at memory's own generation, so the pair disagrees.
    assert_eq!((listed_generation(&h), announced(&h)), (json!(2), 1));
    assert_eq!(
        generation_state(&h).await,
        json!({"memory":2,"hub":1,"current":false})
    );
    // Any admin flag change repairs it: it announces past both.
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!((listed_generation(&h), announced(&h)), (json!(3), 3));
}

fn announced(h: &Hub) -> i64 {
    Connection::open(&h.hub)
        .unwrap()
        .query_row(
            "SELECT CAST(value AS INTEGER) FROM hub_meta WHERE key='memory_verifier_generation'",
            [],
            |r| r.get(0),
        )
        .unwrap_or(0)
}
fn listed_generation(h: &Hub) -> Json {
    let body = std::fs::read(verifier::sidecar(&h.memory())).unwrap();
    serde_json::from_slice::<Json>(&body).unwrap()["generation"].clone()
}

#[tokio::test]
async fn a_valid_list_left_at_an_older_generation_is_republished_on_restart() {
    let mut h = Hub::with(clean(), None);
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    assert_eq!((listed_generation(&h), announced(&h)), (json!(1), 1));
    h.store.take();
    // The rename of the change's list was lost: the old list is still valid.
    verifier::publish(&h.memory(), 0, &[]).unwrap();
    assert_eq!(listed_generation(&h), json!(0));
    h.open(clean(), None);
    let list: Json =
        serde_json::from_slice(&std::fs::read(verifier::sidecar(&h.memory())).unwrap()).unwrap();
    assert_eq!(list["generation"], json!(1));
    assert_eq!(list["verifiers"], json!(["agent:dot"]));
}

#[tokio::test]
async fn a_flag_change_refused_for_a_listed_principal_announces_nothing() {
    let h = Hub::with(clean(), None);
    policy(
        &h,
        json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["agent:dot"]}),
    );
    let r = flag(&h, "agent:dot", true).await;
    assert_eq!(code(&r), "memory_policy_verifier_listed");
    // Refused before anything was announced, so the pair still matches.
    assert_eq!((listed_generation(&h), announced(&h)), (json!(0), 0));
    assert_eq!(principal_flag(&h, "agent:dot"), None);
}

#[tokio::test]
async fn a_failed_startup_republish_refuses_to_start_memory() {
    let mut h = Hub::with(clean(), None);
    assert_eq!(flag(&h, "agent:dot", true).await.status, 200);
    h.store.take();
    verifier::publish(&h.memory(), 0, &[]).unwrap();
    // Block the temporary file the republish writes first.
    let sidecar = verifier::sidecar(&h.memory());
    let temp = sidecar.with_extension(format!("json.{}.tmp", std::process::id()));
    std::fs::create_dir(&temp).unwrap();
    std::fs::write(temp.join("blocker"), b"x").unwrap();
    let readers = Arc::new(ReaderPool::open_unchecked(&h.hub, 1, 64, READ_LIMIT).unwrap());
    let opened = Store::open(
        Options {
            memory: h.memory(),
            hub: h.hub.clone(),
            admin_digest: digest(ADMIN_TOKEN),
            clock: ids::Clock {
                file: Some(h.clock.clone()),
            },
            scanner: clean(),
            arbiter: h.arbiter.clone(),
            hook: None,
        },
        readers,
    );
    assert!(opened.is_err());
    // The stale list stays below the hub's generation, so the policy writer
    // refuses listings until a start republishes it.
    assert_eq!((listed_generation(&h), announced(&h)), (json!(0), 1));
    std::fs::remove_dir_all(&temp).unwrap();
    h.open(clean(), None);
    assert_eq!(listed_generation(&h), json!(1));
}

#[tokio::test]
async fn a_replayed_flag_change_or_resync_announces_nothing() {
    let h = Hub::with(clean(), None);
    for request in [
        json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()}),
        json!({"action":"verifier_resync","client_event_id":h.id()}),
    ] {
        let first = h.call(ADMIN_TOKEN, request.clone()).await;
        assert_eq!(first.status, 200, "{first:?}");
        let state = generation_state(&h).await;
        assert_eq!(state["current"], true, "{state}");
        let events = h.events();
        for _ in 0..2 {
            assert_eq!(h.call(ADMIN_TOKEN, request.clone()).await, first);
        }
        // The same id for a different request is a conflict, announced nowhere.
        let id = request["client_event_id"].clone();
        let other = if request["action"] == "verifier" {
            json!({"action":"verifier_resync","client_event_id":id})
        } else {
            json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":id})
        };
        let conflict = h.call(ADMIN_TOKEN, other).await;
        assert_eq!(
            code(&conflict),
            "memory_client_event_conflict",
            "{conflict:?}"
        );
        assert_eq!(generation_state(&h).await, state);
        assert_eq!(listed_generation(&h), state["memory"]);
        assert_eq!(h.events(), events);
    }
    assert_eq!(
        generation_state(&h).await,
        json!({"memory":2,"hub":2,"current":true})
    );
}
