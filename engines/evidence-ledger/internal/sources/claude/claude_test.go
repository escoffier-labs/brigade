package claude

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

const fixture = "../../../testdata/harnesses/claude-project.fixture.jsonl"

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
		t.Fatal("no records emitted from claude fixture")
	}
	if res.Records != len(recs) {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, len(recs))
	}
	for _, rec := range recs {
		if rec.Source.Kind != "claude" {
			t.Fatalf("source kind = %q, want claude", rec.Source.Kind)
		}
	}
}

func TestGenerateLinksToolResultToToolUse(t *testing.T) {
	recs, _ := parseRecords(t, fixture, sources.Options{})
	var toolUseID, resultTarget string
	for _, rec := range recs {
		if strings.HasPrefix(rec.Item.ExternalID, "claude:tool_use:") {
			toolUseID = rec.Item.ExternalID
		}
		for _, rel := range rec.Relations {
			if rel.Type == "result_of" {
				resultTarget = rel.TargetExternalID
			}
		}
	}
	if toolUseID == "" {
		t.Fatal("expected a claude:tool_use item from fixture")
	}
	if resultTarget != toolUseID {
		t.Fatalf("tool_result relation target = %q, want %q", resultTarget, toolUseID)
	}
}

func TestGenerateMalformedInput(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "mixed.jsonl")
	content := strings.Join([]string{
		`{"type":"user","uuid":"u1","timestamp":"2026-06-03T19:00:00Z","sessionId":"s","message":{"role":"user","content":"valid one"}}`,
		`{ broken json`,
		`{"type":"assistant","uuid":"a1","timestamp":"2026-06-03T19:01:00Z","sessionId":"s","message":{"role":"assistant","content":[{"type":"text","text":"valid two"}]}}`,
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
