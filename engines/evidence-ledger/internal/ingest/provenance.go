package ingest

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/provenance"
)

// Indexed provenance projections in item_metadata. Search and SQL filter these
// keys without parsing nested metadata_json.provenance.
const (
	MetaKeyProvenanceOrigin        = "provenance.origin"
	MetaKeyProvenanceModality      = "provenance.modality"
	MetaKeyProvenanceTrustLabel    = "provenance.trust_label"
	MetaKeyProvenanceContentScope  = "provenance.content_scope"
	MetaKeyProvenanceContentDigest = "provenance.content_digest"
)

// ProvenanceEventSchema is the append-only trust-transition event schema.
const ProvenanceEventSchema = "brigade.provenance-event.v1"

// ProvenanceEvent is an immutable trust transition row.
type ProvenanceEvent struct {
	Schema               string         `json:"schema"`
	SchemaVersion        int            `json:"schema_version"`
	At                   string         `json:"at"`
	ItemRef              string         `json:"item_ref"`
	FromLabel            string         `json:"from_label"`
	ToLabel              string         `json:"to_label"`
	EnvelopeContentHash  string         `json:"envelope_content_hash"`
	ContentScope         string         `json:"content_scope"`
	OperatorCommand      string         `json:"operator_command"`
	Evidence             map[string]any `json:"evidence"`
}

// BackfillProvenanceResult reports one resumable backfill batch.
type BackfillProvenanceResult struct {
	Scanned    int                       `json:"scanned"`
	Updated    int                       `json:"updated"`
	Skipped    int                       `json:"skipped"`
	Malformed  int                       `json:"malformed"`
	Events     int                       `json:"events"`
	Cursor     string                    `json:"cursor"`
	Remaining  int                       `json:"remaining"`
	Evidence   []BackfillItemEvidence    `json:"evidence,omitempty"`
}

// BackfillItemEvidence is bounded per-item evidence for isolated malformed rows.
type BackfillItemEvidence struct {
	ItemID string `json:"item_id"`
	Reason string `json:"reason"`
}

const maxBackfillEvidence = 32

type backfillOutcome int

const (
	backfillSkipped backfillOutcome = iota
	backfillUpdated
	backfillMalformed
)

func indexProvenanceProjections(tx *sql.Tx, itemID string, env provenance.Envelope) error {
	pairs := []struct {
		key   string
		value string
	}{
		{MetaKeyProvenanceOrigin, env.Origin},
		{MetaKeyProvenanceModality, env.Modality},
		{MetaKeyProvenanceTrustLabel, env.Trust.Label},
		{MetaKeyProvenanceContentScope, env.Hashes.ContentScope},
	}
	if env.Hashes.Content != nil {
		pairs = append(pairs, struct {
			key   string
			value string
		}{MetaKeyProvenanceContentDigest, *env.Hashes.Content})
	}
	for _, pair := range pairs {
		if pair.value == "" {
			continue
		}
		if _, err := tx.Exec(`insert or ignore into item_metadata(item_id, key, value) values(?,?,?)`, itemID, pair.key, pair.value); err != nil {
			return err
		}
	}
	return nil
}

func replaceProvenanceProjections(tx *sql.Tx, itemID string, env provenance.Envelope) error {
	if _, err := tx.Exec(`delete from item_metadata where item_id = ? and key in (?,?,?,?,?)`,
		itemID,
		MetaKeyProvenanceOrigin,
		MetaKeyProvenanceModality,
		MetaKeyProvenanceTrustLabel,
		MetaKeyProvenanceContentScope,
		MetaKeyProvenanceContentDigest,
	); err != nil {
		return err
	}
	return indexProvenanceProjections(tx, itemID, env)
}

// AppendProvenanceEvent inserts one immutable provenance_events row.
// Event IDs are stable across wall-clock time so concurrent inferred backfills
// that race on the same transition collapse via INSERT OR IGNORE.
func AppendProvenanceEvent(tx *sql.Tx, itemID string, fromLabel, toLabel, contentHash, contentScope, operatorCommand string, evidence map[string]any) error {
	if evidence == nil {
		evidence = map[string]any{}
	}
	at := time.Now().UTC().Format(time.RFC3339Nano)
	event := ProvenanceEvent{
		Schema:              ProvenanceEventSchema,
		SchemaVersion:       1,
		At:                  at,
		ItemRef:             "miseledger:item:" + itemID,
		FromLabel:           fromLabel,
		ToLabel:             toLabel,
		EnvelopeContentHash: contentHash,
		ContentScope:        contentScope,
		OperatorCommand:     operatorCommand,
		Evidence:            evidence,
	}
	raw, err := json.Marshal(event)
	if err != nil {
		return err
	}
	evidenceRaw, err := json.Marshal(evidence)
	if err != nil {
		return err
	}
	eventID := stableID("provenance-event", itemID, fromLabel, toLabel, contentHash, contentScope, operatorCommand)
	_, err = tx.Exec(`insert or ignore into provenance_events(id, item_id, at, from_label, to_label, envelope_content_hash, content_scope, operator_command, evidence_json, event_json)
values(?,?,?,?,?,?,?,?,?,?)`,
		eventID, itemID, at, nullIfEmpty(fromLabel), toLabel, nullIfEmpty(contentHash), nullIfEmpty(contentScope), nullIfEmpty(operatorCommand), string(evidenceRaw), string(raw))
	return err
}

// TransitionTrustLabel updates an item's embedded envelope trust label, refreshes
// indexed projections, and appends an immutable provenance_events row. Events
// are never deleted or rewritten.
func TransitionTrustLabel(db *sql.DB, itemID, toLabel, operatorCommand string, evidence map[string]any) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var metadataJSON, text string
	if err := tx.QueryRow(`select metadata_json, coalesce(text,'') from items where id = ?`, itemID).Scan(&metadataJSON, &text); err != nil {
		return err
	}
	meta := map[string]any{}
	if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
		return err
	}
	rawEnv, ok := meta["provenance"]
	if !ok {
		return fmt.Errorf("item %s has no provenance envelope", itemID)
	}
	envBytes, err := json.Marshal(rawEnv)
	if err != nil {
		return err
	}
	var env provenance.Envelope
	if err := json.Unmarshal(envBytes, &env); err != nil {
		return err
	}
	fromLabel := env.Trust.Label
	if fromLabel == toLabel {
		return nil
	}
	at := time.Now().UTC().Format(time.RFC3339Nano)
	env.Trust.Label = toLabel
	env.Trust.AssignedBy = operatorCommand
	env.Trust.AssignedAt = &at
	if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
		return err
	}
	contentHash := ""
	if env.Hashes.Content != nil {
		contentHash = *env.Hashes.Content
	} else {
		contentHash = provenance.ContentSHA256(text)
	}
	meta["provenance"] = env
	updated, err := json.Marshal(meta)
	if err != nil {
		return err
	}
	if _, err := tx.Exec(`update items set metadata_json = ? where id = ?`, string(updated), itemID); err != nil {
		return err
	}
	if err := replaceProvenanceProjections(tx, itemID, env); err != nil {
		return err
	}
	if err := AppendProvenanceEvent(tx, itemID, fromLabel, toLabel, contentHash, env.Hashes.ContentScope, operatorCommand, evidence); err != nil {
		return err
	}
	return tx.Commit()
}

// BackfillProvenance walks items after afterID in batches, writing inferred
// envelopes for rows missing provenance and ensuring indexed projections exist.
// Structurally valid existing envelopes are preserved regardless of content
// digest match; only projections are repaired. Malformed provenance is isolated
// per row with bounded evidence so the batch stays resumable. Inferred rows
// always use trust.label=unknown.
func BackfillProvenance(db *sql.DB, batchSize int, afterID string) (BackfillProvenanceResult, error) {
	if batchSize <= 0 {
		batchSize = 100
	}
	result := BackfillProvenanceResult{Cursor: afterID}
	rows, err := db.Query(`select id, coalesce(text,''), metadata_json, coalesce(raw_hash,''), coalesce(raw_path,''), source_id, collection_id, external_id
from items
where id > ?
order by id
limit ?`, afterID, batchSize)
	if err != nil {
		return result, err
	}
	defer rows.Close()

	type itemRow struct {
		id, text, metadataJSON, rawHash, rawPath, sourceID, collectionID, externalID string
	}
	var batch []itemRow
	for rows.Next() {
		var row itemRow
		if err := rows.Scan(&row.id, &row.text, &row.metadataJSON, &row.rawHash, &row.rawPath, &row.sourceID, &row.collectionID, &row.externalID); err != nil {
			return result, err
		}
		batch = append(batch, row)
	}
	if err := rows.Err(); err != nil {
		return result, err
	}

	tx, err := db.Begin()
	if err != nil {
		return result, err
	}
	defer tx.Rollback()

	for _, row := range batch {
		result.Scanned++
		result.Cursor = row.id
		outcome, events, evidence, err := backfillOneItem(tx, row.id, row.text, row.metadataJSON, row.rawHash, row.rawPath, row.sourceID, row.collectionID, row.externalID)
		if err != nil {
			return result, err
		}
		switch outcome {
		case backfillUpdated:
			result.Updated++
		case backfillMalformed:
			result.Malformed++
			if evidence != nil && len(result.Evidence) < maxBackfillEvidence {
				result.Evidence = append(result.Evidence, *evidence)
			}
		default:
			result.Skipped++
		}
		result.Events += events
	}
	if err := tx.Commit(); err != nil {
		return result, err
	}
	if err := db.QueryRow(`select count(*) from items where id > ?`, result.Cursor).Scan(&result.Remaining); err != nil {
		return result, err
	}
	return result, nil
}

func backfillOneItem(tx *sql.Tx, itemID, text, metadataJSON, rawHash, rawPath, sourceID, collectionID, externalID string) (outcome backfillOutcome, events int, evidence *BackfillItemEvidence, err error) {
	// Re-read inside the write transaction so concurrent backfills observe the
	// latest provenance state rather than a stale pre-tx snapshot.
	var liveMetadataJSON, liveText string
	if err := tx.QueryRow(`select metadata_json, coalesce(text,'') from items where id = ?`, itemID).Scan(&liveMetadataJSON, &liveText); err != nil {
		return backfillSkipped, 0, nil, err
	}
	if liveText != "" {
		text = liveText
	}
	metadataJSON = liveMetadataJSON

	meta := map[string]any{}
	if strings.TrimSpace(metadataJSON) != "" {
		if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
			meta = map[string]any{}
		}
	}
	if rawEnv, ok := meta["provenance"]; ok && rawEnv != nil {
		env, parseErr := parseRetainableEnvelope(rawEnv)
		if parseErr != nil {
			return backfillMalformed, 0, &BackfillItemEvidence{
				ItemID: itemID,
				Reason: "malformed provenance: " + parseErr.Error(),
			}, nil
		}
		if projectionsComplete(tx, itemID, env) {
			return backfillSkipped, 0, nil, nil
		}
		if err := replaceProvenanceProjections(tx, itemID, env); err != nil {
			return backfillSkipped, 0, nil, err
		}
		return backfillUpdated, 0, nil, nil
	}

	env, err := buildInferredBackfillEnvelope(tx, itemID, text, rawHash, sourceID, collectionID, externalID)
	if err != nil {
		return backfillSkipped, 0, nil, err
	}
	meta["provenance"] = env
	updated, err := json.Marshal(meta)
	if err != nil {
		return backfillSkipped, 0, nil, err
	}
	// Conditional write: only stamp inferred provenance when still absent.
	res, err := tx.Exec(`update items set metadata_json = ? where id = ? and json_extract(metadata_json, '$.provenance') is null`, string(updated), itemID)
	if err != nil {
		return backfillSkipped, 0, nil, err
	}
	affected, _ := res.RowsAffected()
	if affected == 0 {
		// Another writer filled provenance between our read and conditional
		// update. Re-read once and take the retain/repair path without looping.
		var refreshed string
		if err := tx.QueryRow(`select metadata_json from items where id = ?`, itemID).Scan(&refreshed); err != nil {
			return backfillSkipped, 0, nil, err
		}
		meta = map[string]any{}
		if err := json.Unmarshal([]byte(refreshed), &meta); err != nil {
			meta = map[string]any{}
		}
		rawEnv, ok := meta["provenance"]
		if !ok || rawEnv == nil {
			return backfillSkipped, 0, nil, nil
		}
		env, parseErr := parseRetainableEnvelope(rawEnv)
		if parseErr != nil {
			return backfillMalformed, 0, &BackfillItemEvidence{
				ItemID: itemID,
				Reason: "malformed provenance: " + parseErr.Error(),
			}, nil
		}
		if projectionsComplete(tx, itemID, env) {
			return backfillSkipped, 0, nil, nil
		}
		if err := replaceProvenanceProjections(tx, itemID, env); err != nil {
			return backfillSkipped, 0, nil, err
		}
		return backfillUpdated, 0, nil, nil
	}
	if err := replaceProvenanceProjections(tx, itemID, env); err != nil {
		return backfillSkipped, 0, nil, err
	}
	contentHash := ""
	if env.Hashes.Content != nil {
		contentHash = *env.Hashes.Content
	}
	if err := AppendProvenanceEvent(tx, itemID, "", "unknown", contentHash, env.Hashes.ContentScope, "ingest:ingest.BackfillProvenance", map[string]any{
		"attribution": "inferred",
		"raw_path":    rawPath,
	}); err != nil {
		return backfillSkipped, 0, nil, err
	}
	return backfillUpdated, 1, nil, nil
}

func buildInferredBackfillEnvelope(tx *sql.Tx, itemID, text, rawHash, sourceID, collectionID, externalID string) (provenance.Envelope, error) {
	var sourceKind, collectionExternalID string
	_ = tx.QueryRow(`select kind from sources where id = ?`, sourceID).Scan(&sourceKind)
	_ = tx.QueryRow(`select external_id from collections where id = ?`, collectionID).Scan(&collectionExternalID)
	if sourceKind == "" {
		sourceKind = "unknown"
	}
	if collectionExternalID == "" {
		collectionExternalID = collectionID
	}
	if collectionExternalID == "" {
		collectionExternalID = "unknown"
	}
	if externalID == "" {
		externalID = itemID
	}
	locator := fmt.Sprintf("miseledger://%s/%s/%s", sourceKind, collectionExternalID, externalID)
	at := time.Now().UTC().Format(time.RFC3339Nano)
	origin, modality := inferOriginModality(sourceKind)
	in := provenance.EvidenceInput{
		SourceSystem:    "miseledger",
		SourceKind:      sourceKind,
		SourceProducer:  "ingest.BackfillProvenance",
		Origin:          origin,
		RepositoryID:    "unknown",
		CollectionID:    collectionExternalID,
		ItemID:          externalID,
		LocatorKind:     "uri",
		LocatorValue:    locator,
		Attribution:     "inferred",
		Modality:        modality,
		TrustLabel:      "unknown",
		TrustAssignedBy: "ingest:ingest.BackfillProvenance",
		TrustAssignedAt: &at,
		InjectionStatus: "pending",
		InjectionRules:  []string{},
		Text:            text,
		IngestedAt:      &at,
		CapturedAt:      &at,
	}
	env, err := provenance.NewEvidenceEnvelope(in)
	if err != nil {
		return provenance.Envelope{}, err
	}
	if strings.HasPrefix(rawHash, "sha256:") && len(strings.TrimPrefix(rawHash, "sha256:")) == 64 {
		digest := strings.TrimPrefix(rawHash, "sha256:")
		algo, scope := provenance.HashAlgorithm, provenance.RawScope
		env.Hashes.RawAlgorithm = &algo
		env.Hashes.RawScope = &scope
		env.Hashes.Raw = &digest
		if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
			return provenance.Envelope{}, err
		}
	}
	return env, nil
}

// ParseRetainableEnvelope unmarshals and validates an existing provenance value
// for retention. Content digest may be nil; other structural rules still apply.
func ParseRetainableEnvelope(raw any) (provenance.Envelope, error) {
	return parseRetainableEnvelope(raw)
}

// ValidateRetainableEnvelope reports whether env can be retained without
// rewriting trust history. Nil hashes.content is allowed when the rest is valid.
func ValidateRetainableEnvelope(env provenance.Envelope) error {
	return validateRetainableEnvelope(env)
}

func parseRetainableEnvelope(raw any) (provenance.Envelope, error) {
	envBytes, err := json.Marshal(raw)
	if err != nil {
		return provenance.Envelope{}, err
	}
	var env provenance.Envelope
	if err := json.Unmarshal(envBytes, &env); err != nil {
		return provenance.Envelope{}, err
	}
	if err := validateRetainableEnvelope(env); err != nil {
		return provenance.Envelope{}, err
	}
	return env, nil
}

func validateRetainableEnvelope(env provenance.Envelope) error {
	if err := provenance.Validate(env, provenance.ValidationContext{}); err == nil {
		return nil
	} else if env.Hashes.Content != nil {
		return err
	}
	// Historical rows may omit hashes.content. Probe structural validity with a
	// temporary well-formed digest without mutating the retained envelope.
	probe := env
	placeholder := strings.Repeat("a", 64)
	probe.Hashes.Content = &placeholder
	if err := provenance.Validate(probe, provenance.ValidationContext{}); err != nil {
		return err
	}
	return nil
}

func projectionsComplete(tx *sql.Tx, itemID string, env provenance.Envelope) bool {
	want := map[string]string{
		MetaKeyProvenanceOrigin:       env.Origin,
		MetaKeyProvenanceModality:     env.Modality,
		MetaKeyProvenanceTrustLabel:   env.Trust.Label,
		MetaKeyProvenanceContentScope: env.Hashes.ContentScope,
	}
	if env.Hashes.Content != nil {
		want[MetaKeyProvenanceContentDigest] = *env.Hashes.Content
	} else {
		// Null envelope content digest must not leave a stale projected digest.
		var stale int
		if err := tx.QueryRow(`select count(*) from item_metadata where item_id = ? and key = ?`, itemID, MetaKeyProvenanceContentDigest).Scan(&stale); err != nil || stale != 0 {
			return false
		}
	}
	for key, value := range want {
		if value == "" {
			continue
		}
		var found int
		if err := tx.QueryRow(`select 1 from item_metadata where item_id = ? and key = ? and value = ?`, itemID, key, value).Scan(&found); err != nil {
			return false
		}
	}
	return true
}

func inferOriginModality(sourceKind string) (origin, modality string) {
	switch strings.ToLower(strings.TrimSpace(sourceKind)) {
	case "codex", "claude", "cursor", "opencode", "openclaw", "hermes", "pi", "grok", "brigade-memory":
		return "agent-session", "tool-output"
	case "discrawl", "gitcrawl", "slacrawl", "graincrawl", "notcrawl", "mailcrawl", "telecrawl":
		return "external-service", "tool-output"
	default:
		return "unknown", "unknown"
	}
}
