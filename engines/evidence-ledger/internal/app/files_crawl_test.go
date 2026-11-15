package app

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/adapter"
)

func filesExporter(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("fake exporter uses POSIX shell")
	}
	dir := t.TempDir()
	body := `#!/bin/sh
for arg in "$@"; do
case "$arg" in --budget*|--max-files*) exit 2;; esac
done
path="$2"
if [ -n "$FILES_CRAWL_REWRITE" ]; then printf "later" > "$path"; fi
if [ -n "$FILES_CRAWL_ATTEMPTS" ]; then printf "attempt\n" >> "$FILES_CRAWL_ATTEMPTS"; fi
if [ -n "$FILES_CRAWL_SLOW" ] && [ "${path##*/}" = "001.txt" ]; then exec sleep 5; fi
printf '{"schema":"@SCHEMA@","source":{"kind":"files","name":"files"},"collection":{"external_id":"fixture","kind":"files","name":"fixture"},"item":{"external_id":"%s","kind":"file","created_at":"2026-01-01T00:00:00Z","text":"fixture"},"raw":{"format":"text","path":"%s"}}\n' "$path" "$path"
if [ -n "$FILES_CRAWL_EMIT_SLOW" ]; then exec sleep 5; fi
if [ -n "$FILES_CRAWL_WARN" ]; then printf '{"warnings":["fixture warning"]}\n' >&2; fi
if [ -n "$FILES_CRAWL_BAD" ]; then printf 'invalid record\n'; fi
if [ -z "$FILES_CRAWL_WARN" ]; then printf '{"path":"%s","records":1}\n' "$path" >&2; fi
`
	body = strings.Replace(body, "@SCHEMA@", adapter.SchemaV1, 1)
	if err := os.WriteFile(filepath.Join(dir, "sourceharvest"), []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func TestFilesCrawlResumesAndSkipsUnchanged(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	for i := 0; i < 31; i++ {
		if err := os.WriteFile(filepath.Join(root, fmt.Sprintf("%03d.txt", i)), []byte("first"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	// A directory and similarly named sibling expose full-path sorting mistakes.
	if err := os.Mkdir(filepath.Join(root, "a"), 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a/z.txt", "a.txt"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("first"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	total := 0
	for runs := 0; runs < 10; runs++ {
		got := runJSON(t, "crawl", "files", root, "--max-files", "7", "--json")
		total += int(got["files_parsed"].(float64))
		if got["complete"] == true {
			break
		}
		if got["resume_cursor"] == "" || got["status"] != "complete" {
			t.Fatalf("missing successful checkpoint: %v", got)
		}
	}
	if total != 33 {
		t.Fatalf("parsed %d, want 33", total)
	}
	got := runJSON(t, "crawl", "files", root, "--max-files", "1", "--out", "-", "--json")
	if got["files_parsed"] != float64(0) || got["files_skipped"] != float64(33) || got["complete"] != true {
		t.Fatalf("unchanged: %v", got)
	}
	path := filepath.Join(root, "000.txt")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("other"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, info.ModTime(), info.ModTime()); err != nil {
		t.Fatal(err)
	}
	got = runJSON(t, "crawl", "files", root, "--json")
	if got["files_parsed"] != float64(1) {
		t.Fatalf("same-stat edit missed: %v", got)
	}
	got = runJSON(t, "crawl", "files", root, "--collection", "other", "--json")
	if got["files_parsed"] != float64(33) {
		t.Fatalf("collection reused cache: %v", got)
	}
}

func TestFilesCrawlTimeoutRetainsProgress(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	for i := 0; i < 3; i++ {
		if err := os.WriteFile(filepath.Join(root, fmt.Sprintf("%03d.txt", i)), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("FILES_CRAWL_SLOW", "1")
	got := runJSON(t, "crawl", "files", root, "--budget", "300ms", "--json")
	if got["complete"] != false || !strings.HasSuffix(got["resume_cursor"].(string), "000.txt") || got["inserted_items"] != float64(1) {
		t.Fatalf("timeout lost checkpoint: %v", got)
	}
	t.Setenv("FILES_CRAWL_SLOW", "")
	got = runJSON(t, "crawl", "files", root, "--json")
	if got["complete"] != true || got["files_parsed"] != float64(2) {
		t.Fatalf("resume: %v", got)
	}
	got = runJSON(t, "crawl", "files", root, "--json")
	if got["files_skipped"] != float64(3) {
		t.Fatalf("replayed committed file: %v", got)
	}
}

func TestFilesCrawlSafety(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	for _, dir := range []string{".private", "backups", "node_modules"} {
		if err := os.Mkdir(filepath.Join(root, dir), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, dir, "hidden.txt"), []byte("private"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"good.txt", "secret.bak.txt", "backup.txt", ".secret.txt"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(root, "good.txt"), filepath.Join(root, "link.txt")); err != nil {
		t.Fatal(err)
	}
	f, err := os.Create(filepath.Join(root, "huge.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if err := f.Truncate(filesCrawlMaxBytes + 1); err != nil {
		t.Fatal(err)
	}
	f.Close()
	got := runJSON(t, "crawl", "files", root, "--json")
	if got["files_parsed"] != float64(1) || len(got["warnings"].([]any)) != 1 {
		t.Fatalf("safety filters: %v", got)
	}
}

func TestFilesCrawlCommittedCounts(t *testing.T) {
	for _, mode := range []string{"EMIT_SLOW", "WARN", "BAD"} {
		t.Run(mode, func(t *testing.T) {
			withTempHome(t)
			filesExporter(t)
			root := t.TempDir()
			if err := os.WriteFile(filepath.Join(root, "one.txt"), []byte("fixture"), 0600); err != nil {
				t.Fatal(err)
			}
			t.Setenv("FILES_CRAWL_"+mode, "1")
			got := runJSON(t, "crawl", "files", root, "--budget", "500ms", "--json")
			if got["inserted_items"] != float64(1) {
				t.Fatalf("durable insert lost: %v", got)
			}
			db, _, err := openMigrated()
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			var items, cached int
			if err := db.QueryRow("select count(*) from items").Scan(&items); err != nil {
				t.Fatal(err)
			}
			if err := db.QueryRow("select count(*) from files_crawl_cache").Scan(&cached); err != nil {
				t.Fatal(err)
			}
			if items != 1 || cached != 0 {
				t.Fatalf("items=%d cached=%d", items, cached)
			}
			if mode == "EMIT_SLOW" && got["complete"] != false {
				t.Fatalf("expected bounded result: %v", got)
			}
		})
	}
}

func TestFilesCrawlUnreadablePaths(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	badFile := filepath.Join(root, "a.txt")
	badDir := filepath.Join(root, "b")
	if err := os.WriteFile(badFile, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(badDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(badDir, "hidden.txt"), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "z.txt"), []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	denied := true
	walk := func(root string, visit fs.WalkDirFunc) error {
		return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if denied && path == badDir {
				if err := visit(path, d, nil); err != nil {
					return err
				}
				return visit(path, d, fs.ErrPermission)
			}
			return visit(path, d, err)
		})
	}
	open := func(path string) (*os.File, error) {
		if denied && path == badFile {
			return nil, fs.ErrPermission
		}
		return os.Open(path)
	}
	for i := 0; i < 2; i++ {
		var out, errw bytes.Buffer
		if code := crawlFiles([]string{root, "--max-files", "1", "--json"}, &out, &errw, walk, open); code != 0 {
			t.Fatalf("crawl failed: %s", &errw)
		}
		var got map[string]any
		if err := json.Unmarshal(out.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		if got["complete"] != true || len(got["warnings"].([]any)) != 2 {
			t.Fatalf("unreadable paths blocked traversal: %v", got)
		}
	}
	denied = false
	got := runJSON(t, "crawl", "files", root, "--json")
	if got["files_parsed"] != float64(2) {
		t.Fatalf("unreadable paths were cached: %v", got)
	}
}

func TestFilesCrawlDryRunBudgetFlags(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	t.Chdir(root)
	got := runJSON(t, "crawl", "files", "budget", "--budget=1s", "--max-files", "2", "--dry-run", "--json")
	if got["dry_run"] != true {
		t.Fatalf("dry run: %v", got)
	}
}

func TestFilesCrawlSourceScans(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	path := filepath.Join(t.TempDir(), "one.txt")
	if err := os.WriteFile(path, []byte("first"), 0600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "files", path, "--json")
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	assertScan := func(changed bool) {
		t.Helper()
		diff, err := scanDiff(db, path)
		if err != nil {
			t.Fatal(err)
		}
		if diff["known"] != true || diff["changed"] != changed || diff["records_generated"] != 1 {
			t.Fatalf("scan: %v", diff)
		}
		scans, err := changedScans(db, "files")
		if err != nil {
			t.Fatal(err)
		}
		want := 0
		if changed {
			want = 1
		}
		if len(scans) != want {
			t.Fatalf("changed scans: %v", scans)
		}
	}
	assertScan(false)
	if err := os.WriteFile(path, []byte("later"), 0600); err != nil {
		t.Fatal(err)
	}
	assertScan(true)
	runJSON(t, "crawl", "files", path, "--json")
	assertScan(false)
}

func TestFilesCrawlExporterStartFailure(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	binary, err := exec.LookPath("sourceharvest")
	if err != nil {
		t.Fatal(err)
	}
	// LookPath accepts this executable, but Start cannot load its interpreter.
	if err := os.WriteFile(binary, []byte("#!/missing-fixture-interpreter\n"), 0700); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	for _, name := range []string{"a.txt", "b.txt"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	var out, errw bytes.Buffer
	if code := cmdCrawlFiles([]string{root, "--json"}, &out, &errw); code != 1 {
		t.Fatalf("start failure returned %d: %s %s", code, &out, &errw)
	}
	if !strings.Contains(errw.String(), "crawl files:") {
		t.Fatalf("missing diagnostic: %s", &errw)
	}
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.QueryRow("select count(*) from files_crawl_cursor").Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("start failure checkpointed %d cursors", count)
	}
}

func TestFilesCrawlAncestorErrorKeepsCursor(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	root := t.TempDir()
	dir := filepath.Join(root, "a")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"a/one.txt", "a/two.txt", "z.txt"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte("fixture"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	first := runJSON(t, "crawl", "files", root, "--max-files", "1", "--json")
	walk := func(root string, visit fs.WalkDirFunc) error {
		return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if path == dir {
				if err := visit(path, d, nil); err != nil {
					return err
				}
				return visit(path, d, fs.ErrPermission)
			}
			if path == filepath.Join(root, "z.txt") {
				return fmt.Errorf("fixture stop after ancestor")
			}
			return visit(path, d, err)
		})
	}
	var out, errw bytes.Buffer
	if code := crawlFiles([]string{root, "--json"}, &out, &errw, walk, os.Open); code != 1 {
		t.Fatalf("expected fixture stop: %s", &errw)
	}
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var cursor string
	if err := db.QueryRow("select path from files_crawl_cursor").Scan(&cursor); err != nil {
		t.Fatal(err)
	}
	if cursor != first["resume_cursor"] {
		t.Fatalf("cursor regressed: %q, was %v", cursor, first["resume_cursor"])
	}
}

func TestFilesCrawlBusyRetry(t *testing.T) {
	for _, persistent := range []bool{false, true} {
		t.Run(fmt.Sprint(persistent), func(t *testing.T) {
			withTempHome(t)
			filesExporter(t)
			root := t.TempDir()
			for _, name := range []string{"a.txt", "b.txt"} {
				if err := os.WriteFile(filepath.Join(root, name), []byte("fixture"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			first := runJSON(t, "crawl", "files", root, "--max-files", "1", "--json")
			holder, _, err := openMigrated()
			if err != nil {
				t.Fatal(err)
			}
			defer holder.Close()
			holder.SetMaxOpenConns(1)
			marker := filepath.Join(t.TempDir(), "attempts")
			t.Setenv("FILES_CRAWL_ATTEMPTS", marker)
			locked := false
			released := make(chan error, 1)
			open := func(path string) (*os.File, error) {
				if path == filepath.Join(root, "b.txt") && !locked {
					if _, err := holder.Exec("BEGIN IMMEDIATE"); err != nil {
						return nil, err
					}
					locked = true
					if !persistent {
						go func() {
							// Release only after a fresh exporter proves the first import retried.
							deadline := time.Now().Add(2 * time.Second)
							for time.Now().Before(deadline) {
								data, _ := os.ReadFile(marker)
								if bytes.Count(data, []byte("attempt\n")) >= 2 {
									break
								}
								time.Sleep(time.Millisecond)
							}
							_, err := holder.Exec("ROLLBACK")
							released <- err
						}()
					}
				}
				return os.Open(path)
			}
			budget := "3s"
			if persistent {
				budget = "250ms"
			}
			var out, errw bytes.Buffer
			started := time.Now()
			code := crawlFiles([]string{root, "--budget", budget, "--json"}, &out, &errw, filepath.WalkDir, open)
			elapsed := time.Since(started)
			if persistent {
				if _, err := holder.Exec("ROLLBACK"); err != nil {
					t.Fatal(err)
				}
			} else if err := <-released; err != nil {
				t.Fatal(err)
			}
			if code != 0 {
				t.Fatalf("busy crawl code=%d: %s", code, &errw)
			}
			var got map[string]any
			if err := json.Unmarshal(out.Bytes(), &got); err != nil {
				t.Fatal(err)
			}
			attempts, err := os.ReadFile(marker)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Count(attempts, []byte("attempt\n")) < 2 {
				t.Fatalf("exporter was not restarted: %s", attempts)
			}
			wantItems, wantParsed := 2, float64(1)
			if persistent {
				wantItems, wantParsed = 1, 0
				if elapsed > time.Second {
					t.Fatalf("busy wait exceeded budget: %s", elapsed)
				}
				if got["complete"] != false || got["resume_cursor"] != first["resume_cursor"] {
					t.Fatalf("busy stop skipped file: %v", got)
				}
				var cursor string
				if err := holder.QueryRow("select path from files_crawl_cursor").Scan(&cursor); err != nil {
					t.Fatal(err)
				}
				if cursor != first["resume_cursor"] {
					t.Fatalf("persisted cursor advanced: %s", cursor)
				}
			} else if got["complete"] != true {
				t.Fatalf("retry did not finish: %v", got)
			}
			if got["files_parsed"] != wantParsed || got["inserted_items"] != wantParsed {
				t.Fatalf("retry counts: %v", got)
			}
			var items, cached int
			if err := holder.QueryRow("select count(*) from items").Scan(&items); err != nil {
				t.Fatal(err)
			}
			if err := holder.QueryRow("select count(*) from files_crawl_cache").Scan(&cached); err != nil {
				t.Fatal(err)
			}
			if items != wantItems || cached != wantItems {
				t.Fatalf("items=%d cache=%d want=%d", items, cached, wantItems)
			}
			if persistent {
				resumed := runJSON(t, "crawl", "files", root, "--json")
				if resumed["files_parsed"] != float64(1) || resumed["inserted_items"] != float64(1) || resumed["complete"] != true {
					t.Fatalf("resume: %v", resumed)
				}
			}
		})
	}
}

func TestFilesCrawlScanUsesCrawlDigest(t *testing.T) {
	withTempHome(t)
	filesExporter(t)
	path := filepath.Join(t.TempDir(), "one.txt")
	if err := os.WriteFile(path, []byte("first"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FILES_CRAWL_REWRITE", "1")
	runJSON(t, "crawl", "files", path, "--json")
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	diff, err := scanDiff(db, path)
	if err != nil {
		t.Fatal(err)
	}
	if diff["changed"] != true {
		t.Fatalf("scan used post-export content: %v", diff)
	}
}
