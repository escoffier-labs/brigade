package app

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/openclaw/logspine/internal/sources"
	"github.com/openclaw/logspine/internal/sources/claude"
	"github.com/openclaw/logspine/internal/sources/codex"
	"github.com/openclaw/logspine/internal/sources/openclaw"
)

type discoveredRoot struct {
	Kind      string
	Root      string
	Generator sources.Generator
	External  bool
}

type discoveredImportRow struct {
	SourceKind       string   `json:"source_kind"`
	Root             string   `json:"root"`
	Mode             string   `json:"mode"`
	Skipped          bool     `json:"skipped"`
	Reason           string   `json:"reason,omitempty"`
	DryRun           bool     `json:"dry_run,omitempty"`
	GeneratedRecords int      `json:"generated_records"`
	InsertedItems    int      `json:"inserted_items"`
	AlreadyKnown     bool     `json:"already_known"`
	Warnings         []string `json:"warnings"`
}

func cmdWatch(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: spine watch once|daemon [--json] [--interval DURATION]")
	}
	switch args[0] {
	case "once":
		return cmdImportDiscovered(args[1:], out, errw)
	case "daemon":
		values, _, rest, err := splitFlags(args[1:], map[string]bool{"interval": true, "limit": true, "since": true, "redact": true}, map[string]bool{"json": true, "dry-run": true})
		if err != nil {
			return fatalf(errw, "watch daemon: %s", err)
		}
		if len(rest) != 0 {
			return fatalf(errw, "usage: spine watch daemon [--interval DURATION] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
		}
		interval := time.Minute
		if values["interval"] != "" {
			parsed, err := time.ParseDuration(values["interval"])
			if err != nil || parsed <= 0 {
				return fatalf(errw, "watch daemon: invalid --interval")
			}
			interval = parsed
		}
		importArgs := stripValueFlag(args[1:], "interval")
		for {
			if code := cmdImportDiscovered(importArgs, out, errw); code != 0 {
				return code
			}
			time.Sleep(interval)
		}
	default:
		return fatalf(errw, "usage: spine watch once|daemon")
	}
}

func stripValueFlag(args []string, name string) []string {
	out := make([]string, 0, len(args))
	prefix := "--" + name + "="
	for i := 0; i < len(args); i++ {
		if args[i] == "--"+name {
			i++
			continue
		}
		if strings.HasPrefix(args[i], prefix) {
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func cmdImportDiscovered(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"limit": true, "since": true, "redact": true}, map[string]bool{"json": true, "dry-run": true})
	if err != nil {
		return fatalf(errw, "import discovered: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "usage: spine import discovered [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
	}
	limit, err := parseLimit(values["limit"], 0)
	if err != nil {
		return fatalf(errw, "import discovered: %s", err)
	}
	var db *sql.DB
	if !bools["dry-run"] {
		var openErr error
		db, _, openErr = openMigrated()
		if openErr != nil {
			return fatalf(errw, "import discovered: %s", openErr)
		}
		defer db.Close()
	}
	rows := []discoveredImportRow{}
	for _, root := range discoveredRoots() {
		row := importDiscoveredRoot(db, root, values, limit, bools["dry-run"])
		rows = append(rows, row)
	}
	totalInserted := 0
	totalGenerated := 0
	warnings := []string{}
	for _, row := range rows {
		totalInserted += row.InsertedItems
		totalGenerated += row.GeneratedRecords
		warnings = append(warnings, row.Warnings...)
		if row.Skipped && row.Reason != "" {
			warnings = append(warnings, row.SourceKind+": "+row.Reason)
		}
	}
	result := map[string]any{
		"dry_run":           bools["dry-run"],
		"generated_records": totalGenerated,
		"inserted_items":    totalInserted,
		"warnings":          warnings,
		"sources":           rows,
	}
	if bools["json"] {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "generated=%d imported=%d warnings=%d\n", totalGenerated, totalInserted, len(warnings))
	}
	return 0
}

func importDiscoveredRoot(db *sql.DB, root discoveredRoot, values map[string]string, limit int, dryRun bool) discoveredImportRow {
	row := discoveredImportRow{SourceKind: root.Kind, Root: root.Root, Mode: "native", DryRun: dryRun, Warnings: []string{}}
	if root.External {
		row.Mode = "agenttrail"
	}
	if _, err := os.Stat(root.Root); err != nil {
		row.Skipped = true
		row.Reason = "root not found"
		return row
	}
	if root.External {
		if _, err := exec.LookPath("agenttrail"); err != nil {
			row.Skipped = true
			row.Reason = "agenttrail not on PATH"
			return row
		}
		if dryRun {
			summary, err := dryRunAgentTrail(root.Kind, root.Root, values)
			if err != nil {
				row.Warnings = append(row.Warnings, err.Error())
				return row
			}
			row.GeneratedRecords = summary.Records
			row.Warnings = append(row.Warnings, summary.Warnings...)
			return row
		}
		result, summary, err := runAgentTrailImport(db, root.Kind, root.Root, values)
		if err != nil {
			row.Warnings = append(row.Warnings, err.Error())
			return row
		}
		row.GeneratedRecords = summary.Records
		row.InsertedItems = result.Inserted
		row.AlreadyKnown = result.AlreadyKnown
		row.Warnings = append(row.Warnings, result.Warnings...)
		return row
	}
	if dryRun {
		generated, err := root.Generator(root.Root, sources.Options{Limit: limit, Since: values["since"]}, io.Discard)
		if err != nil {
			row.Warnings = append(row.Warnings, err.Error())
			return row
		}
		row.GeneratedRecords = generated.Records
		row.Warnings = append(row.Warnings, generated.Warnings...)
		return row
	}
	result, generated, err := runNativeImport(db, root.Kind, root.Generator, root.Root, limit, values["since"], true)
	if err != nil {
		row.Warnings = append(row.Warnings, err.Error())
		return row
	}
	row.GeneratedRecords = generated.Records
	row.InsertedItems = result.Inserted
	row.AlreadyKnown = result.AlreadyKnown
	row.Warnings = append(row.Warnings, result.Warnings...)
	return row
}

func dryRunAgentTrail(sourceKind, root string, values map[string]string) (agentTrailSummary, error) {
	cmdArgs := []string{sourceKind, root, "--dry-run", "--json"}
	if values["limit"] != "" {
		cmdArgs = append(cmdArgs, "--limit", values["limit"])
	}
	if values["since"] != "" {
		cmdArgs = append(cmdArgs, "--since", values["since"])
	}
	if values["redact"] != "" {
		cmdArgs = append(cmdArgs, "--redact", values["redact"])
	}
	cmd := exec.Command("agenttrail", cmdArgs...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	b, err := cmd.Output()
	if err != nil {
		msg := stderr.String()
		if msg == "" {
			msg = err.Error()
		}
		return agentTrailSummary{}, fmt.Errorf("%s", msg)
	}
	var summary agentTrailSummary
	if err := json.Unmarshal(b, &summary); err != nil {
		return agentTrailSummary{}, err
	}
	return summary, nil
}

func discoveredRoots() []discoveredRoot {
	home := os.Getenv("HOME")
	return []discoveredRoot{
		{Kind: "codex", Root: filepath.Join(home, ".codex", "sessions"), Generator: codex.Generate},
		{Kind: "openclaw", Root: filepath.Join(home, ".openclaw", "agents"), Generator: openclaw.Generate},
		{Kind: "claude", Root: filepath.Join(home, ".claude", "projects"), Generator: claude.Generate},
		{Kind: "hermes", Root: filepath.Join(home, ".hermes", "sessions"), External: true},
	}
}
