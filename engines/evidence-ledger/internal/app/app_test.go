package app

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"
)

func TestInitCreatesPrivateDirsAndDoctorJSON(t *testing.T) {
	withTempHome(t)
	var out, errb bytes.Buffer
	if code := Run([]string{"init"}, &out, &errb); code != 0 {
		t.Fatalf("init failed: code=%d err=%s", code, errb.String())
	}
	paths := ResolvePaths()
	assertPrivate(t, filepath.Dir(paths.ConfigPath))
	assertPrivate(t, paths.DataDir)
	assertPrivate(t, paths.CacheDir)

	out.Reset()
	errb.Reset()
	if code := Run([]string{"doctor", "--json"}, &out, &errb); code != 0 {
		t.Fatalf("doctor failed: code=%d err=%s out=%s", code, errb.String(), out.String())
	}
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("doctor json invalid: %v", err)
	}
	if got["ok"] != true {
		t.Fatalf("doctor not ok: %v", got)
	}
}

func TestDoctorMCPJSON(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	got := runJSON(t, "doctor", "--mcp", "--json")
	if got["ok"] != true {
		t.Fatalf("doctor --mcp not ok: %v", got)
	}
	checks := got["checks"].([]any)
	seen := map[string]bool{}
	for _, raw := range checks {
		check := raw.(map[string]any)
		seen[check["name"].(string)] = check["ok"] == true
	}
	for _, name := range []string{"mcp_initialize", "mcp_tools"} {
		if !seen[name] {
			t.Fatalf("missing passing %s check in %v", name, checks)
		}
	}
}

func TestDoctorAndEvidenceHelpDoNotCreateArchiveOrCache(t *testing.T) {
	withTempHome(t)
	paths := ResolvePaths()

	for _, command := range []string{"doctor", "evidence"} {
		code, stdout, stderr := run(command, "--help")
		if code != 0 {
			t.Fatalf("%s --help failed: code=%d stderr=%s", command, code, stderr)
		}
		for _, flag := range map[string][]string{
			"doctor":   {"--json", "--mcp", "--archive"},
			"evidence": {"--markdown", "--limit"},
		}[command] {
			if !strings.Contains(stdout, flag) {
				t.Fatalf("%s --help missing %s: %q", command, flag, stdout)
			}
		}
	}

	for _, path := range []string{paths.ConfigPath, paths.DataDir, paths.CacheDir, paths.DBPath} {
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("help created runtime state at %s: %v", path, err)
		}
	}
}

func TestAdapterImportSearchShowExportAndIdempotency(t *testing.T) {
	withTempHome(t)
	fixture := repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")
	agentFixture := repoPath(t, "testdata/adapters/agent-session.fixture.jsonl")
	runOK(t, "init")
	runOK(t, "import", "adapter", fixture, "--source", "discrawl")
	runOK(t, "import", "adapter", agentFixture, "--source", "codex")

	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 4 {
		t.Fatalf("items after import = %v, want 4", status["items"])
	}

	searchOut := runJSON(t, "search", "adapter contract", "--json")
	results := searchOut["results"].([]any)
	if len(results) == 0 {
		t.Fatalf("search returned no results: %v", searchOut)
	}
	first := results[0].(map[string]any)
	id := first["id"].(string)
	show := runJSON(t, "show", id, "--json")
	if show["id"] != id {
		t.Fatalf("show id = %v, want %s", show["id"], id)
	}
	raw := show["raw"].(map[string]any)
	if _, ok := raw["extra_unknown_field"]; !ok && raw["item"].(map[string]any)["external_id"] == "discord:message:2" {
		t.Fatalf("unknown field was not preserved in raw json")
	}

	runOK(t, "search", "AND", "--json")
	runOK(t, "search", "OR", "--json")
	runOK(t, "search", "NOT", "--json")
	runOK(t, "search", "NEAR", "--json")
	runOK(t, "search", "*", "--json")

	exportDir := filepath.Join(t.TempDir(), "export")
	exportOut := runJSON(t, "export", "markdown", "--out", exportDir)
	if exportOut["files"].(float64) == 0 {
		t.Fatalf("export wrote no files: %v", exportOut)
	}
	assertPrivate(t, exportDir)

	sqlOut := runJSON(t, "sql", "select count(*) as items from items", "--json")
	rows := sqlOut["rows"].([]any)
	if rows[0].(map[string]any)["items"].(float64) != 4 {
		t.Fatalf("sql count mismatch: %v", sqlOut)
	}
	if code, _, _ := run("sql", "delete from items", "--json"); code == 0 {
		t.Fatalf("mutation SQL succeeded")
	}

	runOK(t, "import", "adapter", fixture, "--source", "discrawl")
	runOK(t, "import", "adapter", agentFixture, "--source", "codex")
	status = runJSON(t, "status", "--json")
	if status["items"].(float64) != 4 {
		t.Fatalf("items after reimport = %v, want 4", status["items"])
	}
}

func TestImportWarningsForInvalidRecords(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	bad := filepath.Join(t.TempDir(), "bad.jsonl")
	if err := os.WriteFile(bad, []byte(`{"schema":"miseledger.adapter.v1","source":{"kind":"discrawl"},"item":{"external_id":"x","kind":"message"}}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out := runJSON(t, "import", "adapter", bad, "--source", "discrawl", "--json")
	warnings := out["warnings"].([]any)
	if len(warnings) != 1 || !strings.Contains(warnings[0].(string), "collection.external_id") {
		t.Fatalf("unexpected warnings: %v", out)
	}
}

func TestImportAdapterFromStdin(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	jsonl, err := os.ReadFile(repoPath(t, "testdata/adapters/discrawl.fixture.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	oldStdin := stdin
	stdin = bytes.NewReader(jsonl)
	t.Cleanup(func() { stdin = oldStdin })
	out := runJSON(t, "import", "adapter", "-", "--source", "discrawl", "--json")
	if out["inserted_items"].(float64) != 2 {
		t.Fatalf("inserted = %v, want 2: %v", out["inserted_items"], out)
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 2 {
		t.Fatalf("items after stdin import = %v, want 2", status["items"])
	}
}

func TestAdapterExportFilesArePrivateAndAtomic(t *testing.T) {
	withTempHome(t)
	fixture := repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl")
	dir := t.TempDir()
	outPath := filepath.Join(dir, "codex.adapter.jsonl")

	runOK(t, "adapter", "codex", fixture, "--out", outPath, "--json")
	assertPrivate(t, outPath)

	if err := os.WriteFile(outPath, []byte("original\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr := run("adapter", "codex", filepath.Join(dir, "missing"), "--out", outPath)
	if code == 0 {
		t.Fatalf("expected failure, stdout=%s stderr=%s", stdout, stderr)
	}
	b, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "original\n" {
		t.Fatalf("output was replaced on failure: %q", string(b))
	}
	matches, err := filepath.Glob(filepath.Join(dir, ".codex.adapter.jsonl.tmp-*"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) != 0 {
		t.Fatalf("temp files left behind: %v", matches)
	}
}

func TestImportStationTrailWrapper(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	stationtrailDir := t.TempDir()
	fixture := repoPath(t, "testdata/adapters/agent-session.fixture.jsonl")
	script := filepath.Join(stationtrailDir, "stationtrail")
	body := "#!/bin/sh\nsummary=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--summary-out' ]; then shift; summary=\"$1\"; fi\n  shift || true\ndone\nif [ -n \"$summary\" ]; then\n  printf '{\"source\":\"codex\",\"records\":2,\"warnings\":[],\"files\":[{\"path\":\"fixture.jsonl\",\"size\":1,\"mtime\":\"2026-06-03T00:00:00Z\",\"content_hash\":\"sha256:test\",\"records_generated\":2,\"warnings\":0}]}' > \"$summary\"\nfi\ncat " + shellQuote(fixture) + "\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", stationtrailDir+string(os.PathListSeparator)+oldPath)
	out := runJSON(t, "import", "stationtrail", "codex", "fixture", "--json")
	if out["inserted_items"].(float64) != 2 {
		t.Fatalf("inserted = %v, want 2: %v", out["inserted_items"], out)
	}
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("expected scan manifest from stationtrail summary: %v", scans)
	}
}

func TestImportStationTrailWrapperForSupportedSources(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	stationtrailDir := t.TempDir()
	script := filepath.Join(stationtrailDir, "stationtrail")
	body := `#!/bin/sh
source="$1"
summary=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '--summary-out' ]; then shift; summary="$1"; fi
  shift || true
done
case "$source" in
  codex) text='Codex wrapper fixture adapter contract'; actor='assistant' ;;
  claude) text='Claude wrapper fixture native import'; actor='assistant' ;;
  openclaw) text='OpenClaw wrapper fixture normalized schema'; actor='assistant' ;;
  opencode) text='OpenCode wrapper fixture sanitized export'; actor='assistant' ;;
  hermes) text='Hermes wrapper fixture session snapshot'; actor='assistant' ;;
  *) echo "unsupported source" >&2; exit 1 ;;
esac
if [ -n "$summary" ]; then
  printf '{"source":"%s","records":1,"warnings":[],"files":[{"path":"%s.fixture","size":1,"mtime":"2026-06-03T00:00:00Z","content_hash":"sha256:test","records_generated":1,"warnings":0}]}' "$source" "$source" > "$summary"
fi
printf '{"schema":"miseledger.adapter.v1","source":{"kind":"%s","name":"StationTrail Fixture"},"collection":{"external_id":"%s:session:fixture","kind":"agent_session","name":"fixture"},"item":{"external_id":"%s:item:fixture","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"%s","tags":["agent-session","%s"]},"actor":{"external_id":"%s:%s:fixture","type":"%s","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:test","path":"%s.fixture","ordinal":1}}\n' "$source" "$source" "$source" "$text" "$source" "$source" "$actor" "$actor" "$source"
`
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", stationtrailDir+string(os.PathListSeparator)+oldPath)
	for _, source := range []string{"codex", "claude", "openclaw", "opencode", "hermes"} {
		out := runJSON(t, "import", "stationtrail", source, "fixture", "--json")
		if out["inserted_items"].(float64) != 1 {
			t.Fatalf("%s inserted = %v, want 1: %v", source, out["inserted_items"], out)
		}
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 5 || status["sources"].(float64) != 5 {
		t.Fatalf("status after wrapper imports = %v", status)
	}
	search := runJSON(t, "search", "wrapper fixture", "--json")
	if len(search["results"].([]any)) != 5 {
		t.Fatalf("search results = %v", search)
	}
	scans := runJSON(t, "scans", "list", "--json")
	if len(scans["scans"].([]any)) != 5 {
		t.Fatalf("scan rows = %v", scans)
	}
}

func TestImportSourceHarvestWrapper(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	sourceharvestDir := t.TempDir()
	script := filepath.Join(sourceharvestDir, "sourceharvest")
	body := `#!/bin/sh
mode="$1"
path="$2"
text="SourceHarvest $mode wrapper fixture evidence"
printf '{"schema":"miseledger.adapter.v1","source":{"kind":"notes","name":"SourceHarvest Fixture"},"collection":{"external_id":"notes:%s","kind":"notes","name":"notes"},"item":{"external_id":"notes:item:%s","kind":"note","created_at":"2026-06-03T00:00:00Z","text":"%s","tags":["notes","%s"]},"actor":{"external_id":"notes:system:%s","type":"system","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:test","path":"notes-%s.fixture","ordinal":1}}\n' "$mode" "$mode" "$text" "$mode" "$mode" "$mode"
printf '{"source":"notes","path":"%s","records":1,"files":1,"warnings":[],"generated_at":"2026-06-03T00:00:00Z"}\n' "$path" >&2
`
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", sourceharvestDir+string(os.PathListSeparator)+oldPath)
	fixturePath := filepath.Join(t.TempDir(), "sourceharvest.txt")
	if err := os.WriteFile(fixturePath, []byte("sourceharvest fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	dry := runJSON(t, "import", "sourceharvest", "markdown", fixturePath, "--source", "notes", "--collection", "notes:local", "--dry-run", "--json")
	if dry["generated_records"].(float64) != 1 {
		t.Fatalf("dry-run generated = %v, want 1: %v", dry["generated_records"], dry)
	}
	for _, mode := range []string{"markdown", "html", "gitlog", "json"} {
		out := runJSON(t, "import", "sourceharvest", mode, fixturePath, "--source", "notes", "--collection", "notes:"+mode, "--json")
		if out["inserted_items"].(float64) != 1 {
			t.Fatalf("%s inserted = %v, want 1: %v", mode, out["inserted_items"], out)
		}
	}
	search := runJSON(t, "search", "wrapper fixture", "--source", "notes", "--json")
	if len(search["results"].([]any)) != 4 {
		t.Fatalf("sourceharvest wrapper search failed: %v", search)
	}
	scans := runJSON(t, "scans", "list", "--source", "notes", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("expected sourceharvest scan manifest: %v", scans)
	}
}

func TestCrawlSessionsImportsDiscoveredNativeRoots(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	codexRoot := filepath.Join(os.Getenv("HOME"), ".codex", "sessions")
	if err := os.MkdirAll(codexRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(codexRoot, "codex-session.fixture.jsonl"))

	out := runJSON(t, "crawl", "sessions", "--json")
	if out["inserted_items"].(float64) == 0 {
		t.Fatalf("crawl sessions inserted no items: %v", out)
	}
	search := runJSON(t, "search", "exec_command", "--source", "codex", "--json")
	if len(search["results"].([]any)) == 0 {
		t.Fatalf("crawl sessions did not index codex fixture: %v", search)
	}
}

func TestCrawlSessionsImportsDiscoveredOpenCodeRoot(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	opencodeRoot := filepath.Join(os.Getenv("HOME"), ".local", "share", "opencode")
	if err := os.MkdirAll(opencodeRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/opencode-export.fixture.json"), filepath.Join(opencodeRoot, "opencode-export.fixture.json"))

	out := runJSON(t, "crawl", "sessions", "--json")
	if out["inserted_items"].(float64) == 0 {
		t.Fatalf("crawl sessions inserted no items: %v", out)
	}
	search := runJSON(t, "search", "OpenCode adapter contract", "--source", "opencode", "--json")
	if len(search["results"].([]any)) == 0 {
		t.Fatalf("crawl sessions did not index opencode fixture: %v", search)
	}
}

func TestCrawlDocsWrapsSourceHarvestWithDefaults(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	sourceharvestDir := t.TempDir()
	script := filepath.Join(sourceharvestDir, "sourceharvest")
	body := `#!/bin/sh
mode="$1"
path="$2"
source=''
collection=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) shift; source="$1" ;;
    --collection) shift; collection="$1" ;;
  esac
  shift || true
done
if [ -z "$source" ] || [ -z "$collection" ]; then
  echo "missing source or collection" >&2
  exit 1
fi
printf '{"schema":"miseledger.adapter.v1","source":{"kind":"%s","name":"SourceHarvest Fixture"},"collection":{"external_id":"%s","kind":"notes","name":"notes"},"item":{"external_id":"%s:item:%s","kind":"note","created_at":"2026-06-03T00:00:00Z","text":"Crawl docs wrapper fixture evidence","tags":["%s","%s"]},"actor":{"external_id":"%s:system:fixture","type":"system","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:test","path":"%s","ordinal":1}}\n' "$source" "$collection" "$source" "$mode" "$source" "$mode" "$source" "$path"
printf '{"source":"%s","path":"%s","records":1,"files":1,"warnings":[],"generated_at":"2026-06-03T00:00:00Z"}\n' "$source" "$path" >&2
`
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", sourceharvestDir+string(os.PathListSeparator)+oldPath)
	docsDir := filepath.Join(t.TempDir(), "docs")
	if err := os.MkdirAll(docsDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(docsDir, "note.md"), []byte("crawl docs fixture"), 0o600); err != nil {
		t.Fatal(err)
	}

	out := runJSON(t, "crawl", "docs", docsDir, "--json")
	if out["inserted_items"].(float64) != 1 {
		t.Fatalf("crawl docs inserted = %v, want 1: %v", out["inserted_items"], out)
	}
	search := runJSON(t, "search", "Crawl docs wrapper", "--source", "docs", "--json")
	if len(search["results"].([]any)) != 1 {
		t.Fatalf("crawl docs search failed: %v", search)
	}
	scans := runJSON(t, "scans", "list", "--source", "docs", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("crawl docs scan manifest missing: %v", scans)
	}
}

func TestCrawlExternalExporterWrappersIncludeGithubAndTelegram(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	binDir := t.TempDir()
	for _, tc := range []struct {
		binary string
		source string
		text   string
	}{
		{"gitcrawl", "github", "gitcrawl wrapper fixture"},
		{"telecrawl", "telegram", "telecrawl wrapper fixture"},
	} {
		script := filepath.Join(binDir, tc.binary)
		body := fmt.Sprintf(`#!/bin/sh
if [ "$1" != "export" ] || [ "$2" != "adapter" ] || [ "$3" != "--out" ] || [ "$4" != "-" ]; then
  echo "bad adapter export args: $*" >&2
  exit 1
fi
printf '{"schema":"miseledger.adapter.v1","source":{"kind":%q,"name":"Fixture"},"collection":{"external_id":%q,"kind":"messages","name":"Fixture"},"item":{"external_id":%q,"kind":"message","created_at":"2026-06-03T00:00:00Z","text":%q,"tags":["wrapper"]},"actor":{"external_id":%q,"type":"system","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":%q,"ordinal":1}}\n'
`, tc.source, tc.source+":collection", tc.source+":item:1", tc.text, tc.source+":actor", tc.binary+".jsonl")
		if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+oldPath)

	for _, tc := range []struct {
		command string
		source  string
		query   string
	}{
		{"github", "github", "gitcrawl wrapper"},
		{"telegram", "telegram", "telecrawl wrapper"},
	} {
		out := runJSON(t, "crawl", tc.command, "--json")
		if out["source_kind"] != tc.source || out["inserted_items"].(float64) != 1 {
			t.Fatalf("crawl %s = %v, want one %s item", tc.command, out, tc.source)
		}
		search := runJSON(t, "search", tc.query, "--source", tc.source, "--json")
		if len(search["results"].([]any)) != 1 {
			t.Fatalf("crawl %s search failed: %v", tc.command, search)
		}
	}
}

func TestCrawlProviderExports(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	chatGPTFixture := repoPath(t, "testdata/exports/chatgpt-conversations.json")
	claudeFixture := repoPath(t, "testdata/exports/claude-conversations.json")

	chatGPTOut := runJSON(t, "crawl", "chatgpt-export", chatGPTFixture, "--json")
	if chatGPTOut["inserted_items"].(float64) != 2 {
		t.Fatalf("chatgpt inserted = %v, want 2: %v", chatGPTOut["inserted_items"], chatGPTOut)
	}
	claudeOut := runJSON(t, "crawl", "claude-export", claudeFixture, "--json")
	if claudeOut["inserted_items"].(float64) != 2 {
		t.Fatalf("claude inserted = %v, want 2: %v", claudeOut["inserted_items"], claudeOut)
	}
	chatGPTSearch := runJSON(t, "search", "archive crawler", "--source", "chatgpt", "--json")
	if len(chatGPTSearch["results"].([]any)) != 1 {
		t.Fatalf("chatgpt search failed: %v", chatGPTSearch)
	}
	claudeSearch := runJSON(t, "search", "local evidence records", "--source", "claude-export", "--json")
	if len(claudeSearch["results"].([]any)) != 1 {
		t.Fatalf("claude search failed: %v", claudeSearch)
	}
}

func TestSessionsListAndSearch(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	codexFixture := repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl")
	chatGPTFixture := repoPath(t, "testdata/exports/chatgpt-conversations.json")
	runOK(t, "import", "codex", codexFixture, "--json")
	runOK(t, "crawl", "chatgpt-export", chatGPTFixture, "--json")

	listed := runJSON(t, "sessions", "list", "--source", "codex", "--json")
	listSessions := listed["sessions"].([]any)
	if len(listSessions) != 1 {
		t.Fatalf("codex session list = %v", listed)
	}
	codexSession := listSessions[0].(map[string]any)
	if codexSession["source_kind"] != "codex" || codexSession["raw_path"] == "" || codexSession["sample_item_id"] == "" {
		t.Fatalf("codex session missing locator fields: %v", codexSession)
	}

	found := runJSON(t, "sessions", "search", "exec_command", "--source", "codex", "--json")
	foundSessions := found["sessions"].([]any)
	if len(foundSessions) != 1 {
		t.Fatalf("codex session search = %v", found)
	}
	hit := foundSessions[0].(map[string]any)
	if hit["match_count"].(float64) == 0 || !strings.Contains(hit["snippet"].(string), "exec_command") {
		t.Fatalf("codex session hit missing search context: %v", hit)
	}

	chatFound := runJSON(t, "sessions", "search", "archive crawler", "--source", "chatgpt", "--json")
	if len(chatFound["sessions"].([]any)) != 1 {
		t.Fatalf("chatgpt session search = %v", chatFound)
	}
}

func TestCrawlCursorImportsFromDefaultRoot(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	// withTempHome points XDG_CONFIG_HOME at <home>/.config, so the default
	// Cursor root is <home>/.config/cursor; `crawl cursor` finds it with no path.
	root := filepath.Join(os.Getenv("XDG_CONFIG_HOME"), "cursor")
	mustWrite(t, filepath.Join(root, "prompt_history.json"), `["fix the auth timeout bug","release audit checklist"]`)
	mustWrite(t, filepath.Join(root, "chats", "abc123", "meta.json"), `{"id":"abc123","title":"Auth timeout investigation","createdAt":"2026-06-01T00:00:00Z"}`)

	runOK(t, "crawl", "cursor", "--json")

	sessions := runJSON(t, "sessions", "search", "auth timeout", "--source", "cursor", "--json")
	hits := sessions["sessions"].([]any)
	if len(hits) != 1 {
		t.Fatalf("cursor session search = %v", sessions)
	}
	hit := hits[0].(map[string]any)
	if hit["raw_path"] == "" || !strings.Contains(hit["snippet"].(string), "Auth") {
		t.Fatalf("cursor session hit missing locator/snippet: %v", hit)
	}

	prompts := runJSON(t, "search", "release audit", "--source", "cursor", "--json")
	if len(prompts["results"].([]any)) == 0 {
		t.Fatalf("cursor prompt-history search returned nothing: %v", prompts)
	}
}

func TestCrawlChatGPTExportZip(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	raw, err := os.ReadFile(repoPath(t, "testdata/exports/chatgpt-conversations.json"))
	if err != nil {
		t.Fatal(err)
	}
	zipPath := filepath.Join(t.TempDir(), "chatgpt-export.zip")
	writeTestZip(t, zipPath, map[string][]byte{"export/conversations.json": raw})

	out := runJSON(t, "crawl", "chatgpt-export", zipPath, "--json")
	if out["inserted_items"].(float64) != 2 {
		t.Fatalf("zip inserted = %v, want 2: %v", out["inserted_items"], out)
	}
	scans := runJSON(t, "scans", "list", "--source", "chatgpt", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("expected zip scan manifest: %v", scans)
	}
	first := scans["scans"].([]any)[0].(map[string]any)
	if !strings.Contains(first["path"].(string), "!/export/conversations.json") {
		t.Fatalf("scan path does not preserve zip member: %v", first)
	}
}

func TestSourceDiscoveryDoesNotPrintTranscriptContent(t *testing.T) {
	withTempHome(t)
	secret := "PRIVATE_TRANSCRIPT_SHOULD_NOT_APPEAR"
	path := filepath.Join(os.Getenv("HOME"), ".codex", "sessions", "2026", "06", "03")
	if err := os.MkdirAll(path, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, "sample.jsonl"), []byte(`{"type":"event_msg","payload":{"message":"`+secret+`"}}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	hermesPath := filepath.Join(os.Getenv("HOME"), ".hermes", "sessions")
	if err := os.MkdirAll(hermesPath, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(hermesPath, "session_demo.json"), []byte(`{"messages":[{"role":"user","content":"`+secret+`"}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	opencodePath := filepath.Join(os.Getenv("HOME"), ".local", "share", "opencode")
	if err := os.MkdirAll(opencodePath, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(opencodePath, "session_demo.json"), []byte(`{"messages":[{"parts":[{"type":"text","text":"`+secret+`"}]}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	out := runOK(t, "sources", "discover", "--json")
	if strings.Contains(out, secret) || strings.Contains(out, "event_msg") {
		t.Fatalf("source discovery leaked content: %s", out)
	}
	var discovered []map[string]any
	if err := json.Unmarshal([]byte(out), &discovered); err != nil {
		t.Fatalf("invalid discovery json: %v", err)
	}
	if len(discovered) == 0 {
		t.Fatalf("expected discovery candidates")
	}
	foundHermes := false
	foundOpenCode := false
	for _, item := range discovered {
		if item["source_kind"] == "hermes" {
			foundHermes = true
			if item["status"] != "native-json" || item["count"].(float64) != 1 {
				t.Fatalf("unexpected Hermes discovery row: %v", item)
			}
		}
		if item["source_kind"] == "opencode" {
			foundOpenCode = true
			if item["status"] != "native-json" || item["count"].(float64) != 1 {
				t.Fatalf("unexpected OpenCode discovery row: %v", item)
			}
		}
		for key := range item {
			switch key {
			case "source_kind", "root", "exists", "count", "status":
			default:
				t.Fatalf("unexpected discovery key %q in %v", key, item)
			}
		}
	}
	if !foundHermes {
		t.Fatalf("expected Hermes discovery row: %v", discovered)
	}
	if !foundOpenCode {
		t.Fatalf("expected OpenCode discovery row: %v", discovered)
	}
}

func TestNativeAdaptersImportAndEvidence(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	crawler := repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")
	codexFixture := repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl")
	claudeFixture := repoPath(t, "testdata/harnesses/claude-project.fixture.jsonl")
	openclawFixture := repoPath(t, "testdata/harnesses/openclaw-session.fixture.jsonl")
	trajectoryFixture := repoPath(t, "testdata/harnesses/openclaw-trajectory.fixture.jsonl")
	hermesSnapshotFixture := repoPath(t, "testdata/harnesses/session_hermes-demo.fixture.json")
	hermesTrajectoryFixture := repoPath(t, "testdata/harnesses/hermes-trajectory.fixture.jsonl")
	opencodeFixture := repoPath(t, "testdata/harnesses/opencode-export.fixture.json")
	malformedFixture := repoPath(t, "testdata/harnesses/malformed-unknown.fixture.jsonl")

	adapterJSONL := runOK(t, "adapter", "codex", codexFixture, "--out", "-")
	lines := strings.Split(strings.TrimSpace(adapterJSONL), "\n")
	if len(lines) == 0 {
		t.Fatalf("codex adapter emitted no records")
	}
	for _, line := range lines {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("adapter emitted invalid json: %v\n%s", err, line)
		}
		if rec["schema"] != "miseledger.adapter.v1" {
			t.Fatalf("adapter schema = %v", rec["schema"])
		}
	}
	if !strings.Contains(adapterJSONL, "exec_command") || !strings.Contains(adapterJSONL, "encrypted_content present") {
		t.Fatalf("codex adapter did not include real response_item shapes: %s", adapterJSONL)
	}
	hermesAdapterJSONL := runOK(t, "adapter", "hermes", hermesSnapshotFixture, "--out", "-")
	if !strings.Contains(hermesAdapterJSONL, "miseledger.adapter.v1") || !strings.Contains(hermesAdapterJSONL, "Hermes snapshots") {
		t.Fatalf("hermes adapter did not emit expected records: %s", hermesAdapterJSONL)
	}
	opencodeAdapterJSONL := runOK(t, "adapter", "opencode", opencodeFixture, "--out", "-")
	if !strings.Contains(opencodeAdapterJSONL, "miseledger.adapter.v1") || !strings.Contains(opencodeAdapterJSONL, "OpenCode adapter contract") {
		t.Fatalf("opencode adapter did not emit expected records: %s", opencodeAdapterJSONL)
	}

	runOK(t, "import", "adapter", crawler, "--source", "discrawl")
	codexImport := runJSON(t, "import", "codex", codexFixture, "--json")
	if codexImport["inserted_items"].(float64) == 0 {
		t.Fatalf("codex import inserted no items: %v", codexImport)
	}
	openclawImport := runJSON(t, "import", "openclaw", openclawFixture, "--json")
	if openclawImport["inserted_items"].(float64) == 0 {
		t.Fatalf("openclaw import inserted no items: %v", openclawImport)
	}
	claudeImport := runJSON(t, "import", "claude", claudeFixture, "--json")
	if claudeImport["inserted_items"].(float64) == 0 {
		t.Fatalf("claude import inserted no items: %v", claudeImport)
	}
	runOK(t, "import", "openclaw", trajectoryFixture, "--json")
	hermesSnapshotImport := runJSON(t, "import", "hermes", hermesSnapshotFixture, "--json")
	if hermesSnapshotImport["inserted_items"].(float64) == 0 {
		t.Fatalf("hermes snapshot import inserted no items: %v", hermesSnapshotImport)
	}
	hermesTrajectoryImport := runJSON(t, "import", "hermes", hermesTrajectoryFixture, "--json")
	if hermesTrajectoryImport["inserted_items"].(float64) == 0 {
		t.Fatalf("hermes trajectory import inserted no items: %v", hermesTrajectoryImport)
	}
	opencodeImport := runJSON(t, "import", "opencode", opencodeFixture, "--json")
	if opencodeImport["inserted_items"].(float64) == 0 {
		t.Fatalf("opencode import inserted no items: %v", opencodeImport)
	}

	before := runJSON(t, "status", "--json")
	runOK(t, "import", "codex", codexFixture, "--json")
	runOK(t, "import", "openclaw", openclawFixture, "--json")
	runOK(t, "import", "claude", claudeFixture, "--json")
	runOK(t, "import", "hermes", hermesSnapshotFixture, "--json")
	runOK(t, "import", "hermes", hermesTrajectoryFixture, "--json")
	runOK(t, "import", "opencode", opencodeFixture, "--json")
	after := runJSON(t, "status", "--json")
	if before["items"] != after["items"] {
		t.Fatalf("reimport changed item count: before=%v after=%v", before["items"], after["items"])
	}
	scans := runJSON(t, "scans", "list", "--json")
	scanItems := scans["scans"].([]any)
	if len(scanItems) < 3 {
		t.Fatalf("scan manifest too small: %v", scans)
	}
	firstScan := scanItems[0].(map[string]any)
	shownScan := runJSON(t, "scans", "show", firstScan["id"].(string), "--json")
	if shownScan["id"] != firstScan["id"] {
		t.Fatalf("scan show mismatch: %v vs %v", shownScan, firstScan)
	}

	crawlerSearch := runJSON(t, "search", "adapter contract", "--source", "discrawl", "--json")
	if len(crawlerSearch["results"].([]any)) == 0 {
		t.Fatalf("crawler search returned no results")
	}
	agentSearch := runJSON(t, "search", "adapter contract", "--source", "codex", "--json")
	if len(agentSearch["results"].([]any)) == 0 {
		t.Fatalf("codex search returned no results")
	}
	openclawSearch := runJSON(t, "search", "normalized schema", "--source", "openclaw", "--json")
	if len(openclawSearch["results"].([]any)) == 0 {
		t.Fatalf("openclaw search returned no results")
	}
	claudeSearch := runJSON(t, "search", "Claude native import", "--source", "claude", "--json")
	if len(claudeSearch["results"].([]any)) == 0 {
		t.Fatalf("claude search returned no results")
	}
	hermesSearch := runJSON(t, "search", "Hermes snapshots", "--source", "hermes", "--json")
	if len(hermesSearch["results"].([]any)) == 0 {
		t.Fatalf("hermes snapshot search returned no results")
	}
	hermesTrajectorySearch := runJSON(t, "search", "trajectory adapter", "--source", "hermes", "--json")
	if len(hermesTrajectorySearch["results"].([]any)) == 0 {
		t.Fatalf("hermes trajectory search returned no results")
	}
	opencodeSearch := runJSON(t, "search", "OpenCode adapter contract", "--source", "opencode", "--json")
	if len(opencodeSearch["results"].([]any)) == 0 {
		t.Fatalf("opencode search returned no results")
	}
	commandSearch := runJSON(t, "search", "exec_command", "--source", "codex", "--kind", "command", "--json")
	commandResults := commandSearch["results"].([]any)
	if len(commandResults) == 0 {
		t.Fatalf("codex function call command search returned no results")
	}
	commandID := commandResults[0].(map[string]any)["id"].(string)
	commandShow := runJSON(t, "show", commandID, "--json")
	commandMeta := commandShow["metadata"].(map[string]any)
	if commandMeta["call_id"] != "call-123" || commandMeta["name"] != "exec_command" || commandMeta["payload_type"] != "function_call" {
		t.Fatalf("codex call metadata not preserved: %v", commandMeta)
	}
	codexResult := runJSON(t, "search", "call-123", "--source", "codex", "--kind", "tool_call", "--json")
	if len(codexResult["results"].([]any)) == 0 {
		t.Fatalf("codex call result search returned no results: %v", codexResult)
	}
	codexResultID := codexResult["results"].([]any)[0].(map[string]any)["id"].(string)
	codexResultShow := runJSON(t, "show", codexResultID, "--json")
	codexRelations := codexResultShow["relations"].([]any)
	if len(codexRelations) == 0 || codexRelations[0].(map[string]any)["target_item_id"] == nil {
		t.Fatalf("codex call result relation was not resolved: %v", codexResultShow)
	}
	claudeTool := runJSON(t, "search", "evidence examples", "--source", "claude", "--kind", "tool_call", "--json")
	if len(claudeTool["results"].([]any)) == 0 {
		t.Fatalf("claude tool result search returned no results: %v", claudeTool)
	}
	claudeToolID := claudeTool["results"].([]any)[0].(map[string]any)["id"].(string)
	claudeToolShow := runJSON(t, "show", claudeToolID, "--json")
	claudeRelations := claudeToolShow["relations"].([]any)
	if len(claudeRelations) == 0 || claudeRelations[0].(map[string]any)["target_item_id"] == nil {
		t.Fatalf("claude tool result relation was not resolved: %v", claudeToolShow)
	}
	hermesToolResult := runJSON(t, "search", "adapter smoke completed", "--source", "hermes", "--kind", "tool_call", "--json")
	if len(hermesToolResult["results"].([]any)) == 0 {
		t.Fatalf("hermes tool result search returned no results: %v", hermesToolResult)
	}
	hermesToolID := hermesToolResult["results"].([]any)[0].(map[string]any)["id"].(string)
	hermesToolShow := runJSON(t, "show", hermesToolID, "--json")
	hermesRelations := hermesToolShow["relations"].([]any)
	if len(hermesRelations) == 0 || hermesRelations[0].(map[string]any)["target_item_id"] == nil {
		t.Fatalf("hermes tool result relation was not resolved: %v", hermesToolShow)
	}

	evidence := runJSON(t, "evidence", "adapter contract", "--json")
	if evidence["untrusted_context"] != true {
		t.Fatalf("evidence missing untrusted_context: %v", evidence)
	}
	results := evidence["results"].([]any)
	if len(results) == 0 {
		t.Fatalf("evidence returned no results")
	}
	first := results[0].(map[string]any)
	rawRef := first["raw_ref"].(map[string]any)
	if rawRef["path"] == "" || rawRef["hash"] == "" {
		t.Fatalf("evidence missing raw refs: %v", first)
	}
	if _, ok := first["artifacts"].([]any); !ok {
		t.Fatalf("evidence artifacts was not an array: %T %v", first["artifacts"], first["artifacts"])
	}
	projectEvidence := runJSON(t, "evidence", "Claude native import", "--project", "miseledger", "--json")
	if len(projectEvidence["results"].([]any)) == 0 {
		t.Fatalf("project-filtered evidence returned no results: %v", projectEvidence)
	}

	dryRun := runJSON(t, "import", "codex", malformedFixture, "--dry-run", "--json")
	if dryRun["generated_records"].(float64) == 0 {
		t.Fatalf("malformed fixture did not preserve valid records: %v", dryRun)
	}
	if len(dryRun["warnings"].([]any)) == 0 {
		t.Fatalf("malformed fixture produced no warnings: %v", dryRun)
	}
	discovery := runJSONArray(t, "sources", "discover", "--json")
	if len(discovery) == 0 {
		t.Fatalf("source discovery returned no candidates: %v", discovery)
	}
}

func TestDirectoryImportRecordsEachScannedFile(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(dir, "one.jsonl"))
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(dir, "two.jsonl"))
	runOK(t, "import", "codex", dir, "--json")
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	scanItems := scans["scans"].([]any)
	if len(scanItems) != 2 {
		t.Fatalf("scan rows = %d, want 2: %v", len(scanItems), scans)
	}
	for _, scan := range scanItems {
		row := scan.(map[string]any)
		if row["records_generated"].(float64) == 0 || row["content_hash"] == "" || row["generated_hash"] == "" {
			t.Fatalf("incomplete scan row: %v", row)
		}
	}
	firstPath := scanItems[0].(map[string]any)["path"].(string)
	diff := runJSON(t, "scans", "diff", firstPath, "--json")
	if diff["changed"] != false || diff["status"] != "unchanged" {
		t.Fatalf("initial scan diff = %v", diff)
	}
	f, err := os.OpenFile(firstPath, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteString("\n"); err != nil {
		_ = f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	changed := runJSON(t, "scans", "changed", "--source", "codex", "--json")
	if len(changed["changed"].([]any)) == 0 {
		t.Fatalf("changed scan was not detected: %v", changed)
	}
}

func TestNativeImportFastPathSkipsSecondRun(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(dir, "one.jsonl"))

	first := runJSON(t, "import", "codex", dir, "--json")
	if first["files_parsed"].(float64) != 1 || first["files_skipped"].(float64) != 0 {
		t.Fatalf("first import counters = %v", first)
	}
	second := runJSON(t, "import", "codex", dir, "--json")
	if second["files_parsed"].(float64) != 0 || second["files_skipped"].(float64) != 1 {
		t.Fatalf("second import counters = %v", second)
	}
	if second["inserted_items"].(float64) != 0 {
		t.Fatalf("second import inserted items despite skip: %v", second)
	}
	normal := runOK(t, "import", "codex", dir)
	if !strings.Contains(normal, "files_parsed=0") || !strings.Contains(normal, "files_skipped=1") {
		t.Fatalf("normal output missing file counters: %s", normal)
	}
	when := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(filepath.Join(dir, "one.jsonl"), when, when); err != nil {
		t.Fatal(err)
	}
	touched := runJSON(t, "import", "codex", dir, "--json")
	if touched["files_parsed"].(float64) != 0 || touched["files_skipped"].(float64) != 1 {
		t.Fatalf("mtime-only change should use hash fallback skip: %v", touched)
	}
	withSince := runJSON(t, "import", "codex", dir, "--since", "2030-01-01", "--json")
	if withSince["files_parsed"].(float64) != 1 || withSince["files_skipped"].(float64) != 0 || withSince["inserted_items"].(float64) != 0 {
		t.Fatalf("--since import should parse and filter records: %v", withSince)
	}
}

func TestNativeImportFastPathReparsesSameSizeChangedFile(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	path := filepath.Join(t.TempDir(), "session.jsonl")
	writeCodexFixture(t, path, "alpha")
	first := runJSON(t, "import", "codex", path, "--json")
	if first["files_parsed"].(float64) != 1 || first["files_skipped"].(float64) != 0 {
		t.Fatalf("first import counters = %v", first)
	}

	writeCodexFixture(t, path, "bravo")
	when := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(path, when, when); err != nil {
		t.Fatal(err)
	}
	second := runJSON(t, "import", "codex", path, "--json")
	if second["files_parsed"].(float64) != 1 || second["files_skipped"].(float64) != 0 {
		t.Fatalf("changed file should be reparsed: %v", second)
	}
	if second["inserted_items"].(float64) == 0 {
		t.Fatalf("changed file import inserted no new item: %v", second)
	}
}

func TestNativeImportDryRunDoesNotWriteSourceScans(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(dir, "one.jsonl"))

	dry := runJSON(t, "import", "codex", dir, "--dry-run", "--json")
	if dry["files_parsed"].(float64) != 1 || dry["files_skipped"].(float64) != 0 {
		t.Fatalf("dry-run counters = %v", dry)
	}
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	if raw := scans["scans"]; raw != nil {
		if got := len(raw.([]any)); got != 0 {
			t.Fatalf("dry-run wrote %d source_scans rows: %v", got, scans)
		}
	}
}

func TestCrawlSessionsUsesNativeFastPath(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	root := filepath.Join(os.Getenv("HOME"), ".codex", "sessions", "2026", "06", "03")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(root, "codex.jsonl"))

	first := runJSON(t, "crawl", "sessions", "--json")
	firstCodex := discoveredSource(t, first, "codex")
	if firstCodex["files_parsed"].(float64) != 1 || firstCodex["files_skipped"].(float64) != 0 {
		t.Fatalf("first crawl counters = %v", first)
	}
	second := runJSON(t, "crawl", "sessions", "--json")
	secondCodex := discoveredSource(t, second, "codex")
	if secondCodex["files_parsed"].(float64) != 0 || secondCodex["files_skipped"].(float64) != 1 {
		t.Fatalf("second crawl counters = %v", second)
	}
	if second["files_skipped"].(float64) == 0 {
		t.Fatalf("crawl summary did not report skipped files: %v", second)
	}
}

func TestArchiveOperations(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	runOK(t, "import", "codex", repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), "--json")
	runOK(t, "import", "claude", repoPath(t, "testdata/harnesses/claude-project.fixture.jsonl"), "--json")

	stats := runJSON(t, "stats", "--json")
	totals := stats["totals"].(map[string]any)
	if totals["items"].(float64) == 0 || totals["sources"].(float64) == 0 {
		t.Fatalf("bad stats totals: %v", stats)
	}
	if len(stats["by_source"].([]any)) == 0 || len(stats["by_item_kind"].([]any)) == 0 {
		t.Fatalf("stats missing groups: %v", stats)
	}

	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	res, err := db.Exec(`update relations set target_item_id = null where coalesce(target_external_id,'') != ''`)
	if err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	n, _ := res.RowsAffected()
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if n == 0 {
		t.Fatalf("fixture imports produced no relations to backfill")
	}

	backfill := runJSON(t, "relations", "backfill", "--json")
	if backfill["resolved"].(float64) == 0 || backfill["unresolved_after"].(float64) != 0 {
		t.Fatalf("bad relation backfill result: %v", backfill)
	}

	compact := runJSON(t, "compact", "--json")
	if compact["ok"] != true || compact["after_size_bytes"].(float64) == 0 {
		t.Fatalf("bad compact result: %v", compact)
	}

	doctor := runJSON(t, "doctor", "--archive", "--json")
	if doctor["ok"] != true {
		t.Fatalf("doctor --archive not ok: %v", doctor)
	}

	explained := runJSON(t, "explain", "adapter contract", "--source", "codex", "--json")
	if explained["untrusted_context"] != true || explained["result_count"].(float64) == 0 {
		t.Fatalf("bad explain result: %v", explained)
	}

	evidence := runJSON(t, "evidence", "adapter contract", "--source", "codex", "--json")
	if evidence["id"] == "" || !strings.HasPrefix(evidence["resource_uri"].(string), "miseledger://evidence/") {
		t.Fatalf("evidence missing stable reference: %v", evidence)
	}
	shown := runJSON(t, "evidence", "show", evidence["id"].(string), "--json")
	if shown["id"] != evidence["id"] {
		t.Fatalf("evidence show mismatch: %v vs %v", shown, evidence)
	}
	listed := runJSON(t, "evidence", "list", "--json")
	if len(listed["bundles"].([]any)) == 0 {
		t.Fatalf("evidence list empty: %v", listed)
	}

	params := json.RawMessage(`{"name":"show_evidence_bundle","arguments":{"id":"` + evidence["id"].(string) + `"}}`)
	resp := handleMCPRequest(mcpRequest{JSONRPC: "2.0", ID: float64(7), Method: "tools/call", Params: params})
	if resp.Error != nil {
		t.Fatalf("mcp show_evidence_bundle error: %#v", resp.Error)
	}

	db, _, err = openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(`update imports set completed_at = '2001-01-01T00:00:00Z'`)
	if err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	_, err = db.Exec(`insert or ignore into import_warnings(import_id, ordinal, warning) select id, 99, 'old warning' from imports limit 1`)
	if err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	pruneDry := runJSON(t, "prune", "imports", "--before", "2002-01-01", "--dry-run", "--json")
	if pruneDry["matched_imports"].(float64) == 0 || pruneDry["dry_run"] != true {
		t.Fatalf("bad prune imports dry-run: %v", pruneDry)
	}
	pruneImports := runJSON(t, "prune", "imports", "--before", "2002-01-01", "--json")
	if pruneImports["deleted_imports"].(float64) == 0 {
		t.Fatalf("bad prune imports: %v", pruneImports)
	}

	dir := t.TempDir()
	scanFile := filepath.Join(dir, "gone.jsonl")
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), scanFile)
	runOK(t, "import", "codex", scanFile, "--json")
	if err := os.Remove(scanFile); err != nil {
		t.Fatal(err)
	}
	pruneScans := runJSON(t, "prune", "scans", "--missing", "--json")
	if pruneScans["deleted_scans"].(float64) == 0 {
		t.Fatalf("bad prune scans: %v", pruneScans)
	}
}

func TestPrunePolicyDryRunReportsOperationalNoiseOnly(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	fixture := filepath.Join(t.TempDir(), "retention.adapter.jsonl")
	body := strings.Join([]string{
		retentionRecord("old-tool", "tool_call", "2020-01-02T00:00:00Z", "old tool output", nil),
		retentionRecord("old-message", "message", "2020-01-02T00:00:00Z", "old message to keep", nil),
		retentionRecord("fresh-tool", "tool_call", "2030-01-02T00:00:00Z", "fresh tool output", nil),
	}, "")
	if err := os.WriteFile(fixture, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", fixture, "--json")

	dry := runJSON(t, "prune", "policy", "--json")
	if dry["dry_run"] != true || dry["matched_items"].(float64) != 1 {
		t.Fatalf("dry run = %v, want one matched item and dry_run true", dry)
	}
	flagForm := runJSON(t, "prune", "--policy", "default", "--json")
	if flagForm["dry_run"] != true || flagForm["matched_items"].(float64) != 1 {
		t.Fatalf("flag-form dry run = %v, want one matched item and dry_run true", flagForm)
	}
	tiers := dry["tiers"].([]any)
	if len(tiers) != 1 {
		t.Fatalf("tiers = %v, want one matched tier", tiers)
	}
	tier := tiers[0].(map[string]any)
	if tier["tier"] != "default-operational-noise" || tier["item_kind"] != "tool_call" {
		t.Fatalf("tier = %v, want old tool_call operational noise", tier)
	}
	status := runJSON(t, "status", "--json")
	if status["items"].(float64) != 3 {
		t.Fatalf("dry run deleted items: %v", status)
	}
}

func TestPrunePolicyApplyExportsDeletesAndTombstones(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	fixture := filepath.Join(t.TempDir(), "retention.adapter.jsonl")
	body := strings.Join([]string{
		retentionRecord("old-tool", "tool_call", "2020-01-02T00:00:00Z", "old tool output", nil),
		retentionRecord("old-message", "message", "2020-01-02T00:00:00Z", "old message to keep", []map[string]string{{"target_external_id": "old-tool", "type": "mentions"}}),
	}, "")
	if err := os.WriteFile(fixture, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", fixture, "--json")
	runOK(t, "relations", "backfill", "--json")

	exportPath := filepath.Join(t.TempDir(), "retention-prune.jsonl.gz")
	out := runJSON(t, "prune", "policy", "--apply", "--export", exportPath, "--json")
	if out["dry_run"] != false || out["deleted_items"].(float64) != 1 || out["exported_items"].(float64) != 1 {
		t.Fatalf("apply result = %v, want one exported and deleted item", out)
	}
	if out["tombstoned_relations"].(float64) != 1 {
		t.Fatalf("tombstoned_relations = %v, want 1", out["tombstoned_relations"])
	}
	lines := readGzipLines(t, exportPath)
	if len(lines) != 1 || !strings.Contains(lines[0], `"external_id":"old-tool"`) {
		t.Fatalf("export lines = %v, want old-tool JSONL only", lines)
	}

	searchDeleted := runJSON(t, "search", "old tool output", "--json")
	if len(searchDeleted["results"].([]any)) != 0 {
		t.Fatalf("deleted tool_call still searchable: %v", searchDeleted)
	}
	searchKept := runJSON(t, "search", "old message to keep", "--json")
	if len(searchKept["results"].([]any)) != 1 {
		t.Fatalf("kept message missing after prune: %v", searchKept)
	}
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	var target sql.NullString
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'mentions'`).Scan(&target); err != nil {
		_ = db.Close()
		t.Fatal(err)
	}
	if target.Valid {
		_ = db.Close()
		t.Fatalf("relation target_item_id = %q after prune, want tombstone null", target.String)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	restored := runJSON(t, "import", "adapter", exportPath, "--json")
	if restored["inserted_items"].(float64) != 1 {
		t.Fatalf("restore from prune export = %v, want one restored item", restored)
	}
	restoredSearch := runJSON(t, "search", "old tool output", "--json")
	if len(restoredSearch["results"].([]any)) != 1 {
		t.Fatalf("restored tool_call missing from search: %v", restoredSearch)
	}
	db, _, err = openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'mentions'`).Scan(&target); err != nil {
		t.Fatal(err)
	}
	if !target.Valid {
		t.Fatal("relation target_item_id stayed null after restore, want rehydrated target")
	}
}

func TestImportDiscoveredAndWatchOnce(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	root := filepath.Join(os.Getenv("HOME"), ".codex", "sessions", "2026", "06", "03")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(root, "codex.jsonl"))
	hermesRoot := filepath.Join(os.Getenv("HOME"), ".hermes", "sessions")
	if err := os.MkdirAll(hermesRoot, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/session_hermes-demo.fixture.json"), filepath.Join(hermesRoot, "session_hermes-demo.json"))
	out := runJSON(t, "import", "discovered", "--json")
	if out["inserted_items"].(float64) == 0 {
		t.Fatalf("discovered import inserted no items: %v", out)
	}
	again := runJSON(t, "watch", "once", "--json")
	if again["inserted_items"].(float64) != 0 {
		t.Fatalf("watch once was not idempotent: %v", again)
	}
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("expected discovered scan manifest: %v", scans)
	}
	hermesScans := runJSON(t, "scans", "list", "--source", "hermes", "--json")
	if len(hermesScans["scans"].([]any)) != 1 {
		t.Fatalf("expected Hermes discovered scan manifest: %v", hermesScans)
	}
	skipped := runJSON(t, "watch", "once", "--if-changed", "--json")
	if skipped["skipped"] != true {
		t.Fatalf("watch once --if-changed should skip unchanged scans: %v", skipped)
	}
	runOK(t, "watch", "daemon", "--max-runs", "1", "--json")
}

func TestHTTPAPIAndMCPTools(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	runOK(t, "import", "adapter", repoPath(t, "testdata/adapters/discrawl.fixture.jsonl"), "--source", "discrawl")

	handler := newHTTPHandler()
	req := httptest.NewRequest(http.MethodGet, "/search?q=adapter+contract&source=discrawl", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("search http status=%d body=%s", rec.Code, rec.Body.String())
	}
	var searchBody map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &searchBody); err != nil {
		t.Fatalf("bad search body: %v", err)
	}
	results := searchBody["results"].([]any)
	if len(results) == 0 {
		t.Fatalf("http search returned no results: %v", searchBody)
	}
	id := results[0].(map[string]any)["id"].(string)

	req = httptest.NewRequest(http.MethodGet, "/items/"+id, nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("show http status=%d body=%s", rec.Code, rec.Body.String())
	}

	// The browser UI is served at the root; unknown paths stay 404.
	req = httptest.NewRequest(http.MethodGet, "/", nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `id="q"`) {
		t.Fatalf("ui http status=%d (want 200 with search box)", rec.Code)
	}
	req = httptest.NewRequest(http.MethodGet, "/does-not-exist", nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown path status=%d (want 404)", rec.Code)
	}

	// The session finder endpoint backs the UI Sessions mode.
	req = httptest.NewRequest(http.MethodGet, "/sessions?q=adapter+contract&source=discrawl", nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("sessions http status=%d body=%s", rec.Code, rec.Body.String())
	}
	var sessionsBody map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &sessionsBody); err != nil {
		t.Fatalf("bad sessions body: %v", err)
	}
	if _, ok := sessionsBody["sessions"]; !ok {
		t.Fatalf("sessions body missing sessions key: %v", sessionsBody)
	}

	// The transcript endpoint backs the detail pane; it needs collection+source.
	req = httptest.NewRequest(http.MethodGet, "/session/items", nil)
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("session/items without params status=%d (want 400)", rec.Code)
	}

	req = httptest.NewRequest(http.MethodPost, "/evidence", strings.NewReader(`{"query":"adapter contract","source":"discrawl","limit":5,"include_related":true,"include_artifact_text":true}`))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("evidence http status=%d body=%s", rec.Code, rec.Body.String())
	}
	var evidence map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &evidence); err != nil {
		t.Fatalf("bad evidence body: %v", err)
	}
	if evidence["untrusted_context"] != true || len(evidence["grouped_by_source"].(map[string]any)) == 0 {
		t.Fatalf("bad evidence body: %v", evidence)
	}
	if evidence["id"] == "" || evidence["resource_uri"] == "" {
		t.Fatalf("http evidence missing stable reference: %v", evidence)
	}
	firstEvidence := evidence["results"].([]any)[0].(map[string]any)
	if firstEvidence["score"] == "" {
		t.Fatalf("evidence missing score: %v", firstEvidence)
	}

	params := json.RawMessage(`{"name":"create_evidence_bundle","arguments":{"query":"adapter contract","source":"discrawl","limit":5,"include_related":true,"include_artifact_text":true}}`)
	resp := handleMCPRequest(mcpRequest{JSONRPC: "2.0", ID: float64(1), Method: "tools/call", Params: params})
	if resp.Error != nil {
		t.Fatalf("mcp error: %#v", resp.Error)
	}
	result := resp.Result.(map[string]any)
	content := result["content"].([]map[string]any)
	if !strings.Contains(content[0]["text"].(string), `"untrusted_context":true`) {
		t.Fatalf("mcp content missing evidence bundle: %v", content)
	}
	if !strings.Contains(content[0]["text"].(string), `"resource_uri":"miseledger://evidence/`) {
		t.Fatalf("mcp content missing evidence resource uri: %v", content)
	}
}

func runOK(t *testing.T, args ...string) string {
	t.Helper()
	code, out, errb := run(args...)
	if code != 0 {
		t.Fatalf("%v failed: code=%d err=%s out=%s", args, code, errb, out)
	}
	return out
}

func runJSON(t *testing.T, args ...string) map[string]any {
	t.Helper()
	out := runOK(t, args...)
	var got map[string]any
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("%v returned invalid json: %v\n%s", args, err, out)
	}
	return got
}

func runJSONArray(t *testing.T, args ...string) []any {
	t.Helper()
	out := runOK(t, args...)
	var got []any
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("%v returned invalid json array: %v\n%s", args, err, out)
	}
	return got
}

func run(args ...string) (int, string, string) {
	var out, errb bytes.Buffer
	code := Run(args, &out, &errb)
	return code, out.String(), errb.String()
}

func mustWrite(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func retentionRecord(externalID, kind, createdAt, text string, relations []map[string]string) string {
	rec := map[string]any{
		"schema": "miseledger.adapter.v1",
		"source": map[string]any{"kind": "retention", "name": "Retention Fixture"},
		"collection": map[string]any{
			"external_id": "retention:collection",
			"kind":        "agent_session",
			"name":        "retention",
		},
		"item": map[string]any{
			"external_id": externalID,
			"kind":        kind,
			"created_at":  createdAt,
			"text":        text,
			"tags":        []string{"retention"},
		},
		"actor": map[string]any{
			"external_id": "retention:actor",
			"type":        "agent",
			"name":        "fixture",
		},
		"artifacts": []map[string]any{{
			"external_id": "artifact:" + externalID,
			"kind":        "text",
			"text":        "artifact text for " + externalID,
		}},
		"links":     []any{},
		"relations": relations,
		"raw": map[string]any{
			"format":  "json",
			"path":    "retention.jsonl",
			"ordinal": 1,
		},
	}
	b, _ := json.Marshal(rec)
	return string(b) + "\n"
}

func readGzipLines(t *testing.T, path string) []string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	defer gz.Close()
	b, err := io.ReadAll(gz)
	if err != nil {
		t.Fatal(err)
	}
	lines := []string{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if strings.TrimSpace(line) != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

func withTempHome(t *testing.T) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, ".config"))
	t.Setenv("XDG_DATA_HOME", filepath.Join(home, ".local", "share"))
	t.Setenv("XDG_CACHE_HOME", filepath.Join(home, ".cache"))
}

func assertPrivate(t *testing.T, path string) {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("%s mode = %o, want private", path, info.Mode().Perm())
	}
}

func repoPath(t *testing.T, rel string) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime.Caller failed")
	}
	return filepath.Join(filepath.Dir(file), "..", "..", rel)
}

func copyFixture(t *testing.T, from, to string) {
	t.Helper()
	data, err := os.ReadFile(from)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(to, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func insertSyntheticSearchArchive(t *testing.T, db *sql.DB, n int) {
	t.Helper()
	if _, err := db.Exec(`insert into sources(id, kind, name, created_at, updated_at) values('source-1','synthetic','Synthetic','2026-07-02T00:00:00Z','2026-07-02T00:00:00Z')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into collections(id, source_id, external_id, kind, name) values('collection-1','source-1','collection:synthetic','agent_session','Synthetic')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into actors(id, source_id, external_id, type, name) values('actor-1','source-1','actor:synthetic','agent','Synthetic')`); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("item-%03d", i)
		createdAt := fmt.Sprintf("2026-07-02T00:%02d:00Z", i)
		text := fmt.Sprintf("needle synthetic search row %03d", i)
		if _, err := db.Exec(`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, text, content_hash, raw_json)
values(?,?,?,?,?,?,?,?,?,?)`, id, "source-1", "collection-1", "actor-1", id, "message", createdAt, text, "hash-"+id, "{}"); err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`, id, "synthetic", "agent_session", "message", "agent", text); err != nil {
			t.Fatal(err)
		}
		if i%10 == 0 {
			if _, err := db.Exec(`insert into relations(id, source_item_id, target_item_id, target_external_id, relation_type, confidence) values(?,?,?,?,?,?)`, "rel-"+id, id, "item-001", "item-001", "derived_from", 1.0); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func explainPlan(t *testing.T, db *sql.DB, sqlText string, args ...any) string {
	t.Helper()
	rows, err := db.Query("explain query plan "+sqlText, args...)
	if err != nil {
		t.Fatalf("explain query plan: %v", err)
	}
	defer rows.Close()
	var details []string
	for rows.Next() {
		var id, parent, notUsed int
		var detail string
		if err := rows.Scan(&id, &parent, &notUsed, &detail); err != nil {
			t.Fatal(err)
		}
		details = append(details, detail)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return strings.Join(details, "\n")
}

func writeCodexFixture(t *testing.T, path, text string) {
	t.Helper()
	quoted, err := json.Marshal(text)
	if err != nil {
		t.Fatal(err)
	}
	line := `{"type":"event_msg","timestamp":"2026-06-03T15:00:30Z","payload":{"session_id":"fastpath-demo","type":"message","role":"assistant","text":` + string(quoted) + `}}` + "\n"
	if err := os.WriteFile(path, []byte(line), 0o600); err != nil {
		t.Fatal(err)
	}
}

func discoveredSource(t *testing.T, result map[string]any, sourceKind string) map[string]any {
	t.Helper()
	for _, raw := range result["sources"].([]any) {
		row := raw.(map[string]any)
		if row["source_kind"] == sourceKind {
			return row
		}
	}
	t.Fatalf("missing discovered source %q in %v", sourceKind, result)
	return nil
}

func writeTestZip(t *testing.T, path string, files map[string][]byte) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	zw := zip.NewWriter(f)
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		w, err := zw.Create(name)
		if err != nil {
			_ = f.Close()
			t.Fatal(err)
		}
		if _, err := w.Write(files[name]); err != nil {
			_ = f.Close()
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		_ = f.Close()
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'"
}

func TestImportDiscoveredAttributesWarningsAndFailures(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	root := filepath.Join(os.Getenv("HOME"), ".codex", "sessions", "2026", "06", "03")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	// A malformed line produces a parse warning that must be attributed.
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(root, "good.jsonl"))
	copyFixture(t, repoPath(t, "testdata/harnesses/malformed-unknown.fixture.jsonl"), filepath.Join(root, "bad.jsonl"))

	out := runJSON(t, "import", "discovered", "--json")
	if _, ok := out["failures"]; !ok {
		t.Fatalf("result missing failures key: %v", out)
	}
	warnings, _ := out["warnings"].([]any)
	if len(warnings) == 0 {
		t.Fatalf("expected attributed warnings from malformed file: %v", out)
	}
	for _, w := range warnings {
		s := w.(string)
		if !strings.Contains(s, ": ") {
			t.Fatalf("warning not attributed to a source: %q", s)
		}
	}
}

func TestSearchMultiTermAndPrefix(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	runOK(t, "import", "adapter", repoPath(t, "testdata/adapters/discrawl.fixture.jsonl"), "--source", "discrawl")
	runOK(t, "import", "adapter", repoPath(t, "testdata/adapters/agent-session.fixture.jsonl"), "--source", "codex")

	// Two terms that both occur in the corpus but not as a contiguous phrase.
	// The old single-phrase builder required adjacency; AND semantics should
	// find the item that contains both words anywhere.
	multi := runJSON(t, "search", "contract adapter", "--json")
	if len(multi["results"].([]any)) == 0 {
		t.Fatalf("multi-term AND search returned nothing: %v", multi)
	}

	// Prefix match: "adapt*" should match "adapter"/"adapters".
	prefix := runJSON(t, "search", "adapt*", "--json")
	if len(prefix["results"].([]any)) == 0 {
		t.Fatalf("prefix search returned nothing: %v", prefix)
	}

	// Bare FTS operators and punctuation must stay literal, never crash.
	for _, q := range []string{"AND", "OR", "NEAR", "*", `"`, "a AND b"} {
		if code, _, errb := run("search", q, "--json"); code != 0 {
			t.Fatalf("search %q crashed: code=%d err=%s", q, code, errb)
		}
	}
}

func TestSearchPlanBoundsFTSCandidatesBeforeJoins(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	insertSyntheticSearchArchive(t, db, 40)

	opts := SearchOpts{Query: "needle", Limit: 5}
	sqlText, params := buildSearchQuery(opts)
	plan := explainPlan(t, db, sqlText, params...)
	for _, want := range []string{
		"MATERIALIZE fts_candidates",
		"SCAN item_fts",
		"idx_relations_source_item",
		"idx_relations_target_item",
	} {
		if !strings.Contains(plan, want) {
			t.Fatalf("search plan missing %q:\n%s", want, plan)
		}
	}

	results, err := search(db, opts)
	if err != nil {
		t.Fatalf("search: %v", err)
	}
	if len(results) != opts.Limit {
		t.Fatalf("results = %d, want %d: %#v", len(results), opts.Limit, results)
	}
	if results[0].ID != "item-030" {
		t.Fatalf("relation boost/order changed: first result = %s, want item-030", results[0].ID)
	}

	explained, err := explainSearch(db, opts)
	if err != nil {
		t.Fatalf("explainSearch: %v", err)
	}
	if explained["result_count"] != opts.Limit {
		t.Fatalf("explainSearch result_count = %v, want %d", explained["result_count"], opts.Limit)
	}

	bundle, err := evidenceBundle(db, SearchOpts{Query: "needle", Limit: 5, IncludeRelated: true})
	if err != nil {
		t.Fatalf("evidenceBundle: %v", err)
	}
	items := bundle["results"].([]map[string]any)
	if len(items) != opts.Limit {
		t.Fatalf("evidence results = %d, want %d", len(items), opts.Limit)
	}
	if related, ok := items[0]["related"].([]map[string]any); !ok || len(related) == 0 {
		t.Fatalf("first evidence item missing related rows: %#v", items[0])
	}
}

func TestEvalStationTrailCompat(t *testing.T) {
	good := stationTrailCapabilities{Version: "0.1.5", Schema: "miseledger.adapter.v1", Sources: []string{"codex", "claude", "openclaw", "opencode", "hermes"}}
	cases := []struct {
		name    string
		caps    stationTrailCapabilities
		ok      bool
		source  string
		wantErr bool
	}{
		{"old binary tolerated", stationTrailCapabilities{}, false, "codex", false},
		{"compatible", good, true, "codex", false},
		{"opencode compatible", good, true, "opencode", false},
		{"schema mismatch", stationTrailCapabilities{Version: "9", Schema: "other.v2", Sources: []string{"codex"}}, true, "codex", true},
		{"unsupported source", good, true, "aicrawl", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := evalStationTrailCompat(tc.caps, tc.ok, tc.source)
			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr = %v", err, tc.wantErr)
			}
		})
	}
}

func TestMissingExternalToolDiagnostics(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	statusBefore := runJSON(t, "status", "--json")
	itemsBefore := statusBefore["items"].(float64)
	// Empty PATH so LookPath cannot resolve wrapper binaries. miseledger itself
	// runs in-process, so no shell PATH is required for CLI dispatch.
	t.Setenv("PATH", t.TempDir())

	cases := []struct {
		name     string
		args     []string
		tool     string
		context  string
		hintPart string
	}{
		{
			name:     "sourceharvest import",
			args:     []string{"import", "sourceharvest", "markdown", filepath.Join(t.TempDir(), "notes.md"), "--source", "notes"},
			tool:     "sourceharvest",
			context:  "import sourceharvest",
			hintPart: "github.com/escoffier-labs/sourceharvest",
		},
		{
			name:     "sourceharvest dry-run",
			args:     []string{"import", "sourceharvest", "markdown", filepath.Join(t.TempDir(), "notes.md"), "--source", "notes", "--dry-run"},
			tool:     "sourceharvest",
			context:  "import sourceharvest",
			hintPart: "github.com/escoffier-labs/sourceharvest",
		},
		{
			name:     "stationtrail import",
			args:     []string{"import", "stationtrail", "codex", "fixture"},
			tool:     "stationtrail",
			context:  "import stationtrail",
			hintPart: "github.com/escoffier-labs/stationtrail",
		},
		{
			name:     "stationtrail dry-run",
			args:     []string{"import", "stationtrail", "codex", "fixture", "--dry-run"},
			tool:     "stationtrail",
			context:  "import stationtrail",
			hintPart: "github.com/escoffier-labs/stationtrail",
		},
		{
			name:     "crawl discord exporter",
			args:     []string{"crawl", "discord"},
			tool:     "discrawl",
			context:  "crawl discord",
			hintPart: "install discrawl",
		},
		{
			name:     "crawl github dry-run",
			args:     []string{"crawl", "github", "--dry-run"},
			tool:     "gitcrawl",
			context:  "crawl github",
			hintPart: "install gitcrawl",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, stdout, stderr := run(tc.args...)
			if code == 0 {
				t.Fatalf("expected non-zero exit, stdout=%s stderr=%s", stdout, stderr)
			}
			if strings.Contains(stderr, "\n") && strings.Count(strings.TrimSpace(stderr), "\n") > 0 {
				// fatalf prints one diagnostic line (may end with newline)
				lines := strings.Split(strings.TrimSpace(stderr), "\n")
				if len(lines) != 1 {
					t.Fatalf("want one-line diagnostic, got %d lines: %q", len(lines), stderr)
				}
			}
			msg := strings.TrimSpace(stderr)
			for _, want := range []string{tc.tool, "not found on PATH", tc.context, tc.hintPart} {
				if !strings.Contains(msg, want) {
					t.Fatalf("stderr %q missing %q", msg, want)
				}
			}
			status := runJSON(t, "status", "--json")
			if status["items"].(float64) != itemsBefore {
				t.Fatalf("archive mutated: items before=%v after=%v", itemsBefore, status["items"])
			}
		})
	}
}

func TestMissingExternalToolPresentBinaryUnchanged(t *testing.T) {
	// Regression guard: when the binary is on PATH, wrappers still import.
	// Full happy-path coverage lives in TestImportStationTrailWrapper and
	// TestImportSourceHarvestWrapper; this asserts the preflight does not
	// reject a real LookPath hit.
	withTempHome(t)
	runOK(t, "init")
	binDir := t.TempDir()
	fixture := repoPath(t, "testdata/adapters/agent-session.fixture.jsonl")
	script := filepath.Join(binDir, "stationtrail")
	body := "#!/bin/sh\nsummary=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--summary-out' ]; then shift; summary=\"$1\"; fi\n  shift || true\ndone\nif [ -n \"$summary\" ]; then\n  printf '{\"source\":\"codex\",\"records\":2,\"warnings\":[],\"files\":[]}' > \"$summary\"\nfi\ncat " + shellQuote(fixture) + "\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	out := runJSON(t, "import", "stationtrail", "codex", "fixture", "--json")
	if out["inserted_items"].(float64) != 2 {
		t.Fatalf("inserted = %v, want 2 with binary present: %v", out["inserted_items"], out)
	}
}

func TestDoctorWrapperTools(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	binDir := t.TempDir()

	// Create mock binaries for found tools: stationtrail and discrawl
	for _, bin := range []string{"stationtrail", "discrawl"} {
		script := filepath.Join(binDir, bin)
		if err := os.WriteFile(script, []byte("#!/bin/sh\n"), 0o700); err != nil {
			t.Fatal(err)
		}
	}

	// Set PATH to only include binDir, so only stationtrail and discrawl are found
	t.Setenv("PATH", binDir)

	got := runJSON(t, "doctor", "--json")
	if got["ok"] != true {
		t.Fatalf("doctor not ok: %v", got)
	}

	checks := got["checks"].([]any)
	var wrapperToolsCheck map[string]any

	for _, raw := range checks {
		check := raw.(map[string]any)
		if check["name"] == "wrapper_tools" {
			wrapperToolsCheck = check
			break
		}
	}

	if wrapperToolsCheck == nil {
		t.Fatalf("wrapper_tools check not found in doctor output: %v", checks)
	}

	detail := wrapperToolsCheck["detail"].(string)

	// Should find stationtrail and discrawl
	if !strings.Contains(detail, "stationtrail") {
		t.Fatalf("wrapper_tools detail missing stationtrail: %s", detail)
	}
	if !strings.Contains(detail, "discrawl") {
		t.Fatalf("wrapper_tools detail missing discrawl: %s", detail)
	}

	// Should show as missing for other tools
	missingTools := []string{"sourceharvest", "opencode", "gitcrawl", "slacrawl", "graincrawl", "notcrawl", "mailcrawl", "telecrawl"}
	for _, tool := range missingTools {
		if !strings.Contains(detail, tool) {
			t.Fatalf("wrapper_tools detail missing %s: %s", tool, detail)
		}
	}
}

func TestDoctorJSONIncludesStructuredWrapperTools(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	binDir := t.TempDir()
	stationtrail := filepath.Join(binDir, "stationtrail")
	if err := os.WriteFile(stationtrail, []byte("#!/bin/sh\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", binDir)

	got := runJSON(t, "doctor", "--json")
	checks, ok := got["checks"].([]any)
	if !ok || len(checks) == 0 {
		t.Fatalf("doctor checks = %v, want non-empty array", got["checks"])
	}
	for _, raw := range checks {
		check, ok := raw.(map[string]any)
		if !ok {
			t.Fatalf("doctor check = %T, want object", raw)
		}
		for _, field := range []string{"name", "ok", "detail"} {
			if _, exists := check[field]; !exists {
				t.Fatalf("doctor check missing %q: %v", field, check)
			}
		}
	}

	tools, ok := got["wrapper_tools"].([]any)
	if !ok || len(tools) != 10 {
		t.Fatalf("wrapper_tools = %v, want 10 entries", got["wrapper_tools"])
	}
	wantTools := map[string]bool{
		"stationtrail": true, "sourceharvest": true, "opencode": true,
		"discrawl": true, "gitcrawl": true, "slacrawl": true,
		"graincrawl": true, "notcrawl": true, "mailcrawl": true, "telecrawl": true,
	}
	seen := map[string]bool{}
	for _, raw := range tools {
		tool, ok := raw.(map[string]any)
		if !ok {
			t.Fatalf("wrapper tool = %T, want object", raw)
		}
		name, _ := tool["name"].(string)
		if name == "" {
			t.Fatalf("wrapper tool missing name: %v", tool)
		}
		seen[name] = true
		found, _ := tool["found"].(bool)
		if found {
			if path, _ := tool["path"].(string); path != stationtrail {
				t.Fatalf("found stationtrail path = %q, want %q", path, stationtrail)
			}
			if _, exists := tool["hint"]; exists {
				t.Fatalf("found tool includes hint: %v", tool)
			}
			continue
		}
		if _, exists := tool["path"]; exists {
			t.Fatalf("missing tool includes path: %v", tool)
		}
		if hint, _ := tool["hint"].(string); hint == "" {
			t.Fatalf("missing tool has no hint: %v", tool)
		}
	}
	if len(seen) != len(wantTools) {
		t.Fatalf("wrapper_tools missing expected entries: %v", seen)
	}
	for name := range wantTools {
		if !seen[name] {
			t.Fatalf("wrapper_tools missing %q: %v", name, seen)
		}
	}

	code, plain, errText := run("doctor")
	if code != 0 || errText != "" {
		t.Fatalf("plain doctor failed: code=%d err=%q out=%s", code, errText, plain)
	}
	if strings.HasPrefix(plain, "{") || !strings.Contains(plain, "wrapper_tools ok=true") {
		t.Fatalf("plain doctor output changed: %s", plain)
	}
}
