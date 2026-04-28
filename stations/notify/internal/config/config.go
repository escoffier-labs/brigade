// Package config loads agent-notify configuration from a TOML file, with an
// env-only fast path that activates when no config file is present.
package config

import (
	"errors"
	"fmt"
	"os"

	"github.com/BurntSushi/toml"
)

// Config is the parsed configuration tree.
type Config struct {
	Channels map[string]ChannelConfig `toml:"channels"`
	Profiles map[string]ProfileConfig `toml:"profiles"`
	Defaults Defaults                 `toml:"defaults"`
}

type ChannelConfig struct {
	Type string `toml:"type"`

	// Discord
	WebhookURLEnv string `toml:"webhook_url_env"`

	// Telegram
	BotTokenEnv string `toml:"bot_token_env"`
	ChatIDEnv   string `toml:"chat_id_env"`

	// Signal
	URLEnv  string `toml:"url_env"`
	FromEnv string `toml:"from_env"`
	ToEnv   string `toml:"to_env"`
}

type ProfileConfig struct {
	Channels []string `toml:"channels"`
	Default  bool     `toml:"default"`
	Prefix   string   `toml:"prefix"`
}

type Defaults struct {
	TimeoutSeconds int `toml:"timeout_seconds"`
}

// Load reads the TOML file at path, falling back to env-only mode if it
// does not exist. Returns a populated *Config or an error explaining
// what's wrong with the file.
func Load(path string) (*Config, error) {
	cfg := &Config{
		Channels: make(map[string]ChannelConfig),
		Profiles: make(map[string]ProfileConfig),
		Defaults: Defaults{TimeoutSeconds: 10},
	}

	_, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		populateFromEnv(cfg)
		return cfg, nil
	}
	if err != nil {
		return nil, fmt.Errorf("stat config: %w", err)
	}

	if _, err := toml.DecodeFile(path, cfg); err != nil {
		return nil, fmt.Errorf("decode config: %w", err)
	}

	if cfg.Defaults.TimeoutSeconds == 0 {
		cfg.Defaults.TimeoutSeconds = 10
	}
	return cfg, nil
}

// populateFromEnv builds an implicit config from environment variables only.
// Reads conventional env var names for each channel type and registers
// channels under their type name (e.g., "discord", "telegram", "signal").
// All discovered channels are added to a single implicit "default" profile.
func populateFromEnv(cfg *Config) {
	var discovered []string

	if os.Getenv("DISCORD_WEBHOOK_URL") != "" {
		cfg.Channels["discord"] = ChannelConfig{
			Type:          "discord",
			WebhookURLEnv: "DISCORD_WEBHOOK_URL",
		}
		discovered = append(discovered, "discord")
	}
	if os.Getenv("TELEGRAM_BOT_TOKEN") != "" && os.Getenv("TELEGRAM_CHAT_ID") != "" {
		cfg.Channels["telegram"] = ChannelConfig{
			Type:        "telegram",
			BotTokenEnv: "TELEGRAM_BOT_TOKEN",
			ChatIDEnv:   "TELEGRAM_CHAT_ID",
		}
		discovered = append(discovered, "telegram")
	}
	if os.Getenv("SIGNAL_CLI_URL") != "" && os.Getenv("SIGNAL_FROM") != "" && os.Getenv("SIGNAL_TO") != "" {
		cfg.Channels["signal"] = ChannelConfig{
			Type:    "signal",
			URLEnv:  "SIGNAL_CLI_URL",
			FromEnv: "SIGNAL_FROM",
			ToEnv:   "SIGNAL_TO",
		}
		discovered = append(discovered, "signal")
	}

	if len(discovered) > 0 {
		cfg.Profiles["default"] = ProfileConfig{
			Channels: discovered,
			Default:  true,
		}
	}
}
