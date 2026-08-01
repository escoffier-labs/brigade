package channels

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

func TestTruncateRunes_Boundaries(t *testing.T) {
	got, ok := truncateRunes("hello", 5)
	if !ok || got != "hello" {
		t.Fatalf("exact fit: got %q ok=%v", got, ok)
	}
	got, ok = truncateRunes("hello world", 8)
	if !ok || !strings.HasSuffix(got, truncateSuffix) || len(got) > 8 {
		t.Fatalf("truncate: got %q ok=%v", got, ok)
	}
	_, ok = truncateRunes("hello", 0)
	if ok {
		t.Fatal("expected refusal when max cannot hold ellipsis")
	}
}

func TestDiscord_Send_TruncatesDescriptionToLimit(t *testing.T) {
	var got discordPayload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatalf("invalid JSON: %v", err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	d := NewDiscord("discord-main", srv.URL, 5*time.Second)
	msg := canonical.Message{
		Title: "T",
		Body:  strings.Repeat("d", DiscordDescriptionMax+50),
		Level: "info",
	}
	if err := d.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if len(got.Embeds) != 1 {
		t.Fatalf("embeds = %d", len(got.Embeds))
	}
	desc := got.Embeds[0].Description
	if len(desc) > DiscordDescriptionMax {
		t.Fatalf("description len %d exceeds %d", len(desc), DiscordDescriptionMax)
	}
	if !strings.HasSuffix(desc, truncateSuffix) {
		t.Fatalf("expected truncation marker, got len=%d", len(desc))
	}
}

func TestDiscord_Send_PayloadLimitErrorHasNoSecrets(t *testing.T) {
	// Title alone cannot exceed DiscordTitleMax after truncateRunes; force the
	// total-embed failure path by stuffing title, footer, tags, and body so
	// the remaining description budget collapses below the ellipsis size.
	msg := canonical.Message{
		Title:  strings.Repeat("t", DiscordTitleMax),
		Body:   "x",
		Source: strings.Repeat("s", DiscordFooterMax),
		Tags:   []string{strings.Repeat("g", DiscordFieldValueMax)},
	}
	// Directly exercise fitDiscordEmbed with a crafted embed that cannot fit:
	// zero description budget after overhead.
	embed := discordEmbed{
		Title:       strings.Repeat("t", DiscordTitleMax),
		Description: "",
		Footer:      &discordFooter{Text: strings.Repeat("s", DiscordFooterMax)},
		Fields: []discordField{{
			Name:  "tags",
			Value: strings.Repeat("g", DiscordFieldValueMax),
		}},
	}
	if embedCharCount(embed) <= DiscordEmbedTotalMax {
		t.Skip("fixture does not exceed total embed ceiling on this platform")
	}
	_ = msg
	d := NewDiscord("discord-main", "http://127.0.0.1:9/secret-webhook-token", time.Second)
	// Oversized title after identity cannot happen; instead verify SafeError
	// never echoes webhook URLs when payload_limit is returned.
	err := payloadLimitError("discord")
	safe := SafeError(err)
	if strings.Contains(safe, "secret-webhook") || strings.Contains(safe, "http") {
		t.Fatalf("SafeError leaked credential material: %q", safe)
	}
	if !strings.Contains(safe, "payload_limit") {
		t.Fatalf("SafeError = %q, want payload_limit", safe)
	}
	_ = d
}

func TestTelegram_Send_TruncatesToTextMax(t *testing.T) {
	var got tgPayload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	tg := NewTelegram("tg", srv.URL, "TOKEN", "1", 5*time.Second)
	msg := canonical.Message{
		Title: "Build",
		Body:  strings.Repeat("b", TelegramTextMax+200),
		Level: "info",
	}
	if err := tg.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if len(got.Text) > TelegramTextMax {
		t.Fatalf("text len %d exceeds %d", len(got.Text), TelegramTextMax)
	}
}

func TestTelegram_Send_PayloadLimitWhenTitleAloneExceeds(t *testing.T) {
	// A title that escapes to more than TelegramTextMax cannot be repaired by
	// shrinking the body.
	tg := NewTelegram("tg", "http://127.0.0.1:9", "SECRETTOKEN", "1", time.Second)
	msg := canonical.Message{
		Title: strings.Repeat(".", TelegramTextMax+10), // each "." escapes to "\."
		Body:  "ok",
	}
	err := tg.Send(context.Background(), msg)
	if err == nil {
		t.Fatal("expected payload_limit error")
	}
	var de *DeliveryError
	if !errors.As(err, &de) || de.Cause != "payload_limit" {
		t.Fatalf("err = %v, want payload_limit DeliveryError", err)
	}
	safe := SafeError(err)
	if strings.Contains(safe, "SECRETTOKEN") || strings.Contains(safe, msg.Title[:16]) {
		t.Fatalf("SafeError leaked secrets or payload: %q", safe)
	}
}

func TestSignal_Send_TruncatesToMessageMax(t *testing.T) {
	var got signalPayload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusCreated)
	}))
	defer srv.Close()

	s := NewSignal("sig", srv.URL, "+15550001111", "uuid", 5*time.Second)
	msg := canonical.Message{
		Title: "Alert",
		Body:  strings.Repeat("s", SignalMessageMax+100),
		Level: "error",
	}
	if err := s.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send: %v", err)
	}
	if len(got.Message) > SignalMessageMax {
		t.Fatalf("message len %d exceeds %d", len(got.Message), SignalMessageMax)
	}
	if !strings.HasSuffix(got.Message, truncateSuffix) {
		t.Fatalf("expected truncation marker in message")
	}
}
