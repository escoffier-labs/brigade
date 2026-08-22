package cursor

import (
	"bufio"
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
	_ "modernc.org/sqlite"
)

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func records(t *testing.T, buf *bytes.Buffer) []adapter.Record {
	t.Helper()
	var out []adapter.Record
	scanner := bufio.NewScanner(buf)
	scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var rec adapter.Record
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("bad record: %v\n%s", err, line)
		}
		out = append(out, rec)
	}
	return out
}

func parseRecords(t *testing.T, path string, opts sources.Options) ([]adapter.Record, sources.Result) {
	t.Helper()
	var buf bytes.Buffer
	res, err := Generate(path, opts, &buf)
	if err != nil {
		t.Fatalf("Generate(%s): %v", path, err)
	}
	return records(t, &buf), res
}

func TestGeneratePromptHistoryAndSessions(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "prompt_history.json"),
		`["fix the auth timeout bug","release audit checklist","fix the auth timeout bug"]`)
	writeFile(t, filepath.Join(root, "chats", "abc123", "meta.json"),
		`{"id":"abc123","title":"Auth timeout investigation","createdAt":1718600000000,"workspace":"/home/u/repo"}`)
	writeFile(t, filepath.Join(root, "chats", "abc123", "store.db"), "binary-blob-placeholder")
	// A chat with only a store.db and no meta.json should still surface.
	writeFile(t, filepath.Join(root, "acp-sessions", "def456", "store.db"), "binary-blob-placeholder")

	var buf bytes.Buffer
	result, err := Generate(root, sources.Options{}, &buf)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	recs := records(t, &buf)

	var prompts, sessions int
	var sawDedup = map[string]int{}
	for _, r := range recs {
		switch r.Collection.Kind {
		case "prompt_history":
			prompts++
			sawDedup[r.Item.ExternalID]++
		case "agent_session":
			sessions++
		default:
			t.Fatalf("unexpected collection kind %q", r.Collection.Kind)
		}
	}
	// Two distinct prompts emitted (the duplicate shares an external id).
	if prompts != 3 {
		t.Fatalf("expected 3 prompt items emitted, got %d", prompts)
	}
	for id, n := range sawDedup {
		if strings.Contains(id, sources.StableID("fix the auth timeout bug")) && n != 2 {
			t.Fatalf("expected duplicate prompt to share external id, got %d", n)
		}
	}
	if sessions != 2 {
		t.Fatalf("expected 2 sessions (meta.json + store-only), got %d", sessions)
	}
	if result.Records != prompts+sessions {
		t.Fatalf("result.Records=%d but counted %d", result.Records, prompts+sessions)
	}
	// The store-only chat must produce a warning.
	if len(result.Warnings) == 0 {
		t.Fatal("expected a warning for the store-only chat")
	}
	// The titled session should carry its title as searchable text.
	found := false
	for _, r := range recs {
		if r.Collection.Kind == "agent_session" && strings.Contains(r.Item.Text, "Auth timeout investigation") {
			found = true
			if r.Raw.Path == "" {
				t.Fatal("session record missing raw path for resume")
			}
		}
	}
	if !found {
		t.Fatal("titled session text not found")
	}
}

func TestGenerateFixtureEmitsPromptHistoryAndSessions(t *testing.T) {
	recs, res := parseRecords(t, "../../../testdata/harnesses/cursor-config.fixture", sources.Options{})

	collections := map[string]bool{}
	var promptItems, sessionItems int
	var sawPromptSnippet, sawSessionSnippet bool
	for _, rec := range recs {
		if rec.Source.Kind != "cursor" {
			t.Fatalf("source kind = %q, want cursor", rec.Source.Kind)
		}
		collections[rec.Collection.ExternalID] = true
		switch rec.Collection.Kind {
		case "prompt_history":
			promptItems++
			if strings.Contains(rec.Item.Text, "Summarize demo-project release notes") {
				sawPromptSnippet = true
			}
		case "agent_session":
			sessionItems++
			if strings.Contains(rec.Item.Text, "demo-project import plan") {
				sawSessionSnippet = true
			}
		default:
			t.Fatalf("unexpected collection kind %q", rec.Collection.Kind)
		}
	}
	if len(collections) != 3 {
		t.Fatalf("collection count = %d, want 3", len(collections))
	}
	if promptItems != 3 {
		t.Fatalf("prompt item count = %d, want 3", promptItems)
	}
	if sessionItems != 2 {
		t.Fatalf("session item count = %d, want 2", sessionItems)
	}
	if res.Records != promptItems+sessionItems {
		t.Fatalf("result.Records=%d, decoded=%d", res.Records, promptItems+sessionItems)
	}
	if !sawPromptSnippet {
		t.Fatal("prompt fixture snippet did not round-trip")
	}
	if !sawSessionSnippet {
		t.Fatal("session fixture snippet did not round-trip")
	}
}

func TestGenerateSinceFilters(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "chats", "old", "meta.json"),
		`{"id":"old","title":"old chat","createdAt":"2020-01-01T00:00:00Z"}`)
	writeFile(t, filepath.Join(root, "chats", "new", "meta.json"),
		`{"id":"new","title":"new chat","createdAt":"2099-01-01T00:00:00Z"}`)

	var buf bytes.Buffer
	if _, err := Generate(root, sources.Options{Since: "2050-01-01"}, &buf); err != nil {
		t.Fatalf("Generate: %v", err)
	}
	recs := records(t, &buf)
	if len(recs) != 1 || !strings.Contains(recs[0].Item.Text, "new chat") {
		t.Fatalf("since filter failed, got %d records", len(recs))
	}
}

func TestGenerateMissingRoot(t *testing.T) {
	if _, err := Generate(filepath.Join(t.TempDir(), "nope"), sources.Options{}, &bytes.Buffer{}); err == nil {
		t.Fatal("expected error for missing root")
	}
}

func TestDefaultRootRespectsXDG(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", "/tmp/xdg")
	if got := DefaultRoot(); got != filepath.Join("/tmp/xdg", "Cursor", "User") {
		t.Fatalf("DefaultRoot=%q", got)
	}
}

func buildConversationFixture(t *testing.T) string {
	t.Helper()
	sqlText, err := os.ReadFile("../../../testdata/harnesses/cursor-conversations.fixture.sql")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	dbPath := filepath.Join(root, "globalStorage", "conversation-search.db")
	if err := os.MkdirAll(filepath.Dir(dbPath), 0o700); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(string(sqlText)); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	return root
}

func TestGenerateConversationSearchDatabase(t *testing.T) {
	root := buildConversationFixture(t)
	recs, res := parseRecords(t, root, sources.Options{})
	if res.Records != 2 || len(recs) != 2 {
		t.Fatalf("records=%d decoded=%d", res.Records, len(recs))
	}
	var text string
	for _, rec := range recs {
		text += rec.Item.Text
		if rec.Collection.Kind != "agent_session" {
			t.Fatalf("kind=%q", rec.Collection.Kind)
		}
		if rec.Raw.Path != filepath.Join(root, "globalStorage", "conversation-search.db") {
			t.Fatalf("raw=%q", rec.Raw.Path)
		}
	}
	if !strings.Contains(text, "synthetic migration checklist") {
		t.Fatal("conversation body was not indexed")
	}
}

func TestGenerateConversationSearchDirectPathLimitAndSince(t *testing.T) {
	root := buildConversationFixture(t)
	dbPath := filepath.Join(root, "globalStorage", "conversation-search.db")
	recs, res := parseRecords(t, dbPath, sources.Options{Limit: 1, Since: "2026-07-15T12:30:00Z"})
	if res.Records != 1 || len(recs) != 1 {
		t.Fatalf("records=%d decoded=%d", res.Records, len(recs))
	}
	if !strings.Contains(recs[0].Item.Text, "crawler review") {
		t.Fatalf("unexpected record text=%q", recs[0].Item.Text)
	}
}

func TestCountSessionsUsesCurrentDatabaseWithoutBodies(t *testing.T) {
	root := buildConversationFixture(t)
	got, err := CountSessions(root)
	if err != nil {
		t.Fatal(err)
	}
	if got != 2 {
		t.Fatalf("CountSessions=%d want 2", got)
	}
}

func TestGenerateConversationSearchMissingTablesWarns(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "conversation-search.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE placeholder (id INTEGER)`); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	_, res := parseRecords(t, dbPath, sources.Options{})
	if res.Records != 0 || len(res.Warnings) != 1 || !strings.Contains(res.Warnings[0], "required conversation tables") {
		t.Fatalf("result=%+v", res)
	}
}

func TestGenerateConversationSearchReadOnlyAndWALScan(t *testing.T) {
	root := buildConversationFixture(t)
	dbPath := filepath.Join(root, "globalStorage", "conversation-search.db")
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`PRAGMA journal_mode=WAL`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO conversations VALUES (3,'local','','cursor-fixture-003','WAL fixture',1784124000000,0,'fixture-root-c',NULL)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO conversation_fts(rowid,title,body) VALUES (3,'WAL fixture','Cursor WAL scan target')`); err != nil {
		t.Fatal(err)
	}
	var scanned string
	var buf bytes.Buffer
	res, err := Generate(root, sources.Options{AfterFile: func(scan sources.FileScan) error {
		scanned = scan.Path
		return nil
	}}, &buf)
	if err != nil {
		t.Fatal(err)
	}
	if res.Records != 3 || scanned != dbPath+"-wal" {
		t.Fatalf("records=%d scanned=%q", res.Records, scanned)
	}
}

func TestSQLiteOpErrorNamesPathAndStatement(t *testing.T) {
	err := sqliteOpError(
		filepath.Join("User", "globalStorage", "conversation-search.db"),
		conversationSearchSQL,
		errors.New("SQL logic error: out of memory (1)"),
	)
	msg := err.Error()
	for _, want := range []string{
		"conversation-search.db",
		"from conversations c",
		"out of memory",
	} {
		if !strings.Contains(msg, want) {
			t.Fatalf("sqliteOpError=%q missing %q", msg, want)
		}
	}
}

func TestSQLiteFileURIPathWindowsDriveAndRelative(t *testing.T) {
	if got := sqliteFileURIPath(`C:\Users\demo\conversation-search.db`); got != "/C:/Users/demo/conversation-search.db" {
		t.Fatalf("windows drive URI path=%q", got)
	}
	if got := sqliteFileURIPath("User/globalStorage/conversation-search.db"); got != "/User/globalStorage/conversation-search.db" {
		t.Fatalf("relative URI path=%q", got)
	}
	dsn, err := sqliteReadOnlyDSN("conversation-search.db")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(dsn, "file:///") {
		t.Fatalf("dsn=%q is not an absolute file URI", dsn)
	}
	if strings.Contains(dsn, "file://conversation-search.db") {
		t.Fatalf("dsn=%q still has a host component", dsn)
	}
}

func chdir(t *testing.T, dir string) {
	t.Helper()
	cwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(cwd); err != nil {
			t.Errorf("restore cwd: %v", err)
		}
	})
}

func TestGenerateConversationSearchRelativePathImports(t *testing.T) {
	root := buildConversationFixture(t)
	chdir(t, filepath.Dir(root))
	recs, res := parseRecords(t, filepath.Base(root), sources.Options{})
	if res.Records != 2 || len(recs) != 2 {
		t.Fatalf("relative conversation-search import records=%d decoded=%d warnings=%v", res.Records, len(recs), res.Warnings)
	}
}

func TestGenerateConversationSearchZeroByteRelativePathSkips(t *testing.T) {
	dir := t.TempDir()
	chdir(t, dir)
	writeFile(t, filepath.Join("User", "globalStorage", "conversation-search.db"), "")
	writeFile(t, filepath.Join("User", "prompt_history.json"), `["keep importing other cursor surfaces"]`)

	var buf bytes.Buffer
	res, err := Generate("User", sources.Options{}, &buf)
	if err != nil {
		t.Fatalf("0-byte conversation-search.db must not abort import: %v", err)
	}
	if len(res.Warnings) == 0 {
		t.Fatalf("expected a skip warning, got %#v", res)
	}
	joined := strings.Join(res.Warnings, "\n")
	if !strings.Contains(joined, "conversation-search.db") {
		t.Fatalf("warning must name the file: %q", joined)
	}
	recs := records(t, &buf)
	if len(recs) != 1 || !strings.Contains(recs[0].Item.Text, "keep importing other cursor surfaces") {
		t.Fatalf("other cursor surfaces should still import, got %+v", recs)
	}
}

func TestGenerateConversationSearchUnreadableFileSkips(t *testing.T) {
	root := t.TempDir()
	dbPath := filepath.Join(root, "globalStorage", "conversation-search.db")
	writeFile(t, dbPath, "this is not a sqlite database")
	writeFile(t, filepath.Join(root, "prompt_history.json"), `["still index prompt history"]`)

	recs, res := parseRecords(t, root, sources.Options{})
	if len(res.Warnings) == 0 {
		t.Fatal("expected a warning for the unreadable conversation-search.db")
	}
	joined := strings.Join(res.Warnings, "\n")
	if !strings.Contains(joined, dbPath) {
		t.Fatalf("warning must name the file: %q", joined)
	}
	if !strings.Contains(joined, "open") && !strings.Contains(joined, "select") {
		t.Fatalf("warning must name the failed statement: %q", joined)
	}
	if len(recs) != 1 || !strings.Contains(recs[0].Item.Text, "still index prompt history") {
		t.Fatalf("prompt history should still import, got %+v", recs)
	}
}

func TestCountSessionsUnreadableDatabaseContinues(t *testing.T) {
	root := t.TempDir()
	writeFile(t, filepath.Join(root, "globalStorage", "conversation-search.db"), "not-a-database")
	writeFile(t, filepath.Join(root, "prompt_history.json"), `["x"]`)
	got, err := CountSessions(root)
	if err != nil {
		t.Fatalf("CountSessions: %v", err)
	}
	if got != 1 {
		t.Fatalf("CountSessions=%d want 1 (prompt history only)", got)
	}
}

func TestDefaultRootForPlatform(t *testing.T) {
	cases := []struct {
		goos, home, xdg, appData, want string
	}{
		{"linux", "/users/demo", "/config", "", "/config/Cursor/User"},
		{"darwin", "/users/demo", "", "", "/users/demo/Library/Application Support/Cursor/User"},
		{"windows", `C:\\Users\\demo`, "", `C:\\Data`, `C:\\Data/Cursor/User`},
	}
	for _, tc := range cases {
		if got := defaultRoot(tc.goos, tc.home, tc.xdg, tc.appData); got != filepath.Clean(tc.want) {
			t.Fatalf("defaultRoot(%s)=%q want %q", tc.goos, got, filepath.Clean(tc.want))
		}
	}
}
