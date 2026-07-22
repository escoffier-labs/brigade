package adapter

import (
	"encoding/json"
	"fmt"
	"io"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

// CodexNotify reads a Codex CLI notify event JSON and produces a canonical
// message. Codex's notify schema is younger than Claude Code's, so this
// adapter is especially defensive about field name variations.
func CodexNotify(r io.Reader) (canonical.Message, error) {
	raw, err := io.ReadAll(r)
	if err != nil {
		return canonical.Message{}, fmt.Errorf("read input: %w", err)
	}
	return CodexNotifyFromBytes(raw)
}

// CodexNotifyFromBytes parses a Codex CLI notify event JSON from a byte slice.
// Codex passes the event JSON as the last positional argv argument rather than
// on stdin, so the command layer can reach this directly with the arg payload.
func CodexNotifyFromBytes(raw []byte) (canonical.Message, error) {
	var ev map[string]interface{}
	if err := json.Unmarshal(raw, &ev); err != nil {
		return canonical.Message{}, fmt.Errorf("parse codex event: %w", err)
	}

	// Try multiple known/likely field names for the message body.
	body := firstString(ev,
		"last-assistant-message", "last_assistant_message",
		"message", "text", "msg",
	)
	if body == "" {
		body = "Codex turn complete"
	}

	turnID := firstString(ev, "turn-id", "turn_id", "session_id", "id")
	title := "Codex"
	if turnID != "" {
		title = "Codex (" + turnID + ")"
	}

	return canonical.Message{
		Title:  title,
		Body:   body,
		Source: "codex",
	}, nil
}
