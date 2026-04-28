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

// Compile-time assertion that *Discord satisfies the Channel interface.
var _ Channel = (*Discord)(nil)

// Color values for Discord embed sidebar (RGB ints).
const (
	colorInfo    = 0x3498DB // blue
	colorWarn    = 0xF1C40F // yellow
	colorError   = 0xE74C3C // red
	colorSuccess = 0x2ECC71 // green
)

type Discord struct {
	name       string
	webhookURL string
	client     *http.Client
}

func NewDiscord(name, webhookURL string, timeout time.Duration) *Discord {
	return &Discord{
		name:       name,
		webhookURL: webhookURL,
		client:     &http.Client{Timeout: timeout},
	}
}

func (d *Discord) Name() string { return d.name }
func (d *Discord) Type() string { return "discord" }

type discordEmbed struct {
	Title       string         `json:"title,omitempty"`
	Description string         `json:"description"`
	Color       int            `json:"color"`
	Footer      *discordFooter `json:"footer,omitempty"`
	Fields      []discordField `json:"fields,omitempty"`
}

type discordFooter struct {
	Text string `json:"text"`
}

type discordField struct {
	Name   string `json:"name"`
	Value  string `json:"value"`
	Inline bool   `json:"inline"`
}

type discordRequest struct {
	Embeds []discordEmbed `json:"embeds"`
}

func (d *Discord) Send(ctx context.Context, m canonical.Message) error {
	embed := discordEmbed{
		Title:       m.Title,
		Description: m.Body,
		Color:       colorFor(m.Level),
	}
	if m.Source != "" {
		embed.Footer = &discordFooter{Text: m.Source}
	}
	if len(m.Tags) > 0 {
		embed.Fields = []discordField{{
			Name:   "tags",
			Value:  strings.Join(m.Tags, ", "),
			Inline: true,
		}}
	}

	payload := discordRequest{Embeds: []discordEmbed{embed}}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, d.webhookURL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := d.client.Do(req)
	if err != nil {
		return fmt.Errorf("post: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		return fmt.Errorf("discord returned %d", resp.StatusCode)
	}
	return nil
}

func colorFor(level string) int {
	switch level {
	case "warn":
		return colorWarn
	case "error":
		return colorError
	case "success":
		return colorSuccess
	default:
		return colorInfo
	}
}

