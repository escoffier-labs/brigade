//! Attested proof is resolved by full identity, never by a suffix or a claim.
use super::{proposal_tests::*, tests::*, *};
use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::Connection;

fn policy(h: &Hub) {
    let memory = json!({"auto_apply":true,"curation_owner":"hub","evidence_freshness_seconds":86400,
        "tier0_op_classes":["append_evidence"],"reflect_principals":["hub:admin","agent:dot","node-1"]});
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET document=?1",
            [json!({"memory":memory}).to_string()],
        )
        .unwrap();
}
async fn attach(h: &Hub, token: &str, id: &str) -> Reply {
    h.call(token, json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a",
        "evidence_id":id,"artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"fixture evidence"),
        "observed_at":h.now(),"artifact_base64":STANDARD.encode(b"fixture evidence"),"client_event_id":h.id()})).await
}
async fn proposal(h: &Hub, revision: i64, evidence: Json) -> Reply {
    let mut o = op("fleet", "a", revision);
    o["type"] = json!("append_evidence");
    o.as_object_mut().unwrap().remove("text");
    o["evidence_ids"] = evidence;
    h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[o],"client_event_id":h.id()})).await
}
async fn report(h: &Hub, id: &Json) -> Reply {
    h.call(ADMIN_TOKEN, json!({"action":"preflight","proposal_id":id}))
        .await
}
#[tokio::test]
async fn attachments_resolve_but_revocation_and_cross_workspace_aliases_do_not_count() {
    let h = Hub::with(clean(), None);
    policy(&h);
    grant(&h, "fleet", &["read", "reflect"]).await;
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    for (token, id) in [
        (ADMIN_TOKEN, "hub:admin/workspace-a/one"),
        (ADMIN_TOKEN, "hub:admin/workspace-a/two"),
        (AGENT_TOKEN, "agent:dot/workspace-a/three"),
    ] {
        assert_eq!(attach(&h, token, id).await.status, 201);
    }
    let p = proposal(
        &h,
        rev(&a),
        json!([
            "hub:admin/workspace-a/one",
            "hub:admin/workspace-a/two",
            "agent:dot/workspace-a/three",
            "agent:dot/workspace-b/three",
            "invented/workspace-a/missing"
        ]),
    )
    .await;
    assert_eq!(p.status, 201, "{p:?}");
    let r = report(&h, &p.body["proposal_id"]).await;
    assert_eq!(r.body["proof_evidence_count"], 3, "{r:?}");
    assert_eq!(r.body["proof_source_group_count"], 2);
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_message_principals SET revoked_at='revoked'",
            [],
        )
        .unwrap();
    let r = report(&h, &p.body["proposal_id"]).await;
    assert_eq!(r.body["proof_evidence_count"], 2);
    assert_eq!(r.body["proof_source_group_count"], 1);
}

const REFS: [&str; 3] = [
    "hub:admin/workspace-a/one",
    "hub:admin/workspace-a/two",
    "agent:dot/workspace-a/three",
];
async fn ready() -> (Hub, Json) {
    let h = Hub::with(clean(), None);
    policy(&h);
    grant(&h, "fleet", &["read", "reflect"]).await;
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    // Explicit human review establishes the trusted starting version.
    let initial = card(&h, "fleet", "a").await;
    let p = submit(
        &h,
        ADMIN_TOKEN,
        vec![op("fleet", "a", initial["revision"].as_i64().unwrap())],
    )
    .await;
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &p.body["proposal_id"]).await.body["state"],
        "applied"
    );
    // Cooldown is real head-changing history, so advance beyond it.
    h.set_clock(h.now() + 86_401_000);
    for (i, id) in REFS.iter().enumerate() {
        assert_eq!(
            attach(&h, if i == 2 { AGENT_TOKEN } else { ADMIN_TOKEN }, id)
                .await
                .status,
            201
        );
    }
    let a = card(&h, "fleet", "a").await;
    let mut o = op("fleet", "a", a["revision"].as_i64().unwrap());
    // The reviewed initial edit changed the block, so pin its actual bytes.
    o["expected_block_hash"] = proposal_blocks::pins(a["text"].as_str().unwrap())
        .into_iter()
        .find(|p| p["block_id"] == "c-abcdef")
        .unwrap()["hash"]
        .clone();
    o["type"] = json!("append_evidence");
    o.as_object_mut().unwrap().remove("text");
    o["evidence_ids"] = json!(REFS);
    let p=h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[o],"client_event_id":h.id()})).await;
    assert_eq!(p.status, 201);
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "INSERT INTO memory_vectors VALUES('fixture-hash','fixture-embedder',1,x'01020304')",
            [],
        )
        .unwrap();
    (h, p.body["proposal_id"].clone())
}
async fn auto_request(h: &Hub, id: &Json) -> Json {
    json!({"action":"auto_apply","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})
}
// This seam is compiled only in the unit-test crate. Production always returns
// retrieval_eval_unbound. There is no config, environment or wire bypass.
async fn apply_with_eval(h: &Hub, request: Json, bound: bool) -> Reply {
    let store = h.store.as_ref().unwrap();
    let who = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let mut p = store
        .prepare(&who, "auto_apply", request.as_object().unwrap(), &request)
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    db.execute_batch("BEGIN IMMEDIATE").unwrap();
    let hub = Connection::open(&h.hub).unwrap();
    hub.execute_batch("BEGIN IMMEDIATE").unwrap();
    let mut authority =
        ops::Authority::with_credentials(&crate::writer::worker::Tx(&hub), &digest(ADMIN_TOKEN))
            .unwrap();
    authority.retrieval_eval_bound = bound;
    p.action = ops::Action::Proposal(
        proposals::prepare(
            &crate::writer::worker::Tx(&db),
            &who,
            request,
            clean().as_ref(),
        )
        .unwrap(),
    );
    let r = ops::apply(
        &crate::writer::worker::Tx(&db),
        &p,
        &authority,
        clean().as_ref(),
        None,
    )
    .unwrap();
    db.execute_batch(if r.status < 400 { "COMMIT" } else { "ROLLBACK" })
        .unwrap();
    hub.execute_batch("ROLLBACK").unwrap();
    r
}
#[tokio::test]
async fn retrieval_is_last_hold_and_bound_test_path_is_exactly_once_audited_and_reversible() {
    let (h, id) = ready().await;
    let before = card(&h, "fleet", "a").await;
    let r = report(&h, &id).await;
    let failures: Vec<_> = r.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["state"] != "satisfied")
        .map(|c| c["code"].clone())
        .collect();
    assert_eq!(failures, vec![json!("retrieval_eval_unbound")]);
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(r.body["state"], "pending");
    assert_eq!(before, card(&h, "fleet", "a").await);
    let request = auto_request(&h, &id).await;
    let applied = apply_with_eval(&h, request.clone(), true).await;
    assert_eq!(applied.body["state"], "applied");
    let after = card(&h, "fleet", "a").await;
    assert_eq!(after["trust"], before["trust"]);
    assert_eq!(apply_with_eval(&h, request, true).await, applied);
    assert_eq!(
        apply_with_eval(&h, auto_request(&h, &id).await, true)
            .await
            .status,
        409
    );
    let db = Connection::open(h.memory()).unwrap();
    let payload:String=db.query_row("SELECT payload FROM memory_events WHERE kind='proposal.apply' AND payload LIKE '%automatic%'",[],|r|r.get(0)).unwrap();
    let audit: Json = serde_json::from_str(&payload).unwrap();
    assert_eq!(
        audit["gates"]["attesting_credentials"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(audit["gates"]["policy_digest"], "policy-digest-1");
    assert_eq!(audit["gates"]["checks"].as_array().unwrap().len(), 16);
    assert!(!payload.contains(OLD));
    let inverse=h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(inverse.body["state"], "pending");
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &inverse.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
}
#[tokio::test]
async fn kill_switch_is_admin_only_and_blocks_the_next_apply() {
    let (h, id) = ready().await;
    let switch = json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()});
    assert_eq!(h.call(AGENT_TOKEN, switch.clone()).await.status, 403);
    assert_eq!(h.call(ADMIN_TOKEN, switch).await.status, 200);
    let before = card(&h, "fleet", "a").await;
    let r = apply_with_eval(&h, auto_request(&h, &id).await, true).await;
    assert_eq!(r.body["state"], "pending");
    assert_eq!(before, card(&h, "fleet", "a").await);
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "auto_apply_killed")
    );
}

fn mutate_body(h: &Hub, id: &Json, change: impl FnOnce(&mut Json)) {
    let db = Connection::open(h.memory()).unwrap();
    let text: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let mut body: Json = serde_json::from_str(&text).unwrap();
    change(&mut body);
    db.execute(
        "UPDATE memory_proposals SET body=?1 WHERE proposal_id=?2",
        [body.to_string(), id.as_str().unwrap().into()],
    )
    .unwrap();
}
fn change_policy(h: &Hub, key: &str, value: Json) {
    let db = Connection::open(&h.hub).unwrap();
    let text: String = db
        .query_row("SELECT document FROM fleet_policy_revisions", [], |r| {
            r.get(0)
        })
        .unwrap();
    let mut policy: Json = serde_json::from_str(&text).unwrap();
    policy["memory"][key] = value;
    db.execute(
        "UPDATE fleet_policy_revisions SET document=?1",
        [policy.to_string()],
    )
    .unwrap();
}
fn bytes_snapshot(h: &Hub) -> Vec<String> {
    let db = Connection::open(h.memory()).unwrap();
    [
        "memory_cards",
        "memory_card_revisions",
        "memory_vectors",
        "memory_lineage",
        "memory_embed_jobs",
    ]
    .iter()
    .map(|table| {
        let mut st = db.prepare(&format!("SELECT * FROM {table}")).unwrap();
        let n = st.column_count();
        let mut rows: Vec<String> = st
            .query_map([], |r| {
                Ok((0..n)
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
#[tokio::test]
async fn each_auto_apply_gate_independently_holds_without_card_or_vector_changes() {
    for (mutation, expected) in [
        ("policy_off", "auto_apply_disabled"),
        ("policy_invalid", "policy_invalid"),
        ("class", "class_disabled"),
        ("principal", "reflect_principal_disabled"),
        ("owner", "owner_not_hub"),
        ("handoff", "owner_not_hub"),
        ("evidence", "evidence_floor_unmet"),
        ("sources", "source_group_floor_unmet"),
        ("expired", "evidence_floor_unmet"),
        ("revoked", "source_group_floor_unmet"),
        ("proposer", "proposer_credential_invalid"),
        ("unreviewed", "card_trust_denied"),
        ("private", "scan_not_clean"),
        ("flagged", "scan_not_clean"),
        ("base", "memory_base_mismatch"),
        ("pin", "pinned_block_changed"),
        ("policy_digest", "policy_changed"),
        ("epoch", "memory_epoch_changed"),
        ("cooldown", "cooldown_active"),
        ("tier1", "tier_requires_review"),
        ("tier2", "tier_requires_review"),
        ("blast", "blast_cap_exceeded"),
    ] {
        let (h, mut id) = ready().await;
        let db = Connection::open(h.memory()).unwrap();
        match mutation {
            "policy_off" => change_policy(&h, "auto_apply", json!(false)),
            "policy_invalid" => change_policy(&h, "auto_apply", json!(1)),
            "class" => change_policy(&h, "tier0_op_classes", json!([])),
            "principal" => change_policy(&h, "reflect_principals", json!([])),
            "owner" => change_policy(&h, "curation_owner", json!("external")),
            "handoff" => change_policy(&h, "curation_owner", json!("handoff-only")),
            "evidence" => mutate_body(&h, &id, |b| {
                b["cards"][0]["after_text"] = json!("Claim. ^c-abcdef");
            }),
            "sources" => change_policy(&h, "reflect_principals", json!(["hub:admin"])),
            "expired" => h.set_clock(h.now() + 86_401_000),
            "revoked" => {
                Connection::open(&h.hub)
                    .unwrap()
                    .execute(
                        "UPDATE fleet_message_principals SET revoked_at='revoked'",
                        [],
                    )
                    .unwrap();
            }
            "proposer" => mutate_body(&h, &id, |b| b["proposer_credential"] = json!("not-valid")),
            "unreviewed" => {
                db.execute("UPDATE memory_cards SET trust='agent-unreviewed'", [])
                    .unwrap();
                mutate_body(&h, &id, |b| {
                    b["cards"][0]["before_trust"] = json!("agent-unreviewed")
                });
            }
            "private" | "flagged" => {
                db.execute("UPDATE memory_card_revisions SET scan_state=?1", [mutation])
                    .unwrap();
            }
            "base" => {
                db.execute("UPDATE memory_cards SET revision=revision+1", [])
                    .unwrap();
            }
            "pin" => mutate_body(&h, &id, |b| {
                b["cards"][0]["pins"][0]["bytes"] = json!("different")
            }),
            "policy_digest" => {
                Connection::open(&h.hub)
                    .unwrap()
                    .execute("UPDATE fleet_policy_revisions SET digest='changed'", [])
                    .unwrap();
            }
            "epoch" => mutate_body(&h, &id, |b| {
                b["base_epoch"] = json!(ids::mint(h.now() - 1000))
            }),
            "cooldown" => {
                // Events are immutable. Insert genuine history using the event writer.
                let who = h
                    .store
                    .as_ref()
                    .unwrap()
                    .authenticate(ADMIN_TOKEN)
                    .await
                    .unwrap();
                let req = auto_request(&h, &id).await;
                let p = h
                    .store
                    .as_ref()
                    .unwrap()
                    .prepare(&who, "auto_apply", req.as_object().unwrap(), &req)
                    .unwrap();
                ops::append(
                    &crate::writer::worker::Tx(&db),
                    &p,
                    ops::NewEvent {
                        kind: "proposal.apply",
                        scope: Some("fleet"),
                        card_id: Some("a"),
                        changes_head: true,
                        base_revision: None,
                        content_hash: None,
                        trust: None,
                        origin: None,
                        payload: json!({}),
                        policy_digest: None,
                    },
                )
                .unwrap();
            }
            "tier1" | "tier2" => {
                let head = card(&h, "fleet", "a").await;
                let mut edit = op("fleet", "a", head["revision"].as_i64().unwrap());
                edit["expected_block_hash"] = proposal_blocks::pins(head["text"].as_str().unwrap())
                    .into_iter()
                    .find(|pin| pin["block_id"] == "c-abcdef")
                    .unwrap()["hash"]
                    .clone();
                edit["text"] = json!(if mutation == "tier1" {
                    "Use  the scoped gate."
                } else {
                    "Change the claim."
                });
                let opened=h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[edit],"client_event_id":h.id()})).await;
                assert_eq!(opened.status, 201);
                assert_eq!(opened.body["tier"], if mutation == "tier1" { 1 } else { 2 });
                id = opened.body["proposal_id"].clone();
            }
            "blast" => {
                db.execute_batch("INSERT INTO memory_cards SELECT scope,'b',revision,content_hash,state,trust,writer,writer_class,title,summary,tags,created_at,updated_at FROM memory_cards WHERE card_id='a'; INSERT INTO memory_card_revisions SELECT scope,'b',revision,content_hash,trust,state,writer,seq,proposal_id,scan_state,created_at FROM memory_card_revisions WHERE card_id='a';").unwrap();
                mutate_body(&h, &id, |b| {
                    let mut other = b["cards"][0].clone();
                    other["card_id"] = json!("b");
                    b["cards"].as_array_mut().unwrap().push(other);
                    let mut op = b["ops"][0].clone();
                    op["card_id"] = json!("b");
                    b["ops"].as_array_mut().unwrap().push(op);
                });
            }
            _ => unreachable!(),
        }
        let before = bytes_snapshot(&h);
        let r = apply_with_eval(&h, auto_request(&h, &id).await, true).await;
        assert_eq!(r.body["state"], "pending", "{mutation}");
        if mutation == "tier1" {
            assert_eq!(r.body["tier"], 1);
        }
        let codes: Vec<_> = r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["code"].as_str().unwrap())
            .collect();
        assert!(codes.contains(&expected), "{mutation}: {codes:?}");
        assert_eq!(bytes_snapshot(&h), before, "{mutation}");
    }
}

#[tokio::test]
async fn imported_trust_is_preserved_and_rollback_pins_the_actual_applied_trust() {
    let (h, id) = ready().await;
    Connection::open(h.memory())
        .unwrap()
        .execute("UPDATE memory_cards SET trust='imported'", [])
        .unwrap();
    mutate_body(&h, &id, |b| {
        b["cards"][0]["before_trust"] = json!("imported")
    });
    let r = apply_with_eval(&h, auto_request(&h, &id).await, true).await;
    assert_eq!(r.body["state"], "applied");
    assert_eq!(card(&h, "fleet", "a").await["trust"], "imported");
    let inverse=h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &inverse.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
    assert_eq!(card(&h, "fleet", "a").await["trust"], "imported");
}
#[tokio::test]
async fn late_contrary_evidence_is_rechecked_inside_apply_and_decide_recounts_revocations() {
    let (h, id) = ready().await;
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 3);
    let request = json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a",
        "evidence_id":"agent:dot/workspace-a/negative","artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"negative result"),
        "observed_at":h.now(),"artifact_base64":STANDARD.encode(b"negative result"),"stance":"negative","client_event_id":h.id()});
    assert_eq!(h.call(AGENT_TOKEN, request).await.status, 201);
    let before = bytes_snapshot(&h);
    let r = apply_with_eval(&h, auto_request(&h, &id).await, true).await;
    assert!(
        r.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "contrary_evidence")
    );
    assert_eq!(bytes_snapshot(&h), before);
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_message_principals SET revoked_at='revoked'",
            [],
        )
        .unwrap();
    let r = decide(&h, ADMIN_TOKEN, &id).await;
    assert_eq!(r.body["preflight"]["proof_evidence_count"], 2);
    assert_eq!(r.body["preflight"]["proof_source_group_count"], 1);
}
#[tokio::test]
async fn attachment_validates_raw_bytes_namespace_bounds_and_scope_authority() {
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let base = json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a",
        "evidence_id":"agent:dot/workspace-a/one","artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"raw\r\n"),
        "observed_at":h.now(),"artifact_base64":STANDARD.encode(b"raw\r\n"),"client_event_id":h.id()});
    for rights in [&["read"][..], &["reflect"][..]] {
        grant(&h, "fleet", rights).await;
        assert_eq!(h.call(AGENT_TOKEN, base.clone()).await.status, 403);
    }
    grant(&h, "fleet", &["read", "reflect"]).await;
    for (key, value) in [
        ("evidence_id", json!("node-1/workspace-a/one")),
        ("workspace_id", json!("workspace-b")),
        ("artifact_sha256", json!(ids::sha256_hex(b"raw\n"))),
        ("observed_at", json!(h.now() + 1)),
        ("observed_at", json!(true)),
        ("stance", json!("unknown")),
        ("artifact_base64", json!("invalid")),
    ] {
        let mut request = base.clone();
        request[key] = value;
        let before = h.events();
        assert_eq!(h.call(AGENT_TOKEN, request).await.status, 422, "{key}");
        assert_eq!(h.events(), before);
    }
    let mut forged = base.clone();
    forged["principal"] = json!("hub:admin");
    assert_eq!(h.call(AGENT_TOKEN, forged).await.status, 400);
    let r = h.call(AGENT_TOKEN, base.clone()).await;
    assert_eq!(r.status, 201);
    assert_eq!(h.call(AGENT_TOKEN, base.clone()).await, r);
    assert!(!r.body.to_string().contains("raw"));
    let db = Connection::open(h.memory()).unwrap();
    let bytes: Vec<u8> = db
        .query_row("SELECT artifact_bytes FROM memory_evidence", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(bytes, b"raw\r\n");
    assert!(
        db.execute("UPDATE memory_evidence SET observed_ms=0", [])
            .is_err()
    );
    assert!(db.execute("DELETE FROM memory_evidence", []).is_err());
    let mut duplicate = base.clone();
    duplicate["client_event_id"] = json!(h.id());
    assert_eq!(h.call(AGENT_TOKEN, duplicate).await.status, 409);
    for size in [65536, 65537] {
        let bytes = vec![b'a'; size];
        let mut request = base.clone();
        request["evidence_id"] = json!(format!("agent:dot/workspace-a/size{size}"));
        request["client_event_id"] = json!(h.id());
        request["artifact_sha256"] = json!(ids::sha256_hex(&bytes));
        request["artifact_base64"] = json!(STANDARD.encode(&bytes));
        assert_eq!(
            h.call(AGENT_TOKEN, request).await.status,
            if size == 65536 { 201 } else { 422 }
        );
    }
}

#[tokio::test]
async fn revocation_after_preparation_is_seen_by_writer_and_readers_cannot_see_proof_hashes() {
    let (h, id) = ready().await;
    let store = h.store.as_ref().unwrap();
    let request = auto_request(&h, &id).await;
    let who = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let mut prepared = store
        .prepare(&who, "auto_apply", request.as_object().unwrap(), &request)
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    prepared.action = ops::Action::Proposal(
        proposals::prepare(
            &crate::writer::worker::Tx(&db),
            &who,
            request,
            clean().as_ref(),
        )
        .unwrap(),
    );
    assert_eq!(report(&h, &id).await.body["proof_source_group_count"], 2);
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_message_principals SET revoked_at='revoked'",
            [],
        )
        .unwrap();
    let before = bytes_snapshot(&h);
    let r = store.write(prepared).await;
    assert_eq!(r.body["state"], "pending");
    assert_eq!(r.body["preflight"]["proof_evidence_count"], 2);
    assert_eq!(r.body["preflight"]["proof_source_group_count"], 1);
    assert_eq!(bytes_snapshot(&h), before);
    let read = h
        .call(NODE_TOKEN, json!({"action":"preflight","proposal_id":id}))
        .await;
    assert_eq!(read.status, 200);
    assert_eq!(read.body["evidence_sha256"], Json::Null);
    assert_eq!(read.body["attesting_credentials"], Json::Null);
    let mut visible = read.body.clone();
    visible["evaluation_sha256"] = Json::Null;
    assert_eq!(
        read.body["evaluation_sha256"],
        ids::sha256_hex(ids::canonical(&visible).as_bytes())
    );
}
#[tokio::test]
async fn cross_workspace_records_and_rotated_credentials_never_supply_aliased_proof() {
    let (h, id) = ready().await;
    let r=h.call(AGENT_TOKEN,json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-b",
        "evidence_id":"agent:dot/workspace-b/three","artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"other workspace"),
        "observed_at":h.now(),"artifact_base64":STANDARD.encode(b"other workspace"),"client_event_id":h.id()})).await;
    assert_eq!(r.status, 201);
    mutate_body(&h, &id, |b| {
        let text = b["cards"][0]["after_text"]
            .as_str()
            .unwrap()
            .replace("agent:dot/workspace-a/three", "agent:dot/workspace-b/three");
        b["cards"][0]["after_text"] = json!(text);
    });
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 2);
    mutate_body(&h, &id, |b| {
        let text = b["cards"][0]["after_text"]
            .as_str()
            .unwrap()
            .replace("agent:dot/workspace-b/three", "agent:dot/workspace-a/three");
        b["cards"][0]["after_text"] = json!(text);
    });
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 3);
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_message_principals SET token_hash=?1",
            [digest("fake-rotated-token")],
        )
        .unwrap();
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 2);
}

#[tokio::test]
async fn attestation_replay_requires_read_even_when_reflect_is_retained() {
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    grant(&h, "fleet", &["read", "reflect"]).await;
    let request = json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a",
        "evidence_id":"agent:dot/workspace-a/replay","artifact_kind":"verification","artifact_sha256":ids::sha256_hex(b"replay evidence"),
        "observed_at":h.now(),"artifact_base64":STANDARD.encode(b"replay evidence"),"client_event_id":h.id()});
    assert_eq!(h.call(AGENT_TOKEN, request.clone()).await.status, 201);
    grant(&h, "fleet", &["reflect"]).await;
    assert_eq!(h.call(AGENT_TOKEN, request).await.status, 403);
}
#[tokio::test]
async fn attestation_validity_matches_authentication_when_a_credential_is_shadowed() {
    let (h, id) = ready().await;
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 3);
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "INSERT INTO nodes VALUES('node-alias',?1,NULL,'created',NULL)",
            [digest(AGENT_TOKEN)],
        )
        .unwrap();
    let current = h
        .store
        .as_ref()
        .unwrap()
        .authenticate(AGENT_TOKEN)
        .await
        .unwrap();
    assert_eq!(current.class(), "node");
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 2);
}
#[tokio::test]
async fn evidence_freshness_uses_apply_time_after_preparation() {
    let (h, id) = ready().await;
    let store = h.store.as_ref().unwrap();
    let request = auto_request(&h, &id).await;
    let who = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let mut prepared = store
        .prepare(&who, "auto_apply", request.as_object().unwrap(), &request)
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    prepared.action = ops::Action::Proposal(
        proposals::prepare(
            &crate::writer::worker::Tx(&db),
            &who,
            request,
            clean().as_ref(),
        )
        .unwrap(),
    );
    h.set_clock(h.now() + 86_401_000);
    let r = store.write(prepared).await;
    assert_eq!(r.body["preflight"]["proof_evidence_count"], 0);
    assert_eq!(r.body["state"], "pending");
}
#[tokio::test]
async fn attested_credential_identity_includes_the_authenticated_principal() {
    let (h, id) = ready().await;
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_message_principals SET principal_id='agent:other'",
            [],
        )
        .unwrap();
    assert_eq!(
        h.store
            .as_ref()
            .unwrap()
            .authenticate(AGENT_TOKEN)
            .await
            .unwrap()
            .principal(),
        "agent:other"
    );
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 2);
}
