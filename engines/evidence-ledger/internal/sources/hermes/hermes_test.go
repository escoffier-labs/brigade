package hermes

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

func TestGenerateSnapshotFixture(t *testing.T) {
	recs, res := parseRecords(t, "../../../testdata/harnesses/session_hermes-demo.fixture.json", sources.Options{})
	if len(recs) == 0 {
		t.Fatal("no records from hermes snapshot fixture")
	}
	if res.Records != len(recs) {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, len(recs))
	}
	for _, rec := range recs {
		if rec.Source.Kind != "hermes" {
			t.Fatalf("source kind = %q, want hermes", rec.Source.Kind)
		}
	}
}

func TestGenerateSnapshotEmitsToolCallAndResult(t *testing.T) {
	recs, _ := parseRecords(t, "../../../testdata/harnesses/session_hermes-demo.fixture.json", sources.Options{})
	var sawToolCall, sawResultRel bool
	for _, rec := range recs {
		if strings.HasPrefix(rec.Item.ExternalID, "hermes:tool_call:") {
			sawToolCall = true
		}
		for _, rel := range rec.Relations {
			if rel.Type == "result_of" && strings.HasPrefix(rel.TargetExternalID, "hermes:tool_call:") {
				sawResultRel = true
			}
		}
	}
	if !sawToolCall {
		t.Fatal("expected a hermes:tool_call item from snapshot fixture")
	}
	if !sawResultRel {
		t.Fatal("expected a result_of relation from tool result back to tool call")
	}
}

func TestGenerateTrajectoryFixture(t *testing.T) {
	recs, _ := parseRecords(t, "../../../testdata/harnesses/hermes-trajectory.fixture.jsonl", sources.Options{})
	if len(recs) == 0 {
		t.Fatal("no records from hermes trajectory fixture")
	}
	// "gpt" role is normalized to "assistant".
	var sawAssistant bool
	for _, rec := range recs {
		if rec.Actor != nil && rec.Actor.Type == "assistant" {
			sawAssistant = true
		}
	}
	if !sawAssistant {
		t.Fatal("expected gpt role to normalize to assistant actor")
	}
}

func TestGenerateMalformedSnapshotWarns(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "session_broken.json")
	if err := os.WriteFile(path, []byte(`{ not valid json`), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	if len(recs) != 0 {
		t.Fatalf("malformed snapshot should emit no records, got %d", len(recs))
	}
	if len(res.Warnings) == 0 {
		t.Fatal("expected a warning for malformed snapshot")
	}
}

func TestGenerateMalformedTrajectoryLineWarns(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "broken.trajectory.jsonl")
	content := strings.Join([]string{
		`{"conversations":[{"from":"human","value":"valid line"}],"timestamp":"2026-06-03T20:10:00"}`,
		`not json`,
	}, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	recs, res := parseRecords(t, path, sources.Options{})
	if len(res.Warnings) == 0 {
		t.Fatal("expected a warning for malformed trajectory line")
	}
	if len(recs) == 0 {
		t.Fatal("expected the valid trajectory line to still import")
	}
}

func TestIncludeFiltersBackupsAndDumps(t *testing.T) {
	cases := []struct {
		name string
		want bool
	}{
		{"session_demo.json", true},
		{"trajectory_samples.jsonl", true},
		{"failed_trajectories.jsonl", true},
		{"request_dump_1.jsonl", false},
		{"session_demo.backup.json", false},
		{"events.metadata.jsonl", false},
		{"notes.txt", false},
	}
	for _, c := range cases {
		if got := Include(filepath.Join("/tmp", c.name)); got != c.want {
			t.Fatalf("Include(%s)=%v, want %v", c.name, got, c.want)
		}
	}
}
