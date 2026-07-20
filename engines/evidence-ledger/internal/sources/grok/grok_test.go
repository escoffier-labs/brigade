package grok

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

const fixture = "../../../testdata/harnesses/grok-sessions.fixture"

func decodeRecords(t *testing.T, buf *bytes.Buffer) []adapter.Record {
	t.Helper()
	var out []adapter.Record
	scanner := bufio.NewScanner(buf)
	for scanner.Scan() {
		var rec adapter.Record
		if err := json.Unmarshal(scanner.Bytes(), &rec); err != nil {
			t.Fatalf("decode record: %v", err)
		}
		out = append(out, rec)
	}
	if err := scanner.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestGenerateFixtureEmitsSummaryAndMessages(t *testing.T) {
	var buf bytes.Buffer
	res, err := Generate(fixture, sources.Options{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	jsonl := buf.String()
	recs := decodeRecords(t, &buf)
	if res.Records != 6 || len(recs) != 6 {
		t.Fatalf("records=%d decoded=%d", res.Records, len(recs))
	}
	for _, rec := range recs {
		if rec.Source.Kind != "grok" {
			t.Fatalf("source=%q", rec.Source.Kind)
		}
		if rec.Collection.Kind != "agent_session" {
			t.Fatalf("collection=%q", rec.Collection.Kind)
		}
		if rec.Collection.ExternalID != "grok:session:demo-session-001" {
			t.Fatalf("collection id=%q", rec.Collection.ExternalID)
		}
		if !strings.Contains(string(rec.Collection.Metadata), `"workspace":"/workspaces/demo-project"`) {
			t.Fatalf("workspace metadata=%s", rec.Collection.Metadata)
		}
	}
	if !strings.Contains(jsonl, "Synthetic Grok crawler audit") || !strings.Contains(jsonl, "fixture crawl contract") {
		t.Fatal("summary or chat text did not round-trip")
	}
}

func TestGenerateLimitSinceAndScans(t *testing.T) {
	var scans []sources.FileScan
	var buf bytes.Buffer
	res, err := Generate(fixture, sources.Options{
		Limit: 2,
		Since: "2026-07-15T00:00:00Z",
		AfterFile: func(scan sources.FileScan) error {
			scans = append(scans, scan)
			return nil
		},
	}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Records != 2 || len(decodeRecords(t, &buf)) != 2 {
		t.Fatalf("limited records=%d", res.Records)
	}
	if len(scans) != 2 || len(res.Files) != 2 {
		t.Fatalf("scan callbacks=%d result files=%d", len(scans), len(res.Files))
	}

	buf.Reset()
	res, err = Generate(fixture, sources.Options{Since: "2027-01-01"}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Records != 0 || buf.Len() != 0 {
		t.Fatalf("future since emitted records=%d", res.Records)
	}
}

func TestGenerateMalformedChatWarnsAndContinues(t *testing.T) {
	root := t.TempDir()
	session := filepath.Join(root, "%2Fworkspaces%2Fmalformed", "session-bad")
	if err := os.MkdirAll(session, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(session, "summary.json"), []byte(`{"generated_title":"Malformed fixture","created_at":"2026-07-15T12:00:00Z"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	body := "{bad json}\n" + `{"type":"user","content":[{"type":"text","text":"valid line after malformed input"}]}` + "\n"
	if err := os.WriteFile(filepath.Join(session, "chat_history.jsonl"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	var buf bytes.Buffer
	res, err := Generate(root, sources.Options{}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Records != 2 || len(res.Warnings) != 1 {
		t.Fatalf("records=%d warnings=%v", res.Records, res.Warnings)
	}
	if !strings.Contains(buf.String(), "valid line after malformed input") {
		t.Fatal("valid record after malformed line was lost")
	}
}

func TestDefaultRootUsesHome(t *testing.T) {
	t.Setenv("HOME", "/tmp/grok-home")
	if got := DefaultRoot(); got != filepath.Join("/tmp/grok-home", ".grok", "sessions") {
		t.Fatalf("DefaultRoot=%q", got)
	}
}
