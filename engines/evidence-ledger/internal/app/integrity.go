package app

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/provenance"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

const (
	integrityOperatorCommand = "read:integrity.verify"
	integrityMismatchReason  = "integrity_mismatch"
	forensicMismatchWarning  = "integrity_mismatch: true; body revealed for forensic inspection only; trust unchanged"
)

type hashMismatch struct {
	Kind     string
	Expected string
	Actual   string
	Scope    string
	Artifact string
}

type integrityView struct {
	Envelope          map[string]any
	Display           string
	Warning           string
	TrustLabel        string
	Origin            string
	Modality          string
	InjectionStatus   string
	LegacyUnknown     bool
	IntegrityMismatch bool
	ParseError        bool
	Mismatches        []hashMismatch
}

type digestRef struct {
	raw     string
	present bool
	valid   bool
}

func inspectItemIntegrity(text, rawJSON, metadataJSON string, artifacts []map[string]any, verifyArtifactBodies bool) integrityView {
	metadata := decodeMetadata(metadataJSON)
	view := resolveReadEnvelope(metadata)
	view.Mismatches = verifyMaterializedHashes(text, rawJSON, view.Envelope, artifacts, verifyArtifactBodies)
	view.IntegrityMismatch = len(view.Mismatches) > 0
	return view
}

func resolveReadEnvelope(metadata map[string]any) integrityView {
	view := integrityView{
		Envelope:        map[string]any{},
		TrustLabel:      "unknown",
		Origin:          "unknown",
		Modality:        "unknown",
		InjectionStatus: "",
	}
	raw, has := metadata["provenance"]
	if !has || raw == nil {
		env, banner := provenance.SynthesizeLegacyProvenance()
		view.Envelope = envelopeToMap(env)
		view.Display = banner
		view.LegacyUnknown = true
		view.TrustLabel = env.Trust.Label
		view.Origin = env.Origin
		view.Modality = env.Modality
		view.InjectionStatus = env.Trust.Injection.Status
		return view
	}
	envMap := anyToMap(raw)
	view.Envelope = envMap
	// Display fields may come from the stored map so metadata remains visible
	// after a parse failure. Authorization never uses these raw strings.
	view.Origin = stringField(envMap, "origin")
	view.Modality = stringField(envMap, "modality")
	if trust := mapField(envMap, "trust"); trust != nil {
		if label := stringField(trust, "label"); label != "" {
			view.TrustLabel = label
		}
	}
	if parsed, err := ingest.ParseRetainableEnvelope(raw); err != nil {
		view.ParseError = true
		view.Warning = "malformed provenance: " + err.Error()
		view.InjectionStatus = ""
	} else {
		view.LegacyUnknown = isLegacyUnknownEnvelope(parsed)
		view.TrustLabel = parsed.Trust.Label
		view.Origin = parsed.Origin
		view.Modality = parsed.Modality
		view.InjectionStatus = parsed.Trust.Injection.Status
	}
	if view.TrustLabel == "" {
		view.TrustLabel = "unknown"
	}
	if view.LegacyUnknown && view.Display == "" {
		view.Display = provenance.LegacyDisplay
	}
	return view
}

func isLegacyUnknownEnvelope(env provenance.Envelope) bool {
	return env.Origin == "unknown" && env.Modality == "unknown" &&
		env.Attribution == "inferred" && env.Trust.Label == "unknown" &&
		(env.Hashes.Content == nil || *env.Hashes.Content == "")
}

func verifyMaterializedHashes(text, rawJSON string, envMap map[string]any, artifacts []map[string]any, verifyArtifactBodies bool) []hashMismatch {
	var out []hashMismatch
	hashes := mapField(envMap, "hashes")
	if content := inspectDigest(hashes, "content"); content.present {
		actual := provenance.ContentSHA256(text)
		if !content.valid || actual != content.raw {
			scope := stringField(hashes, "content_scope")
			if scope == "" {
				scope = "item.text.utf8.v1"
			}
			out = append(out, hashMismatch{Kind: "content", Expected: content.raw, Actual: actual, Scope: scope})
		}
	}
	if raw := inspectDigest(hashes, "raw"); raw.present && rawJSON != "" {
		actual := provenance.SHA256Bytes([]byte(rawJSON))
		if !raw.valid || actual != raw.raw {
			scope := stringField(hashes, "raw_scope")
			if scope == "" {
				scope = provenance.RawScope
			}
			out = append(out, hashMismatch{Kind: "raw", Expected: raw.raw, Actual: actual, Scope: scope})
		}
	}
	if !verifyArtifactBodies {
		return out
	}
	for _, art := range artifacts {
		out = append(out, verifyArtifactTextIntegrity(art)...)
	}
	return out
}

func verifyArtifactTextIntegrity(art map[string]any) []hashMismatch {
	text, _ := art["text"].(string)
	if strings.TrimSpace(text) == "" {
		return nil
	}
	id, _ := art["id"].(string)
	kind, _ := art["kind"].(string)
	url, _ := art["url"].(string)
	var out []hashMismatch
	textDigests := artifactTextDigests(art)
	if len(textDigests) == 0 {
		out = append(out, hashMismatch{Kind: "artifact", Expected: "", Actual: provenance.ContentSHA256(text), Scope: "artifact.text", Artifact: id})
	} else if !artifactTextMatches(text, textDigests) {
		out = append(out, hashMismatch{Kind: "artifact", Expected: textDigests[0], Actual: provenance.ContentSHA256(text), Scope: "artifact.text", Artifact: id})
	}
	if kind == "url" && url != "" {
		if urlDigest := artifactURLDigest(art); urlDigest != "" {
			actualURL := provenance.SHA256Bytes([]byte(url))
			if actualURL != urlDigest {
				out = append(out, hashMismatch{Kind: "artifact", Expected: urlDigest, Actual: actualURL, Scope: "artifact.url", Artifact: id})
			}
		}
	}
	return out
}

func artifactMetadata(art map[string]any) map[string]any {
	if rawMeta, ok := art["metadata"]; ok {
		if mapped := anyToMap(rawMeta); len(mapped) > 0 {
			return mapped
		}
	}
	raw := art["metadata_json"]
	if raw == nil {
		return map[string]any{}
	}
	switch v := raw.(type) {
	case string:
		return decodeMetadata(v)
	case []byte:
		return decodeMetadata(string(v))
	default:
		if mapped := anyToMap(v); len(mapped) > 0 {
			return mapped
		}
		return decodeMetadata(fmt.Sprint(v))
	}
}

func artifactTextDigests(art map[string]any) []string {
	var out []string
	seen := map[string]bool{}
	add := func(digest string) {
		if digest == "" || seen[digest] {
			return
		}
		seen[digest] = true
		out = append(out, digest)
	}
	meta := artifactMetadata(art)
	if rawEnv, ok := meta["provenance"]; ok && rawEnv != nil {
		artHashes := mapField(anyToMap(rawEnv), "hashes")
		if content := inspectDigest(artHashes, "content"); content.present && content.valid {
			add(content.raw)
		}
	}
	add(storedDigestString(meta["text_hash"]))
	kind, _ := art["kind"].(string)
	if kind != "url" {
		add(storedDigestString(art["content_hash"]))
	}
	return out
}

func artifactURLDigest(art map[string]any) string {
	if digest := storedDigestString(artifactMetadata(art)["url_hash"]); digest != "" {
		return digest
	}
	kind, _ := art["kind"].(string)
	if kind == "url" {
		return storedDigestString(art["content_hash"])
	}
	return ""
}

func artifactTextMatches(text string, digests []string) bool {
	exact := provenance.ContentSHA256(text)
	normalized := provenance.SHA256Bytes([]byte(textnorm.Normalize(text)))
	for _, digest := range digests {
		if digest == exact || digest == normalized {
			return true
		}
	}
	return false
}

func storedDigestString(raw any) string {
	if raw == nil {
		return ""
	}
	s, ok := raw.(string)
	if !ok {
		s = fmt.Sprint(raw)
	}
	s = strings.TrimSpace(s)
	if s == "" || s == "<nil>" {
		return ""
	}
	s = strings.TrimPrefix(s, "sha256:")
	if !canonicalHexDigest(s) {
		return ""
	}
	return s
}

func recordIntegrityMismatchEvents(db *sql.DB, itemID, fromLabel string, mismatches []hashMismatch) error {
	if db == nil || itemID == "" || len(mismatches) == 0 {
		return nil
	}
	if fromLabel == "" {
		fromLabel = "unknown"
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, mismatch := range mismatches {
		evidence := map[string]any{
			"reason":    integrityMismatchReason,
			"hash_kind": mismatch.Kind,
		}
		if mismatch.Artifact != "" {
			evidence["artifact_id"] = mismatch.Artifact
		}
		if err := ingest.AppendIdempotentProvenanceEvent(tx, itemID, fromLabel, fromLabel, mismatch.Expected, mismatch.Scope, integrityOperatorCommand, evidence); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func injectionBlocksForensic(status string) bool {
	return status != "clean"
}

func forensicRevealAllowed(view integrityView, opts showItemOpts) bool {
	if view.ParseError {
		return false
	}
	return opts.ForensicContent && !injectionBlocksForensic(view.InjectionStatus)
}

func contentEligible(view integrityView) bool {
	if view.ParseError || view.IntegrityMismatch || view.LegacyUnknown {
		return false
	}
	if view.TrustLabel == "unknown" || view.TrustLabel == "quarantined" {
		return false
	}
	return view.InjectionStatus == "clean"
}

func shouldOmitIntegrityBody(view integrityView, opts showItemOpts) bool {
	if view.ParseError {
		return true
	}
	if view.IntegrityMismatch && !forensicRevealAllowed(view, opts) {
		return true
	}
	if view.LegacyUnknown && !forensicRevealAllowed(view, opts) {
		return true
	}
	return false
}

func shouldOmitContentBody(view integrityView, opts showItemOpts) bool {
	if shouldOmitIntegrityBody(view, opts) {
		return true
	}
	if contentEligible(view) {
		return false
	}
	if forensicRevealAllowed(view, opts) {
		return false
	}
	return !opts.IncludeUntrustedBody
}

func inspectStoredItem(db *sql.DB, itemID string) (integrityView, error) {
	var text, metadataJSON, rawJSON string
	if err := db.QueryRow(`select coalesce(text,''), metadata_json, coalesce(raw_json,'') from items where id = ?`, itemID).Scan(&text, &metadataJSON, &rawJSON); err != nil {
		return integrityView{}, err
	}
	return inspectItemIntegrity(text, rawJSON, metadataJSON, nil, false), nil
}

func storedItemContentEligible(db *sql.DB, itemID string) bool {
	view, err := inspectStoredItem(db, itemID)
	if err != nil {
		return false
	}
	return contentEligible(view)
}

func attachIntegrityFields(out map[string]any, view integrityView) {
	if view.Envelope != nil {
		out["provenance"] = view.Envelope
	}
	out["integrity_mismatch"] = view.IntegrityMismatch
	if view.Origin != "" {
		out["origin"] = view.Origin
	}
	if view.Modality != "" {
		out["modality"] = view.Modality
	}
	if view.TrustLabel != "" {
		out["trust_label"] = view.TrustLabel
	}
	if view.Display != "" {
		out["provenance_display"] = view.Display
	}
	if view.Warning != "" {
		out["provenance_warning"] = view.Warning
	}
}

type itemQuerier interface {
	Query(query string, args ...any) (*sql.Rows, error)
}

type searchIntegrityItem struct {
	Text         string
	MetadataJSON string
	RawJSON      string
}

func searchResultIDs(results []SearchResult) []string {
	ids := make([]string, 0, len(results))
	seen := make(map[string]struct{}, len(results))
	for _, result := range results {
		if result.ID == "" {
			continue
		}
		if _, ok := seen[result.ID]; ok {
			continue
		}
		seen[result.ID] = struct{}{}
		ids = append(ids, result.ID)
	}
	return ids
}

func searchIntegrityLookupSQL(n int) string {
	return `select id, coalesce(text,''), metadata_json, coalesce(raw_json,'') from items where id in (` + placeholders(n) + `)`
}

func loadItemsForSearchIntegrity(db itemQuerier, ids []string) (map[string]searchIntegrityItem, error) {
	if len(ids) == 0 {
		return map[string]searchIntegrityItem{}, nil
	}
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	rows, err := db.Query(searchIntegrityLookupSQL(len(args)), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]searchIntegrityItem, len(ids))
	for rows.Next() {
		var id string
		var item searchIntegrityItem
		if err := rows.Scan(&id, &item.Text, &item.MetadataJSON, &item.RawJSON); err != nil {
			return nil, err
		}
		out[id] = item
	}
	return out, rows.Err()
}

func applySearchIntegrity(db *sql.DB, results []SearchResult) error {
	items, err := loadItemsForSearchIntegrity(db, searchResultIDs(results))
	if err != nil {
		return err
	}
	for i := range results {
		item, ok := items[results[i].ID]
		if !ok {
			continue
		}
		view := inspectItemIntegrity(item.Text, item.RawJSON, item.MetadataJSON, nil, false)
		results[i].Origin = view.Origin
		results[i].Modality = view.Modality
		results[i].TrustLabel = view.TrustLabel
		if !contentEligible(view) {
			redactIneligibleSearchResult(&results[i])
		}
		if view.IntegrityMismatch {
			results[i].IntegrityMismatch = true
			if err := recordIntegrityMismatchEvents(db, results[i].ID, view.TrustLabel, view.Mismatches); err != nil {
				return err
			}
		}
	}
	return nil
}

const ineligibleClosedFieldMax = 64

// Adapter Record.Validate only requires source/collection/item kind to be
// non-empty, so those columns are free-form attacker text. Ineligible
// projections may emit a value only when it matches these allowlists.
var (
	projectionSourceKinds = map[string]struct{}{
		"brigade": {}, "brigade-memory": {}, "chatgpt": {}, "claude": {},
		"claude-export": {}, "codex": {}, "cursor": {}, "discrawl": {},
		"discord": {}, "gmail": {}, "github": {}, "granola": {}, "grok": {},
		"hermes": {}, "notion": {}, "opencode": {}, "openclaw": {}, "pi": {},
		"slack": {}, "synthetic": {}, "telegram": {},
	}
	projectionCollectionKinds = map[string]struct{}{
		"agent_session": {}, "conversation": {}, "memory_cards": {},
		"messages": {}, "prompt_history": {}, "repository": {},
	}
	projectionItemKinds = map[string]struct{}{
		"memory_card": {}, "message": {}, "session_summary": {},
	}
	projectionOrigins = map[string]struct{}{
		"agent-session": {}, "external-service": {}, "external-web": {},
		"operator-input": {}, "unknown": {}, "workspace": {},
	}
	projectionModalities = map[string]struct{}{
		"external-web": {}, "human-written": {}, "mixed": {},
		"model-generated": {}, "tool-output": {}, "unknown": {},
	}
	projectionTrustLabels = map[string]struct{}{
		"quarantined": {}, "reviewed": {}, "unknown": {}, "untrusted": {},
		"verified": {},
	}
)

func redactIneligibleSearchResult(r *SearchResult) {
	r.Snippet = ""
	r.CollectionName = ""
	r.ActorName = ""
	r.ActorType = ""
	r.SourceKind = allowlistedProjectionField(r.SourceKind, projectionSourceKinds)
	r.CollectionKind = allowlistedProjectionField(r.CollectionKind, projectionCollectionKinds)
	r.Kind = allowlistedProjectionField(r.Kind, projectionItemKinds)
	r.CreatedAt = boundClosedField(r.CreatedAt, ineligibleClosedFieldMax)
	r.Origin = allowlistedProjectionField(r.Origin, projectionOrigins)
	r.Modality = allowlistedProjectionField(r.Modality, projectionModalities)
	r.TrustLabel = allowlistedProjectionField(r.TrustLabel, projectionTrustLabels)
	r.Score = boundClosedField(r.Score, 32)
}

func redactIneligibleBundleItem(item map[string]any) {
	item["snippet"] = ""
	item["source_kind"] = allowlistedProjectionField(stringFromAny(item["source_kind"]), projectionSourceKinds)
	item["kind"] = allowlistedProjectionField(stringFromAny(item["kind"]), projectionItemKinds)
	if col := anyToMap(item["collection"]); len(col) > 0 || item["collection"] != nil {
		col["name"] = ""
		col["kind"] = allowlistedProjectionField(stringFromAny(col["kind"]), projectionCollectionKinds)
		item["collection"] = col
	}
	if actor := anyToMap(item["actor"]); len(actor) > 0 || item["actor"] != nil {
		actor["name"] = ""
		actor["type"] = ""
		item["actor"] = actor
	}
}

func allowlistedProjectionField(value string, allowed map[string]struct{}) string {
	if _, ok := allowed[value]; ok {
		return value
	}
	return ""
}

func boundClosedField(s string, max int) string {
	if max <= 0 || len(s) <= max {
		return s
	}
	return s[:max]
}

const (
	cacheRefFreeFormMax      = 256
	cacheRefFilePathMax      = 256
	cacheRefQualifiedNameMax = 256
	cacheRefRepositoryMax    = 128
)

// cacheRefProjectedSearchOptStrings is the closed set of SearchOpts string
// fields that a cache-ref may contribute to a model-facing payload. Each
// field is drop-or-allowlist sanitized by sanitizeModelFacingEvidence.
// Origin/Modality/TrustLabel are search-only and must not be copied from the
// cache file. A newly added SearchOpts string field fails
// TestCacheRefProjectionStringInventory until it is classified here or in
// cacheRefNonProjectedSearchOptStrings.
var cacheRefProjectedSearchOptStrings = map[string]struct{}{
	"Query":      {},
	"Source":     {},
	"Collection": {},
	"Kind":       {},
	"ActorType":  {},
	"From":       {},
	"To":         {},
	"Project":    {},
	"Tags":       {},
}

// modelFacingEvidenceLiveKeys is the closed top-level shape of every
// model-facing evidence projection (show/materialize and list). Unknown
// cache-ref siblings never survive onto the payload.
var modelFacingEvidenceLiveKeys = map[string]struct{}{
	"id": {}, "resource_uri": {}, "query": {}, "filters": {},
	"generated_at": {}, "untrusted_context": {}, "results": {},
	"grouped_by_source": {}, "integrity_omitted": {},
	"integrity_mismatches": {}, "warnings": {}, "result_count": {},
}

var cacheRefNonProjectedSearchOptStrings = map[string]struct{}{
	"Origin":     {},
	"Modality":   {},
	"TrustLabel": {},
}

// cacheRefSearchOptJSONPaths maps each projected SearchOpts string field to
// the cache-ref / model-facing JSON path that must be sanitized.
var cacheRefSearchOptJSONPaths = map[string]string{
	"Query":      "query",
	"Source":     "filters.source",
	"Collection": "filters.collection",
	"Kind":       "filters.kind",
	"ActorType":  "filters.actor_type",
	"From":       "filters.from",
	"To":         "filters.to",
	"Project":    "filters.project",
	"Tags":       "filters.tags",
}

// sanitizeModelFacingEvidence is the single choke point for every
// model-facing evidence projection: materializeEvidenceBundle (item_ids
// and no-item_ids), evidence show / MCP show_evidence_bundle, and
// listEvidenceBundles. The cache file is same-UID writable, so every
// attacker-writable cache-ref-derived field is dropped or
// allowlist-validated recursively. A newly added free-form field cannot
// bypass this function: unknown keys are deleted and unclassified filter
// strings default to empty.
func sanitizeModelFacingEvidence(payload map[string]any, id string, authority []*CodeReference) {
	if payload == nil {
		return
	}
	for key := range payload {
		if _, ok := modelFacingEvidenceLiveKeys[key]; !ok {
			delete(payload, key)
		}
	}
	payload["id"] = id
	payload["resource_uri"] = evidenceBundleResourceURI(id)
	payload["query"] = ""
	if _, ok := payload["filters"]; ok {
		payload["filters"] = projectUntrustedCacheRefFilters(payload["filters"], authority)
	}
	if raw, ok := payload["generated_at"]; ok {
		payload["generated_at"] = projectCacheRefTimestamp(stringFromAny(raw))
	}
	if raw, ok := payload["result_count"]; ok {
		payload["result_count"] = intFromAny(raw)
	}
}

// projectUntrustedCacheRefFilters rebuilds model-facing filters from a
// same-UID-writable cache-ref. Unknown keys are dropped. Free-form strings
// are dropped or allowlist-validated; a length bound is not sufficient.
// code_reference is re-derived from eligible ledger records when present,
// otherwise re-validated.
func projectUntrustedCacheRefFilters(raw any, authority []*CodeReference) map[string]any {
	src := anyToMap(raw)
	out := map[string]any{}
	if projected := projectCacheRefCodeReference(src["code_reference"], authority); projected != nil {
		out["code_reference"] = projected
	}
	for key, value := range src {
		switch key {
		case "code_reference":
			// Applied from authority or sanitized cache-ref above.
		case "limit":
			out[key] = intFromAny(value)
		case "include_related", "include_artifact_text":
			out[key] = boolFromAny(value)
		case "collection":
			if isObject(value) {
				out[key] = projectUntrustedCacheRefCollection(value)
			} else {
				out[key] = projectCacheRefFilterString(key, stringFromAny(value))
			}
		case "source", "project", "from", "to", "kind", "actor_type", "tags":
			out[key] = projectCacheRefFilterString(key, stringFromAny(value))
		default:
			// Attacker-planted sibling keys stay off the model-facing payload.
		}
	}
	return out
}

func isObject(value any) bool {
	switch value.(type) {
	case map[string]any, *CodeReference, CodeReference:
		return true
	default:
		return false
	}
}

func projectUntrustedCacheRefCollection(raw any) map[string]any {
	src := anyToMap(raw)
	out := map[string]any{}
	out["kind"] = allowlistedProjectionField(stringFromAny(src["kind"]), projectionCollectionKinds)
	out["name"] = ""
	return out
}

func projectCacheRefFilterString(key, value string) string {
	switch key {
	case "source":
		return allowlistedProjectionField(value, projectionSourceKinds)
	case "kind":
		return allowlistedProjectionField(value, projectionItemKinds)
	case "from", "to":
		return projectCacheRefTimestamp(value)
	default:
		// project, tags, actor_type, collection, and any newly added
		// free-form filter key. A length bound is not an allowlist:
		// attacker text under cacheRefFreeFormMax must not survive.
		return ""
	}
}

func projectCacheRefTimestamp(s string) string {
	if s == "" || len(s) > ineligibleClosedFieldMax {
		return ""
	}
	if _, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return s
	}
	if _, err := time.Parse(time.RFC3339, s); err == nil {
		return s
	}
	return ""
}

func projectCacheRefCodeReference(raw any, authority []*CodeReference) *CodeReference {
	if len(authority) > 0 {
		if wanted := parseMaybeCodeReference(raw); wanted != nil {
			for _, ref := range authority {
				if codeReferenceClosedIdentityEqual(ref, wanted) {
					return ref
				}
			}
		}
		return authority[0]
	}
	return sanitizeCacheRefCodeReference(parseMaybeCodeReference(raw))
}

func codeReferenceClosedIdentityEqual(a, b *CodeReference) bool {
	if a == nil || b == nil {
		return false
	}
	return a.Schema == b.Schema && a.Repository == b.Repository &&
		a.Revision.Commit == b.Revision.Commit &&
		a.SymbolKind == b.SymbolKind && a.ChangeKind == b.ChangeKind
}

func parseMaybeCodeReference(raw any) *CodeReference {
	if raw == nil {
		return nil
	}
	if ref, ok := raw.(*CodeReference); ok {
		return sanitizeCacheRefCodeReference(ref)
	}
	if ref, ok := raw.(CodeReference); ok {
		return sanitizeCacheRefCodeReference(&ref)
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	parsed, err := parseCodeReferenceJSON(encoded)
	if err != nil {
		return nil
	}
	return sanitizeCacheRefCodeReference(parsed)
}

func sanitizeCacheRefCodeReference(ref *CodeReference) *CodeReference {
	if ref == nil {
		return nil
	}
	if err := validateCodeReference(*ref); err != nil {
		return nil
	}
	if !cacheRefFreeFormAccepted(ref.FilePath, cacheRefFilePathMax) {
		return nil
	}
	if !cacheRefFreeFormAccepted(ref.QualifiedName, cacheRefQualifiedNameMax) {
		return nil
	}
	if !cacheRefFreeFormAccepted(ref.Repository, cacheRefRepositoryMax) {
		return nil
	}
	out := *ref
	return &out
}

func cacheRefFreeFormAccepted(s string, max int) bool {
	if s == "" || max <= 0 || len(s) > max {
		return false
	}
	for _, r := range s {
		if r < 32 || r == 127 {
			return false
		}
	}
	return true
}

func authorityCodeReferences(db *sql.DB, bundle map[string]any) []*CodeReference {
	var out []*CodeReference
	for _, item := range bundleResultMaps(bundle) {
		id, _ := item["id"].(string)
		if id == "" || !storedItemContentEligible(db, id) {
			continue
		}
		var metadataJSON string
		if err := db.QueryRow(`select metadata_json from items where id = ?`, id).Scan(&metadataJSON); err != nil {
			continue
		}
		out = append(out, storedCodeReferences(metadataJSON)...)
	}
	return out
}

func storedCodeReferences(metadataJSON string) []*CodeReference {
	meta := decodeMetadata(metadataJSON)
	raw, ok := meta["code_references"]
	if !ok || raw == nil {
		return nil
	}
	encoded, err := json.Marshal(raw)
	if err != nil {
		return nil
	}
	var list []json.RawMessage
	if err := json.Unmarshal(encoded, &list); err != nil {
		return nil
	}
	var out []*CodeReference
	for _, item := range list {
		ref, err := parseCodeReferenceJSON(item)
		if err != nil {
			continue
		}
		if projected := sanitizeCacheRefCodeReference(ref); projected != nil {
			out = append(out, projected)
		}
	}
	return out
}

func decodeMetadata(raw string) map[string]any {
	if strings.TrimSpace(raw) == "" {
		return map[string]any{}
	}
	var metadata map[string]any
	if err := json.Unmarshal([]byte(raw), &metadata); err != nil || metadata == nil {
		return map[string]any{}
	}
	return metadata
}

func envelopeToMap(env provenance.Envelope) map[string]any {
	raw, err := json.Marshal(env)
	if err != nil {
		return map[string]any{}
	}
	return decodeMetadata(string(raw))
}

func anyToMap(raw any) map[string]any {
	if raw == nil {
		return map[string]any{}
	}
	if mapped, ok := raw.(map[string]any); ok {
		return mapped
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return map[string]any{}
	}
	return decodeMetadata(string(b))
}

func mapField(m map[string]any, key string) map[string]any {
	if m == nil {
		return nil
	}
	return anyToMap(m[key])
}

func stringField(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	if s, ok := m[key].(string); ok {
		return s
	}
	if m[key] == nil {
		return ""
	}
	return fmt.Sprint(m[key])
}

func inspectDigest(m map[string]any, key string) digestRef {
	if m == nil {
		return digestRef{}
	}
	raw, ok := m[key]
	if !ok || raw == nil {
		return digestRef{}
	}
	s, ok := raw.(string)
	if !ok {
		return digestRef{present: true, valid: false}
	}
	if s == "" {
		return digestRef{}
	}
	return digestRef{raw: s, present: true, valid: canonicalHexDigest(s)}
}

func canonicalHexDigest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func bundleResultMaps(bundle map[string]any) []map[string]any {
	raw, ok := bundle["results"]
	if !ok {
		return nil
	}
	switch results := raw.(type) {
	case []map[string]any:
		return results
	case []any:
		out := make([]map[string]any, 0, len(results))
		for _, item := range results {
			if mapped := anyToMap(item); len(mapped) > 0 || item != nil {
				out = append(out, mapped)
			}
		}
		return out
	default:
		return nil
	}
}
