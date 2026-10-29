package app

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestImportAdapterJSONReportsPersistenceFailure(t *testing.T) {
	withTempHome(t)
	db, _, err := openMigrated()
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`create trigger fail_import before insert on items begin select raise(abort, 'injected durable write failure'); end`); err != nil {
		t.Fatal(err)
	}
	var out, errw bytes.Buffer
	code := cmdImportAdapter([]string{repoPath(t, "testdata/adapters/discrawl.fixture.jsonl"), "--json"}, &out, &errw)
	if code == 0 || !strings.Contains(errw.String(), "injected durable write failure") {
		t.Fatalf("import code=%d stderr=%s", code, errw.String())
	}
	var result struct {
		Status   string   `json:"status"`
		Inserted int      `json:"inserted_items"`
		Errors   []string `json:"errors"`
	}
	if err := json.Unmarshal(out.Bytes(), &result); err != nil {
		t.Fatalf("failure output is not JSON: %s: %v", out.String(), err)
	}
	if result.Status != "failed" || result.Inserted != 0 || len(result.Errors) != 1 {
		t.Fatalf("failure summary=%+v", result)
	}
}
