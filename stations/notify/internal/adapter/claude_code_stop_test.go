package adapter

import (
	"strings"
	"testing"
)

func TestClaudeCodeStop_ExtractsCwdAndSession(t *testing.T) {
	in := `{
		"hook_event_name": "Stop",
		"cwd": "/home/user/repos/foo",
		"session_id": "abc123",
		"transcript_path": "/tmp/cc-trans.jsonl"
	}`
	m, err := ClaudeCodeStop(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Source != "claude-code" {
		t.Errorf("source = %q, want claude-code", m.Source)
	}
	if !strings.Contains(m.Body, "/home/user/repos/foo") {
		t.Errorf("body should contain cwd, got %q", m.Body)
	}
	if m.Title == "" {
		t.Error("expected non-empty title")
	}
}

func TestClaudeCodeStop_FallsBackWhenFieldsMissing(t *testing.T) {
	// Defensive: missing fields should not crash.
	in := `{"hook_event_name": "Stop"}`
	m, err := ClaudeCodeStop(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body == "" {
		t.Error("body should fall back to a sensible default, got empty")
	}
}

func TestClaudeCodeStop_BadJSONErrors(t *testing.T) {
	_, err := ClaudeCodeStop(strings.NewReader("not json"))
	if err == nil {
		t.Fatal("expected error for bad JSON, got nil")
	}
}

func TestClaudeCodeStop_AcceptsCwdAlias(t *testing.T) {
	// Defensive: if a future CC version renames cwd to working_directory,
	// the adapter should try multiple known names.
	in := `{"working_directory": "/tmp/x"}`
	m, err := ClaudeCodeStop(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(m.Body, "/tmp/x") {
		t.Errorf("expected /tmp/x in body via cwd alias, got %q", m.Body)
	}
}
