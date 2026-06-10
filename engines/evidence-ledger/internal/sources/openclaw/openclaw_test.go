package openclaw

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

func TestGenerateSessionFixture(t *testing.T) {
	recs, res := parseRecords(t, "../../../testdata/harnesses/openclaw-session.fixture.jsonl", sources.Options{})
	if len(recs) == 0 {
		t.Fatal("no records from openclaw session fixture")
	}
	if res.Records != len(recs) {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, len(recs))
	}
	for _, rec := range recs {
		if rec.Source.Kind != "openclaw" {
			t.Fatalf("source kind = %q, want openclaw", rec.Source.Kind)
		}
	}
}

func TestGenerateTrajectoryFixtureLinksRunAndSession(t *testing.T) {
	recs, _ := parseRecords(t, "../../../testdata/harnesses/openclaw-trajectory.fixture.jsonl", sources.Options{})
	var sawSessionRel, sawRunRel bool
	for _, rec := range recs {
		for _, rel := range rec.Relations {
			switch rel.Type {
			case "belongs_to_session":
				sawSessionRel = true
			case "belongs_to_run":
				sawRunRel = true
			}
		}
	}
	if !sawSessionRel {
		t.Fatal("expected belongs_to_session relation in trajectory fixture")
	}
	if !sawRunRel {
		t.Fatal("expected belongs_to_run relation in trajectory fixture")
	}
}

func TestGenerateMalformedInput(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mixed.jsonl")
	content := strings.Join([]string{
		`{"type":"message","timestamp":"2026-06-03T16:00:00Z","session_id":"s","role":"human","message":"valid one"}`,
		`not json`,
		`{"type":"custom","timestamp":"2026-06-03T16:01:00Z","session_id":"s","role":"assistant","content":"valid two"}`,
	}, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	if len(res.Warnings) == 0 {
		t.Fatal("expected a warning for malformed line")
	}
	if len(recs) != 2 {
		t.Fatalf("expected 2 valid records, got %d", len(recs))
	}
}

func TestGenerateMissingPathErrors(t *testing.T) {
	var buf bytes.Buffer
	if _, err := Generate(filepath.Join(t.TempDir(), "nope.jsonl"), sources.Options{}, &buf); err == nil {
		t.Fatal("expected error for missing path")
	}
}
