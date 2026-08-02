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

func TestClaudeCodeStop_IncludesCWDOnlyWhenOptedInForEveryAlias(t *testing.T) {
	tests := []struct {
		name  string
		input string
	}{
		{name: "cwd", input: `{"cwd":"/private/project"}`},
		{name: "working directory", input: `{"working_directory":"/private/project"}`},
		{name: "workdir", input: `{"workdir":"/private/project"}`},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m, err := ClaudeCodeStop(strings.NewReader(tt.input), ClaudeCodeStopOptions{IncludeCWD: true})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			want := "Session ended in /private/project"
			if m.Body != want {
				t.Errorf("body = %q, want %q", m.Body, want)
			}
		})
	}
}

func TestClaudeCodeStop_IncludesSessionIDOnlyWhenOptedInForEveryAlias(t *testing.T) {
	tests := []struct {
		name  string
		input string
	}{
		{name: "session id", input: `{"session_id":"private-session"}`},
		{name: "session ID", input: `{"sessionId":"private-session"}`},
		{name: "session", input: `{"session":"private-session"}`},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			m, err := ClaudeCodeStop(strings.NewReader(tt.input), ClaudeCodeStopOptions{IncludeSessionID: true})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			want := "Session ended (session private-session)"
			if m.Body != want {
				t.Errorf("body = %q, want %q", m.Body, want)
			}
		})
	}
}

func TestClaudeCodeStop_IncludesBothPrivateFieldsWhenBothOptedIn(t *testing.T) {
	in := `{"cwd":"/private/project","session_id":"private-session"}`
	m, err := ClaudeCodeStop(strings.NewReader(in), ClaudeCodeStopOptions{IncludeCWD: true, IncludeSessionID: true})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := "Session ended in /private/project (session private-session)"
	if m.Body != want {
		t.Errorf("body = %q, want %q", m.Body, want)
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
