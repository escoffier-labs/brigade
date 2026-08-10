package ingest

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
)

func TestResolveRelationsQualifiedAndLegacy(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")
	db, err := archive.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}

	legacy := strings.Join([]string{
		`{"schema":"miseledger.adapter.v1","source":{"kind":"reltest","name":"Rel"},"collection":{"external_id":"c","kind":"k","name":"c"},"item":{"external_id":"a","kind":"message","created_at":"2026-01-01T00:00:00Z","text":"a"},"relations":[],"raw":{"format":"json","path":"a.json","ordinal":1}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"reltest","name":"Rel"},"collection":{"external_id":"c","kind":"k","name":"c"},"item":{"external_id":"b","kind":"message","created_at":"2026-01-01T00:00:01Z","text":"b"},"relations":[{"target_external_id":"a","type":"derived_from"}],"raw":{"format":"json","path":"b.json","ordinal":2}}`,
	}, "\n") + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(legacy), "legacy.jsonl", ""); err != nil {
		t.Fatal(err)
	}

	qualified := strings.Join([]string{
		`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade","name":"Brigade"},"collection":{"external_id":"brigade:receipts","kind":"brigade_receipt","name":"receipts"},"item":{"external_id":"receipt:1","kind":"receipt","created_at":"2026-01-01T00:00:00Z","text":"receipt"},"relations":[],"raw":{"format":"json","path":"r.json","ordinal":1}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory:cards","kind":"memory_cards","name":"cards"},"item":{"external_id":"card-1","kind":"memory_card","created_at":"2026-01-01T00:00:02Z","text":"card"},"relations":[{"type":"derived_from","target":{"source":"brigade","collection":"brigade:receipts","external_id":"receipt:1"}},{"type":"supported_by","target":{"source":"brigade","collection":"brigade:receipts","external_id":"missing"}}],"raw":{"format":"json","path":"c.json","ordinal":1}}`,
	}, "\n") + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(qualified), "qualified.jsonl", ""); err != nil {
		t.Fatal(err)
	}

	var legacyResolved, qualifiedResolved, unresolved int
	if err := db.QueryRow(`select count(*) from relations where relation_type='derived_from' and coalesce(target_source_kind,'')='' and target_item_id is not null`).Scan(&legacyResolved); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from relations where relation_type='derived_from' and target_source_kind='brigade' and target_item_id is not null`).Scan(&qualifiedResolved); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from relations where relation_type='supported_by' and target_item_id is null`).Scan(&unresolved); err != nil {
		t.Fatal(err)
	}
	if legacyResolved != 1 || qualifiedResolved != 1 || unresolved != 1 {
		t.Fatalf("legacy=%d qualified=%d unresolved=%d", legacyResolved, qualifiedResolved, unresolved)
	}
}

func TestResolveRelationsQualifiedWithoutCollectionRequiresUniqueLiveTarget(t *testing.T) {
	dir := t.TempDir()
	db, err := archive.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}

	records := strings.Join([]string{
		`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory-a","kind":"memory_cards","name":"A"},"item":{"external_id":"card-shared","kind":"memory_card","created_at":"2026-01-01T00:00:00Z","text":"A shared"},"relations":[],"raw":{"format":"json","path":"a-shared.json","ordinal":1}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory-b","kind":"memory_cards","name":"B"},"item":{"external_id":"card-shared","kind":"memory_card","created_at":"2026-01-01T00:00:01Z","text":"B shared"},"relations":[],"raw":{"format":"json","path":"b-shared.json","ordinal":2}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"brigade-memory","name":"Memory"},"collection":{"external_id":"memory-a","kind":"memory_cards","name":"A"},"item":{"external_id":"card-unique","kind":"memory_card","created_at":"2026-01-01T00:00:02Z","text":"unique"},"relations":[],"raw":{"format":"json","path":"a-unique.json","ordinal":3}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"support","name":"Support"},"collection":{"external_id":"support:records","kind":"support","name":"support"},"item":{"external_id":"support:ambiguous","kind":"record","created_at":"2026-01-01T00:00:03Z","text":"ambiguous"},"relations":[{"type":"ambiguous","target":{"source":"brigade-memory","external_id":"card-shared"}}],"raw":{"format":"json","path":"ambiguous.json","ordinal":4}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"support","name":"Support"},"collection":{"external_id":"support:records","kind":"support","name":"support"},"item":{"external_id":"support:unique","kind":"record","created_at":"2026-01-01T00:00:04Z","text":"unique"},"relations":[{"type":"unique","target":{"source":"brigade-memory","external_id":"card-unique"}}],"raw":{"format":"json","path":"unique.json","ordinal":5}}`,
		`{"schema":"miseledger.adapter.v1","source":{"kind":"support","name":"Support"},"collection":{"external_id":"support:records","kind":"support","name":"support"},"item":{"external_id":"support:explicit","kind":"record","created_at":"2026-01-01T00:00:05Z","text":"explicit"},"relations":[{"type":"explicit","target":{"source":"brigade-memory","collection":"memory-b","external_id":"card-shared"}}],"raw":{"format":"json","path":"explicit.json","ordinal":6}}`,
	}, "\n") + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(records), "qualified-unique.jsonl", ""); err != nil {
		t.Fatal(err)
	}

	var ambiguous, unique, explicit string
	if err := db.QueryRow(`select coalesce(target_item_id, '') from relations where relation_type = 'ambiguous'`).Scan(&ambiguous); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select coalesce(target_item_id, '') from relations where relation_type = 'unique'`).Scan(&unique); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select coalesce(target_item_id, '') from relations where relation_type = 'explicit'`).Scan(&explicit); err != nil {
		t.Fatal(err)
	}
	if ambiguous != "" {
		t.Fatalf("collection-omitted shared target resolved to %q; want unresolved", ambiguous)
	}
	if unique == "" {
		t.Fatal("collection-omitted unique target stayed unresolved")
	}
	var explicitCollection string
	if err := db.QueryRow(`select c.external_id from items i join collections c on c.id = i.collection_id where i.id = ?`, explicit).Scan(&explicitCollection); err != nil {
		t.Fatal(err)
	}
	if explicitCollection != "memory-b" {
		t.Fatalf("explicit collection target resolved to %q; want memory-b", explicitCollection)
	}
}
