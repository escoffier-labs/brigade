package app

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/sources/claude"
	"github.com/escoffier-labs/miseledger/internal/sources/codex"
	"github.com/escoffier-labs/miseledger/internal/sources/hermes"
	"github.com/escoffier-labs/miseledger/internal/sources/openclaw"
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
	// Failed marks a hard error (generator or import failed) as distinct from
	// a parse-level warning or a benign skip. Error carries the message.
	Failed bool   `json:"failed,omitempty"`
	Error  string `json:"error,omitempty"`
}

func cmdWatch(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger watch once|daemon [--json] [--interval DURATION]")
	}
	switch args[0] {
	case "once":
		ifChanged, importArgs, err := parseWatchOnceArgs(args[1:])
		if err != nil {
			return fatalf(errw, "watch once: %s", err)
		}
		if ifChanged {
			shouldRun, err := shouldImportForChangedScans()
			if err != nil {
				return fatalf(errw, "watch once: %s", err)
			}
			if !shouldRun {
				writeJSON(out, map[string]any{"skipped": true, "reason": "no changed scans"})
				return 0
			}
		}
		return cmdImportDiscovered(importArgs, out, errw)
	case "daemon":
		values, _, rest, err := splitFlags(args[1:], map[string]bool{"interval": true, "limit": true, "since": true, "redact": true, "max-runs": true}, map[string]bool{"json": true, "dry-run": true, "if-changed": true})
		if err != nil {
			return fatalf(errw, "watch daemon: %s", err)
		}
		if len(rest) != 0 {
			return fatalf(errw, "usage: miseledger watch daemon [--interval DURATION] [--max-runs N] [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
		}
		interval := time.Minute
		if values["interval"] != "" {
			parsed, err := time.ParseDuration(values["interval"])
			if err != nil || parsed <= 0 {
				return fatalf(errw, "watch daemon: invalid --interval")
			}
			interval = parsed
		}
		maxRuns, err := parseLimit(values["max-runs"], 0)
		if err != nil {
			return fatalf(errw, "watch daemon: invalid --max-runs")
		}
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		importArgs := stripValueFlag(stripValueFlag(args[1:], "interval"), "max-runs")
		runs := 0
		for {
			if hasBoolFlag(args[1:], "if-changed") {
				shouldRun, err := shouldImportForChangedScans()
				if err != nil {
					return fatalf(errw, "watch daemon: %s", err)
				}
				if !shouldRun {
					fmt.Fprintf(errw, "watch skipped: no changed scans\n")
				} else if code := cmdImportDiscovered(importArgs, out, errw); code != 0 {
					return code
				}
			} else if code := cmdImportDiscovered(importArgs, out, errw); code != 0 {
				return code
			}
			runs++
			if maxRuns > 0 && runs >= maxRuns {
				return 0
			}
			timer := time.NewTimer(interval)
			select {
			case <-ctx.Done():
				timer.Stop()
				fmt.Fprintf(errw, "watch stopped\n")
				return 0
			case <-timer.C:
			}
		}
	default:
		return fatalf(errw, "usage: miseledger watch once|daemon")
	}
}

func parseWatchOnceArgs(args []string) (bool, []string, error) {
	_, bools, rest, err := splitFlags(args, map[string]bool{"limit": true, "since": true, "redact": true}, map[string]bool{"json": true, "dry-run": true, "if-changed": true})
	if err != nil {
		return false, nil, err
	}
	if len(rest) != 0 {
		return false, nil, fmt.Errorf("usage: miseledger watch once [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
	}
	return bools["if-changed"], stripBoolFlag(args, "if-changed"), nil
}

func shouldImportForChangedScans() (bool, error) {
	db, _, err := openMigrated()
	if err != nil {
		return false, err
	}
	defer db.Close()
	var scans int
	if err := db.QueryRow(`select count(*) from source_scans`).Scan(&scans); err != nil {
		return false, err
	}
	if scans == 0 {
		return true, nil
	}
	changed, err := changedScans(db, "")
	if err != nil {
		return false, err
	}
	return len(changed) > 0, nil
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

func stripBoolFlag(args []string, name string) []string {
	out := make([]string, 0, len(args))
	long := "--" + name
	for _, arg := range args {
		if arg == long {
			continue
		}
		out = append(out, arg)
	}
	return out
}

func hasBoolFlag(args []string, name string) bool {
	long := "--" + name
	for _, arg := range args {
		if arg == long {
			return true
		}
	}
	return false
}

func cmdImportDiscovered(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"limit": true, "since": true, "redact": true}, map[string]bool{"json": true, "dry-run": true})
	if err != nil {
		return fatalf(errw, "import discovered: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "usage: miseledger import discovered [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
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
	failures := []string{}
	for _, row := range rows {
		totalInserted += row.InsertedItems
		totalGenerated += row.GeneratedRecords
		// Attribute every warning to its source so a flat list is still
		// traceable (skip reasons were already prefixed; parse warnings were not).
		for _, w := range row.Warnings {
			warnings = append(warnings, row.SourceKind+": "+w)
		}
		if row.Skipped && row.Reason != "" {
			warnings = append(warnings, row.SourceKind+": "+row.Reason)
		}
		if row.Failed {
			failures = append(failures, row.SourceKind+": "+row.Error)
		}
	}
	result := map[string]any{
		"dry_run":           bools["dry-run"],
		"generated_records": totalGenerated,
		"inserted_items":    totalInserted,
		"warnings":          warnings,
		"failures":          failures,
		"sources":           rows,
	}
	if bools["json"] {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "generated=%d imported=%d warnings=%d failures=%d\n", totalGenerated, totalInserted, len(warnings), len(failures))
		for _, f := range failures {
			fmt.Fprintf(errw, "import failed: %s\n", f)
		}
	}
	// A hard failure in any source is an error, not a silent generated=0.
	if len(failures) > 0 {
		return 1
	}
	return 0
}

func importDiscoveredRoot(db *sql.DB, root discoveredRoot, values map[string]string, limit int, dryRun bool) discoveredImportRow {
	row := discoveredImportRow{SourceKind: root.Kind, Root: root.Root, Mode: "native", DryRun: dryRun, Warnings: []string{}}
	if root.External {
		row.Mode = "stationtrail"
	}
	if _, err := os.Stat(root.Root); err != nil {
		row.Skipped = true
		row.Reason = "root not found"
		return row
	}
	if root.External {
		if _, err := exec.LookPath("stationtrail"); err != nil {
			row.Skipped = true
			row.Reason = "stationtrail not on PATH"
			return row
		}
		if dryRun {
			summary, err := dryRunStationTrail(root.Kind, root.Root, values)
			if err != nil {
				row.Failed = true
				row.Error = err.Error()
				return row
			}
			row.GeneratedRecords = summary.Records
			row.Warnings = append(row.Warnings, summary.Warnings...)
			return row
		}
		result, summary, err := runStationTrailImport(db, root.Kind, root.Root, values)
		if err != nil {
			row.Failed = true
			row.Error = err.Error()
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
			row.Failed = true
			row.Error = err.Error()
			return row
		}
		row.GeneratedRecords = generated.Records
		row.Warnings = append(row.Warnings, generated.Warnings...)
		return row
	}
	result, generated, err := runNativeImport(db, root.Kind, root.Generator, root.Root, limit, values["since"], true)
	if err != nil {
		row.Failed = true
		row.Error = err.Error()
		return row
	}
	row.GeneratedRecords = generated.Records
	row.InsertedItems = result.Inserted
	row.AlreadyKnown = result.AlreadyKnown
	row.Warnings = append(row.Warnings, result.Warnings...)
	return row
}

func dryRunStationTrail(sourceKind, root string, values map[string]string) (stationTrailSummary, error) {
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
	cmd := exec.Command("stationtrail", cmdArgs...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	b, err := cmd.Output()
	if err != nil {
		msg := stderr.String()
		if msg == "" {
			msg = err.Error()
		}
		return stationTrailSummary{}, fmt.Errorf("%s", msg)
	}
	var summary stationTrailSummary
	if err := json.Unmarshal(b, &summary); err != nil {
		return stationTrailSummary{}, err
	}
	return summary, nil
}

func discoveredRoots() []discoveredRoot {
	home := os.Getenv("HOME")
	return []discoveredRoot{
		{Kind: "codex", Root: filepath.Join(home, ".codex", "sessions"), Generator: codex.Generate},
		{Kind: "openclaw", Root: filepath.Join(home, ".openclaw", "agents"), Generator: openclaw.Generate},
		{Kind: "claude", Root: filepath.Join(home, ".claude", "projects"), Generator: claude.Generate},
		{Kind: "hermes", Root: filepath.Join(home, ".hermes", "sessions"), Generator: hermes.Generate},
	}
}
