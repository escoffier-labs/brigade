package archive

import (
	"database/sql"
	"fmt"
	"os"

	"github.com/openclaw/logspine/internal/security"
	_ "modernc.org/sqlite"
)

const SchemaVersion = 1

func Open(path string) (*sql.DB, error) {
	if err := security.EnsurePrivateParent(path); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec("PRAGMA foreign_keys = ON"); err != nil {
		_ = db.Close()
		return nil, err
	}
	if _, err := os.Stat(path); err == nil {
		_ = security.ChmodPrivateFile(path)
	}
	return db, nil
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
`
