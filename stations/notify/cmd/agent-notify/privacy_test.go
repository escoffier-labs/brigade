package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
)

// recordingTransport wraps the default transport to record every outbound URL.
type recordingTransport struct {
	mu   sync.Mutex
	urls []string
	rt   http.RoundTripper
}

func (r *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	r.mu.Lock()
	r.urls = append(r.urls, req.URL.Host+req.URL.Path)
	r.mu.Unlock()
	return r.rt.RoundTrip(req)
}

// TestPrivacy_NoUnconfiguredHTTP verifies that running agent-notify only
// produces HTTP calls to configured channel endpoints - no telemetry, no
// update checks, no cloud round-trips.
func TestPrivacy_NoUnconfiguredHTTP(t *testing.T) {
	// Replace the default transport so we can see every outbound call.
	rec := &recordingTransport{rt: http.DefaultTransport}
	prev := http.DefaultTransport
	http.DefaultTransport = rec
	t.Cleanup(func() { http.DefaultTransport = prev })

	// One configured channel.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	// Strip "http://" so we can compare against recorded "host+path".
	expectedHost := strings.TrimPrefix(srv.URL, "http://")

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "test message"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("expected exit 0, got %d (stderr: %s)", code, stderr)
	}

	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.urls) == 0 {
		t.Fatal("no HTTP calls recorded; channel not actually called?")
	}
	for _, u := range rec.urls {
		if !strings.HasPrefix(u, expectedHost) {
			t.Errorf("unexpected outbound HTTP to %q (only configured: %q)", u, expectedHost)
		}
	}
}

// TestPrivacy_ZeroHTTPWhenNoChannelsConfigured asserts that with no channels
// configured, agent-notify makes ZERO outbound HTTP calls (no telemetry, no
// update checks, no liveness probes - nothing).
func TestPrivacy_ZeroHTTPWhenNoChannelsConfigured(t *testing.T) {
	rec := &recordingTransport{rt: http.DefaultTransport}
	prev := http.DefaultTransport
	http.DefaultTransport = rec
	t.Cleanup(func() { http.DefaultTransport = prev })

	// Explicitly clear all known channel env vars to prevent leakage from
	// parallel or prior tests.
	for _, k := range []string{
		"DISCORD_WEBHOOK_URL",
		"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
		"SIGNAL_CLI_URL", "SIGNAL_FROM", "SIGNAL_TO",
	} {
		os.Unsetenv(k)
	}

	// No env vars set -> no channels -> exit 2 from setup.
	code, _, _ := runMain(t,
		[]string{"agent-notify", "test"},
		"",
		nil,
	)
	if code != 2 {
		t.Fatalf("expected exit 2 with no channels, got %d", code)
	}

	rec.mu.Lock()
	defer rec.mu.Unlock()
	if len(rec.urls) != 0 {
		t.Errorf("expected zero HTTP calls with no channels, got %d: %v", len(rec.urls), rec.urls)
	}
}
