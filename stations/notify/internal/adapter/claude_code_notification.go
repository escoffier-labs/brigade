package adapter

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

// ClaudeCodeNotification reads a Claude Code Notification hook event JSON
// and produces a canonical message.
func ClaudeCodeNotification(r io.Reader) (canonical.Message, error) {
	raw, err := io.ReadAll(r)
	if err != nil {
		return canonical.Message{}, fmt.Errorf("read input: %w", err)
	}
	var ev map[string]interface{}
	if err := json.Unmarshal(raw, &ev); err != nil {
		return canonical.Message{}, fmt.Errorf("parse hook event: %w", err)
	}

	body := firstString(ev, "message", "text", "msg")
	if body == "" {
		body = "Claude Code notification"
	}

	return canonical.Message{
		Title:  "Claude Code",
		Body:   body,
		Source: "claude-code",
	}, nil
}
