// Package canonical defines the internal message shape that all input adapters
// produce and all channel adapters consume.
package canonical

import "fmt"

type Message struct {
	Title  string   `json:"title,omitempty"`
	Body   string   `json:"body"`
	Level  string   `json:"level,omitempty"`
	Source string   `json:"source,omitempty"`
	Tags   []string `json:"tags,omitempty"`
}

var validLevels = map[string]struct{}{
	"info":    {},
	"warn":    {},
	"error":   {},
	"success": {},
}

// Validate checks required fields and normalizes optional ones.
// On success, mutates the receiver to apply defaults (e.g., Level = "info").
func (m *Message) Validate() error {
	if m.Body == "" {
		return fmt.Errorf("body is required")
	}
	if m.Level == "" {
		m.Level = "info"
		return nil
	}
	if _, ok := validLevels[m.Level]; !ok {
		return fmt.Errorf("invalid level %q: must be one of info, warn, error, success", m.Level)
	}
	return nil
}
