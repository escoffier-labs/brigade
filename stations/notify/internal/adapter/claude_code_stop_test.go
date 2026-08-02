package adapter

import (
	"strings"
	"testing"
)

func TestClaudeCodeStop_OmitsPrivateFieldsByDefaultForEveryAlias(t *testing.T) {
	tests := []struct {
		name  string
		input string
		value string
	}{
		{name: "cwd", input: `{"cwd":"/private/project"}`, value: "/private/project"},
		{name: "working directory", input: `{"working_directory":"/private/project"}`, value: "/private/project"},
		{name: "workdir", input: `{"workdir":"/private/project"}`, value: "/private/project"},
		{name: "session id", input: `{"session_id":"private-session"}`, value: "private-session"},
		{name: "session ID", input: `{"sessionId":"private-session"}`, value: "private-session"},
		{name: "session", input: `{"session":"private-session"}`, value: "private-session"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m, err := ClaudeCodeStop(strings.NewReader(tt.input), ClaudeCodeStopOptions{})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if m.Body != "Session ended" {
				t.Errorf("body = %q, want default body without private fields", m.Body)
			}
			if strings.Contains(m.Body, tt.value) {
				t.Errorf("body leaked private value %q: %q", tt.value, m.Body)
			}
		})
	}
}

func TestClaudeCodeStop_IncludesPrivateFieldsOnlyWhenOptedIn(t *testing.T) {
	in := `{"cwd":"/private/project","session_id":"private-session"}`
	tests := []struct {
		name    string
		options ClaudeCodeStopOptions
		want    string
	}{
		{name: "cwd only", options: ClaudeCodeStopOptions{IncludeCWD: true}, want: "Session ended in /private/project"},
		{name: "session only", options: ClaudeCodeStopOptions{IncludeSessionID: true}, want: "Session ended (session private-session)"},
		{name: "both", options: ClaudeCodeStopOptions{IncludeCWD: true, IncludeSessionID: true}, want: "Session ended in /private/project (session private-session)"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m, err := ClaudeCodeStop(strings.NewReader(in), tt.options)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if m.Body != tt.want {
				t.Errorf("body = %q, want %q", m.Body, tt.want)
			}
		})
	}
}

func TestClaudeCodeStop_FallsBackWhenFieldsMissing(t *testing.T) {
	// Defensive: missing fields should not crash.
	in := `{"hook_event_name": "Stop"}`
	m, err := ClaudeCodeStop(strings.NewReader(in), ClaudeCodeStopOptions{IncludeCWD: true, IncludeSessionID: true})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Body != "Session ended" {
		t.Errorf("body = %q, want safe fallback body", m.Body)
	}
}

func TestClaudeCodeStop_BadJSONErrors(t *testing.T) {
	_, err := ClaudeCodeStop(strings.NewReader("not json"), ClaudeCodeStopOptions{})
	if err == nil {
		t.Fatal("expected error for bad JSON, got nil")
	}
}
