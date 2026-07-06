package app

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/ingest"
)

// nativeExporter describes a crawler binary that emits miseledger.adapter.v1
// JSONL via a `<binary> export adapter` subcommand. Each entry in the crawl
// router that maps to such a crawler shares cmdCrawlExporter, so MiseLedger
// never duplicates a crawler's SQLite schema; the crawler owns its own mapping.
type nativeExporter struct {
	binary     string   // executable on PATH, e.g. "discrawl"
	sourceKind string   // adapter source kind recorded on import, e.g. "discord"
	exportCmd  []string // subcommand that emits adapter JSONL; nil => ["export", "adapter"]
	usage      string   // one-line usage shown for --help
}

// adapterCmd is the subcommand prefix that emits adapter JSONL. Most crawlers
// expose `<binary> export adapter`; a few (e.g. mailcrawl) use their own verb.
func (e nativeExporter) adapterCmd() []string {
	if len(e.exportCmd) == 0 {
		return []string{"export", "adapter"}
	}
	return e.exportCmd
}

var nativeExporters = map[string]nativeExporter{
	"discord":  {binary: "discrawl", sourceKind: "discord", usage: "miseledger crawl discord [--since RFC3339] [--limit N] [--channel NAME] [--guild ID] [--json] [--dry-run]"},
	"github":   {binary: "gitcrawl", sourceKind: "github", usage: "miseledger crawl github [--repo OWNER/NAME] [--state open|closed|all] [--limit N] [--json] [--dry-run]"},
	"slack":    {binary: "slacrawl", sourceKind: "slack", usage: "miseledger crawl slack [--workspace ID] [--channel ID] [--limit N] [--json] [--dry-run]"},
	"granola":  {binary: "graincrawl", sourceKind: "granola", usage: "miseledger crawl granola [--limit N] [--json] [--dry-run]"},
	"notion":   {binary: "notcrawl", sourceKind: "notion", usage: "miseledger crawl notion [--limit N] [--json] [--dry-run]"},
	"gmail":    {binary: "mailcrawl", sourceKind: "gmail", exportCmd: []string{"gmail", "export"}, usage: "miseledger crawl gmail --account EMAIL --query QUERY [--limit N] [--metadata-only] [--json] [--dry-run]"},
	"telegram": {binary: "telecrawl", sourceKind: "telegram", usage: "miseledger crawl telegram [--chat NAME] [--limit N] [--json] [--dry-run]"},
}

// cmdCrawlExporter shells out to a crawler's `export adapter` subcommand and
// streams its miseledger.adapter.v1 JSONL straight into the adapter importer.
// It mirrors cmdImportSourceHarvest but talks to a source-owned exporter binary.
// Pass-through flags go to the crawler unchanged; --json and --dry-run are
// handled here.
func cmdCrawlExporter(ex nativeExporter, args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: "+ex.usage)
		return 0
	}
	asJSON, dryRun, passArgs := splitWrapperFlags(args)
	exportArgs := append(append(ex.adapterCmd(), "--out", "-"), passArgs...)

	if dryRun {
		records, warnings, err := dryRunExporter(ex.binary, exportArgs)
		if err != nil {
			return fatalf(errw, "crawl %s: %s", ex.sourceKind, err)
		}
		if asJSON {
			writeJSON(out, map[string]any{"dry_run": true, "generated_records": records, "warnings": warnings})
		} else {
			fmt.Fprintf(out, "generated=%d warnings=%d\n", records, len(warnings))
		}
		return 0
	}

	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "crawl %s: %s", ex.sourceKind, err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), externalScannerTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, ex.binary, exportArgs...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return fatalf(errw, "crawl %s: %s", ex.sourceKind, err)
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return fatalf(errw, "crawl %s: %s", ex.sourceKind, err)
	}
	sourceURI := ex.binary + "://" + strings.Join(ex.adapterCmd(), "/")
	result, importErr := ingest.ImportAdapterReader(db, stdout, sourceURI, ex.sourceKind)
	waitErr := cmd.Wait()
	if ctx.Err() == context.DeadlineExceeded {
		return fatalf(errw, "crawl %s: timed out after %s", ex.sourceKind, externalScannerTimeout)
	}
	if importErr != nil {
		return fatalf(errw, "crawl %s: %s", ex.sourceKind, importErr)
	}
	if waitErr != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = waitErr.Error()
		}
		return fatalf(errw, "crawl %s: %s", ex.sourceKind, msg)
	}
	if asJSON {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d warnings=%d already_known=%v source=%s\n", result.Inserted, len(result.Warnings), result.AlreadyKnown, result.SourceKind)
	}
	return 0
}

// dryRunExporter runs the export and counts valid adapter records without
// touching the database, so `crawl <source> --dry-run` is a safe preview.
func dryRunExporter(binary string, exportArgs []string) (int, []string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), externalScannerTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, binary, exportArgs...)
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
		if ctx.Err() == context.DeadlineExceeded {
			return records, warnings, fmt.Errorf("%s timed out after %s", binary, externalScannerTimeout)
		}
		return 0, nil, err
	}
	if err := cmd.Wait(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return records, warnings, fmt.Errorf("%s timed out after %s", binary, externalScannerTimeout)
		}
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return 0, nil, fmt.Errorf("%s", msg)
	}
	return records, warnings, nil
}
