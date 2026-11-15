package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/ingest"
	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/toolpath"
)

// Files are committed individually before advancing the cursor. A killed run
// replays at most its current file. Content hashes also catch same-stat edits.
const filesCrawlMaxBytes = 8 * 1024 * 1024

func cmdCrawlFiles(args []string, out, errw io.Writer) int {
	asJSON, dryRun, args := splitWrapperFlags(args)
	// Keep the existing exporter dry-run contract without updating checkpoints.
	if dryRun {
		// Budget controls belong to the wrapper, including on a dry run.
		filtered := []string{}
		for i := 0; i < len(args); i++ {
			name := strings.TrimLeft(strings.SplitN(args[i], "=", 2)[0], "-")
			if strings.HasPrefix(args[i], "-") && (name == "budget" || name == "max-files") {
				if !strings.Contains(args[i], "=") && i+1 < len(args) {
					i++
				}
				continue
			}
			filtered = append(filtered, args[i])
		}
		args = filtered
		if asJSON {
			args = append(args, "--json")
		}
		return cmdCrawlSourceHarvest("files", "files", append(args, "--dry-run"), out, errw)
	}
	flags := flag.NewFlagSet("crawl files", flag.ContinueOnError)
	flags.SetOutput(errw)
	flags.Usage = func() {
		fmt.Fprintln(errw, "usage: brigade-evidence crawl files <path> [options]")
		flags.PrintDefaults()
	}
	source := flags.String("source", "files", "source kind")
	collection := flags.String("collection", "", "collection ID")
	kind := flags.String("collection-kind", "files", "collection kind")
	glob := flags.String("glob", "*.txt,*.md,*.markdown", "comma-separated filename globs")
	budget := flags.Duration("budget", 600*time.Second, "time budget, returns success with a resume cursor")
	maxFiles := flags.Int("max-files", 1000, "maximum exporter invocations per run")
	output := flags.String("out", "-", "export stream destination (must be stdout)")
	limit := flags.Int("limit", 0, "maximum exporter invocations per run (alias)")
	// Exporter flags historically accept a leading path.
	path := ""
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		path, args = args[0], args[1:]
	}
	if err := flags.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return 0
		}
		return 1
	}
	if path == "" && flags.NArg() == 1 {
		path = flags.Arg(0)
	} else if flags.NArg() != 0 {
		return fatalf(errw, "crawl files: expected one path")
	}
	if *output != "-" {
		return fatalf(errw, "crawl files: --out must be - for archive ingestion")
	}
	if path == "" || *budget <= 0 || *budget > 600*time.Second || *maxFiles <= 0 {
		return fatalf(errw, "crawl files: path, positive max-files and budget up to 10m are required")
	}
	if *limit > 0 {
		*maxFiles = *limit
	}
	inputPath := path
	root, err := filepath.Abs(path)
	if err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	info, err := os.Lstat(root)
	if err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return fatalf(errw, "crawl files: symlink root refused")
	}
	if *collection == "" {
		*collection = defaultCollection("files", path)
	}
	for _, pattern := range strings.Split(*glob, ",") {
		if _, err := filepath.Match(strings.TrimSpace(pattern), ""); err != nil {
			return fatalf(errw, "crawl files: %s", err)
		}
	}
	if err := toolpath.Require("sourceharvest", toolpath.HintSourceHarvest); err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	defer db.Close()
	// Separate from source_scans: option changes and collections must not share
	// cache identities. Existing archive migrations need no rewrite.
	_, err = db.Exec(`create table if not exists files_crawl_cache (profile text not null, path text not null, hash text not null, primary key(profile,path));
 create table if not exists files_crawl_cursor (profile text primary key, path text not null)`)
	if err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	identity := sha256.Sum256([]byte(strings.Join([]string{root, filepath.Clean(inputPath), *source, *collection, *kind, *glob}, "\x00")))
	profile := hex.EncodeToString(identity[:])
	cursor := ""
	err = db.QueryRow(`select path from files_crawl_cursor where profile=?`, profile).Scan(&cursor)
	if err != nil && err != sql.ErrNoRows {
		return fatalf(errw, "crawl files: %s", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), *budget)
	defer cancel()
	exported, parsed, skipped, inserted := 0, 0, 0, 0
	pending := false
	entries := 0
	warnings := []string{}
	checkpoint := func(path string) error {
		if _, err := db.Exec(`insert into files_crawl_cursor values(?,?) on conflict(profile) do update set path=excluded.path`, profile, path); err != nil {
			return err
		}
		cursor = path
		return nil
	}
	warnAndCheckpoint := func(path string, err error) error {
		warnings = append(warnings, fmt.Sprintf("%s: %s", path, err))
		if _, err := db.Exec(`delete from files_crawl_cache where profile=? and path=?`, profile, path); err != nil {
			return err
		}
		return checkpoint(path)
	}
	// WalkDir orders siblings, not full paths. NUL-separated components preserve
	// that order, including a directory named a next to a file named a.txt.
	order := func(path string) string { return strings.ReplaceAll(path, string(filepath.Separator), "\x00") }
	startCursor := cursor
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, walkErr error) error {
		if ctx.Err() != nil || entries >= 10000 {
			pending = true
			return fs.SkipAll
		}
		if walkErr != nil {
			if err := warnAndCheckpoint(path, walkErr); err != nil {
				return err
			}
			if d != nil && d.IsDir() {
				return fs.SkipDir
			}
			return nil
		}
		if path != root && startCursor != "" && order(path) <= order(startCursor) {
			if d.IsDir() && path != startCursor && !strings.HasPrefix(startCursor, path+string(filepath.Separator)) {
				return fs.SkipDir
			}
			if !d.IsDir() {
				return nil
			}
		}
		entries++
		name := strings.ToLower(d.Name())
		if d.IsDir() {
			if order(path) > order(cursor) {
				cursor = path
			}
			if path != root && (strings.HasPrefix(name, ".") || name == "node_modules" || name == "backup" || name == "backups" || name == "deleted") {
				return fs.SkipDir
			}
			return nil
		}
		cursorBeforeFile := cursor
		cursor = path
		if !d.Type().IsRegular() || strings.HasPrefix(name, ".") || strings.Contains(name, ".bak") || strings.Contains(name, "backup") {
			return nil
		}
		match := false
		for _, pattern := range strings.Split(*glob, ",") {
			ok, _ := filepath.Match(strings.TrimSpace(pattern), d.Name())
			match = match || ok
		}
		if !match {
			return nil
		}
		cursor = cursorBeforeFile
		stat, err := d.Info()
		if err != nil {
			return warnAndCheckpoint(path, err)
		}
		if stat.Size() > filesCrawlMaxBytes {
			warnings = append(warnings, fmt.Sprintf("%s: exceeds file size cap", path))
			return checkpoint(path)
		}
		f, err := os.Open(path)
		if err != nil {
			return warnAndCheckpoint(path, err)
		}
		data, err := io.ReadAll(io.LimitReader(f, filesCrawlMaxBytes+1))
		_ = f.Close()
		if err != nil {
			return warnAndCheckpoint(path, err)
		}
		if len(data) > filesCrawlMaxBytes {
			warnings = append(warnings, fmt.Sprintf("%s: exceeds file size cap", path))
			return checkpoint(path)
		}
		hash := sha256.Sum256(data)
		digest := hex.EncodeToString(hash[:])
		known := ""
		err = db.QueryRow(`select hash from files_crawl_cache where profile=? and path=?`, profile, path).Scan(&known)
		if err != nil && err != sql.ErrNoRows {
			return err
		}
		if known == digest {
			skipped++
			return checkpoint(path)
		}
		if ctx.Err() != nil || exported >= *maxFiles {
			pending = true
			return fs.SkipAll
		}
		exportPath := path
		if !filepath.IsAbs(inputPath) {
			cwd, err := os.Getwd()
			if err != nil {
				return err
			}
			exportPath, err = filepath.Rel(cwd, path)
			if err != nil {
				return err
			}
			if !info.IsDir() {
				exportPath = inputPath
			}
		}
		cmd := exec.CommandContext(ctx, "sourceharvest", "files", exportPath, "--source", *source, "--collection", *collection, "--collection-kind", *kind, "--glob", *glob, "--out", "-", "--json")
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return err
		}
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		if err := cmd.Start(); err != nil {
			if ctx.Err() != nil {
				pending = true
				return fs.SkipAll
			}
			return warnAndCheckpoint(path, err)
		}
		exported++
		result, importErr := ingest.ImportAdapterReader(db, stdout, "sourceharvest://files "+path, "")
		if importErr != nil {
			_ = stdout.Close()
			cancel()
		}
		waitErr := cmd.Wait()
		inserted += result.Inserted
		if importErr != nil {
			return importErr
		}
		if ctx.Err() == context.DeadlineExceeded {
			pending = true
			return fs.SkipAll
		}
		if waitErr != nil {
			return warnAndCheckpoint(path, fmt.Errorf("sourceharvest: %s: %w", strings.TrimSpace(stderr.String()), waitErr))
		}
		summary := parseSourceHarvestSummary(stderr.Bytes())
		warnings = append(warnings, summary.Warnings...)
		warnings = append(warnings, result.Warnings...)
		// Bad records and exporter warnings advance traversal but stay uncached.
		if len(result.Warnings) > 0 || len(summary.Warnings) > 0 {
			return warnAndCheckpoint(path, fmt.Errorf("export incomplete, left uncached"))
		}
		parsed++
		// Write the cache before the checkpoint. A crash between them leaves
		// the cursor behind a committed cache hit, avoiding another export.
		if _, err := db.Exec(`insert into files_crawl_cache values(?,?,?) on conflict(profile,path) do update set hash=excluded.hash`, profile, path, digest); err != nil {
			return err
		}
		return checkpoint(path)
	})
	if err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	if !pending {
		cursor = ""
	}
	if err := checkpoint(cursor); err != nil {
		return fatalf(errw, "crawl files: %s", err)
	}
	result := map[string]any{"status": "complete", "inserted_items": inserted, "files_parsed": parsed, "files_skipped": skipped, "warnings": warnings, "complete": !pending, "resume_cursor": cursor}
	if asJSON {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d files_parsed=%d files_skipped=%d complete=%v resume_cursor=%q warnings=%d\n", inserted, parsed, skipped, !pending, cursor, len(warnings))
	}
	return 0
}
