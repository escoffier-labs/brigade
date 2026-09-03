package app

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
)

func TestMigrateCodexArguments(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	largeArgs := `{"cmd":"exec_prefix ` + strings.Repeat("a", 4200) + ` UNICORN_TOKEN_PAST_CAP"}`
	ordinal1 := int64(1)
	meta1 := map[string]any{
		"harness":    "codex",
		"event_type": "response_item",
		"session_id": "s1",
		"name":       "exec_command",
		"call_id":    "call-1",
		"arguments":  largeArgs,
	}
	meta1Bytes, _ := json.Marshal(meta1)
	text1 := "function_call\nexec_command\ncall-1\n" + largeArgs

	rec1 := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "codex", Name: "Codex Sessions", Version: "1.0.0"},
		Collection: adapter.Collection{
			ExternalID: "codex:session:s1",
			Kind:       "agent_session",
			Name:       "s1",
		},
		Item: adapter.Item{
			ExternalID: "codex:call:call-1",
			Kind:       "tool_call",
			CreatedAt:  "2026-07-14T19:29:50Z",
			Text:       text1,
			Tags:       []string{"agent-session", "codex"},
			Metadata:   json.RawMessage(meta1Bytes),
		},
		Actor: &adapter.Actor{
			ExternalID: "codex:assistant",
			Type:       "agent",
			Name:       "assistant",
		},
		Raw: adapter.RawRef{
			Format:  "json",
			Hash:    "sha256:preCapHash1",
			Path:    "/path/to/session1.jsonl",
			Ordinal: &ordinal1,
		},
	}

	ordinal3 := int64(3)
	smallArgs := `{"cmd":"ls -la"}`
	meta3 := map[string]any{
		"harness":    "codex",
		"event_type": "response_item",
		"session_id": "s1",
		"name":       "exec_command",
		"call_id":    "call-3",
		"arguments":  smallArgs,
	}
	meta3Bytes, _ := json.Marshal(meta3)
	text3 := "function_call\nexec_command\ncall-3\n" + smallArgs

	rec3 := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "codex", Name: "Codex Sessions", Version: "1.0.0"},
		Collection: adapter.Collection{
			ExternalID: "codex:session:s1",
			Kind:       "agent_session",
			Name:       "s1",
		},
		Item: adapter.Item{
			ExternalID: "codex:call:call-3",
			Kind:       "tool_call",
			CreatedAt:  "2026-07-14T19:29:52Z",
			Text:       text3,
			Tags:       []string{"agent-session", "codex"},
			Metadata:   json.RawMessage(meta3Bytes),
		},
		Actor: &adapter.Actor{
			ExternalID: "codex:assistant",
			Type:       "agent",
			Name:       "assistant",
		},
		Raw: adapter.RawRef{
			Format:  "json",
			Hash:    "sha256:preCapHash3",
			Path:    "/path/to/session1.jsonl",
			Ordinal: &ordinal3,
		},
	}

	ordinal4 := int64(4)
	migratedArgs := strings.Repeat("b", 4000) + "\n[truncated]"
	meta4 := map[string]any{
		"harness":          "codex",
		"event_type":       "response_item",
		"session_id":       "s1",
		"name":             "exec_command",
		"call_id":          "call-4",
		"arguments":        migratedArgs,
		"arguments_digest": "deadbeef1234",
	}
	meta4Bytes, _ := json.Marshal(meta4)
	text4 := "function_call\nexec_command\ncall-4\n" + migratedArgs

	rec4 := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "codex", Name: "Codex Sessions", Version: "1.0.0"},
		Collection: adapter.Collection{
			ExternalID: "codex:session:s1",
			Kind:       "agent_session",
			Name:       "s1",
		},
		Item: adapter.Item{
			ExternalID: "codex:call:call-4",
			Kind:       "tool_call",
			CreatedAt:  "2026-07-14T19:29:54Z",
			Text:       text4,
			Tags:       []string{"agent-session", "codex"},
			Metadata:   json.RawMessage(meta4Bytes),
		},
		Actor: &adapter.Actor{
			ExternalID: "codex:assistant",
			Type:       "agent",
			Name:       "assistant",
		},
		Raw: adapter.RawRef{
			Format:  "json",
			Hash:    "sha256:preCapHash4",
			Path:    "/path/to/session1.jsonl",
			Ordinal: &ordinal4,
		},
	}

	otherLargeArgs := `{"cmd":"other_cmd ` + strings.Repeat("z", 4500) + `"}`
	ordinal2 := int64(2)
	meta2 := map[string]any{
		"harness":   "other",
		"arguments": otherLargeArgs,
	}
	meta2Bytes, _ := json.Marshal(meta2)
	text2 := "other_call\n" + otherLargeArgs

	rec2 := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "other", Name: "Other System", Version: "1.0.0"},
		Collection: adapter.Collection{
			ExternalID: "other:session:s2",
			Kind:       "agent_session",
			Name:       "s2",
		},
		Item: adapter.Item{
			ExternalID: "other:call:call-2",
			Kind:       "tool_call",
			CreatedAt:  "2026-07-14T19:29:51Z",
			Text:       text2,
			Tags:       []string{"other"},
			Metadata:   json.RawMessage(meta2Bytes),
		},
		Actor: &adapter.Actor{
			ExternalID: "other:agent",
			Type:       "agent",
			Name:       "agent",
		},
		Raw: adapter.RawRef{
			Format:  "json",
			Hash:    "sha256:preCapHash2",
			Path:    "/path/to/session2.jsonl",
			Ordinal: &ordinal2,
		},
	}

	tempDir := t.TempDir()
	rec1Bytes, _ := json.Marshal(rec1)
	rec3Bytes, _ := json.Marshal(rec3)
	rec4Bytes, _ := json.Marshal(rec4)
	codexPath := filepath.Join(tempDir, "codex.adapter.jsonl")
	if err := os.WriteFile(codexPath, []byte(string(rec1Bytes)+"\n"+string(rec3Bytes)+"\n"+string(rec4Bytes)+"\n"), 0644); err != nil {
		t.Fatal(err)
	}

	rec2Bytes, _ := json.Marshal(rec2)
	otherPath := filepath.Join(tempDir, "other.adapter.jsonl")
	if err := os.WriteFile(otherPath, []byte(string(rec2Bytes)+"\n"), 0644); err != nil {
		t.Fatal(err)
	}

	runOK(t, "import", "adapter", codexPath, "--source", "codex")
	runOK(t, "import", "adapter", otherPath, "--source", "other")

	db, _, err := openMigrated()
	if err != nil {
		t.Fatalf("failed to open test db: %s", err)
	}
	defer db.Close()

	// Verify FTS before migration contains token past cap
	var countBefore int
	if err := db.QueryRow(`SELECT count(*) FROM item_fts WHERE item_fts MATCH 'UNICORN_TOKEN_PAST_CAP'`).Scan(&countBefore); err != nil {
		t.Fatalf("fts query: %s", err)
	}
	if countBefore != 1 {
		t.Fatalf("expected 1 match for UNICORN_TOKEN_PAST_CAP before migration, got %d", countBefore)
	}
	var prefixCountBefore int
	if err := db.QueryRow(`SELECT count(*) FROM item_fts WHERE item_fts MATCH 'exec_prefix'`).Scan(&prefixCountBefore); err != nil {
		t.Fatalf("fts query: %s", err)
	}
	if prefixCountBefore < 1 {
		t.Fatalf("expected match for exec_prefix before migration")
	}

	// Verify IntegrityMismatch == false before migration for rec1
	var rec1ID string
	if err := db.QueryRow(`SELECT id FROM items WHERE external_id = 'codex:call:call-1'`).Scan(&rec1ID); err != nil {
		t.Fatal(err)
	}
	viewBefore, err := inspectStoredItem(db, rec1ID)
	if err != nil {
		t.Fatalf("inspectStoredItem before: %s", err)
	}
	if viewBefore.IntegrityMismatch {
		t.Fatalf("expected IntegrityMismatch == false before migration, got mismatches: %+v", viewBefore.Mismatches)
	}

	// Verify --apply and --dry-run together fail with usage
	var errw bytes.Buffer
	code := cmdMigrateCodexArguments([]string{"--apply", "--dry-run"}, io.Discard, &errw)
	if code == 0 {
		t.Fatalf("expected non-zero exit for --apply --dry-run together")
	}
	if !strings.Contains(errw.String(), "usage: miseledger migrate codex-arguments") {
		t.Fatalf("expected usage message, got: %s", errw.String())
	}

	// Verify --help documents full-table LIKE scan
	var helpOut bytes.Buffer
	code = cmdMigrateCodexArguments([]string{"--help"}, &helpOut, io.Discard)
	if code != 0 {
		t.Fatalf("expected 0 exit for --help")
	}
	if !strings.Contains(helpOut.String(), "LIKE scan") {
		t.Fatalf("expected help to document full-table LIKE scan, got: %s", helpOut.String())
	}

	// Run dry-run
	var out bytes.Buffer
	errw.Reset()
	code = cmdMigrateCodexArguments([]string{"--json"}, &out, &errw)
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
	if matched != 1 {
		t.Fatalf("expected 1 matched (only rec1), got %d", matched)
	}

	// Check rec1 unchanged (dry-run)
	var r1, m1, t1 string
	if err := db.QueryRow(`SELECT raw_json, metadata_json, text FROM items WHERE external_id = 'codex:call:call-1'`).Scan(&r1, &m1, &t1); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(r1, "UNICORN_TOKEN_PAST_CAP") || !strings.Contains(m1, "UNICORN_TOKEN_PAST_CAP") || !strings.Contains(t1, "UNICORN_TOKEN_PAST_CAP") {
		t.Fatalf("dry-run mutated row 1")
	}
	var parsedM1Dry map[string]any
	if err := json.Unmarshal([]byte(m1), &parsedM1Dry); err != nil || parsedM1Dry["arguments"] != largeArgs {
		t.Fatalf("dry-run mutated metadata_json: %v", parsedM1Dry["arguments"])
	}

	// Run apply
	out.Reset()
	errw.Reset()
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
	if matched != 1 {
		t.Fatalf("expected 1 matched, got %d", matched)
	}

	// Verify mutations on row 1
	if err := db.QueryRow(`SELECT raw_json, metadata_json, text FROM items WHERE external_id = 'codex:call:call-1'`).Scan(&r1, &m1, &t1); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(r1, "UNICORN_TOKEN_PAST_CAP") {
		t.Fatalf("raw_json still contains UNICORN_TOKEN_PAST_CAP for rec1")
	}
	if strings.Contains(m1, "UNICORN_TOKEN_PAST_CAP") {
		t.Fatalf("metadata_json still contains UNICORN_TOKEN_PAST_CAP for rec1")
	}
	if strings.Contains(t1, "UNICORN_TOKEN_PAST_CAP") {
		t.Fatalf("text still contains UNICORN_TOKEN_PAST_CAP for rec1")
	}
	if !strings.Contains(m1, "[truncated]") {
		t.Fatalf("metadata_json does not contain [truncated] for rec1")
	}
	if !strings.Contains(t1, "[truncated]") {
		t.Fatalf("text does not contain [truncated] for rec1")
	}
	expectedDigest := sha256.Sum256([]byte(largeArgs))
	expectedDigestHex := hex.EncodeToString(expectedDigest[:])
	if !strings.Contains(m1, expectedDigestHex) {
		t.Fatalf("metadata_json missing arguments_digest for rec1")
	}

	// Verify raw_json shape matches adapter.Record and raw fields untouched
	var rec1After adapter.Record
	if err := json.Unmarshal([]byte(r1), &rec1After); err != nil {
		t.Fatalf("unmarshal rec1 raw_json as adapter.Record: %s", err)
	}
	if rec1After.Raw.Format != "json" || rec1After.Raw.Hash != "sha256:preCapHash1" || rec1After.Raw.Path != "/path/to/session1.jsonl" || *rec1After.Raw.Ordinal != 1 {
		t.Fatalf("raw field was mutated: %+v", rec1After.Raw)
	}
	var rec1MetaAfter map[string]any
	if err := json.Unmarshal(rec1After.Item.Metadata, &rec1MetaAfter); err != nil {
		t.Fatalf("unmarshal rec1 Item.Metadata: %s", err)
	}
	if !strings.Contains(rec1MetaAfter["arguments"].(string), "[truncated]") {
		t.Fatalf("rec1 Item.Metadata arguments not truncated: %v", rec1MetaAfter["arguments"])
	}
	if rec1MetaAfter["arguments_digest"] != expectedDigestHex {
		t.Fatalf("rec1 Item.Metadata arguments_digest mismatch: %v", rec1MetaAfter["arguments_digest"])
	}
	if !strings.Contains(rec1After.Item.Text, "[truncated]") {
		t.Fatalf("rec1 Item.Text not truncated")
	}
	if strings.Contains(rec1After.Item.Text, "UNICORN_TOKEN_PAST_CAP") {
		t.Fatalf("rec1 Item.Text still contains UNICORN_TOKEN_PAST_CAP")
	}

	// Verify inspectStoredItem(db, id).IntegrityMismatch == false after migration
	viewAfter, err := inspectStoredItem(db, rec1ID)
	if err != nil {
		t.Fatalf("inspectStoredItem after: %s", err)
	}
	if viewAfter.IntegrityMismatch {
		t.Fatalf("expected inspectStoredItem(db, id).IntegrityMismatch == false after migration, got mismatches: %+v", viewAfter.Mismatches)
	}

	// Verify metadata_json decoded with UseNumber preserves integer types
	var parsedM1Apply map[string]any
	decM1 := json.NewDecoder(bytes.NewReader([]byte(m1)))
	decM1.UseNumber()
	if err := decM1.Decode(&parsedM1Apply); err != nil {
		t.Fatalf("decode m1: %s", err)
	}
	provMap, ok := parsedM1Apply["provenance"].(map[string]any)
	if !ok {
		t.Fatalf("expected provenance map in metadata_json")
	}
	if ver, ok := provMap["schema_version"].(json.Number); !ok || ver.String() != "1" {
		t.Fatalf("expected schema_version json.Number 1, got %v (%T)", provMap["schema_version"], provMap["schema_version"])
	}

	// Verify row 2 (other source) untouched
	var r2, m2, t2 string
	if err := db.QueryRow(`SELECT raw_json, metadata_json, text FROM items WHERE external_id = 'other:call:call-2'`).Scan(&r2, &m2, &t2); err != nil {
		t.Fatal(err)
	}
	var parsedM2 map[string]any
	if err := json.Unmarshal([]byte(m2), &parsedM2); err != nil || parsedM2["arguments"] != otherLargeArgs {
		t.Fatalf("rec2 should not be migrated, got %v", parsedM2["arguments"])
	}

	// Verify row 3 (small arguments) untouched
	var r3, m3, t3 string
	if err := db.QueryRow(`SELECT raw_json, metadata_json, text FROM items WHERE external_id = 'codex:call:call-3'`).Scan(&r3, &m3, &t3); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(r3, "small") && !strings.Contains(r3, "ls -la") {
		t.Fatalf("rec3 raw_json missing small arguments")
	}
	if strings.Contains(m3, "[truncated]") || strings.Contains(t3, "[truncated]") {
		t.Fatalf("rec3 should NOT be truncated")
	}

	// Verify FTS after migration: token past cap must be gone, token before cap remains
	var countAfter int
	if err := db.QueryRow(`SELECT count(*) FROM item_fts WHERE item_fts MATCH 'UNICORN_TOKEN_PAST_CAP'`).Scan(&countAfter); err != nil {
		t.Fatalf("fts query after migration: %s", err)
	}
	if countAfter != 0 {
		t.Fatalf("expected 0 matches for UNICORN_TOKEN_PAST_CAP after migration, got %d", countAfter)
	}
	var prefixCountAfter int
	if err := db.QueryRow(`SELECT count(*) FROM item_fts WHERE item_fts MATCH 'exec_prefix'`).Scan(&prefixCountAfter); err != nil {
		t.Fatalf("fts query after migration: %s", err)
	}
	if prefixCountAfter < 1 {
		t.Fatalf("expected exec_prefix to still match in FTS after migration")
	}

	// Run apply again (idempotent)
	out.Reset()
	errw.Reset()
	code = cmdMigrateCodexArguments([]string{"--apply", "--json"}, &out, &errw)
	if code != 0 {
		t.Fatalf("second apply failed: %s", errw.String())
	}
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
	out.Reset()
	if code := cmdMigrate([]string{"--help"}, out, errw); code != 0 {
		t.Fatalf("migrate --help must exit 0, got %d", code)
	}
	if !strings.Contains(out.String(), "LIKE scan") {
		t.Fatalf("expected LIKE scan in help, got %s", out.String())
	}
}
