package app

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type scheduleConfig struct {
	Interval time.Duration
	Jobs     []scheduleJob
}

type scheduleJob struct {
	Name    string
	Command string
	Args    []string
	Enabled bool
}

type scheduleJobResult struct {
	Name     string   `json:"name"`
	Command  string   `json:"command"`
	Args     []string `json:"args"`
	Skipped  bool     `json:"skipped,omitempty"`
	ExitCode int      `json:"exit_code"`
	Stdout   string   `json:"stdout,omitempty"`
	Stderr   string   `json:"stderr,omitempty"`
	Error    string   `json:"error,omitempty"`
}

type scheduleRunResult struct {
	ConfigPath  string              `json:"config_path"`
	Jobs        []scheduleJobResult `json:"jobs"`
	Successful  int                 `json:"successful_jobs"`
	Failed      int                 `json:"failed_jobs"`
	Skipped     int                 `json:"skipped_jobs"`
	StartedAt   string              `json:"started_at"`
	CompletedAt string              `json:"completed_at"`
	DurationMS  int64               `json:"duration_ms"`
}

func cmdSchedule(args []string, out, errw io.Writer) int {
	if len(args) == 0 || hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: miseledger schedule run|daemon <config.toml> [--json] [--interval DURATION] [--max-runs N]")
		return 0
	}
	switch args[0] {
	case "run":
		return cmdScheduleRun(args[1:], out, errw)
	case "daemon":
		return cmdScheduleDaemon(args[1:], out, errw)
	default:
		return fatalf(errw, "usage: miseledger schedule run|daemon <config.toml> [--json] [--interval DURATION] [--max-runs N]")
	}
}

func cmdScheduleRun(args []string, out, errw io.Writer) int {
	_, bools, rest, err := splitFlags(args, nil, map[string]bool{"json": true})
	if err != nil {
		return fatalf(errw, "schedule run: %s", err)
	}
	if len(rest) != 1 {
		return fatalf(errw, "usage: miseledger schedule run <config.toml> [--json]")
	}
	result, code := runScheduleConfig(rest[0])
	if bools["json"] {
		writeJSON(out, result)
		return code
	}
	for _, job := range result.Jobs {
		if job.Skipped {
			fmt.Fprintf(out, "skipped %s\n", job.Name)
			continue
		}
		if job.ExitCode == 0 {
			fmt.Fprintf(out, "ok %s\n", job.Name)
		} else {
			fmt.Fprintf(out, "failed %s exit=%d\n", job.Name, job.ExitCode)
			if job.Error != "" {
				fmt.Fprintf(errw, "%s: %s\n", job.Name, job.Error)
			}
		}
	}
	return code
}

func cmdScheduleDaemon(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"interval": true, "max-runs": true}, map[string]bool{"json": true})
	if err != nil {
		return fatalf(errw, "schedule daemon: %s", err)
	}
	if len(rest) != 1 {
		return fatalf(errw, "usage: miseledger schedule daemon <config.toml> [--interval DURATION] [--max-runs N] [--json]")
	}
	cfg, err := readScheduleConfig(rest[0])
	if err != nil {
		return fatalf(errw, "schedule daemon: %s", err)
	}
	interval := cfg.Interval
	if interval <= 0 {
		interval = time.Minute
	}
	if values["interval"] != "" {
		parsed, err := time.ParseDuration(values["interval"])
		if err != nil || parsed <= 0 {
			return fatalf(errw, "schedule daemon: invalid --interval")
		}
		interval = parsed
	}
	maxRuns, err := parseLimit(values["max-runs"], 0)
	if err != nil {
		return fatalf(errw, "schedule daemon: invalid --max-runs")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	runs := []scheduleRunResult{}
	failedRuns := 0
	for {
		run, code := executeSchedule(rest[0], cfg)
		runs = append(runs, run)
		if code != 0 {
			failedRuns++
		}
		if !bools["json"] {
			fmt.Fprintf(out, "schedule run %d: jobs=%d failed=%d skipped=%d\n", len(runs), len(run.Jobs), run.Failed, run.Skipped)
		}
		if maxRuns > 0 && len(runs) >= maxRuns {
			break
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			fmt.Fprintf(errw, "schedule stopped\n")
			maxRuns = len(runs)
		case <-timer.C:
		}
		if maxRuns > 0 && len(runs) >= maxRuns {
			break
		}
	}
	result := map[string]any{
		"config_path": rest[0],
		"runs":        len(runs),
		"failed_runs": failedRuns,
		"interval":    interval.String(),
		"run_results": runs,
	}
	if bools["json"] {
		writeJSON(out, result)
	}
	if failedRuns > 0 {
		return 1
	}
	return 0
}

func runScheduleConfig(path string) (scheduleRunResult, int) {
	cfg, err := readScheduleConfig(path)
	if err != nil {
		now := time.Now().UTC().Format(time.RFC3339Nano)
		return scheduleRunResult{
			ConfigPath:  path,
			StartedAt:   now,
			CompletedAt: now,
			Failed:      1,
			Jobs: []scheduleJobResult{{
				Name:     "config",
				ExitCode: 1,
				Error:    err.Error(),
			}},
		}, 1
	}
	return executeSchedule(path, cfg)
}

func executeSchedule(path string, cfg scheduleConfig) (scheduleRunResult, int) {
	start := time.Now().UTC()
	result := scheduleRunResult{
		ConfigPath: path,
		StartedAt:  start.Format(time.RFC3339Nano),
		Jobs:       []scheduleJobResult{},
	}
	for _, job := range cfg.Jobs {
		jobResult := scheduleJobResult{
			Name:    job.Name,
			Command: job.Command,
			Args:    append([]string(nil), job.Args...),
		}
		if !job.Enabled {
			jobResult.Skipped = true
			result.Skipped++
			result.Jobs = append(result.Jobs, jobResult)
			continue
		}
		var stdout, stderr bytes.Buffer
		jobResult.ExitCode = runScheduledCommand(job.Command, job.Args, &stdout, &stderr)
		jobResult.Stdout = strings.TrimSpace(stdout.String())
		jobResult.Stderr = strings.TrimSpace(stderr.String())
		if jobResult.ExitCode == 0 {
			result.Successful++
		} else {
			result.Failed++
			if jobResult.Stderr != "" {
				jobResult.Error = jobResult.Stderr
			} else {
				jobResult.Error = fmt.Sprintf("exit code %d", jobResult.ExitCode)
			}
		}
		result.Jobs = append(result.Jobs, jobResult)
	}
	completed := time.Now().UTC()
	result.CompletedAt = completed.Format(time.RFC3339Nano)
	result.DurationMS = completed.Sub(start).Milliseconds()
	if result.Failed > 0 {
		return result, 1
	}
	return result, 0
}

func runScheduledCommand(command string, args []string, out, errw io.Writer) int {
	switch command {
	case "crawl":
		return cmdCrawl(args, out, errw)
	case "import":
		return cmdImport(args, out, errw)
	case "watch":
		return cmdWatch(args, out, errw)
	case "adapter":
		return cmdAdapter(args, out, errw)
	case "relations":
		return cmdRelations(args, out, errw)
	case "compact":
		return cmdCompact(args, out, errw)
	default:
		return fatalf(errw, "unknown scheduled command: %s", command)
	}
}

func readScheduleConfig(path string) (scheduleConfig, error) {
	body, err := os.ReadFile(path)
	if err != nil {
		return scheduleConfig{}, err
	}
	cfg, err := parseScheduleConfig(string(body))
	if err != nil {
		return scheduleConfig{}, err
	}
	if len(cfg.Jobs) == 0 {
		return scheduleConfig{}, fmt.Errorf("no schedule jobs configured")
	}
	for i := range cfg.Jobs {
		if cfg.Jobs[i].Name == "" {
			cfg.Jobs[i].Name = fmt.Sprintf("job-%d", i+1)
		}
		if cfg.Jobs[i].Command == "" {
			return scheduleConfig{}, fmt.Errorf("%s: command is required", cfg.Jobs[i].Name)
		}
	}
	return cfg, nil
}

func parseScheduleConfig(body string) (scheduleConfig, error) {
	cfg := scheduleConfig{}
	var current *scheduleJob
	lines := strings.Split(body, "\n")
	for i, raw := range lines {
		line := stripScheduleComment(strings.TrimSpace(raw))
		if line == "" {
			continue
		}
		if line == "[[jobs]]" {
			cfg.Jobs = append(cfg.Jobs, scheduleJob{Enabled: true})
			current = &cfg.Jobs[len(cfg.Jobs)-1]
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			return scheduleConfig{}, fmt.Errorf("line %d: expected key = value", i+1)
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		if current == nil {
			switch key {
			case "interval":
				intervalText, err := parseScheduleString(value)
				if err != nil {
					return scheduleConfig{}, fmt.Errorf("line %d: %s", i+1, err)
				}
				interval, err := time.ParseDuration(intervalText)
				if err != nil || interval <= 0 {
					return scheduleConfig{}, fmt.Errorf("line %d: invalid interval", i+1)
				}
				cfg.Interval = interval
			default:
				return scheduleConfig{}, fmt.Errorf("line %d: unknown top-level key %q", i+1, key)
			}
			continue
		}
		switch key {
		case "name":
			v, err := parseScheduleString(value)
			if err != nil {
				return scheduleConfig{}, fmt.Errorf("line %d: %s", i+1, err)
			}
			current.Name = v
		case "command":
			v, err := parseScheduleString(value)
			if err != nil {
				return scheduleConfig{}, fmt.Errorf("line %d: %s", i+1, err)
			}
			current.Command = v
		case "args":
			v, err := parseScheduleStringArray(value)
			if err != nil {
				return scheduleConfig{}, fmt.Errorf("line %d: %s", i+1, err)
			}
			current.Args = v
		case "enabled":
			v, err := strconv.ParseBool(value)
			if err != nil {
				return scheduleConfig{}, fmt.Errorf("line %d: invalid enabled value", i+1)
			}
			current.Enabled = v
		default:
			return scheduleConfig{}, fmt.Errorf("line %d: unknown job key %q", i+1, key)
		}
	}
	return cfg, nil
}

func stripScheduleComment(line string) string {
	inString := false
	escaped := false
	for i, r := range line {
		if escaped {
			escaped = false
			continue
		}
		if r == '\\' && inString {
			escaped = true
			continue
		}
		if r == '"' {
			inString = !inString
			continue
		}
		if r == '#' && !inString {
			return strings.TrimSpace(line[:i])
		}
	}
	return line
}

func parseScheduleString(value string) (string, error) {
	unquoted, err := strconv.Unquote(value)
	if err != nil {
		return "", fmt.Errorf("expected quoted string")
	}
	return unquoted, nil
}

func parseScheduleStringArray(value string) ([]string, error) {
	value = strings.TrimSpace(value)
	if !strings.HasPrefix(value, "[") || !strings.HasSuffix(value, "]") {
		return nil, fmt.Errorf("expected string array")
	}
	inner := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(value, "["), "]"))
	if inner == "" {
		return []string{}, nil
	}
	parts := []string{}
	for _, raw := range splitScheduleArray(inner) {
		v, err := parseScheduleString(strings.TrimSpace(raw))
		if err != nil {
			return nil, err
		}
		parts = append(parts, v)
	}
	return parts, nil
}

func splitScheduleArray(inner string) []string {
	parts := []string{}
	start := 0
	inString := false
	escaped := false
	for i, r := range inner {
		if escaped {
			escaped = false
			continue
		}
		if r == '\\' && inString {
			escaped = true
			continue
		}
		if r == '"' {
			inString = !inString
			continue
		}
		if r == ',' && !inString {
			parts = append(parts, inner[start:i])
			start = i + 1
		}
	}
	parts = append(parts, inner[start:])
	return parts
}
