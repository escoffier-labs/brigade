package ingest

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"
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

// BeginMemoryScan inserts a running scan row and returns its id.
func BeginMemoryScan(db *sql.DB, sourcePath, engineVersion string) (string, error) {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	id := stableID("memory-scan", MemorySourceKind, sourcePath, now)
	meta, _ := json.Marshal(map[string]any{
		"capability":     MemoryCapability,
		"engine_version": engineVersion,
	})
	_, err := db.Exec(`insert into source_scan_runs(
  id, source_kind, source_path, started_at, status, stale, metadata_json
) values(?,?,?,?,?,?,?)`, id, MemorySourceKind, sourcePath, now, "running", 0, string(meta))
	return id, err
}

// FailMemoryScan marks the scan failed/interrupted, tombstones nothing, and
// marks the prior completed snapshot stale.
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
	staleMarked, err := markPriorMemorySnapshotStale(tx)
	if err != nil {
		return err
	}
	if receipt == nil {
		receipt = &MemoryScanReceipt{}
	}
	receipt.ScanID = scanID
	receipt.SourceKind = MemorySourceKind
	receipt.Status = status
	receipt.Stale = true
	receipt.Partial = true
	receipt.Capability = MemoryCapability
	receipt.PriorSnapshotMarkedStale = staleMarked
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
	return tx.Commit()
}

// CompleteMemoryScan records observed ids, soft-tombstones missing memory_card
// items scoped to brigade-memory only, and writes the receipt counts.
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

	removed, err := softTombstoneMissingMemoryCards(tx, seen, now)
	if err != nil {
		return err
	}
	receipt.Removed = removed

	live, err := liveMemoryExternalIDs(tx)
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

	unresolved, err := memoryUnresolvedRelationCount(tx)
	if err != nil {
		return err
	}
	receipt.UnresolvedRelations = unresolved
	receipt.MalformedSkipped = receipt.Skipped + receipt.Failed

	if _, err := resolveRelations(tx); err != nil {
		return err
	}

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

func markPriorMemorySnapshotStale(tx *sql.Tx) (bool, error) {
	res, err := tx.Exec(`update source_scan_runs set stale = 1
where source_kind = ? and status = 'completed' and stale = 0`, MemorySourceKind)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

func softTombstoneMissingMemoryCards(tx *sql.Tx, seen map[string]ObservedCard, now string) (int, error) {
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
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null`, MemorySourceKind)
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

func liveMemoryExternalIDs(tx *sql.Tx) (map[string]string, error) {
	rows, err := tx.Query(`
select i.external_id, i.content_hash
from items i
join sources s on s.id = i.source_id
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null
order by i.created_at desc, i.id desc`, MemorySourceKind)
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

func memoryUnresolvedRelationCount(tx *sql.Tx) (int, error) {
	var n int
	err := tx.QueryRow(`
select count(*)
from relations r
join items i on i.id = r.source_item_id
join sources s on s.id = i.source_id
where s.kind = ?
  and i.kind = 'memory_card'
  and i.tombstoned_at is null
  and r.target_item_id is null
  and coalesce(r.target_external_id, '') != ''`, MemorySourceKind).Scan(&n)
	return n, err
}

// LiveMemoryProjection returns live external_id -> content_hash for rebuild checks.
func LiveMemoryProjection(db *sql.DB) (map[string]string, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	return liveMemoryExternalIDs(tx)
}

// RebuildMemoryProjection deletes only the brigade-memory derived projection.
func RebuildMemoryProjection(db *sql.DB) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	rows, err := tx.Query(`select i.id from items i join sources s on s.id = i.source_id where s.kind = ?`, MemorySourceKind)
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
	if _, err := tx.Exec(`delete from source_scans where source_kind = ?`, MemorySourceKind); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from source_scan_observed where scan_id in (select id from source_scan_runs where source_kind = ?)`, MemorySourceKind); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from source_scan_runs where source_kind = ?`, MemorySourceKind); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from collections where source_id in (select id from sources where kind = ?)`, MemorySourceKind); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from actors where source_id in (select id from sources where kind = ?)`, MemorySourceKind); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from sources where kind = ?`, MemorySourceKind); err != nil {
		return err
	}
	return tx.Commit()
}

// MemoryHealth summarizes doctor/status fields for the memory projection.
type MemoryHealth struct {
	Capability          string  `json:"capability"`
	EngineVersion       string  `json:"engine_version"`
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
func CollectMemoryHealth(db *sql.DB, engineVersion string) (MemoryHealth, error) {
	h := MemoryHealth{
		Capability:    MemoryCapability,
		EngineVersion: engineVersion,
		Status:        "absent",
	}
	var scanID, status string
	var completed sql.NullString
	var stale, canonical, live, divergence, unresolved, malformed int
	err := db.QueryRow(`select id, status, completed_at, stale, canonical_count, live_count,
  hash_divergence_count, unresolved_relation_count, malformed_skipped_count
from source_scan_runs
where source_kind = ?
order by started_at desc limit 1`, MemorySourceKind).Scan(
		&scanID, &status, &completed, &stale, &canonical, &live, &divergence, &unresolved, &malformed)
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
		_ = db.QueryRow(`select id, completed_at from source_scan_runs
where source_kind = ? and status = 'completed'
order by completed_at desc limit 1`, MemorySourceKind).Scan(&lastID, &lastAt)
		h.LastCompletedScanID = lastID
		if lastAt.Valid {
			h.LastCompletedAt = &lastAt.String
		}
	}

	// Prefer live counts from the archive when present.
	tx, err := db.Begin()
	if err != nil {
		return h, err
	}
	defer tx.Rollback()
	liveMap, err := liveMemoryExternalIDs(tx)
	if err != nil {
		return h, err
	}
	h.LiveCount = len(liveMap)
	unresolved, err = memoryUnresolvedRelationCount(tx)
	if err != nil {
		return h, err
	}
	h.UnresolvedRelations = unresolved
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
func PriorLiveHashes(db *sql.DB) (map[string]string, error) {
	return LiveMemoryProjection(db)
}

// SoftTombstoneCount returns how many live memory_card rows would be removed
// for the given observed set without writing. Used by interrupted-scan tests.
func SoftTombstoneCount(db *sql.DB, observedExternalIDs []string) (int, error) {
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
where s.kind = ? and i.kind = 'memory_card' and i.tombstoned_at is null`, MemorySourceKind)
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
