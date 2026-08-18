package ingest

import (
	"encoding/json"
	"strings"
	"sync"
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

func TestTransitionTrustLabelCycleRecordsEveryOccurrence(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:cycle","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"trust cycle fixture","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test"); err != nil {
		t.Fatal(err)
	}
	var itemID string
	if err := db.QueryRow(`select id from items limit 1`).Scan(&itemID); err != nil {
		t.Fatal(err)
	}
	op := "operator:brigade evidence trust review"
	// quarantined (ingest) -> untrusted -> quarantined -> untrusted
	for _, to := range []string{"untrusted", "quarantined", "untrusted"} {
		if err := TransitionTrustLabel(db, itemID, to, op, map[string]any{"cycle": true}); err != nil {
			t.Fatal(err)
		}
	}
	var count int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, itemID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	// initial ingest + three real transitions
	if count != 4 {
		t.Fatalf("events = %d, want 4 (initial + three cycle transitions)", count)
	}
	rows, err := db.Query(`select coalesce(from_label,''), to_label from provenance_events where item_id = ? order by at, id`, itemID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got [][2]string
	for rows.Next() {
		var fromLabel, toLabel string
		if err := rows.Scan(&fromLabel, &toLabel); err != nil {
			t.Fatal(err)
		}
		got = append(got, [2]string{fromLabel, toLabel})
	}
	want := [][2]string{
		{"", "quarantined"},
		{"quarantined", "untrusted"},
		{"untrusted", "quarantined"},
		{"quarantined", "untrusted"},
	}
	if len(got) != len(want) {
		t.Fatalf("transitions = %#v, want %#v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("transition[%d] = %v, want %v (full=%v)", i, got[i], want[i], got)
		}
	}
	// unchanged-label remains a no-op
	if err := TransitionTrustLabel(db, itemID, "untrusted", op, nil); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, itemID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 4 {
		t.Fatalf("same-label no-op changed event count to %d", count)
	}
}

func TestReviewTrustLabelRequiresExactCurrentDigest(t *testing.T) {
	db, err := archive.Open(t.TempDir() + "/miseledger.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := archive.Migrate(db); err != nil {
		t.Fatal(err)
	}
	jsonl := `{"schema":"miseledger.adapter.v1","source":{"kind":"reader-test","name":"Reader Test"},"collection":{"external_id":"reader:collection","kind":"agent_session","name":"reader"},"item":{"external_id":"reader:item:review","kind":"message","created_at":"2026-06-03T00:00:00Z","text":"review fixture text","tags":["reader"]},"actor":{"external_id":"reader:actor","type":"human","name":"reader"},"artifacts":[],"links":[],"relations":[],"raw":{"format":"json","path":"reader.jsonl","ordinal":1}}` + "\n"
	if _, err := ImportAdapterReader(db, strings.NewReader(jsonl), "reader://fixture", "reader-test"); err != nil {
		t.Fatal(err)
	}
	var itemID, text string
	if err := db.QueryRow(`select id, coalesce(text,'') from items limit 1`).Scan(&itemID, &text); err != nil {
		t.Fatal(err)
	}
	want := provenance.ContentSHA256(text)
	if err := ReviewTrustLabel(db, itemID, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "reviewed", "operator:brigade evidence trust review", nil); err == nil {
		t.Fatal("forged digest must not review")
	}
	if err := ReviewTrustLabel(db, itemID, want, "reviewed", "operator:brigade evidence trust review", map[string]any{"kind": "operator-review"}); err != nil {
		t.Fatal(err)
	}
	var label string
	if err := db.QueryRow(`select value from item_metadata where item_id = ? and key = ?`, itemID, MetaKeyProvenanceTrustLabel).Scan(&label); err != nil {
		t.Fatal(err)
	}
	if label != "reviewed" {
		t.Fatalf("trust label = %q, want reviewed", label)
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

func validHistoricalEnvelope(trustLabel string, contentDigest *string) map[string]any {
	at := "2026-06-03T00:00:00Z"
	hashes := map[string]any{
		"content_algorithm": "sha256",
		"content_scope":     "item.text.utf8.v1",
		"content":           nil,
		"raw_algorithm":     nil,
		"raw_scope":         nil,
		"raw":               nil,
	}
	if contentDigest != nil {
		hashes["content"] = *contentDigest
	}
	return map[string]any{
		"schema":         "brigade.provenance-envelope.v1",
		"schema_version": 1,
		"source":         map[string]any{"system": "miseledger", "kind": "legacy-source", "producer": "historical"},
		"origin":         "external-service",
		"repository":     map[string]any{"id": "unknown", "revision": nil},
		"session":        map[string]any{"id": nil, "harness": nil},
		"collection_id":  "legacy:collection",
		"item_id":        "legacy:item",
		"locator":        map[string]any{"kind": "uri", "value": "miseledger://legacy-source/legacy:collection/legacy:item"},
		"attribution":    "observed",
		"modality":       "tool-output",
		"trust": map[string]any{
			"label":       trustLabel,
			"assigned_by": "ingest:historical",
			"assigned_at": at,
			"trust_policy": map[string]any{
				"schema":         "brigade.trust-policy.v1",
				"schema_version": 1,
			},
			"injection": map[string]any{"status": "pending", "count": 0, "rules": []any{}},
		},
		"hashes":      hashes,
		"captured_at": at,
		"ingested_at": at,
	}
}

func TestBackfillPreservesValidEnvelopeWithNilOrMismatchedDigest(t *testing.T) {
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
	wrong := strings.Repeat("b", 64)
	cases := []struct {
		id       string
		text     string
		envelope map[string]any
	}{
		{"item-nil", "nil digest body", validHistoricalEnvelope("reviewed", nil)},
		{"item-mismatch", "mismatch digest body", validHistoricalEnvelope("verified", &wrong)},
	}
	for i, tc := range cases {
		meta, _ := json.Marshal(map[string]any{"provenance": tc.envelope})
		if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			tc.id, "src1", "col1", "legacy:item:"+tc.id, "message", now, now, tc.text, "", "sha256:"+hashString(tc.text+"\n"), `{}`, "sha256:"+hashString("raw"+tc.id), "legacy.jsonl", i+1, string(meta)); err != nil {
			t.Fatal(err)
		}
	}
	result, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Malformed != 0 {
		t.Fatalf("malformed = %d evidence=%v, want 0", result.Malformed, result.Evidence)
	}
	if result.Updated != 2 {
		t.Fatalf("updated = %d, want 2 projection repairs", result.Updated)
	}
	for _, tc := range cases {
		var metadataJSON string
		if err := db.QueryRow(`select metadata_json from items where id = ?`, tc.id).Scan(&metadataJSON); err != nil {
			t.Fatal(err)
		}
		var meta map[string]any
		if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
			t.Fatal(err)
		}
		trust := meta["provenance"].(map[string]any)["trust"].(map[string]any)
		want := tc.envelope["trust"].(map[string]any)["label"]
		if trust["label"] != want {
			t.Fatalf("%s trust reset to %v, want preserved %v", tc.id, trust["label"], want)
		}
		var events int
		if err := db.QueryRow(`select count(*) from provenance_events where item_id = ?`, tc.id).Scan(&events); err != nil {
			t.Fatal(err)
		}
		if events != 0 {
			t.Fatalf("%s grew provenance_events = %d, want 0 (no trust rewrite)", tc.id, events)
		}
		var trustProj string
		if err := db.QueryRow(`select value from item_metadata where item_id = ? and key = ?`, tc.id, MetaKeyProvenanceTrustLabel).Scan(&trustProj); err != nil {
			t.Fatal(err)
		}
		if trustProj != want {
			t.Fatalf("%s trust projection = %q, want %v", tc.id, trustProj, want)
		}
	}
}

func TestBackfillIsolatesMalformedProvenanceAndRemainsResumable(t *testing.T) {
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
	insert := func(id, text, metadataJSON string, ordinal int) {
		t.Helper()
		if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			id, "src1", "col1", "legacy:item:"+id, "message", now, now, text, "", "sha256:"+hashString(text+"\n"), `{}`, "sha256:"+hashString("raw"+id), "legacy.jsonl", ordinal, metadataJSON); err != nil {
			t.Fatal(err)
		}
	}
	insert("itema", "good before", `{}`, 1)
	badExact := provenance.ContentSHA256("matching but malformed")
	badEnv := validHistoricalEnvelope("quarantined", &badExact)
	badEnv["origin"] = "not-a-real-origin"
	badMeta, _ := json.Marshal(map[string]any{"provenance": badEnv})
	insert("itemb", "matching but malformed", string(badMeta), 2)
	insert("itemc", "good after", `{}`, 3)

	first, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatalf("batch aborted on malformed row: %v", err)
	}
	if first.Malformed != 1 || first.Updated != 2 || first.Skipped != 0 {
		t.Fatalf("first = %+v, want malformed=1 updated=2 skipped=0", first)
	}
	if len(first.Evidence) != 1 || first.Evidence[0].ItemID != "itemb" {
		t.Fatalf("evidence = %#v, want itemb", first.Evidence)
	}
	var badMetaAfter string
	if err := db.QueryRow(`select metadata_json from items where id = 'itemb'`).Scan(&badMetaAfter); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(badMetaAfter, "not-a-real-origin") {
		t.Fatalf("malformed envelope was rewritten: %s", badMetaAfter)
	}
	// Retry from start must still advance: malformed stays isolated, goods skip.
	second, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if second.Malformed != 1 || second.Updated != 0 || second.Skipped != 2 {
		t.Fatalf("retry = %+v, want malformed=1 updated=0 skipped=2", second)
	}
	// Resume after malformed cursor progresses past the bad row.
	third, err := BackfillProvenance(db, 10, "itemb")
	if err != nil {
		t.Fatal(err)
	}
	if third.Scanned != 1 || third.Cursor != "itemc" || third.Malformed != 0 {
		t.Fatalf("resume after malformed = %+v, want progress past itemb", third)
	}
}

func TestBackfillValidatesMatchingDigestBeforeRetain(t *testing.T) {
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
	text := "matching digest malformed trust"
	digest := provenance.ContentSHA256(text)
	env := validHistoricalEnvelope("trusted", &digest) // closed-set violation
	meta, _ := json.Marshal(map[string]any{"provenance": env})
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		"item-match-bad", "src1", "col1", "legacy:item:match-bad", "message", now, now, text, "", "sha256:"+hashString(text+"\n"), `{}`, "sha256:"+hashString("raw"), "legacy.jsonl", 1, string(meta)); err != nil {
		t.Fatal(err)
	}
	result, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Malformed != 1 || result.Updated != 0 {
		t.Fatalf("result = %+v, want matching-but-invalid treated as malformed", result)
	}
}

func TestBackfillConcurrentInferredEventsAreIdempotent(t *testing.T) {
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
	text := "concurrent backfill body"
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		"item-concurrent", "src1", "col1", "legacy:item:concurrent", "message", now, now, text, "", "sha256:"+hashString(text+"\n"), `{}`, "sha256:"+hashString("raw"), "legacy.jsonl", 1, `{}`); err != nil {
		t.Fatal(err)
	}

	const workers = 8
	var wg sync.WaitGroup
	errs := make(chan error, workers)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var err error
			for attempt := 0; attempt < 32; attempt++ {
				_, err = BackfillProvenance(db, 10, "")
				if err == nil || !strings.Contains(err.Error(), "SQLITE_BUSY") {
					break
				}
			}
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent backfill error: %v", err)
		}
	}
	var events int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = 'item-concurrent'`).Scan(&events); err != nil {
		t.Fatal(err)
	}
	if events != 1 {
		t.Fatalf("provenance_events = %d, want 1 idempotent inferred event", events)
	}
	var trust string
	if err := db.QueryRow(`select json_extract(metadata_json, '$.provenance.trust.label') from items where id = 'item-concurrent'`).Scan(&trust); err != nil {
		t.Fatal(err)
	}
	if trust != "unknown" {
		t.Fatalf("trust label = %q, want unknown", trust)
	}
}

func TestAppendProvenanceEventStableIDIsIdempotent(t *testing.T) {
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
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, metadata_json)
values('item-stable','src1','col1','legacy:item:stable','message',?,?,'text','','sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','{}','{}')`, now, now); err != nil {
		t.Fatal(err)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	digest := strings.Repeat("d", 64)
	for i := 0; i < 3; i++ {
		if err := AppendIdempotentProvenanceEvent(tx, "item-stable", "", "unknown", digest, "item.text.utf8.v1", "ingest:ingest.BackfillProvenance", map[string]any{"n": i}); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	var events int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = 'item-stable'`).Scan(&events); err != nil {
		t.Fatal(err)
	}
	if events != 1 {
		t.Fatalf("events = %d, want 1 from INSERT OR IGNORE stable id", events)
	}
}

func TestBackfillRepairsStaleContentDigestWhenEnvelopeHashNull(t *testing.T) {
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
	text := "null content digest with stale projection"
	env := validHistoricalEnvelope("reviewed", nil)
	meta, _ := json.Marshal(map[string]any{"provenance": env})
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		"item-stale-digest", "src1", "col1", "legacy:item:stale", "message", now, now, text, "", "sha256:"+hashString(text+"\n"), `{}`, "sha256:"+hashString("raw"), "legacy.jsonl", 1, string(meta)); err != nil {
		t.Fatal(err)
	}
	// Seed complete-looking projections including a stale content digest.
	for _, pair := range [][2]string{
		{MetaKeyProvenanceOrigin, "external-service"},
		{MetaKeyProvenanceModality, "tool-output"},
		{MetaKeyProvenanceTrustLabel, "reviewed"},
		{MetaKeyProvenanceContentScope, "item.text.utf8.v1"},
		{MetaKeyProvenanceContentDigest, strings.Repeat("c", 64)},
	} {
		if _, err := db.Exec(`insert into item_metadata(item_id, key, value) values(?,?,?)`, "item-stale-digest", pair[0], pair[1]); err != nil {
			t.Fatal(err)
		}
	}
	result, err := BackfillProvenance(db, 10, "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Updated != 1 {
		t.Fatalf("result = %+v, want updated=1 to clear stale digest", result)
	}
	var digestCount int
	if err := db.QueryRow(`select count(*) from item_metadata where item_id = ? and key = ?`, "item-stale-digest", MetaKeyProvenanceContentDigest).Scan(&digestCount); err != nil {
		t.Fatal(err)
	}
	if digestCount != 0 {
		t.Fatalf("stale provenance.content_digest still present (count=%d)", digestCount)
	}
	var trust string
	if err := db.QueryRow(`select json_extract(metadata_json, '$.provenance.trust.label') from items where id = 'item-stale-digest'`).Scan(&trust); err != nil {
		t.Fatal(err)
	}
	if trust != "reviewed" {
		t.Fatalf("trust reset to %q, want preserved reviewed", trust)
	}
}
