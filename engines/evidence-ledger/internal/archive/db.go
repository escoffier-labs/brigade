package archive

import (
	"database/sql"
	"fmt"
	"os"

	"github.com/escoffier-labs/miseledger/internal/security"
	_ "modernc.org/sqlite"
)

const SchemaVersion = 1

func Open(path string) (*sql.DB, error) {
	if err := security.EnsurePrivateParent(path); err != nil {
		return nil, err
	}
	// Pragmas go in the DSN so every pooled connection gets them. A plain
	// Exec only configures whichever connection served it: foreign_keys was
	// silently off for the rest of the pool, and with no busy_timeout any
	// cross-connection or cross-process lock contention failed instantly
	// with SQLITE_BUSY. WAL lets readers (stats, search, a second CLI
	// invocation) coexist with a long-running import.
	// synchronous(NORMAL) is the recommended pairing with WAL: fsync happens at
	// checkpoint rather than on every commit, which is a large speedup for
	// batched bulk imports and is still crash-safe (a power loss can lose the
	// last unsynced transactions but cannot corrupt the database).
	dsn := "file:" + path +
		"?_pragma=busy_timeout(10000)" +
		"&_pragma=journal_mode(WAL)" +
		"&_pragma=synchronous(NORMAL)" +
		"&_pragma=foreign_keys(1)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, err
	}
	chmodDBFiles(path)
	return db, nil
}

// chmodDBFiles keeps the database and its WAL sidecars private. The -wal and
// -shm files hold the same evidence as the main DB, so they must not be left
// world-readable. They are created lazily by SQLite, so this is best-effort.
func chmodDBFiles(path string) {
	for _, p := range []string{path, path + "-wal", path + "-shm"} {
		if _, err := os.Stat(p); err == nil {
			_ = security.ChmodPrivateFile(p)
		}
	}
}

// Checkpoint runs a non-blocking PASSIVE WAL checkpoint: it folds what it can
// from the WAL into the main database without taking a lock that would stall
// concurrent readers (search, stats, a second CLI). This is the right cadence
// after routine imports and prunes. Use CheckpointTruncate for explicit
// compaction when shrinking the -wal file is the goal.
func Checkpoint(db *sql.DB, path string) error {
	if _, err := db.Exec("PRAGMA wal_checkpoint(PASSIVE)"); err != nil {
		return err
	}
	chmodDBFiles(path)
	return nil
}

// CheckpointTruncate fully flushes the WAL and truncates the -wal file. It can
// briefly block readers, so it is reserved for the explicit compact command.
func CheckpointTruncate(db *sql.DB, path string) error {
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		return err
	}
	chmodDBFiles(path)
	return nil
}

func Migrate(db *sql.DB) error {
	if _, err := db.Exec(schemaSQL); err != nil {
		return err
	}
	if _, err := db.Exec("PRAGMA user_version = " + fmt.Sprint(SchemaVersion)); err != nil {
		return err
	}
	return nil
}

func UserVersion(db *sql.DB) (int, error) {
	var version int
	err := db.QueryRow("PRAGMA user_version").Scan(&version)
	return version, err
}

func HasFTS(db *sql.DB) bool {
	_, err := db.Exec("CREATE VIRTUAL TABLE IF NOT EXISTS fts_probe USING fts5(x)")
	if err != nil {
		return false
	}
	_, _ = db.Exec("DROP TABLE IF EXISTS fts_probe")
	return true
}

const schemaSQL = `
create table if not exists sources(
  id text primary key,
  kind text not null,
  name text,
  version text,
  created_at text not null,
  updated_at text not null
);

create table if not exists collections(
  id text primary key,
  source_id text not null references sources(id),
  external_id text not null,
  kind text not null,
  name text,
  metadata_json text not null default '{}',
  created_at text,
  updated_at text,
  unique(source_id, external_id)
);

create table if not exists actors(
  id text primary key,
  source_id text not null references sources(id),
  external_id text not null,
  type text not null,
  name text,
  metadata_json text not null default '{}',
  unique(source_id, external_id)
);

create table if not exists items(
  id text primary key,
  source_id text not null references sources(id),
  collection_id text not null references collections(id),
  actor_id text references actors(id),
  external_id text not null,
  kind text not null,
  created_at text,
  updated_at text,
  text text,
  summary text,
  content_hash text not null,
  raw_json text not null,
  raw_hash text,
  raw_path text,
  raw_ordinal integer,
  metadata_json text not null default '{}',
  unique(source_id, collection_id, external_id, content_hash)
);

create table if not exists events(
  id text primary key,
  source_id text not null references sources(id),
  collection_id text not null references collections(id),
  actor_id text references actors(id),
  item_id text not null references items(id),
  kind text not null,
  occurred_at text,
  metadata_json text not null default '{}'
);

create table if not exists artifacts(
  id text primary key,
  source_id text not null references sources(id),
  item_id text references items(id),
  external_id text,
  kind text not null,
  path text,
  url text,
  mime_type text,
  text text,
  content_hash text,
  metadata_json text not null default '{}'
);

create table if not exists relations(
  id text primary key,
  source_item_id text not null,
  target_item_id text,
  target_external_id text,
  relation_type text not null,
  confidence real not null default 1.0,
  metadata_json text not null default '{}'
);

create table if not exists imports(
  id text primary key,
  source_kind text not null,
  source_path text,
  source_hash text not null,
  started_at text not null,
  completed_at text,
  item_count integer not null default 0,
  warning_count integer not null default 0,
  metadata_json text not null default '{}',
  unique(source_kind, source_hash)
);

create table if not exists import_warnings(
  import_id text not null references imports(id),
  ordinal integer not null,
  warning text not null,
  primary key(import_id, ordinal)
);

create table if not exists item_tags(
  item_id text not null references items(id),
  tag text not null,
  primary key(item_id, tag)
);

create table if not exists item_metadata(
  item_id text not null references items(id),
  key text not null,
  value text not null,
  primary key(item_id, key, value)
);

create table if not exists source_scans(
  id text primary key,
  source_kind text not null,
  path text not null,
  size integer not null,
  mtime text not null,
  content_hash text not null,
  generated_hash text not null,
  first_seen_at text not null,
  last_seen_at text not null,
  last_imported_at text,
  records_generated integer not null default 0,
  warnings integer not null default 0,
  unique(source_kind, path)
);

create virtual table if not exists item_fts using fts5(
  item_id unindexed,
  source_kind unindexed,
  collection_kind unindexed,
  item_kind unindexed,
  actor_type unindexed,
  body
);

create index if not exists idx_items_source_kind on items(source_id, kind, created_at);
create index if not exists idx_events_item on events(item_id);
create index if not exists idx_artifacts_item on artifacts(item_id);
create index if not exists idx_item_tags_tag on item_tags(tag);
create index if not exists idx_item_metadata_key_value on item_metadata(key, value);
create index if not exists idx_source_scans_kind on source_scans(source_kind, last_seen_at);
`
