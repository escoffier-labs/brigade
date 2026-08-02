package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

type capturedRequest struct {
	method      string
	path        string
	contentType string
	body        []byte
}

var telegramAPIBaseTestMu sync.Mutex

// useTelegramAPIBase confines the package-private test endpoint override to
// one test at a time and restores the real endpoint during cleanup. The
// production read and test write are both synchronized by telegramAPIBase.
func useTelegramAPIBase(t *testing.T, baseURL string) {
	t.Helper()
	telegramAPIBaseTestMu.Lock()
	restore := setTelegramAPIBaseForTest(baseURL)
	t.Cleanup(func() {
		restore()
		telegramAPIBaseTestMu.Unlock()
	})
}

func captureRequest(r *http.Request) capturedRequest {
	body, _ := io.ReadAll(r.Body)
	return capturedRequest{
		method:      r.Method,
		path:        r.URL.Path,
		contentType: r.Header.Get("Content-Type"),
		body:        body,
	}
}

func receiveOneRequest(t *testing.T, requests <-chan capturedRequest) capturedRequest {
	t.Helper()
	// runMain waits for every dispatch worker before returning, so the channel
	// contains the complete request set when this helper inspects it.
	select {
	case request := <-requests:
		select {
		case duplicate := <-requests:
			t.Fatalf("received duplicate request: %+v", duplicate)
		default:
		}
		return request
	default:
		t.Fatal("mock endpoint received no request")
		return capturedRequest{}
	}
}

func TestE2E_TwoChannelsBothReceiveOneMessage(t *testing.T) {
	discordRequests := make(chan capturedRequest, 2)
	telegramRequests := make(chan capturedRequest, 2)

	const discordWebhookPath = "/api/webhooks/e2e-id/e2e-token"

	discordSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		discordRequests <- captureRequest(r)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer discordSrv.Close()

	telegramSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		telegramRequests <- captureRequest(r)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"result":{}}`))
	}))
	defer telegramSrv.Close()
	useTelegramAPIBase(t, telegramSrv.URL)

	const message = "fanout payload"
	code, _, stderr := runMain(t,
		[]string{"agent-notify", message},
		"",
		map[string]string{
			"DISCORD_WEBHOOK_URL": discordSrv.URL + discordWebhookPath,
			"TELEGRAM_BOT_TOKEN":  "TESTTOKEN",
			"TELEGRAM_CHAT_ID":    "12345",
		},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}

	discordRequest := receiveOneRequest(t, discordRequests)
	if discordRequest.method != http.MethodPost || discordRequest.contentType != "application/json" {
		t.Fatalf("Discord request = method %q, Content-Type %q", discordRequest.method, discordRequest.contentType)
	}
	if discordRequest.path != discordWebhookPath {
		t.Fatalf("Discord path = %q, want %q", discordRequest.path, discordWebhookPath)
	}
	var discordPayload struct {
		Embeds []struct {
			Description string `json:"description"`
		} `json:"embeds"`
	}
	if err := json.Unmarshal(discordRequest.body, &discordPayload); err != nil {
		t.Fatalf("parse Discord payload: %v", err)
	}
	if len(discordPayload.Embeds) != 1 || discordPayload.Embeds[0].Description != message {
		t.Fatalf("Discord payload = %#v, want one embed with body %q", discordPayload, message)
	}
	if strings.Count(discordPayload.Embeds[0].Description, message) != 1 {
		t.Fatalf("Discord message body duplicated: %q", discordPayload.Embeds[0].Description)
	}

	telegramRequest := receiveOneRequest(t, telegramRequests)
	if telegramRequest.method != http.MethodPost || telegramRequest.contentType != "application/json" {
		t.Fatalf("Telegram request = method %q, Content-Type %q", telegramRequest.method, telegramRequest.contentType)
	}
	if telegramRequest.path != "/botTESTTOKEN/sendMessage" {
		t.Fatalf("Telegram path = %q, want %q", telegramRequest.path, "/botTESTTOKEN/sendMessage")
	}
	var telegramPayload struct {
		ChatID    string `json:"chat_id"`
		Text      string `json:"text"`
		ParseMode string `json:"parse_mode"`
	}
	if err := json.Unmarshal(telegramRequest.body, &telegramPayload); err != nil {
		t.Fatalf("parse Telegram payload: %v", err)
	}
	if telegramPayload.ChatID != "12345" || telegramPayload.ParseMode != "MarkdownV2" || telegramPayload.Text != "ℹ️ "+message {
		t.Fatalf("Telegram payload = %#v", telegramPayload)
	}
	if strings.Count(telegramPayload.Text, message) != 1 {
		t.Fatalf("Telegram message body duplicated: %q", telegramPayload.Text)
	}
}

func TestE2E_DiscordFailureStillDeliversToTelegram(t *testing.T) {
	discordRequests := make(chan capturedRequest, 2)
	telegramRequests := make(chan capturedRequest, 2)
	discordSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		discordRequests <- captureRequest(r)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer discordSrv.Close()

	telegramSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		telegramRequests <- captureRequest(r)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"result":{}}`))
	}))
	defer telegramSrv.Close()
	useTelegramAPIBase(t, telegramSrv.URL)

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "partial failure message"},
		"",
		map[string]string{
			"DISCORD_WEBHOOK_URL": discordSrv.URL,
			"TELEGRAM_BOT_TOKEN":  "TESTTOKEN",
			"TELEGRAM_CHAT_ID":    "12345",
		},
	)
	if code != exitFailures {
		t.Fatalf("exit = %d, want partial-failure code %d (stderr = %s)", code, exitFailures, stderr)
	}
	const wantDiscordErr = "provider=discord stage=response status=500 cause=http_status"
	var discordFailLine string
	for _, line := range strings.Split(strings.TrimSpace(stderr), "\n") {
		if strings.Contains(line, "FAIL channel=discord type=discord") {
			discordFailLine = line
			break
		}
	}
	if discordFailLine == "" {
		t.Fatalf("stderr does not report the Discord failure: %q", stderr)
	}
	const errPrefix = "error="
	errIdx := strings.Index(discordFailLine, errPrefix)
	if errIdx < 0 {
		t.Fatalf("Discord FAIL line missing error field: %q", discordFailLine)
	}
	if got := discordFailLine[errIdx+len(errPrefix):]; got != wantDiscordErr {
		t.Fatalf("Discord error classification = %q, want exactly %q", got, wantDiscordErr)
	}
	if strings.Contains(stderr, "FAIL channel=telegram") {
		t.Fatalf("stderr reports a spurious Telegram failure: %q", stderr)
	}
	if receiveOneRequest(t, discordRequests).method != http.MethodPost {
		t.Fatal("Discord failure endpoint did not receive a POST")
	}
	if receiveOneRequest(t, telegramRequests).method != http.MethodPost {
		t.Fatal("Telegram did not receive a POST after Discord failed")
	}
}
