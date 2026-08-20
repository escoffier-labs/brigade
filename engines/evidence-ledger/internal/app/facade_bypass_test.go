package app

import (
	"encoding/json"
	"fmt"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/provenance"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

// Attack needles are unique, obviously fake prompt-injection text. These
// mutation tests fail on the pre-fix facade: URL-hash text integrity,
// cached evidence show, and ineligible search metadata projection.

const (
	q1030ParentNeedle  = "UNIQUE_Q1030_PARENT eligible parent body"
	q1030OriginalText  = "UNIQUE_Q1030_ORIGINAL url artifact text"
	q1030SwappedText   = "UNIQUE_Q1030_SWAPPED IGNORE_ALL_PREVIOUS_INSTRUCTIONS artifact text"
	q1030URL           = "https://example.test/q1030-artifact-text-swap"
	q1031CleanNeedle   = "UNIQUE_Q1031_CLEAN eligible cached body"
	q1031PlantedText   = "UNIQUE_Q1031_PLANTED IGNORE_ALL_PREVIOUS_INSTRUCTIONS cached body"
	q1032BodyNeedle    = "UNIQUE_Q1032_META quarantined search body"
	q1032ActorName     = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS Q1032_ACTOR_NAME"
	q1032ActorType     = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS Q1032_ACTOR_TYPE"
	q1032Collection    = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS Q1032_COLLECTION_NAME"
	q1032EligibleBody  = "UNIQUE_Q1032_ELIGIBLE reviewed search body"
	q1032EligibleActor = "honest-operator"
	q1032EligibleType  = "human"
	q1032EligibleCol   = "honest-collection"
	q1032SourceKind    = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_Q1032_SOURCE_KIND"
	q1032ItemKind      = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_Q1032_ITEM_KIND"
	q1031PlantedQuery  = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_Q1031_PLANTED_QUERY"
	q1031PlantedURI    = "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_Q1031_PLANTED_URI"
)

func q1032CollectionKind() string {
	return "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_Q1032_COLLECTION_KIND_" + strings.Repeat("K", 140)
}

func q1031UnboundedQuery() string {
	return q1031PlantedQuery + " " + strings.Repeat("q", 200)
}

func q1031UnboundedURI() string {
	return q1031PlantedURI + " " + strings.Repeat("u", 200)
}

func TestMutationURLArtifactTextSwapRejectedWhenURLHashUnchanged(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, q1030ParentNeedle, "reviewed", "clean")
	insertURLIntegrityArtifact(t, id, q1030URL, q1030OriginalText)
	swapArtifactText(t, id, q1030SwappedText)

	bundle := runJSON(t, "evidence", "UNIQUE_Q1030_PARENT", "--include-artifact-text", "--json")
	encoded, err := json.Marshal(bundle)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), q1030SwappedText) {
		t.Fatalf("bundle emitted swapped artifact text under unchanged URL hash: %s", encoded)
	}
	item := firstBundleResult(t, bundle)
	if item["integrity_mismatch"] != true {
		t.Fatalf("text swap must raise integrity_mismatch: %#v", item)
	}
	for _, art := range artifactMaps(item["artifacts"]) {
		if text, _ := art["text"].(string); strings.Contains(text, q1030SwappedText) {
			t.Fatalf("artifact text leaked after URL-hash swap: %#v", art)
		}
	}

	mcp, err := mcpEvidence(map[string]any{"query": "UNIQUE_Q1030_PARENT", "include_artifact_text": true})
	if err != nil {
		t.Fatal(err)
	}
	mcpEncoded, err := json.Marshal(mcpTextPayload(t, mcp))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(mcpEncoded), q1030SwappedText) {
		t.Fatalf("mcp bundle emitted swapped artifact text: %s", mcpEncoded)
	}
}

func TestEligibleURLArtifactTextEmittedWhenDigestsMatch(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, q1030ParentNeedle, "reviewed", "clean")
	insertURLIntegrityArtifact(t, id, q1030URL, q1030OriginalText)

	bundle := runJSON(t, "evidence", "UNIQUE_Q1030_PARENT", "--include-artifact-text", "--json")
	item := firstBundleResult(t, bundle)
	if item["integrity_mismatch"] == true {
		t.Fatalf("matching URL artifact flagged: %#v", item)
	}
	found := false
	for _, art := range artifactMaps(item["artifacts"]) {
		if text, _ := art["text"].(string); text == q1030OriginalText {
			found = true
		}
		if _, ok := art["metadata_json"]; ok {
			t.Fatalf("bundle leaked artifact metadata_json: %#v", art)
		}
	}
	if !found {
		t.Fatalf("matching URL artifact text omitted: %#v", item)
	}
}

func TestMutationURLHashNeverSatisfiesArtifactText(t *testing.T) {
	art := map[string]any{
		"id":           "art-q1030-unit",
		"kind":         "url",
		"url":          q1030URL,
		"text":         q1030SwappedText,
		"content_hash": "sha256:" + provenance.SHA256Bytes([]byte(q1030URL)),
	}
	mismatches := verifyMaterializedHashes("", "", map[string]any{}, []map[string]any{art}, true)
	if len(mismatches) == 0 {
		t.Fatal("URL hash satisfied text integrity; reverting the digest split must go red")
	}
	found := false
	for _, mismatch := range mismatches {
		if mismatch.Kind == "artifact" && mismatch.Artifact == "art-q1030-unit" {
			found = true
			if mismatch.Actual == provenance.SHA256Bytes([]byte(q1030URL)) {
				t.Fatalf("mismatch reported the URL digest as the text actual: %#v", mismatch)
			}
		}
	}
	if !found {
		t.Fatalf("expected artifact text mismatch, got %#v", mismatches)
	}
}

func TestMutationCachedShowBypassesLiveEligibilityWithoutRegen(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, q1031CleanNeedle, "reviewed", "clean")
	bundle := runJSON(t, "evidence", "UNIQUE_Q1031_CLEAN", "--json")
	bundleID, _ := bundle["id"].(string)
	if bundleID == "" {
		t.Fatalf("missing bundle id: %#v", bundle)
	}

	path, err := evidenceBundlePath(bundleID)
	if err != nil {
		t.Fatal(err)
	}
	cached, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(cached), q1031CleanNeedle) {
		t.Fatalf("evidence cache stored a model-facing body: %s", cached)
	}

	forged := map[string]any{
		"id":           bundleID,
		"query":        "UNIQUE_Q1031_CLEAN",
		"resource_uri": "miseledger://evidence/" + bundleID,
		"results": []map[string]any{{
			"id":      id,
			"snippet": q1031PlantedText,
			"artifacts": []map[string]any{{
				"id":   "art-forged",
				"text": q1031PlantedText,
			}},
		}},
	}
	forgedBytes, err := json.MarshalIndent(forged, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(forgedBytes, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}

	shown := runJSON(t, "evidence", "show", bundleID, "--json")
	shownBytes, err := json.Marshal(shown)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(shownBytes), q1031PlantedText) {
		t.Fatalf("evidence show emitted planted cache body: %s", shownBytes)
	}

	mcp, err := mcpEvidenceShow(map[string]any{"id": bundleID})
	if err != nil {
		t.Fatal(err)
	}
	mcpBytes, err := json.Marshal(mcpTextPayload(t, mcp))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(mcpBytes), q1031PlantedText) {
		t.Fatalf("show_evidence_bundle emitted planted cache body: %s", mcpBytes)
	}

	patchItemProvenance(t, id, func(env map[string]any) {
		trust := env["trust"].(map[string]any)
		trust["label"] = "quarantined"
		trust["injection"].(map[string]any)["status"] = "pending"
	})
	if _, err := openAndExec(t, `update item_metadata set value = ? where item_id = ? and key = ?`, "quarantined", id, ingest.MetaKeyProvenanceTrustLabel); err != nil {
		t.Fatal(err)
	}

	after := runJSON(t, "evidence", "show", bundleID, "--json")
	afterBytes, err := json.Marshal(after)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(afterBytes), q1031CleanNeedle) {
		t.Fatalf("cached show reused stale eligible snippet after quarantine: %s", afterBytes)
	}
	afterItem := firstBundleResult(t, after)
	if snippet, _ := afterItem["snippet"].(string); snippet != "" {
		t.Fatalf("quarantined cached show leaked snippet: %q", snippet)
	}
}

func TestMutationIneligibleSearchMetadataProjectionDropsFreeFormFields(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	ineligibleID := insertCleanIntegrityItem(t, q1032BodyNeedle, "quarantined", "pending")
	attachFreeFormProjection(t, ineligibleID, q1032Collection, q1032ActorType, q1032ActorName)
	attachHostileKinds(t, ineligibleID, q1032SourceKind, q1032CollectionKind(), q1032ItemKind)
	eligibleID := insertCleanIntegrityItem(t, q1032EligibleBody, "reviewed", "clean")
	attachEligibleProjection(t, eligibleID, q1032EligibleCol, q1032EligibleType, q1032EligibleActor)

	search := runJSON(t, "search", "UNIQUE_Q1032_META", "--json")
	assertProjectionHidesNeedles(t, search, "search")
	ineligibleHit := searchHitByID(t, search["results"], ineligibleID)
	if ineligibleHit["id"] != ineligibleID {
		t.Fatalf("search dropped ineligible id: %#v", search)
	}
	if ineligibleHit["trust_label"] != "quarantined" {
		t.Fatalf("search dropped closed-set trust: %#v", ineligibleHit)
	}
	assertIneligibleKindsDropped(t, ineligibleHit, "search hit")

	mcp, err := mcpSearch(map[string]any{"query": "UNIQUE_Q1032_META"})
	if err != nil {
		t.Fatal(err)
	}
	assertProjectionHidesNeedles(t, mcpTextPayload(t, mcp), "mcp search_evidence")

	bundle := runJSON(t, "evidence", "UNIQUE_Q1032_META", "--json")
	assertProjectionHidesNeedles(t, bundle, "evidence bundle")
	bundleItem := bundleItemByID(t, bundle, ineligibleID)
	if col := anyToMap(bundleItem["collection"]); strings.Contains(fmt.Sprint(col["name"]), "Q1032_COLLECTION_NAME") {
		t.Fatalf("bundle leaked collection.name: %#v", bundleItem)
	}
	if actor := anyToMap(bundleItem["actor"]); strings.Contains(fmt.Sprint(actor["name"]), "Q1032_ACTOR_NAME") || strings.Contains(fmt.Sprint(actor["type"]), "Q1032_ACTOR_TYPE") {
		t.Fatalf("bundle leaked actor free-form fields: %#v", bundleItem)
	}
	assertIneligibleKindsDropped(t, bundleItem, "evidence bundle item")
	if col := anyToMap(bundleItem["collection"]); strings.Contains(fmt.Sprint(col["kind"]), "Q1032_COLLECTION_KIND") {
		t.Fatalf("bundle leaked unbounded collection.kind: %#v", bundleItem)
	}

	explained := runJSON(t, "explain", "UNIQUE_Q1032_META", "--json")
	assertProjectionHidesNeedles(t, explained, "explain")

	eligibleSearch := runJSON(t, "search", "UNIQUE_Q1032_ELIGIBLE", "--json")
	eligibleHit := searchHitByID(t, eligibleSearch["results"], eligibleID)
	if eligibleHit["actor_name"] != q1032EligibleActor || eligibleHit["actor_type"] != q1032EligibleType || eligibleHit["collection_name"] != q1032EligibleCol {
		t.Fatalf("eligible search lost honest metadata: %#v", eligibleHit)
	}
}

func TestMutationIneligibleFreeFormKindsDroppedFromProjection(t *testing.T) {
	hostileCol := q1032CollectionKind()
	if len(hostileCol) < 200 {
		t.Fatalf("collection kind fixture too short to catch the unbounded bundle path: %d", len(hostileCol))
	}
	hit := SearchResult{
		ID:             "item-q1032-kinds",
		SourceKind:     q1032SourceKind,
		CollectionKind: hostileCol,
		Kind:           q1032ItemKind,
		TrustLabel:     "quarantined",
		Origin:         "workspace",
		Modality:       "tool-output",
	}
	redactIneligibleSearchResult(&hit)
	assertIneligibleKindsDropped(t, map[string]any{
		"source_kind":     hit.SourceKind,
		"collection_kind": hit.CollectionKind,
		"kind":            hit.Kind,
	}, "redactIneligibleSearchResult")
	if hit.TrustLabel != "quarantined" || hit.Origin != "workspace" || hit.Modality != "tool-output" {
		t.Fatalf("closed-set fields dropped: %#v", hit)
	}
	if hit.SourceKind != "" || hit.CollectionKind != "" || hit.Kind != "" {
		t.Fatalf("free-form kinds survived allowlist: %#v", hit)
	}
	honest := SearchResult{SourceKind: "synthetic", CollectionKind: "agent_session", Kind: "message", TrustLabel: "quarantined"}
	redactIneligibleSearchResult(&honest)
	if honest.SourceKind != "synthetic" || honest.CollectionKind != "agent_session" || honest.Kind != "message" {
		t.Fatalf("allowlisted kinds dropped on ineligible hit: %#v", honest)
	}

	item := map[string]any{
		"source_kind": q1032SourceKind,
		"kind":        q1032ItemKind,
		"snippet":     "should clear",
		"collection":  map[string]any{"kind": hostileCol, "name": q1032Collection, "external_id": "col"},
		"actor":       map[string]any{"name": q1032ActorName, "type": q1032ActorType},
	}
	redactIneligibleBundleItem(item)
	assertIneligibleKindsDropped(t, item, "redactIneligibleBundleItem")
	if col := anyToMap(item["collection"]); col["kind"] != "" || strings.Contains(fmt.Sprint(col["kind"]), "Q1032") {
		t.Fatalf("bundle collection.kind not allowlisted: %#v", item)
	}
}

func TestMutationCachedShowDropsCacheRefQueryAndResourceURI(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	_ = insertCleanIntegrityItem(t, q1031CleanNeedle, "reviewed", "clean")
	bundle := runJSON(t, "evidence", "UNIQUE_Q1031_CLEAN", "--json")
	bundleID, _ := bundle["id"].(string)
	if bundleID == "" {
		t.Fatalf("missing bundle id: %#v", bundle)
	}
	path, err := evidenceBundlePath(bundleID)
	if err != nil {
		t.Fatal(err)
	}
	plantedQuery := q1031UnboundedQuery()
	plantedURI := q1031UnboundedURI()
	forged := map[string]any{
		"schema":       evidenceBundleRefSchema,
		"id":           bundleID,
		"query":        plantedQuery,
		"resource_uri": plantedURI,
		"filters":      map[string]any{},
		"item_ids":     evidenceBundleItemIDs(bundle),
		"generated_at": "2026-08-20T00:00:00Z",
	}
	forgedBytes, err := json.MarshalIndent(forged, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(forgedBytes, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}

	shown := runJSON(t, "evidence", "show", bundleID, "--json")
	assertCacheRefFieldsHidden(t, shown, bundleID, plantedQuery, plantedURI, "evidence show")

	mcp, err := mcpEvidenceShow(map[string]any{"id": bundleID})
	if err != nil {
		t.Fatal(err)
	}
	assertCacheRefFieldsHidden(t, mcpTextPayload(t, mcp), bundleID, plantedQuery, plantedURI, "show_evidence_bundle")
}

func TestProjectMaterializedBundleIgnoresCacheRefQueryAndURI(t *testing.T) {
	id := "abc123def456abc123def456"
	bundle := map[string]any{
		"id":           "attacker-id",
		"query":        q1031UnboundedQuery(),
		"resource_uri": q1031UnboundedURI(),
		"results":      []map[string]any{},
	}
	projectMaterializedBundle(bundle, id, nil)
	assertCacheRefFieldsHidden(t, bundle, id, q1031UnboundedQuery(), q1031UnboundedURI(), "projectMaterializedBundle")
}

func opusCacheRefQualifiedNameCanary() string {
	prefix := "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_OPUS_QNAME_"
	return prefix + strings.Repeat("N", 2066-len(prefix))
}

func opusCacheRefFilePathCanary() string {
	prefix := "src/"
	suffix := ".py"
	return prefix + strings.Repeat("P", 2025-len(prefix)-len(suffix)) + suffix
}

func honestCacheRefCodeReference() CodeReference {
	return CodeReference{
		Schema:        "brigade.code-reference.v1",
		Repository:    "escoffier-labs/brigade",
		Revision:      CodeRevision{Commit: strings.Repeat("a", 40)},
		FilePath:      "src/brigade/receipts_cmd.py",
		QualifiedName: "brigade.receipts_cmd._metadata_with_delta",
		SymbolKind:    "function",
		SourceSpan:    SourceSpan{StartLine: 787, LineCount: 3},
		ChangeKind:    "changed",
	}
}

func plantedCacheRefCodeReference() map[string]any {
	qname := opusCacheRefQualifiedNameCanary()
	path := opusCacheRefFilePathCanary()
	return map[string]any{
		"schema":         "brigade.code-reference.v1",
		"repository":     "escoffier-labs/brigade",
		"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
		"file_path":      path,
		"qualified_name": qname,
		"symbol_kind":    "function",
		"source_span":    map[string]any{"start_line": 787, "line_count": 3},
		"change_kind":    "changed",
	}
}

// TestOpusCacheRefFiltersChannel is the independent re-review probe: cache-ref
// filters.code_reference.qualified_name and file_path were echoed verbatim
// (2066- and 2025-char payloads) into evidence show / show_evidence_bundle.
// Filters remain a channel for legitimate bounded values; the attacker
// payloads must not appear.
func TestOpusCacheRefFiltersChannel(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	itemID := insertCleanIntegrityItem(t, q1031CleanNeedle, "reviewed", "clean")
	honest := honestCacheRefCodeReference()
	attachItemCodeReference(t, itemID, honest)
	bundle := runJSON(t, "evidence", "UNIQUE_Q1031_CLEAN", "--json")
	bundleID, _ := bundle["id"].(string)
	if bundleID == "" {
		t.Fatalf("missing bundle id: %#v", bundle)
	}
	path, err := evidenceBundlePath(bundleID)
	if err != nil {
		t.Fatal(err)
	}
	planted := plantedCacheRefCodeReference()
	if got := len(planted["qualified_name"].(string)); got < 2066 {
		t.Fatalf("qualified_name canary too short: %d", got)
	}
	if got := len(planted["file_path"].(string)); got < 2025 {
		t.Fatalf("file_path canary too short: %d", got)
	}
	forged := map[string]any{
		"schema":       evidenceBundleRefSchema,
		"id":           bundleID,
		"query":        "UNIQUE_Q1031_CLEAN",
		"resource_uri": q1031UnboundedURI(),
		"filters": map[string]any{
			"source":         "synthetic",
			"project":        "workspace",
			"code_reference": planted,
		},
		"item_ids":     evidenceBundleItemIDs(bundle),
		"generated_at": "2026-08-20T00:00:00Z",
	}
	forgedBytes, err := json.MarshalIndent(forged, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, append(forgedBytes, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}

	shown := runJSON(t, "evidence", "show", bundleID, "--json")
	assertOpusCacheRefFiltersChannel(t, shown, bundleID, "evidence show")

	mcp, err := mcpEvidenceShow(map[string]any{"id": bundleID})
	if err != nil {
		t.Fatal(err)
	}
	assertOpusCacheRefFiltersChannel(t, mcpTextPayload(t, mcp), bundleID, "show_evidence_bundle")
}

func assertOpusCacheRefFiltersChannel(t *testing.T, payload map[string]any, bundleID, surface string) {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	qname := opusCacheRefQualifiedNameCanary()
	fpath := opusCacheRefFilePathCanary()
	for _, needle := range []string{qname, fpath, "UNIQUE_OPUS_QNAME_", strings.Repeat("P", 200), q1031UnboundedURI()} {
		if strings.Contains(got, needle) {
			t.Fatalf("%s echoed cache-ref filters channel %q: %s", surface, needle, got)
		}
	}
	if _, ok := payload["filters"]; !ok {
		t.Fatalf("%s dropped filters channel: %#v", surface, payload)
	}
	if payload["id"] != bundleID {
		t.Fatalf("%s id = %v, want path-validated %q", surface, payload["id"], bundleID)
	}
	filters := anyToMap(payload["filters"])
	ref := anyToMap(filters["code_reference"])
	honest := honestCacheRefCodeReference()
	if stringFromAny(ref["qualified_name"]) != honest.QualifiedName || stringFromAny(ref["file_path"]) != honest.FilePath {
		t.Fatalf("%s did not re-derive code_reference from the eligible ledger record: %#v", surface, filters["code_reference"])
	}
}

func TestProjectUntrustedCacheRefFiltersDropsEveryString(t *testing.T) {
	canary := "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_CACHE_REF_CANARY_"
	over := canary + strings.Repeat("Z", cacheRefFreeFormMax)
	filters := map[string]any{
		"source":        over,
		"project":       over,
		"from":          over,
		"to":            over,
		"collection":    map[string]any{"name": over, "kind": over, "external_id": over},
		"kind":          over,
		"actor_type":    over,
		"tags":          over,
		"extra_hostile": over,
		"code_reference": map[string]any{
			"schema":         over,
			"repository":     over,
			"revision":       map[string]any{"commit": over},
			"file_path":      "src/" + over + ".py",
			"qualified_name": over,
			"symbol_kind":    over,
			"change_kind":    over,
			"extra_hostile":  over,
		},
	}
	out := projectUntrustedCacheRefFilters(filters, nil)
	assertCacheRefCanaryAbsent(t, map[string]any{"filters": out}, over, "projectUntrustedCacheRefFilters")
	if _, ok := out["extra_hostile"]; ok {
		t.Fatalf("unknown cache-ref key survived projection: %#v", out)
	}
	if ref := anyToMap(out["code_reference"]); len(ref) != 0 {
		if _, ok := ref["extra_hostile"]; ok {
			t.Fatalf("unknown code_reference key survived projection: %#v", ref)
		}
	}
}

func TestCacheRefProjectionStringInventory(t *testing.T) {
	seen := map[string]struct{}{}
	for _, name := range exportedStringFields(reflect.TypeOf(SearchOpts{})) {
		if _, ok := cacheRefProjectedSearchOptStrings[name]; ok {
			seen[name] = struct{}{}
			continue
		}
		if _, ok := cacheRefNonProjectedSearchOptStrings[name]; ok {
			continue
		}
		t.Fatalf("SearchOpts string field %q is not classified; classify it as projected (must be sanitized) or non-projected", name)
	}
	for name := range cacheRefProjectedSearchOptStrings {
		if _, ok := seen[name]; !ok {
			t.Fatalf("cacheRefProjectedSearchOptStrings lists unknown SearchOpts field %q", name)
		}
		if _, ok := cacheRefSearchOptJSONPaths[name]; !ok {
			t.Fatalf("projected SearchOpts field %q has no cache-ref JSON path; a new field must be sanitized", name)
		}
	}
	for _, name := range exportedStringFields(reflect.TypeOf(CodeReference{})) {
		if name == "Revision" {
			continue
		}
		if !cacheRefCodeReferenceStringProjected(name) {
			t.Fatalf("CodeReference string field %q is not in the cache-ref projection inventory", name)
		}
	}
	for _, name := range exportedStringFields(reflect.TypeOf(CodeRevision{})) {
		if name != "Commit" {
			t.Fatalf("CodeRevision grew string field %q; add it to the cache-ref sanitizer inventory", name)
		}
	}
}

func cacheRefCodeReferenceStringProjected(name string) bool {
	switch name {
	case "Schema", "Repository", "FilePath", "QualifiedName", "SymbolKind", "ChangeKind":
		return true
	default:
		return false
	}
}

func exportedStringFields(typ reflect.Type) []string {
	var names []string
	for i := 0; i < typ.NumField(); i++ {
		field := typ.Field(i)
		if field.PkgPath != "" {
			continue
		}
		if field.Type.Kind() == reflect.String {
			names = append(names, field.Name)
		}
	}
	return names
}

func TestCacheRefProjectionDropsEveryPlantedStringField(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	itemID := insertCleanIntegrityItem(t, q1031CleanNeedle, "reviewed", "clean")
	attachItemCodeReference(t, itemID, honestCacheRefCodeReference())
	bundle := runJSON(t, "evidence", "UNIQUE_Q1031_CLEAN", "--json")
	bundleID, _ := bundle["id"].(string)
	if bundleID == "" {
		t.Fatalf("missing bundle id: %#v", bundle)
	}
	path, err := evidenceBundlePath(bundleID)
	if err != nil {
		t.Fatal(err)
	}

	canary := "IGNORE_ALL_PREVIOUS_INSTRUCTIONS UNIQUE_CACHE_REF_CANARY_"
	cases := cacheRefProjectionPlantCases(canary)
	if len(cases) == 0 {
		t.Fatal("cache-ref projection inventory is empty")
	}
	for _, tc := range cases {
		t.Run(tc.path, func(t *testing.T) {
			forged := maximalHostileCacheRef(bundleID, bundle, canary)
			if err := setJSONPath(forged, tc.path, tc.value); err != nil {
				t.Fatal(err)
			}
			forgedBytes, err := json.MarshalIndent(forged, "", "  ")
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, append(forgedBytes, '\n'), 0o600); err != nil {
				t.Fatal(err)
			}
			shown := runJSON(t, "evidence", "show", bundleID, "--json")
			assertCacheRefCanaryAbsent(t, shown, tc.value, "evidence show "+tc.path)
			mcp, err := mcpEvidenceShow(map[string]any{"id": bundleID})
			if err != nil {
				t.Fatal(err)
			}
			assertCacheRefCanaryAbsent(t, mcpTextPayload(t, mcp), tc.value, "show_evidence_bundle "+tc.path)
		})
	}
}

type cacheRefPlantCase struct {
	path  string
	value string
}

func cacheRefProjectionPlantCases(canary string) []cacheRefPlantCase {
	over := canary + strings.Repeat("Z", cacheRefFreeFormMax)
	cases := []cacheRefPlantCase{
		{path: "resource_uri", value: over},
		{path: "schema", value: over},
		{path: "generated_at", value: over},
		{path: "extra_hostile", value: over},
		{path: "filters.extra_hostile", value: over},
		{path: "filters.collection.name", value: over},
		{path: "filters.collection.kind", value: over},
		{path: "filters.collection.external_id", value: over},
		{path: "filters.code_reference.extra_hostile", value: over},
		{path: "filters.code_reference.revision.commit", value: over},
	}
	for name := range cacheRefProjectedSearchOptStrings {
		path, ok := cacheRefSearchOptJSONPaths[name]
		if !ok {
			continue
		}
		cases = append(cases, cacheRefPlantCase{path: path, value: over})
	}
	for _, name := range exportedStringFields(reflect.TypeOf(CodeReference{})) {
		jsonName := jsonFieldName(reflect.TypeOf(CodeReference{}), name)
		value := over
		if jsonName == "file_path" {
			value = "src/" + over + ".py"
		}
		cases = append(cases, cacheRefPlantCase{path: "filters.code_reference." + jsonName, value: value})
	}
	return cases
}

func jsonFieldName(typ reflect.Type, fieldName string) string {
	field, ok := typ.FieldByName(fieldName)
	if !ok {
		return fieldName
	}
	tag := field.Tag.Get("json")
	name, _, _ := strings.Cut(tag, ",")
	if name == "" || name == "-" {
		return fieldName
	}
	return name
}

func maximalHostileCacheRef(bundleID string, bundle map[string]any, canary string) map[string]any {
	over := canary + strings.Repeat("Z", cacheRefFreeFormMax)
	return map[string]any{
		"schema":        over,
		"id":            bundleID,
		"query":         "UNIQUE_Q1031_CLEAN",
		"resource_uri":  over,
		"generated_at":  over,
		"extra_hostile": over,
		"item_ids":      evidenceBundleItemIDs(bundle),
		"filters": map[string]any{
			"source":        over,
			"project":       over,
			"from":          over,
			"to":            over,
			"collection":    over,
			"kind":          over,
			"actor_type":    over,
			"tags":          over,
			"extra_hostile": over,
			"code_reference": map[string]any{
				"schema":         "brigade.code-reference.v1",
				"repository":     "escoffier-labs/brigade",
				"revision":       map[string]any{"commit": strings.Repeat("a", 40)},
				"file_path":      "src/brigade/receipts_cmd.py",
				"qualified_name": "brigade.receipts_cmd._metadata_with_delta",
				"symbol_kind":    "function",
				"source_span":    map[string]any{"start_line": 787, "line_count": 3},
				"change_kind":    "changed",
				"extra_hostile":  over,
			},
		},
	}
}

func setJSONPath(root map[string]any, path, value string) error {
	parts := strings.Split(path, ".")
	cur := root
	for i, part := range parts {
		if i == len(parts)-1 {
			cur[part] = value
			return nil
		}
		next, ok := cur[part]
		if !ok || next == nil {
			child := map[string]any{}
			cur[part] = child
			cur = child
			continue
		}
		child, ok := next.(map[string]any)
		if !ok {
			child = map[string]any{}
			cur[part] = child
		}
		cur = child
	}
	return nil
}

func assertCacheRefCanaryAbsent(t *testing.T, payload map[string]any, canary, surface string) {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	if strings.Contains(got, canary) {
		t.Fatalf("%s carried unbounded cache-ref canary: %s", surface, got)
	}
	if strings.Contains(got, "UNIQUE_CACHE_REF_CANARY_") {
		t.Fatalf("%s carried cache-ref canary token: %s", surface, got)
	}
	assertCacheRefPayloadBounded(t, payload["filters"], "filters")
}

func assertCacheRefPayloadBounded(t *testing.T, raw any, path string) {
	t.Helper()
	switch typed := raw.(type) {
	case map[string]any:
		for key, value := range typed {
			assertCacheRefPayloadBounded(t, value, path+"."+key)
		}
	case []any:
		for i, value := range typed {
			assertCacheRefPayloadBounded(t, value, fmt.Sprintf("%s[%d]", path, i))
		}
	case string:
		if len(typed) > cacheRefFreeFormMax {
			t.Fatalf("%s is unbounded (%d bytes): %q", path, len(typed), typed)
		}
		if strings.Contains(typed, "UNIQUE_CACHE_REF_CANARY_") || strings.Contains(typed, "IGNORE_ALL_PREVIOUS_INSTRUCTIONS") {
			t.Fatalf("%s echoed attacker canary: %q", path, typed)
		}
	case *CodeReference:
		assertCacheRefPayloadBounded(t, anyToMap(typed), path)
	}
}

func attachItemCodeReference(t *testing.T, itemID string, ref CodeReference) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	var metadataJSON string
	if err := db.QueryRow(`select metadata_json from items where id = ?`, itemID).Scan(&metadataJSON); err != nil {
		t.Fatal(err)
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(metadataJSON), &raw); err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal([]CodeReference{ref})
	if err != nil {
		t.Fatal(err)
	}
	raw["code_references"] = encoded
	out, err := json.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set metadata_json = ? where id = ?`, string(out), itemID); err != nil {
		t.Fatal(err)
	}
}

func insertURLIntegrityArtifact(t *testing.T, itemID, url, text string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	at := "2026-08-17T00:00:00Z"
	env, err := provenance.NewEvidenceEnvelope(provenance.EvidenceInput{
		SourceSystem: "miseledger", SourceKind: "synthetic", SourceProducer: "facade_bypass_test",
		Origin: "workspace", RepositoryID: "unknown",
		CollectionID: "integrity-col", ItemID: itemID,
		LocatorKind: "uri", LocatorValue: url,
		Attribution: "observed", Modality: "tool-output",
		TrustLabel: "reviewed", TrustAssignedBy: "test:facade", TrustAssignedAt: &at,
		InjectionStatus: "clean", InjectionRules: []string{},
		Text: text, CapturedAt: &at, IngestedAt: &at,
	})
	if err != nil {
		t.Fatal(err)
	}
	meta, err := json.Marshal(map[string]any{
		"provenance": env,
		"url_hash":   "sha256:" + provenance.SHA256Bytes([]byte(url)),
		"text_hash":  "sha256:" + provenance.SHA256Bytes([]byte(textnorm.Normalize(text))),
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?)`,
		"art-url-"+itemID, "integrity-src", itemID, "url:"+itemID, "url", "", url, "text/uri-list", text, "sha256:"+provenance.SHA256Bytes([]byte(url)), string(meta)); err != nil {
		t.Fatal(err)
	}
}

func swapArtifactText(t *testing.T, itemID, newText string) {
	t.Helper()
	if _, err := openAndExec(t, `update artifacts set text = ? where item_id = ?`, newText, itemID); err != nil {
		t.Fatal(err)
	}
}

func attachFreeFormProjection(t *testing.T, itemID, collectionName, actorType, actorName string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	if _, err := db.Exec(`update collections set name = ? where id = 'integrity-col'`, collectionName); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or replace into actors(id, source_id, external_id, type, name, metadata_json) values(?,?,?,?,?,?)`,
		"actor-"+itemID, "integrity-src", "actor:"+itemID, actorType, actorName, "{}"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set actor_id = ? where id = ?`, "actor-"+itemID, itemID); err != nil {
		t.Fatal(err)
	}
}

func attachHostileKinds(t *testing.T, itemID, sourceKind, collectionKind, itemKind string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	at := "2026-08-17T00:00:00Z"
	srcID := "hostile-src-" + itemID
	colID := "hostile-col-" + itemID
	if _, err := db.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)`,
		srcID, sourceKind, "Hostile", "1", at, at); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)`,
		colID, srcID, colID, collectionKind, q1032Collection, "{}", at, at); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set source_id = ?, collection_id = ?, kind = ? where id = ?`, srcID, colID, itemKind, itemID); err != nil {
		t.Fatal(err)
	}
}

func attachEligibleProjection(t *testing.T, itemID, collectionName, actorType, actorName string) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	if _, err := db.Exec(`insert or ignore into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)`,
		"eligible-col", "integrity-src", "eligible-col", "agent_session", collectionName, "{}", "2026-08-17T00:00:00Z", "2026-08-17T00:00:00Z"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update collections set name = ? where id = 'eligible-col'`, collectionName); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set collection_id = ? where id = ?`, "eligible-col", itemID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`insert or replace into actors(id, source_id, external_id, type, name, metadata_json) values(?,?,?,?,?,?)`,
		"actor-"+itemID, "integrity-src", "actor:"+itemID, actorType, actorName, "{}"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set actor_id = ? where id = ?`, "actor-"+itemID, itemID); err != nil {
		t.Fatal(err)
	}
}

func openAndExec(t *testing.T, query string, args ...any) (int64, error) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	res, err := db.Exec(query, args...)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

func assertProjectionHidesNeedles(t *testing.T, payload any, surface string) {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	for _, needle := range []string{q1032ActorName, q1032ActorType, q1032Collection, q1032SourceKind, q1032ItemKind, "UNIQUE_Q1032_COLLECTION_KIND"} {
		if strings.Contains(got, needle) {
			t.Fatalf("%s leaked free-form metadata %q: %s", surface, needle, got)
		}
	}
}

func assertIneligibleKindsDropped(t *testing.T, payload map[string]any, surface string) {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	for _, needle := range []string{q1032SourceKind, q1032ItemKind, "UNIQUE_Q1032_COLLECTION_KIND", "IGNORE_ALL_PREVIOUS_INSTRUCTIONS"} {
		if strings.Contains(got, needle) {
			t.Fatalf("%s leaked free-form kind %q: %s", surface, needle, got)
		}
	}
}

func assertCacheRefFieldsHidden(t *testing.T, payload map[string]any, bundleID, plantedQuery, plantedURI, surface string) {
	t.Helper()
	encoded, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	for _, needle := range []string{plantedQuery, plantedURI, q1031PlantedQuery, q1031PlantedURI} {
		if strings.Contains(got, needle) {
			t.Fatalf("%s echoed cache-ref field %q: %s", surface, needle, got)
		}
	}
	if payload["id"] != bundleID {
		t.Fatalf("%s id = %v, want path-validated %q", surface, payload["id"], bundleID)
	}
	wantURI := evidenceBundleResourceURI(bundleID)
	if payload["resource_uri"] != wantURI {
		t.Fatalf("%s resource_uri = %v, want reconstructed %q", surface, payload["resource_uri"], wantURI)
	}
	if query, _ := payload["query"].(string); query != "" && (strings.Contains(query, "UNIQUE_Q1031_PLANTED_QUERY") || len(query) > cacheRefQueryMax) {
		t.Fatalf("%s echoed cache-ref query: %q", surface, query)
	}
}

func searchHitByID(t *testing.T, raw any, id string) map[string]any {
	t.Helper()
	results, ok := raw.([]any)
	if !ok {
		t.Fatalf("results type %T", raw)
	}
	for _, row := range results {
		hit, _ := row.(map[string]any)
		if hit["id"] == id {
			return hit
		}
	}
	t.Fatalf("missing search hit %s in %#v", id, raw)
	return nil
}

func bundleItemByID(t *testing.T, bundle map[string]any, id string) map[string]any {
	t.Helper()
	for _, item := range bundleResultMaps(bundle) {
		if item["id"] == id {
			return item
		}
	}
	t.Fatalf("missing bundle item %s in %#v", id, bundle)
	return nil
}
