// Command agent-notify dispatches notifications to Discord, Telegram, and
// Signal channels. Reads from stdin or positional arg, routes via flags
// + config, sends to channels best-effort with structured stderr on failure.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/adapter"
	"github.com/escoffier-labs/agent-notify/internal/canonical"
	"github.com/escoffier-labs/agent-notify/internal/channels"
	"github.com/escoffier-labs/agent-notify/internal/config"
	"github.com/escoffier-labs/agent-notify/internal/router"
)

const (
	exitOK       = 0
	exitFailures = 1 // returned when N>0 channel sends failed; exact count returned
	exitConfig   = 2 // returned for config / setup errors before any send is attempted
)

var (
	version   = "dev"
	commit    = "unknown"
	buildDate = "unknown"
)

func main() {
	os.Exit(run(os.Args, os.Stdin, os.Stdout, os.Stderr))
}

// run is the testable entry point. Returns an exit code.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	if len(args) > 1 {
		switch args[1] {
		case "send":
			return runSend(append([]string{args[0]}, args[2:]...), stdin, stdout, stderr)
		case "version":
			return runVersion(args[2:], stdout, stderr)
		case "init":
			return runInit(args[2:], stdout, stderr)
		case "status":
			return runStatus(args[2:], stdout, stderr)
		case "doctor":
			return runDoctor(args[2:], stdout, stderr)
		case "hooks":
			return runHooks(args[2:], stdout, stderr)
		}
	}
	return runSend(args, stdin, stdout, stderr)
}

func runSend(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.SetOutput(stderr)

	var (
		hookFlag    = fs.String("hook", "", "input adapter: claude-code-stop | claude-code-notification | codex-notify | custom")
		toFlag      = fs.String("to", "", "comma-separated channel names; overrides --profile")
		profileFlag = fs.String("profile", "", "profile name from config")
		skipFlag    = fs.String("skip", "", "comma-separated channel names to skip from resolved list")
		configPath  = fs.String("config", defaultConfigPath(), "path to TOML config file")
	)

	if err := fs.Parse(args[1:]); err != nil {
		return exitConfig
	}

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintf(stderr, "[agent-notify] config error: %v\n", err)
		return exitConfig
	}
	if len(cfg.Channels) == 0 {
		fmt.Fprintln(stderr, "[agent-notify] no channels configured (set env vars or write a config file)")
		return exitConfig
	}

	// Build the canonical message.
	msg, err := buildMessage(*hookFlag, fs.Args(), stdin)
	if err != nil {
		fmt.Fprintf(stderr, "[agent-notify] input error: %v\n", err)
		return exitConfig
	}
	if err := msg.Validate(); err != nil {
		fmt.Fprintf(stderr, "[agent-notify] message error: %v\n", err)
		return exitConfig
	}

	// Resolve channels.
	names, err := router.Resolve(cfg, *toFlag, *profileFlag, *skipFlag)
	if err != nil {
		fmt.Fprintf(stderr, "[agent-notify] routing error: %v\n", err)
		return exitConfig
	}
	if len(names) == 0 {
		fmt.Fprintln(stderr, "[agent-notify] no channels selected after routing")
		return exitConfig
	}

	// Apply the selected profile prefix. This includes the implicit default
	// profile when no explicit --profile is passed.
	if profileName := selectedProfile(cfg, *profileFlag); profileName != "" {
		if p, ok := cfg.Profiles[profileName]; ok && p.Prefix != "" {
			msg.Body = p.Prefix + msg.Body
		}
	}

	// Build registry of just the selected channels.
	reg, err := buildRegistry(cfg, names)
	if err != nil {
		fmt.Fprintf(stderr, "[agent-notify] channel build error: %v\n", err)
		return exitConfig
	}

	// Fan out, best-effort.
	failed := dispatch(reg, names, msg, stderr, time.Duration(cfg.Defaults.TimeoutSeconds)*time.Second)
	if failed > 0 {
		return failed // exit code = number of failures (>= 1)
	}
	return exitOK
}

func runVersion(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("version", flag.ContinueOnError)
	fs.SetOutput(stderr)
	jsonOut := fs.Bool("json", false, "machine-readable JSON output")
	if err := fs.Parse(args); err != nil {
		return exitConfig
	}

	payload := map[string]string{
		"version":    version,
		"commit":     commit,
		"build_date": buildDate,
		"go_version": runtime.Version(),
		"os":         runtime.GOOS,
		"arch":       runtime.GOARCH,
	}
	if *jsonOut {
		return printJSON(stdout, stderr, payload)
	}
	fmt.Fprintf(stdout, "agent-notify %s\ncommit: %s\nbuilt:  %s\ngo:     %s\nos/arch: %s/%s\n",
		version, commit, buildDate, runtime.Version(), runtime.GOOS, runtime.GOARCH)
	return exitOK
}

func runInit(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("init", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", defaultConfigPath(), "path to TOML config file")
	force := fs.Bool("force", false, "overwrite an existing config")
	if err := fs.Parse(args); err != nil {
		return exitConfig
	}
	if *configPath == "" {
		fmt.Fprintln(stderr, "[agent-notify] config path is empty")
		return exitConfig
	}
	if _, err := os.Stat(*configPath); err == nil && !*force {
		fmt.Fprintf(stderr, "[agent-notify] config already exists: %s (use --force)\n", *configPath)
		return exitConfig
	}
	if err := os.MkdirAll(filepath.Dir(*configPath), 0o700); err != nil {
		fmt.Fprintf(stderr, "[agent-notify] create config dir: %v\n", err)
		return exitConfig
	}
	if err := os.WriteFile(*configPath, []byte(sampleConfig()), 0o600); err != nil {
		fmt.Fprintf(stderr, "[agent-notify] write config: %v\n", err)
		return exitConfig
	}
	fmt.Fprintf(stdout, "wrote config to %s\n", *configPath)
	return exitOK
}

func runStatus(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", defaultConfigPath(), "path to TOML config file")
	profileFlag := fs.String("profile", "", "profile name from config")
	jsonOut := fs.Bool("json", false, "machine-readable JSON output")
	if err := fs.Parse(args); err != nil {
		return exitConfig
	}

	payload, code := inspectConfig(*configPath, *profileFlag, "", false)
	if *jsonOut {
		jsonCode := printJSON(stdout, stderr, payload)
		if jsonCode != exitOK {
			return jsonCode
		}
		return code
	}
	if code != exitOK {
		fmt.Fprintf(stderr, "[agent-notify] %s\n", payload["summary"])
		return code
	}
	fmt.Fprintf(stdout, "configured: %v\nconfig:     %s\nprofile:    %s\nchannels:   %v\n",
		payload["configured"], payload["config_path"], payload["selected_profile"], payload["selected_channels"])
	return exitOK
}

func runDoctor(args []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("doctor", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", defaultConfigPath(), "path to TOML config file")
	profileFlag := fs.String("profile", "", "profile name from config")
	skipFlag := fs.String("skip", "", "comma-separated channel names to skip from resolved list")
	skipNetwork := fs.Bool("skip-network", true, "skip live network sends; reserved for future smoke checks")
	jsonOut := fs.Bool("json", false, "machine-readable JSON output")
	if err := fs.Parse(args); err != nil {
		return exitConfig
	}

	payload, code := inspectConfig(*configPath, *profileFlag, *skipFlag, *skipNetwork)
	if *jsonOut {
		jsonCode := printJSON(stdout, stderr, payload)
		if jsonCode != exitOK {
			return jsonCode
		}
		return code
	}
	for _, row := range payload["checks"].([]map[string]string) {
		fmt.Fprintf(stdout, "[%-4s] %s: %s\n", row["status"], row["name"], row["detail"])
	}
	return code
}

func runHooks(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "print" {
		fmt.Fprintln(stderr, "usage: agent-notify hooks print <codex|claude-code> [--profile name]")
		return exitConfig
	}
	if len(args) < 2 {
		fmt.Fprintln(stderr, "usage: agent-notify hooks print <codex|claude-code> [--profile name]")
		return exitConfig
	}
	target := args[1]
	fs := flag.NewFlagSet("hooks print", flag.ContinueOnError)
	fs.SetOutput(stderr)
	profileFlag := fs.String("profile", "agent-stop", "profile name to use in hook command")
	if err := fs.Parse(args[2:]); err != nil {
		return exitConfig
	}
	profileArgs := []string{}
	if *profileFlag != "" {
		profileArgs = []string{"--profile", *profileFlag}
	}
	switch target {
	case "codex":
		parts := append([]string{"agent-notify", "--hook", "codex-notify"}, profileArgs...)
		fmt.Fprintf(stdout, "notify = [%s]\n", quoteTOMLArray(parts))
	case "claude-code":
		cmdStop := strings.Join(append([]string{"agent-notify", "--hook", "claude-code-stop"}, profileArgs...), " ")
		cmdNotification := strings.Join(append([]string{"agent-notify", "--hook", "claude-code-notification"}, profileArgs...), " ")
		fmt.Fprintf(stdout, "{\n  \"hooks\": {\n    \"Stop\": [{\"hooks\": [{\"type\": \"command\", \"command\": %q}]}],\n    \"Notification\": [{\"hooks\": [{\"type\": \"command\", \"command\": %q}]}]\n  }\n}\n", cmdStop, cmdNotification)
	default:
		fmt.Fprintf(stderr, "[agent-notify] unknown hook target %q\n", target)
		return exitConfig
	}
	return exitOK
}

func defaultConfigPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".config", "agent-notify", "config.toml")
}

func buildMessage(hook string, posArgs []string, stdin io.Reader) (canonical.Message, error) {
	switch hook {
	case "claude-code-stop":
		return adapter.ClaudeCodeStop(stdin)
	case "claude-code-notification":
		return adapter.ClaudeCodeNotification(stdin)
	case "codex-notify":
		return adapter.CodexNotify(stdin)
	case "", "custom":
		// Prefer positional arg; otherwise read stdin.
		if len(posArgs) > 0 {
			return adapter.FromString(strings.Join(posArgs, " ")), nil
		}
		return adapter.AutoDetect(stdin)
	default:
		return canonical.Message{}, fmt.Errorf("unknown --hook %q", hook)
	}
}

func selectedProfile(cfg *config.Config, explicit string) string {
	if explicit != "" {
		return explicit
	}
	for _, name := range sortedProfileNames(cfg) {
		p := cfg.Profiles[name]
		if p.Default {
			return name
		}
	}
	return ""
}

func sortedProfileNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Profiles))
	for name := range cfg.Profiles {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func buildRegistry(cfg *config.Config, names []string) (*channels.Registry, error) {
	reg := channels.NewRegistry()
	timeout := time.Duration(cfg.Defaults.TimeoutSeconds) * time.Second

	for _, name := range names {
		cc, ok := cfg.Channels[name]
		if !ok {
			return nil, fmt.Errorf("channel %q not in config", name)
		}
		switch cc.Type {
		case "discord":
			url := os.Getenv(cc.WebhookURLEnv)
			if url == "" {
				return nil, fmt.Errorf("channel %q: env %s is empty", name, cc.WebhookURLEnv)
			}
			reg.Register(name, channels.NewDiscord(name, url, timeout))
		case "telegram":
			tok := os.Getenv(cc.BotTokenEnv)
			chat := os.Getenv(cc.ChatIDEnv)
			if tok == "" || chat == "" {
				return nil, fmt.Errorf("channel %q: missing env (token or chat_id)", name)
			}
			reg.Register(name, channels.NewTelegram(name, "https://api.telegram.org", tok, chat, timeout))
		case "signal":
			url := os.Getenv(cc.URLEnv)
			from := os.Getenv(cc.FromEnv)
			to := os.Getenv(cc.ToEnv)
			if url == "" || from == "" || to == "" {
				return nil, fmt.Errorf("channel %q: missing env (url/from/to)", name)
			}
			reg.Register(name, channels.NewSignal(name, url, from, to, timeout))
		default:
			return nil, fmt.Errorf("channel %q: unknown type %q", name, cc.Type)
		}
	}
	return reg, nil
}

// dispatch sends the message to each named channel concurrently, best-effort.
// Returns the number of channels that failed.
func dispatch(reg *channels.Registry, names []string, msg canonical.Message, stderr io.Writer, timeout time.Duration) int {
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	type result struct {
		name    string
		channel string
		err     error
	}

	results := make(chan result, len(names))
	var wg sync.WaitGroup

	for _, name := range names {
		ch, ok := reg.Get(name)
		if !ok {
			results <- result{name: name, channel: "?", err: fmt.Errorf("not in registry")}
			continue
		}
		wg.Add(1)
		go func(c channels.Channel) {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()
			err := c.Send(ctx, msg)
			results <- result{name: c.Name(), channel: c.Type(), err: err}
		}(ch)
	}

	wg.Wait()
	close(results)

	failed := 0
	for r := range results {
		if r.err != nil {
			failed++
			fmt.Fprintf(stderr, "[agent-notify] FAIL channel=%s type=%s error=%v\n", r.name, r.channel, r.err)
		}
	}
	return failed
}

func inspectConfig(configPath, profileName, skip string, skippedNetwork bool) (map[string]any, int) {
	checks := []map[string]string{}
	addCheck := func(status, name, detail string) {
		checks = append(checks, map[string]string{"status": status, "name": name, "detail": detail})
	}

	configFileExists := false
	if configPath != "" {
		if _, err := os.Stat(configPath); err == nil {
			configFileExists = true
		}
	}

	cfg, err := config.Load(configPath)
	if err != nil {
		addCheck("FAIL", "config", err.Error())
		return inspectPayload(configPath, configFileExists, false, "", nil, nil, checks, skippedNetwork, "config failed to load"), exitConfig
	}
	if configFileExists {
		addCheck("OK", "config", "loaded")
	} else {
		addCheck("WARN", "config", "config file missing; using environment-only discovery")
	}

	if cfg.Defaults.TimeoutSeconds <= 0 {
		addCheck("FAIL", "defaults.timeout_seconds", "must be greater than zero")
	}

	if len(cfg.Channels) == 0 {
		addCheck("FAIL", "channels", "no channels configured")
		return inspectPayload(configPath, configFileExists, false, "", nil, nil, checks, skippedNetwork, "no channels configured"), exitConfig
	}

	defaultProfile := selectedProfile(cfg, "")
	if defaultProfile == "" {
		addCheck("WARN", "profiles.default", "no default profile; falling back to all channels")
	}
	defaultCount := 0
	for _, name := range sortedProfileNames(cfg) {
		p := cfg.Profiles[name]
		if p.Default {
			defaultCount++
		}
		for _, ch := range p.Channels {
			if _, ok := cfg.Channels[ch]; !ok {
				addCheck("FAIL", "profile:"+name, "references unknown channel "+ch)
			}
		}
	}
	if defaultCount > 1 {
		addCheck("WARN", "profiles.default", "multiple default profiles; first sorted profile wins")
	}

	names, routeErr := router.Resolve(cfg, "", profileName, skip)
	if routeErr != nil {
		addCheck("FAIL", "routing", routeErr.Error())
	}
	selected := selectedProfile(cfg, profileName)
	if profileName != "" {
		selected = profileName
	}
	if routeErr == nil && len(names) == 0 {
		addCheck("FAIL", "routing", "no channels selected")
	}
	if routeErr == nil {
		addCheck("OK", "routing", fmt.Sprintf("%d channel(s) selected", len(names)))
	}

	channelRows := inspectChannels(cfg, names, routeErr == nil, addCheck)
	failCount, warnCount := countChecks(checks)
	code := exitOK
	if failCount > 0 {
		code = exitConfig
	}
	payload := inspectPayload(configPath, configFileExists, failCount == 0 && len(cfg.Channels) > 0, selected, names, channelRows, checks, skippedNetwork, "ok")
	payload["fail_count"] = failCount
	payload["warn_count"] = warnCount
	return payload, code
}

func inspectChannels(cfg *config.Config, selected []string, routeOK bool, addCheck func(status, name, detail string)) []map[string]any {
	rows := make([]map[string]any, 0, len(cfg.Channels))
	selectedSet := make(map[string]struct{}, len(selected))
	for _, name := range selected {
		selectedSet[name] = struct{}{}
	}
	for _, name := range sortedChannelNames(cfg) {
		ch := cfg.Channels[name]
		status := "OK"
		detail := "env present"
		envPresent := true
		switch ch.Type {
		case "discord":
			envPresent = ch.WebhookURLEnv != "" && os.Getenv(ch.WebhookURLEnv) != ""
			if !envPresent {
				status, detail = "FAIL", "webhook env missing or empty"
			}
		case "telegram":
			envPresent = ch.BotTokenEnv != "" && ch.ChatIDEnv != "" && os.Getenv(ch.BotTokenEnv) != "" && os.Getenv(ch.ChatIDEnv) != ""
			if !envPresent {
				status, detail = "FAIL", "token/chat env missing or empty"
			}
		case "signal":
			envPresent = ch.URLEnv != "" && ch.FromEnv != "" && ch.ToEnv != "" && os.Getenv(ch.URLEnv) != "" && os.Getenv(ch.FromEnv) != "" && os.Getenv(ch.ToEnv) != ""
			if !envPresent {
				status, detail = "FAIL", "url/from/to env missing or empty"
			}
		default:
			envPresent = false
			status, detail = "FAIL", "unknown channel type "+ch.Type
		}
		if status == "FAIL" && routeOK && !isSelected(selectedSet, name) && strings.Contains(detail, "env") {
			status = "WARN"
			detail = "inactive channel: " + detail
		}
		addCheck(status, "channel:"+name, detail)
		rows = append(rows, map[string]any{
			"name":        name,
			"type":        ch.Type,
			"env_present": envPresent,
			"status":      status,
			"detail":      detail,
		})
	}
	return rows
}

func isSelected(selectedSet map[string]struct{}, name string) bool {
	_, ok := selectedSet[name]
	return ok
}

func sortedChannelNames(cfg *config.Config) []string {
	names := make([]string, 0, len(cfg.Channels))
	for name := range cfg.Channels {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func inspectPayload(configPath string, configFileExists, configured bool, selectedProfile string, selected []string, channels []map[string]any, checks []map[string]string, skippedNetwork bool, summary string) map[string]any {
	if selected == nil {
		selected = []string{}
	}
	if channels == nil {
		channels = []map[string]any{}
	}
	failCount, warnCount := countChecks(checks)
	return map[string]any{
		"configured":         configured,
		"config_path":        configPath,
		"config_file_exists": configFileExists,
		"selected_profile":   selectedProfile,
		"selected_channels":  selected,
		"channels":           channels,
		"checks":             checks,
		"fail_count":         failCount,
		"warn_count":         warnCount,
		"skipped_network":    skippedNetwork,
		"summary":            summary,
	}
}

func countChecks(checks []map[string]string) (int, int) {
	failCount := 0
	warnCount := 0
	for _, ck := range checks {
		switch ck["status"] {
		case "FAIL":
			failCount++
		case "WARN":
			warnCount++
		}
	}
	return failCount, warnCount
}

func printJSON(stdout, stderr io.Writer, payload any) int {
	b, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		fmt.Fprintf(stderr, "[agent-notify] json error: %v\n", err)
		return exitConfig
	}
	fmt.Fprintln(stdout, string(b))
	return exitOK
}

func quoteTOMLArray(parts []string) string {
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		encoded, _ := json.Marshal(part)
		out = append(out, string(encoded))
	}
	return strings.Join(out, ", ")
}

func sampleConfig() string {
	return `[defaults]
timeout_seconds = 10

[channels.telegram-personal]
type = "telegram"
bot_token_env = "TELEGRAM_BOT_TOKEN"
chat_id_env = "TELEGRAM_CHAT_ID"

[channels.discord-main]
type = "discord"
webhook_url_env = "DISCORD_WEBHOOK_URL"

[channels.signal-personal]
type = "signal"
url_env = "SIGNAL_CLI_URL"
from_env = "SIGNAL_FROM"
to_env = "SIGNAL_TO"

[profiles.agent-stop]
channels = ["telegram-personal", "discord-main"]
default = true

[profiles.urgent]
channels = ["telegram-personal", "discord-main", "signal-personal"]
prefix = "[urgent] "
`
}
