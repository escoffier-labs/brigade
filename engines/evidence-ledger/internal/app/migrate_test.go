package app

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestMigrateCodexArguments(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	db, _, err := openMigrated()
	if err != nil {
		t.Fatalf("failed to open test db: %s", err)
	}
	defer db.Close()

	// Insert dummy sources
	_, err = db.Exec(`INSERT INTO sources (id, kind, version, created_at, updated_at) VALUES ('s1', 'codex', 'v1', '2023-01-01', '2023-01-01')`)
	if err != nil {
		t.Fatalf("insert source: %s", err)
	}
	_, err = db.Exec(`INSERT INTO sources (id, kind, version, created_at, updated_at) VALUES ('s2', 'other', 'v1', '2023-01-01', '2023-01-01')`)
	if err != nil {
		t.Fatalf("insert source: %s", err)
	}
	_, err = db.Exec(`INSERT INTO collections (id, source_id, external_id, kind, created_at, updated_at) VALUES ('c1', 's1', 'c1-ext', 'session', '2023-01-01', '2023-01-01')`)
	if err != nil {
		t.Fatalf("insert collection: %s", err)
	}
	_, err = db.Exec(`INSERT INTO collections (id, source_id, external_id, kind, created_at, updated_at) VALUES ('c2', 's2', 'c2-ext', 'session', '2023-01-01', '2023-01-01')`)
	if err != nil {
		t.Fatalf("insert collection: %s", err)
	}

	largeArgs := strings.Repeat("a", 5000)

	// Item 1: codex with large arguments, needs migration
	raw1 := fmt.Sprintf(`{"payload":{"arguments":"%s"}}`, largeArgs)
	meta1 := fmt.Sprintf(`{"arguments":"%s"}`, largeArgs)
	_, err = db.Exec(`INSERT INTO items (id, source_id, collection_id, external_id, kind, created_at, content_hash, raw_json, metadata_json, ingest_seq) VALUES ('i1', 's1', 'c1', 'ext1', 'tool_call', '2023-01-01', 'h1', ?, ?, 1)`, raw1, meta1)
	if err != nil {
		t.Fatalf("insert i1: %s", err)
	}

	// Item 2: other source with large arguments (should NOT be migrated)
	raw2 := fmt.Sprintf(`{"payload":{"arguments":"%s"}}`, largeArgs)
	meta2 := fmt.Sprintf(`{"arguments":"%s"}`, largeArgs)
	_, err = db.Exec(`INSERT INTO items (id, source_id, collection_id, external_id, kind, created_at, content_hash, raw_json, metadata_json, ingest_seq) VALUES ('i2', 's2', 'c2', 'ext2', 'tool_call', '2023-01-01', 'h2', ?, ?, 2)`, raw2, meta2)
	if err != nil {
		t.Fatalf("insert i2: %s", err)
	}

	// Item 3: codex with small arguments (no truncation needed, but should strip from raw_json)
	raw3 := `{"payload":{"arguments":"small"}}`
	meta3 := `{"arguments":"small"}`
	_, err = db.Exec(`INSERT INTO items (id, source_id, collection_id, external_id, kind, created_at, content_hash, raw_json, metadata_json, ingest_seq) VALUES ('i3', 's1', 'c1', 'ext3', 'tool_call', '2023-01-01', 'h3', ?, ?, 3)`, raw3, meta3)
	if err != nil {
		t.Fatalf("insert i3: %s", err)
	}

	// Item 4: codex already migrated (arguments truncated, in meta, not in raw)
	raw4 := `{"payload":{"other":"data"}}`
	meta4 := `{"arguments":"truncated\n[truncated]","arguments_digest":"deadbeef"}`
	_, err = db.Exec(`INSERT INTO items (id, source_id, collection_id, external_id, kind, created_at, content_hash, raw_json, metadata_json, ingest_seq) VALUES ('i4', 's1', 'c1', 'ext4', 'tool_call', '2023-01-01', 'h4', ?, ?, 4)`, raw4, meta4)
	if err != nil {
		t.Fatalf("insert i4: %s", err)
	}

	// Run dry-run
	var out, errw bytes.Buffer
	code := cmdMigrateCodexArguments([]string{"--json"}, &out, &errw)
	if code != 0 {
		t.Fatalf("cmd failed: %s", errw.String())
	}
	var res map[string]any
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatalf("failed to decode json: %s\nOutput: %s", err, out.String())
	}

	if res["dry_run"] != true {
		t.Fatalf("expected dry-run = true")
	}
	matched := int(res["matched"].(float64))
	if matched != 2 {
		t.Fatalf("expected 2 matched (i1, i3), got %d", matched)
	}

	// Check i1 unchanged (dry-run)
	var r1, m1 string
	db.QueryRow(`SELECT raw_json, metadata_json FROM items WHERE id = 'i1'`).Scan(&r1, &m1)
	if !strings.Contains(r1, largeArgs) {
		t.Fatalf("dry-run mutated raw_json")
	}

	// Run apply
	out.Reset()
	code = cmdMigrateCodexArguments([]string{"--apply", "--json"}, &out, &errw)
	if code != 0 {
		t.Fatalf("apply failed: %s", errw.String())
	}
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatalf("decode err: %s", err)
	}
	if res["dry_run"] != false {
		t.Fatalf("expected dry-run = false")
	}
	matched = int(res["matched"].(float64))
	if matched != 2 {
		t.Fatalf("expected 2 matched, got %d", matched)
	}

	// Verify mutations
	db.QueryRow(`SELECT raw_json, metadata_json FROM items WHERE id = 'i1'`).Scan(&r1, &m1)
	if strings.Contains(r1, largeArgs) {
		t.Fatalf("raw_json still contains large arguments for i1")
	}
	if !strings.Contains(m1, "[truncated]") {
		t.Fatalf("metadata_json does not contain [truncated] for i1")
	}
	if !strings.Contains(m1, "arguments_digest") {
		t.Fatalf("metadata_json missing arguments_digest for i1")
	}

	db.QueryRow(`SELECT raw_json, metadata_json FROM items WHERE id = 'i2'`).Scan(&r1, &m1)
	if !strings.Contains(r1, largeArgs) {
		t.Fatalf("i2 should not be migrated")
	}

	db.QueryRow(`SELECT raw_json, metadata_json FROM items WHERE id = 'i3'`).Scan(&r1, &m1)
	if strings.Contains(r1, "small") {
		t.Fatalf("i3 raw_json should not contain 'small' anymore")
	}
	if strings.Contains(m1, "[truncated]") {
		t.Fatalf("i3 metadata_json should NOT be truncated")
	}

	// Run apply again (idempotent)
	out.Reset()
	code = cmdMigrateCodexArguments([]string{"--apply", "--json"}, &out, &errw)
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatalf("decode err: %s", err)
	}
	matched = int(res["matched"].(float64))
	if matched != 0 {
		t.Fatalf("expected 0 matched on second run, got %d", matched)
	}
}

// TestMigrateCommandRegistered goes through the command table, not the
// function, so a missing dispatcher entry fails here (it did on 2026-09-03).
func TestMigrateCommandRegistered(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	got := runJSON(t, "migrate", "codex-arguments", "--json")
	if got["dry_run"] != true {
		t.Fatalf("expected dry_run=true from the registered command, got %v", got)
	}
	out, errw := &bytes.Buffer{}, &bytes.Buffer{}
	if code := cmdMigrate(nil, out, errw); code == 0 {
		t.Fatalf("migrate without a target must fail, got exit 0")
	}
	if !strings.Contains(errw.String(), "usage: miseledger migrate codex-arguments") {
		t.Fatalf("expected usage text, got %q", errw.String())
	}
}
