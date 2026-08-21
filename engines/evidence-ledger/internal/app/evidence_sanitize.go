package app

import (
	"encoding/json"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	eligibilityEligible   = "eligible"
	eligibilityIneligible = "ineligible"

	reasonParseError        = "parse_error"
	reasonIntegrityMismatch = "integrity_mismatch"
	reasonLegacyUnknown     = "legacy_unknown"
	reasonTrustUnknown      = "trust_unknown"
	reasonTrustQuarantined  = "trust_quarantined"
	reasonInjectionNotClean = "injection_not_clean"
	reasonSourceMissing     = "source_missing"
)

type evidenceEligibility struct {
	Status string // "eligible" | "ineligible"
	Reason string // closed reason-code enum
}

type evidenceOutbound struct {
	Tree                any
	Decisions           map[string]evidenceEligibility
	IncludeArtifactText bool
}

type walkLevel int

const (
	walkBundleRoot walkLevel = iota
	walkListWrapper
	walkListEntry
	walkItem
	walkCollection
	walkActor
	walkRawRef
	walkArtifact
	walkRelated
	walkProvenance
	walkProvenanceSource
	walkProvenanceRepo
	walkProvenanceSession
	walkProvenanceLocator
	walkProvenanceTrust
	walkProvenanceInjection
	walkProvenanceHashes
	walkGrouped
	walkPass
)

var (
	evidenceBundleIDPattern  = regexp.MustCompile(`^[0-9a-f]{24}$`)
	evidenceCacheFilePattern = regexp.MustCompile(`^[0-9a-f]{24}\.json$`)
	contentHashPattern       = regexp.MustCompile(`^(?:sha256:)?[0-9a-f]{64}$`)
	resourceURIPrefix        = "miseledger://evidence/"
)

var allowedSourceKinds = map[string]struct{}{
	"synthetic": {}, "codex": {}, "claude": {}, "openclaw": {}, "hermes": {},
	"cursor": {}, "grok": {}, "opencode": {}, "pi": {}, "brigade": {},
	"brigade-memory": {}, "discrawl": {}, "native-scan": {}, "native-uncommitted": {},
	"adapter": {}, "gmail": {},
}

var allowedCollectionKinds = map[string]struct{}{
	"agent_session": {}, "memory_cards": {}, "memory_card": {},
	"brigade_receipts": {}, "receipts": {},
}

var allowedRelationTypes = map[string]struct{}{
	"derived_from": {}, "created_by": {}, "reinforced_by": {},
	"supported_by": {}, "supersedes": {}, "contradicts": {},
	"mentions": {}, "supports": {},
}

var allowedTrustLabels = map[string]struct{}{
	"unknown": {}, "untrusted": {}, "reviewed": {}, "verified": {}, "quarantined": {},
}

var bundleRootKeys = map[string]struct{}{
	"id": {}, "resource_uri": {}, "generated_at": {}, "regenerated_at": {},
	"results": {}, "grouped_by_source": {}, "integrity_omitted": {},
	"integrity_mismatches": {}, "warnings": {}, "untrusted_context": {},
	"result_count": {},
}

var listWrapperKeys = map[string]struct{}{
	"bundles": {},
}

var listEntryKeys = map[string]struct{}{
	"id": {}, "resource_uri": {}, "generated_at": {}, "result_count": {},
}

var eligibleItemKeys = map[string]struct{}{
	"id": {}, "external_id": {}, "snippet": {}, "timestamp": {},
	"source_kind": {}, "kind": {}, "score": {}, "collection": {},
	"actor": {}, "raw_ref": {}, "artifacts": {}, "related": {},
	"provenance": {}, "integrity_mismatch": {}, "origin": {},
	"modality": {}, "trust_label": {},
}

var collectionKeys = map[string]struct{}{
	"external_id": {}, "kind": {}, "name": {},
}

var actorKeys = map[string]struct{}{
	"external_id": {}, "type": {}, "name": {},
}

var rawRefKeys = map[string]struct{}{
	"path": {}, "hash": {}, "ordinal": {},
}

var artifactKeys = map[string]struct{}{
	"id": {}, "kind": {}, "path": {}, "url": {}, "mime_type": {},
	"text": {}, "content_hash": {},
}

var relatedKeys = map[string]struct{}{
	"relation_type": {}, "target_external_id": {}, "target_item_id": {},
	"target_kind": {}, "target_created_at": {},
}

var provenanceKeys = map[string]struct{}{
	"schema": {}, "schema_version": {}, "source": {}, "origin": {},
	"repository": {}, "session": {}, "collection_id": {}, "item_id": {},
	"locator": {}, "attribution": {}, "modality": {}, "trust": {},
	"hashes": {}, "captured_at": {}, "ingested_at": {},
}

var provenanceSourceKeys = map[string]struct{}{
	"system": {}, "kind": {}, "producer": {},
}

var provenanceRepoKeys = map[string]struct{}{
	"id": {}, "revision": {},
}

var provenanceSessionKeys = map[string]struct{}{
	"id": {}, "harness": {},
}

var provenanceLocatorKeys = map[string]struct{}{
	"kind": {}, "value": {},
}

var provenanceTrustKeys = map[string]struct{}{
	"label": {}, "assigned_by": {}, "assigned_at": {}, "policy": {}, "injection": {},
}

var provenanceInjectionKeys = map[string]struct{}{
	"status": {}, "count": {}, "rules": {},
}

var provenanceHashKeys = map[string]struct{}{
	"algo": {}, "content": {}, "content_scope": {}, "raw": {}, "raw_scope": {},
}

func reasonCode(view integrityView) string {
	if view.ParseError {
		return reasonParseError
	}
	if view.IntegrityMismatch {
		return reasonIntegrityMismatch
	}
	if view.LegacyUnknown {
		return reasonLegacyUnknown
	}
	if view.TrustLabel == "quarantined" {
		return reasonTrustQuarantined
	}
	if view.TrustLabel == "unknown" {
		return reasonTrustUnknown
	}
	if view.InjectionStatus != "clean" {
		return reasonInjectionNotClean
	}
	return ""
}

func ineligibleStub(id, reason string) map[string]any {
	return map[string]any{
		"id":                 id,
		"eligibility_status": eligibilityIneligible,
		"reason_code":        reason,
	}
}

func finalizeEvidenceResponse(out evidenceOutbound) ([]byte, error) {
	raw, err := json.Marshal(out.Tree)
	if err != nil {
		return nil, err
	}
	var tree any
	if err := json.Unmarshal(raw, &tree); err != nil {
		return nil, err
	}
	sanitized := walkEvidenceTree(tree, out, walkBundleRoot)
	return json.Marshal(sanitized)
}

func walkEvidenceTree(v any, out evidenceOutbound, level walkLevel) any {
	switch n := v.(type) {
	case map[string]any:
		return walkEvidenceObject(n, out, level)
	case []any:
		child := childLevel(level)
		items := make([]any, 0, len(n))
		for _, elem := range n {
			items = append(items, walkEvidenceTree(elem, out, child))
		}
		return items
	default:
		return v
	}
}

func childLevel(level walkLevel) walkLevel {
	switch level {
	case walkBundleRoot:
		return walkItem
	case walkListWrapper:
		return walkListEntry
	default:
		return level
	}
}

func walkEvidenceObject(m map[string]any, out evidenceOutbound, level walkLevel) any {
	if level == walkBundleRoot {
		if _, ok := m["bundles"]; ok {
			level = walkListWrapper
		} else if _, ok := m["results"]; !ok {
			if _, ok := m["result_count"]; ok {
				level = walkListEntry
			}
		}
	}
	if level == walkItem {
		return walkEvidenceItem(m, out)
	}
	allow := allowlistFor(level)
	if allow == nil {
		return nil
	}
	clean := make(map[string]any, len(m))
	for key, val := range m {
		if _, ok := allow[key]; !ok {
			continue
		}
		if walked, keep := walkAllowedField(key, val, out, level); keep {
			clean[key] = walked
		}
	}
	return clean
}

func walkEvidenceItem(m map[string]any, out evidenceOutbound) any {
	id, _ := m["id"].(string)
	if status, _ := m["eligibility_status"].(string); status == eligibilityIneligible {
		reason, _ := m["reason_code"].(string)
		if !validReasonCode(reason) {
			reason = reasonSourceMissing
		}
		return ineligibleStub(id, reason)
	}
	if decision, ok := out.Decisions[id]; ok && decision.Status != eligibilityEligible {
		reason := decision.Reason
		if !validReasonCode(reason) {
			reason = reasonSourceMissing
		}
		return ineligibleStub(id, reason)
	}
	if len(out.Decisions) > 0 {
		if _, ok := out.Decisions[id]; !ok && id != "" {
			return ineligibleStub(id, reasonSourceMissing)
		}
	}
	clean := make(map[string]any, len(m))
	for key, val := range m {
		if _, ok := eligibleItemKeys[key]; !ok {
			continue
		}
		if walked, keep := walkAllowedField(key, val, out, walkItem); keep {
			clean[key] = walked
		}
	}
	return clean
}

func allowlistFor(level walkLevel) map[string]struct{} {
	switch level {
	case walkBundleRoot:
		return bundleRootKeys
	case walkListWrapper:
		return listWrapperKeys
	case walkListEntry:
		return listEntryKeys
	case walkCollection:
		return collectionKeys
	case walkActor:
		return actorKeys
	case walkRawRef:
		return rawRefKeys
	case walkArtifact:
		return artifactKeys
	case walkRelated:
		return relatedKeys
	case walkProvenance:
		return provenanceKeys
	case walkProvenanceSource:
		return provenanceSourceKeys
	case walkProvenanceRepo:
		return provenanceRepoKeys
	case walkProvenanceSession:
		return provenanceSessionKeys
	case walkProvenanceLocator:
		return provenanceLocatorKeys
	case walkProvenanceTrust:
		return provenanceTrustKeys
	case walkProvenanceInjection:
		return provenanceInjectionKeys
	case walkProvenanceHashes:
		return provenanceHashKeys
	case walkGrouped, walkPass:
		return nil
	default:
		return nil
	}
}

func walkAllowedField(key string, val any, out evidenceOutbound, level walkLevel) (any, bool) {
	switch level {
	case walkBundleRoot:
		return walkBundleRootField(key, val, out)
	case walkListWrapper:
		return walkEvidenceTree(val, out, walkListEntry), true
	case walkListEntry:
		return walkListEntryField(key, val)
	case walkItem:
		return walkItemField(key, val, out)
	case walkCollection:
		return walkCollectionField(key, val)
	case walkActor:
		return val, true
	case walkRawRef:
		return walkRawRefField(key, val)
	case walkArtifact:
		return walkArtifactField(key, val, out)
	case walkRelated:
		return walkRelatedField(key, val)
	case walkProvenance:
		return walkProvenanceField(key, val, out)
	case walkProvenanceTrust:
		if key == "injection" {
			return walkEvidenceTree(val, out, walkProvenanceInjection), true
		}
		return val, true
	case walkProvenanceHashes:
		if key == "content" || key == "raw" {
			s, ok := val.(string)
			if !ok || !contentHashPattern.MatchString(s) {
				return nil, false
			}
		}
		return val, true
	default:
		return walkEvidenceTree(val, out, walkPass), true
	}
}

func walkBundleRootField(key string, val any, out evidenceOutbound) (any, bool) {
	switch key {
	case "id":
		s, ok := val.(string)
		if !ok || !evidenceBundleIDPattern.MatchString(s) {
			return nil, false
		}
		return s, true
	case "resource_uri":
		s, ok := val.(string)
		if !ok || !validResourceURI(s) {
			return nil, false
		}
		return s, true
	case "generated_at", "regenerated_at":
		s, ok := val.(string)
		if !ok || !validRFC3339(s) {
			return nil, false
		}
		return s, true
	case "results":
		return walkEvidenceTree(val, out, walkItem), true
	case "grouped_by_source":
		return walkGroupedBySource(val), true
	case "warnings":
		return walkStringSlice(val), true
	case "integrity_omitted", "integrity_mismatches", "result_count":
		if _, ok := coerceNumber(val); !ok {
			return nil, false
		}
		return val, true
	case "untrusted_context":
		if _, ok := val.(bool); !ok {
			return nil, false
		}
		return val, true
	default:
		return nil, false
	}
}

func walkListEntryField(key string, val any) (any, bool) {
	switch key {
	case "id":
		s, ok := val.(string)
		if !ok || !evidenceBundleIDPattern.MatchString(s) {
			return nil, false
		}
		return s, true
	case "resource_uri":
		s, ok := val.(string)
		if !ok || !validResourceURI(s) {
			return nil, false
		}
		return s, true
	case "generated_at":
		s, ok := val.(string)
		if !ok || !validRFC3339(s) {
			return nil, false
		}
		return s, true
	case "result_count":
		if _, ok := coerceNumber(val); !ok {
			return nil, false
		}
		return val, true
	default:
		return nil, false
	}
}

func walkItemField(key string, val any, out evidenceOutbound) (any, bool) {
	switch key {
	case "timestamp":
		s, ok := val.(string)
		if !ok || !validRFC3339(s) {
			return nil, false
		}
		return s, true
	case "source_kind":
		s, ok := val.(string)
		if !ok || !allowedEnum(allowedSourceKinds, s) {
			return nil, false
		}
		return s, true
	case "trust_label":
		s, ok := val.(string)
		if !ok || !allowedEnum(allowedTrustLabels, s) {
			return nil, false
		}
		return s, true
	case "score":
		n, ok := coerceNumber(val)
		if !ok {
			return nil, false
		}
		return n, true
	case "collection":
		return walkEvidenceTree(val, out, walkCollection), true
	case "actor":
		return walkEvidenceTree(val, out, walkActor), true
	case "raw_ref":
		return walkEvidenceTree(val, out, walkRawRef), true
	case "artifacts":
		return walkEvidenceTree(val, out, walkArtifact), true
	case "related":
		return walkEvidenceTree(val, out, walkRelated), true
	case "provenance":
		return walkEvidenceTree(val, out, walkProvenance), true
	case "integrity_mismatch":
		if _, ok := val.(bool); !ok {
			return nil, false
		}
		return val, true
	default:
		return val, true
	}
}

func walkCollectionField(key string, val any) (any, bool) {
	if key == "kind" {
		s, ok := val.(string)
		if !ok || !allowedEnum(allowedCollectionKinds, s) {
			return nil, false
		}
		return s, true
	}
	return val, true
}

func walkRawRefField(key string, val any) (any, bool) {
	if key == "hash" {
		s, ok := val.(string)
		if !ok || !contentHashPattern.MatchString(s) {
			return nil, false
		}
		return s, true
	}
	if key == "ordinal" {
		if _, ok := coerceNumber(val); !ok {
			return nil, false
		}
		return val, true
	}
	return val, true
}

func walkArtifactField(key string, val any, out evidenceOutbound) (any, bool) {
	if key == "text" {
		if !out.IncludeArtifactText {
			return nil, false
		}
		return val, true
	}
	if key == "content_hash" {
		s, ok := val.(string)
		if !ok || !contentHashPattern.MatchString(s) {
			return nil, false
		}
		return s, true
	}
	return val, true
}

func walkRelatedField(key string, val any) (any, bool) {
	switch key {
	case "relation_type":
		s, ok := val.(string)
		if !ok || !allowedEnum(allowedRelationTypes, s) {
			return nil, false
		}
		return s, true
	case "target_created_at":
		s, ok := val.(string)
		if !ok || s == "" {
			return nil, false
		}
		if !validRFC3339(s) {
			return nil, false
		}
		return s, true
	default:
		return val, true
	}
}

func walkProvenanceField(key string, val any, out evidenceOutbound) (any, bool) {
	switch key {
	case "source":
		return walkEvidenceTree(val, out, walkProvenanceSource), true
	case "repository":
		return walkEvidenceTree(val, out, walkProvenanceRepo), true
	case "session":
		return walkEvidenceTree(val, out, walkProvenanceSession), true
	case "locator":
		return walkEvidenceTree(val, out, walkProvenanceLocator), true
	case "trust":
		return walkEvidenceTree(val, out, walkProvenanceTrust), true
	case "hashes":
		return walkEvidenceTree(val, out, walkProvenanceHashes), true
	case "captured_at", "ingested_at":
		s, ok := val.(string)
		if !ok || !validRFC3339(s) {
			return nil, false
		}
		return s, true
	default:
		return val, true
	}
}

func walkGroupedBySource(val any) any {
	m, ok := val.(map[string]any)
	if !ok {
		return map[string]any{}
	}
	out := make(map[string]any, len(m))
	for key, raw := range m {
		if n, ok := coerceNumber(raw); ok {
			out[key] = n
		}
	}
	return out
}

func walkStringSlice(val any) []any {
	raw, ok := val.([]any)
	if !ok {
		return []any{}
	}
	out := make([]any, 0, len(raw))
	for _, item := range raw {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func validReasonCode(reason string) bool {
	switch reason {
	case reasonParseError, reasonIntegrityMismatch, reasonLegacyUnknown,
		reasonTrustUnknown, reasonTrustQuarantined, reasonInjectionNotClean,
		reasonSourceMissing:
		return true
	default:
		return false
	}
}

func validResourceURI(s string) bool {
	if !strings.HasPrefix(s, resourceURIPrefix) {
		return false
	}
	return evidenceBundleIDPattern.MatchString(s[len(resourceURIPrefix):])
}

func validRFC3339(s string) bool {
	if _, err := time.Parse(time.RFC3339Nano, s); err == nil {
		return true
	}
	_, err := time.Parse(time.RFC3339, s)
	return err == nil
}

func allowedEnum(set map[string]struct{}, value string) bool {
	_, ok := set[value]
	return ok
}

func coerceNumber(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case json.Number:
		f, err := n.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(n, 64)
		return f, err == nil
	default:
		return 0, false
	}
}
