package app

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/ingest"
)

// E2 (#843): generic search/show/evidence/doctor read-path contract after E1.
func TestE2OneLiveVersionSearchOmitsSupersededAndTombstones(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	cardID := "card-e2live00-1111-4222-8333-444444444444"
	path := filepath.Join(cards, "edit.md")
	oldUnique := "UNIQUE_OLD_TOKEN_E2_ABSENT_AFTER_EDIT"
	newUnique := "UNIQUE_NEW_TOKEN_E2_ONLY_LIVE"
	if err := os.WriteFile(path, []byte("---\nid: "+cardID+"\ntopic: e2\n---\n\n# Body\n\n"+oldUnique+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")
	if err := os.WriteFile(path, []byte("---\nid: "+cardID+"\ntopic: e2\n---\n\n# Body\n\n"+newUnique+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")

	oldSearch := runJSON(t, "search", oldUnique, "--source", ingest.MemorySourceKind, "--json")
	if len(oldSearch["results"].([]any)) != 0 {
		t.Fatalf("old unique text must leave default search after edit: %v", oldSearch)
	}
	newSearch := runJSON(t, "search", newUnique, "--source", ingest.MemorySourceKind, "--json")
	results := newSearch["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("expected exactly one live search hit, got %v", newSearch)
	}

	db := openTestDB(t)
	defer db.Close()
	var ftsOld int
	if err := db.QueryRow(`select count(*) from item_fts where item_fts match ?`, `"`+oldUnique+`"`).Scan(&ftsOld); err != nil {
		t.Fatal(err)
	}
	if ftsOld != 0 {
		t.Fatalf("old unique text still present in FTS rows: count=%d", ftsOld)
	}
	var live, tombstoned int
	if err := db.QueryRow(`
select
  sum(case when i.tombstoned_at is null then 1 else 0 end),
  sum(case when i.tombstoned_at is not null then 1 else 0 end)
from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ? and i.external_id = ?`,
		ingest.MemorySourceKind, testMemoryNamespace, cardID).Scan(&live, &tombstoned); err != nil {
		t.Fatal(err)
	}
	if live != 1 || tombstoned < 1 {
		t.Fatalf("expected one live + superseded tombstone, live=%d tombstoned=%d", live, tombstoned)
	}

	// Completed removal: deleted card must not appear in search and doctor must
	// not flag the intentional tombstone as missing FTS.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")
	removedSearch := runJSON(t, "search", newUnique, "--source", ingest.MemorySourceKind, "--json")
	if len(removedSearch["results"].([]any)) != 0 {
		t.Fatalf("completed-removal tombstone must not appear in search: %v", removedSearch)
	}
	evidence := runJSON(t, "evidence", newUnique, "--source", ingest.MemorySourceKind, "--json")
	if len(evidence["results"].([]any)) != 0 {
		t.Fatalf("completed-removal tombstone must not appear in evidence: %v", evidence)
	}

	code, out, errb := run("doctor", "--archive", "--json")
	_ = code
	_ = errb
	var doctor map[string]any
	if err := json.Unmarshal([]byte(out), &doctor); err != nil {
		t.Fatalf("doctor json: %v\n%s", err, out)
	}
	checks := doctor["checks"].([]any)
	foundFTS := false
	for _, raw := range checks {
		check := raw.(map[string]any)
		if check["name"] == "archive_items_missing_fts" {
			foundFTS = true
			if check["ok"] != true {
				t.Fatalf("intentional tombstones must be excluded from archive_items_missing_fts: %v", check)
			}
		}
	}
	if !foundFTS {
		t.Fatalf("archive_items_missing_fts check missing: %v", checks)
	}
}

func TestE2ShowQualifiedRelationsLiveStateAndSafeDefaultBody(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := copyEngineMemoryFixtures(t)
	receiptFixture := repoPath(t, "testdata/adapters/memory/brigade-receipt.fixture.jsonl")
	runOK(t, "import", "adapter", receiptFixture, "--json")
	crawl := runJSON(t, "crawl", "memory", ws, "--json")
	if crawl["status"] != "completed" {
		t.Fatalf("crawl = %v", crawl)
	}

	// Injection-like card: default show omits body/raw; opt-in restores them.
	injSearch := runJSON(t, "search", "IGNORE ALL PREVIOUS", "--source", ingest.MemorySourceKind, "--kind", "memory_card", "--json")
	if len(injSearch["results"].([]any)) == 0 {
		t.Fatalf("injection fixture should still be searchable by metadata/text index: %v", injSearch)
	}
	injID := injSearch["results"].([]any)[0].(map[string]any)["id"].(string)
	safe := runJSON(t, "show", injID, "--json")
	if safe["untrusted_body_omitted"] != true {
		t.Fatalf("default show must omit quarantined injection-pending body: %v", safe)
	}
	if _, ok := safe["text"]; ok {
		t.Fatalf("default show leaked text: %v", safe)
	}
	if _, ok := safe["raw"]; ok {
		t.Fatalf("default show leaked raw: %v", safe)
	}
	if safe["live"] != true || safe["tombstoned"] != false {
		t.Fatalf("live/tombstone state missing: %v", safe)
	}
	revealed := runJSON(t, "show", injID, "--json", "--include-untrusted-body")
	if revealed["untrusted_body_omitted"] == true {
		t.Fatalf("opt-in must reveal body: %v", revealed)
	}
	text, _ := revealed["text"].(string)
	if !strings.Contains(text, "IGNORE ALL PREVIOUS") {
		t.Fatalf("opt-in text missing injection fixture body: %v", revealed)
	}

	// Unresolved qualified relation target fields on show --json.
	unresolvedSearch := runJSON(t, "search", "Points at a missing receipt", "--source", ingest.MemorySourceKind, "--json")
	if len(unresolvedSearch["results"].([]any)) == 0 {
		t.Fatalf("unresolved relation card missing: %v", unresolvedSearch)
	}
	unresolvedID := unresolvedSearch["results"].([]any)[0].(map[string]any)["id"].(string)
	unresolvedShow := runJSON(t, "show", unresolvedID, "--json")
	rels := unresolvedShow["relations"].([]any)
	if len(rels) == 0 {
		t.Fatalf("expected outbound relation: %v", unresolvedShow)
	}
	foundUnresolved := false
	for _, raw := range rels {
		rel := raw.(map[string]any)
		if rel["target_external_id"] == "receipt:does-not-exist" {
			foundUnresolved = true
			if rel["target_source_kind"] != "brigade" {
				t.Fatalf("target_source_kind = %v", rel["target_source_kind"])
			}
			if rel["target_collection_external_id"] != "brigade:receipts" {
				t.Fatalf("target_collection_external_id = %v", rel["target_collection_external_id"])
			}
			if rel["target_item_id"] != nil && rel["target_item_id"] != "" {
				t.Fatalf("unresolved target_item_id must be null/empty: %v", rel)
			}
			if rel["target_live"] != nil {
				t.Fatalf("unresolved target_live must be null: %v", rel)
			}
		}
	}
	if !foundUnresolved {
		t.Fatalf("qualified unresolved relation missing: %v", unresolvedShow)
	}

	// Resolved qualified cross-source relation (card -> receipt).
	db := openTestDB(t)
	defer db.Close()
	var cardWithReceipt string
	if err := db.QueryRow(`
select i.id from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
join relations r on r.source_item_id = i.id
where s.kind = ? and c.external_id = ? and i.kind = 'memory_card'
  and i.tombstoned_at is null and r.target_item_id is not null
  and coalesce(r.target_source_kind,'') != ''
limit 1`, ingest.MemorySourceKind, testMemoryNamespace).Scan(&cardWithReceipt); err != nil {
		t.Fatalf("need a live memory card with resolved qualified relation: %v", err)
	}
	resolvedShow := runJSON(t, "show", cardWithReceipt, "--json")
	foundResolved := false
	for _, raw := range resolvedShow["relations"].([]any) {
		rel := raw.(map[string]any)
		if rel["target_item_id"] == nil || rel["target_item_id"] == "" {
			continue
		}
		if rel["target_source_kind"] == nil || rel["target_source_kind"] == "" {
			t.Fatalf("resolved relation missing target_source_kind: %v", rel)
		}
		if rel["target_collection_external_id"] == nil || rel["target_collection_external_id"] == "" {
			t.Fatalf("resolved relation missing target_collection_external_id: %v", rel)
		}
		if rel["target_external_id"] == nil || rel["target_external_id"] == "" {
			t.Fatalf("resolved relation missing target_external_id: %v", rel)
		}
		if rel["target_live"] != true {
			t.Fatalf("resolved live target expected target_live=true: %v", rel)
		}
		foundResolved = true
		break
	}
	if !foundResolved {
		t.Fatalf("qualified resolved relation missing: %v", resolvedShow)
	}

	// Tombstoned target: show of a relation whose target was removed.
	targetPath := filepath.Join(ws, "memory", "cards", "valid-explicit.md")
	targetBytes, err := os.ReadFile(targetPath)
	if err != nil {
		t.Fatal(err)
	}
	var targetID string
	for _, line := range strings.Split(string(targetBytes), "\n") {
		if strings.HasPrefix(line, "id:") {
			targetID = strings.TrimSpace(strings.TrimPrefix(line, "id:"))
			break
		}
	}
	if targetID == "" {
		t.Fatal("fixture card id missing")
	}
	support := map[string]any{
		"schema": "miseledger.adapter.v1",
		"source": map[string]any{"kind": "brigade", "name": "Brigade"},
		"collection": map[string]any{
			"external_id": "brigade:receipts",
			"kind":        "brigade_receipt",
			"name":        "receipts",
		},
		"item": map[string]any{
			"external_id": "receipt:e2-tombstone-probe",
			"kind":        "receipt",
			"created_at":  "2026-01-01T00:00:00Z",
			"text":        "e2 tombstone probe",
		},
		"relations": []map[string]any{{
			"type": "supports",
			"target": map[string]any{
				"source":     ingest.MemorySourceKind,
				"collection": testMemoryNamespace,
				"external_id": targetID,
			},
		}},
		"raw": map[string]any{"format": "json", "path": "e2.json", "ordinal": 1},
	}
	supportBytes, _ := json.Marshal(support)
	supportPath := filepath.Join(t.TempDir(), "support.jsonl")
	if err := os.WriteFile(supportPath, append(supportBytes, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", supportPath, "--json")
	if err := os.Remove(targetPath); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")

	probeSearch := runJSON(t, "search", "e2 tombstone probe", "--json")
	if len(probeSearch["results"].([]any)) == 0 {
		t.Fatalf("probe receipt missing: %v", probeSearch)
	}
	probeID := probeSearch["results"].([]any)[0].(map[string]any)["id"].(string)
	probeShow := runJSON(t, "show", probeID, "--json")
	foundTombstonedTarget := false
	for _, raw := range probeShow["relations"].([]any) {
		rel := raw.(map[string]any)
		if rel["target_external_id"] != targetID {
			continue
		}
		foundTombstonedTarget = true
		if rel["target_source_kind"] != ingest.MemorySourceKind {
			t.Fatalf("tombstoned target_source_kind = %v", rel["target_source_kind"])
		}
		if rel["target_collection_external_id"] != testMemoryNamespace {
			t.Fatalf("tombstoned target_collection_external_id = %v", rel["target_collection_external_id"])
		}
		// Completed removal clears target_item_id; qualified identity remains.
		if rel["target_item_id"] != nil && rel["target_item_id"] != "" {
			t.Fatalf("tombstoned target should be unresolved after removal: %v", rel)
		}
	}
	if !foundTombstonedTarget {
		t.Fatalf("expected qualified tombstoned/unresolved target on probe: %v", probeShow)
	}
}

func TestE2ExportOmitsSupersededAndTombstonedBodies(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	cardID := "card-e2export-1111-4222-8333-444444444444"
	path := filepath.Join(cards, "export.md")
	oldUnique := "EXPORT_OLD_BODY_TOKEN_E2"
	newUnique := "EXPORT_NEW_BODY_TOKEN_E2"
	if err := os.WriteFile(path, []byte("---\nid: "+cardID+"\ntopic: export\n---\n\n"+oldUnique+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")
	if err := os.WriteFile(path, []byte("---\nid: "+cardID+"\ntopic: export\n---\n\n"+newUnique+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")

	outDir := filepath.Join(t.TempDir(), "export")
	runJSON(t, "export", "markdown", "--out", outDir)
	exported := readExportTree(t, outDir)
	if strings.Contains(exported, oldUnique) {
		t.Fatalf("markdown export leaked superseded body:\n%s", exported)
	}
	if !strings.Contains(exported, newUnique) {
		t.Fatalf("markdown export missing live body:\n%s", exported)
	}
	managedBefore := managedExportMarkdownBasenames(t, outDir)
	if len(managedBefore) == 0 {
		t.Fatal("expected at least one managed markdown file after live export")
	}
	userOwned := filepath.Join(outDir, "user-owned-notes.txt")
	if err := os.WriteFile(userOwned, []byte("keep me\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	runOK(t, "crawl", "memory", ws, "--json")
	// Reuse the original destination: managed markdown must be reconciled away
	// after the last live item is tombstoned; non-managed files stay put.
	runJSON(t, "export", "markdown", "--out", outDir)
	exported2 := readExportTree(t, outDir)
	if strings.Contains(exported2, newUnique) || strings.Contains(exported2, oldUnique) {
		t.Fatalf("completed-removal tombstone leaked into reused export out:\n%s", exported2)
	}
	for _, name := range managedBefore {
		if _, err := os.Stat(filepath.Join(outDir, name)); !os.IsNotExist(err) {
			t.Fatalf("managed export file %q still present after empty reconcile: err=%v", name, err)
		}
	}
	if data, err := os.ReadFile(userOwned); err != nil || string(data) != "keep me\n" {
		t.Fatalf("non-managed file was disturbed: err=%v data=%q", err, data)
	}
}

func TestE2SearchKeepsLiveWhenFTSCandidatePoolExceedsCap(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	db := openTestDB(t)
	defer db.Close()

	token := "E2_FTS_POOL_TOKEN_OVER_CAP"
	seedPreE2OverCapStaleFTSPool(t, db, token, searchCandidateLimit(0)+1)

	results, err := search(db, SearchOpts{Query: token, Limit: 20})
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 1 {
		t.Fatalf("live hit must survive >candidate-cap stale FTS rows, got %#v", results)
	}
	if results[0].ID != "item-e2-pool-live" {
		t.Fatalf("expected live item id, got %#v", results)
	}
}

func TestE2SessionsAndRelatedFilterDuplicateLiveAndTombstones(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	db := openTestDB(t)
	seedPreE2DuplicateSessionArchive(t, db)
	db.Close()

	listed := runJSON(t, "sessions", "list", "--source", "codex", "--json")
	sessions := listed["sessions"].([]any)
	if len(sessions) != 1 {
		t.Fatalf("sessions list = %v", listed)
	}
	row := sessions[0].(map[string]any)
	// Two distinct live external_ids (msg:e2 latest + msg:e2-peer); superseded
	// duplicate and tombstone must not inflate the count.
	if int(row["item_count"].(float64)) != 2 {
		t.Fatalf("session item_count must count only latest-live versions, got %v", row)
	}
	preview, _ := row["preview"].(string)
	if strings.Contains(preview, "SESSION_OLD_UNIQUE_E2") || strings.Contains(preview, "SESSION_TOMB_UNIQUE_E2") {
		t.Fatalf("session preview leaked non-live text: %q", preview)
	}
	if !strings.Contains(preview, "SESSION_NEW_UNIQUE_E2") {
		t.Fatalf("session preview missing live text: %q", preview)
	}

	searched := runJSON(t, "sessions", "search", "SESSION_OLD_UNIQUE_E2", "--source", "codex", "--json")
	if len(searched["sessions"].([]any)) != 0 {
		t.Fatalf("sessions search must not hit superseded duplicate live row: %v", searched)
	}
	tombSearch := runJSON(t, "sessions", "search", "SESSION_TOMB_UNIQUE_E2", "--source", "codex", "--json")
	if len(tombSearch["sessions"].([]any)) != 0 {
		t.Fatalf("sessions search must not hit tombstone: %v", tombSearch)
	}
	searchedNew := runJSON(t, "sessions", "search", "SESSION_NEW_UNIQUE_E2", "--source", "codex", "--json")
	if len(searchedNew["sessions"].([]any)) != 1 {
		t.Fatalf("sessions search missing live row: %v", searchedNew)
	}
	liveSession := searchedNew["sessions"].([]any)[0].(map[string]any)
	if int(liveSession["item_count"].(float64)) != 2 {
		t.Fatalf("search stats item_count=%v want 2", liveSession["item_count"])
	}

	db = openTestDB(t)
	defer db.Close()
	items, err := sessionItems(db, "session:e2-dup", "codex", 50)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 2 {
		t.Fatalf("transcript must expose only latest-live items, got %#v", items)
	}
	joined := ""
	for _, item := range items {
		joined += fmt.Sprint(item["text"]) + "\n"
	}
	if strings.Contains(joined, "SESSION_OLD_UNIQUE_E2") || strings.Contains(joined, "SESSION_TOMB_UNIQUE_E2") {
		t.Fatalf("transcript leaked non-live text: %s", joined)
	}
	if !strings.Contains(joined, "SESSION_NEW_UNIQUE_E2") || !strings.Contains(joined, "SESSION_PEER_UNIQUE_E2") {
		t.Fatalf("transcript missing live texts: %s", joined)
	}
	count, first, last, err := sessionStats(db, "collection-e2-dup")
	if err != nil {
		t.Fatal(err)
	}
	if count != 2 || first == "" || last == "" {
		t.Fatalf("sessionStats=%d %q %q", count, first, last)
	}

	// Related expansion: live root must not pull superseded/tombstoned neighbors.
	related := relatedItems(db, "item-e2-live")
	for _, rel := range related {
		target := fmt.Sprint(rel["target_item_id"])
		if target == "item-e2-old" || target == "item-e2-tomb" {
			t.Fatalf("related expansion re-entered non-live row: %#v", related)
		}
	}
	if len(related) != 1 || fmt.Sprint(related[0]["target_item_id"]) != "item-e2-peer" {
		t.Fatalf("expected only live peer relation, got %#v", related)
	}

	outDir := filepath.Join(t.TempDir(), "export-dup")
	if _, err := exportMarkdown(db, outDir); err != nil {
		t.Fatal(err)
	}
	exported := readExportTree(t, outDir)
	if strings.Contains(exported, "SESSION_OLD_UNIQUE_E2") || strings.Contains(exported, "SESSION_TOMB_UNIQUE_E2") {
		t.Fatalf("export leaked duplicate/tombstone session text:\n%s", exported)
	}
	if !strings.Contains(exported, "SESSION_NEW_UNIQUE_E2") {
		t.Fatalf("export missing live session text:\n%s", exported)
	}
}

func readExportTree(t *testing.T, dir string) string {
	t.Helper()
	var b strings.Builder
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		data, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		b.Write(data)
		b.WriteByte('\n')
	}
	return b.String()
}

func managedExportMarkdownBasenames(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range entries {
		name := e.Name()
		if isManagedExportBasename(name) {
			names = append(names, name)
		}
	}
	return names
}

// seedPreE2OverCapStaleFTSPool builds >candidateCap superseded non-tombstoned
// FTS hits that would fill the materialize LIMIT before a matching live row if
// latest-live filtering ran only after candidate selection.
func seedPreE2OverCapStaleFTSPool(t *testing.T, db *sql.DB, token string, staleCount int) {
	t.Helper()
	if staleCount < 1 {
		t.Fatalf("staleCount must be positive, got %d", staleCount)
	}
	stmts := []string{
		`insert into sources(id, kind, name, version, created_at, updated_at) values('source-e2-pool','codex','Codex','1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
		`insert into collections(id, source_id, external_id, kind, name, created_at, updated_at) values('collection-e2-pool','source-e2-pool','session:e2-pool','agent_session','pool','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
		`insert into actors(id, source_id, external_id, type, name) values('actor-e2-pool','source-e2-pool','actor:e2-pool','human','Human')`,
	}
	for _, stmt := range stmts {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("seed header failed on %s: %v", stmt, err)
		}
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback() }()

	itemStmt, err := tx.Prepare(`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq, tombstoned_at)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,null)`)
	if err != nil {
		t.Fatal(err)
	}
	defer itemStmt.Close()
	ftsStmt, err := tx.Prepare(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`)
	if err != nil {
		t.Fatal(err)
	}
	defer ftsStmt.Close()

	for i := 0; i < staleCount; i++ {
		ext := fmt.Sprintf("msg:e2-pool-%04d", i)
		staleID := fmt.Sprintf("item-e2-pool-stale-%04d", i)
		liveID := fmt.Sprintf("item-e2-pool-curr-%04d", i)
		// Lexicographically early stale ids + identical bm25 text fill the
		// ordered candidate LIMIT ahead of the later live hit when the live
		// predicate is applied only after materialize.
		if _, err := itemStmt.Exec(staleID, "source-e2-pool", "collection-e2-pool", "actor-e2-pool", ext, "message",
			"2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", token, fmt.Sprintf("sha256:stale-%04d", i), "{}", "{}", i*2+1); err != nil {
			t.Fatalf("insert stale item %d: %v", i, err)
		}
		if _, err := ftsStmt.Exec(staleID, "codex", "agent_session", "message", "human", token); err != nil {
			t.Fatalf("insert stale fts %d: %v", i, err)
		}
		currText := fmt.Sprintf("current body without shared token %04d", i)
		if _, err := itemStmt.Exec(liveID, "source-e2-pool", "collection-e2-pool", "actor-e2-pool", ext, "message",
			"2026-01-01T00:00:01Z", "2026-01-01T00:00:01Z", currText, fmt.Sprintf("sha256:curr-%04d", i), "{}", "{}", i*2+2); err != nil {
			t.Fatalf("insert current item %d: %v", i, err)
		}
		if _, err := ftsStmt.Exec(liveID, "codex", "agent_session", "message", "human", currText); err != nil {
			t.Fatalf("insert current fts %d: %v", i, err)
		}
	}

	liveExt := "msg:e2-pool-live"
	liveSeq := staleCount*2 + 10
	if _, err := itemStmt.Exec("item-e2-pool-live", "source-e2-pool", "collection-e2-pool", "actor-e2-pool", liveExt, "message",
		"2026-01-01T00:00:02Z", "2026-01-01T00:00:02Z", token, "sha256:pool-live", "{}", "{}", liveSeq); err != nil {
		t.Fatalf("insert live item: %v", err)
	}
	if _, err := ftsStmt.Exec("item-e2-pool-live", "codex", "agent_session", "message", "human", token); err != nil {
		t.Fatalf("insert live fts: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}

// seedPreE2DuplicateSessionArchive writes a pre-E2 shape: two non-tombstoned
// content-hash versions for one external_id plus an intentional tombstone, with
// FTS rows still present for the superseded/tombstoned text.
func seedPreE2DuplicateSessionArchive(t *testing.T, db *sql.DB) {
	t.Helper()
	stmts := []string{
		`insert into sources(id, kind, name, version, created_at, updated_at) values('source-e2','codex','Codex','1','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
		`insert into collections(id, source_id, external_id, kind, name, created_at, updated_at) values('collection-e2-dup','source-e2','session:e2-dup','agent_session','dup','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')`,
		`insert into actors(id, source_id, external_id, type, name) values('actor-e2','source-e2','actor:e2','human','Human')`,
		`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq, tombstoned_at)
values('item-e2-old','source-e2','collection-e2-dup','actor-e2','msg:e2','message','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','SESSION_OLD_UNIQUE_E2','sha256:old','{}','{}',1,null)`,
		`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq, tombstoned_at)
values('item-e2-live','source-e2','collection-e2-dup','actor-e2','msg:e2','message','2026-01-01T00:00:01Z','2026-01-01T00:00:01Z','SESSION_NEW_UNIQUE_E2','sha256:new','{}','{}',2,null)`,
		`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq, tombstoned_at)
values('item-e2-tomb','source-e2','collection-e2-dup','actor-e2','msg:e2-tomb','message','2026-01-01T00:00:02Z','2026-01-01T00:00:02Z','SESSION_TOMB_UNIQUE_E2','sha256:tomb','{}','{}',3,'2026-01-02T00:00:00Z')`,
		`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq, tombstoned_at)
values('item-e2-peer','source-e2','collection-e2-dup','actor-e2','msg:e2-peer','message','2026-01-01T00:00:03Z','2026-01-01T00:00:03Z','SESSION_PEER_UNIQUE_E2','sha256:peer','{}','{}',4,null)`,
		`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values
('item-e2-old','codex','agent_session','message','human','SESSION_OLD_UNIQUE_E2'),
('item-e2-live','codex','agent_session','message','human','SESSION_NEW_UNIQUE_E2'),
('item-e2-tomb','codex','agent_session','message','human','SESSION_TOMB_UNIQUE_E2'),
('item-e2-peer','codex','agent_session','message','human','SESSION_PEER_UNIQUE_E2')`,
		`insert into item_metadata(item_id, key, value) values
('item-e2-old','project','miseledger'),
('item-e2-live','project','miseledger'),
('item-e2-live','model','gpt-test'),
('item-e2-tomb','project','miseledger')`,
		`insert into relations(id, source_item_id, target_item_id, target_external_id, relation_type, confidence) values
('rel-e2-old','item-e2-live','item-e2-old','msg:e2','derived_from',1.0),
('rel-e2-tomb','item-e2-live','item-e2-tomb','msg:e2-tomb','derived_from',1.0),
('rel-e2-peer','item-e2-live','item-e2-peer','msg:e2-peer','derived_from',1.0)`,
	}
	for _, stmt := range stmts {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("seed failed on %s: %v", stmt, err)
		}
	}
}
