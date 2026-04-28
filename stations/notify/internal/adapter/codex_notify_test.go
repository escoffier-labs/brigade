package adapter

import (
	"strings"
	"testing"
)

func TestCodexNotify_ExtractsMessageAndSession(t *testing.T) {
	in := `{
		"type": "agent-turn-complete",
		"turn-id": "turn-42",
		"input-messages": [],
		"last-assistant-message": "Done. Tests pass."
	}`
	m, err := CodexNotify(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Source != "codex" {
		t.Errorf("source = %q, want codex", m.Source)
	}
	if !strings.Contains(m.Body, "Done. Tests pass.") {
		t.Errorf("body should contain last-assistant-message, got %q", m.Body)
	}
}

func TestCodexNotify_FallsBackOnMissingMessage(t *testing.T) {
	in := `{"type": "agent-turn-complete"}`
	m, err := CodexNotify(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body == "" {
		t.Error("body should fall back to a default")
	}
}

func TestCodexNotify_BadJSONErrors(t *testing.T) {
	_, err := CodexNotify(strings.NewReader("not json"))
	if err == nil {
		t.Fatal("expected error for bad JSON")
	}
}
