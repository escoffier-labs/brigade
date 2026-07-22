package codex

import (
	"bufio"
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

const fixture = "../../../testdata/harnesses/codex-session.fixture.jsonl"

// parseRecords runs Generate over a path and decodes each emitted JSONL line back
// into an adapter.Record so the output contract can be asserted.
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
	if len(recs) == 0 {
		t.Fatal("no records emitted from codex fixture")
	}
	if res.Records != len(recs) {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, len(recs))
	}
	for _, rec := range recs {
		if rec.Source.Kind != "codex" {
			t.Fatalf("source kind = %q, want codex", rec.Source.Kind)
		}
		if rec.Schema != adapter.SchemaV1 {
			t.Fatalf("schema = %q", rec.Schema)
		}
	}
}

func TestGenerateLimit(t *testing.T) {
	recs, _ := parseRecords(t, fixture, sources.Options{Limit: 1})
	if len(recs) != 1 {
		t.Fatalf("limit 1 emitted %d records", len(recs))
	}
}

func TestGenerateCapturesToolCallRelation(t *testing.T) {
	recs, _ := parseRecords(t, fixture, sources.Options{})
	var sawResultRelation bool
	for _, rec := range recs {
		for _, rel := range rec.Relations {
			if rel.Type == "result_of" && strings.HasPrefix(rel.TargetExternalID, "codex:call:") {
				sawResultRelation = true
			}
		}
	}
	if !sawResultRelation {
		t.Fatal("expected a result_of relation linking call_result back to call")
	}
}

func TestGenerateMalformedAndUnknownInput(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mixed.jsonl")
	content := strings.Join([]string{
		`{"type":"event_msg","timestamp":"2026-06-03T15:00:00Z","payload":{"session_id":"s","role":"user","message":"valid event one"}}`,
		`not valid json`,
		``,
		`{"type":"session_meta","timestamp":"2026-06-03T15:00:01Z","payload":{"session_id":"s"}}`,
		`{"type":"event_msg","timestamp":"2026-06-03T15:00:02Z","payload":{"session_id":"s","role":"assistant","message":"valid event two"}}`,
	}, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	// Malformed JSON must surface as a warning, never crash the run.
	if len(res.Warnings) == 0 {
		t.Fatal("expected at least one warning for malformed line")
	}
	// Valid events on either side of the malformed line still import.
	if len(recs) < 2 {
		t.Fatalf("expected the two valid events to import, got %d", len(recs))
	}
}

// Regression: a rollout file with a single line beyond the 10MB scanner limit
// used to abort the whole codex import with bufio.Scanner: token too long.
// The oversized line must be skipped with a warning while every other line in
// the same file (and the rest of the tree) still imports.
func TestGenerateSkipsOversizedLine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "rollout-oversized.jsonl")
	huge := `{"type":"event_msg","timestamp":"2026-07-14T19:29:49Z","payload":{"session_id":"s","role":"assistant","message":"` +
		strings.Repeat("a", sources.MaxLineBytes+1024) + `"}}`
	content := strings.Join([]string{
		`{"type":"event_msg","timestamp":"2026-07-14T19:29:48Z","payload":{"session_id":"s","role":"user","message":"before the oversized line"}}`,
		huge,
		`{"type":"event_msg","timestamp":"2026-07-14T19:29:50Z","payload":{"session_id":"s","role":"assistant","message":"after the oversized line"}}`,
	}, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	if len(recs) != 2 {
		t.Fatalf("records = %d, want the two normal lines to import", len(recs))
	}
	var warned bool
	for _, w := range res.Warnings {
		if strings.Contains(w, "line too long") {
			warned = true
		}
	}
	if !warned {
		t.Fatalf("expected a line-too-long warning, got %v", res.Warnings)
	}
}

func TestGenerateMissingPathErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := Generate(filepath.Join(t.TempDir(), "nope.jsonl"), sources.Options{}, &buf); err == nil {
		t.Fatal("expected error for missing path")
	}
}

func TestGenerateSkipsUnchangedFiles(t *testing.T) {
	dir := t.TempDir()
	src, err := os.ReadFile(fixture)
	if err != nil {
		t.Fatal(err)
	}
	a := filepath.Join(dir, "a.jsonl")
	b := filepath.Join(dir, "b.jsonl")
	if err := os.WriteFile(a, src, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(b, src, 0o600); err != nil {
		t.Fatal(err)
	}

	// First pass with no Skip: both files read, both produce records and hashes.
	_, full := parseRecords(t, dir, sources.Options{})
	if len(full.Files) != 2 {
		t.Fatalf("first pass files = %d, want 2", len(full.Files))
	}
	for _, f := range full.Files {
		if f.Skipped {
			t.Fatalf("first pass should skip nothing: %+v", f)
		}
		if f.ContentHash == "" || f.Records == 0 {
			t.Fatalf("first pass file not fully scanned: %+v", f)
		}
	}

	// Build a manifest from the first pass and skip file b only.
	manifest := map[string]sources.FileScan{}
	for _, f := range full.Files {
		manifest[f.Path] = f
	}
	opts := sources.Options{Skip: func(p string, size int64, mtime string) bool {
		prior, ok := manifest[p]
		return ok && p == b && prior.Size == size && prior.MTime == mtime
	}}
	recs, inc := parseRecords(t, dir, opts)

	var skipped, scanned *sources.FileScan
	for i := range inc.Files {
		switch inc.Files[i].Path {
		case a:
			scanned = &inc.Files[i]
		case b:
			skipped = &inc.Files[i]
		}
	}
	if skipped == nil || !skipped.Skipped {
		t.Fatalf("file b should be skipped: %+v", skipped)
	}
	if skipped.ContentHash != "" {
		t.Fatalf("skipped file must not be hashed: %+v", skipped)
	}
	if scanned == nil || scanned.Skipped || scanned.ContentHash == "" {
		t.Fatalf("file a should be scanned: %+v", scanned)
	}
	// Only the scanned file's records were emitted.
	if len(recs) == 0 || len(recs) >= len(full.Files)*100 {
		// sanity: at least some records, fewer than the full two-file pass
	}
	if inc.Records >= full.Records {
		t.Fatalf("incremental records (%d) should be fewer than full (%d)", inc.Records, full.Records)
	}
}
