//! `fleet-hub-memory.db` schema. Rust owns this file's DDL (D7); Python never opens it.
//! Later slices add the index, vector, proposal and evidence tables as new versions.
use rusqlite::{Connection, OptionalExtension};

pub const USER_VERSION: i64 = 7;
/// "BRM1": refuses to treat an unrelated SQLite file as the memory store.
pub const APPLICATION_ID: i64 = 0x4252_4D31;
pub const NORMALIZER_VERSION: &str = "1";
const TRUST: &str = "'reviewed','policy-promoted','imported','agent-unreviewed','flagged'";
/// Version 3 admits `private` (the S18 amendment to D24).
const SCAN_STATES: &str = "'clean','private','flagged','pending'";
/// Tables whose scan-state CHECK names `private` from version 3.
const SCANNED: [&str; 3] = ["memory_card_revisions", "memory_scans", "memory_handoffs"];

/// One scanned table's DDL under `name`, so a migration can build a copy.
fn scanned_table(table: &str, name: &str) -> String {
    match table {
        "memory_card_revisions" => format!(
            "CREATE TABLE {name}(
  scope TEXT NOT NULL,
  card_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  content_hash TEXT NOT NULL REFERENCES memory_blobs(content_hash),
  trust TEXT NOT NULL CHECK(trust IN ({TRUST})),
  state TEXT NOT NULL CHECK(state IN ('live','retired')),
  writer TEXT NOT NULL,
  seq INTEGER NOT NULL,
  proposal_id TEXT,
  scan_state TEXT NOT NULL CHECK(scan_state IN ({SCAN_STATES})),
  created_at TEXT NOT NULL,
  PRIMARY KEY(scope, card_id, revision)
) WITHOUT ROWID;
"
        ),
        "memory_scans" => format!(
            "CREATE TABLE {name}(
  content_hash TEXT NOT NULL,
  ruleset_version TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ({SCAN_STATES},'blocked')),
  rule_ids TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY(content_hash, ruleset_version)
) WITHOUT ROWID;
"
        ),
        _ => format!(
            "CREATE TABLE {name}(
  handoff_id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_hash TEXT NOT NULL REFERENCES memory_blobs(content_hash),
  state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
  scan_state TEXT NOT NULL CHECK(scan_state IN ({SCAN_STATES})),
  seq INTEGER NOT NULL,
  created_at TEXT NOT NULL
) WITHOUT ROWID;
"
        ),
    }
}

/// Version 2 to 3: SQLite cannot alter a CHECK, so rebuild each scanned
/// table with the same columns and rows. Nothing references these tables and
/// they carry no index or trigger beyond their primary keys.
fn private_scan_state() -> String {
    let mut sql = String::new();
    for table in SCANNED {
        sql += &scanned_table(table, &format!("{table}_v3"));
        sql += &format!(
            "INSERT INTO {table}_v3 SELECT * FROM {table};\nDROP TABLE {table};\nALTER TABLE {table}_v3 RENAME TO {table};\n"
        );
    }
    sql
}

pub(super) fn ddl() -> String {
    let scanned: String = SCANNED.iter().map(|t| scanned_table(t, t)).collect();
    format!(
        "
CREATE TABLE memory_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE memory_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  client_event_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('card.put','card.retire','card.trust','card.import',
    'evidence.attach','handoff.submit','proposal.open','proposal.decide','proposal.apply',
    'proposal.rollback','grant.change','recall.capture','memory.auto_apply_switch','evidence.purge','retrieval_eval.record')),
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
{scanned}CREATE TABLE memory_grants(
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
{}{}{}{}
",
        super::embedding::DDL,
        super::proposals::DDL,
        super::attestation::DDL,
        super::retrieval_eval::DDL
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
    "memory_vectors",
    "memory_lineage",
    "memory_embed_jobs",
    "memory_proposals",
    "memory_evidence",
    "memory_retrieval_evals",
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
    // Each step runs in its own transaction and re-reads the version under the
    // write lock, so a later step appends here without repeating an earlier one.
    // Version 2 is S15b's embedding tables, version 3 the private scan state.
    if version < 2 {
        step(c, super::embedding::DDL, 2).map_err(err)?;
    }
    if version < 3 {
        step(c, &private_scan_state(), 3).map_err(err)?;
    }
    if version < 4 {
        step(c, super::proposals::DDL, 4).map_err(err)?;
    }
    if version < 5 {
        step(c, &super::attestation::legacy_ddl(), 5).map_err(err)?;
    }
    if version < 6 {
        step(c, &review_schema(), 6).map_err(err)?;
    }
    if version < 7 {
        step(c, &eval_schema(), 7).map_err(err)?;
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

/// Extend the event CHECK without losing sequence high watermarks or triggers.
fn review_schema() -> String {
    format!(
        "{} {}",
        super::attestation::tombstone_migration(),
        event_schema()
    )
}

fn event_schema() -> String {
    let ddl = ddl();
    let block = &ddl[ddl.find("CREATE TABLE memory_events(").unwrap()
        ..ddl.find("CREATE TABLE memory_blobs(").unwrap()];
    let (table, rest) = block.split_once("CREATE INDEX memory_events_card").unwrap();
    format!(
        "CREATE TEMP TABLE memory_events_high_water AS SELECT seq FROM sqlite_sequence WHERE name='memory_events'; {} INSERT INTO memory_events_v6 SELECT * FROM memory_events; DROP TABLE memory_events; ALTER TABLE memory_events_v6 RENAME TO memory_events; CREATE INDEX memory_events_card{} UPDATE sqlite_sequence SET seq=MAX(seq,COALESCE((SELECT seq FROM memory_events_high_water),0)) WHERE name='memory_events'; DROP TABLE memory_events_high_water;",
        table.replace(
            "CREATE TABLE memory_events(",
            "CREATE TABLE memory_events_v6("
        ),
        rest
    )
}

fn eval_schema() -> String {
    format!("{} {}", super::retrieval_eval::DDL, event_schema())
}

/// Apply one migration step unless another process already recorded it.
/// The version is read again under the write lock: two hubs opening the same
/// older file at once rebuild it once.
pub(super) fn step(c: &Connection, sql: &str, version: i64) -> rusqlite::Result<()> {
    c.execute_batch("BEGIN IMMEDIATE")?;
    let applied = c
        .pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
        .and_then(|current| {
            if current >= version {
                return Ok(());
            }
            c.execute_batch(sql)
                .and_then(|()| c.pragma_update(None, "user_version", version))
        })
        .and_then(|()| c.execute_batch("COMMIT"));
    if applied.is_err() {
        let _ = c.execute_batch("ROLLBACK");
    }
    applied
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
