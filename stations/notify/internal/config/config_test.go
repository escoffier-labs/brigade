package config

import (
	"os"
	"path/filepath"
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
