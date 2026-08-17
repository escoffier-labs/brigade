package app

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/provenance"
)

const forgedContentDigest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func TestSearchSuppressesMismatchedSnippetAndFlagsIntegrity(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "UNIQUE_INTEGRITY_NEEDLE matching body", "untrusted", "clean")
	forgeItemContentHash(t, id, forgedContentDigest)

	search := runJSON(t, "search", "UNIQUE_INTEGRITY_NEEDLE", "--json")
	results := search["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("search results = %#v", search)
	}
	hit := results[0].(map[string]any)
	if hit["integrity_mismatch"] != true {
		t.Fatalf("search hit missing integrity_mismatch: %#v", hit)
	}
	if snippet, _ := hit["snippet"].(string); snippet != "" {
		t.Fatalf("mismatched search snippet leaked: %q", snippet)
	}
	if hit["trust_label"] != "untrusted" {
		t.Fatalf("search changed trust: %#v", hit)
	}
	assertItemNotDeleted(t, id)
	assertIntegrityEventCount(t, id, 1)
}

func TestShowHidesMismatchedBodyAndForensicRevealsWithoutTrustChange(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "forensic show body", "reviewed", "clean")
	forgeItemContentHash(t, id, forgedContentDigest)

	hidden := runJSON(t, "show", id, "--json")
	if hidden["integrity_mismatch"] != true {
		t.Fatalf("show missing integrity_mismatch: %#v", hidden)
	}
	if _, ok := hidden["text"]; ok {
		t.Fatalf("default show leaked mismatched text: %#v", hidden)
	}
	if hidden["integrity_body_omitted"] != true {
		t.Fatalf("default show should omit mismatched body: %#v", hidden)
	}
	if hidden["trust_label"] != "reviewed" {
		t.Fatalf("show changed trust: %#v", hidden)
	}

	revealed := runJSON(t, "show", id, "--json", "--forensic-content")
	if revealed["integrity_mismatch"] != true {
		t.Fatalf("forensic show missing integrity_mismatch: %#v", revealed)
	}
	text, _ := revealed["text"].(string)
	if text != "forensic show body" {
		t.Fatalf("forensic show text = %q", text)
	}
	warning, _ := revealed["integrity_warning"].(string)
	if !strings.Contains(warning, "integrity_mismatch: true") || !strings.Contains(warning, "trust unchanged") {
		t.Fatalf("forensic warning = %q", warning)
	}
	if revealed["trust_label"] != "reviewed" {
		t.Fatalf("forensic show changed trust: %#v", revealed)
	}
	assertItemNotDeleted(t, id)
	assertIntegrityEventCount(t, id, 1)

	runJSON(t, "show", id, "--json", "--forensic-content")
	assertIntegrityEventCount(t, id, 1)
}

func TestForensicContentRevealsLegacyUnknownBannerAndBlocksInjection(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	legacyID := insertLegacyIntegrityItem(t, "legacy unknown body")
	pendingID := insertCleanIntegrityItem(t, "pending injection body", "quarantined", "pending")
	flaggedID := insertCleanIntegrityItem(t, "flagged injection body", "quarantined", "flagged")
	errorID := insertCleanIntegrityItem(t, "error injection body", "quarantined", "error")
	forgeItemContentHash(t, pendingID, forgedContentDigest)
	forgeItemContentHash(t, flaggedID, forgedContentDigest)
	forgeItemContentHash(t, errorID, forgedContentDigest)

	legacyHidden := runJSON(t, "show", legacyID, "--json")
	if legacyHidden["provenance_display"] != provenance.LegacyDisplay {
		t.Fatalf("legacy display = %#v", legacyHidden["provenance_display"])
	}
	if _, ok := legacyHidden["text"]; ok {
		t.Fatalf("default legacy show leaked body: %#v", legacyHidden)
	}
	legacyReveal := runJSON(t, "show", legacyID, "--json", "--forensic-content")
	if legacyReveal["text"] != "legacy unknown body" {
		t.Fatalf("forensic legacy text = %#v", legacyReveal["text"])
	}
	if legacyReveal["provenance_display"] != provenance.LegacyDisplay {
		t.Fatalf("forensic legacy missing banner: %#v", legacyReveal)
	}
	if legacyReveal["trust_label"] != "unknown" {
		t.Fatalf("forensic legacy changed trust: %#v", legacyReveal)
	}

	for _, id := range []string{pendingID, flaggedID, errorID} {
		revealed := runJSON(t, "show", id, "--json", "--forensic-content")
		if _, ok := revealed["text"]; ok {
			t.Fatalf("forensic reveal leaked injection-blocked body for %s: %#v", id, revealed)
		}
		if revealed["integrity_mismatch"] != true {
			t.Fatalf("injection-blocked mismatch not flagged for %s: %#v", id, revealed)
		}
	}
}

func TestHTTPAndMCPHaveNoForensicRevealPath(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "http mcp mismatch body", "untrusted", "clean")
	forgeItemContentHash(t, id, forgedContentDigest)

	handler := newHTTPHandler()
	req := httptest.NewRequest(http.MethodGet, "/items/"+id+"?forensic_content=true&include_untrusted_body=true", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("http show status=%d body=%s", rec.Code, rec.Body.String())
	}
	var httpItem map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &httpItem); err != nil {
		t.Fatal(err)
	}
	if httpItem["integrity_mismatch"] != true {
		t.Fatalf("http show missing integrity_mismatch: %#v", httpItem)
	}
	if _, ok := httpItem["text"]; ok {
		t.Fatalf("http forensic query leaked mismatched body: %#v", httpItem)
	}

	mcp, err := mcpShow(map[string]any{"id": id, "include_untrusted_body": true, "forensic_content": true})
	if err != nil {
		t.Fatal(err)
	}
	payload := mcpTextPayload(t, mcp)
	if payload["integrity_mismatch"] != true {
		t.Fatalf("mcp show missing integrity_mismatch: %#v", payload)
	}
	if _, ok := payload["text"]; ok {
		t.Fatalf("mcp leaked mismatched body: %#v", payload)
	}
}

func TestEvidenceBundleMarkdownAndCachePreserveEnvelopeAndOmissions(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "UNIQUE_BUNDLE_NEEDLE bundle body", "untrusted", "clean")
	forgeItemContentHash(t, id, forgedContentDigest)

	bundle := runJSON(t, "evidence", "UNIQUE_BUNDLE_NEEDLE", "--json")
	if bundle["integrity_omitted"].(float64) != 1 {
		t.Fatalf("bundle integrity_omitted = %#v", bundle["integrity_omitted"])
	}
	results := bundle["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("bundle results = %#v", bundle)
	}
	item := results[0].(map[string]any)
	if item["integrity_mismatch"] != true || item["origin"] != "workspace" || item["trust_label"] != "untrusted" {
		t.Fatalf("bundle item envelope fields = %#v", item)
	}
	if snippet, _ := item["snippet"].(string); snippet != "" {
		t.Fatalf("bundle leaked mismatched snippet: %q", snippet)
	}

	code, markdown, errb := run("evidence", "UNIQUE_BUNDLE_NEEDLE", "--markdown")
	if code != 0 {
		t.Fatalf("markdown evidence failed: %s %s", errb, markdown)
	}
	if !strings.Contains(markdown, "Integrity omitted: 1") {
		t.Fatalf("markdown missing omission counter: %s", markdown)
	}
	if !strings.Contains(markdown, "Integrity mismatch: true") {
		t.Fatalf("markdown missing mismatch field: %s", markdown)
	}
	if strings.Contains(markdown, "UNIQUE_BUNDLE_NEEDLE bundle body") {
		t.Fatalf("markdown leaked mismatched body: %s", markdown)
	}

	cached := runJSON(t, "evidence", "show", bundle["id"].(string), "--json")
	if cached["integrity_omitted"].(float64) != 1 {
		t.Fatalf("cached bundle dropped integrity_omitted: %#v", cached)
	}
	cachedItem := cached["results"].([]any)[0].(map[string]any)
	if cachedItem["integrity_mismatch"] != true || cachedItem["origin"] != "workspace" {
		t.Fatalf("cached bundle dropped envelope fields: %#v", cachedItem)
	}
	assertItemNotDeleted(t, id)
	assertIntegrityEventCount(t, id, 1)
}

func TestIntegrityMismatchDoesNotDowngradeTrustLabel(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, "no downgrade label", "verified", "clean")
	forgeItemContentHash(t, id, forgedContentDigest)
	show := runJSON(t, "show", id, "--json")
	if show["trust_label"] != "verified" {
		t.Fatalf("trust label changed: %#v", show)
	}
	db := openTestDB(t)
	defer db.Close()
	var projected string
	if err := db.QueryRow(`select value from item_metadata where item_id = ? and key = ?`, id, ingest.MetaKeyProvenanceTrustLabel).Scan(&projected); err != nil {
		t.Fatal(err)
	}
	if projected != "verified" {
		t.Fatalf("projected trust = %q", projected)
	}
}

func insertCleanIntegrityItem(t *testing.T, text, trust, injection string) string {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	ensureIntegrityParents(t, db)
	id := "item-" + provenance.ContentSHA256(text)[:12]
	at := "2026-08-17T00:00:00Z"
	raw := []byte(`{"fixture":"` + id + `"}`)
	env, err := provenance.NewEvidenceEnvelope(provenance.EvidenceInput{
		SourceSystem: "miseledger", SourceKind: "synthetic", SourceProducer: "integrity_test",
		Origin: "workspace", RepositoryID: "unknown",
		CollectionID: "integrity-col", ItemID: id,
		LocatorKind: "uri", LocatorValue: "miseledger://synthetic/integrity-col/" + id,
		Attribution: "observed", Modality: "tool-output",
		TrustLabel: trust, TrustAssignedBy: "test:integrity", TrustAssignedAt: &at,
		InjectionStatus: injection, InjectionRules: []string{},
		Text: text, RawBytes: raw, CapturedAt: &at, IngestedAt: &at,
	})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := json.Marshal(map[string]any{"provenance": env})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "integrity-src", "integrity-col", "ext:"+id, "message", at, at, text, "", "sha256:"+strings.Repeat("c", 64), string(raw), "sha256:"+provenance.SHA256Bytes(raw), "integrity.jsonl", 1, string(meta)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or ignore into item_metadata(item_id, key, value) values(?,?,?)`, id, ingest.MetaKeyProvenanceTrustLabel, trust); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`, id, "synthetic", "agent_session", "message", "agent", text); err != nil {
		t.Fatal(err)
	}
	return id
}

func insertLegacyIntegrityItem(t *testing.T, text string) string {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	ensureIntegrityParents(t, db)
	id := "legacy-" + provenance.ContentSHA256(text)[:12]
	at := "2026-08-17T00:00:00Z"
	if _, err := db.Exec(`insert into items(id, source_id, collection_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		id, "integrity-src", "integrity-col", "ext:"+id, "message", at, at, text, "", "sha256:"+strings.Repeat("d", 64), "{}", "sha256:"+strings.Repeat("e", 64), "legacy.jsonl", 1, "{}"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`, id, "synthetic", "agent_session", "message", "agent", text); err != nil {
		t.Fatal(err)
	}
	return id
}

func ensureIntegrityParents(t *testing.T, db *sql.DB) {
	t.Helper()
	at := "2026-08-17T00:00:00Z"
	if _, err := db.Exec(`insert or ignore into sources(id, kind, name, version, created_at, updated_at) values('integrity-src','synthetic','Integrity','1',?,?)`, at, at); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or ignore into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values('integrity-col','integrity-src','integrity-col','agent_session','integrity','{}',?,?)`, at, at); err != nil {
		t.Fatal(err)
	}
}

func forgeItemContentHash(t *testing.T, itemID, digest string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	var metadataJSON string
	if err := db.QueryRow(`select metadata_json from items where id = ?`, itemID).Scan(&metadataJSON); err != nil {
		t.Fatal(err)
	}
	meta := map[string]any{}
	if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
		t.Fatal(err)
	}
	env := meta["provenance"].(map[string]any)
	hashes := env["hashes"].(map[string]any)
	hashes["content"] = digest
	updated, err := json.Marshal(meta)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set metadata_json = ? where id = ?`, string(updated), itemID); err != nil {
		t.Fatal(err)
	}
}

func assertItemNotDeleted(t *testing.T, itemID string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	var n int
	if err := db.QueryRow(`select count(*) from items where id = ?`, itemID).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("item %s deleted; count=%d", itemID, n)
	}
}

func assertIntegrityEventCount(t *testing.T, itemID string, want int) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	var n int
	if err := db.QueryRow(`select count(*) from provenance_events where item_id = ? and operator_command = ?`, itemID, integrityOperatorCommand).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != want {
		t.Fatalf("integrity events = %d, want %d", n, want)
	}
}

func mcpTextPayload(t *testing.T, result map[string]any) map[string]any {
	t.Helper()
	content := result["content"].([]map[string]any)
	var payload map[string]any
	if err := json.Unmarshal([]byte(content[0]["text"].(string)), &payload); err != nil {
		t.Fatal(err)
	}
	return payload
}
