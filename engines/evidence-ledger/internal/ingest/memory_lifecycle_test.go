package ingest

import (
	"database/sql"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
)

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
	legacySource := `{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory:cards","kind":"memory_cards","name":"legacy"},"item":{"external_id":"card-legacy-source","kind":"memory_card","created_at":"2026-01-01T00:00:01Z","text":"legacy source"},"relations":[{"type":"legacy_inbound","target_external_id":"` + cardID + `"}],"raw":{"format":"json","path":"legacy.json","ordinal":1}}`
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "original")+"\n"+legacySource+"\n"), "seed.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := DetachMemoryNamespace(db, ns); err != nil {
		t.Fatal(err)
	}
	if _, err := ImportAdapterReader(db, strings.NewReader(memoryRecord(ns, cardID, "changed")+"\n"), "rebuilt.jsonl", ""); err != nil {
		t.Fatal(err)
	}
	if err := FinalizeMemoryRebuild(db, ns); err != nil {
		t.Fatal(err)
	}
	var collection string
	if err := db.QueryRow(`select c.external_id from relations r
join items i on i.id = r.target_item_id
join collections c on c.id = i.collection_id
where r.relation_type = 'legacy_inbound'`).Scan(&collection); err != nil {
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
