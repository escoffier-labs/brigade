package pi

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

func metaString(t *testing.T, raw json.RawMessage, key string) string {
	t.Helper()
	var m map[string]any
	if len(raw) == 0 {
		return ""
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("metadata unmarshal: %v", err)
	}
	v, _ := m[key].(string)
	return v
}

const fixture = "../../../testdata/harnesses/--tmp-example-project--/pi-session.fixture.jsonl"

func parseRecords(t *testing.T, path string, opts sources.Options) ([]adapter.Record, sources.Result) {
	t.Helper()
	var buf bytes.Buffer
	res, err := Generate(path, opts, &buf)
	if err != nil {
		t.Fatalf("Generate(%s): %v", path, err)
	}
	var recs []adapter.Record
	scanner := bufio.NewScanner(&buf)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	for scanner.Scan() {
		line := scanner.Bytes()
		if strings.TrimSpace(string(line)) == "" {
			continue
		}
		rec, err := adapter.Parse(append([]byte(nil), line...))
		if err != nil {
			t.Fatalf("emitted line failed adapter.Parse/Validate: %v\nline: %s", err, line)
		}
		recs = append(recs, rec)
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return recs, res
}

func TestGenerateFixtureEmitsValidRecords(t *testing.T) {
	recs, res := parseRecords(t, fixture, sources.Options{})
	if len(recs) != 2 {
		t.Fatalf("expected 2 message records, got %d", len(recs))
	}
	if res.Records != len(recs) {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, len(recs))
	}
	if len(res.Warnings) != 0 {
		t.Fatalf("expected no warnings for skipped non-message events, got %v", res.Warnings)
	}
	for _, rec := range recs {
		if rec.Source.Kind != "pi" {
			t.Fatalf("source kind = %q, want pi", rec.Source.Kind)
		}
	}
}

func TestGenerateMapsSessionCWDAndProject(t *testing.T) {
	recs, _ := parseRecords(t, fixture, sources.Options{})
	if len(recs) == 0 {
		t.Fatal("no records emitted")
	}
	rec := recs[0]
	if got := metaString(t, rec.Collection.Metadata, "cwd"); got != "/tmp/example-project" {
		t.Fatalf("collection cwd = %q, want /tmp/example-project", got)
	}
	if got := metaString(t, rec.Collection.Metadata, "project"); got != "--tmp-example-project--" {
		t.Fatalf("collection project = %q, want --tmp-example-project--", got)
	}
	if got := metaString(t, rec.Item.Metadata, "cwd"); got != "/tmp/example-project" {
		t.Fatalf("item cwd = %q, want /tmp/example-project", got)
	}
	if got := metaString(t, rec.Item.Metadata, "project"); got != "--tmp-example-project--" {
		t.Fatalf("item project = %q, want --tmp-example-project--", got)
	}
}

func TestGeneratePreservesRolesAndText(t *testing.T) {
	recs, _ := parseRecords(t, fixture, sources.Options{})
	var userText, assistantText string
	var userRole, assistantRole string
	for _, rec := range recs {
		if rec.Actor == nil {
			t.Fatalf("missing actor on %q", rec.Item.ExternalID)
		}
		switch rec.Actor.Type {
		case "human":
			userRole = rec.Actor.Type
			userText = rec.Item.Text
		case "assistant":
			assistantRole = rec.Actor.Type
			assistantText = rec.Item.Text
		}
		if rec.Item.CreatedAt == "" {
			t.Fatalf("missing timestamp on %q", rec.Item.ExternalID)
		}
	}
	if userRole != "human" || !strings.Contains(userText, "Pi native import") {
		t.Fatalf("user message = %q (%q), want human with Pi native import text", userRole, userText)
	}
	if assistantRole != "assistant" || !strings.Contains(assistantText, "normalize into MiseLedger") {
		t.Fatalf("assistant message = %q (%q), want assistant with normalize text", assistantRole, assistantText)
	}
}

func TestGenerateMalformedInput(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mixed.jsonl")
	content := strings.Join([]string{
		`{"type":"session","version":3,"id":"s","timestamp":"2026-06-03T19:00:00Z","cwd":"/tmp/example-project"}`,
		`{ broken json`,
		`{"type":"message","id":"u1","timestamp":"2026-06-03T19:01:00Z","message":{"role":"user","content":[{"type":"text","text":"valid one"}]}}`,
		`{"type":"message","id":"a1","timestamp":"2026-06-03T19:02:00Z","message":{"role":"assistant","content":[{"type":"text","text":"valid two"}]}}`,
	}, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	if len(res.Warnings) == 0 {
		t.Fatal("expected a warning for the malformed line")
	}
	if len(recs) != 2 {
		t.Fatalf("expected 2 valid records around malformed line, got %d", len(recs))
	}
}

func TestGenerateMissingPathErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := Generate(filepath.Join(t.TempDir(), "nope.jsonl"), sources.Options{}, &buf); err == nil {
		t.Fatal("expected error for missing path")
	}
}

func TestDefaultRoot(t *testing.T) {
	root := DefaultRoot()
	if !strings.HasSuffix(root, filepath.Join(".pi", "agent", "sessions")) {
		t.Fatalf("DefaultRoot() = %q, want suffix .pi/agent/sessions", root)
	}
}
