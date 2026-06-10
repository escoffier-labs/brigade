package channels

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

type discordPayload struct {
	Embeds []struct {
		Title       string `json:"title"`
		Description string `json:"description"`
		Color       int    `json:"color"`
		Footer      *struct {
			Text string `json:"text"`
		} `json:"footer,omitempty"`
		Fields []struct {
			Name   string `json:"name"`
			Value  string `json:"value"`
			Inline bool   `json:"inline"`
		} `json:"fields,omitempty"`
	} `json:"embeds"`
}

func TestDiscord_Send_PostsExpectedShape(t *testing.T) {
	var got discordPayload
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("expected Content-Type application/json, got %q", ct)
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &got); err != nil {
			t.Fatalf("invalid JSON to webhook: %v", err)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	d := NewDiscord("discord-main", srv.URL, 5*time.Second)
	msg := canonical.Message{
		Title:  "Build done",
		Body:   "All tests passed",
		Level:  "success",
		Source: "ci",
		Tags:   []string{"main", "12345"},
	}
	if err := d.Send(context.Background(), msg); err != nil {
		t.Fatalf("Send failed: %v", err)
	}

	if len(got.Embeds) != 1 {
		t.Fatalf("expected 1 embed, got %d", len(got.Embeds))
	}
	e := got.Embeds[0]
	if e.Title != "Build done" {
		t.Errorf("title wrong: %q", e.Title)
	}
	if e.Description != "All tests passed" {
		t.Errorf("description wrong: %q", e.Description)
	}
	const greenSuccess = 0x2ECC71
	if e.Color != greenSuccess {
		t.Errorf("expected success color %d, got %d", greenSuccess, e.Color)
	}
	if e.Footer == nil || e.Footer.Text != "ci" {
		t.Errorf("expected footer 'ci', got %+v", e.Footer)
	}
	if len(e.Fields) != 1 {
		t.Errorf("expected 1 tag field, got %d", len(e.Fields))
	}
}

func TestDiscord_Send_ReturnsErrorOn5xx(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	d := NewDiscord("discord-main", srv.URL, 5*time.Second)
	err := d.Send(context.Background(), canonical.Message{Body: "x"})
	if err == nil {
		t.Fatal("expected error on 500 response, got nil")
	}
}

func TestDiscord_NameAndType(t *testing.T) {
	d := NewDiscord("foo", "http://x", time.Second)
	if d.Name() != "foo" {
		t.Errorf("Name = %s, want foo", d.Name())
	}
	if d.Type() != "discord" {
		t.Errorf("Type = %s, want discord", d.Type())
	}
}
