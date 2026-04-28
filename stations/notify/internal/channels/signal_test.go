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

type signalPayload struct {
	Message    string   `json:"message"`
	Number     string   `json:"number"`
	Recipients []string `json:"recipients"`
}

func TestSignal_Send_PostsExpectedShape(t *testing.T) {
	var got signalPayload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("expected Content-Type application/json, got %q", ct)
		}
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusCreated)
	}))
	defer srv.Close()

	s := NewSignal("signal-personal", srv.URL, "+15551112222", "uuid-abc", 5*time.Second)
	msg := canonical.Message{
		Title: "Alert",
		Body:  "wazuh: 5 alerts",
		Level: "error",
	}
	if err := s.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send failed: %v", err)
	}

	if got.Number != "+15551112222" {
		t.Errorf("number = %s, want +15551112222", got.Number)
	}
	if len(got.Recipients) != 1 || got.Recipients[0] != "uuid-abc" {
		t.Errorf("recipients = %v, want [uuid-abc]", got.Recipients)
	}
	if !strings.Contains(got.Message, "🚨") {
		t.Errorf("expected error emoji, got %q", got.Message)
	}
	if !strings.Contains(got.Message, "Alert") {
		t.Errorf("expected title, got %q", got.Message)
	}
	if !strings.Contains(got.Message, "wazuh: 5 alerts") {
		t.Errorf("expected body, got %q", got.Message)
	}
}

func TestSignal_Send_ReturnsErrorOnFailure(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	s := NewSignal("signal-personal", srv.URL, "+1", "u", 5*time.Second)
	err := s.Send(context.Background(), canonical.Message{Body: "x"})
	if err == nil {
		t.Fatal("expected error on 500, got nil")
	}
}

func TestSignal_NameAndType(t *testing.T) {
	s := NewSignal("sig-personal", "http://x", "+1", "uuid", time.Second)
	if s.Name() != "sig-personal" {
		t.Errorf("Name = %s, want sig-personal", s.Name())
	}
	if s.Type() != "signal" {
		t.Errorf("Type = %s, want signal", s.Type())
	}
}
