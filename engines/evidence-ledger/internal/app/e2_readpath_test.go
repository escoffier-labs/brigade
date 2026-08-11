package app

import (
	"encoding/json"
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
