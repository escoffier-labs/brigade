package config

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoad_EnvOnlyFastPath_DiscordOnly(t *testing.T) {
	t.Setenv("DISCORD_WEBHOOK_URL", "https://discord.test/webhook/123")
	t.Setenv("TELEGRAM_BOT_TOKEN", "")
	t.Setenv("SIGNAL_CLI_URL", "")

	cfg, err := Load("/nonexistent/config.toml")
	if err != nil {
		t.Fatalf("expected fast-path success, got %v", err)
	}
	if len(cfg.Channels) != 1 {
		t.Fatalf("expected 1 channel from env, got %d", len(cfg.Channels))
	}
	c, ok := cfg.Channels["discord"]
	if !ok {
		t.Fatal("expected discord channel registered")
	}
	if c.Type != "discord" {
		t.Errorf("expected type=discord, got %s", c.Type)
	}
	if len(cfg.Profiles) != 1 {
		t.Fatalf("expected 1 implicit default profile, got %d", len(cfg.Profiles))
	}
	p, ok := cfg.Profiles["default"]
	if !ok || !p.Default {
		t.Fatal("expected an implicit default profile named 'default'")
	}
	if cfg.ClaudeCodeStop.IncludeCWD || cfg.ClaudeCodeStop.IncludeSessionID {
		t.Fatalf("Claude Code Stop disclosures = %#v, want both disabled by default", cfg.ClaudeCodeStop)
	}
}

func TestLoad_EnvOnlyFastPath_AllThreeChannels(t *testing.T) {
	t.Setenv("DISCORD_WEBHOOK_URL", "https://discord.test/x")
	t.Setenv("TELEGRAM_BOT_TOKEN", "tok")
	t.Setenv("TELEGRAM_CHAT_ID", "123")
	t.Setenv("SIGNAL_CLI_URL", "http://sig.test/v2/send")
	t.Setenv("SIGNAL_FROM", "+15551112222")
	t.Setenv("SIGNAL_TO", "uuid-123")

	cfg, err := Load("/nonexistent/config.toml")
	if err != nil {
		t.Fatalf("expected fast-path success, got %v", err)
	}
	if len(cfg.Channels) != 3 {
		t.Fatalf("expected 3 channels, got %d", len(cfg.Channels))
	}
	if len(cfg.Profiles["default"].Channels) != 3 {
		t.Errorf("expected 3 channels in default profile, got %d", len(cfg.Profiles["default"].Channels))
	}
}

func TestLoad_TOML_Parses(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[channels.tg-personal]
type = "telegram"
bot_token_env = "TG_TOKEN"
chat_id_env = "TG_CHAT"

[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_URL"

[profiles.agent-stop]
channels = ["tg-personal", "discord-main"]
default = true

[profiles.error]
channels = ["tg-personal"]
prefix = "🚨 "

[claude_code_stop]
include_cwd = true
include_session_id = true
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if len(cfg.Channels) != 2 {
		t.Errorf("expected 2 channels, got %d", len(cfg.Channels))
	}
	if cfg.Channels["tg-personal"].Type != "telegram" {
		t.Errorf("tg-personal type wrong: %s", cfg.Channels["tg-personal"].Type)
	}
	if !cfg.Profiles["agent-stop"].Default {
		t.Error("expected agent-stop to be default")
	}
	if cfg.Profiles["error"].Prefix != "🚨 " {
		t.Errorf("expected error prefix '🚨 ', got %q", cfg.Profiles["error"].Prefix)
	}
	if !cfg.ClaudeCodeStop.IncludeCWD || !cfg.ClaudeCodeStop.IncludeSessionID {
		t.Errorf("Claude Code Stop disclosures = %#v, want both enabled", cfg.ClaudeCodeStop)
	}
}

func TestLoad_ClaudeCodeStopDisclosureOptions(t *testing.T) {
	tests := []struct {
		name                  string
		body                  string
		wantCWD               bool
		wantSessionIdentifier bool
	}{
		{name: "cwd only", body: "include_cwd = true\ninclude_session_id = false", wantCWD: true},
		{name: "session only", body: "include_cwd = false\ninclude_session_id = true", wantSessionIdentifier: true},
		{name: "both", body: "include_cwd = true\ninclude_session_id = true", wantCWD: true, wantSessionIdentifier: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.toml")
			body := "[claude_code_stop]\n" + tt.body + "\n"
			if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}

			cfg, err := Load(path)
			if err != nil {
				t.Fatalf("Load failed: %v", err)
			}
			if cfg.ClaudeCodeStop.IncludeCWD != tt.wantCWD {
				t.Errorf("IncludeCWD = %t, want %t", cfg.ClaudeCodeStop.IncludeCWD, tt.wantCWD)
			}
			if cfg.ClaudeCodeStop.IncludeSessionID != tt.wantSessionIdentifier {
				t.Errorf("IncludeSessionID = %t, want %t", cfg.ClaudeCodeStop.IncludeSessionID, tt.wantSessionIdentifier)
			}
		})
	}
}

func TestLoad_DefaultTimeoutIs10s(t *testing.T) {
	cfg, err := Load("/nonexistent/config.toml")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Defaults.TimeoutSeconds != 10 {
		t.Errorf("expected default timeout 10s, got %d", cfg.Defaults.TimeoutSeconds)
	}
}

func TestLoad_ZeroTimeoutFails(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[defaults]
timeout_seconds = 0
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	_, err := Load(path)
	if err == nil {
		t.Fatal("expected Load to fail for explicit zero timeout")
	}
	cfgErr, ok := AsConfigError(err)
	if !ok {
		t.Fatalf("expected ConfigError, got %T: %v", err, err)
	}
	if cfgErr.Field != "defaults.timeout_seconds" {
		t.Errorf("field = %q, want defaults.timeout_seconds", cfgErr.Field)
	}
}

func TestLoad_NegativeTimeoutFails(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[defaults]
timeout_seconds = -5
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	_, err := Load(path)
	if err == nil {
		t.Fatal("expected Load to fail for negative timeout")
	}
	cfgErr, ok := AsConfigError(err)
	if !ok {
		t.Fatalf("expected ConfigError, got %T: %v", err, err)
	}
	if cfgErr.Field != "defaults.timeout_seconds" {
		t.Errorf("field = %q, want defaults.timeout_seconds", cfgErr.Field)
	}
	if !strings.Contains(cfgErr.Detail, "-5") {
		t.Errorf("detail = %q, want observed value -5", cfgErr.Detail)
	}
}

func TestLoad_AbsentTimeoutDefaultsTo10s(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_URL"
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if cfg.Defaults.TimeoutSeconds != 10 {
		t.Errorf("expected absent timeout to default to 10s, got %d", cfg.Defaults.TimeoutSeconds)
	}
}

func TestLoad_OneSecondTimeoutAccepted(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[defaults]
timeout_seconds = 1
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if cfg.Defaults.TimeoutSeconds != 1 {
		t.Errorf("expected timeout 1s, got %d", cfg.Defaults.TimeoutSeconds)
	}
}

func TestLoad_PositiveTimeoutUnchanged(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.toml")
	body := `
[defaults]
timeout_seconds = 30
`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load failed: %v", err)
	}
	if cfg.Defaults.TimeoutSeconds != 30 {
		t.Errorf("expected timeout 30s, got %d", cfg.Defaults.TimeoutSeconds)
	}
}
