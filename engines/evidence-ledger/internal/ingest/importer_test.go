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

func TestReimportSkipsWritesForKnownItems(t *testing.T) {
	// Re-importing already-known items must not re-run the sources/collections
	// upserts (which bump updated_at). A partial import that is retried should
	// fast-path over the committed prefix instead of rewriting it, so a capped
	// retry makes real forward progress instead of grinding the prefix.
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"skip-test","name":"Skip Test"},"collection":{"external_id":"skip:collection","kind":"agent_session","name":"skip"},"item":{"external_id":"skip:item:1","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"skip write test","tags":["skip"]},"actor":{"external_id":"skip:actor","type":"human","name":"skip"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"skip.jsonl","ordinal":1}}` + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "skip://fixture", "skip-test"); err != nil {
		t.Fatal(err)
	}
	var srcUpdated, colUpdated string
	if err := db.QueryRow(`select updated_at from sources limit 1`).Scan(&srcUpdated); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select updated_at from collections limit 1`).Scan(&colUpdated); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "skip://fixture", "skip-test"); err != nil {
		t.Fatal(err)
	}
	var srcUpdated2, colUpdated2 string
	if err := db.QueryRow(`select updated_at from sources limit 1`).Scan(&srcUpdated2); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select updated_at from collections limit 1`).Scan(&colUpdated2); err != nil {
		t.Fatal(err)
	}
	if srcUpdated2 != srcUpdated {
		t.Fatalf("sources.updated_at changed on re-import (%q -> %q); known items should skip the source upsert", srcUpdated, srcUpdated2)
	}
	if colUpdated2 != colUpdated {
		t.Fatalf("collections.updated_at changed on re-import (%q -> %q); known items should skip the collection upsert", colUpdated, colUpdated2)
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

func TestImportAdapterReaderBatchedProgressAndResume(t *testing.T) {
	// More than one batch (importBatchSize=1000) so commits happen mid-stream.
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	build := func(n int) string {
		var b strings.Builder
		for i := 0; i < n; i++ {
			b.WriteString(`{"schema":"miseledger.adapter.v1","source":{"kind":"batch-test","name":"Batch"},"collection":{"external_id":"batch:c","kind":"agent_session","name":"batch"},"item":{"external_id":"batch:item:`)
			b.WriteString(strconv.Itoa(i))
			b.WriteString(`","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"batch record `)
			b.WriteString(strconv.Itoa(i))
			b.WriteString(`","tags":["batch"]},"actor":{"external_id":"batch:a","type":"human","name":"batch"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"batch.jsonl","ordinal":`)
			b.WriteString(strconv.Itoa(i + 1))
			b.WriteString(`}}` + "\n")
		}
		return b.String()
	}

	var progressCalls int
	res, err := ImportAdapterReaderProgress(db, strings.NewReader(build(2500)), "batch://fixture", "batch-test", func(int) { progressCalls++ })
	if err != nil {
		t.Fatalf("batched import failed: %s", err)
	}
	if res.Inserted != 2500 {
		t.Fatalf("inserted = %d, want 2500", res.Inserted)
	}
	if progressCalls < 2 {
		t.Fatalf("progress callbacks = %d, want at least 2 across batches", progressCalls)
	}

	// Re-import identical content: idempotent, already-known, inserts nothing.
	again, err := ImportAdapterReader(db, strings.NewReader(build(2500)), "batch://fixture", "batch-test")
	if err != nil {
		t.Fatalf("re-import failed: %s", err)
	}
	if again.Inserted != 0 || !again.AlreadyKnown {
		t.Fatalf("re-import = %+v, want inserted 0 already-known", again)
	}
	var items int
	if err := db.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 2500 {
		t.Fatalf("items = %d, want 2500 (no duplication across batches)", items)
	}
}
