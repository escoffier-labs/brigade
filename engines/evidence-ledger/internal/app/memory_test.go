package app

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/ingest"
)

const testMemoryNamespace = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

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
	if first["memory_namespace"] != testMemoryNamespace {
		t.Fatalf("namespace = %v", first["memory_namespace"])
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
	writeTestNamespace(t, ws, testMemoryNamespace)
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

	var liveAfter int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.kind = 'memory_card' and i.tombstoned_at is null`, ingest.MemorySourceKind).Scan(&liveAfter); err != nil {
		t.Fatal(err)
	}
	if liveAfter != liveBefore {
		t.Fatalf("live count changed: before=%d after=%d", liveBefore, liveAfter)
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

	health, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health after failed crawl = %+v", health)
	}
	if health.LastCompletedScanID != completedScanID {
		t.Fatalf("last_completed_scan_id = %q want %q", health.LastCompletedScanID, completedScanID)
	}
}

func TestCrawlMemoryInterruptedDoesNotTombstone(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
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
	scanID, err := ingest.BeginMemoryScan(db, ws, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	receipt := &ingest.MemoryScanReceipt{SourcePath: ws, EngineVersion: Version, Namespace: testMemoryNamespace, Failed: 1}
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
	health, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
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
	before, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()

	runOK(t, "crawl", "memory", ws, "--rebuild", "--json")
	db = openTestDB(t)
	defer db.Close()
	after, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
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

func TestCrawlMemoryFailedRebuildPreservesPriorProjection(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := copyEngineMemoryFixtures(t)
	completed := runJSON(t, "crawl", "memory", ws, "--json")
	completedScanID, _ := completed["scan_id"].(string)
	db := openTestDB(t)
	before, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	db.Close()
	if len(before) < 1 {
		t.Fatal("expected prior live projection")
	}

	// Remove workspace after a completed scan so rebuild walk fails; prior
	// projection must remain intact (failure-preserving rebuild).
	if err := os.RemoveAll(ws); err != nil {
		t.Fatal(err)
	}
	code, _, stderr := run("crawl", "memory", ws, "--rebuild", "--json")
	if code == 0 {
		t.Fatalf("expected failed rebuild, stderr=%s", stderr)
	}

	db = openTestDB(t)
	defer db.Close()
	after, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != len(before) {
		t.Fatalf("live ids changed after failed rebuild: before=%d after=%d", len(before), len(after))
	}
	for id, hash := range before {
		if after[id] != hash {
			t.Fatalf("hash changed for %s", id)
		}
	}
	var priorCompleted int
	if err := db.QueryRow(`select count(*) from source_scan_runs where id = ? and status = 'completed'`, completedScanID).Scan(&priorCompleted); err != nil {
		t.Fatal(err)
	}
	if priorCompleted != 1 {
		t.Fatalf("completed-scan record missing after failed rebuild")
	}
	health, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health after failed rebuild = %+v", health)
	}
	if health.LastCompletedScanID != completedScanID {
		t.Fatalf("last_completed_scan_id = %q want %q", health.LastCompletedScanID, completedScanID)
	}
}

func TestCrawlMemoryTwoRootNamespaceIsolation(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	nsA := "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	nsB := "memory-99999999-8888-4777-8666-bbbbbbbbbbbb"
	wsA := t.TempDir()
	wsB := t.TempDir()
	writeTestNamespace(t, wsA, nsA)
	writeTestNamespace(t, wsB, nsB)
	cardID := "card-shared0-1111-4222-8333-444444444444"
	for _, ws := range []string{wsA, wsB} {
		cards := filepath.Join(ws, "memory", "cards")
		if err := os.MkdirAll(cards, 0o755); err != nil {
			t.Fatal(err)
		}
		body := "---\nid: " + cardID + "\ntopic: shared\n---\n\n# Shared\nbody for " + filepath.Base(ws) + "\n"
		if err := os.WriteFile(filepath.Join(cards, "shared.md"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	runJSON(t, "crawl", "memory", wsA, "--json")
	runJSON(t, "crawl", "memory", wsB, "--json")

	db := openTestDB(t)
	defer db.Close()
	liveA, err := ingest.LiveMemoryProjection(db, nsA)
	if err != nil {
		t.Fatal(err)
	}
	liveB, err := ingest.LiveMemoryProjection(db, nsB)
	if err != nil {
		t.Fatal(err)
	}
	if liveA[cardID] == "" || liveB[cardID] == "" {
		t.Fatalf("both namespaces should have the shared card id: A=%v B=%v", liveA, liveB)
	}
	if liveA[cardID] == liveB[cardID] {
		t.Fatalf("distinct roots should keep distinct content hashes for same card id")
	}

	// Reconcile removal in A must not tombstone B.
	if err := os.Remove(filepath.Join(wsA, "memory", "cards", "shared.md")); err != nil {
		t.Fatal(err)
	}
	out := runJSON(t, "crawl", "memory", wsA, "--json")
	if out["removed"].(float64) < 1 {
		t.Fatalf("expected removal in A: %v", out)
	}
	liveA, _ = ingest.LiveMemoryProjection(db, nsA)
	liveB, _ = ingest.LiveMemoryProjection(db, nsB)
	if _, ok := liveA[cardID]; ok {
		t.Fatal("A should have removed shared card")
	}
	if liveB[cardID] == "" {
		t.Fatal("B must remain isolated from A's reconciliation")
	}
}

func TestCrawlMemoryDuplicateExplicitIDFailsBeforeReconcile(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	existing := "---\nid: card-exist00-1111-4222-8333-444444444444\ntopic: exist\n---\n\n# Exist\n"
	if err := os.WriteFile(filepath.Join(cards, "exist.md"), []byte(existing), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	dupID := "card-dup00000-1111-4222-8333-444444444444"
	body := "---\nid: " + dupID + "\ntopic: d\n---\n\n# Dup\n"
	if err := os.WriteFile(filepath.Join(cards, "one.md"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "two.md"), []byte(body+"extra\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	code, _, stderr := run("crawl", "memory", ws, "--json")
	if code == 0 {
		t.Fatal("expected duplicate explicit id failure")
	}
	if !strings.Contains(stderr, "duplicate explicit id") {
		t.Fatalf("stderr=%q", stderr)
	}
	db := openTestDB(t)
	defer db.Close()
	live, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if live["card-exist00-1111-4222-8333-444444444444"] == "" {
		t.Fatal("existing projection must survive duplicate-id fail-closed")
	}
	if _, ok := live[dupID]; ok {
		t.Fatal("duplicate id must not import last-wins rows")
	}
	var tombstoned int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.tombstoned_at is not null`, ingest.MemorySourceKind).Scan(&tombstoned); err != nil {
		t.Fatal(err)
	}
	if tombstoned != 0 {
		t.Fatalf("duplicate id failure must not reconcile/tombstone, got %d", tombstoned)
	}
}

func TestCrawlMemoryTransactionFailureAfterObservation(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	keepID := "card-tx000000-1111-4222-8333-444444444444"
	if err := os.WriteFile(filepath.Join(cards, "keep.md"), []byte("---\nid: "+keepID+"\ntopic: keep\n---\n\n# Keep\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "gone.md"), []byte("---\nid: card-txgone00-1111-4222-8333-444444444444\ntopic: gone\n---\n\n# Gone\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")
	if err := os.Remove(filepath.Join(cards, "gone.md")); err != nil {
		t.Fatal(err)
	}

	db := openTestDB(t)
	defer db.Close()
	before, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if len(before) != 2 {
		t.Fatalf("before live=%d", len(before))
	}

	ingest.SetMemoryScanAfterObserveHookForTest(func(tx *sql.Tx) error {
		return fmt.Errorf("injected observation-to-reconcile failure")
	})
	t.Cleanup(func() { ingest.SetMemoryScanAfterObserveHookForTest(nil) })

	scanID, err := ingest.BeginMemoryScan(db, ws, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	receipt := &ingest.MemoryScanReceipt{SourcePath: ws, Namespace: testMemoryNamespace, EngineVersion: Version}
	err = ingest.CompleteMemoryScan(db, scanID, []ingest.ObservedCard{
		{ExternalID: keepID, ContentHash: before[keepID], RawPath: "memory/cards/keep.md", Outcome: "unchanged"},
	}, receipt)
	if err == nil || !strings.Contains(err.Error(), "injected observation-to-reconcile failure") {
		t.Fatalf("expected injected failure, got %v", err)
	}
	_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)

	after, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != 2 || after[keepID] == "" || after["card-txgone00-1111-4222-8333-444444444444"] == "" {
		t.Fatalf("transaction failure after observation must not tombstone: %v", after)
	}
	var tombstoned int
	if err := db.QueryRow(`
select count(*) from items i join sources s on s.id = i.source_id
where s.kind = ? and i.tombstoned_at is not null`, ingest.MemorySourceKind).Scan(&tombstoned); err != nil {
		t.Fatal(err)
	}
	if tombstoned != 0 {
		t.Fatalf("tombstones=%d", tombstoned)
	}
	health, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health=%+v", health)
	}
}

func TestCrawlMemoryMoveDeleteReplay(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	subdir := filepath.Join(cards, "nested")
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(cards, "move-me.md")
	if err := os.WriteFile(path, []byte("---\nid: card-move000-1111-4222-8333-444444444444\ntopic: move\n---\n\n# Move\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	// Cross-directory move with explicit id preserves identity.
	dest := filepath.Join(subdir, "moved.md")
	if err := os.Rename(path, dest); err != nil {
		t.Fatal(err)
	}
	moved := runJSON(t, "crawl", "memory", ws, "--json")
	if moved["removed"].(float64) != 0 {
		t.Fatalf("explicit move must not remove identity: %v", moved)
	}

	// Pure deletion.
	if err := os.Remove(dest); err != nil {
		t.Fatal(err)
	}
	deleted := runJSON(t, "crawl", "memory", ws, "--json")
	if deleted["removed"].(float64) < 1 {
		t.Fatalf("delete should remove: %v", deleted)
	}

	// Interrupted-then-successful replay: fail a scan, then complete again.
	db := openTestDB(t)
	scanID, err := ingest.BeginMemoryScan(db, ws, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if err := ingest.FailMemoryScan(db, scanID, "interrupted", &ingest.MemoryScanReceipt{
		SourcePath: ws, Namespace: testMemoryNamespace, EngineVersion: Version, Failed: 1,
	}); err != nil {
		t.Fatal(err)
	}
	db.Close()
	if err := os.WriteFile(filepath.Join(cards, "replay.md"), []byte("---\nid: card-replay0-1111-4222-8333-444444444444\ntopic: replay\n---\n\n# Replay\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	replay := runJSON(t, "crawl", "memory", ws, "--json")
	if replay["status"] != "completed" || replay["created"].(float64) < 1 {
		t.Fatalf("replay = %v", replay)
	}
}

func TestCrawlMemoryUnresolvedRelationsPostResolutionAgree(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	body := strings.Join([]string{
		"---",
		"id: card-rel00000-1111-4222-8333-444444444444",
		"topic: rel",
		"derived_from: receipt:handoff-demo-1",
		"derived_from_target_source: brigade",
		"derived_from_target_collection: brigade:receipts",
		"---",
		"",
		"# Rel",
		"",
	}, "\n")
	if err := os.WriteFile(filepath.Join(cards, "rel.md"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	first := runJSON(t, "crawl", "memory", ws, "--json")
	unresolvedFirst := int(first["unresolved_relations"].(float64))
	if unresolvedFirst < 1 {
		t.Fatalf("expected unresolved before receipt import: %v", first)
	}
	health1, _ := first["memory_health"].(map[string]any)
	if int(health1["unresolved_relations"].(float64)) != unresolvedFirst {
		t.Fatalf("crawl/health disagree: crawl=%d health=%v", unresolvedFirst, health1["unresolved_relations"])
	}

	receiptFixture := repoPath(t, "testdata/adapters/memory/brigade-receipt.fixture.jsonl")
	runOK(t, "import", "adapter", receiptFixture, "--json")
	second := runJSON(t, "crawl", "memory", ws, "--json")
	unresolvedSecond := int(second["unresolved_relations"].(float64))
	if unresolvedSecond >= unresolvedFirst {
		t.Fatalf("re-crawl should resolve newly available target: first=%d second=%d %v", unresolvedFirst, unresolvedSecond, second)
	}
	health2, _ := second["memory_health"].(map[string]any)
	if int(health2["unresolved_relations"].(float64)) != unresolvedSecond {
		t.Fatalf("crawl/health disagree after resolve: crawl=%d health=%v", unresolvedSecond, health2["unresolved_relations"])
	}
	db := openTestDB(t)
	defer db.Close()
	var stored int
	if err := db.QueryRow(`select unresolved_relation_count from source_scan_runs where id = ?`, second["scan_id"]).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if stored != unresolvedSecond {
		t.Fatalf("scan record unresolved=%d crawl=%d", stored, unresolvedSecond)
	}
}

func TestMemoryCardRetentionSafety(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
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

func TestMemoryScanReceiptJSONShape(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "only.md"), []byte("---\nid: card-receipt-000-4000-8000-000000000001\ntopic: only\n---\n\n# Only\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	raw := runJSON(t, "crawl", "memory", ws, "--json")
	for _, key := range []string{"scan_id", "created", "updated", "unchanged", "removed", "skipped", "failed", "capability", "engine_version", "memory_namespace", "unresolved_relations"} {
		if _, ok := raw[key]; !ok {
			t.Fatalf("receipt missing %s: %v", key, raw)
		}
	}
	b, _ := json.Marshal(raw)
	if !strings.Contains(string(b), ingest.MemoryCapability) {
		t.Fatalf("capability missing from %s", b)
	}
}

func TestLegacyMemoryCardsScopedRebuildRule(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	db := openTestDB(t)
	defer db.Close()
	// Seed a legacy memory:cards row and ensure namespaced rebuild does not touch it.
	legacyJSONL := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory","version":"1.0.0"},"collection":{"external_id":"memory:cards","kind":"memory_cards","name":"cards"},"item":{"external_id":"card-legacy0-1111-4222-8333-444444444444","kind":"memory_card","created_at":"2026-01-01T00:00:00Z","text":"legacy"},"relations":[],"raw":{"format":"markdown","path":"memory/cards/legacy.md","ordinal":1}}` + "\n"
	dir := t.TempDir()
	path := filepath.Join(dir, "legacy.jsonl")
	if err := os.WriteFile(path, []byte(legacyJSONL), 0o600); err != nil {
		t.Fatal(err)
	}
	db.Close()
	runOK(t, "import", "adapter", path, "--json")

	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "new.md"), []byte("---\nid: card-new00000-1111-4222-8333-444444444444\ntopic: new\n---\n\n# New\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")
	runJSON(t, "crawl", "memory", ws, "--rebuild", "--json")

	db = openTestDB(t)
	defer db.Close()
	legacy, err := ingest.LegacyMemoryExternalIDs(db)
	if err != nil {
		t.Fatal(err)
	}
	if legacy["card-legacy0-1111-4222-8333-444444444444"] == "" {
		t.Fatal("scoped rebuild must leave legacy memory:cards rows intact")
	}

	// Empty-namespace health dual-reads legacy + namespaced live counts.
	agg, err := ingest.CollectMemoryHealth(db, Version, "")
	if err != nil {
		t.Fatal(err)
	}
	scoped, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if agg.LiveCount < scoped.LiveCount+1 {
		t.Fatalf("aggregate live=%d scoped=%d (expected legacy included)", agg.LiveCount, scoped.LiveCount)
	}
	if agg.MemoryNamespace != "" {
		t.Fatalf("aggregate health must not claim a single namespace, got %q", agg.MemoryNamespace)
	}
}

func TestCollectMemoryHealthEmptyAggregatesAllCollections(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	nsA := "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	nsB := "memory-99999999-8888-4777-8666-bbbbbbbbbbbb"
	for i, ns := range []string{nsA, nsB} {
		ws := t.TempDir()
		writeTestNamespace(t, ws, ns)
		cards := filepath.Join(ws, "memory", "cards")
		if err := os.MkdirAll(cards, 0o755); err != nil {
			t.Fatal(err)
		}
		cardID := []string{
			"card-aaaaaaa0-1111-4222-8333-444444444444",
			"card-bbbbbbb0-1111-4222-8333-444444444444",
		}[i]
		body := "---\nid: " + cardID + "\ntopic: t\n---\n\n# Card\n"
		if err := os.WriteFile(filepath.Join(cards, "card.md"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		runJSON(t, "crawl", "memory", ws, "--json")
	}
	db := openTestDB(t)
	defer db.Close()
	agg, err := ingest.CollectMemoryHealth(db, Version, "")
	if err != nil {
		t.Fatal(err)
	}
	a, err := ingest.CollectMemoryHealth(db, Version, nsA)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ingest.CollectMemoryHealth(db, Version, nsB)
	if err != nil {
		t.Fatal(err)
	}
	if a.LiveCount != 1 || b.LiveCount != 1 {
		t.Fatalf("scoped live A=%d B=%d", a.LiveCount, b.LiveCount)
	}
	if agg.LiveCount != a.LiveCount+b.LiveCount {
		t.Fatalf("aggregate live=%d want %d", agg.LiveCount, a.LiveCount+b.LiveCount)
	}
	if a.MemoryNamespace != nsA || b.MemoryNamespace != nsB {
		t.Fatalf("scoped namespaces A=%q B=%q", a.MemoryNamespace, b.MemoryNamespace)
	}
}

func TestCollectMemoryHealthEmptyReportsStaleNamespaceAfterNewerCompletion(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	nsA := "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	nsB := "memory-99999999-8888-4777-8666-bbbbbbbbbbbb"
	for i, ns := range []string{nsA, nsB} {
		ws := t.TempDir()
		writeTestNamespace(t, ws, ns)
		cards := filepath.Join(ws, "memory", "cards")
		if err := os.MkdirAll(cards, 0o755); err != nil {
			t.Fatal(err)
		}
		cardID := []string{
			"card-aaaaaaa0-1111-4222-8333-444444444444",
			"card-bbbbbbb0-1111-4222-8333-444444444444",
		}[i]
		body := "---\nid: " + cardID + "\ntopic: t\n---\n\n# Card\n"
		if err := os.WriteFile(filepath.Join(cards, "card.md"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if i == 0 {
			runJSON(t, "crawl", "memory", ws, "--json")
			db := openTestDB(t)
			scanID, err := ingest.BeginMemoryScan(db, ws, Version, ns)
			if err != nil {
				db.Close()
				t.Fatal(err)
			}
			receipt := &ingest.MemoryScanReceipt{SourcePath: ws, EngineVersion: Version, Namespace: ns, Failed: 1}
			if err := ingest.FailMemoryScan(db, scanID, "interrupted", receipt); err != nil {
				db.Close()
				t.Fatal(err)
			}
			db.Close()
			continue
		}
		runJSON(t, "crawl", "memory", ws, "--json")
	}

	db := openTestDB(t)
	defer db.Close()
	agg, err := ingest.CollectMemoryHealth(db, Version, "")
	if err != nil {
		t.Fatal(err)
	}
	if !agg.Stale || !agg.Partial {
		t.Fatalf("aggregate health must retain stale namespace state after newer completion: %+v", agg)
	}
}

func TestCrawlMemoryRebuildRepointsInboundRelationForChangedContent(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	cardID := "card-rebuild0-1111-4222-8333-444444444444"
	cardPath := filepath.Join(cards, "card.md")
	initial := "---\nid: " + cardID + "\ntopic: rebuild\n---\n\n# Initial\n"
	if err := os.WriteFile(cardPath, []byte(initial), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--json")

	supportJSONL := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:changed-rebuild","kind":"receipt","created_at":"2026-01-01T00:00:00Z","text":"support"},"relations":[{"type":"supports","target":{"source":"brigade-memory","collection":"` + testMemoryNamespace + `","external_id":"` + cardID + `"}}],"raw":{"format":"json","path":"r.json","ordinal":1}}` + "\n"
	supportPath := filepath.Join(t.TempDir(), "support.jsonl")
	if err := os.WriteFile(supportPath, []byte(supportJSONL), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", supportPath, "--json")

	changed := "---\nid: " + cardID + "\ntopic: rebuild\n---\n\n# Changed\n"
	if err := os.WriteFile(cardPath, []byte(changed), 0o600); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "crawl", "memory", ws, "--rebuild", "--json")

	db := openTestDB(t)
	defer db.Close()
	var liveID string
	if err := db.QueryRow(`select i.id from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ? and i.external_id = ? and i.tombstoned_at is null`,
		ingest.MemorySourceKind, testMemoryNamespace, cardID).Scan(&liveID); err != nil {
		t.Fatal(err)
	}
	var inbound int
	if err := db.QueryRow(`select count(*) from relations
where target_item_id = ? and target_source_kind = ? and target_collection_external_id = ? and target_external_id = ?`,
		liveID, ingest.MemorySourceKind, testMemoryNamespace, cardID).Scan(&inbound); err != nil {
		t.Fatal(err)
	}
	if inbound != 1 {
		t.Fatalf("changed-content rebuild lost or mispointed inbound relation: target=%q count=%d", liveID, inbound)
	}
}

func TestRebuildFailureAfterDetachPreservesProjectionGraph(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	cardID := "card-rb000000-1111-4222-8333-444444444444"
	if err := os.WriteFile(filepath.Join(cards, "keep.md"), []byte("---\nid: "+cardID+"\ntopic: keep\n---\n\n# Keep\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	completed := runJSON(t, "crawl", "memory", ws, "--json")
	completedScanID, _ := completed["scan_id"].(string)

	db := openTestDB(t)
	var itemID string
	if err := db.QueryRow(`
select i.id from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ? and i.external_id = ? and i.tombstoned_at is null`,
		ingest.MemorySourceKind, testMemoryNamespace, cardID).Scan(&itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into item_metadata(item_id, key, value) values(?,?,?)`, itemID, "fixture_key", "fixture_value"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into events(id, source_id, collection_id, actor_id, item_id, kind, occurred_at, metadata_json)
select 'evt-memory-1', i.source_id, i.collection_id, i.actor_id, i.id, 'memory_test', i.created_at, '{}' from items i where i.id = ?`, itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
select 'art-memory-1', i.source_id, i.id, 'art:1', 'note', 'memory/cards/keep.md', '', 'text/plain', 'artifact', i.content_hash, '{}' from items i where i.id = ?`, itemID); err != nil {
		t.Fatal(err)
	}
	// Outbound relation from a non-memory item targeting the memory card.
	supportJSONL := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:rebuild-support","kind":"receipt","created_at":"2026-01-01T00:00:00Z","text":"support"},"relations":[{"type":"supports","target":{"source":"brigade-memory","collection":"` + testMemoryNamespace + `","external_id":"` + cardID + `"}}],"raw":{"format":"json","path":"r.json","ordinal":1}}` + "\n"
	db.Close()
	supportPath := filepath.Join(t.TempDir(), "support.jsonl")
	if err := os.WriteFile(supportPath, []byte(supportJSONL), 0o600); err != nil {
		t.Fatal(err)
	}
	runOK(t, "import", "adapter", supportPath, "--json")

	db = openTestDB(t)
	before, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	var metaBefore, eventsBefore, artsBefore, inboundBefore int
	if err := db.QueryRow(`select count(*) from item_metadata where item_id = ?`, itemID).Scan(&metaBefore); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, itemID).Scan(&eventsBefore); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from artifacts where item_id = ?`, itemID).Scan(&artsBefore); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from relations where target_item_id = ?`, itemID).Scan(&inboundBefore); err != nil {
		t.Fatal(err)
	}
	if metaBefore < 1 || eventsBefore < 1 || artsBefore < 1 || inboundBefore < 1 {
		t.Fatalf("fixture graph incomplete meta=%d events=%d arts=%d inbound=%d", metaBefore, eventsBefore, artsBefore, inboundBefore)
	}
	db.Close()

	ingest.MemoryRebuildTestHookAfterDetach = func() error {
		return fmt.Errorf("injected post-detach rebuild failure")
	}
	t.Cleanup(func() { ingest.MemoryRebuildTestHookAfterDetach = nil })

	code, _, stderr := run("crawl", "memory", ws, "--rebuild", "--json")
	if code == 0 {
		t.Fatalf("expected rebuild failure, stderr=%s", stderr)
	}
	if !strings.Contains(stderr, "injected post-detach rebuild failure") {
		t.Fatalf("stderr=%q", stderr)
	}

	db = openTestDB(t)
	defer db.Close()
	after, err := ingest.LiveMemoryProjection(db, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if after[cardID] != before[cardID] {
		t.Fatalf("live hash changed: before=%v after=%v", before, after)
	}
	var metaAfter, eventsAfter, artsAfter, inboundAfter int
	if err := db.QueryRow(`select count(*) from item_metadata where item_id = ?`, itemID).Scan(&metaAfter); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, itemID).Scan(&eventsAfter); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from artifacts where item_id = ?`, itemID).Scan(&artsAfter); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from relations where target_item_id = ?`, itemID).Scan(&inboundAfter); err != nil {
		t.Fatal(err)
	}
	if metaAfter != metaBefore || eventsAfter != eventsBefore || artsAfter != artsBefore || inboundAfter != inboundBefore {
		t.Fatalf("graph not preserved meta %d/%d events %d/%d arts %d/%d inbound %d/%d",
			metaAfter, metaBefore, eventsAfter, eventsBefore, artsAfter, artsBefore, inboundAfter, inboundBefore)
	}
	var priorCompleted int
	if err := db.QueryRow(`select count(*) from source_scan_runs where id = ? and status = 'completed'`, completedScanID).Scan(&priorCompleted); err != nil {
		t.Fatal(err)
	}
	if priorCompleted != 1 {
		t.Fatal("completed-scan record must survive rebuild failure")
	}
	health, err := ingest.CollectMemoryHealth(db, Version, testMemoryNamespace)
	if err != nil {
		t.Fatal(err)
	}
	if !health.Stale || !health.Partial {
		t.Fatalf("health=%+v", health)
	}
	if health.LastCompletedScanID != completedScanID {
		t.Fatalf("last_completed=%q want %q", health.LastCompletedScanID, completedScanID)
	}
}

func copyEngineMemoryFixtures(t *testing.T) string {
	t.Helper()
	src := repoPath(t, "testdata/adapters/memory/cards")
	ws := t.TempDir()
	writeTestNamespace(t, ws, testMemoryNamespace)
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

func writeTestNamespace(t *testing.T, ws, ns string) {
	t.Helper()
	dir := filepath.Join(ws, "memory")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "NAMESPACE"), []byte(ns+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
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
