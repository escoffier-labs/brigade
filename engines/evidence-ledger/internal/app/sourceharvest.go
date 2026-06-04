package app

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/openclaw/logspine/internal/adapter"
	"github.com/openclaw/logspine/internal/ingest"
	"github.com/openclaw/logspine/internal/sources"
)

type sourceHarvestSummary struct {
	Source      string   `json:"source"`
	Path        string   `json:"path"`
	Records     int      `json:"records"`
	Files       int      `json:"files"`
	Warnings    []string `json:"warnings"`
	GeneratedAt string   `json:"generated_at"`
}

func cmdImportSourceHarvest(args []string, out, errw io.Writer) int {
	if len(args) < 2 {
		return fatalf(errw, "usage: spine import sourceharvest <jsonl|markdown|files|html|gitlog|json> <sourceharvest-args> [--json] [--dry-run]")
	}
	asJSON, dryRun, passArgs := splitWrapperFlags(args)
	if !hasFlag(passArgs, "out") {
		passArgs = append(passArgs, "--out", "-")
	}
	if !hasFlag(passArgs, "json") {
		passArgs = append(passArgs, "--json")
	}
	if dryRun {
		records, warnings, err := dryRunSourceHarvest(passArgs)
		if err != nil {
			return fatalf(errw, "import sourceharvest: %s", err)
		}
		result := map[string]any{"dry_run": true, "generated_records": records, "warnings": warnings}
		if asJSON {
			writeJSON(out, result)
		} else {
			fmt.Fprintf(out, "generated=%d warnings=%d\n", records, len(warnings))
		}
		return 0
	}
	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	defer db.Close()
	cmd := exec.Command("sourceharvest", passArgs...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return fatalf(errw, "import sourceharvest: %s", err)
	}
	result, importErr := ingest.ImportAdapterReader(db, stdout, "sourceharvest://"+strings.Join(passArgs, " "), "")
	waitErr := cmd.Wait()
	if importErr != nil {
		return fatalf(errw, "import sourceharvest: %s", importErr)
	}
	if waitErr != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = waitErr.Error()
		}
		return fatalf(errw, "import sourceharvest: %s", msg)
	}
	summary := parseSourceHarvestSummary(stderr.Bytes())
	result.Warnings = append(summary.Warnings, result.Warnings...)
	if summary.Path != "" {
		sourceKind := result.SourceKind
		if sourceKind == "" {
			sourceKind = summary.Source
		}
		if err := recordSourceHarvestScan(db, sourceKind, result.SourceHash, summary); err != nil {
			return fatalf(errw, "import sourceharvest: %s", err)
		}
	}
	if asJSON {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d warnings=%d already_known=%v source=%s\n", result.Inserted, len(result.Warnings), result.AlreadyKnown, result.SourceKind)
	}
	return 0
}

func dryRunSourceHarvest(args []string) (int, []string, error) {
	cmd := exec.Command("sourceharvest", args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return 0, nil, err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return 0, nil, err
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	records := 0
	warnings := []string{}
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		if _, err := adapter.Parse(line); err != nil {
			warnings = append(warnings, err.Error())
			continue
		}
		records++
	}
	if err := scanner.Err(); err != nil {
		return 0, nil, err
	}
	if err := cmd.Wait(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return 0, nil, fmt.Errorf("%s", msg)
	}
	summary := parseSourceHarvestSummary(stderr.Bytes())
	if summary.Records > records {
		records = summary.Records
	}
	warnings = append(summary.Warnings, warnings...)
	return records, warnings, nil
}

func parseSourceHarvestSummary(raw []byte) sourceHarvestSummary {
	var summary sourceHarvestSummary
	text := strings.TrimSpace(string(raw))
	if text == "" {
		return summary
	}
	lines := strings.Split(text, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimSpace(lines[i])
		if !strings.HasPrefix(line, "{") {
			continue
		}
		if json.Unmarshal([]byte(line), &summary) == nil {
			return summary
		}
	}
	return summary
}

func recordSourceHarvestScan(db *sql.DB, sourceKind, generatedHash string, summary sourceHarvestSummary) error {
	if sourceKind == "" || summary.Path == "" {
		return nil
	}
	path := summary.Path
	if abs, err := filepath.Abs(path); err == nil {
		path = abs
	}
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	contentHash := "sha256:" + hashSourceHarvestSummary(summary)
	if info.Mode().IsRegular() {
		if h, err := sources.FileHash(path); err == nil {
			contentHash = "sha256:" + h
		}
	}
	file := sources.FileScan{
		Path:        path,
		Size:        info.Size(),
		MTime:       info.ModTime().UTC().Format(time.RFC3339Nano),
		ContentHash: contentHash,
		Records:     summary.Records,
		Warnings:    len(summary.Warnings),
	}
	return ingest.RecordSourceScans(db, sourceKind, generatedHash, []sources.FileScan{file}, true)
}

func hashSourceHarvestSummary(summary sourceHarvestSummary) string {
	b, _ := json.Marshal(summary)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func splitWrapperFlags(args []string) (bool, bool, []string) {
	asJSON := false
	dryRun := false
	pass := make([]string, 0, len(args))
	for _, arg := range args {
		switch arg {
		case "--json":
			asJSON = true
		case "--dry-run":
			dryRun = true
		default:
			pass = append(pass, arg)
		}
	}
	return asJSON, dryRun, pass
}

func hasFlag(args []string, name string) bool {
	long := "--" + name
	prefix := long + "="
	for _, arg := range args {
		if arg == long || strings.HasPrefix(arg, prefix) {
			return true
		}
	}
	return false
}
