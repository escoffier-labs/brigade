package channels

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/solomonneas/agent-notify/internal/canonical"
)

// Compile-time assertion that *Telegram satisfies the Channel interface.
var _ Channel = (*Telegram)(nil)

type Telegram struct {
	name     string
	apiBase  string // e.g., https://api.telegram.org (overridable for tests)
	botToken string
	chatID   string
	client   *http.Client
}

// NewTelegram constructs a Telegram channel. apiBase is typically
// "https://api.telegram.org" but is parameterized for tests.
func NewTelegram(name, apiBase, botToken, chatID string, timeout time.Duration) *Telegram {
	return &Telegram{
		name:     name,
		apiBase:  strings.TrimRight(apiBase, "/"),
		botToken: botToken,
		chatID:   chatID,
		client:   &http.Client{Timeout: timeout},
	}
}

func (t *Telegram) Name() string { return t.name }
func (t *Telegram) Type() string { return "telegram" }

type tgRequest struct {
	ChatID    string `json:"chat_id"`
	Text      string `json:"text"`
	ParseMode string `json:"parse_mode"`
}

func (t *Telegram) Send(ctx context.Context, m canonical.Message) error {
	text := formatTelegram(m)
	payload := tgRequest{
		ChatID:    t.chatID,
		Text:      text,
		ParseMode: "MarkdownV2",
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	url := fmt.Sprintf("%s/bot%s/sendMessage", t.apiBase, t.botToken)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := t.client.Do(req)
	if err != nil {
		return fmt.Errorf("post: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("telegram returned %d", resp.StatusCode)
	}
	return nil
}

func formatTelegram(m canonical.Message) string {
	var sb strings.Builder
	sb.WriteString(emojiFor(m.Level))
	sb.WriteString(" ")
	if m.Title != "" {
		sb.WriteString("*")
		sb.WriteString(escapeMDV2(m.Title))
		sb.WriteString("*\n")
	}
	sb.WriteString(escapeMDV2(m.Body))
	if len(m.Tags) > 0 {
		sb.WriteString("\n_")
		sb.WriteString(escapeMDV2(strings.Join(m.Tags, ", ")))
		sb.WriteString("_")
	}
	return sb.String()
}

// escapeMDV2 escapes the characters Telegram MarkdownV2 requires escaping
// when they appear in text (per Bot API docs).
func escapeMDV2(s string) string {
	special := []string{"_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"}
	for _, c := range special {
		s = strings.ReplaceAll(s, c, "\\"+c)
	}
	return s
}
