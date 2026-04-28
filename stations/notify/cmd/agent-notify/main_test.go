package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// runMain calls the main package's run() function with the given args,
// stdin, and env vars, returning the exit code, stdout, and stderr.
func runMain(t *testing.T, args []string, stdin string, env map[string]string) (int, string, string) {
	t.Helper()
	for k, v := range env {
		t.Setenv(k, v)
	}
	stdinR := strings.NewReader(stdin)
	var stdout, stderr bytes.Buffer
	code := run(args, stdinR, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

func TestRun_PlainStringToDiscord_ExitsZero(t *testing.T) {
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "build done"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	if got == nil {
		t.Fatal("Discord webhook never received the request")
	}
}

func TestRun_NoChannelsConfigured_Exit2(t *testing.T) {
	for _, k := range []string{"DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SIGNAL_CLI_URL", "SIGNAL_FROM", "SIGNAL_TO"} {
		os.Unsetenv(k)
	}
	code, _, stderr := runMain(t,
		[]string{"agent-notify", "hello"},
		"",
		nil,
	)
	if code != 2 {
		t.Fatalf("expected exit 2 for no channels, got %d (stderr: %s)", code, stderr)
	}
	if !strings.Contains(stderr, "no channels configured") {
		t.Errorf("expected stderr to mention no channels, got %q", stderr)
	}
}

func TestRun_OneChannelFails_ExitsOne(t *testing.T) {
	failingSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer failingSrv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "x"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": failingSrv.URL},
	)
	if code != 1 {
		t.Fatalf("expected exit 1 for one failing channel, got %d (stderr: %s)", code, stderr)
	}
	if !strings.Contains(stderr, "FAIL channel=discord") {
		t.Errorf("expected FAIL line in stderr, got %q", stderr)
	}
}

func TestRun_StdinStringWorks(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify"},
		"piped message",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	if hits != 1 {
		t.Errorf("expected 1 webhook hit, got %d", hits)
	}
}
