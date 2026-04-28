// Package adapter converts heterogeneous input shapes into the canonical
// message used by the router and channel adapters.
package adapter

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/solomonneas/agent-notify/internal/canonical"
)

// FromString builds a canonical message from a plain string body.
func FromString(s string) canonical.Message {
	return canonical.Message{Body: s}
}

// AutoDetect inspects the reader content and returns a canonical message.
//
// Detection rules:
//   - Empty input returns an error.
//   - If the input is valid JSON AND has a non-empty "body" field, it is
//     treated as canonical JSON (any extra fields are accepted but ignored).
//   - If the input is valid JSON but lacks a "body" field, it is treated
//     as a non-canonical structured message and returns a helpful error.
//   - Otherwise the entire input is treated as a plain string body.
func AutoDetect(r io.Reader) (canonical.Message, error) {
	raw, err := io.ReadAll(r)
	if err != nil {
		return canonical.Message{}, fmt.Errorf("read input: %w", err)
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return canonical.Message{}, fmt.Errorf("input is empty")
	}

	// Try canonical JSON first if it looks like a JSON object.
	if strings.HasPrefix(trimmed, "{") {
		var m canonical.Message
		if err := json.Unmarshal([]byte(trimmed), &m); err == nil && m.Body != "" {
			return m, nil
		}
		// Looks like JSON but not canonical (missing body or unparseable).
		return canonical.Message{}, fmt.Errorf("input parsed as JSON but missing required \"body\" field; pass plain string or canonical {title,body,level,source,tags} JSON")
	}

	return FromString(trimmed), nil
}
