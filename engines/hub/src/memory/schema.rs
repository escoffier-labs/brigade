//! `fleet-hub-memory.db` schema. Rust owns this file's DDL (D7); Python never opens it.
//! Later slices add the index, vector, proposal and evidence tables as new versions.
use rusqlite::{Connection, OptionalExtension};

pub const USER_VERSION: i64 = 1;
/// "BRM1": refuses to treat an unrelated SQLite file as the memory store.
pub const APPLICATION_ID: i64 = 0x4252_4D31;
pub const NORMALIZER_VERSION: &str = "1";
const TRUST: &str = "'reviewed','policy-promoted','imported','agent-unreviewed','flagged'";

fn ddl() -> String {
    format!(
        "
CREATE TABLE memory_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE memory_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  client_event_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('card.put','card.retire','card.trust','card.import',
    'evidence.attach','handoff.submit','proposal.open','proposal.decide','proposal.apply',
    'proposal.rollback','grant.change','recall.capture')),
  principal TEXT NOT NULL,
  scope TEXT,
  card_id TEXT,
  revision INTEGER CHECK(revision IS NULL OR revision = seq),
  base_revision INTEGER,
  content_hash TEXT,
  trust TEXT CHECK(trust IS NULL OR trust IN ({TRUST})),
  origin TEXT CHECK(origin IS NULL OR origin IN ('interactive','scheduled','worker')),
  payload TEXT NOT NULL,
  policy_digest TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX memory_events_card ON memory_events(scope, card_id, seq);
CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events
  BEGIN SELECT RAISE(ABORT, 'memory_events is append-only'); END;
CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events
  BEGIN SELECT RAISE(ABORT, 'memory_events is append-only'); END;
CREATE TABLE memory_blobs(
  content_hash TEXT PRIMARY KEY,
  bytes BLOB NOT NULL,
  size INTEGER NOT NULL,
  created_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE memory_cards(
  scope TEXT NOT NULL,
  card_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_hash TEXT NOT NULL REFERENCES memory_blobs(content_hash),
  state TEXT NOT NULL CHECK(state IN ('live','retired')),
  trust TEXT NOT NULL CHECK(trust IN ({TRUST})),
  writer TEXT NOT NULL,
  writer_class TEXT NOT NULL CHECK(writer_class IN ('admin','node','principal')),
  title TEXT,
  summary TEXT,
  tags TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(scope, card_id)
) WITHOUT ROWID;
CREATE INDEX memory_cards_id ON memory_cards(card_id);
CREATE TABLE memory_card_revisions(
  scope TEXT NOT NULL,
  card_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_hash TEXT NOT NULL REFERENCES memory_blobs(content_hash),
  trust TEXT NOT NULL CHECK(trust IN ({TRUST})),
  state TEXT NOT NULL CHECK(state IN ('live','retired')),
  writer TEXT NOT NULL,
  seq INTEGER NOT NULL,
  proposal_id TEXT,
  scan_state TEXT NOT NULL CHECK(scan_state IN ('clean','flagged','pending')),
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope, card_id, revision)
) WITHOUT ROWID;
CREATE TABLE memory_scans(
  content_hash TEXT NOT NULL,
  ruleset_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('clean','flagged','blocked','pending')),
  rule_ids TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY(content_hash, ruleset_version)
) WITHOUT ROWID;
CREATE TABLE memory_handoffs(
  handoff_id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_hash TEXT NOT NULL REFERENCES memory_blobs(content_hash),
  state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
  scan_state TEXT NOT NULL CHECK(scan_state IN ('clean','flagged','pending')),
  seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE memory_grants(
  principal TEXT NOT NULL,
  scope TEXT NOT NULL,
  rights TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  PRIMARY KEY(principal, scope)
) WITHOUT ROWID;
CREATE TABLE memory_idempotency(
  principal TEXT NOT NULL,
  client_event_id TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  status INTEGER NOT NULL,
  response TEXT NOT NULL,
  created_ms INTEGER NOT NULL,
  PRIMARY KEY(principal, client_event_id)
) WITHOUT ROWID;
CREATE INDEX memory_idempotency_age ON memory_idempotency(created_ms);
"
    )
}

pub const TABLES: &[&str] = &[
    "memory_meta",
    "memory_events",
    "memory_blobs",
    "memory_cards",
    "memory_card_revisions",
    "memory_scans",
    "memory_handoffs",
    "memory_grants",
    "memory_idempotency",
];

/// Create a fresh file's schema, or check an existing one. Never downgrades,
/// never touches a file whose `application_id` or version it does not know.
pub fn migrate(c: &Connection, now_ms: i64) -> Result<(), String> {
    let err = |e: rusqlite::Error| e.to_string();
    let version: i64 = c
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .map_err(err)?;
    let application: i64 = c
        .pragma_query_value(None, "application_id", |r| r.get(0))
        .map_err(err)?;
    if version == 0 {
        let objects: i64 = c
            .query_row("SELECT count(*) FROM sqlite_schema", [], |r| r.get(0))
            .map_err(err)?;
        if objects != 0 || application != 0 {
            return Err("memory file is not empty and has no memory schema".into());
        }
        c.execute_batch("BEGIN IMMEDIATE").map_err(err)?;
        let created = (|| {
            c.execute_batch(&ddl())?;
            let meta = [
                ("log_epoch", super::ids::mint(now_ms)),
                ("normalizer_version", NORMALIZER_VERSION.into()),
                ("index_format_version", "0".into()),
                ("created_at", super::ids::isoformat(now_ms)),
            ];
            for (k, v) in meta {
                c.execute("INSERT INTO memory_meta(key,value) VALUES(?1,?2)", [k, &v])?;
            }
            c.execute_batch(&format!(
                "PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version={USER_VERSION};"
            ))
        })();
        // A failed COMMIT can leave the transaction open; end it either way.
        if let Err(e) = created.and_then(|()| c.execute_batch("COMMIT")) {
            let _ = c.execute_batch("ROLLBACK");
            return Err(e.to_string());
        }
        return Ok(());
    }
    if application != APPLICATION_ID {
        return Err("memory file has a foreign application_id".into());
    }
    if version > USER_VERSION {
        return Err(format!(
            "memory schema version {version} is newer than this hub ({USER_VERSION})"
        ));
    }
    for table in TABLES {
        let present: Option<i64> = c
            .query_row(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?1",
                [table],
                |r| r.get(0),
            )
            .optional()
            .map_err(err)?;
        if present.is_none() {
            return Err(format!("memory schema is missing {table}"));
        }
    }
    log_epoch(c).map_err(err)?;
    Ok(())
}

pub fn log_epoch(c: &Connection) -> rusqlite::Result<String> {
    c.query_row(
        "SELECT value FROM memory_meta WHERE key='log_epoch'",
        [],
        |r| r.get(0),
    )
}

/// Start a new `log_epoch` on a file restored from a copy, before the hub
/// serves it. Revisions are unique only within one epoch, so a write based on
/// a revision read before the restore is then refused with
/// `memory_epoch_changed` instead of matching a reissued number. Run with the
/// hub stopped; the backup restore tool (S31) calls this. Returns the new epoch.
pub fn rotate_epoch(c: &Connection, now_ms: i64) -> rusqlite::Result<String> {
    let epoch = super::ids::mint(now_ms);
    c.execute_batch("BEGIN IMMEDIATE")?;
    let result = c.execute(
        "UPDATE memory_meta SET value=?1 WHERE key='log_epoch'",
        [&epoch],
    );
    match result {
        Ok(1) => {
            if let Err(e) = c.execute_batch("COMMIT") {
                let _ = c.execute_batch("ROLLBACK");
                return Err(e);
            }
        }
        Ok(_) => {
            c.execute_batch("ROLLBACK")?;
            return Err(rusqlite::Error::QueryReturnedNoRows);
        }
        Err(e) => {
            let _ = c.execute_batch("ROLLBACK");
            return Err(e);
        }
    }
    Ok(epoch)
}
