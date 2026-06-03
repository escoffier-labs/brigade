package app

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
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
	if err := os.WriteFile(bad, []byte(`{"schema":"logspine.adapter.v1","source":{"kind":"discrawl"},"item":{"external_id":"x","kind":"message"}}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	out := runJSON(t, "import", "adapter", bad, "--source", "discrawl", "--json")
	warnings := out["warnings"].([]any)
	if len(warnings) != 1 || !strings.Contains(warnings[0].(string), "collection.external_id") {
		t.Fatalf("unexpected warnings: %v", out)
	}
}

func TestNativeAdaptersImportAndEvidence(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	crawler := repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")
	codexFixture := repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl")
	openclawFixture := repoPath(t, "testdata/harnesses/openclaw-session.fixture.jsonl")
	trajectoryFixture := repoPath(t, "testdata/harnesses/openclaw-trajectory.fixture.jsonl")
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
		if rec["schema"] != "logspine.adapter.v1" {
			t.Fatalf("adapter schema = %v", rec["schema"])
		}
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
	runOK(t, "import", "openclaw", trajectoryFixture, "--json")

	before := runJSON(t, "status", "--json")
	runOK(t, "import", "codex", codexFixture, "--json")
	runOK(t, "import", "openclaw", openclawFixture, "--json")
	after := runJSON(t, "status", "--json")
	if before["items"] != after["items"] {
		t.Fatalf("reimport changed item count: before=%v after=%v", before["items"], after["items"])
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

	dryRun := runJSON(t, "import", "codex", malformedFixture, "--dry-run", "--json")
	if dryRun["generated_records"].(float64) == 0 {
		t.Fatalf("malformed fixture did not preserve valid records: %v", dryRun)
	}
	if len(dryRun["warnings"].([]any)) == 0 {
		t.Fatalf("malformed fixture produced no warnings: %v", dryRun)
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

func run(args ...string) (int, string, string) {
	var out, errb bytes.Buffer
	code := Run(args, &out, &errb)
	return code, out.String(), errb.String()
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
