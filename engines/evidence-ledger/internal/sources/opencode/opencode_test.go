package opencode

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

func fixturePath(name string) string {
	return filepath.Join("..", "..", "..", "testdata", "harnesses", name)
}

func generate(t *testing.T, path string, opts sources.Options) ([]adapter.Record, sources.Result) {
	t.Helper()
	var buf bytes.Buffer
	result, err := Generate(path, opts, &buf)
	if err != nil {
		t.Fatalf("Generate(%s) error: %v", path, err)
	}
	var records []adapter.Record
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		rec, err := adapter.Parse([]byte(line))
		if err != nil {
			t.Fatalf("emitted invalid adapter record: %v\n%s", err, line)
		}
		if rec.Source.Kind != "opencode" {
			t.Fatalf("source kind = %q, want opencode", rec.Source.Kind)
		}
		if rec.Collection.Kind != "agent_session" {
			t.Fatalf("collection kind = %q, want agent_session", rec.Collection.Kind)
		}
		records = append(records, rec)
	}
	return records, result
}

func writeTemp(t *testing.T, name, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestGenerateExportFixture(t *testing.T) {
	records, result := generate(t, fixturePath("opencode-export.fixture.json"), sources.Options{})
	if len(records) != 2 {
		t.Fatalf("records = %d, want 2", len(records))
	}
	if result.Records != 2 {
		t.Fatalf("result.Records = %d, want 2", result.Records)
	}
	first := records[0]
	if first.Item.ExternalID != "opencode:message:msg_user" {
		t.Fatalf("first item external id = %q", first.Item.ExternalID)
	}
	if first.Actor == nil || first.Actor.Type != "human" {
		t.Fatalf("first actor = %#v", first.Actor)
	}
	second := records[1]
	if len(second.Artifacts) == 0 || second.Artifacts[0].Kind != "command" {
		t.Fatalf("expected command artifact on assistant message: %#v", second.Artifacts)
	}
	if second.Raw.Ordinal == nil || *second.Raw.Ordinal != 2 {
		t.Fatalf("second raw ordinal = %#v", second.Raw.Ordinal)
	}
}

func TestGenerateLimit(t *testing.T) {
	records, _ := generate(t, fixturePath("opencode-export.fixture.json"), sources.Options{Limit: 1})
	if len(records) != 1 {
		t.Fatalf("limited records = %d, want 1", len(records))
	}
}

func TestGenerateMalformedInput(t *testing.T) {
	cases := []struct {
		name        string
		content     string
		wantRecords bool
		wantWarning bool
	}{
		{
			name:        "truncated json",
			content:     `{"info":{"id":"x"},"messages":[`,
			wantRecords: false,
			wantWarning: true,
		},
		{
			name:        "no messages array",
			content:     `{"info":{"id":"x"}}`,
			wantRecords: false,
			wantWarning: false,
		},
		{
			name:        "empty messages array",
			content:     `{"info":{"id":"x"},"messages":[]}`,
			wantRecords: false,
			wantWarning: false,
		},
		{
			name:        "message with no parts uses fallback label",
			content:     `{"info":{"id":"x"},"messages":[{"info":{"id":"m1","role":"user"},"parts":[]}]}`,
			wantRecords: true,
			wantWarning: false,
		},
		{
			name:        "wrong type for parts is rejected by decoder",
			content:     `{"info":{"id":"x"},"messages":[{"info":{"id":"m1","role":"user"},"parts":"oops"}]}`,
			wantRecords: false,
			wantWarning: true,
		},
		{
			name:        "empty file",
			content:     "",
			wantRecords: false,
			wantWarning: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := writeTemp(t, "opencode.json", tc.content)
			records, result := generate(t, path, sources.Options{})
			if tc.wantRecords && len(records) == 0 {
				t.Fatalf("expected records, got none (warnings=%v)", result.Warnings)
			}
			if !tc.wantRecords && len(records) != 0 {
				t.Fatalf("expected no records, got %d", len(records))
			}
			if tc.wantWarning && len(result.Warnings) == 0 {
				t.Fatalf("expected a warning")
			}
			if !tc.wantWarning && len(result.Warnings) != 0 {
				t.Fatalf("unexpected warnings: %v", result.Warnings)
			}
		})
	}
}

func TestGenerateDirectoryScansJSONFiles(t *testing.T) {
	dir := t.TempDir()
	good := `{"info":{"id":"s1"},"messages":[{"info":{"id":"m1","role":"user"},"parts":[{"type":"text","text":"hello"}]}]}`
	if err := os.WriteFile(filepath.Join(dir, "a.json"), []byte(good), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "b.json"), []byte(good), 0o600); err != nil {
		t.Fatal(err)
	}
	records, result := generate(t, dir, sources.Options{})
	if len(records) != 2 {
		t.Fatalf("directory records = %d, want 2", len(records))
	}
	if len(result.Files) != 2 {
		t.Fatalf("result.Files = %d, want 2", len(result.Files))
	}
}

func TestGenerateRedactsAllClasses(t *testing.T) {
	content := `{"info":{"id":"x","directory":"/home/demo/private"},"messages":[{"info":{"id":"m1","role":"user"},"parts":[{"type":"text","text":"token=abc123 email demo@example.com url https://private.example.com/path host build.internal"}]}]}`
	path := writeTemp(t, "opencode.json", content)
	records, _ := generate(t, path, sources.Options{RedactPaths: true, RedactSecrets: true, RedactEmails: true, RedactURLs: true, RedactHostnames: true})
	if len(records) != 1 {
		t.Fatalf("records = %d, want 1", len(records))
	}
	got := records[0].Item.Text + " " + string(records[0].Item.Metadata)
	for _, forbidden := range []string{"abc123", "demo@example.com", "https://private.example.com/path", "build.internal", "/home/demo/private"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("redaction leaked %q in %q", forbidden, got)
		}
	}
}

func TestGenerateEmptyPathIsError(t *testing.T) {
	_, err := Generate("", sources.Options{}, &bytes.Buffer{})
	if err == nil {
		t.Fatalf("expected error for empty path")
	}
}
