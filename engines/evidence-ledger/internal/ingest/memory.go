package ingest

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/escoffier-labs/miseledger/internal/sources/memory"
)

// MemorySourceKind is the native source kind for canonical Markdown memory cards.
const MemorySourceKind = "brigade-memory"

// MemoryCapability identifies the Slice 1a memory projection contract.
const MemoryCapability = "memory-projection.v1"

// MemoryScanReceipt is the completed/failed scan receipt contract for memory crawls.
type MemoryScanReceipt struct {
	ScanID                   string   `json:"scan_id"`
	SourceKind               string   `json:"source_kind"`
	SourcePath               string   `json:"source_path"`
	Namespace                string   `json:"memory_namespace,omitempty"`
	Status                   string   `json:"status"`
	Stale                    bool     `json:"stale"`
	Capability               string   `json:"capability"`
	EngineVersion            string   `json:"engine_version"`
	Created                  int      `json:"created"`
	Updated                  int      `json:"updated"`
	Unchanged                int      `json:"unchanged"`
	Removed                  int      `json:"removed"`
	Skipped                  int      `json:"skipped"`
	Failed                   int      `json:"failed"`
	CanonicalCount           int      `json:"canonical_count"`
	LiveCount                int      `json:"live_count"`
	HashDivergence           int      `json:"hash_divergence"`
	UnresolvedRelations      int      `json:"unresolved_relations"`
	MalformedSkipped         int      `json:"malformed_skipped"`
	Partial                  bool     `json:"partial"`
	ObservedExternalIDs      []string `json:"observed_external_ids,omitempty"`
	Warnings                 []string `json:"warnings,omitempty"`
	LastCompletedScanID      string   `json:"last_completed_scan_id,omitempty"`
	PriorSnapshotMarkedStale bool     `json:"prior_snapshot_marked_stale"`
}

// ObservedCard is one card seen during a memory crawl.
type ObservedCard struct {
	ExternalID  string
	ContentHash string
	RawPath     string
	Identity    string // explicit_id | path
	Outcome     string // created | updated | unchanged | skipped | failed
}

// LastMemoryNamespace returns the most recent memory_namespace recorded on a
// source_scan_runs row, used when a failed crawl cannot re-read memory/NAMESPACE.
func LastMemoryNamespace(db *sql.DB) string {
	var ns sql.NullString
	_ = db.QueryRow(`select json_extract(metadata_json, '$.memory_namespace')
from source_scan_runs
where source_kind = ? and coalesce(json_extract(metadata_json, '$.memory_namespace'), '') != ''
order by started_at desc limit 1`, MemorySourceKind).Scan(&ns)
	if ns.Valid {
		return ns.String
	}
	return ""
}

// BeginMemoryScan inserts a running scan row and returns its id.
func BeginMemoryScan(db *sql.DB, sourcePath, engineVersion, namespace string) (string, error) {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	id := stableID("memory-scan", MemorySourceKind, namespace, sourcePath, now)
	meta, _ := json.Marshal(map[string]any{
		"capability":       MemoryCapability,
		"engine_version":   engineVersion,
		"memory_namespace": namespace,
	})
	_, err := db.Exec(`insert into source_scan_runs(
  id, source_kind, source_path, started_at, status, stale, metadata_json
) values(?,?,?,?,?,?,?)`, id, MemorySourceKind, sourcePath, now, "running", 0, string(meta))
	return id, err
}

// FailMemoryScan marks the scan failed/interrupted, tombstones nothing, and
// marks the prior completed snapshot for the same namespace stale.
func FailMemoryScan(db *sql.DB, scanID, status string, receipt *MemoryScanReceipt) error {
	if status == "" {
		status = "failed"
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	namespace := ""
	if receipt != nil {
		namespace = receipt.Namespace
	}
	if namespace == "" && scanID != "" {
		namespace, _ = scanNamespace(tx, scanID)
	}
	staleMarked, err := markPriorMemorySnapshotStale(tx, namespace)
	if err != nil {
		return err
	}
	if receipt == nil {
		receipt = &MemoryScanReceipt{}
	}
	receipt.SourceKind = MemorySourceKind
	receipt.Namespace = namespace
	receipt.Status = status
	receipt.Stale = true
	receipt.Partial = true
	receipt.Capability = MemoryCapability
	receipt.PriorSnapshotMarkedStale = staleMarked
	if scanID != "" {
		receipt.ScanID = scanID
		if _, err := tx.Exec(`update source_scan_runs set status = ?, completed_at = ?, stale = 1,
  created_count=?, updated_count=?, unchanged_count=?, removed_count=?, skipped_count=?, failed_count=?,
  canonical_count=?, live_count=?, hash_divergence_count=?, unresolved_relation_count=?, malformed_skipped_count=?
  where id = ?`,
			status, now,
			receipt.Created, receipt.Updated, receipt.Unchanged, receipt.Removed, receipt.Skipped, receipt.Failed,
			receipt.CanonicalCount, receipt.LiveCount, receipt.HashDivergence, receipt.UnresolvedRelations, receipt.MalformedSkipped,
			scanID); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// CompleteMemoryScan records observed ids, soft-tombstones missing memory_card
// items scoped to the active namespace only, resolves relations, then stores
// the post-resolution unresolved count.
func CompleteMemoryScan(db *sql.DB, scanID string, observed []ObservedCard, receipt *MemoryScanReceipt) error {
	if receipt == nil {
		receipt = &MemoryScanReceipt{}
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	namespace := receipt.Namespace
	if namespace == "" {
		namespace, err = scanNamespace(tx, scanID)
		if err != nil {
			return err
		}
		receipt.Namespace = namespace
	}

	seen := map[string]ObservedCard{}
	pathSet := map[string]bool{}
	for _, card := range observed {
		obsID := card.ExternalID
		if obsID == "" && card.RawPath != "" {
			obsID = "path:" + card.RawPath
		}
		if obsID == "" {
			continue
		}
		seen[obsID] = card
		if card.RawPath != "" {
			pathSet[card.RawPath] = true
		}
		if _, err := tx.Exec(`insert or replace into source_scan_observed(scan_id, external_id, content_hash, raw_path) values(?,?,?,?)`,
			scanID, obsID, card.ContentHash, card.RawPath); err != nil {
			return err
		}
	}

	removed, err := softTombstoneMissingMemoryCards(tx, namespace, seen, now)
	if err != nil {
		return err
	}
	receipt.Removed = removed

	live, err := liveMemoryExternalIDs(tx, namespace)
	if err != nil {
		return err
	}
	receipt.LiveCount = len(live)
	receipt.CanonicalCount = len(pathSet)
	if receipt.CanonicalCount == 0 {
		receipt.CanonicalCount = len(seen)
	}

	divergence := 0
	for id, card := range seen {
		if card.ContentHash == "" {
			continue
		}
		stored, ok := live[id]
		if ok && stored != "" && stored != card.ContentHash {
			divergence++
		}
	}
	receipt.HashDivergence = divergence
	receipt.MalformedSkipped = receipt.Skipped + receipt.Failed

	// Resolve first, then record the post-resolution unresolved count so crawl
	// JSON, the scan row, and health share one meaning.
	if _, err := resolveRelations(tx); err != nil {
		return err
	}
	unresolved, err := memoryUnresolvedRelationCount(tx, namespace)
	if err != nil {
		return err
	}
	receipt.UnresolvedRelations = unresolved

	if _, err := tx.Exec(`update source_scan_runs set status = ?, completed_at = ?, stale = 0,
  created_count=?, updated_count=?, unchanged_count=?, removed_count=?, skipped_count=?, failed_count=?,
  canonical_count=?, live_count=?, hash_divergence_count=?, unresolved_relation_count=?, malformed_skipped_count=?
  where id = ?`,
		"completed", now,
		receipt.Created, receipt.Updated, receipt.Unchanged, receipt.Removed, receipt.Skipped, receipt.Failed,
		receipt.CanonicalCount, receipt.LiveCount, receipt.HashDivergence, receipt.UnresolvedRelations, receipt.MalformedSkipped,
		scanID); err != nil {
		return err
	}

	receipt.ScanID = scanID
	receipt.SourceKind = MemorySourceKind
	receipt.Status = "completed"
	receipt.Stale = false
	receipt.Partial = false
	receipt.Capability = MemoryCapability
	for id := range seen {
		receipt.ObservedExternalIDs = append(receipt.ObservedExternalIDs, id)
	}
	return tx.Commit()
}

func scanNamespace(tx *sql.Tx, scanID string) (string, error) {
	var meta string
	if err := tx.QueryRow(`select coalesce(metadata_json, '{}') from source_scan_runs where id = ?`, scanID).Scan(&meta); err != nil {
		return "", err
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(meta), &parsed); err != nil {
		return "", nil
	}
	ns, _ := parsed["memory_namespace"].(string)
	return ns, nil
}

func markPriorMemorySnapshotStale(tx *sql.Tx, namespace string) (bool, error) {
	// Prefer namespace-scoped stale marking; fall back to source-kind when
	// namespace is unknown so interrupted scans still leave a stale prior.
	var res sql.Result
	var err error
	if namespace != "" {
		res, err = tx.Exec(`update source_scan_runs set stale = 1
where source_kind = ? and status = 'completed' and stale = 0
  and json_extract(metadata_json, '$.memory_namespace') = ?`, MemorySourceKind, namespace)
	} else {
		res, err = tx.Exec(`update source_scan_runs set stale = 1
where source_kind = ? and status = 'completed' and stale = 0`, MemorySourceKind)
	}
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func softTombstoneMissingMemoryCards(tx *sql.Tx, namespace string, seen map[string]ObservedCard, now string) (int, error) {
	presentPaths := map[string]bool{}
	presentIDs := map[string]bool{}
	for _, card := range seen {
		if card.RawPath != "" {
			presentPaths[card.RawPath] = true
		}
		if card.ExternalID != "" && card.Outcome != "skipped" && card.Outcome != "failed" {
			presentIDs[card.ExternalID] = true
		}
		// Skipped/failed files still on disk protect any prior live id for that path.
		if (card.Outcome == "skipped" || card.Outcome == "failed") && card.RawPath != "" {
			presentPaths[card.RawPath] = true
		}
	}

	rows, err := tx.Query(`
select i.id, i.external_id, coalesce(i.raw_path, ''), coalesce(json_extract(i.metadata_json, '$.relative_path'), '')
from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null
  and c.external_id = ?`, MemorySourceKind, namespace)
	if err != nil {
		return 0, err
	}
	defer rows.Close()

	type row struct {
		id, externalID, rawPath, relPath string
	}
	var missing []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.id, &r.externalID, &r.rawPath, &r.relPath); err != nil {
			return 0, err
		}
		pathKey := r.relPath
		if pathKey == "" {
			pathKey = r.rawPath
		}
		if presentPaths[pathKey] || presentIDs[r.externalID] {
			continue
		}
		missing = append(missing, r)
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}

	removedIDs := map[string]bool{}
	for _, r := range missing {
		if _, err := tx.Exec(`update items set tombstoned_at = ? where id = ? and tombstoned_at is null`, now, r.id); err != nil {
			return 0, err
		}
		if _, err := tx.Exec(`delete from item_fts where item_id = ?`, r.id); err != nil {
			return 0, err
		}
		removedIDs[r.externalID] = true
	}
	return len(removedIDs), nil
}

func liveMemoryExternalIDs(tx *sql.Tx, namespace string) (map[string]string, error) {
	rows, err := tx.Query(`
select i.external_id, i.content_hash
from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null
  and c.external_id = ?
order by i.created_at desc, i.id desc`, MemorySourceKind, namespace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]string{}
	for rows.Next() {
		var id, hash string
		if err := rows.Scan(&id, &hash); err != nil {
			return nil, err
		}
		if _, exists := out[id]; !exists {
			out[id] = hash
		}
	}
	return out, rows.Err()
}

// LegacyMemoryExternalIDs returns live rows still under the pre-namespace
// collection memory:cards. Namespaced crawls dual-read these for diagnostics
// and never tombstone or rebuild them (scoped-rebuild rule).
func LegacyMemoryExternalIDs(db *sql.DB) (map[string]string, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	return liveMemoryExternalIDs(tx, memory.LegacyCollectionID)
}

func memoryUnresolvedRelationCount(tx *sql.Tx, namespace string) (int, error) {
	var n int
	err := tx.QueryRow(`
select count(*)
from relations r
join items i on i.id = r.source_item_id
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null
  and c.external_id = ?
  and r.target_item_id is null
  and coalesce(r.target_external_id, '') != ''`, MemorySourceKind, namespace).Scan(&n)
	return n, err
}

// LiveMemoryProjection returns live external_id -> content_hash for a namespace.
func LiveMemoryProjection(db *sql.DB, namespace string) (map[string]string, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	return liveMemoryExternalIDs(tx, namespace)
}

// MemoryNamespaceSnapshot holds derived projection rows for failure-preserving rebuild.
type MemoryNamespaceSnapshot struct {
	Namespace string
	Items     []memoryItemSnapshot
}

type memoryItemSnapshot struct {
	ID           string
	ActorID      sql.NullString
	ExternalID   string
	Kind         string
	CreatedAt    sql.NullString
	UpdatedAt    sql.NullString
	Text         sql.NullString
	Summary      sql.NullString
	ContentHash  string
	RawJSON      string
	RawHash      sql.NullString
	RawPath      sql.NullString
	RawOrdinal   sql.NullInt64
	MetadataJSON string
	TombstonedAt sql.NullString
	Tags         []string
	FTSBody      sql.NullString
	Relations    []memoryRelationSnapshot
}

type memoryRelationSnapshot struct {
	ID                         string
	SourceItemID               string
	TargetItemID               sql.NullString
	TargetExternalID           sql.NullString
	TargetSourceKind           sql.NullString
	TargetCollectionExternalID sql.NullString
	RelationType               string
	Confidence                 float64
	MetadataJSON               string
}

// SnapshotMemoryNamespace captures live+tombstoned derived rows for one namespace.
func SnapshotMemoryNamespace(db *sql.DB, namespace string) (*MemoryNamespaceSnapshot, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.Query(`
select i.id, i.actor_id, i.external_id, i.kind, i.created_at, i.updated_at,
  i.text, i.summary, i.content_hash, i.raw_json, i.raw_hash, i.raw_path, i.raw_ordinal,
  i.metadata_json, i.tombstoned_at
from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, namespace)
	if err != nil {
		return nil, err
	}
	snap := &MemoryNamespaceSnapshot{Namespace: namespace}
	for rows.Next() {
		var item memoryItemSnapshot
		if err := rows.Scan(&item.ID, &item.ActorID, &item.ExternalID, &item.Kind,
			&item.CreatedAt, &item.UpdatedAt, &item.Text, &item.Summary, &item.ContentHash,
			&item.RawJSON, &item.RawHash, &item.RawPath, &item.RawOrdinal, &item.MetadataJSON, &item.TombstonedAt); err != nil {
			rows.Close()
			return nil, err
		}
		snap.Items = append(snap.Items, item)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}
	for i := range snap.Items {
		tagRows, err := tx.Query(`select tag from item_tags where item_id = ?`, snap.Items[i].ID)
		if err != nil {
			return nil, err
		}
		for tagRows.Next() {
			var tag string
			if err := tagRows.Scan(&tag); err != nil {
				tagRows.Close()
				return nil, err
			}
			snap.Items[i].Tags = append(snap.Items[i].Tags, tag)
		}
		tagRows.Close()
		_ = tx.QueryRow(`select body from item_fts where item_id = ?`, snap.Items[i].ID).Scan(&snap.Items[i].FTSBody)
		relRows, err := tx.Query(`
select id, source_item_id, target_item_id, target_external_id, target_source_kind,
  target_collection_external_id, relation_type, confidence, metadata_json
from relations where source_item_id = ?`, snap.Items[i].ID)
		if err != nil {
			return nil, err
		}
		for relRows.Next() {
			var rel memoryRelationSnapshot
			if err := relRows.Scan(&rel.ID, &rel.SourceItemID, &rel.TargetItemID, &rel.TargetExternalID,
				&rel.TargetSourceKind, &rel.TargetCollectionExternalID, &rel.RelationType, &rel.Confidence, &rel.MetadataJSON); err != nil {
				relRows.Close()
				return nil, err
			}
			snap.Items[i].Relations = append(snap.Items[i].Relations, rel)
		}
		relRows.Close()
	}
	return snap, nil
}

// RestoreMemoryNamespace restores a previously snapshotted namespace projection.
func RestoreMemoryNamespace(db *sql.DB, snap *MemoryNamespaceSnapshot) error {
	if snap == nil {
		return nil
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := deleteMemoryNamespaceTx(tx, snap.Namespace); err != nil {
		return err
	}
	sourceID := stableID("source", MemorySourceKind)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if _, err := tx.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)
on conflict(id) do update set updated_at=excluded.updated_at`, sourceID, MemorySourceKind, "Brigade Memory Cards", "1.0.0", now, now); err != nil {
		return err
	}
	collectionID := stableID("collection", MemorySourceKind, snap.Namespace)
	meta := map[string]any{"memory_namespace": snap.Namespace}
	metaJSON, _ := json.Marshal(meta)
	if _, err := tx.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)
on conflict(source_id, external_id) do update set updated_at=excluded.updated_at`,
		collectionID, sourceID, snap.Namespace, "memory_cards", "Memory cards", string(metaJSON), now, now); err != nil {
		return err
	}
	for _, item := range snap.Items {
		if _, err := tx.Exec(`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json, tombstoned_at)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
			item.ID, sourceID, collectionID, nullString(item.ActorID), item.ExternalID, item.Kind,
			nullString(item.CreatedAt), nullString(item.UpdatedAt), nullString(item.Text), nullString(item.Summary),
			item.ContentHash, item.RawJSON, nullString(item.RawHash), nullString(item.RawPath),
			nullInt64(item.RawOrdinal), item.MetadataJSON, nullString(item.TombstonedAt)); err != nil {
			return err
		}
		for _, tag := range item.Tags {
			if _, err := tx.Exec(`insert or ignore into item_tags(item_id, tag) values(?,?)`, item.ID, tag); err != nil {
				return err
			}
		}
		if item.FTSBody.Valid {
			if _, err := tx.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`,
				item.ID, MemorySourceKind, "memory_cards", item.Kind, "system", item.FTSBody.String); err != nil {
				return err
			}
		}
		for _, rel := range item.Relations {
			if _, err := tx.Exec(`insert into relations(id, source_item_id, target_item_id, target_external_id, target_source_kind, target_collection_external_id, relation_type, confidence, metadata_json)
values(?,?,?,?,?,?,?,?,?)`, rel.ID, rel.SourceItemID, nullString(rel.TargetItemID), nullString(rel.TargetExternalID),
				nullString(rel.TargetSourceKind), nullString(rel.TargetCollectionExternalID), rel.RelationType, rel.Confidence, rel.MetadataJSON); err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}

func nullString(v sql.NullString) any {
	if !v.Valid {
		return nil
	}
	return v.String
}

func nullInt64(v sql.NullInt64) any {
	if !v.Valid {
		return nil
	}
	return v.Int64
}

// DeleteMemoryNamespace removes only the derived projection for one namespace.
// Legacy memory:cards rows are never touched.
func DeleteMemoryNamespace(db *sql.DB, namespace string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := deleteMemoryNamespaceTx(tx, namespace); err != nil {
		return err
	}
	return tx.Commit()
}

func deleteMemoryNamespaceTx(tx *sql.Tx, namespace string) error {
	if namespace == "" || namespace == memory.LegacyCollectionID {
		return fmt.Errorf("refusing to delete legacy or empty memory namespace %q", namespace)
	}
	rows, err := tx.Query(`
select i.id from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, namespace)
	if err != nil {
		return err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		ids = append(ids, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := tx.Exec(`delete from item_tags where item_id = ?`, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from item_metadata where item_id = ?`, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from events where item_id = ?`, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from artifacts where item_id = ?`, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from item_fts where item_id = ?`, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from relations where source_item_id = ? or target_item_id = ?`, id, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from items where id = ?`, id); err != nil {
			return err
		}
	}
	// Completed scan records are intentionally retained so a failed rebuild can
	// mark the prior snapshot stale without losing last_completed_scan_id.
	if _, err := tx.Exec(`delete from collections where source_id in (select id from sources where kind = ?) and external_id = ?`,
		MemorySourceKind, namespace); err != nil {
		return err
	}
	return nil
}

// RebuildMemoryProjection deletes only the named namespace's derived projection.
// Prefer Snapshot+Delete+Restore around import for failure-preserving rebuilds.
func RebuildMemoryProjection(db *sql.DB, namespace string) error {
	return DeleteMemoryNamespace(db, namespace)
}

// MemoryHealth summarizes doctor/status fields for the memory projection.
type MemoryHealth struct {
	Capability          string  `json:"capability"`
	EngineVersion       string  `json:"engine_version"`
	MemoryNamespace     string  `json:"memory_namespace,omitempty"`
	LastCompletedScanID string  `json:"last_completed_scan_id"`
	LastCompletedAt     *string `json:"last_completed_at"`
	CanonicalCount      int     `json:"canonical_count"`
	LiveCount           int     `json:"live_count"`
	HashDivergence      int     `json:"hash_divergence"`
	UnresolvedRelations int     `json:"unresolved_relations"`
	MalformedSkipped    int     `json:"malformed_skipped"`
	Stale               bool    `json:"stale"`
	Partial             bool    `json:"partial"`
	Status              string  `json:"status"`
}

// CollectMemoryHealth reads the latest scan run plus live counts for a namespace.
// When namespace is empty it reports the latest memory scan across namespaces.
func CollectMemoryHealth(db *sql.DB, engineVersion, namespace string) (MemoryHealth, error) {
	h := MemoryHealth{
		Capability:      MemoryCapability,
		EngineVersion:   engineVersion,
		MemoryNamespace: namespace,
		Status:          "absent",
	}
	var scanID, status string
	var completed sql.NullString
	var stale, canonical, live, divergence, unresolved, malformed int
	var err error
	if namespace != "" {
		err = db.QueryRow(`select id, status, completed_at, stale, canonical_count, live_count,
  hash_divergence_count, unresolved_relation_count, malformed_skipped_count
from source_scan_runs
where source_kind = ? and json_extract(metadata_json, '$.memory_namespace') = ?
order by started_at desc limit 1`, MemorySourceKind, namespace).Scan(
			&scanID, &status, &completed, &stale, &canonical, &live, &divergence, &unresolved, &malformed)
	} else {
		err = db.QueryRow(`select id, status, completed_at, stale, canonical_count, live_count,
  hash_divergence_count, unresolved_relation_count, malformed_skipped_count,
  coalesce(json_extract(metadata_json, '$.memory_namespace'), '')
from source_scan_runs
where source_kind = ?
order by started_at desc limit 1`, MemorySourceKind).Scan(
			&scanID, &status, &completed, &stale, &canonical, &live, &divergence, &unresolved, &malformed, &namespace)
		h.MemoryNamespace = namespace
	}
	if err == sql.ErrNoRows {
		return h, nil
	}
	if err != nil {
		return h, err
	}
	h.Status = status
	h.Stale = stale != 0 || status != "completed"
	h.Partial = status != "completed"
	h.CanonicalCount = canonical
	h.LiveCount = live
	h.HashDivergence = divergence
	h.UnresolvedRelations = unresolved
	h.MalformedSkipped = malformed
	if status == "completed" && completed.Valid {
		h.LastCompletedScanID = scanID
		h.LastCompletedAt = &completed.String
	} else {
		var lastID string
		var lastAt sql.NullString
		if namespace != "" {
			_ = db.QueryRow(`select id, completed_at from source_scan_runs
where source_kind = ? and status = 'completed' and json_extract(metadata_json, '$.memory_namespace') = ?
order by completed_at desc limit 1`, MemorySourceKind, namespace).Scan(&lastID, &lastAt)
		} else {
			_ = db.QueryRow(`select id, completed_at from source_scan_runs
where source_kind = ? and status = 'completed'
order by completed_at desc limit 1`, MemorySourceKind).Scan(&lastID, &lastAt)
		}
		h.LastCompletedScanID = lastID
		if lastAt.Valid {
			h.LastCompletedAt = &lastAt.String
		}
	}

	if namespace != "" {
		tx, err := db.Begin()
		if err != nil {
			return h, err
		}
		defer tx.Rollback()
		liveMap, err := liveMemoryExternalIDs(tx, namespace)
		if err != nil {
			return h, err
		}
		h.LiveCount = len(liveMap)
		unresolved, err = memoryUnresolvedRelationCount(tx, namespace)
		if err != nil {
			return h, err
		}
		h.UnresolvedRelations = unresolved
	}
	return h, nil
}

// ClassifyMemoryOutcomes compares pre-import live hashes to observed cards.
func ClassifyMemoryOutcomes(before map[string]string, observed []ObservedCard) (created, updated, unchanged int, classified []ObservedCard) {
	out := make([]ObservedCard, 0, len(observed))
	for _, card := range observed {
		if card.Outcome == "skipped" || card.Outcome == "failed" {
			out = append(out, card)
			continue
		}
		prev, ok := before[card.ExternalID]
		switch {
		case !ok:
			card.Outcome = "created"
			created++
		case prev == card.ContentHash:
			card.Outcome = "unchanged"
			unchanged++
		default:
			card.Outcome = "updated"
			updated++
		}
		out = append(out, card)
	}
	return created, updated, unchanged, out
}

// PriorLiveHashes is a convenience wrapper around LiveMemoryProjection.
func PriorLiveHashes(db *sql.DB, namespace string) (map[string]string, error) {
	return LiveMemoryProjection(db, namespace)
}

// SoftTombstoneCount returns how many live memory_card rows would be removed
// for the given observed set without writing. Used by interrupted-scan tests.
func SoftTombstoneCount(db *sql.DB, namespace string, observedExternalIDs []string) (int, error) {
	seen := map[string]ObservedCard{}
	for _, id := range observedExternalIDs {
		seen[id] = ObservedCard{ExternalID: id}
	}
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	rows, err := tx.Query(`
select i.external_id
from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and i.kind = 'memory_card' and i.tombstoned_at is null and c.external_id = ?`, MemorySourceKind, namespace)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	n := 0
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return 0, err
		}
		if _, ok := seen[id]; !ok {
			n++
		}
	}
	return n, rows.Err()
}

// AssertMemoryNotInDefaultRetention documents the retention safety invariant.
func AssertMemoryNotInDefaultRetention(itemKind string) error {
	if itemKind == "memory_card" {
		return fmt.Errorf("memory_card must never match default retention tiers")
	}
	return nil
}
