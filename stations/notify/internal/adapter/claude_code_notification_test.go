package adapter

import (
	"strings"
	"testing"
)

func TestClaudeCodeNotification_ExtractsMessage(t *testing.T) {
	in := `{
		"hook_event_name": "Notification",
		"message": "Claude is waiting for your input",
		"cwd": "/repo"
	}`
	m, err := ClaudeCodeNotification(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body != "Claude is waiting for your input" {
		t.Errorf("body = %q", m.Body)
	}
	if m.Source != "claude-code" {
		t.Errorf("source = %q", m.Source)
	}
}

func TestClaudeCodeNotification_FallsBackOnMissingMessage(t *testing.T) {
	in := `{"hook_event_name": "Notification"}`
	m, err := ClaudeCodeNotification(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body == "" {
		t.Error("body should fall back to a default, got empty")
	}
}
