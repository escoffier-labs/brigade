package ingest

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"
	"testing"
	"testing/iotest"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/sources/opencode"
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

// Regression: an adapter line beyond the 10MB scanner limit used to abort the
// whole import with bufio.Scanner: token too long. It must be skipped with a
// warning while surrounding records still import.
func TestImportAdapterReaderSkipsOversizedLine(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	record := func(id, text string) string {
		return `{"schema":"miseledger.adapter.v1","source":{"kind":"oversize-test","name":"Oversize Test"},"collection":{"external_id":"oversize:collection","kind":"agent_session","name":"oversize"},"item":{"external_id":"oversize:item:` + id + `","kind":"message","created_at":"2026-07-14T00:00:00Z","text":"` + text + `","tags":["oversize"]},"actor":{"external_id":"oversize:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"oversize.jsonl","ordinal":1}}`
	}
	jsonl := record("1", "before") + "\n" +
		record("huge", strings.Repeat("a", sources.MaxLineBytes+1024)) + "\n" +
		record("2", "after") + "\n"
	res, err := ImportAdapterReader(db, strings.NewReader(jsonl), "oversize://fixture", "oversize-test")
	if err != nil {
		t.Fatalf("import must not abort on an oversized line: %v", err)
	}
	if res.Inserted != 2 {
		t.Fatalf("inserted = %d, want 2", res.Inserted)
	}
	var warned bool
	for _, w := range res.Warnings {
		if strings.Contains(w, "line too long") {
			warned = true
		}
	}
	if !warned {
		t.Fatalf("expected a line-too-long warning, got %d warnings", len(res.Warnings))
	}
}

func TestImportAdapterReaderPreservesSchemaConformantCodeReferencesInItemMetadata(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	metadata, err := json.Marshal(map[string]any{"code_references": []map[string]any{testCodeReference()}})
	if err != nil {
		t.Fatal(err)
	}
	record := map[string]any{
		"schema":     "miseledger.adapter.v1",
		"source":     map[string]any{"kind": "code-reference-test", "name": "Code reference test"},
		"collection": map[string]any{"external_id": "code-reference:collection", "kind": "agent_session", "name": "code reference"},
		"item": map[string]any{
			"external_id": "code-reference:item:1",
			"kind":        "message",
			"created_at":  "2026-07-19T00:00:00Z",
			"text":        "stored through the existing metadata surface",
			"tags":        []string{"code-reference"},
			"metadata":    json.RawMessage(metadata),
		},
		"actor":     map[string]any{"external_id": "code-reference:actor", "type": "agent", "name": "fixture"},
		"artifacts": []any{}, "links": []any{}, "relations": []any{},
		"raw": map[string]any{"format": "json", "path": "code-reference.jsonl", "ordinal": 1},
	}
	line, err := json.Marshal(record)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(string(line)+"\n"), "code-reference://fixture", "code-reference-test"); err != nil {
		t.Fatal(err)
	}
	var raw string
	if err := db.QueryRow(`select metadata_json from items where external_id = 'code-reference:item:1'`).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var stored map[string]any
	if err := json.Unmarshal([]byte(raw), &stored); err != nil {
		t.Fatal(err)
	}
	if got := stored["code_references"]; fmt.Sprint(got) != fmt.Sprint([]any{testCodeReference()}) {
		t.Fatalf("code_references = %#v, want %#v", got, []any{testCodeReference()})
	}
}

func TestImportAdapterReaderWarnsOnSourceOverrideMismatch(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	result, err := ImportAdapterReader(db, strings.NewReader(adapterRecord("discord", "discord:item:1", "source override mismatch", "discord.jsonl", 1)), "discord://fixture", "discrawl")
	if err != nil {
		t.Fatal(err)
	}
	if result.SourceKind != "discrawl" {
		t.Fatalf("source kind = %q, want discrawl", result.SourceKind)
	}
	if len(result.Warnings) != 1 {
		t.Fatalf("warnings = %#v, want one source override mismatch warning", result.Warnings)
	}
	if warning := result.Warnings[0]; !strings.Contains(warning, `--source "discrawl"`) || !strings.Contains(warning, `source.kind "discord"`) {
		t.Fatalf("warning %q does not name both source kinds", warning)
	}
	var sourceKind string
	if err := db.QueryRow(`select sources.kind from items join sources on sources.id = items.source_id limit 1`).Scan(&sourceKind); err != nil {
		t.Fatal(err)
	}
	if sourceKind != "discrawl" {
		t.Fatalf("stored source_kind = %q, want discrawl", sourceKind)
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

func TestNativeReaderRecordsCommittedFileScanBeforeInterruptedImport(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	b.WriteString(adapterRecord("native-scan", "file1:item:1", "first file committed before interruption", "file1.jsonl", 1))
	if err := WriteSourceScanSentinel(&b, sources.FileScan{
		Path:        "file1.jsonl",
		Size:        100,
		MTime:       "2026-06-03T00:00:00Z",
		ContentHash: "sha256:file1",
		Records:     1,
	}); err != nil {
		t.Fatal(err)
	}
	// Interrupt the stream with a read error after the sentinel. (This used to
	// be an 11MB line tripping bufio.Scanner's limit; oversized lines are now
	// skipped with a warning instead of aborting, so they no longer interrupt.)
	interrupted := io.MultiReader(strings.NewReader(b.String()), iotest.ErrReader(errors.New("simulated interruption")))

	_, err = ImportNativeReaderProgress(db, interrupted, "native://fixture", "native-scan", nil, func(sourceKind, generatedHash string, file sources.FileScan) error {
		return RecordSourceScans(db, sourceKind, generatedHash, []sources.FileScan{file}, true)
	})
	if err == nil {
		t.Fatal("interrupted import returned nil error")
	}
	var scans int
	if err := db.QueryRow(`select count(*) from source_scans where source_kind = 'native-scan' and path = 'file1.jsonl' and records_generated = 1`).Scan(&scans); err != nil {
		t.Fatal(err)
	}
	if scans != 1 {
		t.Fatalf("committed file scan rows = %d, want 1", scans)
	}
	var items int
	if err := db.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 1 {
		t.Fatalf("items = %d, want the sentinel-flushed record to survive interruption", items)
	}
}

func TestNativeReaderDoesNotRecordScanForUncommittedFile(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	// Interrupt with a read error before any file-complete sentinel arrives
	// (formerly an 11MB line tripping bufio.Scanner's limit).
	stream := io.MultiReader(
		strings.NewReader(adapterRecord("native-uncommitted", "file1:item:1", "uncommitted file record", "file1.jsonl", 1)),
		iotest.ErrReader(errors.New("simulated interruption")),
	)
	_, err = ImportNativeReaderProgress(db, stream, "native://fixture", "native-uncommitted", nil, func(sourceKind, generatedHash string, file sources.FileScan) error {
		return RecordSourceScans(db, sourceKind, generatedHash, []sources.FileScan{file}, true)
	})
	if err == nil {
		t.Fatal("interrupted import returned nil error")
	}
	var scans int
	if err := db.QueryRow(`select count(*) from source_scans where source_kind = 'native-uncommitted'`).Scan(&scans); err != nil {
		t.Fatal(err)
	}
	if scans != 0 {
		t.Fatalf("source_scans = %d, want 0 for records that never reached a file-complete sentinel", scans)
	}
	var items int
	if err := db.QueryRow(`select count(*) from items`).Scan(&items); err != nil {
		t.Fatal(err)
	}
	if items != 0 {
		t.Fatalf("items = %d, want open batch rolled back", items)
	}
}

func TestExternalAdapterReaderDoesNotHonorSourceScanSentinel(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	var b strings.Builder
	b.WriteString(adapterRecord("external-sentinel", "item:1", "external adapter record", "adapter.jsonl", 1))
	if err := WriteSourceScanSentinel(&b, sources.FileScan{
		Path:        "injected.jsonl",
		Size:        1,
		MTime:       "2026-06-03T00:00:00Z",
		ContentHash: "sha256:injected",
		Records:     1,
	}); err != nil {
		t.Fatal(err)
	}
	result, err := ImportAdapterReader(db, strings.NewReader(b.String()), "adapter://fixture", "external-sentinel")
	if err != nil {
		t.Fatal(err)
	}
	if result.Inserted != 1 {
		t.Fatalf("inserted = %d, want 1", result.Inserted)
	}
	if len(result.Warnings) == 0 {
		t.Fatalf("external sentinel produced no warning: %+v", result)
	}
	var scans int
	if err := db.QueryRow(`select count(*) from source_scans`).Scan(&scans); err != nil {
		t.Fatal(err)
	}
	if scans != 0 {
		t.Fatalf("external adapter created %d source scan rows, want 0", scans)
	}
}

func TestImportOpenCodeGeneratedAdapterRecords(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	var adapterJSONL bytes.Buffer
	generated, err := opencode.Generate("../../testdata/harnesses/opencode-export.fixture.json", sources.Options{}, &adapterJSONL)
	if err != nil {
		t.Fatalf("generate opencode fixture: %v", err)
	}
	if generated.Records != 2 {
		t.Fatalf("generated records = %d, want 2", generated.Records)
	}
	result, err := ImportAdapterReader(db, &adapterJSONL, "opencode://fixture", "opencode")
	if err != nil {
		t.Fatal(err)
	}
	if result.Inserted != 2 || result.SourceKind != "opencode" {
		t.Fatalf("import result = %+v, want 2 opencode items", result)
	}
	var sources int
	if err := db.QueryRow(`select count(*) from sources where kind = 'opencode'`).Scan(&sources); err != nil {
		t.Fatal(err)
	}
	if sources != 1 {
		t.Fatalf("opencode sources = %d, want 1", sources)
	}
}

func adapterRecord(sourceKind, externalID, text, rawPath string, ordinal int) string {
	return fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":%q,"name":"Test"},"collection":{"external_id":"collection","kind":"agent_session","name":"collection"},"item":{"external_id":%q,"kind":"message","created_at":"2026-06-03T00:00:00Z","text":%q,"tags":["test"]},"actor":{"external_id":"actor","type":"human","name":"actor"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":%q,"ordinal":%d}}`+"\n", sourceKind, externalID, text, rawPath, ordinal)
}

func testCodeReference() map[string]any {
	return map[string]any{
		"schema":         "brigade.code-reference.v1",
		"repository":     "escoffier-labs/brigade",
		"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
		"file_path":      "src/brigade/receipts_cmd.py",
		"qualified_name": "brigade.receipts_cmd._metadata_with_delta",
		"symbol_kind":    "function",
		"source_span":    map[string]any{"start_line": 787, "line_count": 3},
		"change_kind":    "changed",
	}
}
