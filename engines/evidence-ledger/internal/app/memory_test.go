package app

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/ingest"
)

func TestCrawlMemorySlice1a(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := copyEngineMemoryFixtures(t)

	// Seed brigade receipt for qualified relation resolution.
	receiptFixture := repoPath(t, "testdata/adapters/memory/brigade-receipt.fixture.jsonl")
	runOK(t, "import", "adapter", receiptFixture, "--json")

	first := runJSON(t, "crawl", "memory", ws, "--json")
	if first["status"] != "completed" {
		t.Fatalf("first crawl = %v", first)
	}
	if first["created"].(float64) < 1 {
		t.Fatalf("expected creates: %v", first)
	}
	if first["failed"].(float64) != 0 {
		t.Fatalf("failed = %v", first["failed"])
	}
	if first["skipped"].(float64) < 1 {
		t.Fatalf("expected malformed skipped: %v", first)
	}

	// Unchanged re-crawl is idempotent.
	second := runJSON(t, "crawl", "memory", ws, "--json")
	if second["unchanged"].(float64) < 1 || second["created"].(float64) != 0 {
		t.Fatalf("second crawl should be unchanged-heavy: %v", second)
	}
	if second["inserted_items"].(float64) != 0 {
		t.Fatalf("idempotent re-crawl inserted items: %v", second)
	}

	// Update one explicit-id card.
	explicitPath := filepath.Join(ws, "memory", "cards", "valid-explicit.md")
	body, err := os.ReadFile(explicitPath)
	if err != nil {
		t.Fatal(err)
	}
	updated := strings.Replace(string(body), "Body text for the valid explicit-id memory card fixture.", "Edited body for update coverage.", 1)
	if err := os.WriteFile(explicitPath, []byte(updated), 0o600); err != nil {
		t.Fatal(err)
	}
	third := runJSON(t, "crawl", "memory", ws, "--json")
	if third["updated"].(float64) < 1 {
		t.Fatalf("expected update: %v", third)
	}

	db := openTestDB(t)
	defer db.Close()
	var resolved int
	if err := db.QueryRow(`
select count(*) from relations r
join items i on i.id = r.source_item_id
join sources s on s.id = i.source_id
where s.kind = ? and r.relation_type = 'derived_from' and r.target_item_id is not null
  and r.target_source_kind = 'brigade'`, ingest.MemorySourceKind).Scan(&resolved); err != nil {
		t.Fatal(err)
	}
	if resolved < 1 {
		t.Fatal("expected qualified derived_from relation to resolve")
	}
	var unresolved int
	if err := db.QueryRow(`
select count(*) from relations r
join items i on i.id = r.source_item_id
join sources s on s.id = i.source_id
where s.kind = ? and r.relation_type = 'supported_by' and r.target_item_id is null`, ingest.MemorySourceKind).Scan(&unresolved); err != nil {
		t.Fatal(err)
	}
	if unresolved < 1 {
		t.Fatal("expected unresolved supported_by to remain queryable")
	}

	status := runJSON(t, "status", "--json")
	if status["capability"] == nil {
		t.Fatalf("status missing capability: %v", status)
	}
	health, _ := status["memory_health"].(map[string]any)
	if health == nil || health["capability"] != ingest.MemoryCapability {
		t.Fatalf("status memory_health = %v", status["memory_health"])
	}

	doctor := runJSON(t, "doctor", "--json")
	if doctor["memory_health"] == nil {
		t.Fatalf("doctor missing memory_health: %v", doctor)
	}
}

func TestCrawlMemoryExplicitRenameAndPathRemoveCreate(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	explicit := filepath.Join(cards, "alpha.md")
	if err := os.WriteFile(explicit, []byte("---\nid: card-rename-0000-4000-8000-000000000001\ntopic: alpha\n---\n\n# Alpha\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pathCard := filepath.Join(cards, "beta.md")
	if err := os.WriteFile(pathCard, []byte("---\ntopic: beta\n---\n\n# Beta\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	// Explicit-id rename preserves identity.
	if err := os.Rename(explicit, filepath.Join(cards, "alpha-renamed.md")); err != nil {
		t.Fatal(err)
	}
	// Path-identity rename is remove+create.
	if err := os.Rename(pathCard, filepath.Join(cards, "beta-renamed.md")); err != nil {
		t.Fatal(err)
	}
	out := runJSON(t, "crawl", "memory", ws, "--json")
	if out["created"].(float64) < 1 {
		t.Fatalf("path rename should create: %v", out)
	}
	if out["removed"].(float64) < 1 {
		t.Fatalf("path rename should remove old path id: %v", out)
	}

	db := openTestDB(t)
	defer db.Close()
	var liveExplicit int
	if err := db.QueryRow(`
select count(distinct i.external_id) from items i
join sources s on s.id = i.source_id
where s.kind = ? and i.external_id = 'card-rename-0000-4000-8000-000000000001' and i.tombstoned_at is null`, ingest.MemorySourceKind).Scan(&liveExplicit); err != nil {
		t.Fatal(err)
	}
	if liveExplicit != 1 {
		t.Fatalf("explicit rename should keep one live external id, got %d", liveExplicit)
	}
	var oldPathLive int
	if err := db.QueryRow(`
select count(*) from items i
join sources s on s.id = i.source_id
where s.kind = ? and i.external_id = 'path:memory/cards/beta.md' and i.tombstoned_at is null`, ingest.MemorySourceKind).Scan(&oldPathLive); err != nil {
		t.Fatal(err)
	}
	if oldPathLive != 0 {
		t.Fatalf("old path identity should be tombstoned, live=%d", oldPathLive)
	}
}

func TestCrawlMemoryFailedCrawlPreservesPriorSnapshot(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := copyEngineMemoryFixtures(t)

	completed := runJSON(t, "crawl", "memory", ws, "--json")
	if completed["status"] != "completed" {
		t.Fatalf("first crawl = %v", completed)
	}
	completedScanID, _ := completed["scan_id"].(string)
	if completedScanID == "" {
		t.Fatalf("completed crawl missing scan_id: %v", completed)
	}

	db := openTestDB(t)
	var liveBefore int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.kind = 'memory_card' and i.tombstoned_at is null`, ingest.MemorySourceKind).Scan(&liveBefore); err != nil {
		t.Fatal(err)
	}
	if liveBefore < 1 {
		t.Fatalf("expected live memory cards before failure, got %d", liveBefore)
	}
	db.Close()

	if err := os.RemoveAll(ws); err != nil {
		t.Fatal(err)
	}
	code, out, stderr := run("crawl", "memory", ws, "--json")
	if code == 0 {
		t.Fatalf("expected failed crawl, stdout=%s stderr=%s", out, stderr)
	}
	if !strings.Contains(stderr, "crawl memory:") {
		t.Fatalf("expected crawl memory error, stderr=%q", stderr)
	}

	db = openTestDB(t)
	defer db.Close()

	var failedScanID, status string
	var failedCount, scanStale int
	if err := db.QueryRow(`
select id, status, failed_count, stale from source_scan_runs
where source_kind = ? order by started_at desc limit 1`, ingest.MemorySourceKind).Scan(&failedScanID, &status, &failedCount, &scanStale); err != nil {
		t.Fatal(err)
	}
	if status != "failed" {
		t.Fatalf("latest scan status = %q", status)
	}
	if failedScanID == "" || failedScanID == completedScanID {
		t.Fatalf("failed scan_id = %q prior completed = %q", failedScanID, completedScanID)
	}
	if failedCount < 1 {
		t.Fatalf("failed_count = %d", failedCount)
	}
	if scanStale != 1 {
		t.Fatalf("failed scan stale = %d", scanStale)
	}

	var priorCompleted int
	if err := db.QueryRow(`
select count(*) from source_scan_runs
where id = ? and status = 'completed'`, completedScanID).Scan(&priorCompleted); err != nil {
		t.Fatal(err)
	}
	if priorCompleted != 1 {
		t.Fatalf("prior completed snapshot missing for %s", completedScanID)
	}
	var priorStale int
	if err := db.QueryRow(`select stale from source_scan_runs where id = ?`, completedScanID).Scan(&priorStale); err != nil {
		t.Fatal(err)
	}
	if priorStale != 1 {
		t.Fatalf("prior completed snapshot stale = %d", priorStale)
	}

	var tombstoned int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.tombstoned_at is not null`, ingest.MemorySourceKind).Scan(&tombstoned); err != nil {
		t.Fatal(err)
	}
	if tombstoned != 0 {
		t.Fatalf("failed crawl tombstoned %d rows", tombstoned)
	}
	var liveAfter int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.kind = 'memory_card' and i.tombstoned_at is null`, ingest.MemorySourceKind).Scan(&liveAfter); err != nil {
		t.Fatal(err)
	}
	if liveAfter != liveBefore {
		t.Fatalf("live count changed: before=%d after=%d", liveBefore, liveAfter)
	}

	health, err := ingest.CollectMemoryHealth(db, Version)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health after failed crawl = %+v", health)
	}
	if health.LastCompletedScanID != completedScanID {
		t.Fatalf("last_completed_scan_id = %q want %q", health.LastCompletedScanID, completedScanID)
	}

	statusJSON := runJSON(t, "status", "--json")
	memHealth, _ := statusJSON["memory_health"].(map[string]any)
	if memHealth == nil {
		t.Fatalf("status missing memory_health: %v", statusJSON)
	}
	if memHealth["stale"] != true || memHealth["partial"] != true {
		t.Fatalf("status memory_health = %v", memHealth)
	}
	if memHealth["last_completed_scan_id"] != completedScanID {
		t.Fatalf("status last_completed_scan_id = %v", memHealth["last_completed_scan_id"])
	}
}

func TestCrawlMemoryInterruptedDoesNotTombstone(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"one.md", "two.md"} {
		body := "---\ntopic: " + name + "\n---\n\n# " + name + "\n"
		if err := os.WriteFile(filepath.Join(cards, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	db := openTestDB(t)
	defer db.Close()
	scanID, err := ingest.BeginMemoryScan(db, ws, Version)
	if err != nil {
		t.Fatal(err)
	}
	receipt := &ingest.MemoryScanReceipt{SourcePath: ws, EngineVersion: Version, Failed: 1}
	if err := ingest.FailMemoryScan(db, scanID, "interrupted", receipt); err != nil {
		t.Fatal(err)
	}
	var tombstoned int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.tombstoned_at is not null`, ingest.MemorySourceKind).Scan(&tombstoned); err != nil {
		t.Fatal(err)
	}
	if tombstoned != 0 {
		t.Fatalf("interrupted scan tombstoned %d rows", tombstoned)
	}
	var stale int
	if err := db.QueryRow(`select count(*) from source_scan_runs where source_kind = ? and status = 'completed' and stale = 1`, ingest.MemorySourceKind).Scan(&stale); err != nil {
		t.Fatal(err)
	}
	if stale != 1 {
		t.Fatalf("prior completed snapshot stale count = %d", stale)
	}
	health, err := ingest.CollectMemoryHealth(db, Version)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health after interrupt = %+v", health)
	}
}

func TestCrawlMemoryRebuildDeterminism(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := copyEngineMemoryFixtures(t)
	runJSON(t, "crawl", "memory", ws, "--json")
	db := openTestDB(t)
	before, err := ingest.LiveMemoryProjection(db)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()

	rebuild := runJSON(t, "crawl", "memory", ws, "--rebuild", "--json")
	if rebuild["rebuild"] != true || rebuild["status"] != "completed" {
		t.Fatalf("rebuild = %v", rebuild)
	}
	db = openTestDB(t)
	defer db.Close()
	after, err := ingest.LiveMemoryProjection(db)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) == 0 || len(before) != len(after) {
		t.Fatalf("live count before=%d after=%d", len(before), len(after))
	}
	for id, hash := range before {
		if after[id] != hash {
			t.Fatalf("hash mismatch for %s: %s vs %s", id, hash, after[id])
		}
	}
}

func TestMemoryCardRetentionSafety(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "keep.md"), []byte("---\nid: card-retain-0000-4000-8000-000000000001\ntopic: keep\n---\n\n# Keep\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	// Force old created_at so age-based tiers would match if kind were wrong.
	db := openTestDB(t)
	if _, err := db.Exec(`update items set created_at = '2020-01-01T00:00:00Z' where kind = 'memory_card'`); err != nil {
		t.Fatal(err)
	}
	db.Close()

	dry := runJSON(t, "prune", "policy", "--json")
	if dry["matched_items"].(float64) != 0 {
		t.Fatalf("default retention must not match memory_card: %v", dry)
	}
	if err := ingest.AssertMemoryNotInDefaultRetention("memory_card"); err == nil {
		t.Fatal("helper should reject memory_card")
	}
}

func TestQualifiedRelationAdapterCompatibility(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	dir := t.TempDir()
	legacy := filepath.Join(dir, "legacy.jsonl")
	// Legacy same-source relation via target_external_id only.
	body := `{"schema":"miseledger.adapter.v1","source":{"kind":"reltest","name":"Rel"},"collection":{"external_id":"c","kind":"k","name":"c"},"item":{"external_id":"a","kind":"message","created_at":"2026-01-01T00:00:00Z","text":"a"},"relations":[],"raw":{"format":"json","path":"a.json","ordinal":1}}` + "\n" +
		`{"schema":"miseledger.adapter.v1","source":{"kind":"reltest","name":"Rel"},"collection":{"external_id":"c","kind":"k","name":"c"},"item":{"external_id":"b","kind":"message","created_at":"2026-01-01T00:00:01Z","text":"b"},"relations":[{"target_external_id":"a","type":"derived_from"}],"raw":{"format":"json","path":"b.json","ordinal":2}}` + "\n"
	if err := os.WriteFile(legacy, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", legacy, "--json")
	db := openTestDB(t)
	defer db.Close()
	var n int
	if err := db.QueryRow(`select count(*) from relations where target_item_id is not null and coalesce(target_source_kind,'') = ''`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("legacy relation resolved count = %d", n)
	}
}

func copyEngineMemoryFixtures(t *testing.T) string {
	t.Helper()
	src := repoPath(t, "testdata/adapters/memory/cards")
	ws := t.TempDir()
	dst := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(dst, 0o755); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		in, err := os.ReadFile(filepath.Join(src, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dst, e.Name()), in, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return ws
}

func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	paths := ResolvePaths()
	db, err := archive.Open(paths.DBPath)
	if err != nil {
		t.Fatal(err)
	}
	return db
}

func TestMemoryScanReceiptJSONShape(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "only.md"), []byte("---\nid: card-receipt-000-4000-8000-000000000001\ntopic: only\n---\n\n# Only\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	raw := runJSON(t, "crawl", "memory", ws, "--json")
	for _, key := range []string{"scan_id", "created", "updated", "unchanged", "removed", "skipped", "failed", "capability", "engine_version"} {
		if _, ok := raw[key]; !ok {
			t.Fatalf("receipt missing %s: %v", key, raw)
		}
	}
	b, _ := json.Marshal(raw)
	if !strings.Contains(string(b), ingest.MemoryCapability) {
		t.Fatalf("capability missing from %s", b)
	}
}
