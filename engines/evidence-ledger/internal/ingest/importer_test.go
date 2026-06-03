package ingest

import (
	"strings"
	"testing"

	"github.com/openclaw/logspine/internal/archive"
)

func TestImportAdapterReaderIdempotent(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/logspine.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"logspine.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:1","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"streaming adapter reader import","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
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
