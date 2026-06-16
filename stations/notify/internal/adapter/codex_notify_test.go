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
	if m.Title != "Codex (turn-42)" {
		t.Errorf("title = %q, want %q", m.Title, "Codex (turn-42)")
	}
}

func TestCodexNotify_FallsBackOnMissingMessage(t *testing.T) {
	in := `{"type": "agent-turn-complete"}`
	m, err := CodexNotify(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body != "Codex turn complete" {
		t.Errorf("body fallback = %q, want %q", m.Body, "Codex turn complete")
	}
	if m.Title != "Codex" {
		t.Errorf("title without ID = %q, want %q", m.Title, "Codex")
	}
}

func TestCodexNotifyFromBytes_ParsesArgPayload(t *testing.T) {
	in := []byte(`{"type":"agent-turn-complete","turn-id":"turn-7","last-assistant-message":"Built OK."}`)
	m, err := CodexNotifyFromBytes(in)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Source != "codex" {
		t.Errorf("source = %q, want codex", m.Source)
	}
	if !strings.Contains(m.Body, "Built OK.") {
		t.Errorf("body should contain last-assistant-message, got %q", m.Body)
	}
	if m.Title != "Codex (turn-7)" {
		t.Errorf("title = %q, want %q", m.Title, "Codex (turn-7)")
	}
}

func TestCodexNotify_BadJSONErrors(t *testing.T) {
	_, err := CodexNotify(strings.NewReader("not json"))
	if err == nil {
		t.Fatal("expected error for bad JSON")
	}
}
