package app

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

// A child process bounds the unfixed pipe deadlock without leaking a test goroutine.
func TestImportPipeFailureLifecycle(t *testing.T) {
	mode := os.Getenv("BRIGADE_TEST_IMPORT_PIPE_CHILD")
	if mode == "" {
		for _, mode := range []string{"native-item", "native-event", "memory", "memory-rebuild", "sourceharvest", "crawler", "stationtrail"} {
			t.Run(mode, func(t *testing.T) {
				if runtime.GOOS == "windows" && (mode == "sourceharvest" || mode == "crawler" || mode == "stationtrail") {
					t.Skip("fake exporter uses a POSIX exec wrapper")
				}
				ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
				defer cancel()
				cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestImportPipeFailureLifecycle$")
				cmd.Env = append(os.Environ(), "BRIGADE_TEST_IMPORT_PIPE_CHILD="+mode)
				output, err := cmd.CombinedOutput()
				if ctx.Err() != nil {
					t.Fatalf("import did not stop its producer: %v\n%s", ctx.Err(), output)
				}
				if err != nil {
					t.Fatalf("child failed: %v\n%s", err, output)
				}
			})
		}
		return
	}
	withTempHome(t)
	runOK(t, "init")
	db := openTestDB(t)
	defer db.Close()
	if strings.HasPrefix(mode, "memory") {
		ws := t.TempDir()
		writeTestNamespace(t, ws, testMemoryNamespace)
		cards := filepath.Join(ws, "memory", "cards")
		if err := os.MkdirAll(cards, 0o755); err != nil {
			t.Fatal(err)
		}
		card := filepath.Join(cards, "card.md")
		body := "---\nid: pipe-card-1111-4222-8333-444444444444\ntopic: pipe\n---\n\n# Previous projection\n"
		if err := os.WriteFile(card, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		runJSON(t, "crawl", "memory", ws, "--json")
		before, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`create trigger fail_pipe before insert on items when instr(new.text, 'PIPE_FAILURE_NEW') > 0 begin select raise(abort, 'injected pipe persistence failure'); end`); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(card, []byte(strings.Replace(body, "Previous projection", "PIPE_FAILURE_NEW", 1)), 0o600); err != nil {
			t.Fatal(err)
		}
		args := []string{ws, "--json", "--full"}
		if mode == "memory-rebuild" {
			args = append(args, "--rebuild")
		}
		var out, errw bytes.Buffer
		code := cmdCrawlMemory(args, &out, &errw)
		if code == 0 || !strings.Contains(errw.String(), "injected pipe persistence failure") {
			t.Fatalf("code=%d stderr=%s", code, errw.String())
		}
		after, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
		if err != nil {
			t.Fatal(err)
		}
		if fmt.Sprint(before) != fmt.Sprint(after) {
			t.Fatalf("projection changed: before=%v after=%v", before, after)
		}
		var status string
		if err := db.QueryRow(`select status from source_scan_runs order by rowid desc limit 1`).Scan(&status); err != nil {
			t.Fatal(err)
		}
		if status != "interrupted" {
			t.Fatalf("scan status=%q", status)
		}
		return
	}
	table := "items"
	if mode == "native-event" {
		table = "events"
	}
	if _, err := db.Exec("create trigger fail_pipe before insert on " + table + " begin select raise(abort, 'injected pipe persistence failure'); end"); err != nil {
		t.Fatal(err)
	}
	if mode == "sourceharvest" || mode == "crawler" || mode == "stationtrail" {
		bin := t.TempDir()
		name := "fixture-exporter"
		if mode == "sourceharvest" {
			name = "sourceharvest"
		}
		if mode == "stationtrail" {
			name = "stationtrail"
		}
		wrapper := "#!/bin/sh\nexec " + strconv.Quote(os.Args[0]) + " -test.run=^TestImportPipeExporterHelper$\n"
		if mode == "stationtrail" {
			wrapper = "#!/bin/sh\nif [ \"$1\" = capabilities ]; then printf '%s' '{\"tool\":\"stationtrail\",\"version\":\"0.9.0\",\"schema\":\"miseledger.adapter.v1\",\"sources\":[\"codex\"]}'; exit 0; fi\nexec " + strconv.Quote(os.Args[0]) + " -test.run=^TestImportPipeExporterHelper$\n"
		}
		if err := os.WriteFile(filepath.Join(bin, name), []byte(wrapper), 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
		t.Setenv("BRIGADE_TEST_PIPE_EXPORTER", "1")
		t.Setenv("BRIGADE_TEST_PIPE_EXPORTER_KIND", mode)
		var out, errw bytes.Buffer
		var code int
		if mode == "sourceharvest" {
			code = cmdImportSourceHarvest([]string{"jsonl", "unused", "--json"}, &out, &errw)
		} else if mode == "stationtrail" {
			_, _, err := runStationTrailImport(db, "codex", "fixture", map[string]string{})
			if err != nil {
				code = 1
				errw.WriteString(err.Error())
			}
		} else {
			code = cmdCrawlExporter(nativeExporter{binary: name, sourceKind: "discord"}, nil, &out, &errw)
		}
		if code == 0 || !strings.Contains(errw.String(), "injected pipe persistence failure") {
			t.Fatalf("code=%d stderr=%s", code, errw.String())
		}
		return
	}
	line := pipeFixtureRecord(t)
	producerDone := make(chan struct{})
	generator := func(_ string, opts sources.Options, w io.Writer) (sources.Result, error) {
		defer close(producerDone)
		if _, err := w.Write(line); err != nil {
			return sources.Result{}, err
		}
		if opts.AfterFile != nil {
			return sources.Result{}, opts.AfterFile(sources.FileScan{Path: "fixture.jsonl", Records: 1})
		}
		_, err := w.Write(line)
		return sources.Result{Records: 2}, err
	}
	_, _, err := runNativeImportOpts(db, "discord", generator, "fixture://pipe", sources.Options{}, mode == "native-event", nil)
	if err == nil || !strings.Contains(err.Error(), "injected pipe persistence failure") {
		t.Fatalf("import error=%v", err)
	}
	select {
	case <-producerDone:
	default:
		t.Fatal("producer not joined")
	}
	for _, table := range []string{"items", "events", "imports"} {
		var count int
		if err := db.QueryRow("select count(*) from " + table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 0 {
			t.Fatalf("%s count=%d", table, count)
		}
	}
}

func pipeFixtureRecord(t *testing.T) []byte {
	t.Helper()
	fixture := "testdata/adapters/discrawl.fixture.jsonl"
	if os.Getenv("BRIGADE_TEST_PIPE_EXPORTER_KIND") == "stationtrail" {
		fixture = "testdata/adapters/agent-session.fixture.jsonl"
	}
	data, err := os.ReadFile(repoPath(t, fixture))
	if err != nil {
		t.Fatal(err)
	}
	return append(bytes.SplitN(data, []byte("\n"), 2)[0], '\n')
}

func TestImportPipeExporterHelper(t *testing.T) {
	if os.Getenv("BRIGADE_TEST_PIPE_EXPORTER") != "1" {
		return
	}
	line := pipeFixtureRecord(t)
	for i := 0; i < 100000; i++ {
		if _, err := os.Stdout.Write(line); err != nil {
			return
		}
	}
}
