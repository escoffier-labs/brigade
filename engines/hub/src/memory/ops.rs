//! Memory write commands. A command arrives at the memory writer fully
//! prepared (validated, normalized and hashed). `apply` scans inside
//! the memory transaction while the hub write lock is held, and does every
//! decision read there: grants, the idempotency record, the epoch and the head.
use super::{Reply, auth, auth::Credential, ids, scan::Scan};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};

#[derive(Clone, Debug)]
pub struct Base {
    pub revision: i64,
    pub epoch: String,
}
#[derive(Clone, Debug)]
pub enum Action {
    Rescan {
        limit: usize,
        retry_failed: bool,
        after: Option<String>,
    },
    Proposal(super::proposals::Command),
    Embedding {
        command: super::embedding::Command,
        config: Option<super::embedder::Config>,
    },
    Put {
        scope: String,
        card_id: String,
        base: Option<Base>,
        text: String,
        hash: String,
        origin: Option<String>,
        scan: Scan,
        import: bool,
    },
    Retire {
        scope: String,
        card_id: String,
        base: Base,
        reason: Option<String>,
    },
    Handoff {
        kind: String,
        text: String,
        hash: String,
        scan: Scan,
    },
    // Audit-only free-form strings. Capture fields never reach a prompt.
    Capture {
        payload: Json,
    },
    Grant {
        principal: String,
        scope: String,
        rights: Vec<String>,
    },
    /// Test-only command that records the policy revision it saw under the lock.
    #[cfg(test)]
    PolicyProbe,
}
impl Action {
    pub fn name(&self) -> &'static str {
        match self {
            Action::Rescan { .. } => "rescan",
            Action::Proposal(c) => c.name(),
            Action::Embedding { command, .. } => match command {
                super::embedding::Command::Reindex { .. } => "reindex",
                super::embedding::Command::Lease { .. } => "embed_lease",
                super::embedding::Command::Failed { .. } => "embed_failed",
                super::embedding::Command::Submit { .. } => "embed_submit",
            },
            Action::Put { import: false, .. } => "put",
            Action::Put { import: true, .. } => "import",
            Action::Retire { .. } => "retire",
            Action::Handoff { .. } => "handoff",
            Action::Capture { .. } => "capture",
            Action::Grant { .. } => "grant",
            #[cfg(test)]
            Action::PolicyProbe => "probe",
        }
    }
    /// The grant an action needs, and the scope it needs it on.
    fn requirement(&self) -> (Option<&str>, &'static str) {
        match self {
            Action::Proposal(c) => (None, c.right()),
            Action::Embedding {
                command: super::embedding::Command::Reindex { .. },
                ..
            } => (None, "admin"),
            Action::Embedding { .. } => (None, "embed"),
            Action::Put {
                scope,
                import: false,
                ..
            }
            | Action::Retire { scope, .. } => (Some(scope), "write"),
            Action::Put {
                scope,
                import: true,
                ..
            } => (Some(scope), "import"),
            Action::Handoff { .. } => (None, "handoff"),
            Action::Capture { .. } => (None, "read"),
            Action::Grant { .. } | Action::Rescan { .. } => (None, "admin"),
            #[cfg(test)]
            Action::PolicyProbe => (None, "read"),
        }
    }
    /// Decisions and policy applies re-read the policy under the hub lock.
    pub fn needs_policy(&self) -> bool {
        #[cfg(test)]
        if matches!(self, Action::PolicyProbe) {
            return true;
        }
        matches!(self, Action::Proposal(_))
    }
    /// New blob bytes this command may add, for the per-batch bound.
    pub fn new_bytes(&self) -> usize {
        match self {
            Action::Proposal(c) => c.new_bytes(),
            Action::Put { text, .. } | Action::Handoff { text, .. } => text.len(),
            _ => 0,
        }
    }
}
#[derive(Clone, Debug)]
pub struct Prepared {
    pub credential: Credential,
    pub action: Action,
    pub client_event_id: String,
    pub request_digest: String,
    pub now_ms: i64,
}
/// What the memory writer re-read from the hub file under the hub lock.
#[derive(Clone, Debug, Default)]
pub struct Authority {
    pub policy: Option<(i64, String)>,
}

pub fn error(status: u16, code: &str, message: &str) -> Reply {
    Reply::new(status, json!({"error": message, "code": code}))
}
fn with(mut reply: Reply, extra: Json) -> Reply {
    if let (Some(body), Some(extra)) = (reply.body.as_object_mut(), extra.as_object()) {
        for (k, v) in extra {
            body.insert(k.clone(), v.clone());
        }
    }
    reply
}
pub(super) fn t(s: &str) -> Value {
    Value::Text(s.into())
}
fn opt(s: Option<&str>) -> Value {
    s.map(t).unwrap_or(Value::Null)
}
fn int(v: &Value) -> i64 {
    if let Value::Integer(i) = v { *i } else { 0 }
}
pub(super) fn s(v: &Value) -> String {
    if let Value::Text(s) = v {
        s.clone()
    } else {
        String::new()
    }
}

pub fn epoch<T: ReadTx + ?Sized>(tx: &T) -> rusqlite::Result<String> {
    Ok(tx
        .query("SELECT value FROM memory_meta WHERE key='log_epoch'", &[])?
        .first()
        .map(|r| s(&r[0]))
        .unwrap_or_default())
}
pub(super) struct Head {
    pub(super) revision: i64,
    pub(super) hash: String,
    pub(super) state: String,
    pub(super) trust: String,
}
pub(super) fn head<T: ReadTx + ?Sized>(
    tx: &T,
    scope: &str,
    card_id: &str,
) -> rusqlite::Result<Option<Head>> {
    Ok(tx
        .query(
            "SELECT revision,content_hash,state,trust FROM memory_cards WHERE scope=?1 AND card_id=?2",
            &[t(scope), t(card_id)],
        )?
        .first()
        .map(|r| Head {
            revision: int(&r[0]),
            hash: s(&r[1]),
            state: s(&r[2]),
            trust: s(&r[3]),
        }))
}
fn mismatch(current: Option<&Head>) -> Reply {
    with(
        error(
            409,
            "memory_base_mismatch",
            "the card's head moved; re-read it and edit the current revision",
        ),
        json!({
            "current_revision": current.map(|h| h.revision),
            "current_hash": current.map(|h| h.hash.clone()),
        }),
    )
}
fn blocked(rules: &[String]) -> Reply {
    with(
        error(
            422,
            "memory_content_blocked",
            "content blocked by a scan rule",
        ),
        json!({"rules": rules}),
    )
}

pub(super) struct NewEvent<'a> {
    pub(super) kind: &'a str,
    pub(super) scope: Option<&'a str>,
    pub(super) card_id: Option<&'a str>,
    pub(super) changes_head: bool,
    pub(super) base_revision: Option<i64>,
    pub(super) content_hash: Option<&'a str>,
    pub(super) trust: Option<&'a str>,
    pub(super) origin: Option<&'a str>,
    pub(super) payload: Json,
    pub(super) policy_digest: Option<&'a str>,
}
/// Appends one event and returns its `seq`. A head-changing event's revision
/// equals its `seq`. `seq` is chosen inside the write transaction and never
/// reused while the file is not restored, since events are never deleted.
pub(super) fn append(tx: &dyn WriteTx, p: &Prepared, e: NewEvent) -> rusqlite::Result<i64> {
    let seq = int(&tx.query(
        "SELECT max(COALESCE((SELECT seq FROM sqlite_sequence WHERE name='memory_events'),0),COALESCE((SELECT max(seq) FROM memory_events),0))+1",
        &[],
    )?[0][0]);
    tx.execute(
        "INSERT INTO memory_events(seq,event_id,client_event_id,kind,principal,scope,card_id,revision,base_revision,content_hash,trust,origin,payload,policy_digest,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)",
        &[
            Value::Integer(seq),
            t(&ids::mint(p.now_ms)),
            t(&p.client_event_id),
            t(e.kind),
            t(p.credential.principal()),
            opt(e.scope),
            opt(e.card_id),
            if e.changes_head {
                Value::Integer(seq)
            } else {
                Value::Null
            },
            e.base_revision.map(Value::Integer).unwrap_or(Value::Null),
            opt(e.content_hash),
            opt(e.trust),
            opt(e.origin),
            t(&ids::canonical(&e.payload)),
            opt(e.policy_digest),
            t(&ids::isoformat(p.now_ms)),
        ],
    )?;
    Ok(seq)
}
pub(super) fn store_blob(
    tx: &dyn WriteTx,
    hash: &str,
    text: &str,
    stamp: &str,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO memory_blobs(content_hash,bytes,size,created_at) VALUES(?1,?2,?3,?4) ON CONFLICT(content_hash) DO NOTHING",
        &[
            t(hash),
            Value::Blob(text.as_bytes().to_vec()),
            Value::Integer(text.len() as i64),
            t(stamp),
        ],
    )?;
    Ok(())
}
pub(super) fn store_scan(
    tx: &dyn WriteTx,
    hash: &str,
    scan: &Scan,
    stamp: &str,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO memory_scans(content_hash,ruleset_version,state,rule_ids,scanned_at) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(content_hash,ruleset_version) DO UPDATE SET state=excluded.state,rule_ids=excluded.rule_ids,scanned_at=excluded.scanned_at",
        &[
            t(hash),
            t(scan.ruleset()),
            t(scan.state()),
            t(&serde_json::to_string(scan.rules()).unwrap()),
            t(stamp),
        ],
    )?;
    Ok(())
}
/// Moves a card's head and records the revision row in one step.
#[allow(clippy::too_many_arguments)]
pub(super) fn set_head(
    tx: &dyn WriteTx,
    p: &Prepared,
    scope: &str,
    card_id: &str,
    revision: i64,
    hash: &str,
    state: &str,
    trust: &str,
    scan_state: &str,
    text: Option<&str>,
) -> rusqlite::Result<()> {
    let stamp = ids::isoformat(p.now_ms);
    let (title, summary, tags) = text.map(ids::describe).unwrap_or_default();
    let principal = p.credential.principal();
    if text.is_some() {
        tx.execute(
            "INSERT INTO memory_cards(scope,card_id,revision,content_hash,state,trust,writer,writer_class,title,summary,tags,created_at,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?12) ON CONFLICT(scope,card_id) DO UPDATE SET revision=excluded.revision,content_hash=excluded.content_hash,state=excluded.state,trust=excluded.trust,writer=excluded.writer,writer_class=excluded.writer_class,title=excluded.title,summary=excluded.summary,tags=excluded.tags,updated_at=excluded.updated_at",
            &[
                t(scope),
                t(card_id),
                Value::Integer(revision),
                t(hash),
                t(state),
                t(trust),
                t(principal),
                t(p.credential.class()),
                opt(title.as_deref()),
                opt(summary.as_deref()),
                opt(tags.as_deref()),
                t(&stamp),
            ],
        )?;
    } else {
        tx.execute(
            "UPDATE memory_cards SET revision=?3,state=?4,trust=?5,updated_at=?6 WHERE scope=?1 AND card_id=?2",
            &[
                t(scope),
                t(card_id),
                Value::Integer(revision),
                t(state),
                t(trust),
                t(&stamp),
            ],
        )?;
    }
    tx.execute(
        "INSERT INTO memory_card_revisions(scope,card_id,revision,content_hash,trust,state,writer,seq,proposal_id,scan_state,created_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?3,NULL,?8,?9)",
        &[
            t(scope),
            t(card_id),
            Value::Integer(revision),
            t(hash),
            t(trust),
            t(state),
            t(principal),
            t(scan_state),
            t(&stamp),
        ],
    )?;
    Ok(())
}
fn event_json(scope: &str, card_id: &str, seq: i64, hash: &str, trust: &str, state: &str) -> Json {
    json!({
        "seq": seq,
        "revision": seq,
        "scope": scope,
        "card_id": card_id,
        "content_hash": hash,
        "trust": trust,
        "state": state,
    })
}
fn scan_state_of<T: ReadTx + ?Sized>(tx: &T, hash: &str) -> rusqlite::Result<String> {
    Ok(tx
        .query(
            "SELECT scan_state FROM memory_card_revisions WHERE content_hash=?1 ORDER BY seq DESC LIMIT 1",
            &[t(hash)],
        )?
        .first()
        .map(|r| s(&r[0]))
        .unwrap_or_else(|| "pending".into()))
}

/// Apply one prepared command. Refusals are returned as replies; the writer
/// rolls back the command's savepoint for any status of 400 or more.
pub fn apply(
    tx: &dyn WriteTx,
    p: &Prepared,
    authority: &Authority,
    scanner: &dyn super::scan::Scanner,
    embedder: Option<(&str, bool)>,
) -> rusqlite::Result<Reply> {
    let (scope, right) = p.action.requirement();
    if !auth::has_right(tx, &p.credential, scope, right)? {
        return Ok(error(
            403,
            "memory_grant_missing",
            &format!("this credential has no {right} grant for this memory action"),
        ));
    }
    if let Action::Proposal(c) = &p.action {
        if let Some(r) = super::proposals::authorize(tx, &p.credential, &c.request)? {
            return Ok(r);
        }
    }
    let principal = p.credential.principal();
    // Embedding commands keep no idempotency row. A lease reply carries card
    // text, and a stored copy would outlive the card's retirement and answer
    // a replay without the grant, state and scan checks. Each command is
    // safe to repeat: a second submit or failure finds no leased job.
    let replayable = !matches!(p.action, Action::Embedding { .. });
    let stored = if replayable {
        tx.query(
        "SELECT request_digest,status,response FROM memory_idempotency WHERE principal=?1 AND client_event_id=?2",
            &[t(principal), t(&p.client_event_id)],
        )?
    } else {
        Vec::new()
    };
    if let Some(row) = stored.first() {
        // Checked before compare-and-set, so a retry of a committed write gets
        // its original answer even after the card moved.
        if s(&row[0]) == p.request_digest {
            let body = serde_json::from_str(&s(&row[2])).unwrap_or(Json::Null);
            return Ok(Reply::new(int(&row[1]) as u16, body));
        }
        return Ok(error(
            409,
            "memory_client_event_conflict",
            "client_event_id was already used for a different request",
        ));
    }
    // Scan the exact immutable normalized bytes after grants and idempotency,
    // inside the savepoint and authority lock. Never accept a prepared scan.
    let mut p = p.clone();
    match &mut p.action {
        Action::Put { text, scan, .. } | Action::Handoff { text, scan, .. } => {
            *scan = scanner.scan(text);
        }
        _ => {}
    }
    let reply = run(tx, &p, authority, scanner)?;
    if reply.status < 300 {
        match &p.action {
            Action::Put { scope, card_id, .. } | Action::Retire { scope, card_id, .. } => {
                super::embedding::cleanup_card(tx, scope, card_id)?;
                if let Some((id, private)) = embedder {
                    super::embedding::queue_card(tx, scope, card_id, id, private)?;
                }
            }
            Action::Proposal(c) if c.name() == "decide" && reply.body["state"] == "applied" => {
                for row in tx.query(
                    "SELECT scope,card_id FROM memory_card_revisions WHERE proposal_id=?1",
                    &[t(reply.body["proposal_id"].as_str().unwrap())],
                )? {
                    let scope = s(&row[0]);
                    let card_id = s(&row[1]);
                    super::embedding::cleanup_card(tx, &scope, &card_id)?;
                    if let Some((id, private)) = embedder {
                        super::embedding::queue_card(tx, &scope, &card_id, id, private)?;
                    }
                }
            }
            _ => (),
        }
    }
    if reply.status < 300
        && replayable
        && !(matches!(p.action, Action::Rescan { .. }) && reply.body["scanned"] == 0)
    {
        tx.execute(
            "INSERT INTO memory_idempotency(principal,client_event_id,request_digest,status,response,created_ms) VALUES(?1,?2,?3,?4,?5,?6)",
            &[
                t(principal),
                t(&p.client_event_id),
                t(&p.request_digest),
                Value::Integer(i64::from(reply.status)),
                t(&reply.body.to_string()),
                Value::Integer(p.now_ms),
            ],
        )?;
    }
    Ok(reply)
}

fn run(
    tx: &dyn WriteTx,
    p: &Prepared,
    _authority: &Authority,
    scanner: &dyn super::scan::Scanner,
) -> rusqlite::Result<Reply> {
    let stamp = ids::isoformat(p.now_ms);
    let current_epoch = epoch(tx)?;
    let epoch_changed = || {
        with(
            error(
                409,
                "memory_epoch_changed",
                "the memory log was restored since this revision was read",
            ),
            json!({"log_epoch": current_epoch}),
        )
    };
    match &p.action {
        Action::Rescan {
            limit,
            retry_failed,
            after,
        } => super::rescan::run(tx, p, scanner, *limit, *retry_failed, after.as_deref()),
        Action::Proposal(c) => {
            let mut reply = super::proposals::run(tx, p, c, _authority, scanner)?;
            if reply.body["log_epoch"].is_string() {
                reply.body["log_epoch"] = json!(current_epoch);
            }
            Ok(reply)
        }
        Action::Embedding { command, config } => {
            super::embedding::apply(tx, p, command, config.as_ref(), scanner)
        }
        Action::Put {
            scope,
            card_id,
            base,
            text,
            hash,
            origin,
            scan,
            import,
        } => {
            if matches!(scan, Scan::Failed) {
                return Ok(with(
                    error(
                        503,
                        "memory_scan_failed",
                        "input cannot be scanned, retrying will not help",
                    ),
                    json!({"retryable": false}),
                ));
            }
            // Only import raises trust here, and only on a clean scan (D24).
            if *import && !scan.permits_raise() {
                return Ok(match scan {
                    Scan::Unavailable => with(
                        error(503, "memory_scan_unavailable", "scan unavailable"),
                        json!({"retryable": true}),
                    ),
                    _ => blocked(scan.rules()),
                });
            }
            if let Scan::Blocked { rules, .. } = scan {
                return Ok(blocked(rules));
            }
            if let Some(base) = base {
                if base.epoch != current_epoch {
                    return Ok(epoch_changed());
                }
            }
            let current = head(tx, scope, card_id)?;
            match (base, &current) {
                (None, None) => {}
                (Some(b), Some(h)) if b.revision == h.revision => {}
                _ => return Ok(mismatch(current.as_ref())),
            }
            let trust = match (import, scan) {
                (true, _) => "imported",
                (false, Scan::Flagged { .. }) => "flagged",
                _ => "agent-unreviewed",
            };
            store_blob(tx, hash, text, &stamp)?;
            store_scan(tx, hash, scan, &stamp)?;
            let seq = append(
                tx,
                p,
                NewEvent {
                    kind: if *import { "card.import" } else { "card.put" },
                    scope: Some(scope),
                    card_id: Some(card_id),
                    changes_head: true,
                    base_revision: base.as_ref().map(|b| b.revision),
                    content_hash: Some(hash),
                    trust: Some(trust),
                    origin: origin.as_deref(),
                    payload: json!({"size": text.len(), "revived": current.as_ref().is_some_and(|h| h.state == "retired"), "tier_floor": scan.tier_floor()}),
                    policy_digest: None,
                },
            )?;
            let scan_state = if scan.state() == "blocked" {
                "pending"
            } else {
                scan.state()
            };
            set_head(
                tx,
                p,
                scope,
                card_id,
                seq,
                hash,
                "live",
                trust,
                scan_state,
                Some(text),
            )?;
            Ok(Reply::new(
                201,
                json!({"event": event_json(scope, card_id, seq, hash, trust, "live"), "log_epoch": current_epoch, "tier_floor": scan.tier_floor()}),
            ))
        }
        Action::Retire {
            scope,
            card_id,
            base,
            reason,
        } => {
            if base.epoch != current_epoch {
                return Ok(epoch_changed());
            }
            let Some(h) = head(tx, scope, card_id)? else {
                return Ok(error(404, "memory_card_not_found", "card not found"));
            };
            if h.revision != base.revision {
                return Ok(mismatch(Some(&h)));
            }
            if h.state == "retired" {
                // Already retired at the revision the caller read: nothing changes.
                return Ok(Reply::new(
                    200,
                    json!({"event": event_json(scope, card_id, h.revision, &h.hash, &h.trust, "retired"), "log_epoch": current_epoch}),
                ));
            }
            let scan_state = scan_state_of(tx, &h.hash)?;
            let seq = append(
                tx,
                p,
                NewEvent {
                    kind: "card.retire",
                    scope: Some(scope),
                    card_id: Some(card_id),
                    changes_head: true,
                    base_revision: Some(base.revision),
                    content_hash: Some(&h.hash),
                    trust: Some(&h.trust),
                    origin: None,
                    payload: json!({"reason": reason}),
                    policy_digest: None,
                },
            )?;
            set_head(
                tx,
                p,
                scope,
                card_id,
                seq,
                &h.hash,
                "retired",
                &h.trust,
                &scan_state,
                None,
            )?;
            Ok(Reply::new(
                200,
                json!({"event": event_json(scope, card_id, seq, &h.hash, &h.trust, "retired"), "log_epoch": current_epoch}),
            ))
        }
        Action::Handoff {
            kind,
            text,
            hash,
            scan,
        } => {
            if matches!(scan, Scan::Failed) {
                return Ok(with(
                    error(
                        503,
                        "memory_scan_failed",
                        "input cannot be scanned, retrying will not help",
                    ),
                    json!({"retryable": false}),
                ));
            }
            if let Scan::Blocked { rules, .. } = scan {
                return Ok(blocked(rules));
            }
            store_blob(tx, hash, text, &stamp)?;
            store_scan(tx, hash, scan, &stamp)?;
            let handoff_id = ids::mint(p.now_ms);
            let seq = append(
                tx,
                p,
                NewEvent {
                    kind: "handoff.submit",
                    scope: None,
                    card_id: None,
                    changes_head: false,
                    base_revision: None,
                    content_hash: Some(hash),
                    trust: None,
                    origin: None,
                    payload: json!({"handoff_id": handoff_id, "kind": kind}),
                    policy_digest: None,
                },
            )?;
            tx.execute(
                "INSERT INTO memory_handoffs(handoff_id,principal,kind,content_hash,state,scan_state,seq,created_at) VALUES(?1,?2,?3,?4,'pending',?5,?6,?7)",
                &[
                    t(&handoff_id),
                    t(p.credential.principal()),
                    t(kind),
                    t(hash),
                    t(scan.state()),
                    Value::Integer(seq),
                    t(&stamp),
                ],
            )?;
            Ok(Reply::new(
                201,
                json!({"handoff_id": handoff_id, "log_epoch": current_epoch}),
            ))
        }
        Action::Capture { payload } => {
            append(
                tx,
                p,
                NewEvent {
                    kind: "recall.capture",
                    scope: None,
                    card_id: None,
                    changes_head: false,
                    base_revision: None,
                    content_hash: None,
                    trust: None,
                    origin: None,
                    payload: payload.clone(),
                    policy_digest: None,
                },
            )?;
            Ok(Reply::new(200, json!({"recorded": true})))
        }
        Action::Grant {
            principal,
            scope,
            rights,
        } => {
            if rights.is_empty() {
                tx.execute(
                    "DELETE FROM memory_grants WHERE principal=?1 AND scope=?2",
                    &[t(principal), t(scope)],
                )?;
            } else {
                tx.execute(
                    "INSERT INTO memory_grants(principal,scope,rights,updated_at,updated_by) VALUES(?1,?2,?3,?4,?5) ON CONFLICT(principal,scope) DO UPDATE SET rights=excluded.rights,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                    &[
                        t(principal),
                        t(scope),
                        t(&rights.join(",")),
                        t(&stamp),
                        t(p.credential.principal()),
                    ],
                )?;
            }
            let grant = json!({"principal": principal, "scope": scope, "rights": rights});
            append(
                tx,
                p,
                NewEvent {
                    kind: "grant.change",
                    scope: None,
                    card_id: None,
                    changes_head: false,
                    base_revision: None,
                    content_hash: None,
                    trust: None,
                    origin: None,
                    payload: grant.clone(),
                    policy_digest: None,
                },
            )?;
            Ok(Reply::new(200, json!({"grant": grant})))
        }
        #[cfg(test)]
        Action::PolicyProbe => Ok(Reply::new(
            200,
            json!({"policy": _authority.policy.as_ref().map(|(r, d)| json!([r, d]))}),
        )),
    }
}
