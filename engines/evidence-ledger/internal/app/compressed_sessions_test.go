package app

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/klauspost/compress/zstd"
)

func TestSourcesDiscoverCodexCompressedSessions(t *testing.T) {
	withTempHome(t)
	root := filepath.Join(os.Getenv("HOME"), ".codex", "sessions")
	content := []byte(`{"type":"event_msg","payload":{"message":"synthetic fixture"}}` + "\n")
	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderConcurrency(1))
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.Close()
	for _, name := range []string{
		"nested/session.jsonl.zst",
		"session.metadata.jsonl.zst", "session.sidecar.jsonl.zst",
		"session.backup.jsonl.zst", "session.bak.jsonl.zst", "session.deleted.jsonl.zst",
		"backups/session.jsonl.zst", "deleted/session.jsonl.zst",
	} {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, encoder.EncodeAll(content, nil), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, plainTwin := range []bool{false, true} {
		if plainTwin {
			if err := os.WriteFile(filepath.Join(root, "nested", "session.jsonl"), content, 0o600); err != nil {
				t.Fatal(err)
			}
		}
		found := false
		for _, item := range runJSONArray(t, "sources", "discover", "--json") {
			row := item.(map[string]any)
			if row["source_kind"] == "codex" {
				found = true
				if row["exists"] != true || row["count"] != float64(1) {
					t.Fatalf("Codex discovery (plain twin=%t) = %v, want one session", plainTwin, row)
				}
			}
		}
		if !found {
			t.Fatal("Codex discovery row missing")
		}
	}
}

func TestNonCodexCompressedSessionsExcluded(t *testing.T) {
	for _, harness := range []string{"claude", "openclaw", "pi"} {
		t.Run(harness, func(t *testing.T) {
			withTempHome(t)
			runOK(t, "init")
			dir := t.TempDir()
			plain := filepath.Join(dir, "session.jsonl")
			// No explicit session ID: compression must not split the collection.
			content := []byte(`{"type":"message","text":"alpha event","message":{"role":"user","content":"alpha event"}}` + "\n")
			if err := os.WriteFile(plain, content, 0o600); err != nil {
				t.Fatal(err)
			}
			first := runJSON(t, "import", harness, dir, "--json")
			if first["inserted_items"].(float64) != 1 {
				t.Fatalf("initial plain import = %v", first)
			}
			before := runJSON(t, "status", "--json")
			encoder, err := zstd.NewWriter(nil)
			if err != nil {
				t.Fatal(err)
			}
			defer encoder.Close()
			compressed := plain + ".zst"
			if err := os.WriteFile(compressed, encoder.EncodeAll(content, nil), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Remove(plain); err != nil {
				t.Fatal(err)
			}
			// Both recursive discovery and explicit inputs must ignore unsupported
			// compressed sessions, including repeat imports after replacement.
			for _, path := range []string{dir, compressed, dir} {
				result := runJSON(t, "import", harness, path, "--json")
				if result["files_parsed"].(float64) != 0 || result["files_skipped"].(float64) != 0 || result["inserted_items"].(float64) != 0 {
					t.Errorf("unsupported compressed import %q = %v", path, result)
				}
			}
			after := runJSON(t, "status", "--json")
			if before["items"] != after["items"] {
				t.Errorf("compression duplicated archive rows: before=%v after=%v", before, after)
			}
			collections := runJSON(t, "sql", "select count(*) as n from collections", "--json")
			if collections["rows"].([]any)[0].(map[string]any)["n"].(float64) != 1 {
				t.Errorf("compression split the collection: %v", collections)
			}
		})
	}
}

func TestCodexCompressedSessionImportIdentity(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	plain := filepath.Join(dir, "session.jsonl")
	// Typical message rows have no session ID and use the filename fallback.
	content := []byte("{\"type\":\"event_msg\",\"payload\":{\"role\":\"user\",\"message\":\"alpha event\"}}\n")
	if err := os.WriteFile(plain, content, 0o600); err != nil {
		t.Fatal(err)
	}
	first := runJSON(t, "import", "codex", dir, "--json")
	if first["inserted_items"].(float64) != 1 {
		t.Fatalf("initial import = %v", first)
	}
	countsQuery := "select (select count(*) from items) as items, (select count(*) from collections) as collections"
	before := runJSON(t, "sql", countsQuery, "--json")["rows"].([]any)[0].(map[string]any)
	encoder, err := zstd.NewWriter(nil)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.Close()
	compressed := plain + ".zst"
	write := func(data []byte) {
		t.Helper()
		if err := os.WriteFile(compressed, encoder.EncodeAll(data, nil), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(content)
	if err := os.Remove(plain); err != nil {
		t.Fatal(err)
	}
	second := runJSON(t, "import", "codex", dir, "--json")
	if second["files_parsed"].(float64) != 1 || second["inserted_items"].(float64) != 0 {
		t.Fatalf("compressed replacement import = %v", second)
	}
	after := runJSON(t, "sql", countsQuery, "--json")["rows"].([]any)[0].(map[string]any)
	if before["items"] != after["items"] || before["collections"] != after["collections"] {
		t.Fatalf("compression duplicated archive rows: before=%v after=%v", before, after)
	}
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	info, err := os.Stat(compressed)
	if err != nil {
		t.Fatal(err)
	}
	hash, err := sources.FileHash(compressed)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, item := range scans["scans"].([]any) {
		row := item.(map[string]any)
		if row["path"] != compressed {
			continue
		}
		found = true
		if row["size"].(float64) != float64(info.Size()) || row["mtime"] != info.ModTime().UTC().Format(time.RFC3339Nano) || row["content_hash"] != "sha256:"+hash {
			t.Fatalf("compressed file metadata = %v", row)
		}
	}
	if !found {
		t.Fatalf("missing compressed scan: %v", scans)
	}
	touched := info.ModTime().Add(2 * time.Second)
	if err := os.Chtimes(compressed, touched, touched); err != nil {
		t.Fatal(err)
	}
	repeated := runJSON(t, "import", "codex", dir, "--json")
	if repeated["files_parsed"].(float64) != 0 || repeated["files_skipped"].(float64) != 1 {
		t.Fatalf("unchanged compressed bytes should skip: %v", repeated)
	}
	// Same-size, restored-mtime rewrites must still be detected by the byte hash.
	write([]byte(strings.ReplaceAll(string(content), "alpha", "bravo")))
	changedInfo, err := os.Stat(compressed)
	if err != nil {
		t.Fatal(err)
	}
	if changedInfo.Size() != info.Size() {
		t.Fatalf("fixture size changed: %d != %d", changedInfo.Size(), info.Size())
	}
	if err := os.Chtimes(compressed, touched, touched); err != nil {
		t.Fatal(err)
	}
	changed := runJSON(t, "import", "codex", dir, "--json")
	if changed["files_parsed"].(float64) != 1 || changed["inserted_items"].(float64) != 1 {
		t.Fatalf("changed compressed bytes should reparse: %v", changed)
	}
	search := runJSON(t, "search", "bravo", "--source", "codex", "--json")
	if len(search["results"].([]any)) != 1 {
		t.Fatalf("changed event absent: %v", search)
	}
}
