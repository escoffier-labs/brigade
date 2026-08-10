package ingest

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
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

	if memoryScanAfterObserveHook != nil {
		if err := memoryScanAfterObserveHook(tx); err != nil {
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
// collection memory:cards. Empty-namespace status/doctor health dual-reads
// these; namespaced crawls never tombstone or rebuild them (scoped-rebuild).
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

// SetMemoryScanAfterObserveHookForTest injects a failure after observation rows
// are written and before reconciliation. Tests must clear it with a nil argument.
func SetMemoryScanAfterObserveHookForTest(fn func(tx *sql.Tx) error) {
	memoryScanAfterObserveHook = fn
}

// memoryScanAfterObserveHook is set by tests to inject a failure after
// observation rows are written and before reconciliation/tombstones.
var memoryScanAfterObserveHook func(tx *sql.Tx) error

// MemoryRebuildTestHookAfterDetach is set by tests to fail after the live
// namespace collection has been detached aside for rebuild.
var MemoryRebuildTestHookAfterDetach func() error

const memoryBackupPrefix = "__miseledger_memory_backup__:"
const memoryItemBackupPrefix = "__miseledger_item_backup__:"

func backupCollectionExternalID(namespace string) string {
	return memoryBackupPrefix + namespace
}

func isBackupCollectionExternalID(externalID string) bool {
	return strings.HasPrefix(externalID, memoryBackupPrefix)
}

func backupItemID(id string) string {
	if strings.HasPrefix(id, memoryItemBackupPrefix) {
		return id
	}
	return memoryItemBackupPrefix + id
}

func originalItemID(id string) string {
	return strings.TrimPrefix(id, memoryItemBackupPrefix)
}

// RecoverMemoryRebuildState restores a backup collection left by a crashed
// rebuild before any new crawl observes live hashes.
func RecoverMemoryRebuildState(db *sql.DB, namespace string) error {
	if namespace == "" || namespace == memory.LegacyCollectionID || isBackupCollectionExternalID(namespace) {
		return nil
	}
	backup := backupCollectionExternalID(namespace)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var backupCount int
	if err := tx.QueryRow(`select count(*) from collections c
join sources s on s.id = c.source_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, backup).Scan(&backupCount); err != nil {
		return err
	}
	if backupCount == 0 {
		return tx.Commit()
	}
	if err := deleteMemoryNamespaceTx(tx, namespace); err != nil {
		return err
	}
	if err := moveCollectionExternalID(tx, backup, namespace, true); err != nil {
		return err
	}
	return tx.Commit()
}

// DetachMemoryNamespace moves the live namespace collection to a backup
// collection id so a rebuild can import into a fresh live collection without
// deleting prior rows. Item primary keys are remapped with a backup prefix so
// re-import can recreate the original content-addressed ids.
func DetachMemoryNamespace(db *sql.DB, namespace string) error {
	if namespace == "" || namespace == memory.LegacyCollectionID || isBackupCollectionExternalID(namespace) {
		return fmt.Errorf("refusing to detach legacy or invalid memory namespace %q", namespace)
	}
	backup := backupCollectionExternalID(namespace)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var backupCount int
	if err := tx.QueryRow(`select count(*) from collections c
join sources s on s.id = c.source_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, backup).Scan(&backupCount); err != nil {
		return err
	}
	var liveCount int
	if err := tx.QueryRow(`select count(*) from collections c
join sources s on s.id = c.source_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, namespace).Scan(&liveCount); err != nil {
		return err
	}
	if backupCount > 0 && liveCount == 0 {
		if err := moveCollectionExternalID(tx, backup, namespace, true); err != nil {
			return err
		}
		liveCount = 1
	}
	if backupCount > 0 && liveCount > 0 {
		if err := deleteMemoryNamespaceTx(tx, backup); err != nil {
			return err
		}
	}
	if liveCount == 0 {
		return tx.Commit()
	}
	if err := moveCollectionExternalID(tx, namespace, backup, false); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	if MemoryRebuildTestHookAfterDetach != nil {
		if err := MemoryRebuildTestHookAfterDetach(); err != nil {
			_ = AbortMemoryRebuild(db, namespace)
			return err
		}
	}
	return nil
}

// FinalizeMemoryRebuild repoints inbound relations onto the new live items and
// drops the backup collection.
func FinalizeMemoryRebuild(db *sql.DB, namespace string) error {
	backup := backupCollectionExternalID(namespace)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := repointBackupItemRelations(tx, namespace); err != nil {
		return err
	}
	if err := deleteMemoryNamespaceTx(tx, backup); err != nil {
		return err
	}
	return tx.Commit()
}

// AbortMemoryRebuild deletes any partial new live collection and restores the
// backup collection so prior live IDs, hashes, relations, metadata, events,
// and artifacts remain intact.
func AbortMemoryRebuild(db *sql.DB, namespace string) error {
	if namespace == "" || namespace == memory.LegacyCollectionID {
		return fmt.Errorf("refusing to abort rebuild for invalid namespace %q", namespace)
	}
	backup := backupCollectionExternalID(namespace)
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err := deleteMemoryNamespaceTx(tx, namespace); err != nil {
		return err
	}
	var backupCount int
	if err := tx.QueryRow(`select count(*) from collections c
join sources s on s.id = c.source_id
where s.kind = ? and c.external_id = ?`, MemorySourceKind, backup).Scan(&backupCount); err != nil {
		return err
	}
	if backupCount > 0 {
		if err := moveCollectionExternalID(tx, backup, namespace, true); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func moveCollectionExternalID(tx *sql.Tx, fromExternal, toExternal string, restoreOriginalIDs bool) error {
	sourceID := stableID("source", MemorySourceKind)
	fromID := stableID("collection", MemorySourceKind, fromExternal)
	toID := stableID("collection", MemorySourceKind, toExternal)
	var kind, name, meta sql.NullString
	var created, updated sql.NullString
	if err := tx.QueryRow(`select kind, name, metadata_json, created_at, updated_at from collections where id = ?`, fromID).Scan(
		&kind, &name, &meta, &created, &updated); err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	kindVal := "memory_cards"
	if kind.Valid && kind.String != "" {
		kindVal = kind.String
	}
	nameVal := "Memory cards"
	if name.Valid && name.String != "" {
		nameVal = name.String
	}
	metaVal := "{}"
	if meta.Valid && meta.String != "" {
		metaVal = meta.String
	}
	createdVal := now
	if created.Valid && created.String != "" {
		createdVal = created.String
	}
	if _, err := tx.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at)
values(?,?,?,?,?,?,?,?)
on conflict(id) do update set external_id=excluded.external_id, kind=excluded.kind, name=excluded.name,
  metadata_json=excluded.metadata_json, updated_at=excluded.updated_at`,
		toID, sourceID, toExternal, kindVal, nameVal, metaVal, createdVal, now); err != nil {
		return err
	}

	rows, err := tx.Query(`select id from items where collection_id = ?`, fromID)
	if err != nil {
		return err
	}
	var itemIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return err
		}
		itemIDs = append(itemIDs, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	for _, oldID := range itemIDs {
		newID := backupItemID(oldID)
		if restoreOriginalIDs {
			newID = originalItemID(oldID)
		}
		if newID == oldID {
			if _, err := tx.Exec(`update items set collection_id = ? where id = ?`, toID, oldID); err != nil {
				return err
			}
			if _, err := tx.Exec(`update events set collection_id = ? where item_id = ?`, toID, oldID); err != nil {
				return err
			}
			continue
		}
		if err := cloneItemWithNewID(tx, oldID, newID, toID); err != nil {
			return err
		}
		if err := deleteItemGraph(tx, oldID); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`delete from collections where id = ?`, fromID); err != nil {
		return err
	}
	return nil
}

func cloneItemWithNewID(tx *sql.Tx, oldID, newID, collectionID string) error {
	if _, err := tx.Exec(`insert into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json, tombstoned_at)
select ?, source_id, ?, actor_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json, tombstoned_at
from items where id = ?`, newID, collectionID, oldID); err != nil {
		return err
	}
	if _, err := tx.Exec(`insert into item_tags(item_id, tag) select ?, tag from item_tags where item_id = ?`, newID, oldID); err != nil {
		return err
	}
	if _, err := tx.Exec(`insert into item_metadata(item_id, key, value) select ?, key, value from item_metadata where item_id = ?`, newID, oldID); err != nil {
		return err
	}
	if _, err := tx.Exec(`insert into events(id, source_id, collection_id, actor_id, item_id, kind, occurred_at, metadata_json)
select printf('bakevt:%s:%s', ?, id), source_id, ?, actor_id, ?, kind, occurred_at, metadata_json from events where item_id = ?`, newID, collectionID, newID, oldID); err != nil {
		return err
	}
	if _, err := tx.Exec(`insert into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json)
select printf('bakart:%s:%s', ?, id), source_id, ?, external_id, kind, path, url, mime_type, text, content_hash, metadata_json from artifacts where item_id = ?`, newID, newID, oldID); err != nil {
		return err
	}
	_, _ = tx.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body)
select ?, source_kind, collection_kind, item_kind, actor_type, body from item_fts where item_id = ?`, newID, oldID)
	if _, err := tx.Exec(`insert into relations(id, source_item_id, target_item_id, target_external_id, target_source_kind, target_collection_external_id, relation_type, confidence, metadata_json)
select printf('bakrel:%s:%s', ?, id), ?, target_item_id, target_external_id, target_source_kind, target_collection_external_id, relation_type, confidence, metadata_json
from relations where source_item_id = ?`, newID, newID, oldID); err != nil {
		return err
	}
	if _, err := tx.Exec(`update relations set target_item_id = ? where target_item_id = ?`, newID, oldID); err != nil {
		return err
	}
	return nil
}

func deleteItemGraph(tx *sql.Tx, itemID string) error {
	if _, err := tx.Exec(`delete from item_tags where item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from item_metadata where item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from events where item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from artifacts where item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from item_fts where item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from relations where source_item_id = ?`, itemID); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from items where id = ?`, itemID); err != nil {
		return err
	}
	return nil
}

func repointBackupItemRelations(tx *sql.Tx, namespace string) error {
	rows, err := tx.Query(`select id, target_item_id, target_external_id from relations
where target_item_id like ? and target_source_kind = ? and target_collection_external_id = ?`,
		memoryItemBackupPrefix+"%", MemorySourceKind, namespace)
	if err != nil {
		return err
	}
	type rel struct {
		id, target string
		externalID sql.NullString
	}
	var pending []rel
	for rows.Next() {
		var r rel
		if err := rows.Scan(&r.id, &r.target, &r.externalID); err != nil {
			rows.Close()
			return err
		}
		pending = append(pending, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}
	for _, r := range pending {
		liveID := originalItemID(r.target)
		if r.externalID.Valid && r.externalID.String != "" {
			err := tx.QueryRow(`select i.id from items i
join sources s on s.id = i.source_id
join collections c on c.id = i.collection_id
where s.kind = ? and c.external_id = ? and i.external_id = ? and i.tombstoned_at is null`,
				MemorySourceKind, namespace, r.externalID.String).Scan(&liveID)
			if err == sql.ErrNoRows {
				continue
			}
			if err != nil {
				return err
			}
		} else {
			var exists int
			if err := tx.QueryRow(`select count(*) from items where id = ?`, liveID).Scan(&exists); err != nil {
				return err
			}
			if exists == 0 {
				continue
			}
		}
		if _, err := tx.Exec(`update relations set target_item_id = ? where id = ?`, liveID, r.id); err != nil {
			return err
		}
	}
	return nil
}

// DeleteMemoryNamespace removes only the derived projection for one namespace
// collection external id (live or backup). Legacy memory:cards rows are never touched.
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
		if err := deleteItemGraph(tx, id); err != nil {
			return err
		}
		if _, err := tx.Exec(`delete from relations where target_item_id = ?`, id); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(`delete from collections where source_id in (select id from sources where kind = ?) and external_id = ?`,
		MemorySourceKind, namespace); err != nil {
		return err
	}
	return nil
}

// RebuildMemoryProjection detaches the live namespace for a staged rebuild.
// Callers must FinalizeMemoryRebuild on success or AbortMemoryRebuild on failure.
func RebuildMemoryProjection(db *sql.DB, namespace string) error {
	return DetachMemoryNamespace(db, namespace)
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

// CollectMemoryHealth reads the latest scan run plus live counts.
// When namespace is non-empty, live/unresolved counts are scoped to that
// collection. When namespace is empty, status/doctor aggregation covers every
// brigade-memory collection including legacy memory:cards (dual-read).
func CollectMemoryHealth(db *sql.DB, engineVersion, namespace string) (MemoryHealth, error) {
	h := MemoryHealth{
		Capability:      MemoryCapability,
		EngineVersion:   engineVersion,
		MemoryNamespace: namespace,
		Status:          "absent",
	}
	scoped := namespace
	var scanID, status string
	var completed sql.NullString
	var stale, canonical, live, divergence, unresolved, malformed int
	var err error
	if scoped != "" {
		err = db.QueryRow(`select id, status, completed_at, stale, canonical_count, live_count,
  hash_divergence_count, unresolved_relation_count, malformed_skipped_count
from source_scan_runs
where source_kind = ? and json_extract(metadata_json, '$.memory_namespace') = ?
order by started_at desc limit 1`, MemorySourceKind, scoped).Scan(
			&scanID, &status, &completed, &stale, &canonical, &live, &divergence, &unresolved, &malformed)
	} else {
		err = db.QueryRow(`select id, status, completed_at, stale, canonical_count, live_count,
  hash_divergence_count, unresolved_relation_count, malformed_skipped_count
from source_scan_runs
where source_kind = ?
order by started_at desc limit 1`, MemorySourceKind).Scan(
			&scanID, &status, &completed, &stale, &canonical, &live, &divergence, &unresolved, &malformed)
	}
	if err == sql.ErrNoRows {
		// Still surface live dual-read aggregates when cards exist without scans.
		if scoped == "" {
			if aggErr := refreshAggregateMemoryHealth(db, &h); aggErr != nil {
				return h, aggErr
			}
		}
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
	if scoped == "" {
		if err := refreshAggregateMemoryScanHealth(db, &h); err != nil {
			return h, err
		}
	}
	if status == "completed" && completed.Valid {
		h.LastCompletedScanID = scanID
		h.LastCompletedAt = &completed.String
	} else {
		var lastID string
		var lastAt sql.NullString
		if scoped != "" {
			_ = db.QueryRow(`select id, completed_at from source_scan_runs
where source_kind = ? and status = 'completed' and json_extract(metadata_json, '$.memory_namespace') = ?
order by completed_at desc limit 1`, MemorySourceKind, scoped).Scan(&lastID, &lastAt)
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

	tx, err := db.Begin()
	if err != nil {
		return h, err
	}
	defer tx.Rollback()
	if scoped != "" {
		liveMap, err := liveMemoryExternalIDs(tx, scoped)
		if err != nil {
			return h, err
		}
		h.LiveCount = len(liveMap)
		unresolved, err = memoryUnresolvedRelationCount(tx, scoped)
		if err != nil {
			return h, err
		}
		h.UnresolvedRelations = unresolved
		return h, nil
	}
	liveCount, unresolvedCount, err := aggregateLiveMemoryHealth(tx)
	if err != nil {
		return h, err
	}
	h.LiveCount = liveCount
	h.UnresolvedRelations = unresolvedCount
	h.MemoryNamespace = ""
	return h, nil
}

// refreshAggregateMemoryScanHealth folds the latest scan state from every
// memory namespace into empty-namespace doctor/status health. A newer healthy
// namespace must not conceal a failed or stale scan in another namespace.
func refreshAggregateMemoryScanHealth(db *sql.DB, h *MemoryHealth) error {
	rows, err := db.Query(`
select status, stale from source_scan_runs current
where source_kind = ?
  and not exists (
    select 1 from source_scan_runs newer
    where newer.source_kind = current.source_kind
      and coalesce(json_extract(newer.metadata_json, '$.memory_namespace'), '') =
          coalesce(json_extract(current.metadata_json, '$.memory_namespace'), '')
      and (newer.started_at > current.started_at or
           (newer.started_at = current.started_at and newer.id > current.id))
  )`, MemorySourceKind)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var status string
		var stale int
		if err := rows.Scan(&status, &stale); err != nil {
			return err
		}
		if stale != 0 || status != "completed" {
			h.Stale = true
		}
		if status != "completed" {
			h.Partial = true
		}
	}
	return rows.Err()
}

func refreshAggregateMemoryHealth(db *sql.DB, h *MemoryHealth) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	liveCount, unresolvedCount, err := aggregateLiveMemoryHealth(tx)
	if err != nil {
		return err
	}
	if liveCount > 0 {
		h.LiveCount = liveCount
		h.UnresolvedRelations = unresolvedCount
		if h.Status == "absent" {
			h.Status = "present"
		}
	}
	return nil
}

func aggregateLiveMemoryHealth(tx *sql.Tx) (liveCount, unresolvedCount int, err error) {
	rows, err := tx.Query(`
select c.external_id
from collections c
join sources s on s.id = c.source_id
where s.kind = ?
  and c.kind = 'memory_cards'
  and c.external_id not like ?`, MemorySourceKind, memoryBackupPrefix+"%")
	if err != nil {
		return 0, 0, err
	}
	var collections []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return 0, 0, err
		}
		collections = append(collections, id)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, 0, err
	}
	for _, collection := range collections {
		live, err := liveMemoryExternalIDs(tx, collection)
		if err != nil {
			return 0, 0, err
		}
		liveCount += len(live)
		unresolved, err := memoryUnresolvedRelationCount(tx, collection)
		if err != nil {
			return 0, 0, err
		}
		unresolvedCount += unresolved
	}
	return liveCount, unresolvedCount, nil
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
