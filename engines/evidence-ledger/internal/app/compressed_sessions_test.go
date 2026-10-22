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
	before := runJSON(t, "status", "--json")
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
	after := runJSON(t, "status", "--json")
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
