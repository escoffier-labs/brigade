package channels

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/solomonneas/agent-notify/internal/canonical"
)

type tgPayload struct {
	ChatID    string `json:"chat_id"`
	Text      string `json:"text"`
	ParseMode string `json:"parse_mode"`
}

func TestTelegram_Send_PostsExpectedShape(t *testing.T) {
	var got tgPayload
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("expected Content-Type application/json, got %q", ct)
		}
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"result":{}}`))
	}))
	defer srv.Close()

	tg := NewTelegram("tg-personal", srv.URL, "TESTTOKEN", "12345", 5*time.Second)
	msg := canonical.Message{
		Title:  "Build",
		Body:   "all tests passed",
		Level:  "success",
		Source: "ci",
	}
	if err := tg.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send failed: %v", err)
	}

	if !strings.Contains(gotPath, "/botTESTTOKEN/sendMessage") {
		t.Errorf("expected path containing /botTESTTOKEN/sendMessage, got %s", gotPath)
	}
	if got.ChatID != "12345" {
		t.Errorf("chat_id = %q, want 12345", got.ChatID)
	}
	if got.ParseMode != "MarkdownV2" {
		t.Errorf("parse_mode = %q, want MarkdownV2", got.ParseMode)
	}
	if !strings.Contains(got.Text, "✅") {
		t.Errorf("expected success emoji in text, got %q", got.Text)
	}
	if !strings.Contains(got.Text, "all tests passed") {
		t.Errorf("expected body in text, got %q", got.Text)
	}
}

func TestTelegram_Send_ReturnsErrorOnNonOK(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"ok":false,"description":"chat not found"}`))
	}))
	defer srv.Close()

	tg := NewTelegram("tg-personal", srv.URL, "tok", "0", 5*time.Second)
	err := tg.Send(context.Background(), canonical.Message{Body: "x"})
	if err == nil {
		t.Fatal("expected error on 400, got nil")
	}
}

func TestTelegram_NameAndType(t *testing.T) {
	tg := NewTelegram("foo", "http://x", "tok", "123", time.Second)
	if tg.Name() != "foo" {
		t.Errorf("Name = %s, want foo", tg.Name())
	}
	if tg.Type() != "telegram" {
		t.Errorf("Type = %s, want telegram", tg.Type())
	}
}
