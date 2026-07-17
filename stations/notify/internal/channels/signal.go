package channels

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

// Compile-time assertion that *Signal satisfies the Channel interface.
var _ Channel = (*Signal)(nil)

type Signal struct {
	name      string
	url       string
	from      string
	recipient string
	client    *http.Client
}

func NewSignal(name, url, from, recipient string, timeout time.Duration) *Signal {
	return &Signal{
		name:      name,
		url:       url,
		from:      from,
		recipient: recipient,
		client:    &http.Client{Timeout: timeout},
	}
}

func (s *Signal) Name() string { return s.name }
func (s *Signal) Type() string { return "signal" }

type signalRequest struct {
	Message    string   `json:"message"`
	Number     string   `json:"number"`
	Recipients []string `json:"recipients"`
}

func (s *Signal) Send(ctx context.Context, m canonical.Message) error {
	text := formatSignal(m)
	payload := signalRequest{
		Message:    text,
		Number:     s.from,
		Recipients: []string{s.recipient},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return encodingError(s.Type())
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(body))
	if err != nil {
		return requestError(s.Type())
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return transportError(s.Type(), "send", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return statusError(s.Type(), resp.StatusCode)
	}
	return nil
}

func formatSignal(m canonical.Message) string {
	var sb strings.Builder
	sb.WriteString(emojiFor(m.Level))
	sb.WriteString(" ")
	if m.Title != "" {
		sb.WriteString(m.Title)
		sb.WriteString("\n")
	}
	sb.WriteString(m.Body)
	if len(m.Tags) > 0 {
		sb.WriteString("\n[")
		sb.WriteString(strings.Join(m.Tags, ", "))
		sb.WriteString("]")
	}
	return sb.String()
}
