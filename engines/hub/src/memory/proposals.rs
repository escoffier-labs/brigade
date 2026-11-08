//! Reviewed hub proposals (S19a). Preparation is read-only, before writer locks.
//! Every transition re-authorizes every target scope under the authority lock.
use super::{
    Reply, auth, ids,
    ops::{self, Authority, NewEvent, Prepared, error, s, t},
    proposal_blocks as blocks,
    scan::{Scan, Scanner},
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::collections::BTreeMap;

pub const DDL: &str = "CREATE TABLE memory_proposals(
 proposal_id TEXT PRIMARY KEY,
 state TEXT NOT NULL CHECK(state IN ('pending','applied','rejected','stale','held')),
 body TEXT NOT NULL,
 principal TEXT NOT NULL,
 created_at TEXT NOT NULL,
 decided_by TEXT
) WITHOUT ROWID;";
const MAX_OPS: usize = 64;
const MAX_CARDS: usize = 8;

#[derive(Clone, Debug)]
pub struct Command {
    pub request: Json,
    pub draft: Result<Json, Reply>,
}
impl Command {
    pub fn name(&self) -> &'static str {
        match self.request["action"].as_str() {
            Some("propose") => "propose",
            Some("decide") => "decide",
            Some("auto_apply") => "auto_apply",
            _ => "rollback",
        }
    }
    pub fn right(&self) -> &'static str {
        if matches!(self.name(), "propose" | "auto_apply") {
            "reflect"
        } else {
            "review"
        }
    }
    pub fn new_bytes(&self) -> usize {
        self.draft
            .as_ref()
            .ok()
            .and_then(|d| d["cards"].as_array())
            .map(|cards| {
                cards
                    .iter()
                    .map(|c| c["after_text"].as_str().map(str::len).unwrap_or(0))
                    .sum()
            })
            .unwrap_or(0)
    }
}
fn invalid(reason: &str) -> Reply {
    let mut reply = error(422, "memory_proposal_invalid", reason);
    reply.body["reason"] = json!(reason);
    reply.body["ops"] = json!([]);
    reply
}
fn missing() -> Reply {
    error(404, "memory_proposal_not_found", "proposal not found")
}
pub(super) fn load<T: ReadTx + ?Sized>(tx: &T, id: &str) -> rusqlite::Result<Option<Json>> {
    let rows = tx.query(
        "SELECT state,body,principal FROM memory_proposals WHERE proposal_id=?1",
        &[t(id)],
    )?;
    Ok(rows.first().map(|r| {
        let mut body: Json = serde_json::from_str(&s(&r[1])).unwrap_or(Json::Null);
        body["state"] = json!(s(&r[0]));
        body["proposer"] = json!(s(&r[2]));
        body
    }))
}
pub(super) fn cards(body: &Json) -> &[Json] {
    body["cards"].as_array().map(Vec::as_slice).unwrap_or(&[])
}
pub(super) fn string<'a>(v: &'a Json, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn denied(right: &str) -> Reply {
    error(
        403,
        "memory_grant_missing",
        &format!("this credential needs read and {right} grants on every target scope"),
    )
}
pub(super) fn scoped_right<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    scope: &str,
    right: &str,
) -> rusqlite::Result<bool> {
    Ok(auth::has_right(tx, who, Some(scope), "read")?
        && auth::has_right(tx, who, Some(scope), right)?)
}
fn self_review(p: &Prepared, body: &Json) -> Option<Reply> {
    (p.credential != auth::Credential::Admin
        && string(body, "proposer") == p.credential.principal())
    .then(|| {
        error(
            403,
            "memory_self_review",
            "a proposer cannot accept their own proposal",
        )
    })
}
/// This check is also called before the idempotency lookup. Replays cannot
/// disclose a collection's stored diff after the caller loses its exact grant.
pub fn authorize<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    request: &Json,
) -> rusqlite::Result<Option<Reply>> {
    let right = match request["action"].as_str() {
        Some("propose") => "reflect",
        Some("preflight") => "read",
        Some("auto_apply") => "reflect",
        _ => "review",
    };
    if request["action"] == "propose" {
        if let Some(ops) = request["ops"].as_array() {
            for op in ops {
                for target in [op, &op["other_card"]] {
                    if let Some(scope) = target["scope"].as_str().filter(|s| ids::valid_scope(s)) {
                        if !scoped_right(tx, who, scope, right)? {
                            return Ok(Some(denied(right)));
                        }
                    }
                }
            }
        }
    } else {
        let Some(proposal) = load(tx, string(request, "proposal_id"))? else {
            return Ok(Some(missing()));
        };
        for card in cards(&proposal) {
            if !scoped_right(tx, who, string(card, "scope"), right)? {
                return Ok(Some(denied(right)));
            }
        }
    }
    Ok(None)
}
pub(super) fn op_schema(op: &Json) -> Result<(), &'static str> {
    let map = op.as_object().ok_or("op must be an object")?;
    let extras: &[&str] = match string(op, "type") {
        "replace_block" | "add_previously" => &["text"],
        "remove_block" => &[],
        "append_evidence" => &["evidence_ids"],
        "supersede_card" | "merge_exact_duplicates" => &["other_card"],
        _ => return Err("op is not a supported card-block operation"),
    };
    let common = [
        "type",
        "scope",
        "card_id",
        "base_revision",
        "block_id",
        "expected_block_hash",
    ];
    if map
        .keys()
        .any(|k| k != "tier" && !common.contains(&k.as_str()) && !extras.contains(&k.as_str()))
        || common.iter().chain(extras).any(|k| !map.contains_key(*k))
    {
        return Err("invalid typed op fields");
    }
    if !ids::valid_scope(string(op, "scope")) || !ids::valid_card_id(string(op, "card_id")) {
        return Err("op must address a memory card in a valid scope");
    }
    if let Some(other) = op.get("other_card") {
        if !other.as_object().is_some_and(|o| {
            o.len() == 3
                && o.keys()
                    .all(|k| ["scope", "card_id", "base_revision"].contains(&k.as_str()))
        }) || !ids::valid_scope(string(other, "scope"))
            || !ids::valid_card_id(string(other, "card_id"))
            || other["base_revision"].as_i64().is_none_or(|r| r <= 0)
        {
            return Err("other_card must name scope, card_id and positive base_revision");
        }
        if other["scope"] == op["scope"] && other["card_id"] == op["card_id"] {
            return Err("other_card must name a different card");
        }
    }
    if op["base_revision"].as_i64().is_none_or(|r| r <= 0) {
        return Err("positive base_revision required");
    }
    Ok(())
}
fn bytes<T: ReadTx + ?Sized>(tx: &T, hash: &str) -> rusqlite::Result<String> {
    let rows = tx.query(
        "SELECT bytes FROM memory_blobs WHERE content_hash=?1",
        &[t(hash)],
    )?;
    Ok(rows
        .first()
        .map(|r| match &r[0] {
            Value::Blob(b) => String::from_utf8_lossy(b).into_owned(),
            other => s(other),
        })
        .unwrap_or_default())
}
fn scan_refusal(scan: &Scan, raise: bool) -> Option<Reply> {
    match scan {
        Scan::Failed => Some(error(503, "memory_scan_failed", "input cannot be scanned")),
        Scan::Unavailable if raise => {
            Some(error(503, "memory_scan_unavailable", "scan unavailable"))
        }
        Scan::Blocked { rules, .. } => {
            let mut r = error(
                422,
                "memory_content_blocked",
                "content blocked by a scan rule",
            );
            r.body["rules"] = json!(rules);
            Some(r)
        }
        _ => None,
    }
}
fn snapshot<T: ReadTx + ?Sized>(tx: &T, op: &Json) -> rusqlite::Result<Result<Json, String>> {
    let scope = string(op, "scope");
    let id = string(op, "card_id");
    let Some(h) = ops::head(tx, scope, id)? else {
        return Ok(Err("card not found".into()));
    };
    if h.state != "live" || op["base_revision"] != h.revision {
        return Ok(Err("card base revision is not the live head".into()));
    }
    let text = bytes(tx, &h.hash)?;
    let rows = tx.query("SELECT scan_state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision=?3", &[t(scope),t(id),Value::Integer(h.revision)])?;
    let scan = rows.first().map(|r| s(&r[0]));
    Ok(Ok(
        json!({"scope":scope,"card_id":id,"base_revision":h.revision,"before_hash":h.hash,"before_text":text,"before_trust":h.trust,"before_state":h.state,"before_scan":scan,"after_text":text,"after_state":"live","pins":[]}),
    ))
}
fn card_diff(card: &Json) -> String {
    let scope = string(card, "scope");
    let id = string(card, "card_id");
    let mut diff = blocks::diff(
        scope,
        id,
        string(card, "before_text"),
        string(card, "after_text"),
    );
    if card["before_state"] != card["after_state"] {
        diff += &format!(
            "--- a/{scope}/{id}/@state\n+++ b/{scope}/{id}/@state\n@@ -1 +1 @@\n-{}\n+{}\n",
            string(card, "before_state"),
            string(card, "after_state")
        );
    }
    diff
}
/// Read-only dry run of every op. Errors carry one indexed result per op,
/// including those that validated. No partial proposal is retained.
fn submit<T: ReadTx + ?Sized>(
    tx: &T,
    request: &Json,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Result<Json, Reply>> {
    let Some(ops) = request["ops"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= MAX_OPS)
    else {
        return Ok(Err(invalid(
            "ops must contain 1 to 64 card-block operations",
        )));
    };
    let epoch = ops::epoch(tx)?;
    if string(request, "base_epoch") != epoch {
        let mut r = error(
            409,
            "memory_epoch_changed",
            "the memory log was restored since this revision was read",
        );
        r.body["log_epoch"] = json!(epoch);
        return Ok(Err(r));
    }

    let mut by_card: BTreeMap<(String, String), Json> = BTreeMap::new();
    let mut results = Vec::new();
    for (index, op) in ops.iter().enumerate() {
        let outcome = (|| -> rusqlite::Result<Result<(), String>> {
            if let Err(r) = op_schema(op) {
                return Ok(Err(r.into()));
            }
            if string(request, "base_epoch") != epoch {
                return Ok(Err("base_epoch differs from log_epoch".into()));
            }
            let scope = string(op, "scope");
            let id = string(op, "card_id");
            let key = (scope.to_owned(), id.to_owned());
            for target in [Some(op), op.get("other_card")].into_iter().flatten() {
                let target_key = (
                    string(target, "scope").to_owned(),
                    string(target, "card_id").to_owned(),
                );
                if !by_card.contains_key(&target_key) {
                    if by_card.len() >= MAX_CARDS {
                        return Ok(Err("proposal exceeds eight cards".into()));
                    }
                    match snapshot(tx, target)? {
                        Ok(c) => {
                            by_card.insert(target_key.clone(), c);
                        }
                        Err(r) => return Ok(Err(r)),
                    }
                }
                let c = &by_card[&target_key];
                if c["base_revision"] != target["base_revision"] || c["after_state"] != "live" {
                    return Ok(Err("inconsistent or retired source card".into()));
                }
            }
            let other = op.get("other_card").map(|o| {
                let c = by_card[&(
                    string(o, "scope").to_owned(),
                    string(o, "card_id").to_owned(),
                )]
                    .clone();
                (
                    c,
                    format!("{}/{}", string(o, "scope"), string(o, "card_id")),
                )
            });
            let card = by_card.get_mut(&key).unwrap();
            if card["base_revision"] != op["base_revision"] {
                return Ok(Err("ops on a card must share a base revision".into()));
            }
            // Pins always name the original revision, even with several ops.
            let pin = match blocks::pin(
                string(card, "before_text"),
                string(op, "block_id"),
                string(op, "expected_block_hash"),
            ) {
                Ok(p) => p,
                Err(r) => return Ok(Err(r.into())),
            };
            let text = if matches!(
                string(op, "type"),
                "replace_block" | "remove_block" | "add_previously"
            ) {
                match blocks::apply(string(card, "after_text"), op) {
                    Ok((r, _)) => r,
                    Err(r) => return Ok(Err(r.into())),
                }
            } else {
                match super::proposal_metadata::apply(
                    string(card, "after_text"),
                    op,
                    other
                        .as_ref()
                        .map(|(c, n)| (string(c, "after_text"), n.as_str())),
                ) {
                    Ok(r) => r,
                    Err(r) => return Ok(Err(r.into())),
                }
            };
            if text.len() > ids::MAX_TEXT_BYTES {
                return Ok(Err("result exceeds card byte limit".into()));
            }
            // Scan the op's text separately and the complete resulting card.
            for value in [op["text"].as_str(), Some(text.as_str())]
                .into_iter()
                .flatten()
            {
                if let Some(r) = scan_refusal(&scanner.scan(value), false) {
                    return Ok(Err(format!("{}", r.body["code"])));
                }
            }
            if text == string(card, "after_text") && op["type"] != "merge_exact_duplicates" {
                return Ok(Err("op has no delta".into()));
            }
            card["after_text"] = json!(text);
            card["pins"].as_array_mut().unwrap().push(pin);
            if let Some(target) = op.get("other_card") {
                let c = by_card
                    .get_mut(&(
                        string(target, "scope").to_owned(),
                        string(target, "card_id").to_owned(),
                    ))
                    .unwrap();
                c["pins"] = json!(blocks::pins(string(c, "before_text")));
                if op["type"] == "merge_exact_duplicates" {
                    c["after_state"] = json!("retired");
                }
            }

            Ok(Ok(()))
        })()?;
        results.push(match outcome {
            Ok(()) => json!({"index":index,"valid":true}),
            Err(reason) => json!({"index":index,"valid":false,"reason":reason}),
        });
    }
    if results.iter().any(|r| r["valid"] == false) {
        let mut r = invalid("proposal refused because at least one op is invalid");
        r.body["ops"] = json!(results);
        return Ok(Err(r));
    }
    let mut diff = String::new();
    for card in by_card.values_mut() {
        card["after_hash"] = json!(ids::sha256_hex(string(card, "after_text").as_bytes()));
        diff += &card_diff(card);
    }
    // Tier claims are accepted as input but never retained for later readers.
    let ops: Vec<Json> = ops
        .iter()
        .cloned()
        .map(|mut op| {
            op.as_object_mut().unwrap().remove("tier");
            op
        })
        .collect();
    Ok(Ok(
        json!({"base_epoch":epoch,"workspace_id":request["workspace_id"],"cards":by_card.into_values().collect::<Vec<_>>(),"ops":ops,"diff":diff,"tier":2,"rollback_of":null}),
    ))
}
/// Prepare CPU work and diffs on a query-only reader. A refusal is carried to
/// the writer so a committed retry still reaches the idempotency lookup first.
pub fn prepare<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    request: Json,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Command> {
    let draft = if let Some(r) = authorize(tx, who, &request)? {
        Err(r)
    } else if request["action"] == "propose" {
        submit(tx, &request, scanner)?
    } else {
        match load(tx, string(&request, "proposal_id"))? {
            Some(body) => Ok(body),
            None => Err(missing()),
        }
    };
    Ok(Command { request, draft })
}
pub(super) fn stale_reason<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
) -> rusqlite::Result<Option<&'static str>> {
    if string(body, "base_epoch") != ops::epoch(tx)? {
        return Ok(Some("memory_epoch_changed"));
    }
    for card in cards(body) {
        let Some(h) = ops::head(tx, string(card, "scope"), string(card, "card_id"))? else {
            return Ok(Some("card_missing"));
        };
        if card["base_revision"] != h.revision {
            return Ok(Some("memory_base_mismatch"));
        }
        let text = bytes(tx, &h.hash)?;
        if string(card, "before_hash") != h.hash || ids::sha256_hex(text.as_bytes()) != h.hash {
            return Ok(Some("pinned_content_changed"));
        }
        for pin in card["pins"].as_array().unwrap() {
            match blocks::pin(&text, string(pin, "block_id"), string(pin, "hash")) {
                Ok(current) if current == *pin => (),
                _ => return Ok(Some("pinned_block_changed")),
            }
        }
    }
    Ok(None)
}
fn metadata(body: &Json) -> Json {
    json!({"proposal_id":body["proposal_id"],"decision_mode":body["decision_mode"],"auto_apply_gates":body["auto_apply_gates"],"rollback_of":body["rollback_of"],"tier":body["tier"],"policy_revision":body["policy_revision"],"policy_digest":body["policy_digest"],"decision_policy_revision":body["decision_policy_revision"],"decision_policy_digest":body["decision_policy_digest"],"state":body["state"],"decision":body["decision"],"reason":body["reason"],"rules":body["rules"],
        "cards":cards(body).iter().map(|c|json!({"scope":c["scope"],"card_id":c["card_id"],"base_revision":c["base_revision"],"before_hash":c["before_hash"],"after_hash":c["after_hash"],"before_state":c["before_state"],"after_state":c["after_state"],"pins":c["pins"].as_array().unwrap().iter().map(|p|json!({"block_id":p["block_id"],"hash":p["hash"]})).collect::<Vec<_>>()})).collect::<Vec<_>>()})
}
fn event(tx: &dyn WriteTx, p: &Prepared, kind: &str, body: &Json) -> rusqlite::Result<()> {
    for card in cards(body) {
        let mut visible = body.clone();
        visible["cards"] = json!([card]);
        ops::append(
            tx,
            p,
            NewEvent {
                kind,
                scope: Some(string(card, "scope")),
                card_id: Some(string(card, "card_id")),
                changes_head: false,
                base_revision: card["base_revision"].as_i64(),
                content_hash: None,
                trust: None,
                origin: None,
                payload: metadata(&visible),
                policy_digest: if kind == "proposal.decide" {
                    body["decision_policy_digest"].as_str()
                } else {
                    body["policy_digest"].as_str()
                },
            },
        )?;
    }
    Ok(())
}

pub(super) fn reply(body: &Json, status: u16) -> Reply {
    Reply::new(
        status,
        json!({"proposal_id":body["proposal_id"],"state":body["state"],"tier":body["tier"],"policy_revision":body["policy_revision"],"policy_digest":body["policy_digest"],"decision_policy_revision":body["decision_policy_revision"],"decision_policy_digest":body["decision_policy_digest"],"diff":body["diff"],"targets":metadata(body)["cards"],"reason":body["reason"],"rules":body["rules"],"preflight":body["preflight"],"log_epoch":body["base_epoch"]}),
    )
}
/// Automatic calls may be made by reflect-only callers, including replays.
pub(super) fn redact_auto_reply<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    proposal: &Json,
    response: &mut Json,
) -> rusqlite::Result<()> {
    if super::preflight::redact(tx, who, proposal, &mut response["preflight"])? {
        response["diff"] = Json::Null;
        if let Some(targets) = response["targets"].as_array_mut() {
            for target in targets {
                target["after_hash"] = Json::Null;
            }
        }
    }
    Ok(())
}
fn update(
    tx: &dyn WriteTx,
    body: &Json,
    p: &Prepared,
    authority: &Authority,
) -> rusqlite::Result<Option<Reply>> {
    if let Some(r) = super::hardening::quota(tx, body, &authority.proposal_limits, true)? {
        return Ok(Some(r));
    }
    tx.execute(
        "UPDATE memory_proposals SET state=?2,body=?3,decided_by=?4,retained_bytes=length(CAST(?3 AS BLOB)),terminal_ms=CASE WHEN ?2 IN ('applied','rejected','stale') THEN ?5 ELSE NULL END WHERE proposal_id=?1",
        &[
            t(string(body, "proposal_id")),
            t(string(body, "state")),
            t(&body.to_string()),
            if body["state"] == "pending" {
                Value::Null
            } else {
                t(p.credential.principal())
            },
            Value::Integer(p.now_ms),
        ],
    )?;
    Ok(None)
}
fn open(
    tx: &dyn WriteTx,
    p: &Prepared,
    mut body: Json,
    authority: &Authority,
    kind: &str,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    body["proposal_id"] = json!(ids::mint(p.now_ms));
    body["proposer"] = json!(p.credential.principal());
    body["proposer_credential"] = json!(super::attestation::identity(&p.credential, authority));
    body["rules"] = json!([]);
    body["decision_policy_revision"] = Json::Null;
    body["decision_policy_digest"] = Json::Null;
    body["tier"] = json!(super::proposal_tiers::classify(tx, &body, scanner)?);
    body["opened_tier"] = body["tier"].clone();
    if body["state"].is_null() {
        body["state"] = json!("pending");
    }
    body["policy_revision"] = json!(authority.policy.as_ref().map(|a| a.0));
    body["policy_digest"] = json!(authority.policy.as_ref().map(|a| &a.1));
    if !body["rollback_of"].is_null() {
        for card in body["cards"].as_array_mut().unwrap() {
            let rows = tx.query("SELECT scan_state FROM memory_card_revisions WHERE scope=?1 AND card_id=?2 AND revision=?3", &[t(string(card,"scope")),t(string(card,"card_id")),Value::Integer(card["base_revision"].as_i64().unwrap_or(0))])?;
            card["before_scan"] = json!(rows.first().map(|r| s(&r[0])));
        }
    }
    body["created_at"] = json!(ids::isoformat(p.now_ms));
    if let Some(refusal) = super::hardening::admit(tx, &body, &authority.proposal_limits)? {
        return Ok(refusal);
    }
    tx.execute("INSERT INTO memory_proposals(proposal_id,state,body,principal,created_at,digest,retained_bytes,terminal_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",&[t(string(&body,"proposal_id")),t(string(&body,"state")),t(&body.to_string()),t(p.credential.principal()),t(&ids::isoformat(p.now_ms)),t(&super::hardening::digest(&body)),Value::Integer(body.to_string().len() as i64),if body["state"]=="stale" {Value::Integer(p.now_ms)} else {Value::Null}])?;
    for card in cards(&body) {
        tx.execute(
            "INSERT OR IGNORE INTO memory_proposal_scopes VALUES(?1,?2)",
            &[t(string(&body, "proposal_id")), t(string(card, "scope"))],
        )?;
    }
    event(tx, p, kind, &body)?;
    Ok(reply(&body, 201))
}
/// Atomic reviewed apply. Authorization is deliberately local to this boundary
/// as well as `decide`, including rollback compensations and every collection.
fn apply_cards(
    tx: &dyn WriteTx,
    p: &Prepared,
    body: &mut Json,
    authority: &Authority,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Option<Reply>> {
    let automatic = matches!(&p.action,ops::Action::Proposal(c) if c.name()=="auto_apply");
    for card in cards(body) {
        if !scoped_right(
            tx,
            &p.credential,
            string(card, "scope"),
            if automatic { "reflect" } else { "review" },
        )? {
            return Ok(Some(denied("review")));
        }
    }
    if !automatic {
        if let Some(r) = self_review(p, body) {
            return Ok(Some(r));
        }
        // Reviewed calls still require explicit acceptance. Only the automatic
        // action can enter the separate all-gates boundary below.
        if !matches!(&p.action, ops::Action::Proposal(cmd) if cmd.name() == "decide" && cmd.request["decision"] == "accept")
        {
            return Ok(Some(error(
                403,
                "memory_review_required",
                "an explicit reviewer decision is required",
            )));
        }
    }
    if !automatic {
        body["tier"] = json!(super::proposal_tiers::classify(tx, body, scanner)?);
    }
    // Keep diagnostics before blocker returns. Reusing the later apply-loop
    // scans would move scan work before those checks or need a second report path.
    if automatic {
        let snapshot = match &p.action {
            ops::Action::Proposal(c) => c.draft.as_ref().ok().map(|b| &b["_auto_snapshot"]),
            _ => None,
        };
        let snapshot = snapshot.cloned().unwrap_or(Json::Null);
        let proposal_digest = ids::sha256_hex(ids::canonical(body).as_bytes());
        body["preflight"] = snapshot["report"].clone();
        let current_token = super::hardening::token(tx)?;
        if let Some(reason) = super::hardening::cheap(tx, body, authority)? {
            body["preflight"] = super::hardening::held(body, reason);
        } else if snapshot["proposal_sha256"] != proposal_digest
            || snapshot["token"] != current_token
            || snapshot["authority"] != super::hardening::authority_digest(authority)
            || snapshot["now_ms"]
                .as_i64()
                .is_none_or(|ms| p.now_ms < ms || p.now_ms - ms > 1000)
            || (super::preflight::all_satisfied(&body["preflight"])
                && body["preflight"]["valid_until_ms"]
                    .as_i64()
                    .is_none_or(|ms| p.now_ms > ms))
        {
            body["preflight"] = if snapshot["token"]["memory_vector_log"]
                != current_token["memory_vector_log"]
            {
                json!({"checks":[{"gate":"retrieval_eval","code":"retrieval_eval_vector_changed","state":"held"}]})
            } else {
                json!({"checks":[{"gate":"snapshot","code":"auto_apply_snapshot_changed","state":"held"}]})
            };
        }
    } else {
        body["preflight"] = super::preflight::evaluate(tx, body, authority, scanner, p.now_ms)?;
    }
    body["rules"] = json!([]);
    if automatic {
        body["auto_apply_gates"] = body["preflight"]["checks"].clone();
    }
    if automatic && !super::preflight::all_satisfied(&body["preflight"]) {
        // A failed automatic attempt leaves human review available.
        body["state"] = json!("pending");
        body["reason"] = json!("auto_apply_held");
        return Ok(None);
    }
    if automatic {
        body["tier"] = json!(super::proposal_tiers::classify(tx, body, scanner)?);
    }
    if body["policy_revision"] != json!(authority.policy.as_ref().map(|a| a.0))
        || body["policy_digest"] != json!(authority.policy.as_ref().map(|a| &a.1))
    {
        body["state"] = json!("stale");
        body["reason"] = json!("policy_changed");
        return Ok(None);
    }
    if let Some(reason) = stale_reason(tx, body)? {
        body["state"] = json!("stale");
        body["reason"] = json!(reason);
        return Ok(None);
    }
    if let Some(reason) = super::preflight::input_blocker(tx, body)? {
        body["state"] = json!("held");
        body["reason"] = json!(reason);
        return Ok(None);
    }
    // All blockers and scans are checked before the first head changes.
    let mut scans = Vec::new();
    for card in cards(body) {
        let scan = scanner.scan(string(card, "after_text"));
        if automatic && !matches!(&scan,Scan::Clean{ruleset} if !ruleset.is_empty()) {
            body["state"] = json!("pending");
            body["reason"] = json!("scan_not_clean");
            return Ok(None);
        }
        if matches!(scan, Scan::Blocked { .. } | Scan::Failed) {
            body["rules"] = json!(scan.rules());
            body["state"] = json!("held");
            body["reason"] = json!(if matches!(scan, Scan::Failed) {
                "memory_scan_failed"
            } else {
                "memory_content_blocked"
            });
            return Ok(None);
        }
        if let Some(r) = scan_refusal(&scan, true) {
            return Ok(Some(r));
        }
        if !scan.permits_raise() {
            body["state"] = json!("held");
            body["reason"] = json!("memory_scan_flagged");
            body["rules"] = json!(scan.rules());
            return Ok(None);
        }
        scans.push(scan);
    }
    let id = string(body, "proposal_id").to_string();
    let rollback = !body["rollback_of"].is_null();
    let digest = body["policy_digest"].as_str().map(String::from);
    let body_audit = json!({"checks":body["preflight"]["checks"],"retrieval_eval_record":body["preflight"]["retrieval_eval_record"],"policy_digest":body["policy_digest"],"attesting_credentials":body["preflight"]["attesting_credentials"]});
    for (card, scan) in body["cards"].as_array_mut().unwrap().iter_mut().zip(scans) {
        let scope = string(card, "scope");
        let card_id = string(card, "card_id");
        let hash = string(card, "after_hash");
        let trust = if rollback {
            string(card, "restore_trust")
        } else if automatic {
            string(card, "before_trust")
        } else {
            "reviewed"
        };
        if card["before_text"] == card["after_text"] && card["before_state"] == card["after_state"]
        {
            continue;
        }
        ops::store_blob(
            tx,
            hash,
            string(card, "after_text"),
            &ids::isoformat(p.now_ms),
        )?;
        ops::store_scan(tx, hash, &scan, &ids::isoformat(p.now_ms))?;
        let seq = ops::append(
            tx,
            p,
            NewEvent {
                kind: if rollback {
                    "proposal.rollback"
                } else {
                    "proposal.apply"
                },
                scope: Some(scope),
                card_id: Some(card_id),
                changes_head: true,
                base_revision: card["base_revision"].as_i64(),
                content_hash: Some(hash),
                trust: Some(trust),
                origin: None,
                payload: if automatic {
                    json!({"proposal_id":id,"decision_mode":"automatic","gates":body_audit.clone()})
                } else {
                    json!({"proposal_id":id})
                },
                policy_digest: digest.as_deref(),
            },
        )?;
        ops::set_head(
            tx,
            p,
            scope,
            card_id,
            seq,
            hash,
            string(card, "after_state"),
            trust,
            scan.state(),
            Some(string(card, "after_text")),
        )?;
        tx.execute("UPDATE memory_card_revisions SET proposal_id=?1 WHERE scope=?2 AND card_id=?3 AND revision=?4",&[t(&id),t(scope),t(card_id),Value::Integer(seq)])?;
        let applied_trust = trust.to_string();
        card["applied_revision"] = json!(seq);
        card["applied_trust"] = json!(applied_trust);
    }
    body["state"] = json!("applied");
    body["reason"] = Json::Null;
    Ok(None)
}
fn rollback(body: &Json) -> Json {
    let mut inverse = body.clone();
    inverse["rollback_of"] = body["proposal_id"].clone();
    inverse["state"] = Json::Null;
    inverse["reason"] = Json::Null;
    inverse["decision"] = Json::Null;
    inverse["decision_mode"] = Json::Null;
    inverse["auto_apply_gates"] = Json::Null;
    inverse["preflight"] = Json::Null;
    inverse["rules"] = json!([]);
    inverse["cards"]
        .as_array_mut()
        .unwrap()
        .retain(|c| c["applied_revision"].is_i64());
    let mut diff = String::new();
    for card in inverse["cards"].as_array_mut().unwrap() {
        let old = card.clone();
        card["base_revision"] = old["applied_revision"].clone();
        card["before_hash"] = old["after_hash"].clone();
        card["before_text"] = old["after_text"].clone();
        card["before_state"] = old["after_state"].clone();
        card["before_trust"] = json!(if let Some(trust) = old["applied_trust"].as_str() {
            trust
        } else if body["rollback_of"].is_null() {
            "reviewed"
        } else {
            string(&old, "restore_trust")
        });
        card["restore_trust"] = old["before_trust"].clone();
        card["after_hash"] = old["before_hash"].clone();
        card["after_text"] = old["before_text"].clone();
        card["after_state"] = old["before_state"].clone();
        // The exact whole-version hash pins even a removed block. Retained
        // anchors are separately checked when present in the applied bytes.
        card["pins"] = json!(blocks::pins(string(&old, "after_text")));
        diff += &card_diff(card);
        card.as_object_mut().unwrap().remove("applied_revision");
    }
    inverse["diff"] = json!(diff);
    inverse["ops"] = json!([]);
    inverse
}

pub fn run(
    tx: &dyn WriteTx,
    p: &Prepared,
    cmd: &Command,
    authority: &Authority,
    scanner: &dyn Scanner,
) -> rusqlite::Result<Reply> {
    if let Some(r) = authorize(tx, &p.credential, &cmd.request)? {
        return Ok(r);
    }
    let mut body = match &cmd.draft {
        Ok(b) => b.clone(),
        Err(r) => return Ok(r.clone()),
    };
    if cmd.name() == "propose" {
        if let Some(reason) = stale_reason(tx, &body)? {
            let mut r = invalid("proposal base changed during validation");
            r.body["ops"] = json!(
                cmd.request["ops"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .enumerate()
                    .map(|(index, _)| json!({"index":index,"valid":false,"reason":reason}))
                    .collect::<Vec<_>>()
            );
            return Ok(r);
        }
        // Repeat the write-time scan under the same boundary used by put.
        for card in cards(&body) {
            if let Some(r) = scan_refusal(&scanner.scan(string(card, "after_text")), false) {
                return Ok(r);
            }
        }
        return open(tx, p, body, authority, "proposal.open", scanner);
    }
    body = match load(tx, string(&cmd.request, "proposal_id"))? {
        Some(b) => b,
        None => return Ok(missing()),
    };
    if string(&cmd.request, "base_epoch") != ops::epoch(tx)? {
        let mut r = error(
            409,
            "memory_epoch_changed",
            "the memory log was restored since this revision was read",
        );
        r.body["log_epoch"] = json!(ops::epoch(tx)?);
        return Ok(r);
    }
    if body["compacted"] == true {
        return Ok(error(
            409,
            "memory_proposal_compacted",
            "proposal body retention expired, immutable audit retained",
        ));
    }
    if cmd.name() == "rollback" {
        if body["state"] != "applied" {
            return Ok(error(
                409,
                "memory_proposal_state",
                "only an applied proposal can be rolled back",
            ));
        }
        let mut inverse = rollback(&body);
        if let Some(reason) = stale_reason(tx, &inverse)? {
            inverse["state"] = json!("stale");
            inverse["reason"] = json!(reason);
        }
        for card in cards(&inverse) {
            if let Some(r) = scan_refusal(&scanner.scan(string(card, "after_text")), false) {
                return Ok(r);
            }
        }
        return open(tx, p, inverse, authority, "proposal.rollback", scanner);
    }
    if cmd.name() == "auto_apply" {
        if !body["rollback_of"].is_null() {
            return Ok(error(
                409,
                "memory_rollback_review_required",
                "compensating proposals require a reviewed decision",
            ));
        }
        if body["state"] != "pending" {
            return Ok(error(
                409,
                "memory_proposal_state",
                "automatic apply requires a pending proposal",
            ));
        }
        if let Some(r) = apply_cards(tx, p, &mut body, authority, scanner)? {
            return Ok(r);
        }
        if body["state"] == "applied" {
            body["decision"] = json!("auto_apply");
            body["decision_mode"] = json!("automatic");
            body["decision_policy_revision"] = json!(authority.policy.as_ref().map(|a| a.0));
            body["decision_policy_digest"] = json!(authority.policy.as_ref().map(|a| &a.1));
            if let Some(r) = update(tx, &body, p, authority)? {
                return Ok(r);
            }
            event(tx, p, "proposal.decide", &body)?;
        }
        // An unsuccessful attempt never writes a proposal transition. Report
        // the persisted pending state even if a late check changed the local body.
        if body["state"] != "applied" {
            body["state"] = json!("pending");
        }
        let mut response = reply(&body, 200);
        redact_auto_reply(tx, &p.credential, &body, &mut response.body)?;
        return Ok(response);
    }
    let redecide = body["state"] == "stale" && body["reason"] == "policy_changed";
    if !matches!(string(&body, "state"), "pending" | "held") && !redecide {
        return Ok(error(
            409,
            "memory_proposal_state",
            "proposal already decided",
        ));
    }
    if cmd.request["decision"] == "accept" {
        if let Some(r) = self_review(p, &body) {
            return Ok(r);
        }
    }
    let policy = authority.policy.as_ref();
    let current_digest = json!(policy.map(|a| &a.1));
    let expected = cmd.request.get("expected_policy_digest");
    if (redecide && expected.is_none()) || expected.is_some_and(|v| *v != current_digest) {
        let mut r = error(
            409,
            "memory_policy_digest_mismatch",
            "expected_policy_digest must match the current policy and is required for a policy-stale decision",
        );
        r.body["policy_digest"] = current_digest;
        r.body["policy_revision"] = json!(policy.map(|a| a.0));
        return Ok(r);
    }
    body["tier"] = json!(super::proposal_tiers::classify(tx, &body, scanner)?);
    body["decision_policy_revision"] = json!(policy.map(|a| a.0));
    body["decision_policy_digest"] = json!(policy.map(|a| &a.1));
    if redecide {
        // A fresh explicit request must acknowledge the current policy digest.
        // Idempotent replay has already returned before reaching this code.
        body["policy_revision"] = body["decision_policy_revision"].clone();
        body["policy_digest"] = body["decision_policy_digest"].clone();
    }
    if cmd.request["decision"] == "reject" {
        body["state"] = json!("rejected");
        body["reason"] = Json::Null;
        body["rules"] = json!([]);
    } else if body["policy_revision"] != json!(policy.map(|a| a.0))
        || body["policy_digest"] != json!(policy.map(|a| &a.1))
    {
        body["state"] = json!("stale");
        body["reason"] = json!("policy_changed");
    } else if let Some(r) = apply_cards(tx, p, &mut body, authority, scanner)? {
        return Ok(r);
    }
    body["decision"] = cmd.request["decision"].clone();
    if let Some(r) = update(tx, &body, p, authority)? {
        return Ok(r);
    }
    event(tx, p, "proposal.decide", &body)?;
    Ok(reply(&body, 200))
}

#[cfg(test)]
mod tests {
    use super::super::{ops::Action, tests::*};
    use super::*;
    #[tokio::test]
    async fn tier1_mutation_guard_requires_explicit_decide_at_apply_boundary() {
        let h = Hub::with(super::super::proposal_tests::clean(), None);
        let a = h
            .put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                None,
                super::super::proposal_tests::OLD,
            )
            .await;
        let mut operation = super::super::proposal_tests::op("fleet", "a", rev(&a));
        operation["text"] = json!("Use  the focused gate.");
        let opened = super::super::proposal_tests::submit(&h, ADMIN_TOKEN, vec![operation]).await;
        assert_eq!(opened.body["tier"], 1);
        let db = rusqlite::Connection::open(h.memory()).unwrap();
        let tx = crate::writer::worker::Tx(&db);
        let mut body = load(&tx, opened.body["proposal_id"].as_str().unwrap())
            .unwrap()
            .unwrap();
        // A mutant that omits the explicit-decision guard would apply this
        // cosmetic Tier 1 edit with no calibration and no reviewed decision.
        let p = Prepared {
            credential: auth::Credential::Admin,
            action: Action::Proposal(Command {
                request: json!({"action":"propose"}),
                draft: Ok(body.clone()),
            }),
            client_event_id: h.id(),
            request_digest: String::new(),
            now_ms: h.now(),
        };
        let r = apply_cards(
            &tx,
            &p,
            &mut body,
            &Authority::read(&crate::writer::worker::Tx(
                &rusqlite::Connection::open(&h.hub).unwrap(),
            ))
            .unwrap(),
            super::super::proposal_tests::clean().as_ref(),
        )
        .unwrap();
        assert_eq!(r.as_ref().map(|r| r.status), Some(403));
        assert_eq!(body["state"], "pending");
        assert_eq!(
            ops::head(&tx, "fleet", "a").unwrap().unwrap().revision,
            rev(&a)
        );
    }
    #[tokio::test]
    async fn apply_boundary_requires_read_and_review_on_each_target_scope() {
        for scope in ["repo:restricted", "collection:notes"] {
            for right in ["read", "review"] {
                let h = Hub::with(
                    std::sync::Arc::new(Fixed(Scan::Clean {
                        ruleset: "test-v7".into(),
                    })),
                    None,
                );
                let a = h
                    .put(ADMIN_TOKEN, scope, "a", None, "Claim. ^c-abcdef")
                    .await;
                let opened=h.call(ADMIN_TOKEN,json!({"action":"propose","base_epoch":h.epoch().await,"client_event_id":h.id(),"ops":[{"type":"remove_block","scope":scope,"card_id":"a","base_revision":rev(&a),"block_id":"c-abcdef","expected_block_hash":ids::sha256_hex(b"Claim. ^c-abcdef")}]})).await;
                assert_eq!(opened.status, 201, "{opened:?}");
                let db = rusqlite::Connection::open(h.memory()).unwrap();
                db.execute(
                "INSERT INTO memory_grants VALUES('agent:dot','repo:other','review','t','admin')",
                [],
            )
            .unwrap();
                if scope.starts_with("collection:") {
                    db.execute(
                        "INSERT INTO memory_grants VALUES('agent:dot','*','review','t','admin')",
                        [],
                    )
                    .unwrap();
                }
                db.execute(
                    "INSERT INTO memory_grants VALUES('agent:dot',?1,?2,'t','admin')",
                    [scope, right],
                )
                .unwrap();
                let tx = crate::writer::worker::Tx(&db);
                let mut body = load(&tx, opened.body["proposal_id"].as_str().unwrap())
                    .unwrap()
                    .unwrap();
                let p = Prepared {
                    credential: auth::Credential::Principal {
                        id: "agent:dot".into(),
                        digest: "fake".into(),
                    },
                    action: Action::Capture { payload: json!({}) },
                    client_event_id: h.id(),
                    request_digest: String::new(),
                    now_ms: h.now(),
                };
                let r = apply_cards(
                    &tx,
                    &p,
                    &mut body,
                    &Authority::default(),
                    &Fixed(Scan::Clean {
                        ruleset: "test-v7".into(),
                    }),
                )
                .unwrap()
                .unwrap();
                assert_eq!(r.status, 403, "{r:?}");
                assert_eq!(
                    ops::head(&tx, scope, "a").unwrap().unwrap().revision,
                    rev(&a)
                );
            }
        }
    }
}
