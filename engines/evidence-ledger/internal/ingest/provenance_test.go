package ingest

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/provenance"
)

func TestImportProjectsProvenanceScalarsAndAppendsEvent(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:proj","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"project   provenance\nscalars","summary":"extra summary","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test"); err != nil {
		t.Fatal(err)
	}
	var itemID, contentHash, metadataJSON string
	if err := db.QueryRow(`select id, content_hash, metadata_json from items limit 1`).Scan(&itemID, &contentHash, &metadataJSON); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(contentHash, "sha256:") {
		t.Fatalf("items.content_hash = %q, want normalized sha256: prefix preserved", contentHash)
	}
	var meta map[string]any
	if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
		t.Fatal(err)
	}
	env := meta["provenance"].(map[string]any)
	exact := provenance.ContentSHA256("project   provenance\nscalars")
	if env["hashes"].(map[string]any)["content"] != exact {
		t.Fatalf("envelope content digest mismatch")
	}
	if contentHash == "sha256:"+exact {
		t.Fatalf("envelope exact digest must not equal normalized items.content_hash")
	}
	want := map[string]string{
		MetaKeyProvenanceOrigin:        "external-service",
		MetaKeyProvenanceModality:      "tool-output",
		MetaKeyProvenanceTrustLabel:    "quarantined",
		MetaKeyProvenanceContentScope:  "item.text.utf8.v1",
		MetaKeyProvenanceContentDigest: exact,
	}
	for key, value := range want {
		var got string
		if err := db.QueryRow(`select value from item_metadata where item_id = ? and key = ?`, itemID, key).Scan(&got); err != nil {
			t.Fatalf("missing projection %s: %v", key, err)
		}
		if got != value {
			t.Fatalf("projection %s = %q, want %q", key, got, value)
		}
	}
	var events int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, itemID).Scan(&events); err != nil {
		t.Fatal(err)
	}
	if events != 1 {
		t.Fatalf("provenance_events = %d, want 1 initial assignment", events)
	}
	var toLabel string
	if err := db.QueryRow(`select to_label from provenance_events where item_id = ?`, itemID).Scan(&toLabel); err != nil {
		t.Fatal(err)
	}
	if toLabel != "quarantined" {
		t.Fatalf("to_label = %q, want quarantined", toLabel)
	}
}

func TestTransitionTrustLabelAppendsImmutableEvent(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:trust","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"trust transition fixture","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test"); err != nil {
		t.Fatal(err)
	}
	var itemID string
	if err := db.QueryRow(`select id from items limit 1`).Scan(&itemID); err != nil {
		t.Fatal(err)
	}
	if err := TransitionTrustLabel(db, itemID, "untrusted", "scanner:demo.release", map[string]any{"reason": "clean-scan"}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, itemID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 2 {
		t.Fatalf("events = %d, want 2 (initial + transition)", count)
	}
	var fromLabel, toLabel, trustProj string
	if err := db.QueryRow(`select from_label, to_label from provenance_events where item_id = ? order by at desc limit 1`, itemID).Scan(&fromLabel, &toLabel); err != nil {
		t.Fatal(err)
	}
	if fromLabel != "quarantined" || toLabel != "untrusted" {
		t.Fatalf("transition = %q -> %q, want quarantined -> untrusted", fromLabel, toLabel)
	}
	if err := db.QueryRow(`select value from item_metadata where item_id = ? and key = ?`, itemID, MetaKeyProvenanceTrustLabel).Scan(&trustProj); err != nil {
		t.Fatal(err)
	}
	if trustProj != "untrusted" {
		t.Fatalf("trust projection = %q, want untrusted", trustProj)
	}
	// Events are immutable: transition never deletes prior rows.
	if err := TransitionTrustLabel(db, itemID, "untrusted", "scanner:demo.release", nil); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, itemID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 2 {
		t.Fatalf("idempotent same-label transition changed event count to %d", count)
	}
}

func TestBackfillProvenanceBatchedResumableIdempotent(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	now := "2026-06-03T00:00:00Z"
	if _, err := db.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values('src1','legacy-source','Legacy','1',?,?)`, now, now); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values('col1','src1','legacy:collection','agent_session','legacy','{}',?,?)`, now, now); err != nil {
		t.Fatal(err)
	}
	texts := []string{"legacy item one", "legacy item two", "legacy item three"}
	for i, text := range texts {
		id := "item" + string(rune('a'+i))
		bodyHash := "sha256:" + hashString(text+"\n")
		if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			id, "src1", "col1", "legacy:item:"+id, "message", now, now, text, "", bodyHash, `{"legacy":true}`, "sha256:"+hashString("raw"+id), "legacy.jsonl", i+1, `{}`); err != nil {
			t.Fatal(err)
		}
	}

	first, err := BackfillProvenance(db, 2, "")
	if err != nil {
		t.Fatal(err)
	}
	if first.Scanned != 2 || first.Updated != 2 || first.Remaining != 1 {
		t.Fatalf("first batch = %+v, want scanned=2 updated=2 remaining=1", first)
	}
	second, err := BackfillProvenance(db, 2, first.Cursor)
	if err != nil {
		t.Fatal(err)
	}
	if second.Scanned != 1 || second.Updated != 1 || second.Remaining != 0 {
		t.Fatalf("second batch = %+v, want scanned=1 updated=1 remaining=0", second)
	}
	var unknownTrust int
	if err := db.QueryRow(`select count(*) from item_metadata where key = ? and value = 'unknown'`, MetaKeyProvenanceTrustLabel).Scan(&unknownTrust); err != nil {
		t.Fatal(err)
	}
	if unknownTrust != 3 {
		t.Fatalf("unknown trust projections = %d, want 3", unknownTrust)
	}
	for _, id := range []string{"itema", "itemb", "itemc"} {
		var metadataJSON string
		if err := db.QueryRow(`select metadata_json from items where id = ?`, id).Scan(&metadataJSON); err != nil {
			t.Fatal(err)
		}
		var meta map[string]any
		if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
			t.Fatal(err)
		}
		trust := meta["provenance"].(map[string]any)["trust"].(map[string]any)
		if trust["label"] != "unknown" {
			t.Fatalf("%s trust label = %v, want unknown", id, trust["label"])
		}
		attr := meta["provenance"].(map[string]any)["attribution"]
		if attr != "inferred" {
			t.Fatalf("%s attribution = %v, want inferred", id, attr)
		}
	}
	third, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if third.Updated != 0 || third.Skipped != 3 {
		t.Fatalf("idempotent pass = %+v, want updated=0 skipped=3", third)
	}
}
