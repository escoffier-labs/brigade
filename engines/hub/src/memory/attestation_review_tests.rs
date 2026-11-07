//! S19e review regressions at the wire and transaction boundaries.
use super::{attestation_tests::*, proposal_tests::*, tests::*, *};
use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::Connection;

fn request(h: &Hub, principal: &str, workspace: &str, local: &str, stance: &str) -> Json {
    json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":workspace,
        "evidence_id":format!("{principal}/{workspace}/{local}"),"artifact_kind":"verification",
        "artifact_sha256":ids::sha256_hex(b"valid evidence"),"observed_at":h.now(),
        "artifact_base64":STANDARD.encode(b"valid evidence"),"stance":stance,"client_event_id":h.id()})
}
fn redacted(report: &Json) {
    for field in [
        "evidence_sha256",
        "attesting_credentials",
        "candidate_sha256",
        "delta_sha256",
    ] {
        assert!(report[field].is_null(), "{field}");
    }
    assert!(report["policy_limits"]["reflect_principals"].is_null());
    let mut visible = report.clone();
    visible["evaluation_sha256"] = Json::Null;
    assert_eq!(
        report["evaluation_sha256"],
        ids::sha256_hex(ids::canonical(&visible).as_bytes())
    );
}
#[tokio::test]
async fn automatic_replies_and_cached_replays_enforce_review_redaction() {
    for reviewed_first in [false, true] {
        let (h, id) = ready().await;
        if reviewed_first {
            grant(&h, "fleet", &["read", "reflect", "review"]).await;
        }
        let req = auto_request(&h, &id).await;
        let r = h.call(AGENT_TOKEN, req.clone()).await;
        assert_eq!(r.status, 200);
        if reviewed_first {
            assert!(r.body["preflight"]["candidate_sha256"].is_string());
            grant(&h, "fleet", &["read", "reflect"]).await;
        } else {
            redacted(&r.body["preflight"]);
            assert!(r.body["diff"].is_null());
            assert!(
                r.body["targets"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|t| t["after_hash"].is_null())
            );
            let cached:String=Connection::open(h.memory()).unwrap().query_row("SELECT response FROM memory_idempotency WHERE principal='agent:dot' AND client_event_id=?1",[req["client_event_id"].as_str().unwrap()],|r|r.get(0)).unwrap();
            let cached: Json = serde_json::from_str(&cached).unwrap();
            redacted(&cached["preflight"]);
            assert!(cached["diff"].is_null());
        }
        let replay = h.call(AGENT_TOKEN, req).await;
        redacted(&replay.body["preflight"]);
        assert!(replay.body["diff"].is_null());
        assert!(
            replay.body["targets"]
                .as_array()
                .unwrap()
                .iter()
                .all(|t| t["after_hash"].is_null())
        );
    }
}
#[tokio::test]
async fn held_automatic_attempts_leave_proposals_and_decision_events_untouched() {
    let (h, id) = ready().await;
    let db = Connection::open(h.memory()).unwrap();
    let before: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let count = h.events();
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(r.body["state"], "pending");
    let after: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        ids::sha256_hex(before.as_bytes()),
        ids::sha256_hex(after.as_bytes())
    );
    assert_eq!(h.events(), count);
}
#[tokio::test]
async fn contrary_and_negative_records_in_other_workspaces_hold_the_card() {
    for stance in ["contrary", "negative"] {
        let (h, id) = ready().await;
        assert_eq!(
            h.call(
                AGENT_TOKEN,
                request(&h, "agent:dot", "workspace-b", "late", stance)
            )
            .await
            .status,
            201
        );
        let before = bytes_snapshot(&h);
        let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
        assert_eq!(r.body["state"], "pending");
        assert!(
            r.body["preflight"]["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["code"] == "contrary_evidence")
        );
        assert_eq!(bytes_snapshot(&h), before);
        super::retrieval_eval_tests::record_current(&h).await;
        assert_eq!(h.call(ADMIN_TOKEN,json!({"action":"evidence.purge","evidence_id":"agent:dot/workspace-b/late","client_event_id":h.id()})).await.status,200);
        assert_eq!(
            h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await.body["state"],
            "applied"
        );
    }
}
#[tokio::test]
async fn clean_utf8_is_required_before_storing_artifacts() {
    let mut h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let mut req = request(&h, "hub:admin", "workspace-a", "binary", "support");
    req["artifact_base64"] = json!(STANDARD.encode([255u8]));
    req["artifact_sha256"] = json!(ids::sha256_hex(&[255u8]));
    assert_eq!(h.call(ADMIN_TOKEN, req).await.status, 422);
    for scan in [
        Scan::Private {
            ruleset: "test".into(),
            rules: vec![],
        },
        Scan::Flagged {
            ruleset: "test".into(),
            rules: vec![],
        },
        Scan::Unavailable,
        Scan::Clean {
            ruleset: String::new(),
        },
    ] {
        h.store.take();
        h.open(Arc::new(Fixed(scan)), None);
        let r = h
            .call(
                ADMIN_TOKEN,
                request(&h, "hub:admin", "workspace-a", "not-clean", "support"),
            )
            .await;
        assert!(r.status >= 400);
        let n: i64 = Connection::open(h.memory())
            .unwrap()
            .query_row("SELECT count(*) FROM memory_evidence", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0);
    }
}
#[tokio::test]
async fn principal_cap_spans_workspaces_and_admin_purge_frees_capacity() {
    let h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    for i in 0..8 {
        assert_eq!(
            h.call(
                ADMIN_TOKEN,
                request(&h, "hub:admin", "workspace-a", &format!("r{i}"), "support")
            )
            .await
            .status,
            201
        );
    }
    let count = h.events();
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            request(&h, "hub:admin", "workspace-b", "overflow", "support")
        )
        .await
        .status,
        409
    );
    assert_eq!(count, h.events());
    let purge = json!({"action":"evidence.purge","evidence_id":"hub:admin/workspace-a/r0","client_event_id":h.id()});
    assert_eq!(h.call(AGENT_TOKEN, purge.clone()).await.status, 403);
    let r = h.call(ADMIN_TOKEN, purge.clone()).await;
    assert_eq!(r.status, 200);
    assert_eq!(h.call(ADMIN_TOKEN, purge).await, r);
    let db = Connection::open(h.memory()).unwrap();
    let (tombstone,bytes):(i64,Vec<u8>)=db.query_row("SELECT tombstoned,artifact_bytes FROM memory_evidence WHERE evidence_id='hub:admin/workspace-a/r0'",[],|r|Ok((r.get(0)?,r.get(1)?))).unwrap();
    assert_eq!(tombstone, 1);
    assert!(bytes.is_empty());
    let columns: Vec<(String, String)> = db
        .prepare("PRAGMA table_info(memory_evidence)")
        .unwrap()
        .query_map([], |r| Ok((r.get(1)?, r.get(2)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    for (column, kind) in columns {
        let sql = match column.as_str() {
            "tombstoned" => "UPDATE memory_evidence SET tombstoned=0 WHERE tombstoned=1".into(),
            "artifact_bytes" => {
                "UPDATE memory_evidence SET artifact_bytes=x'61' WHERE tombstoned=1".into()
            }
            _ => {
                let value = if kind == "INTEGER" {
                    format!("COALESCE({column},0)+1")
                } else {
                    format!("COALESCE({column},'')||'_changed'")
                };
                format!(
                    "UPDATE memory_evidence SET tombstoned=1,artifact_bytes=x'',{column}={value} WHERE tombstoned=0"
                )
            }
        };
        let error = db.execute(&sql, []).unwrap_err().to_string();
        assert!(
            error.contains("memory_evidence is immutable"),
            "{column}: {error}"
        );
    }
    assert!(db.execute("DELETE FROM memory_evidence", []).is_err());
    let n: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_events WHERE kind='evidence.purge'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1);
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            request(&h, "hub:admin", "workspace-b", "recovered", "support")
        )
        .await
        .status,
        201
    );
}
#[tokio::test]
async fn workspace_cap_is_enforced_at_attach_and_purge_removes_proof_and_holds() {
    let (h, id) = ready().await;
    let db = Connection::open(h.memory()).unwrap();
    for i in 3..64 {
        db.execute("INSERT INTO memory_evidence(evidence_id,scope,card_id,workspace_id,principal,credential_id,credential_class,artifact_kind,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,created_ms) SELECT ?1,scope,card_id,workspace_id,'legacy-principal',credential_id,credential_class,artifact_kind,artifact_sha256,artifact_bytes,observed_ms,stance,scan_state,created_ms FROM memory_evidence LIMIT 1",[format!("legacy/workspace-a/r{i}")]).unwrap();
    }
    assert_eq!(
        h.call(
            AGENT_TOKEN,
            request(&h, "agent:dot", "workspace-a", "overflow", "support")
        )
        .await
        .status,
        409
    );
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            json!({"action":"evidence.purge","evidence_id":REFS[0],"client_event_id":h.id()})
        )
        .await
        .status,
        200
    );
    assert_eq!(
        h.call(
            AGENT_TOKEN,
            request(&h, "agent:dot", "workspace-a", "next", "support")
        )
        .await
        .status,
        201
    );
    assert_eq!(report(&h, &id).await.body["proof_evidence_count"], 2);
}
#[tokio::test]
async fn only_new_ids_on_changed_cards_count_and_compensations_are_refused() {
    let (h, id) = ready().await;
    assert_eq!(decide(&h, ADMIN_TOKEN, &id).await.body["state"], "applied");
    let next = "node-1/workspace-a/four";
    assert_eq!(attach(&h, NODE_TOKEN, next).await.status, 201);
    let db = Connection::open(h.memory()).unwrap();
    let text: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let mut op: Json = serde_json::from_str::<Json>(&text).unwrap()["ops"][0].clone();
    op["base_revision"] = card(&h, "fleet", "a").await["revision"].clone();
    op["evidence_ids"] = json!([next]);
    let p=h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[op],"client_event_id":h.id()})).await;
    assert_eq!(p.status, 201);
    let pid = &p.body["proposal_id"];
    assert_eq!(report(&h, pid).await.body["proof_evidence_count"], 1);
    mutate_body(&h, pid, |b| {
        b["cards"][0]["after_text"] = b["cards"][0]["before_text"].clone()
    });
    assert_eq!(report(&h, pid).await.body["proof_evidence_count"], 0);
    mutate_body(&h, pid, |b| b["rollback_of"] = id.clone());
    assert_eq!(
        h.call(ADMIN_TOKEN, auto_request(&h, pid).await)
            .await
            .status,
        409
    );
}
#[tokio::test]
async fn switch_event_has_its_own_kind() {
    let h = Hub::with(clean(), None);
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()})
        )
        .await
        .status,
        200
    );
    let n: i64 = Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT count(*) FROM memory_events WHERE kind='memory.auto_apply_switch'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(n, 1);
}

#[tokio::test]
async fn source_groups_are_principals_and_exclude_the_proposer() {
    let h = Hub::with(clean(), None);
    policy(&h);
    grant(&h, "fleet", &["read", "reflect"]).await;
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let alias_bearer = "fake-alias-node-token";
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "INSERT INTO nodes VALUES('agent:dot',?1,NULL,'created',NULL)",
            [digest(alias_bearer)],
        )
        .unwrap();
    for (token, id) in [
        (ADMIN_TOKEN, "hub:admin/workspace-a/one"),
        (AGENT_TOKEN, "agent:dot/workspace-a/two"),
        (alias_bearer, "agent:dot/workspace-a/three"),
    ] {
        assert_eq!(attach(&h, token, id).await.status, 201);
    }
    let p = proposal(
        &h,
        card(&h, "fleet", "a").await["revision"].as_i64().unwrap(),
        json!([
            "hub:admin/workspace-a/one",
            "agent:dot/workspace-a/two",
            "agent:dot/workspace-a/three"
        ]),
    )
    .await;
    let r = report(&h, &p.body["proposal_id"]).await;
    assert_eq!(r.body["proof_evidence_count"], 3);
    assert_eq!(r.body["proof_source_group_count"], 1);
}
#[tokio::test]
async fn cheap_filters_do_not_fetch_expired_artifact_bytes() {
    use crate::writer::ReadTx;
    use rusqlite::types::Value;
    struct Measured<'a> {
        tx: crate::writer::worker::Tx<'a>,
        bytes: std::cell::Cell<usize>,
    }
    impl ReadTx for Measured<'_> {
        fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
            let rows = self.tx.query(sql, params)?;
            let count = rows
                .iter()
                .flatten()
                .map(|v| if let Value::Blob(b) = v { b.len() } else { 0 })
                .sum::<usize>();
            self.bytes.set(self.bytes.get() + count);
            Ok(rows)
        }
    }
    let (h, id) = ready().await;
    h.set_clock(h.now() + 86_401_000);
    let db = Connection::open(h.memory()).unwrap();
    let hub = Connection::open(&h.hub).unwrap();
    let authority =
        ops::Authority::with_credentials(&crate::writer::worker::Tx(&hub), &digest(ADMIN_TOKEN))
            .unwrap();
    let body = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    let mut counts = Vec::new();
    for _ in 0..2 {
        let measured = Measured {
            tx: crate::writer::worker::Tx(&db),
            bytes: std::cell::Cell::new(0),
        };
        let proof =
            attestation::resolve(&measured, &body, &authority, clean().as_ref(), h.now()).unwrap();
        assert!(proof.hashes.is_empty());
        counts.push(measured.bytes.get());
    }
    eprintln!("Expired-proof artifact bytes fetched in two runs: {counts:?}");
    assert_eq!(counts, vec![0, 0]);
}

#[test]
fn version_five_upgrade_preserves_rows_epoch_sequence_and_immutability() {
    let db = Connection::open_in_memory().unwrap();
    let ddl = schema::ddl()
        .replace(attestation::DDL, &attestation::legacy_ddl())
        .replace(",'memory.auto_apply_switch','evidence.purge'", "");
    db.execute_batch(&ddl).unwrap();
    assert!(
        db.prepare("SELECT tombstoned FROM memory_evidence")
            .is_err()
    );
    db.execute_batch("INSERT INTO memory_meta VALUES('log_epoch','legacy-epoch'); INSERT INTO memory_events(event_id,client_event_id,kind,principal,payload,created_at) VALUES('legacy-event','legacy-client','recall.capture','legacy','{}','created'); UPDATE sqlite_sequence SET seq=500 WHERE name='memory_events';").unwrap();
    db.execute("INSERT INTO memory_evidence VALUES('legacy/workspace-a/one','fleet','a','workspace-a','legacy','credential','node','verification',?1,?2,0,'support','clean',0)",rusqlite::params![ids::sha256_hex(b"legacy"),b"legacy".as_slice()]).unwrap();
    db.pragma_update(None, "application_id", schema::APPLICATION_ID)
        .unwrap();
    db.execute_batch("DROP TABLE memory_retrieval_evals")
        .unwrap();
    db.pragma_update(None, "user_version", 5).unwrap();
    schema::migrate(&db, 0).unwrap();
    assert_eq!(schema::log_epoch(&db).unwrap(), "legacy-epoch");
    assert_eq!(
        db.query_row("SELECT tombstoned FROM memory_evidence", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        db.query_row("SELECT artifact_bytes FROM memory_evidence", [], |r| r
            .get::<_, Vec<u8>>(0))
            .unwrap(),
        b"legacy"
    );
    assert_eq!(
        db.query_row("SELECT seq FROM memory_events", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    db.execute("INSERT INTO memory_events(event_id,client_event_id,kind,principal,payload,created_at) VALUES('switch-event','switch-client','memory.auto_apply_switch','admin','{}','created')",[]).unwrap();
    assert_eq!(db.last_insert_rowid(), 501);
    assert!(
        db.execute("UPDATE memory_events SET principal='changed'", [])
            .is_err()
    );
    assert!(db.execute("DELETE FROM memory_events", []).is_err());
    assert!(
        db.execute("UPDATE memory_evidence SET observed_ms=1", [])
            .is_err()
    );
    assert!(db.execute("DELETE FROM memory_evidence", []).is_err());
    schema::migrate(&db, 0).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        schema::USER_VERSION
    );
}

#[tokio::test]
async fn automatic_reply_requires_review_on_every_target_scope() {
    let (h, id) = ready().await;
    grant(&h, "fleet", &["read", "reflect", "review"]).await;
    let b = h
        .put(ADMIN_TOKEN, "collection:review-source", "b", None, OLD)
        .await;
    grant(&h, "collection:review-source", &["read", "reflect"]).await;
    let db = Connection::open(h.memory()).unwrap();
    let body = proposals::load(&crate::writer::worker::Tx(&db), id.as_str().unwrap())
        .unwrap()
        .unwrap();
    let p=h.call(ADMIN_TOKEN,json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[body["ops"][0],op("collection:review-source","b",rev(&b))],"client_event_id":h.id()})).await;
    assert_eq!(p.status, 201);
    let r = h
        .call(AGENT_TOKEN, auto_request(&h, &p.body["proposal_id"]).await)
        .await;
    assert_eq!(r.status, 200);
    redacted(&r.body["preflight"]);
    assert!(r.body["diff"].is_null());
    grant(
        &h,
        "collection:review-source",
        &["read", "reflect", "review"],
    )
    .await;
    let reviewed = h
        .call(AGENT_TOKEN, auto_request(&h, &p.body["proposal_id"]).await)
        .await;
    assert!(reviewed.body["preflight"]["candidate_sha256"].is_string());
    assert!(reviewed.body["diff"].is_string());
}
