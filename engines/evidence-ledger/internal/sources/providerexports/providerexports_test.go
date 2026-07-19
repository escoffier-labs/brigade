package providerexports

import (
	"bytes"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/sources"
)

func TestGenerateChatGPTLimitAndSince(t *testing.T) {
	fixture := filepath.Join("..", "..", "..", "testdata", "exports", "chatgpt-conversations.json")
	var out bytes.Buffer
	result, err := GenerateChatGPT(fixture, sources.Options{Limit: 1, Since: "2026-06-17"}, &out)
	if err != nil {
		t.Fatal(err)
	}
	if result.Records != 1 {
		t.Fatalf("records = %d, want 1", result.Records)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("lines = %d, want 1: %s", len(lines), out.String())
	}
	var rec map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &rec); err != nil {
		t.Fatal(err)
	}
	if rec["schema"] != "miseledger.adapter.v1" {
		t.Fatalf("bad schema: %v", rec)
	}
	source := rec["source"].(map[string]any)
	if source["kind"] != "chatgpt" {
		t.Fatalf("source kind = %v, want chatgpt", source["kind"])
	}
}

func TestGenerateClaudeSinceFiltersOldMessages(t *testing.T) {
	fixture := filepath.Join("..", "..", "..", "testdata", "exports", "claude-conversations.json")
	var out bytes.Buffer
	result, err := GenerateClaude(fixture, sources.Options{Since: "2026-06-17T10:00:02Z"}, &out)
	if err != nil {
		t.Fatal(err)
	}
	if result.Records != 1 {
		t.Fatalf("records = %d, want 1", result.Records)
	}
	if !strings.Contains(out.String(), "Claude export conversations become local evidence records") {
		t.Fatalf("expected assistant message after since filter: %s", out.String())
	}
}
