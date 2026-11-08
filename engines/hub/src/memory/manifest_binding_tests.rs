//! phase-memory-verify slice 4: proposal-bound attestations and the reviewer
//! hash read (D5).
use super::{proposal_tests::*, tests::*, *};
use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::Connection;
use std::sync::atomic::{AtomicUsize, Ordering};

static PROBE: AtomicUsize = AtomicUsize::new(0);
const WORKSPACE: &str = "workspace-a";

fn policy(h: &Hub, digest: &str) {
    let memory = json!({"auto_apply":true,"curation_owner":"hub","evidence_freshness_seconds":86400,
        "tier0_op_classes":["append_evidence"],"reflect_principals":["agent:other"]});
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET document=?1,digest=?2",
            [json!({ "memory": memory }).to_string(), digest.to_string()],
        )
        .unwrap();
}
async fn grant_to(h: &Hub, principal: &str, scope: &str, rights: &[&str]) {
    let r = h.call(ADMIN_TOKEN, json!({"action":"grant","principal":principal,"scope":scope,"rights":rights,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
}
/// `agent:dot` is a verifier with read and reflect on `fleet` and
/// `repo:other`. Cards `fleet/a`, `fleet/b` and `repo:other/a` exist.
async fn ready() -> (Hub, i64) {
    let h = Hub::with(clean(), None);
    policy(&h, "sha256:policy-one");
    grant(&h, "fleet", &["read", "reflect"]).await;
    grant(&h, "repo:other", &["read", "reflect"]).await;
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    h.put(ADMIN_TOKEN, "repo:other", "a", None, OLD).await;
    let flagged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(flagged.status, 200, "{flagged:?}");
    (h, rev(&a))
}
fn manifest(scope: &str, card: &str, revision: i64) -> Vec<u8> {
    json!({"schema":"brigade.memory-verify-manifest.v1",
           "card":{"scope":scope,"card_id":card,"base_revision":revision},
           "blocks":[{"block_id":"c-abcdef","verdict":"contradicted","confidence":"high"}]})
    .to_string()
    .into_bytes()
}
fn attach_request(h: &Hub, name: &str, scope: &str, card: &str, bytes: &[u8], kind: &str) -> Json {
    json!({"action":"evidence.attach","scope":scope,"card_id":card,"workspace_id":WORKSPACE,
        "evidence_id":format!("agent:dot/{WORKSPACE}/{name}"),"artifact_kind":kind,
        "artifact_sha256":ids::sha256_hex(bytes),"observed_at":h.now(),
        "artifact_base64":STANDARD.encode(bytes),"stance":"contrary","client_event_id":h.id()})
}
/// A contrary manifest for `scope/card` attested by the verifier.
async fn attest(h: &Hub, name: &str, scope: &str, card: &str, revision: i64) -> Reply {
    let bytes = manifest(scope, card, revision);
    h.call(
        AGENT_TOKEN,
        attach_request(h, name, scope, card, &bytes, "memory-verify-manifest"),
    )
    .await
}
fn eid(name: &str) -> String {
    format!("agent:dot/{WORKSPACE}/{name}")
}
fn pin(text: &str) -> Json {
    proposal_blocks::pins(text)
        .into_iter()
        .find(|p| p["block_id"] == "c-abcdef")
        .unwrap()["hash"]
        .clone()
}
fn cite(scope: &str, card: &str, revision: i64, refs: &[String]) -> Json {
    let mut o = op(scope, card, revision);
    o.as_object_mut().unwrap().remove("text");
    o["type"] = json!("append_evidence");
    o["evidence_ids"] = json!(refs);
    o
}
async fn propose(h: &Hub, token: &str, ops: Vec<Json>) -> Reply {
    h.call(token, json!({"action":"propose","workspace_id":WORKSPACE,"base_epoch":h.epoch().await,"ops":ops,"client_event_id":h.id()})).await
}
async fn decision(h: &Hub, id: &Json, decision: &str) -> Reply {
    h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":id,"decision":decision,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}
async fn show(h: &Hub, token: &str, id: &Json) -> Reply {
    h.call(token, json!({"action":"proposal_show","proposal_id":id}))
        .await
}
fn state(h: &Hub, name: &str) -> (String, Option<String>) {
    Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT state,proposal_id FROM memory_evidence_bindings WHERE evidence_id=?1",
            [eid(name)],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap()
}
fn proposals(h: &Hub) -> i64 {
    Connection::open(h.memory())
        .unwrap()
        .query_row("SELECT count(*) FROM memory_proposals", [], |r| r.get(0))
        .unwrap()
}
/// Whether contrary evidence holds automatic apply on `fleet/a` now: the
/// preflight of a fresh admin probe proposal for the card.
async fn held(h: &Hub) -> bool {
    let current = card(h, "fleet", "a").await;
    let mut o = cite(
        "fleet",
        "a",
        current["revision"].as_i64().unwrap(),
        &[format!(
            "hub:admin/{WORKSPACE}/probe-{}",
            PROBE.fetch_add(1, Ordering::Relaxed)
        )],
    );
    o["expected_block_hash"] = pin(current["text"].as_str().unwrap());
    let probe = propose(h, ADMIN_TOKEN, vec![o]).await;
    assert_eq!(probe.status, 201, "{probe:?}");
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"preflight","proposal_id":probe.body["proposal_id"]}),
        )
        .await;
    let check = r.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["gate"] == "evidence_authority")
        .unwrap()
        .clone();
    assert_ne!(check["code"], "evidence_authority_unbound", "{r:?}");
    check["code"] == "contrary_evidence"
}
/// Fill the verifier's eight slots on `fleet/a`: the attached subject and
/// seven fillers.
async fn fill(h: &Hub, subject: &str, a: i64) {
    for i in 0..7 {
        let name = format!("{subject}-filler-{i}");
        assert_eq!(attest(h, &name, "fleet", "a", a).await.status, 201);
    }
}
fn state_opt(h: &Hub, name: &str) -> Option<String> {
    Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT state FROM memory_evidence_bindings WHERE evidence_id=?1",
            [eid(name)],
            |r| r.get(0),
        )
        .ok()
}
async fn at_cap(h: &Hub, a: i64) -> bool {
    let name = format!("extra-{}", PROBE.fetch_add(1, Ordering::Relaxed));
    let r = attest(h, &name, "fleet", "a", a).await;
    match r.status {
        201 => false,
        409 => {
            assert_eq!(code(&r), "memory_evidence_principal_cap", "{r:?}");
            true
        }
        _ => panic!("{r:?}"),
    }
}
fn assert_refused(r: &Reply) {
    assert_eq!(
        (r.status, code(r)),
        (422, "memory_manifest_binding_invalid"),
        "{r:?}"
    );
}

#[tokio::test]
async fn a_pending_contrary_manifest_never_holds_automatic_apply() {
    let (h, a) = ready().await;
    let attached = attest(&h, "m1", "fleet", "a", a).await;
    assert_eq!(attached.status, 201, "{attached:?}");
    assert_eq!(attached.body["state"], "pending_review");
    assert_eq!(state(&h, "m1"), ("pending_review".into(), None));
    assert!(!held(&h).await);
    let opened = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(
        state(&h, "m1"),
        (
            "pending_review".into(),
            opened.body["proposal_id"].as_str().map(String::from)
        )
    );
    assert!(!held(&h).await, "a bound pending manifest holds nothing");
    // Control: the same record from a principal with no flag holds at once.
    grant_to(&h, "node-1", "fleet", &["read", "reflect"]).await;
    let bytes = manifest("fleet", "a", a);
    let mut plain = attach_request(&h, "x", "fleet", "a", &bytes, "memory-verify-manifest");
    plain["evidence_id"] = json!(format!("node-1/{WORKSPACE}/plain"));
    assert_eq!(h.call(NODE_TOKEN, plain).await.status, 201);
    assert!(held(&h).await);
}

#[tokio::test]
async fn accept_activates_the_contrary_hold() {
    let (h, a) = ready().await;
    assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
    let opened = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert!(!held(&h).await);
    let accepted = decision(&h, &opened.body["proposal_id"], "accept").await;
    assert_eq!(accepted.body["state"], "applied", "{accepted:?}");
    assert_eq!(state(&h, "m1").0, "active");
    assert!(held(&h).await);
}

#[derive(Clone, Copy, Debug)]
enum End {
    Reject,
    Stale,
    Compaction,
    Unbound,
}

#[tokio::test]
async fn reject_stale_compaction_and_the_unbound_sweep_each_leave_an_inert_record() {
    for end in [End::Reject, End::Stale, End::Compaction, End::Unbound] {
        let (h, a) = ready().await;
        assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
        let mut proposal = None;
        if !matches!(end, End::Unbound) {
            let opened = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
            assert_eq!(opened.status, 201, "{end:?} {opened:?}");
            proposal = Some(opened.body["proposal_id"].clone());
        } else {
            // A record older than 24 hours cannot bind even before the sweep.
            h.set_clock(h.now() + 24 * 3600 * 1000 + 1);
        }
        fill(&h, "m1", a).await;
        assert!(at_cap(&h, a).await, "{end:?}: eight records fill the slots");
        match end {
            End::Reject => {
                let r = decision(&h, proposal.as_ref().unwrap(), "reject").await;
                assert_eq!(r.body["state"], "rejected", "{r:?}");
            }
            End::Stale => {
                // The card moves on under the pending proposal.
                let current = card(&h, "fleet", "a").await;
                let moved = h
                    .put(
                        ADMIN_TOKEN,
                        "fleet",
                        "a",
                        current["revision"].as_i64(),
                        "Use the newer gate. ^c-abcdef",
                    )
                    .await;
                assert!(rev(&moved) > a, "{moved:?}");
                let r = decision(&h, proposal.as_ref().unwrap(), "accept").await;
                assert_eq!(r.body["state"], "stale", "{r:?}");
                assert_ne!(r.body["reason"], "policy_changed");
            }
            End::Compaction => {
                // A policy-stale proposal can still be decided, so its record
                // stays pending until compaction settles it.
                policy(&h, "sha256:policy-two");
                let r = decision(&h, proposal.as_ref().unwrap(), "accept").await;
                assert_eq!(
                    (r.body["state"].as_str(), r.body["reason"].as_str()),
                    (Some("stale"), Some("policy_changed")),
                    "{r:?}"
                );
                assert_eq!(state(&h, "m1").0, "pending_review");
                let db = Connection::open(h.memory()).unwrap();
                let compacted = hardening::compact(
                    &db,
                    h.now() + 8 * 86_400_000,
                    &hardening::Limits::default(),
                    256,
                )
                .unwrap();
                assert!(compacted >= 1);
            }
            End::Unbound => {
                let r = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
                assert_refused(&r);
                let db = Connection::open(h.memory()).unwrap();
                assert!(writer::sweep(&db, h.now()).unwrap() >= 1);
            }
        }
        assert_eq!(state(&h, "m1").0, "inert", "{end:?}");
        assert!(!held(&h).await, "{end:?}: an inert record holds nothing");
        assert!(
            !at_cap(&h, a).await,
            "{end:?}: an inert record frees its slot"
        );
        // An inert record never comes back.
        assert!(
            Connection::open(h.memory())
                .unwrap()
                .execute(
                    "UPDATE memory_evidence_bindings SET state='active' WHERE evidence_id=?1",
                    [eid("m1")],
                )
                .is_err()
        );
    }
}

#[tokio::test]
async fn a_record_binds_only_to_its_attesters_first_proposal() {
    let (h, a) = ready().await;
    assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
    let (count, events) = (proposals(&h), h.events());
    // Another principal's proposal, even an admin's.
    let other = propose(&h, ADMIN_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
    assert_refused(&other);
    assert_eq!((proposals(&h), h.events()), (count, events));
    assert_eq!(state(&h, "m1"), ("pending_review".into(), None));
    let first = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("m1")])]).await;
    assert_eq!(first.status, 201, "{first:?}");
    let (count, events) = (proposals(&h), h.events());
    let second = propose(
        &h,
        AGENT_TOKEN,
        vec![cite(
            "fleet",
            "a",
            a,
            &[eid("m1"), format!("node-1/{WORKSPACE}/other")],
        )],
    )
    .await;
    assert_refused(&second);
    assert_eq!((proposals(&h), h.events()), (count, events));
    assert_eq!(
        state(&h, "m1").1.as_deref(),
        first.body["proposal_id"].as_str()
    );
}

#[tokio::test]
async fn a_manifest_for_card_a_never_binds_a_proposal_that_changes_card_b() {
    let (h, a) = ready().await;
    let b = card(&h, "fleet", "b").await["revision"].as_i64().unwrap();
    assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
    let (count, events) = (proposals(&h), h.events());
    // Cited from an op on card B.
    let r = propose(&h, AGENT_TOKEN, vec![cite("fleet", "b", b, &[eid("m1")])]).await;
    assert_refused(&r);
    // Cited from card A while another op changes card B.
    let r = propose(
        &h,
        AGENT_TOKEN,
        vec![cite("fleet", "a", a, &[eid("m1")]), op("fleet", "b", b)],
    )
    .await;
    assert_refused(&r);
    assert_eq!((proposals(&h), h.events()), (count, events));
    assert_eq!(state(&h, "m1"), ("pending_review".into(), None));
}

#[tokio::test]
async fn the_same_card_id_or_an_other_card_in_another_scope_is_refused() {
    let (h, a) = ready().await;
    let other = card(&h, "repo:other", "a").await["revision"]
        .as_i64()
        .unwrap();
    let b = card(&h, "fleet", "b").await["revision"].as_i64().unwrap();
    assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
    let (count, events) = (proposals(&h), h.events());
    let r = propose(
        &h,
        AGENT_TOKEN,
        vec![cite("repo:other", "a", other, &[eid("m1")])],
    )
    .await;
    assert_refused(&r);
    let supersede = |scope: &str, revision: i64| {
        let mut o = op("fleet", "a", a);
        o.as_object_mut().unwrap().remove("text");
        o["type"] = json!("supersede_card");
        o["other_card"] = json!({"scope":scope,"card_id":if scope == "fleet" {"b"} else {"a"},"base_revision":revision});
        o
    };
    let r = propose(
        &h,
        AGENT_TOKEN,
        vec![
            cite("fleet", "a", a, &[eid("m1")]),
            supersede("repo:other", other),
        ],
    )
    .await;
    assert_refused(&r);
    assert_eq!((proposals(&h), h.events()), (count, events));
    // Control: an other_card in the attested scope binds.
    let r = propose(
        &h,
        AGENT_TOKEN,
        vec![cite("fleet", "a", a, &[eid("m1")]), supersede("fleet", b)],
    )
    .await;
    assert_eq!(r.status, 201, "{r:?}");
    assert_eq!(state(&h, "m1").1.as_deref(), r.body["proposal_id"].as_str());
}

#[tokio::test]
async fn a_revision_or_card_the_manifest_did_not_check_is_refused() {
    let (h, a) = ready().await;
    assert_eq!(attest(&h, "m1", "fleet", "a", a).await.status, 201);
    let moved = h
        .put(
            ADMIN_TOKEN,
            "fleet",
            "a",
            Some(a),
            "Use the newer gate. ^c-abcdef",
        )
        .await;
    let now = rev(&moved);
    assert!(now > a, "{moved:?}");
    let mut o = cite("fleet", "a", now, &[eid("m1")]);
    o["expected_block_hash"] = json!(ids::sha256_hex(b"Use the newer gate. ^c-abcdef"));
    let (count, events) = (proposals(&h), h.events());
    assert_refused(&propose(&h, AGENT_TOKEN, vec![o]).await);
    assert_eq!((proposals(&h), h.events()), (count, events));
    // At attach, a manifest naming another card or scope is refused.
    for (scope, card_id) in [("fleet", "b"), ("repo:other", "a")] {
        let bytes = manifest(scope, card_id, now);
        let r = h
            .call(
                AGENT_TOKEN,
                attach_request(
                    &h,
                    "mismatch",
                    "fleet",
                    "a",
                    &bytes,
                    "memory-verify-manifest",
                ),
            )
            .await;
        assert_eq!(
            (r.status, code(&r)),
            (422, "memory_manifest_card_mismatch"),
            "{r:?}"
        );
    }
    assert!(state_opt(&h, "mismatch").is_none());
    assert_eq!(h.events(), events);
    // A verifier's record must be a manifest.
    let r = h
        .call(
            AGENT_TOKEN,
            attach_request(&h, "plain", "fleet", "a", b"not a manifest", "verification"),
        )
        .await;
    assert_eq!(
        (r.status, code(&r)),
        (422, "memory_evidence_invalid"),
        "{r:?}"
    );
}

#[tokio::test]
async fn an_accepted_proposal_for_b_never_activates_a_contrary_record_for_a() {
    let (h, a) = ready().await;
    let b = card(&h, "fleet", "b").await["revision"].as_i64().unwrap();
    assert_eq!(attest(&h, "ma", "fleet", "a", a).await.status, 201);
    // A manifest for A filed as an attestation of B is refused at attach.
    let bytes = manifest("fleet", "a", a);
    let r = h
        .call(
            AGENT_TOKEN,
            attach_request(
                &h,
                "mb-for-a",
                "fleet",
                "b",
                &bytes,
                "memory-verify-manifest",
            ),
        )
        .await;
    assert_eq!(code(&r), "memory_manifest_card_mismatch", "{r:?}");
    // Citing A's record from any proposal touching B is refused.
    assert_refused(&propose(&h, AGENT_TOKEN, vec![cite("fleet", "b", b, &[eid("ma")])]).await);
    assert_refused(
        &propose(
            &h,
            AGENT_TOKEN,
            vec![
                cite("fleet", "b", b, &[eid("ma")]),
                cite("fleet", "a", a, &[eid("ma")]),
            ],
        )
        .await,
    );
    // A proposal for B with its own manifest is accepted.
    assert_eq!(attest(&h, "mb", "fleet", "b", b).await.status, 201);
    let opened = propose(&h, AGENT_TOKEN, vec![cite("fleet", "b", b, &[eid("mb")])]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    let accepted = decision(&h, &opened.body["proposal_id"], "accept").await;
    assert_eq!(accepted.body["state"], "applied", "{accepted:?}");
    assert_eq!(state(&h, "mb").0, "active");
    assert_eq!(state(&h, "ma"), ("pending_review".into(), None));
    assert!(!held(&h).await);
    h.set_clock(h.now() + 24 * 3600 * 1000 + 1);
    let db = Connection::open(h.memory()).unwrap();
    writer::sweep(&db, h.now()).unwrap();
    assert_eq!(state(&h, "ma").0, "inert");
    assert!(!held(&h).await);
}

/// Three proposals from the verifier on `fleet/a`, binding zero, one and
/// three manifests.
async fn three_proposals(h: &Hub, a: i64) -> [Json; 3] {
    for name in ["m1", "m2", "m3", "m4"] {
        assert_eq!(attest(h, name, "fleet", "a", a).await.status, 201);
    }
    let mut ids = Vec::new();
    for refs in [
        vec![format!("node-1/{WORKSPACE}/plain")],
        vec![eid("m1")],
        vec![eid("m2"), eid("m3"), eid("m4")],
    ] {
        let r = propose(h, AGENT_TOKEN, vec![cite("fleet", "a", a, &refs)]).await;
        assert_eq!(r.status, 201, "{r:?}");
        ids.push(r.body["proposal_id"].clone());
    }
    ids.try_into().unwrap()
}

#[tokio::test]
async fn proposal_show_returns_manifest_hashes_to_a_reviewer_of_every_target() {
    let (h, a) = ready().await;
    let [zero, one, many] = three_proposals(&h, a).await;
    grant_to(&h, "node-1", "fleet", &["read", "review"]).await;
    for token in [ADMIN_TOKEN, NODE_TOKEN] {
        let r = show(&h, token, &zero).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert_eq!(r.body["verifier_manifests"], json!([]));
        let r = show(&h, token, &one).await;
        let bytes = manifest("fleet", "a", a);
        assert_eq!(
            r.body["verifier_manifests"],
            json!([{"evidence_id":eid("m1"),"artifact_kind":"memory-verify-manifest",
                    "artifact_sha256":ids::sha256_hex(&bytes),"stance":"contrary","state":"pending_review"}])
        );
        let r = show(&h, token, &many).await;
        let listed: Vec<_> = r.body["verifier_manifests"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["evidence_id"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(listed, vec![eid("m2"), eid("m3"), eid("m4")]);
    }
    // The state follows the decision.
    decision(&h, &one, "reject").await;
    assert_eq!(
        show(&h, ADMIN_TOKEN, &one).await.body["verifier_manifests"][0]["state"],
        "inert"
    );
}

/// The reply's structure with every scalar replaced by its type.
fn shape(v: &Json) -> Json {
    match v {
        Json::Object(map) => Json::Object(map.iter().map(|(k, v)| (k.clone(), shape(v))).collect()),
        Json::Array(items) => Json::Array(items.iter().map(shape).collect()),
        Json::String(_) => json!("string"),
        Json::Number(_) => json!("number"),
        Json::Bool(_) => json!("bool"),
        Json::Null => Json::Null,
    }
}
/// The reply without the fields a non-reviewer sees and that legitimately
/// differ between proposals: the id, the diff and the resulting hash.
fn masked(v: &Json) -> Json {
    let mut v = v.clone();
    v["proposal_id"] = Json::Null;
    v["diff"] = Json::Null;
    for target in v["targets"].as_array_mut().unwrap() {
        target["after_hash"] = Json::Null;
    }
    v
}

#[tokio::test]
async fn a_non_reviewer_sees_the_same_shape_for_zero_one_and_many_manifests() {
    let (h, a) = ready().await;
    let ids = three_proposals(&h, a).await;
    // `read` alone on the target scope, then `review` on another scope only.
    grant_to(&h, "node-1", "fleet", &["read"]).await;
    for extra in [None, Some("repo:other")] {
        if let Some(scope) = extra {
            grant_to(&h, "node-1", scope, &["read", "review"]).await;
        }
        let replies: Vec<Json> = {
            let mut out = Vec::new();
            for id in &ids {
                let r = show(&h, NODE_TOKEN, id).await;
                assert_eq!(r.status, 200, "{r:?}");
                assert!(r.body.get("verifier_manifests").is_none(), "{r:?}");
                out.push(r.body);
            }
            out
        };
        for reply in &replies[1..] {
            assert_eq!(shape(reply), shape(&replies[0]));
            assert_eq!(masked(reply), masked(&replies[0]));
        }
    }
}

#[tokio::test]
async fn the_list_needs_review_on_each_bound_attestations_scope() {
    let (h, a) = ready().await;
    let [_, one, _] = three_proposals(&h, a).await;
    grant_to(&h, "node-1", "fleet", &["read", "review"]).await;
    assert!(show(&h, NODE_TOKEN, &one).await.body["verifier_manifests"].is_array());
    // A record from another scope bound to the same proposal, which the
    // binding check never allows, still keeps the list from this reviewer.
    let db = Connection::open(h.memory()).unwrap();
    db.execute(
        "INSERT INTO memory_evidence(evidence_id,scope,card_id,workspace_id,principal,credential_id,credential_class,artifact_kind,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,created_ms) SELECT 'agent:dot/workspace-a/elsewhere','repo:other',card_id,workspace_id,principal,credential_id,credential_class,artifact_kind,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,created_ms FROM memory_evidence WHERE evidence_id=?1",
        [eid("m1")],
    )
    .unwrap();
    db.execute(
        "INSERT INTO memory_evidence_bindings VALUES('agent:dot/workspace-a/elsewhere','agent:dot','repo:other',?1,1,'pending_review',?2,?3,?3,NULL)",
        rusqlite::params![a, one.as_str().unwrap(), h.now()],
    )
    .unwrap();
    let r = show(&h, NODE_TOKEN, &one).await;
    assert_eq!(r.status, 200);
    assert!(r.body.get("verifier_manifests").is_none(), "{r:?}");
    assert_eq!(
        show(&h, ADMIN_TOKEN, &one).await.body["verifier_manifests"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}

#[tokio::test]
async fn non_verifier_attestations_behave_exactly_as_before() {
    let h = Hub::with(clean(), None);
    policy(&h, "sha256:policy-one");
    grant(&h, "fleet", &["read", "reflect"]).await;
    let a = rev(&h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await);
    // Unflagged: any kind and any bytes, no manifest parsing, no binding row.
    let r = h
        .call(
            AGENT_TOKEN,
            attach_request(
                &h,
                "plain",
                "fleet",
                "a",
                b"free text",
                "memory-verify-manifest",
            ),
        )
        .await;
    assert_eq!(r.status, 201, "{r:?}");
    assert!(r.body.get("state").is_none());
    assert!(state_opt(&h, "plain").is_none());
    // The contrary record holds at once, and a proposal citing it is stored.
    assert!(held(&h).await);
    let opened = propose(
        &h,
        AGENT_TOKEN,
        vec![cite("fleet", "a", a, &[eid("plain")])],
    )
    .await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(
        show(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["verifier_manifests"],
        json!([])
    );
    // Eight records still fill the principal's slots on the card.
    for i in 0..7 {
        assert_eq!(
            h.call(
                AGENT_TOKEN,
                attach_request(&h, &format!("p{i}"), "fleet", "a", b"x", "verification"),
            )
            .await
            .status,
            201
        );
    }
    let r = h
        .call(
            AGENT_TOKEN,
            attach_request(&h, "p8", "fleet", "a", b"x", "verification"),
        )
        .await;
    assert_eq!(code(&r), "memory_evidence_principal_cap", "{r:?}");
}

#[test]
fn version_eleven_migrates_to_twelve_with_no_binding_rows() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = Connection::open(&path).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch("DROP TABLE memory_evidence_bindings; PRAGMA user_version=11;")
        .unwrap();
    let epoch = schema::log_epoch(&db).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let version = |db: &Connection| -> i64 {
        db.query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap()
    };
    assert_eq!(version(&db), 12);
    assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
    let rows: i64 = db
        .query_row("SELECT count(*) FROM memory_evidence_bindings", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(rows, 0);
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    assert_eq!(version(&db), 12);
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

/// A manifest padded to just under the 64 KiB artifact limit.
fn large_manifest(a: i64) -> Vec<u8> {
    let mut m: Json = serde_json::from_slice(&manifest("fleet", "a", a)).unwrap();
    let base = m.to_string().len() + r#","pad":"""#.len();
    m["pad"] = json!("x".repeat(65_000 - base));
    let bytes = m.to_string().into_bytes();
    assert_eq!(bytes.len(), 65_000);
    bytes
}
async fn attach_large(h: &Hub, name: &str, a: i64) -> Reply {
    let bytes = large_manifest(a);
    h.call(
        AGENT_TOKEN,
        attach_request(h, name, "fleet", "a", &bytes, "memory-verify-manifest"),
    )
    .await
}
fn purged(h: &Hub) -> (i64, i64, i64) {
    let db = Connection::open(h.memory()).unwrap();
    let count = |sql: &str| -> i64 { db.query_row(sql, [], |r| r.get(0)).unwrap() };
    (
        count("SELECT count(*) FROM memory_evidence_bindings WHERE purged_ms IS NOT NULL"),
        count(
            "SELECT count(*) FROM memory_evidence e JOIN memory_evidence_bindings b USING(evidence_id) WHERE b.purged_ms IS NOT NULL AND e.tombstoned=1 AND length(e.artifact_bytes)=0 AND length(e.artifact_sha256)=64",
        ),
        count(
            "SELECT count(*) FROM memory_events WHERE kind='evidence.purge' AND json_extract(payload,'$.reason')='verifier_retention'",
        ),
    )
}

#[tokio::test]
async fn churn_hits_the_retained_budget_and_the_35_day_purge_frees_it() {
    let (h, a) = ready().await;
    let size = large_manifest(a).len() as i64;
    let expected = manifest_binding::BUDGET.principal_bytes / size;
    assert!(expected < manifest_binding::BUDGET.principal_records);
    let (mut accepted, mut refused, mut inert) = (0, None, 0);
    // Attach eight, then reject their proposal so all eight go inert and free
    // their slots. Without a retained budget this would never stop.
    'rounds: for round in 0..40 {
        let mut names = Vec::new();
        for i in 0..8 {
            let name = format!("r{round}-{i}");
            let r = attach_large(&h, &name, a).await;
            if r.status != 201 {
                refused = Some(r);
                break 'rounds;
            }
            accepted += 1;
            names.push(eid(&name));
        }
        let opened = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &names)]).await;
        assert_eq!(opened.status, 201, "{opened:?}");
        let r = decision(&h, &opened.body["proposal_id"], "reject").await;
        assert_eq!(r.body["state"], "rejected", "{r:?}");
        inert += names.len() as i64;
    }
    let refused = refused.expect("the retained budget refuses churn");
    assert_eq!(
        (refused.status, code(&refused)),
        (429, "memory_evidence_retained_principal_quota"),
        "{refused:?}"
    );
    assert_eq!(accepted, expected);
    assert!(!held(&h).await);
    // Not yet: records keep their bytes for 35 days after going inert.
    h.set_clock(h.now() + manifest_binding::RETENTION_MS - 60_000);
    let db = Connection::open(h.memory()).unwrap();
    writer::sweep(&db, h.now()).unwrap();
    assert_eq!(purged(&h), (0, 0, 0));
    assert_eq!(
        code(&attach_large(&h, "early", a).await),
        "memory_evidence_retained_principal_quota"
    );
    h.set_clock(h.now() + 60_000);
    // Each idle sweep purges a bounded batch. Keep sweeping until done.
    for _ in 0..200 {
        if purged(&h).0 == inert {
            break;
        }
        let _ = writer::sweep(&db, h.now());
    }
    assert_eq!(purged(&h), (inert, inert, inert));
    let again = attach_large(&h, "after-purge", a).await;
    assert_eq!(again.status, 201, "{again:?}");
    // Purged records never hold and never count. `purged` checked above that
    // each keeps its sha256.
    assert!(!held(&h).await);
}

#[tokio::test]
async fn the_scope_and_record_budgets_count_retained_records_only() {
    let (h, a) = ready().await;
    for name in ["m1", "m2"] {
        assert_eq!(attest(&h, name, "fleet", "a", a).await.status, 201);
    }
    let size = manifest("fleet", "a", a).len() as i64;
    let check = |budget: manifest_binding::Budget, scope: &str| {
        let db = Connection::open(h.memory()).unwrap();
        let tx = crate::writer::worker::Tx(&db);
        manifest_binding::retained_refusal(&tx, "agent:dot", scope, size, &budget)
            .unwrap()
            .map(|r| code(&r).to_string())
    };
    let roomy = manifest_binding::BUDGET;
    assert_eq!(check(roomy, "fleet"), None);
    let scope_bytes = manifest_binding::Budget {
        scope_bytes: 3 * size - 1,
        ..roomy
    };
    assert_eq!(
        check(scope_bytes, "fleet").as_deref(),
        Some("memory_evidence_retained_scope_quota")
    );
    // Another scope has nothing retained.
    assert_eq!(check(scope_bytes, "repo:other"), None);
    let records = manifest_binding::Budget {
        principal_records: 2,
        ..roomy
    };
    assert_eq!(
        check(records, "fleet").as_deref(),
        Some("memory_evidence_retained_principal_quota")
    );
    // An admin purge stops a record counting at once.
    let purge = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"evidence.purge","evidence_id":eid("m1"),"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(purge.status, 200, "{purge:?}");
    assert_eq!(check(records, "fleet"), None);
    assert_eq!(check(scope_bytes, "fleet"), None);
}

#[tokio::test]
async fn retention_purges_only_records_inert_for_35_days() {
    let (h, a) = ready().await;
    for name in ["kept", "dropped"] {
        assert_eq!(attest(&h, name, "fleet", "a", a).await.status, 201);
    }
    let accepted = propose(&h, AGENT_TOKEN, vec![cite("fleet", "a", a, &[eid("kept")])]).await;
    let rejected = propose(
        &h,
        AGENT_TOKEN,
        vec![cite("fleet", "a", a, &[eid("dropped")])],
    )
    .await;
    assert_eq!(
        decision(&h, &rejected.body["proposal_id"], "reject")
            .await
            .status,
        200
    );
    assert_eq!(
        decision(&h, &accepted.body["proposal_id"], "accept")
            .await
            .body["state"],
        "applied"
    );
    let db = Connection::open(h.memory()).unwrap();
    h.set_clock(h.now() + manifest_binding::RETENTION_MS + 1);
    writer::sweep(&db, h.now()).unwrap();
    assert_eq!(state(&h, "kept").0, "active");
    assert_eq!(purged(&h), (1, 1, 1));
    let bytes = |name: &str| -> i64 {
        db.query_row(
            "SELECT length(artifact_bytes) FROM memory_evidence WHERE evidence_id=?1",
            [eid(name)],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(bytes("dropped"), 0);
    assert!(bytes("kept") > 0);
    // A purged row is final.
    assert!(
        db.execute(
            "UPDATE memory_evidence_bindings SET purged_ms=1 WHERE evidence_id=?1",
            [eid("dropped")],
        )
        .is_err()
    );
}
