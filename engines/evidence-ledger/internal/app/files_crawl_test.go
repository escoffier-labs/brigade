package app

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/adapter"
)

func filesExporter(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("fake exporter uses POSIX shell")
	}
	dir := t.TempDir()
	body := `#!/bin/sh
path="$2"
if [ -n "$FILES_CRAWL_SLOW" ] && [ "${path##*/}" = "001.txt" ]; then exec sleep 5; fi
printf '{"schema":"@SCHEMA@","source":{"kind":"files","name":"files"},"collection":{"external_id":"fixture","kind":"files","name":"fixture"},"item":{"external_id":"%s","kind":"file","created_at":"2026-01-01T00:00:00Z","text":"fixture"},"raw":{"format":"text","path":"%s"}}\n' "$path" "$path"
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
	got := runJSON(t, "crawl", "files", root, "--json")
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
