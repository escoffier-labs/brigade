package app

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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

func TestImportAgentTrailWrapper(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	agenttrailDir := t.TempDir()
	fixture := repoPath(t, "testdata/adapters/agent-session.fixture.jsonl")
	script := filepath.Join(agenttrailDir, "agenttrail")
	body := "#!/bin/sh\nsummary=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--summary-out' ]; then shift; summary=\"$1\"; fi\n  shift || true\ndone\nif [ -n \"$summary\" ]; then\n  printf '{\"source\":\"codex\",\"records\":2,\"warnings\":[],\"files\":[{\"path\":\"fixture.jsonl\",\"size\":1,\"mtime\":\"2026-06-03T00:00:00Z\",\"content_hash\":\"sha256:test\",\"records_generated\":2,\"warnings\":0}]}' > \"$summary\"\nfi\ncat " + shellQuote(fixture) + "\n"
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", agenttrailDir+string(os.PathListSeparator)+oldPath)
	out := runJSON(t, "import", "agenttrail", "codex", "fixture", "--json")
	if out["inserted_items"].(float64) != 2 {
		t.Fatalf("inserted = %v, want 2: %v", out["inserted_items"], out)
	}
	scans := runJSON(t, "scans", "list", "--source", "codex", "--json")
	if len(scans["scans"].([]any)) != 1 {
		t.Fatalf("expected scan manifest from agenttrail summary: %v", scans)
	}
}

func TestImportAgentTrailWrapperForSupportedSources(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	agenttrailDir := t.TempDir()
	script := filepath.Join(agenttrailDir, "agenttrail")
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
printf '{"schema":"logspine.adapter.v1","source":{"kind":"%s","name":"AgentTrail Fixture"},"collection":{"external_id":"%s:session:fixture","kind":"agent_session","name":"fixture"},"item":{"external_id":"%s:item:fixture","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"%s","tags":["agent-session","%s"]},"actor":{"external_id":"%s:%s:fixture","type":"%s","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:test","path":"%s.fixture","ordinal":1}}\n' "$source" "$source" "$source" "$text" "$source" "$source" "$actor" "$actor" "$source"
`
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", agenttrailDir+string(os.PathListSeparator)+oldPath)
	for _, source := range []string{"codex", "claude", "openclaw", "opencode", "hermes"} {
		out := runJSON(t, "import", "agenttrail", source, "fixture", "--json")
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
printf '{"schema":"logspine.adapter.v1","source":{"kind":"notes","name":"SourceHarvest Fixture"},"collection":{"external_id":"notes:local","kind":"notes","name":"notes"},"item":{"external_id":"notes:item:fixture","kind":"note","created_at":"2026-06-03T00:00:00Z","text":"SourceHarvest wrapper fixture evidence","tags":["notes"]},"actor":{"external_id":"notes:system:fixture","type":"system","name":"fixture"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","hash":"sha256:test","path":"notes.fixture","ordinal":1}}\n'
`
	if err := os.WriteFile(script, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	oldPath := os.Getenv("PATH")
	t.Setenv("PATH", sourceharvestDir+string(os.PathListSeparator)+oldPath)
	dry := runJSON(t, "import", "sourceharvest", "markdown", "fixture", "--source", "notes", "--collection", "notes:local", "--dry-run", "--json")
	if dry["generated_records"].(float64) != 1 {
		t.Fatalf("dry-run generated = %v, want 1: %v", dry["generated_records"], dry)
	}
	out := runJSON(t, "import", "sourceharvest", "markdown", "fixture", "--source", "notes", "--collection", "notes:local", "--json")
	if out["inserted_items"].(float64) != 1 {
		t.Fatalf("inserted = %v, want 1: %v", out["inserted_items"], out)
	}
	search := runJSON(t, "search", "SourceHarvest wrapper", "--json")
	if len(search["results"].([]any)) != 1 {
		t.Fatalf("sourceharvest wrapper search failed: %v", search)
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
	for _, item := range discovered {
		if item["source_kind"] == "hermes" {
			foundHermes = true
			if item["status"] != "agenttrail-supported" || item["count"].(float64) != 1 {
				t.Fatalf("unexpected Hermes discovery row: %v", item)
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
}

func TestNativeAdaptersImportAndEvidence(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	crawler := repoPath(t, "testdata/adapters/discrawl.fixture.jsonl")
	codexFixture := repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl")
	claudeFixture := repoPath(t, "testdata/harnesses/claude-project.fixture.jsonl")
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
	if !strings.Contains(adapterJSONL, "exec_command") || !strings.Contains(adapterJSONL, "encrypted_content present") {
		t.Fatalf("codex adapter did not include real response_item shapes: %s", adapterJSONL)
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

	before := runJSON(t, "status", "--json")
	runOK(t, "import", "codex", codexFixture, "--json")
	runOK(t, "import", "openclaw", openclawFixture, "--json")
	runOK(t, "import", "claude", claudeFixture, "--json")
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
	projectEvidence := runJSON(t, "evidence", "Claude native import", "--project", "logspine", "--json")
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

func TestImportDiscoveredAndWatchOnce(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	root := filepath.Join(os.Getenv("HOME"), ".codex", "sessions", "2026", "06", "03")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	copyFixture(t, repoPath(t, "testdata/harnesses/codex-session.fixture.jsonl"), filepath.Join(root, "codex.jsonl"))
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

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'"
}
