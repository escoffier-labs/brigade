package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
	"github.com/escoffier-labs/agent-notify/internal/channels"
)

type leakingErrorChannel struct {
	err error
}

func (c leakingErrorChannel) Name() string { return "leaking" }
func (c leakingErrorChannel) Type() string { return "test" }
func (c leakingErrorChannel) Send(context.Context, canonical.Message) error {
	return c.err
}

// configSubcommands are the run() subcommands whose flag sets define
// --config, mirroring the dispatch in run(). version and hooks do not accept
// --config, so injecting the flag there would be a parse error.
var configSubcommands = map[string]bool{
	"send":   true,
	"init":   true,
	"status": true,
	"doctor": true,
}

// withIsolatedConfig injects --config pointing at a guaranteed-absent path
// unless the test supplied --config itself. Without this, tests fall through
// to defaultConfigPath() and read the real config of the invoking user from
// ~/.config/agent-notify/config.toml, which makes results depend on the
// developer machine. An explicit absent path (a file inside a fresh
// t.TempDir()) works on Windows too, where overriding HOME alone does not
// change os.UserHomeDir.
func withIsolatedConfig(t *testing.T, args []string) []string {
	t.Helper()
	if len(args) == 0 {
		return args
	}
	for _, a := range args {
		if a == "--config" || strings.HasPrefix(a, "--config=") {
			return args
		}
	}
	insertAt := 1
	if len(args) > 1 {
		if configSubcommands[args[1]] {
			insertAt = 2
		} else if args[1] == "version" || args[1] == "hooks" {
			return args
		}
	}
	absent := filepath.Join(t.TempDir(), "agent-notify", "config.toml")
	isolated := append([]string{}, args[:insertAt]...)
	isolated = append(isolated, "--config", absent)
	return append(isolated, args[insertAt:]...)
}

// runMain calls the main package's run() function with the given args,
// stdin, and env vars, returning the exit code, stdout, and stderr.
func runMain(t *testing.T, args []string, stdin string, env map[string]string) (int, string, string) {
	t.Helper()
	for k, v := range env {
		t.Setenv(k, v)
	}
	stdinR := strings.NewReader(stdin)
	var stdout, stderr bytes.Buffer
	code := run(withIsolatedConfig(t, args), stdinR, &stdout, &stderr)
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

func TestRun_OneChannelFails_ExitsSendFailureCode(t *testing.T) {
	failingSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer failingSrv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "x"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": failingSrv.URL},
	)
	// Send failures use exitFailures (3), kept distinct from the config
	// error code (2) so the two cases never collide.
	if code != 3 {
		t.Fatalf("expected exit 3 for a failing channel, got %d (stderr: %s)", code, stderr)
	}
	if !strings.Contains(stderr, "FAIL channel=discord") {
		t.Errorf("expected FAIL line in stderr, got %q", stderr)
	}
}

func TestDispatch_FinalSanitizerDropsUntrustedErrorText(t *testing.T) {
	const sentinel = "SENTINEL-DISPATCH-CREDENTIAL"
	reg := channels.NewRegistry()
	reg.Register("leaking", leakingErrorChannel{err: fmt.Errorf(
		"post https://example.invalid/hooks/%s?token=%s: connection refused",
		sentinel,
		sentinel,
	)})
	var stderr bytes.Buffer

	failed := dispatch(
		reg,
		[]string{"leaking"},
		canonical.Message{Body: "test"},
		&stderr,
		time.Second,
	)

	if failed != 1 {
		t.Fatalf("failed = %d, want 1", failed)
	}
	if strings.Contains(stderr.String(), sentinel) || strings.Contains(stderr.String(), "example.invalid") {
		t.Fatalf("dispatcher leaked untrusted error text: %q", stderr.String())
	}
	if !strings.Contains(stderr.String(), "stage=dispatch") || !strings.Contains(stderr.String(), "cause=connection") {
		t.Fatalf("dispatcher omitted safe classification: %q", stderr.String())
	}
}

func TestRun_TransportFailureNeverPrintsCredential(t *testing.T) {
	const sentinel = "SENTINEL-CLI-CREDENTIAL"
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	endpoint := srv.URL + "/hooks/" + sentinel + "?token=" + sentinel
	srv.Close()

	code, stdout, stderr := runMain(t,
		[]string{"agent-notify", "test"},
		"",
		map[string]string{
			"DISCORD_WEBHOOK_URL": endpoint,
			"TELEGRAM_BOT_TOKEN":  "",
			"TELEGRAM_CHAT_ID":    "",
			"SIGNAL_CLI_URL":      "",
			"SIGNAL_FROM":         "",
			"SIGNAL_TO":           "",
		},
	)

	if code != exitFailures {
		t.Fatalf("exit = %d, want %d", code, exitFailures)
	}
	for stream, value := range map[string]string{"stdout": stdout, "stderr": stderr} {
		if strings.Contains(value, sentinel) || strings.Contains(value, "token=") {
			t.Fatalf("%s leaked credential: %q", stream, value)
		}
	}
	if !strings.Contains(stderr, "provider=discord") || !strings.Contains(stderr, "stage=send") {
		t.Fatalf("stderr omitted safe transport fields: %q", stderr)
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

func TestRun_SendSubcommandPreservesDispatch(t *testing.T) {
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "send", "build", "done"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	embeds, _ := got["embeds"].([]interface{})
	if len(embeds) != 1 {
		t.Fatalf("expected one embed, got %#v", got["embeds"])
	}
	embed, _ := embeds[0].(map[string]interface{})
	if embed["description"] != "build done" {
		t.Fatalf("description = %#v, want build done", embed["description"])
	}
}

func TestRun_DefaultProfilePrefixApplies(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(cfgPath, []byte(`
[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[profiles.operator]
channels = ["discord-main"]
default = true
prefix = "[agent] "
`), 0o644); err != nil {
		t.Fatal(err)
	}
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "--config", cfgPath, "done"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	embeds, _ := got["embeds"].([]interface{})
	embed, _ := embeds[0].(map[string]interface{})
	if embed["description"] != "[agent] done" {
		t.Fatalf("description = %#v, want prefixed body", embed["description"])
	}
}

func TestRun_MultipleDefaultProfilesUseStableOrder(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.toml")
	if err := os.WriteFile(cfgPath, []byte(`
[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[profiles.z-last]
channels = ["discord-main"]
default = true
prefix = "[z] "

[profiles.a-first]
channels = ["discord-main"]
default = true
prefix = "[a] "
`), 0o644); err != nil {
		t.Fatal(err)
	}
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	code, _, stderr := runMain(t,
		[]string{"agent-notify", "--config", cfgPath, "done"},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	embeds, _ := got["embeds"].([]interface{})
	embed, _ := embeds[0].(map[string]interface{})
	if embed["description"] != "[a] done" {
		t.Fatalf("description = %#v, want stable first default prefix", embed["description"])
	}
}

func TestRun_VersionJSON(t *testing.T) {
	code, stdout, stderr := runMain(t, []string{"agent-notify", "version", "--json"}, "", nil)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatalf("invalid json: %v\n%s", err, stdout)
	}
	if payload["version"] == "" || payload["go_version"] == "" {
		t.Fatalf("missing version fields: %#v", payload)
	}
}

func TestRun_InitWritesSampleConfig(t *testing.T) {
	cfgPath := filepath.Join(t.TempDir(), "agent-notify", "config.toml")
	code, _, stderr := runMain(t, []string{"agent-notify", "init", "--config", cfgPath}, "", nil)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	body, err := os.ReadFile(cfgPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "[profiles.agent-stop]") {
		t.Fatalf("sample config missing agent-stop profile:\n%s", body)
	}
}

func TestRun_DoctorJSONReportsMissingConfigAsUnconfigured(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "missing.toml")
	for _, k := range []string{"DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SIGNAL_CLI_URL", "SIGNAL_FROM", "SIGNAL_TO"} {
		os.Unsetenv(k)
	}
	code, stdout, stderr := runMain(t, []string{"agent-notify", "doctor", "--json", "--config", missing}, "", nil)
	if code != 2 {
		t.Fatalf("exit = %d, want 2 (stderr=%s)", code, stderr)
	}
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatalf("invalid json: %v\n%s", err, stdout)
	}
	if payload["configured"] != false {
		t.Fatalf("configured = %#v, want false", payload["configured"])
	}
	if payload["fail_count"].(float64) == 0 {
		t.Fatalf("expected failures, got %#v", payload)
	}
}

func TestRun_StatusJSONWarnsForInactiveChannelMissingEnv(t *testing.T) {
	cfgPath := filepath.Join(t.TempDir(), "config.toml")
	if err := os.WriteFile(cfgPath, []byte(`
[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[channels.signal-personal]
type = "signal"
url_env = "SIGNAL_CLI_URL"
from_env = "SIGNAL_FROM"
to_env = "SIGNAL_TO"

[profiles.operator]
channels = ["discord-main"]
default = true
`), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"SIGNAL_CLI_URL", "SIGNAL_FROM", "SIGNAL_TO"} {
		os.Unsetenv(k)
	}

	code, stdout, stderr := runMain(t,
		[]string{"agent-notify", "status", "--json", "--config", cfgPath},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": "http://127.0.0.1:1/webhook"},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s\nstdout=%s", code, stderr, stdout)
	}
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(stdout), &payload); err != nil {
		t.Fatalf("invalid json: %v\n%s", err, stdout)
	}
	if payload["fail_count"].(float64) != 0 {
		t.Fatalf("fail_count = %#v, want 0: %#v", payload["fail_count"], payload)
	}
	if payload["warn_count"].(float64) == 0 {
		t.Fatalf("expected inactive channel warning: %#v", payload)
	}
}

func TestRun_CodexNotifyFromArg_UsesPositionalEventJSON(t *testing.T) {
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	// Codex passes the event JSON as the last positional argv argument and
	// nothing on stdin. Body should come from the arg payload, not stdin.
	event := `{"type":"agent-turn-complete","turn-id":"turn-99","last-assistant-message":"all green from arg"}`
	code, _, stderr := runMain(t,
		[]string{"agent-notify", "--hook", "codex-notify", event},
		"",
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	embeds, _ := got["embeds"].([]interface{})
	if len(embeds) != 1 {
		t.Fatalf("expected one embed, got %#v", got["embeds"])
	}
	embed, _ := embeds[0].(map[string]interface{})
	if embed["description"] != "all green from arg" {
		t.Fatalf("description = %#v, want body parsed from positional arg", embed["description"])
	}
	if title, _ := embed["title"].(string); title != "Codex (turn-99)" {
		t.Fatalf("title = %#v, want Codex (turn-99)", embed["title"])
	}
}

func TestRun_CodexNotifyNoArgs_FallsBackToStdin(t *testing.T) {
	var got map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	event := `{"type":"agent-turn-complete","last-assistant-message":"from stdin"}`
	code, _, stderr := runMain(t,
		[]string{"agent-notify", "--hook", "codex-notify"},
		event,
		map[string]string{"DISCORD_WEBHOOK_URL": srv.URL},
	)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	embeds, _ := got["embeds"].([]interface{})
	embed, _ := embeds[0].(map[string]interface{})
	if embed["description"] != "from stdin" {
		t.Fatalf("description = %#v, want body parsed from stdin", embed["description"])
	}
}

func TestRun_HooksPrintCodex(t *testing.T) {
	code, stdout, stderr := runMain(t, []string{"agent-notify", "hooks", "print", "codex", "--profile", "agent-stop"}, "", nil)
	if code != 0 {
		t.Fatalf("exit = %d, stderr = %s", code, stderr)
	}
	want := `notify = ["agent-notify", "--hook", "codex-notify", "--profile", "agent-stop"]`
	if strings.TrimSpace(stdout) != want {
		t.Fatalf("stdout = %q, want %q", strings.TrimSpace(stdout), want)
	}
}
