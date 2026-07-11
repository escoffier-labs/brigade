package cursor

import (
	"bufio"
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func records(t *testing.T, buf *bytes.Buffer) []adapter.Record {
	t.Helper()
	var out []adapter.Record
	scanner := bufio.NewScanner(buf)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var rec adapter.Record
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("bad record: %v\n%s", err, line)
		}
		out = append(out, rec)
	}
	return out
}

func parseRecords(t *testing.T, path string, opts sources.Options) ([]adapter.Record, sources.Result) {
	t.Helper()
	var buf bytes.Buffer
	res, err := Generate(path, opts, &buf)
	if err != nil {
		t.Fatalf("Generate(%s): %v", path, err)
	}
	return records(t, &buf), res
}

func TestGeneratePromptHistoryAndSessions(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "prompt_history.json"),
		`["fix the auth timeout bug","release audit checklist","fix the auth timeout bug"]`)
	writeFile(t, filepath.Join(root, "chats", "abc123", "meta.json"),
		`{"id":"abc123","title":"Auth timeout investigation","createdAt":1718600000000,"workspace":"/home/u/repo"}`)
	writeFile(t, filepath.Join(root, "chats", "abc123", "store.db"), "binary-blob-placeholder")
	// A chat with only a store.db and no meta.json should still surface.
	writeFile(t, filepath.Join(root, "acp-sessions", "def456", "store.db"), "binary-blob-placeholder")

	var buf bytes.Buffer
	result, err := Generate(root, sources.Options{}, &buf)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	recs := records(t, &buf)

	var prompts, sessions int
	var sawDedup = map[string]int{}
	for _, r := range recs {
		switch r.Collection.Kind {
		case "prompt_history":
			prompts++
			sawDedup[r.Item.ExternalID]++
		case "agent_session":
			sessions++
		default:
			t.Fatalf("unexpected collection kind %q", r.Collection.Kind)
		}
	}
	// Two distinct prompts emitted (the duplicate shares an external id).
	if prompts != 3 {
		t.Fatalf("expected 3 prompt items emitted, got %d", prompts)
	}
	for id, n := range sawDedup {
		if strings.Contains(id, sources.StableID("fix the auth timeout bug")) && n != 2 {
			t.Fatalf("expected duplicate prompt to share external id, got %d", n)
		}
	}
	if sessions != 2 {
		t.Fatalf("expected 2 sessions (meta.json + store-only), got %d", sessions)
	}
	if result.Records != prompts+sessions {
		t.Fatalf("result.Records=%d but counted %d", result.Records, prompts+sessions)
	}
	// The store-only chat must produce a warning.
	if len(result.Warnings) == 0 {
		t.Fatal("expected a warning for the store-only chat")
	}
	// The titled session should carry its title as searchable text.
	found := false
	for _, r := range recs {
		if r.Collection.Kind == "agent_session" && strings.Contains(r.Item.Text, "Auth timeout investigation") {
			found = true
			if r.Raw.Path == "" {
				t.Fatal("session record missing raw path for resume")
			}
		}
	}
	if !found {
		t.Fatal("titled session text not found")
	}
}

func TestGenerateFixtureEmitsPromptHistoryAndSessions(t *testing.T) {
	recs, res := parseRecords(t, "../../../testdata/harnesses/cursor-config.fixture", sources.Options{})

	collections := map[string]bool{}
	var promptItems, sessionItems int
	var sawPromptSnippet, sawSessionSnippet bool
	for _, rec := range recs {
		if rec.Source.Kind != "cursor" {
			t.Fatalf("source kind = %q, want cursor", rec.Source.Kind)
		}
		collections[rec.Collection.ExternalID] = true
		switch rec.Collection.Kind {
		case "prompt_history":
			promptItems++
			if strings.Contains(rec.Item.Text, "Summarize demo-project release notes") {
				sawPromptSnippet = true
			}
		case "agent_session":
			sessionItems++
			if strings.Contains(rec.Item.Text, "demo-project import plan") {
				sawSessionSnippet = true
			}
		default:
			t.Fatalf("unexpected collection kind %q", rec.Collection.Kind)
		}
	}
	if len(collections) != 3 {
		t.Fatalf("collection count = %d, want 3", len(collections))
	}
	if promptItems != 3 {
		t.Fatalf("prompt item count = %d, want 3", promptItems)
	}
	if sessionItems != 2 {
		t.Fatalf("session item count = %d, want 2", sessionItems)
	}
	if res.Records != promptItems+sessionItems {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, promptItems+sessionItems)
	}
	if !sawPromptSnippet {
		t.Fatal("prompt fixture snippet did not round-trip")
	}
	if !sawSessionSnippet {
		t.Fatal("session fixture snippet did not round-trip")
	}
}

func TestGenerateSinceFilters(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "chats", "old", "meta.json"),
		`{"id":"old","title":"old chat","createdAt":"2020-01-01T00:00:00Z"}`)
	writeFile(t, filepath.Join(root, "chats", "new", "meta.json"),
		`{"id":"new","title":"new chat","createdAt":"2099-01-01T00:00:00Z"}`)

	var buf bytes.Buffer
	if _, err := Generate(root, sources.Options{Since: "2050-01-01"}, &buf); err != nil {
		t.Fatalf("Generate: %v", err)
	}
	recs := records(t, &buf)
	if len(recs) != 1 || !strings.Contains(recs[0].Item.Text, "new chat") {
		t.Fatalf("since filter failed, got %d records", len(recs))
	}
}

func TestGenerateMissingRoot(t *testing.T) {
	if _, err := Generate(filepath.Join(t.TempDir(), "nope"), sources.Options{}, &bytes.Buffer{}); err == nil {
		t.Fatal("expected error for missing root")
	}
}

func TestDefaultRootRespectsXDG(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "/tmp/xdg")
	if got := DefaultRoot(); got != filepath.Join("/tmp/xdg", "cursor") {
		t.Fatalf("DefaultRoot=%q", got)
	}
}
