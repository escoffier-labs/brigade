package adapter

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

// ClaudeCodeStop reads a Claude Code Stop hook event JSON from r and
// produces a canonical message.
//
// Defensive parsing: tries multiple known field names for cwd, falls back
// to sensible defaults when fields are missing. Survives most schema
// additions and aliased renames without changes.
func ClaudeCodeStop(r io.Reader, options ClaudeCodeStopOptions) (canonical.Message, error) {
	raw, err := ReadBounded(r)
	if err != nil {
		return canonical.Message{}, err
	}
	var ev map[string]interface{}
	if err := json.Unmarshal(raw, &ev); err != nil {
		return canonical.Message{}, fmt.Errorf("parse hook event: %w", err)
	}

	cwd := firstString(ev, "cwd", "working_directory", "workdir")
	sessionID := firstString(ev, "session_id", "sessionId", "session")

	body := "Session ended"
	if options.IncludeCWD && cwd != "" {
		body = "Session ended in " + cwd
	}
	if options.IncludeSessionID && sessionID != "" {
		body += " (session " + sessionID + ")"
	}

	return canonical.Message{
		Title:  "Claude Code",
		Body:   body,
		Source: "claude-code",
	}, nil
}

// ClaudeCodeStopOptions controls the private hook-event fields included in a
// Claude Code Stop notification. Both disclosures are disabled by default.
type ClaudeCodeStopOptions struct {
	IncludeCWD       bool
	IncludeSessionID bool
}

// firstString returns the first non-empty string value found at any of the
// given keys in the map.
func firstString(m map[string]interface{}, keys ...string) string {
	for _, k := range keys {
		if v, ok := m[k]; ok {
			if s, ok := v.(string); ok && s != "" {
				return s
			}
		}
	}
	return ""
}
