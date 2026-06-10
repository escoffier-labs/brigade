package ingest

import (
	"strconv"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
)

func TestImportAdapterReaderIdempotent(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:1","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"streaming adapter reader import","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
	first, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test")
	if err != nil {
		t.Fatal(err)
	}
	if first.Inserted != 1 || first.AlreadyKnown {
		t.Fatalf("first import = %+v, want inserted 1 and not already known", first)
	}
	second, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test")
	if err != nil {
		t.Fatal(err)
	}
	if second.Inserted != 0 || !second.AlreadyKnown {
		t.Fatalf("second import = %+v, want inserted 0 and already known", second)
	}
	var items int
	if err := db.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 1 {
		t.Fatalf("items = %d, want 1", items)
	}
}

func TestImportAdapterReaderLargeImportDoesNotSelfDeadlock(t *testing.T) {
	// Regression: imports large enough to spill SQLite's page cache made the
	// write transaction take an exclusive lock; the already-known check then
	// read through a second pooled connection and failed instantly with
	// SQLITE_BUSY, so every real-world import errored and rolled back.
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	padding := strings.Repeat("evidence text that occupies cache pages ", 64)
	var b strings.Builder
	for i := 0; i < 3000; i++ {
		b.WriteString(`{"schema":"miseledger.adapter.v1","source":{"kind":"bulk-test","name":"Bulk Test"},"collection":{"external_id":"bulk:collection","kind":"agent_session","name":"bulk"},"item":{"external_id":"bulk:item:`)
		b.WriteString(strconv.Itoa(i))
		b.WriteString(`","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"`)
		b.WriteString(padding)
		b.WriteString(`","tags":["bulk"]},"actor":{"external_id":"bulk:actor","type":"human","name":"bulk"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"bulk.jsonl","ordinal":`)
		b.WriteString(strconv.Itoa(i + 1))
		b.WriteString(`}}`)
		b.WriteString("\n")
	}
	result, err := ImportAdapterReader(db, strings.NewReader(b.String()), "bulk://fixture", "bulk-test")
	if err != nil {
		t.Fatalf("large import failed: %s", err)
	}
	if result.Inserted != 3000 {
		t.Fatalf("inserted = %d, want 3000", result.Inserted)
	}
	var items int
	if err := db.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 3000 {
		t.Fatalf("items = %d, want 3000", items)
	}
}
