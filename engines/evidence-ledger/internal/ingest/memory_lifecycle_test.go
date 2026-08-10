package ingest

import (
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
)

func TestAggregateMemoryHealthCountsLegacyCompletedScanOnce(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	legacy := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory:cards","kind":"memory_cards","name":"legacy"},"item":{"external_id":"card-legacy-health","kind":"memory_card","created_at":"2026-01-01T00:00:00Z","text":"legacy"},"relations":[{"type":"missing","target_external_id":"missing"}],"raw":{"format":"json","path":"legacy.json","ordinal":1}}`
	if _, err := ImportAdapterReader(db, strings.NewReader(legacy+"\n"+memoryRecord(ns, "card-new-health", "new")+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	for _, scan := range []struct {
		id, meta   string
		unresolved int
	}{{"legacy-completed", "{}", 1}, {"namespaced-completed", `{"memory_namespace":"` + ns + `"}`, 0}} {
		if _, err := db.Exec(`insert into source_scan_runs(id, source_kind, source_path, started_at, completed_at, status, stale, canonical_count, live_count, hash_divergence_count, unresolved_relation_count, malformed_skipped_count, metadata_json)
values(?,?,?, ?, ?, 'completed', 0, 1, 1, 0, ?, 0, ?)`, scan.id, MemorySourceKind, scan.id, scan.id, scan.id, scan.unresolved, scan.meta); err != nil {
			t.Fatal(err)
		}
	}
	health, err := CollectMemoryHealth(db, "test", "")
	if err != nil {
		t.Fatal(err)
	}
	if health.LiveCount != 2 || health.UnresolvedRelations != 1 {
		t.Fatalf("legacy health double-counted: %+v", health)
	}
}

func TestLegacyMemoryRelationStaysInSourceNamespaceAcrossRebuild(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const nsA = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	const nsB = "memory-99999999-8888-4777-8666-bbbbbbbbbbbb"
	const cardID = "card-bare000-1111-4222-8333-444444444444"
	records := strings.Join([]string{
		memoryRecordAt(nsB, cardID, "B", "2026-01-01T00:00:00Z"),
		memoryRecordAt(nsA, cardID, "A", "2026-01-01T00:00:02Z"),
		memoryBareRelationRecord(nsA, "card-bare-source", cardID, "2026-01-01T00:00:03Z"),
	}, "\n") + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(records), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	assertRelationTargetCollection(t, db, "bare", nsA)
	if err := DetachMemoryNamespace(db, nsA); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecordAt(nsA, cardID, "A rebuilt", "2026-01-01T00:00:04Z")+"\n"+memoryBareRelationRecord(nsA, "card-bare-source", cardID, "2026-01-01T00:00:05Z")+"\n"), "rebuild.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeMemoryRebuild(db, nsA); err != nil {
		t.Fatal(err)
	}
	assertRelationTargetCollection(t, db, "bare", nsA)
}

func TestCompleteMemoryScanTombstoneKeepsExternalInboundUnresolved(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	const cardID = "card-tombstone-1111-4222-8333-444444444444"
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "live")+"\n"+supportRelationRecord("tombstone-inbound", cardID)+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	scanID, err := BeginMemoryScan(db, "workspace", "test", ns)
	if err != nil {
		t.Fatal(err)
	}
	if err := CompleteMemoryScan(db, scanID, nil, &MemoryScanReceipt{Namespace: ns}); err != nil {
		t.Fatal(err)
	}
	var target sql.NullString
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'tombstone-inbound'`).Scan(&target); err != nil {
		t.Fatal(err)
	}
	if target.Valid && target.String != "" {
		t.Fatalf("tombstoned target remained resolved to %q", target.String)
	}
}

func TestAbortMemoryRebuildRestoresRecordedBackupVersion(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	const cardID = "card-versioned-1111-4222-8333-444444444444"
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "original")+"\n"+supportRelationRecord("versioned-inbound", cardID)+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := DetachMemoryNamespace(db, ns); err != nil {
		t.Fatal(err)
	}
	backup := backupCollectionExternalID(ns)
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(backup, cardID, "older retained version")+"\n"), "backup-version.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "new partial")+"\n"), "new.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var partialID string
	if err := db.QueryRow(`select i.id from items i join collections c on c.id = i.collection_id where c.external_id = ? and i.external_id = ?`, ns, cardID).Scan(&partialID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update relations set target_item_id = ? where relation_type = 'versioned-inbound'`, partialID); err != nil {
		t.Fatal(err)
	}
	if err := AbortMemoryRebuild(db, ns); err != nil {
		t.Fatal(err)
	}
	assertRelationTargetCollection(t, db, "versioned-inbound", ns)
}

func memoryRecordAt(collection, externalID, text, createdAt string) string {
	return fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":%q,"kind":"memory_cards","name":"cards"},"item":{"external_id":%q,"kind":"memory_card","created_at":%q,"text":%q},"relations":[],"raw":{"format":"json","path":%q,"ordinal":1}}`, collection, externalID, createdAt, text, externalID+".json")
}

func memoryBareRelationRecord(collection, sourceID, targetID, createdAt string) string {
	return fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":%q,"kind":"memory_cards","name":"cards"},"item":{"external_id":%q,"kind":"memory_card","created_at":%q,"text":"source"},"relations":[{"type":"bare","target_external_id":%q}],"raw":{"format":"json","path":%q,"ordinal":1}}`, collection, sourceID, createdAt, targetID, sourceID+".json")
}

func assertRelationTargetCollection(t *testing.T, db *sql.DB, relationType, want string) {
	t.Helper()
	var got string
	if err := db.QueryRow(`select c.external_id from relations r join items i on i.id = r.target_item_id join collections c on c.id = i.collection_id where r.relation_type = ?`, relationType).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("relation %s target collection = %q, want %q", relationType, got, want)
	}
}

func TestFinalizeMemoryRebuildKeepsExternalInboundRelationsWhenTargetIsGoneOrAmbiguous(t *testing.T) {
	for _, tc := range []struct {
		name      string
		addSecond bool
	}{
		{name: "gone"},
		{name: "ambiguous", addSecond: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := openMemoryLifecycleDB(t)
			defer db.Close()
			const nsA = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
			const nsB = "memory-99999999-8888-4777-8666-bbbbbbbbbbbb"
			const cardID = "card-inbound0-1111-4222-8333-444444444444"
			if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(nsA, cardID, "original")+"\n"+supportRelationRecord("inbound", cardID)+"\n"), "seed.jsonl", ""); err != nil {
				t.Fatal(err)
			}
			if tc.addSecond {
				if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(nsB, cardID, "other namespace")+"\n"), "second.jsonl", ""); err != nil {
					t.Fatal(err)
				}
			}
			if err := DetachMemoryNamespace(db, nsA); err != nil {
				t.Fatal(err)
			}
			if tc.addSecond {
				if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(nsA, cardID, "rebuilt A")+"\n"), "rebuilt.jsonl", ""); err != nil {
					t.Fatal(err)
				}
			}
			if err := FinalizeMemoryRebuild(db, nsA); err != nil {
				t.Fatal(err)
			}
			var target sql.NullString
			if err := db.QueryRow(`select target_item_id from relations where relation_type = 'inbound'`).Scan(&target); err != nil {
				t.Fatalf("external inbound relation was deleted: %v", err)
			}
			if target.Valid && target.String != "" {
				t.Fatalf("unrepointable relation stayed resolved to %q", target.String)
			}
		})
	}
}

func TestFinalizeMemoryRebuildRepointsUniqueLegacySameSourceRelation(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	const cardID = "card-legacy00-1111-4222-8333-444444444444"
	legacySource := memoryBareRelationRecord(ns, "card-legacy-source", cardID, "2026-01-01T00:00:01Z")
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "original")+"\n"+legacySource+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := DetachMemoryNamespace(db, ns); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "changed")+"\n"+legacySource+"\n"), "rebuilt.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeMemoryRebuild(db, ns); err != nil {
		t.Fatal(err)
	}
	var collection string
	if err := db.QueryRow(`select c.external_id from relations r
join items i on i.id = r.target_item_id
join collections c on c.id = i.collection_id
where r.relation_type = 'bare'`).Scan(&collection); err != nil {
		t.Fatalf("legacy same-source relation was not remapped: %v", err)
	}
	if collection != ns {
		t.Fatalf("legacy relation remapped to %q, want %q", collection, ns)
	}
}

func TestAbortMemoryRebuildPreservesExternalInboundRelationAfterNewTargetResolution(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-11111111-2222-4333-8444-aaaaaaaaaaaa"
	const cardID = "card-abort000-1111-4222-8333-444444444444"
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "original")+"\n"+supportRelationRecord("abort_inbound", cardID)+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := DetachMemoryNamespace(db, ns); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "new")+"\n"), "new.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var newID string
	if err := db.QueryRow(`select i.id from items i join collections c on c.id = i.collection_id where c.external_id = ? and i.external_id = ?`, ns, cardID).Scan(&newID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update relations set target_item_id = ? where relation_type = 'abort_inbound'`, newID); err != nil {
		t.Fatal(err)
	}
	if err := AbortMemoryRebuild(db, ns); err != nil {
		t.Fatal(err)
	}
	var restored string
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'abort_inbound'`).Scan(&restored); err != nil {
		t.Fatalf("external inbound relation was deleted during abort: %v", err)
	}
	var collection string
	if err := db.QueryRow(`select c.external_id from items i join collections c on c.id = i.collection_id where i.id = ?`, restored).Scan(&collection); err != nil {
		t.Fatal(err)
	}
	if collection != ns {
		t.Fatalf("abort relation restored to %q, want %q", collection, ns)
	}
}

func openMemoryLifecycleDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := archive.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := archive.Migrate(db); err != nil {
		db.Close()
		t.Fatal(err)
	}
	return db
}

func TestDirectImportReingestRepointsInboundToRestoredVersion(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	const cardID = "card-direct00-1111-4222-8333-444444444444"
	v1 := memoryRecord(ns, cardID, "direct-body-one")
	v2 := memoryRecord(ns, cardID, "direct-body-two")
	inboundRec := fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:direct-inbound","kind":"receipt","created_at":"2026-01-01T00:00:00Z","text":"support"},"relations":[{"type":"direct_inbound","target":{"source":"brigade-memory","collection":%q,"external_id":%q}}],"raw":{"format":"json","path":"r.json","ordinal":1}}`, ns, cardID)

	// Import v1 alone first so a later identical v1 re-import hits AlreadyKnown.
	if _, err := ImportAdapterReader(db, strings.NewReader(v1+"\n"), "direct-v1.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var v1ID string
	if err := db.QueryRow(`select id from items where external_id = ?`, cardID).Scan(&v1ID); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(inboundRec+"\n"), "direct-inbound.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var inbound string
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'direct_inbound'`).Scan(&inbound); err != nil {
		t.Fatal(err)
	}
	if inbound != v1ID {
		t.Fatalf("inbound should start on v1=%s, got %s", v1ID, inbound)
	}
	var eventsBefore int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, v1ID).Scan(&eventsBefore); err != nil {
		t.Fatal(err)
	}

	if _, err := ImportAdapterReader(db, strings.NewReader(v2+"\n"), "direct-v2.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var v2ID string
	if err := db.QueryRow(`
select i.id from items i
join collections c on c.id = i.collection_id
where c.external_id = ? and i.external_id = ? and i.tombstoned_at is null
order by i.ingest_seq desc, i.id desc`, ns, cardID).Scan(&v2ID); err != nil {
		t.Fatal(err)
	}
	if v2ID == v1ID {
		t.Fatal("v2 must mint a distinct content-addressed item")
	}
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'direct_inbound'`).Scan(&inbound); err != nil {
		t.Fatal(err)
	}
	if inbound != v2ID {
		t.Fatalf("after v2 import inbound should be v2=%s, got %s", v2ID, inbound)
	}

	// Identical v1 bytes take the AlreadyKnown source-hash return; restamp alone
	// is not enough — resolveRelations must run before that return.
	again, err := ImportAdapterReader(db, strings.NewReader(v1+"\n"), "direct-v1-again.jsonl", "")
	if err != nil {
		t.Fatal(err)
	}
	if !again.AlreadyKnown || again.Inserted != 0 {
		t.Fatalf("expected AlreadyKnown identical re-import, got %+v", again)
	}
	var versionCount int
	if err := db.QueryRow(`select count(*) from items where external_id = ?`, cardID).Scan(&versionCount); err != nil {
		t.Fatal(err)
	}
	if versionCount != 2 {
		t.Fatalf("versions=%d want 2", versionCount)
	}
	live, err := LiveMemoryProjection(db, ns)
	if err != nil {
		t.Fatal(err)
	}
	var v1Hash string
	if err := db.QueryRow(`select content_hash from items where id = ?`, v1ID).Scan(&v1Hash); err != nil {
		t.Fatal(err)
	}
	if live[cardID] != v1Hash {
		t.Fatalf("live projection=%s want restored v1 %s", live[cardID], v1Hash)
	}
	if err := db.QueryRow(`select target_item_id from relations where relation_type = 'direct_inbound'`).Scan(&inbound); err != nil {
		t.Fatal(err)
	}
	if inbound != v1ID {
		t.Fatalf("direct-import AlreadyKnown path must repoint inbound to v1=%s, got %s", v1ID, inbound)
	}
	var eventsAfter int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, v1ID).Scan(&eventsAfter); err != nil {
		t.Fatal(err)
	}
	if eventsAfter != eventsBefore {
		t.Fatalf("must not mint events on known re-import: before=%d after=%d", eventsBefore, eventsAfter)
	}
}

func TestUpsertRecordRestampsKnownContentWithoutDuplicateEvent(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	const cardID = "card-restamp0-1111-4222-8333-444444444444"
	v1 := memoryRecord(ns, cardID, "body-one")
	v2 := memoryRecord(ns, cardID, "body-two")
	if _, err := ImportAdapterReader(db, strings.NewReader(v1+"\n"), "v1.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var v1ID string
	var v1Seq int64
	if err := db.QueryRow(`select id, ingest_seq from items where external_id = ?`, cardID).Scan(&v1ID, &v1Seq); err != nil {
		t.Fatal(err)
	}
	var eventsBefore int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, v1ID).Scan(&eventsBefore); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(v2+"\n"), "v2.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(v1+"\n"), "v1-again.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var versionCount int
	if err := db.QueryRow(`select count(*) from items where external_id = ?`, cardID).Scan(&versionCount); err != nil {
		t.Fatal(err)
	}
	if versionCount != 2 {
		t.Fatalf("versions=%d want 2", versionCount)
	}
	live, err := LiveMemoryProjection(db, ns)
	if err != nil {
		t.Fatal(err)
	}
	var v1Hash string
	var v1SeqAfter int64
	if err := db.QueryRow(`select content_hash, ingest_seq from items where id = ?`, v1ID).Scan(&v1Hash, &v1SeqAfter); err != nil {
		t.Fatal(err)
	}
	if live[cardID] != v1Hash {
		t.Fatalf("live=%s want restored v1 hash %s", live[cardID], v1Hash)
	}
	if v1SeqAfter <= v1Seq {
		t.Fatalf("known re-ingest must advance ingest_seq before=%d after=%d", v1Seq, v1SeqAfter)
	}
	var eventsAfter int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, v1ID).Scan(&eventsAfter); err != nil {
		t.Fatal(err)
	}
	if eventsAfter != eventsBefore {
		t.Fatalf("events before=%d after=%d", eventsBefore, eventsAfter)
	}
}

func TestLiveMemoryLatestVersionUsesIngestSeqNotWallClockOrHashOrder(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	const cardID = "card-order000-1111-4222-8333-444444444444"
	now := "2026-08-10T00:00:00Z"
	if _, err := db.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)`,
		"src-memory", MemorySourceKind, "Memory", "1.0.0", now, now); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)`,
		"col-memory", "src-memory", ns, "memory_cards", "cards", "{}", now, now); err != nil {
		t.Fatal(err)
	}
	// Lexicographically greater id is older; smaller id is newer by ingest_seq.
	// Equal/backward wall-clock updated_at must not beat the DB-monotonic seq.
	olderID := "zzzz-older-content-hash-id"
	newerID := "aaaa-newer-content-hash-id"
	olderHash := "sha256:older"
	newerHash := "sha256:newer"
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq)
values(?,?,?,?,?,?,?,?,?,?,?,?)`, olderID, "src-memory", "col-memory", cardID, "memory_card", "", "2026-08-10T03:00:00Z", "older", olderHash, "{}", "{}", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq)
values(?,?,?,?,?,?,?,?,?,?,?,?)`, newerID, "src-memory", "col-memory", cardID, "memory_card", "", "2026-08-10T01:00:00Z", "newer", newerHash, "{}", "{}", 2); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into relations(id, source_item_id, target_item_id, target_external_id, target_source_kind, target_collection_external_id, relation_type, confidence, metadata_json)
values(?,?,?,?,?,?,?,?,?)`, "rel-stale-out", olderID, nil, "missing-target", "", "", "supported_by", 1.0, "{}"); err != nil {
		t.Fatal(err)
	}

	var byClock, byID string
	if err := db.QueryRow(`select id from items where external_id = ? order by updated_at desc, id desc`, cardID).Scan(&byClock); err != nil {
		t.Fatal(err)
	}
	if byClock != olderID {
		t.Fatalf("precondition failed: wall-clock order should pick older %s, got %s", olderID, byClock)
	}
	if err := db.QueryRow(`select id from items where external_id = ? order by id desc`, cardID).Scan(&byID); err != nil {
		t.Fatal(err)
	}
	if byID != olderID {
		t.Fatalf("precondition failed: id tie-break should pick older %s, got %s", olderID, byID)
	}

	live, err := LiveMemoryProjection(db, ns)
	if err != nil {
		t.Fatal(err)
	}
	if live[cardID] != newerHash {
		t.Fatalf("live projection selected %q, want newer hash %q", live[cardID], newerHash)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	unresolved, err := memoryUnresolvedRelationCount(tx, ns)
	if err != nil {
		t.Fatal(err)
	}
	if unresolved != 0 {
		t.Fatalf("stale outbound unresolved on older version contaminated count: %d", unresolved)
	}
}

func TestLiveMemoryLatestVersionEqualWallClockUsesIngestSeq(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	const cardID = "card-equalclk-1111-4222-8333-444444444444"
	now := "2026-08-10T00:00:00Z"
	stamp := "2026-08-10T02:00:00Z"
	if _, err := db.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)`,
		"src-memory", MemorySourceKind, "Memory", "1.0.0", now, now); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)`,
		"col-memory", "src-memory", ns, "memory_cards", "cards", "{}", now, now); err != nil {
		t.Fatal(err)
	}
	olderID := "zzzz-equal-clock-older"
	newerID := "aaaa-equal-clock-newer"
	olderHash := "sha256:equal-older"
	newerHash := "sha256:equal-newer"
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq)
values(?,?,?,?,?,?,?,?,?,?,?,?)`, olderID, "src-memory", "col-memory", cardID, "memory_card", "", stamp, "older", olderHash, "{}", "{}", 10); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, content_hash, raw_json, metadata_json, ingest_seq)
values(?,?,?,?,?,?,?,?,?,?,?,?)`, newerID, "src-memory", "col-memory", cardID, "memory_card", "", stamp, "newer", newerHash, "{}", "{}", 11); err != nil {
		t.Fatal(err)
	}
	var byClockID string
	if err := db.QueryRow(`select id from items where external_id = ? order by updated_at desc, id desc`, cardID).Scan(&byClockID); err != nil {
		t.Fatal(err)
	}
	if byClockID != olderID {
		t.Fatalf("precondition failed: equal clock + id desc should pick older %s, got %s", olderID, byClockID)
	}
	live, err := LiveMemoryProjection(db, ns)
	if err != nil {
		t.Fatal(err)
	}
	if live[cardID] != newerHash {
		t.Fatalf("equal wall-clock must still prefer ingest_seq; got %q want %q", live[cardID], newerHash)
	}
}

func TestKnownItemSameTextFrontmatterReconcilesCanonicalFieldsReceiptAToB(t *testing.T) {
	db := openMemoryLifecycleDB(t)
	defer db.Close()
	const ns = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	const cardID = "card-front000-1111-4222-8333-444444444444"
	const body = "same-text-frontmatter-body"
	receiptA := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:front-a","kind":"receipt","created_at":"2026-01-01T00:00:00Z","text":"receipt-a"},"relations":[],"raw":{"format":"json","path":"a.json","ordinal":1}}`
	receiptB := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:front-b","kind":"receipt","created_at":"2026-01-01T00:00:01Z","text":"receipt-b"},"relations":[],"raw":{"format":"json","path":"b.json","ordinal":1}}`
	cardA := fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":%q,"kind":"memory_cards","name":"cards"},"item":{"external_id":%q,"kind":"memory_card","created_at":"2026-01-01T00:00:02Z","text":%q,"tags":["tag-a"],"metadata":{"topic":"alpha","status":"draft"}},"artifacts":[{"external_id":"art-a","kind":"note","text":"artifact-a"}],"relations":[{"type":"derived_from","target":{"source":"brigade","collection":"brigade:receipts","external_id":"receipt:front-a"}}],"raw":{"format":"json","path":"card.json","ordinal":1}}`, ns, cardID, body)
	cardB := fmt.Sprintf(`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":%q,"kind":"memory_cards","name":"cards"},"item":{"external_id":%q,"kind":"memory_card","created_at":"2026-01-01T00:00:02Z","text":%q,"tags":["tag-b"],"metadata":{"topic":"beta","status":"ready"}},"artifacts":[{"external_id":"art-b","kind":"note","text":"artifact-b"}],"relations":[{"type":"derived_from","target":{"source":"brigade","collection":"brigade:receipts","external_id":"receipt:front-b"}}],"raw":{"format":"json","path":"card.json","ordinal":1}}`, ns, cardID, body)

	if _, err := ImportAdapterReader(db, strings.NewReader(receiptA+"\n"+receiptB+"\n"+cardA+"\n"), "front-a.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	var cardItemID, receiptAID, receiptBID string
	var seqBefore int64
	if err := db.QueryRow(`select id, ingest_seq from items where external_id = ?`, cardID).Scan(&cardItemID, &seqBefore); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select id from items where external_id = 'receipt:front-a'`).Scan(&receiptAID); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select id from items where external_id = 'receipt:front-b'`).Scan(&receiptBID); err != nil {
		t.Fatal(err)
	}
	var targetA string
	if err := db.QueryRow(`select target_item_id from relations where source_item_id = ? and relation_type = 'derived_from'`, cardItemID).Scan(&targetA); err != nil {
		t.Fatal(err)
	}
	if targetA != receiptAID {
		t.Fatalf("initial relation target=%s want receipt A %s", targetA, receiptAID)
	}
	var eventsBefore int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, cardItemID).Scan(&eventsBefore); err != nil {
		t.Fatal(err)
	}

	again, err := ImportAdapterReader(db, strings.NewReader(cardB+"\n"), "front-b.jsonl", "")
	if err != nil {
		t.Fatal(err)
	}
	// New source path/hash is not whole-file AlreadyKnown; item identity is Text+Summary.
	if again.Inserted != 0 {
		t.Fatalf("same-text frontmatter edit must not mint a new item, got %+v", again)
	}
	var versionCount int
	if err := db.QueryRow(`select count(*) from items where external_id = ?`, cardID).Scan(&versionCount); err != nil {
		t.Fatal(err)
	}
	if versionCount != 1 {
		t.Fatalf("same-text must keep one content-addressed row, got %d", versionCount)
	}
	var seqAfter int64
	var metaJSON string
	if err := db.QueryRow(`select ingest_seq, metadata_json from items where id = ?`, cardItemID).Scan(&seqAfter, &metaJSON); err != nil {
		t.Fatal(err)
	}
	if seqAfter <= seqBefore {
		t.Fatalf("ingest_seq must advance on known reconcile before=%d after=%d", seqBefore, seqAfter)
	}
	if !strings.Contains(metaJSON, `"topic":"beta"`) || !strings.Contains(metaJSON, `"status":"ready"`) {
		t.Fatalf("metadata_json missing receipt-B frontmatter fields: %s", metaJSON)
	}
	if !strings.Contains(metaJSON, `"provenance"`) {
		t.Fatalf("provenance envelope missing after reconcile: %s", metaJSON)
	}
	var tagCount int
	var tag string
	if err := db.QueryRow(`select count(*), min(tag) from item_tags where item_id = ?`, cardItemID).Scan(&tagCount, &tag); err != nil {
		t.Fatal(err)
	}
	if tagCount != 1 || tag != "tag-b" {
		t.Fatalf("tags after reconcile count=%d tag=%q want only tag-b", tagCount, tag)
	}
	var artCount int
	var artExt string
	if err := db.QueryRow(`select count(*), min(external_id) from artifacts where item_id = ?`, cardItemID).Scan(&artCount, &artExt); err != nil {
		t.Fatal(err)
	}
	if artCount != 1 || artExt != "art-b" {
		t.Fatalf("artifacts after reconcile count=%d ext=%q want only art-b", artCount, artExt)
	}
	var relCount int
	var targetB string
	if err := db.QueryRow(`select count(*), min(target_item_id) from relations where source_item_id = ? and relation_type = 'derived_from'`, cardItemID).Scan(&relCount, &targetB); err != nil {
		t.Fatal(err)
	}
	if relCount != 1 || targetB != receiptBID {
		t.Fatalf("relation after reconcile count=%d target=%s want receipt B %s", relCount, targetB, receiptBID)
	}
	var eventsAfter int
	if err := db.QueryRow(`select count(*) from events where item_id = ?`, cardItemID).Scan(&eventsAfter); err != nil {
		t.Fatal(err)
	}
	if eventsAfter != eventsBefore {
		t.Fatalf("must not mint events on known reconcile: before=%d after=%d", eventsBefore, eventsAfter)
	}
	live, err := LiveMemoryProjection(db, ns)
	if err != nil {
		t.Fatal(err)
	}
	var cardHash string
	if err := db.QueryRow(`select content_hash from items where id = ?`, cardItemID).Scan(&cardHash); err != nil {
		t.Fatal(err)
	}
	if live[cardID] != cardHash {
		t.Fatalf("live projection=%s want %s", live[cardID], cardHash)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	unresolved, err := memoryUnresolvedRelationCount(tx, ns)
	if err != nil {
		t.Fatal(err)
	}
	if unresolved != 0 {
		t.Fatalf("health unresolved after receipt-B reconcile: %d", unresolved)
	}
}
