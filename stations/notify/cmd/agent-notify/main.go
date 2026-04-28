// Command agent-notify dispatches notifications to Discord, Telegram, and
// Signal channels. Reads from stdin or positional arg, routes via flags
// + config, sends to channels best-effort with structured stderr on failure.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/solomonneas/agent-notify/internal/adapter"
	"github.com/solomonneas/agent-notify/internal/canonical"
	"github.com/solomonneas/agent-notify/internal/channels"
	"github.com/solomonneas/agent-notify/internal/config"
	"github.com/solomonneas/agent-notify/internal/router"
)

const (
	exitOK       = 0
	exitFailures = 1 // returned when N>0 channel sends failed; exact count returned
	exitConfig   = 2 // returned for config / setup errors before any send is attempted
)

func main() {
	os.Exit(run(os.Args, os.Stdin, os.Stdout, os.Stderr))
}

// run is the testable entry point. Returns an exit code.
func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
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

	// Apply profile prefix if a profile was used.
	if *profileFlag != "" {
		if p, ok := cfg.Profiles[*profileFlag]; ok && p.Prefix != "" {
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
	failed := dispatch(reg, names, msg, stderr)
	if failed > 0 {
		return failed // exit code = number of failures (>= 1)
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
			return adapter.FromString(posArgs[0]), nil
		}
		return adapter.AutoDetect(stdin)
	default:
		return canonical.Message{}, fmt.Errorf("unknown --hook %q", hook)
	}
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
func dispatch(reg *channels.Registry, names []string, msg canonical.Message, stderr io.Writer) int {
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
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
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
