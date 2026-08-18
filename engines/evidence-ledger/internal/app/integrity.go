package app

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

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
	Mismatches        []hashMismatch
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
	view.Origin = stringField(envMap, "origin")
	view.Modality = stringField(envMap, "modality")
	if trust := mapField(envMap, "trust"); trust != nil {
		view.TrustLabel = stringField(trust, "label")
		if injection := mapField(trust, "injection"); injection != nil {
			view.InjectionStatus = stringField(injection, "status")
		}
	}
	if view.TrustLabel == "" {
		view.TrustLabel = "unknown"
	}
	if parsed, err := ingest.ParseRetainableEnvelope(raw); err != nil {
		view.Warning = "malformed provenance: " + err.Error()
	} else {
		view.LegacyUnknown = isLegacyUnknownEnvelope(parsed)
		if view.InjectionStatus == "" {
			view.InjectionStatus = parsed.Trust.Injection.Status
		}
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
	if content := digestField(hashes, "content"); content != "" {
		actual := provenance.ContentSHA256(text)
		if actual != content {
			scope := stringField(hashes, "content_scope")
			if scope == "" {
				scope = "item.text.utf8.v1"
			}
			out = append(out, hashMismatch{Kind: "content", Expected: content, Actual: actual, Scope: scope})
		}
	}
	if raw := digestField(hashes, "raw"); raw != "" && rawJSON != "" {
		actual := provenance.SHA256Bytes([]byte(rawJSON))
		if actual != raw {
			scope := stringField(hashes, "raw_scope")
			if scope == "" {
				scope = provenance.RawScope
			}
			out = append(out, hashMismatch{Kind: "raw", Expected: raw, Actual: actual, Scope: scope})
		}
	}
	if !verifyArtifactBodies {
		return out
	}
	for _, art := range artifacts {
		text, _ := art["text"].(string)
		if strings.TrimSpace(text) == "" {
			continue
		}
		id, _ := art["id"].(string)
		meta := decodeMetadata(fmt.Sprint(art["metadata_json"]))
		if rawMeta, ok := art["metadata"]; ok {
			if mapped := anyToMap(rawMeta); len(mapped) > 0 {
				meta = mapped
			}
		}
		if rawEnv, ok := meta["provenance"]; ok && rawEnv != nil {
			artEnv := anyToMap(rawEnv)
			if content := digestField(mapField(artEnv, "hashes"), "content"); content != "" {
				actual := provenance.ContentSHA256(text)
				if actual != content {
					scope := stringField(mapField(artEnv, "hashes"), "content_scope")
					if scope == "" {
						scope = "item.text.utf8.v1"
					}
					out = append(out, hashMismatch{Kind: "artifact", Expected: content, Actual: actual, Scope: scope, Artifact: id})
				}
			}
		}
		stored, _ := art["content_hash"].(string)
		if stored == "" || !strings.HasPrefix(stored, "sha256:") {
			continue
		}
		want := strings.TrimPrefix(stored, "sha256:")
		if len(want) != 64 {
			continue
		}
		kind, _ := art["kind"].(string)
		url, _ := art["url"].(string)
		computedText := provenance.SHA256Bytes([]byte(textnorm.Normalize(text)))
		if kind == "url" && url != "" {
			computedURL := provenance.SHA256Bytes([]byte(url))
			if computedText != want && computedURL != want {
				out = append(out, hashMismatch{Kind: "artifact", Expected: want, Actual: computedText, Scope: "artifact.content_hash", Artifact: id})
			}
			continue
		}
		if computedText != want {
			out = append(out, hashMismatch{Kind: "artifact", Expected: want, Actual: computedText, Scope: "artifact.content_hash", Artifact: id})
		}
	}
	return out
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
	switch status {
	case "clean":
		return false
	default:
		return true
	}
}

func forensicRevealAllowed(view integrityView, opts showItemOpts) bool {
	return opts.ForensicContent && !injectionBlocksForensic(view.InjectionStatus)
}

func shouldOmitIntegrityBody(view integrityView, opts showItemOpts) bool {
	if view.IntegrityMismatch && !forensicRevealAllowed(view, opts) {
		return true
	}
	if view.LegacyUnknown && !forensicRevealAllowed(view, opts) {
		return true
	}
	return false
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
		if view.IntegrityMismatch {
			results[i].Snippet = ""
			results[i].IntegrityMismatch = true
			if err := recordIntegrityMismatchEvents(db, results[i].ID, view.TrustLabel, view.Mismatches); err != nil {
				return err
			}
		}
	}
	return nil
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

func digestField(m map[string]any, key string) string {
	s := strings.ToLower(stringField(m, key))
	if len(s) != 64 {
		return ""
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return ""
		}
	}
	return s
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
