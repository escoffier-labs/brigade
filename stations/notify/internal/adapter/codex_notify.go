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

	model := firstString(ev, "model", "model-name", "model_name")
	if model == "" {
		// Codex's notify event often omits the model; fall back to the model
		// recorded for this turn in the Codex CLI session rollout. Resolve by
		// the explicit thread id only (never session_id, which is not a
		// thread alias).
		threadID := firstString(ev, "thread-id", "thread_id")
		if threadID != "" && turnID != "" {
			model = resolveCodexModel(threadID, turnID)
		}
	}

	return canonical.Message{
		Title:  title,
		Body:   body,
		Source: "codex",
		Model:  model,
	}, nil
}
