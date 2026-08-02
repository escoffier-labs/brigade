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

func TestDiscord_OverheadCeilingLeavesDescriptionBudget(t *testing.T) {
	// Guards the arithmetic that makes payload_limit unreachable from
	// Discord.Send: if per-field ceilings ever grow so the description budget
	// can collapse below the ellipsis, Send must be tested through Send.
	maxOverhead := DiscordTitleMax + DiscordFooterMax + len("tags") + DiscordFieldValueMax
	if budget := DiscordEmbedTotalMax - maxOverhead; budget < len(truncateSuffix) {
		t.Fatalf("description budget %d < ellipsis %d: payload_limit reachable via Send", budget, len(truncateSuffix))
	}
}

func TestSafeError_PayloadLimitHasNoSecrets(t *testing.T) {
	// payload_limit is not reachable from Discord.Send under the current
	// per-field ceilings: maximum non-description overhead is
	// DiscordTitleMax + DiscordFooterMax + len("tags") + DiscordFieldValueMax
	// = 3332, leaving a description budget of 6000-3332 = 2668, far above
	// len(truncateSuffix), so fitDiscordEmbed always truncates and sends (a
	// large-body Send fixture only truncates). The payload_limit
	// sanitization contract is therefore tested directly here.
	err := payloadLimitError("discord")
	safe := SafeError(err)
	if strings.Contains(safe, "http") || strings.Contains(safe, "webhook") {
		t.Fatalf("SafeError leaked credential material: %q", safe)
	}
	if !strings.Contains(safe, "payload_limit") {
		t.Fatalf("SafeError = %q, want payload_limit", safe)
	}
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

func TestTelegram_FitShortBodyNearLimitOverhead(t *testing.T) {
	// Regression: fitTelegramText's binary search shrank hi when truncateRunes
	// rejected a too-small mid, discarding every larger candidate. A short
	// body whose fixed overhead lands just under TelegramTextMax then failed
	// with payload_limit even though an ellipsis-only body fits.
	msg := canonical.Message{Level: "info", Body: "abcde"}
	// Grow a plain (escape-free) title until the full text just exceeds
	// TelegramTextMax; collapsing the 5-byte body to the ellipsis then fits.
	fixed := len(formatTelegram(msg))           // indicator + " " + body, no title
	titleLen := TelegramTextMax + 2 - fixed - 3 // 3 = "*", title, "*\n"
	msg.Title = strings.Repeat("x", titleLen)
	if got := len(formatTelegram(msg)); got <= TelegramTextMax {
		t.Fatalf("fixture must exceed TelegramTextMax, got %d", got)
	}
	text, err := fitTelegramText(msg)
	if err != nil {
		t.Fatalf("fitTelegramText: %v; short body near-limit overhead must truncate, not fail", err)
	}
	if len(text) > TelegramTextMax {
		t.Fatalf("text len %d exceeds %d", len(text), TelegramTextMax)
	}
	if !strings.Contains(text, truncateSuffix) {
		t.Fatal("expected truncation marker in fitted text")
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
