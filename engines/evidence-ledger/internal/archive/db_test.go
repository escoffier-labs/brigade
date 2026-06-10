package archive

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
)

func openMigrated(t *testing.T) *sql.DB {
	t.Helper()
	path := filepath.Join(t.TempDir(), "miseledger.db")
	db, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	return db
}

func TestOpenAndMigrateSetsUserVersion(t *testing.T) {
	db := openMigrated(t)
	got, err := UserVersion(db)
	if err != nil {
		t.Fatalf("UserVersion: %v", err)
	}
	if got != SchemaVersion {
		t.Fatalf("user_version = %d, want %d", got, SchemaVersion)
	}
}

func TestMigrateIsIdempotent(t *testing.T) {
	db := openMigrated(t)
	if err := Migrate(db); err != nil {
		t.Fatalf("second Migrate failed: %v", err)
	}
}

func TestCoreTablesExist(t *testing.T) {
	db := openMigrated(t)
	for _, table := range []string{"sources", "collections", "actors", "items", "events", "artifacts", "relations", "imports", "item_fts"} {
		var name string
		err := db.QueryRow(
			"select name from sqlite_master where type in ('table','view') and name = ?",
			table,
		).Scan(&name)
		if err != nil {
			t.Fatalf("table %q not found after migrate: %v", table, err)
		}
	}
}

func TestForeignKeysEnforced(t *testing.T) {
	db := openMigrated(t)
	// items.source_id references sources(id); an orphan insert must fail with
	// PRAGMA foreign_keys = ON set by Open.
	_, err := db.Exec(
		`insert into items(id, source_id, collection_id, external_id, kind, content_hash, raw_json)
		 values('i1','missing-source','missing-collection','ext','message','h','{}')`,
	)
	if err == nil {
		t.Fatal("expected foreign key violation inserting orphan item")
	}
}

func TestHasFTS(t *testing.T) {
	db := openMigrated(t)
	if !HasFTS(db) {
		t.Fatal("HasFTS returned false; modernc.org/sqlite should support fts5")
	}
}

func TestUserVersionOnFreshDB(t *testing.T) {
	path := filepath.Join(t.TempDir(), "fresh.db")
	db, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()
	v, err := UserVersion(db)
	if err != nil {
		t.Fatalf("UserVersion: %v", err)
	}
	if v != 0 {
		t.Fatalf("fresh db user_version = %d, want 0 before migrate", v)
	}
}

func TestOpenAppliesPragmasToAllConnections(t *testing.T) {
	db, err := Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	// Exercise more than one pooled connection; DSN pragmas must hold on all
	// of them, unlike the old per-connection Exec.
	for i := 0; i < 4; i++ {
		var mode string
		if err := db.QueryRow("PRAGMA journal_mode").Scan(&mode); err != nil {
			t.Fatal(err)
		}
		if mode != "wal" {
			t.Fatalf("journal_mode = %q, want wal", mode)
		}
		var timeout int
		if err := db.QueryRow("PRAGMA busy_timeout").Scan(&timeout); err != nil {
			t.Fatal(err)
		}
		if timeout != 10000 {
			t.Fatalf("busy_timeout = %d, want 10000", timeout)
		}
		var fk int
		if err := db.QueryRow("PRAGMA foreign_keys").Scan(&fk); err != nil {
			t.Fatal(err)
		}
		if fk != 1 {
			t.Fatalf("foreign_keys = %d, want 1", fk)
		}
	}
}

func TestCheckpointTruncatesWALAndKeepsSidecarsPrivate(t *testing.T) {
	path := t.TempDir() + "/miseledger.db"
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := Migrate(db); err != nil {
		t.Fatal(err)
	}
	// Generate WAL traffic.
	if _, err := db.Exec(`insert into sources(id, kind, name, created_at, updated_at) values('s','k','n','t','t')`); err != nil {
		t.Fatal(err)
	}
	if err := Checkpoint(db, path); err != nil {
		t.Fatalf("checkpoint: %v", err)
	}
	// Any WAL/SHM sidecars that exist must be private (0600), not world-readable.
	for _, suffix := range []string{"", "-wal", "-shm"} {
		info, err := os.Stat(path + suffix)
		if err != nil {
			continue // sidecar may not exist after truncate; that is fine
		}
		if perm := info.Mode().Perm(); perm&0o077 != 0 {
			t.Fatalf("%s perms = %o, want no group/other access", path+suffix, perm)
		}
	}
}
