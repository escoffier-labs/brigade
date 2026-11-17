//! Anchoring keeps original bytes and uses the existing proposal journal.
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{proposal_blocks::anchor_blocks, proposal_tests::*, tests::*, *};

#[test]
fn deterministic_suffixes_preserve_bytes_and_reserve_existing_ids() {
    let original = "---\nevidence: []\n---\nFirst claim.\n\n- Second claim. ^c-abcdef\n";
    let anchored = anchor_blocks(original, "a").unwrap();
    let digest = ids::sha256_hex(b"a\x000\x00First claim.\x000");
    let expected = original.replacen(
        "First claim.",
        &format!("First claim. ^c-{}", &digest[..6]),
        1,
    );
    assert_eq!(anchored, expected);
    assert_eq!(anchor_blocks(original, "a").unwrap(), anchored);
    assert_eq!(anchor_blocks(&anchored, "a").unwrap(), anchored);
    assert_eq!(
        super::proposal_blocks::strip_anchors(&anchored),
        original.replace(" ^c-abcdef", "")
    );
    assert!(anchored.ends_with("- Second claim. ^c-abcdef\n"));
    let first = super::proposal_blocks::pins(&anchored)[0]["block_id"]
        .as_str()
        .unwrap()
        .to_string();
    let colliding = format!("First claim.\n\nKeep. ^{first}\n");
    let collision_result = anchor_blocks(&colliding, "a").unwrap();
    assert_eq!(collision_result.matches(&format!("^{first}")).count(), 1);
    assert!(collision_result.ends_with(&format!("Keep. ^{first}\n")));
    let reserved = format!("---\nclaim_anchor_ids: [\"{first}\"]\n---\nFirst claim.\n");
    let result = anchor_blocks(&reserved, "a").unwrap();
    assert!(!result.contains(&format!(" ^{first}")));
    assert!(anchor_blocks("A. ^c-abcdef\n\nB. ^c-abcdef\n", "a").is_err());
}

#[tokio::test]
async fn human_anchoring_is_review_only_and_rollback_restores_exact_bytes() {
    let h = Hub::with(clean(), None);
    let original = "---\nevidence: []\n---\nSee [[missing]].\n\n- Keep this. ^c-abcdef\n";
    let put = h.put(ADMIN_TOKEN, "fleet", "a", None, original).await;
    let before = card(&h, "fleet", "a").await;
    assert_eq!(before["auto_authored"], false);
    let operation =
        json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":rev(&put)});
    let opened = submit(&h, ADMIN_TOKEN, vec![operation]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    // No local policy can replace the hub's missing authority signals.
    let automatic = h.call(ADMIN_TOKEN, json!({"action":"auto_apply","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_ne!(automatic.body["state"], "applied", "{automatic:?}");
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
    let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let after = card(&h, "fleet", "a").await;
    assert_eq!(
        after["text"],
        anchor_blocks(before["text"].as_str().unwrap(), "a").unwrap()
    );
    let rollback = h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(rollback.status, 201, "{rollback:?}");
    let restored = decide(&h, ADMIN_TOKEN, &rollback.body["proposal_id"]).await;
    assert_eq!(restored.body["state"], "applied", "{restored:?}");
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
}

#[test]
fn shared_span_table_and_whitespace_suffixes() {
    let cases: Json =
        serde_json::from_str(include_str!("../../../../tests/fixtures/anchor_spans.json")).unwrap();
    for case in cases.as_array().unwrap() {
        let text = case["text"].as_str().unwrap();
        let expected: Vec<_> = case["blocks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|b| b.as_str().unwrap())
            .collect();
        assert_eq!(
            proposal_blocks::span_texts(text),
            expected,
            "{}",
            case["name"]
        );
        let anchored = anchor_blocks(text, "a").unwrap();
        assert_eq!(anchor_blocks(&anchored, "a").unwrap(), anchored);
        assert_eq!(
            proposal_blocks::strip_anchors(&anchored),
            proposal_blocks::strip_anchors(text)
        );
    }
    for text in ["Claim. ^c-abcdef   \n", "Café. ^c-abcdef\t\r\n"] {
        assert_eq!(anchor_blocks(text, "a").unwrap(), text);
        assert_eq!(proposal_blocks::pins(text)[0]["block_id"], "c-abcdef");
        let pins = proposal_blocks::pins(text);
        assert!(proposal_blocks::pin(text, "c-abcdef", pins[0]["hash"].as_str().unwrap()).is_ok());
        assert_eq!(
            proposal_blocks::strip_anchors(text),
            text.replace(" ^c-abcdef", "")
        );
    }
    for (before, ending) in [("Claim.  \n", "  \n"), ("Café.\t\r\n", "\t\r\n")] {
        let after = anchor_blocks(before, "a").unwrap();
        assert!(after.ends_with(ending), "{after:?}");
        assert_eq!(proposal_blocks::strip_anchors(&after), before);
    }
}

// These helpers run on the fixture scope, where a retrieval report authorizes the apply.
// ranking_gate_tests covers real scopes, where the ranking projection does.
async fn anchor_ready(token: &str) -> (Hub, Json) {
    if token != AGENT_TOKEN {
        return anchor_ready_origin(token, None).await;
    }
    let h = Hub::with(clean(), None);
    attestation_tests::policy(&h);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    let grant = h.call(ADMIN_TOKEN, json!({"action":"grant","principal":"node-1","scope":FIXTURE_SCOPE,"rights":["read","reflect"],"client_event_id":h.id()})).await;
    assert_eq!(grant.status, 200);
    let put = h.call(ADMIN_TOKEN, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"a","text":format!("{OLD}\n\nClaim.\n"),"client_event_id":h.id()})).await;
    assert_eq!(put.status, 201);
    for (i, id) in attestation_tests::REFS.iter().enumerate() {
        assert_eq!(
            attestation_tests::attach(&h, if i == 2 { AGENT_TOKEN } else { NODE_TOKEN }, id)
                .await
                .status,
            201
        );
    }
    let opened = attestation_tests::proposal(&h, rev(&put), json!(attestation_tests::REFS)).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    super::retrieval_eval_tests::record_current(&h).await;
    let applied = h
        .call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&h, &opened.body["proposal_id"]).await,
        )
        .await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    h.set_clock(h.now() + 86_401_000);
    let db = rusqlite::Connection::open(&h.hub).unwrap();
    let raw: String = db
        .query_row("SELECT document FROM fleet_policy_revisions", [], |r| {
            r.get(0)
        })
        .unwrap();
    let mut doc: Json = serde_json::from_str(&raw).unwrap();
    doc["memory"]["tier0_op_classes"] = json!(["anchor_blocks"]);
    db.execute(
        "UPDATE fleet_policy_revisions SET document=?1",
        [doc.to_string()],
    )
    .unwrap();
    let current = card(&h, FIXTURE_SCOPE, "a").await;
    assert_eq!(current["auto_authored"], token == AGENT_TOKEN);
    let opened = submit(&h, ADMIN_TOKEN, vec![json!({"type":"anchor_blocks","scope":FIXTURE_SCOPE,"card_id":"a","base_revision":current["revision"]})]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    super::retrieval_eval_tests::record_current(&h).await;
    (h, opened.body["proposal_id"].clone())
}

async fn anchor_ready_origin(token: &str, origin: Option<&str>) -> (Hub, Json) {
    let h = Hub::with(clean(), None);
    attestation_tests::policy(&h);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect", "import"]).await;
    let db = rusqlite::Connection::open(&h.hub).unwrap();
    let doc: String = db
        .query_row("SELECT document FROM fleet_policy_revisions", [], |r| {
            r.get(0)
        })
        .unwrap();
    let mut doc: Json = serde_json::from_str(&doc).unwrap();
    doc["memory"]["tier0_op_classes"] = json!(["anchor_blocks"]);
    db.execute(
        "UPDATE fleet_policy_revisions SET document=?1",
        [doc.to_string()],
    )
    .unwrap();
    let put = h.call(token, json!({"action":"import","scope":FIXTURE_SCOPE,"card_id":"a","text":"Claim.\n","origin":origin,"client_event_id":h.id()})).await;
    assert_eq!(put.status, 201, "{put:?}");
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![
            json!({"type":"anchor_blocks","scope":FIXTURE_SCOPE,"card_id":"a","base_revision":rev(&put)}),
        ],
    )
    .await;
    assert_eq!(opened.status, 201, "{opened:?}");
    super::retrieval_eval_tests::record_current(&h).await;
    (h, opened.body["proposal_id"].clone())
}

#[tokio::test]
async fn structural_anchoring_applies_with_every_gate_satisfied() {
    let (h, id) = anchor_ready(AGENT_TOKEN).await;
    let report = attestation_tests::report(&h, &id).await;
    assert!(preflight::all_satisfied(&report.body), "{report:?}");
    let applied = h
        .call(ADMIN_TOKEN, attestation_tests::auto_request(&h, &id).await)
        .await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
}

#[tokio::test]
async fn human_anchoring_requires_review_and_live_authorship_is_rechecked() {
    let (h, id) = anchor_ready(ADMIN_TOKEN).await;
    let report = attestation_tests::report(&h, &id).await;
    assert!(
        report.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "tier_requires_review"),
        "{report:?}"
    );
    let automatic = h
        .call(ADMIN_TOKEN, attestation_tests::auto_request(&h, &id).await)
        .await;
    assert_eq!(automatic.body["reason"], "auto_apply_held");
    assert!(
        automatic.body["preflight"]["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|check| check["code"] == "tier_requires_review")
    );
    assert!(automatic.body.get("auto_apply_gates").is_none());
    let (h, id) = anchor_ready_origin(AGENT_TOKEN, Some("interactive")).await;
    let report = attestation_tests::report(&h, &id).await;
    assert!(
        report.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "tier_requires_review"),
        "{report:?}"
    );
    let (h, id) = anchor_ready(AGENT_TOKEN).await;
    let current = card(&h, FIXTURE_SCOPE, "a").await;
    let changed = h
        .put(
            ADMIN_TOKEN,
            FIXTURE_SCOPE,
            "a",
            current["revision"].as_i64(),
            "Human edit.\n",
        )
        .await;
    assert_eq!(changed.status, 201, "{changed:?}");
    let reply = h
        .call(ADMIN_TOKEN, attestation_tests::auto_request(&h, &id).await)
        .await;
    assert_ne!(reply.body["state"], "applied", "{reply:?}");
}

#[tokio::test]
async fn anchor_proof_refuses_non_suffix_changes_and_client_fields() {
    let (h, id) = anchor_ready(AGENT_TOKEN).await;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let raw: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let mut body: Json = serde_json::from_str(&raw).unwrap();
    body["cards"][0]["after_text"] = json!("Changed claim. ^c-abcdef\n");
    db.execute(
        "UPDATE memory_proposals SET body=?1 WHERE proposal_id=?2",
        [body.to_string(), id.as_str().unwrap().to_string()],
    )
    .unwrap();
    let report = attestation_tests::report(&h, &id).await;
    assert!(
        report.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "tier_requires_review"),
        "{report:?}"
    );
    for field in ["text", "evidence_ids", "before_text", "after_text"] {
        let mut operation =
            json!({"type":"anchor_blocks","scope":FIXTURE_SCOPE,"card_id":"a","base_revision":1});
        operation[field] = json!("workspace supplied");
        assert!(proposals::op_schema(&operation).is_err());
        let refused = submit(&h, ADMIN_TOKEN, vec![operation]).await;
        assert_eq!(refused.status, 422, "{refused:?}");
    }
}

async fn forged_origin_stays_review_only(token: &str, origin: Option<&str>, action: &str) {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read", "write", "import", "reflect"]).await;
    let put = h.call(token, json!({"action":action,"scope":"fleet","card_id":"a","text":"Claim.\n","origin":origin,"client_event_id":h.id()})).await;
    assert_eq!(put.status, 201, "{put:?}");
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![
            json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":rev(&put)}),
        ],
    )
    .await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
}

#[tokio::test]
async fn admin_worker_origin_is_not_authority() {
    for action in ["put", "import"] {
        forged_origin_stays_review_only(ADMIN_TOKEN, Some("worker"), action).await;
    }
}

#[tokio::test]
async fn node_scheduled_origin_is_not_authority() {
    let h = Hub::with(clean(), None);
    let grant = h.call(ADMIN_TOKEN, json!({"action":"grant","principal":"node-1","scope":"fleet","rights":["read","write","import"],"client_event_id":h.id()})).await;
    assert_eq!(grant.status, 200);
    for action in ["put", "import"] {
        let put = h.call(NODE_TOKEN, json!({"action":action,"scope":"fleet","card_id":action,"text":"Claim.\n","origin":"scheduled","client_event_id":h.id()})).await;
        assert_eq!(put.status, 201, "{put:?}");
        let opened = submit(&h, ADMIN_TOKEN, vec![json!({"type":"anchor_blocks","scope":"fleet","card_id":action,"base_revision":rev(&put)})]).await;
        assert_eq!(opened.status, 201, "{opened:?}");
        assert_eq!(opened.body["tier"], 2, "{opened:?}");
    }
}

#[tokio::test]
async fn principal_without_origin_is_not_authority() {
    for action in ["put", "import"] {
        forged_origin_stays_review_only(AGENT_TOKEN, None, action).await;
    }
}

#[tokio::test]
async fn human_reviewed_apply_is_not_automated_authorship() {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read", "review"]).await;
    let put = h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":"a","text":format!("{OLD}\n\nClaim.\n"),"origin":"worker","client_event_id":h.id()})).await;
    assert_eq!(put.status, 201);
    let edit = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&put))]).await;
    let applied = decide(&h, AGENT_TOKEN, &edit.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let current = card(&h, "fleet", "a").await;
    let opened = submit(&h, ADMIN_TOKEN, vec![json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":current["revision"]})]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
}

#[tokio::test]
async fn anchor_no_delta_has_stable_code_only_for_all_no_delta_anchors() {
    let h = Hub::with(clean(), None);
    let put = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let anchor =
        json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":rev(&put)});
    let reply = submit(&h, ADMIN_TOKEN, vec![anchor.clone()]).await;
    assert_eq!(reply.status, 422);
    assert_eq!(
        reply.body["error"],
        "proposal refused because at least one op is invalid"
    );
    assert_eq!(
        reply.body["ops"],
        json!([{"index":0,"valid":false,"reason":"op has no delta"}])
    );
    assert_eq!(reply.body["code"], "memory_anchor_no_delta");
    let mut invalid = anchor.clone();
    invalid["card_id"] = json!("missing");
    let reply = submit(&h, ADMIN_TOKEN, vec![anchor.clone(), invalid]).await;
    assert_eq!(reply.body["code"], "memory_proposal_invalid");
    let mut no_delta = op("fleet", "a", rev(&put));
    no_delta["text"] = json!("Use the focused gate.");
    let reply = submit(&h, ADMIN_TOKEN, vec![anchor, no_delta]).await;
    assert_eq!(reply.body["code"], "memory_proposal_invalid");
}

#[tokio::test]
async fn reflector_withdraws_only_own_anchors_and_releases_quota() {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read", "reflect"]).await;
    let put = h.put(ADMIN_TOKEN, "fleet", "a", None, "Claim.\n").await;
    let op =
        json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":rev(&put)});
    let other = submit(&h, ADMIN_TOKEN, vec![op.clone()]).await;
    let epoch = h.epoch().await;
    let reject = |id: Json| json!({"action":"decide","proposal_id":id,"decision":"reject","base_epoch":epoch,"client_event_id":h.id()});
    let denied = h
        .call(AGENT_TOKEN, reject(other.body["proposal_id"].clone()))
        .await;
    assert_eq!(denied.status, 403, "{denied:?}");
    let reviewed = h
        .call(ADMIN_TOKEN, reject(other.body["proposal_id"].clone()))
        .await;
    assert_eq!(reviewed.body["state"], "rejected", "{reviewed:?}");
    let own = submit(&h, AGENT_TOKEN, vec![op]).await;
    assert_eq!(own.status, 201, "{own:?}");
    let before = h.call(AGENT_TOKEN, json!({"action":"status"})).await;
    assert_eq!(before.body["proposal_quota"]["pending"], 1);
    assert_eq!(before.body["proposal_quota"]["pending_per_principal"], 32);
    let closed = h
        .call(AGENT_TOKEN, reject(own.body["proposal_id"].clone()))
        .await;
    assert_eq!(closed.body["state"], "rejected", "{closed:?}");
    assert_eq!(closed.body["reason"], "anchor_withdrawn");
    let after = h.call(AGENT_TOKEN, json!({"action":"status"})).await;
    assert_eq!(after.body["proposal_quota"]["pending"], 0);
    let put = h
        .put(ADMIN_TOKEN, "fleet", "b", None, "Claim. ^c-abcdef\n")
        .await;
    let non_anchor = submit(&h, AGENT_TOKEN, vec![json!({"type":"remove_block","scope":"fleet","card_id":"b","base_revision":rev(&put),"block_id":"c-abcdef","expected_block_hash":ids::sha256_hex(b"Claim. ^c-abcdef")})]).await;
    assert_eq!(non_anchor.status, 201, "{non_anchor:?}");
    let denied = h
        .call(AGENT_TOKEN, reject(non_anchor.body["proposal_id"].clone()))
        .await;
    assert_eq!(denied.status, 403, "{denied:?}");
}
