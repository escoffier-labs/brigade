package ingest

import (
	"bufio"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

type AdapterResult struct {
	ImportID     string   `json:"import_id"`
	SourceKind   string   `json:"source_kind"`
	SourcePath   string   `json:"source_path"`
	SourceHash   string   `json:"source_hash"`
	Inserted     int      `json:"inserted_items"`
	Warnings     []string `json:"warnings"`
	AlreadyKnown bool     `json:"already_known"`
	FilesParsed  int      `json:"files_parsed"`
	FilesSkipped int      `json:"files_skipped"`
}

func ImportAdapterFile(db *sql.DB, path, sourceOverride string) (AdapterResult, error) {
	return ImportAdapterFileProgress(db, path, sourceOverride, nil)
}

// ImportAdapterFileProgress is ImportAdapterFile with a progress callback
// invoked (if non-nil) with the running inserted count after each committed
// batch.
func ImportAdapterFileProgress(db *sql.DB, path, sourceOverride string, progress func(inserted int)) (AdapterResult, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return AdapterResult{}, err
	}
	f, err := os.Open(abs)
	if err != nil {
		return AdapterResult{}, err
	}
	defer f.Close()
	return ImportAdapterReaderProgress(db, f, abs, sourceOverride, progress)
}

// importBatchSize is how many records are committed per transaction. Batching
// keeps the WAL bounded, makes partial progress durable across a crash, and is
// the cadence at which import progress is reported.
const importBatchSize = 1000

func ImportAdapterReader(db *sql.DB, r io.Reader, sourcePath, sourceOverride string) (AdapterResult, error) {
	return ImportAdapterReaderProgress(db, r, sourcePath, sourceOverride, nil)
}

// ImportAdapterReaderProgress imports adapter JSONL, committing every
// importBatchSize records and invoking progress (if non-nil) with the running
// inserted count after each committed batch. Inserts are idempotent
// (INSERT OR IGNORE), so a crash mid-import loses at most the open batch and a
// re-run safely resumes.
func ImportAdapterReaderProgress(db *sql.DB, r io.Reader, sourcePath, sourceOverride string, progress func(inserted int)) (AdapterResult, error) {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	sourceKind := sourceOverride

	result := AdapterResult{SourceKind: sourceKind, SourcePath: sourcePath}
	h := sha256.New()
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	ordinal := int64(0)

	tx, err := db.Begin()
	if err != nil {
		return AdapterResult{}, err
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()
	batchCount := 0
	flush := func() error {
		if err := tx.Commit(); err != nil {
			return err
		}
		if progress != nil {
			progress(result.Inserted)
		}
		next, err := db.Begin()
		if err != nil {
			return err
		}
		tx = next
		batchCount = 0
		return nil
	}

	for scanner.Scan() {
		line := append([]byte(nil), scanner.Bytes()...)
		_, _ = h.Write(line)
		_, _ = h.Write([]byte("\n"))
		ordinal++
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		rec, err := adapter.Parse(line)
		if err != nil {
			result.Warnings = append(result.Warnings, fmt.Sprintf("line %d: %s", ordinal, err))
			continue
		}
		if sourceOverride != "" {
			rec.Source.Kind = sourceOverride
		}
		if sourceKind == "" && rec.Source.Kind != "" {
			sourceKind = rec.Source.Kind
			result.SourceKind = sourceKind
		}
		inserted, err := upsertRecord(tx, rec, sourcePath, ordinal, line)
		if err != nil {
			result.Warnings = append(result.Warnings, fmt.Sprintf("line %d: %s", ordinal, err))
			continue
		}
		if inserted {
			result.Inserted++
		}
		batchCount++
		if batchCount >= importBatchSize {
			if err := flush(); err != nil {
				return AdapterResult{}, err
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return AdapterResult{}, err
	}
	sourceHash := "sha256:" + hex.EncodeToString(h.Sum(nil))
	if sourceKind == "" {
		sourceKind = "adapter"
		result.SourceKind = sourceKind
	}
	importID := stableID("import", sourceKind, sourcePath, sourceHash)
	result.ImportID = importID
	result.SourceHash = sourceHash

	var exists int
	err = tx.QueryRow("select count(*) from imports where source_kind = ? and source_hash = ? and completed_at is not null", sourceKind, sourceHash).Scan(&exists)
	if err != nil {
		return AdapterResult{}, err
	}
	if exists > 0 {
		// Same content already fully imported. The batches above only re-ran
		// idempotent INSERT OR IGNOREs (no-ops), so committing the final empty
		// tx is harmless and we skip writing a duplicate import row.
		result.AlreadyKnown = true
		committed = true
		return result, tx.Commit()
	}
	if _, err := tx.Exec(`insert or ignore into imports(id, source_kind, source_path, source_hash, started_at) values(?,?,?,?,?)`, importID, sourceKind, sourcePath, sourceHash, now); err != nil {
		return AdapterResult{}, err
	}
	for i, warning := range result.Warnings {
		if _, err := tx.Exec(`insert or replace into import_warnings(import_id, ordinal, warning) values(?,?,?)`, importID, i+1, warning); err != nil {
			return AdapterResult{}, err
		}
	}
	if _, err := resolveRelations(tx); err != nil {
		return AdapterResult{}, err
	}
	if _, err := tx.Exec(`update imports set completed_at = ?, item_count = ?, warning_count = ? where id = ?`, now, result.Inserted, len(result.Warnings), importID); err != nil {
		return AdapterResult{}, err
	}
	committed = true
	return result, tx.Commit()
}

func BackfillRelations(db *sql.DB) (int64, error) {
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	n, err := resolveRelations(tx)
	if err != nil {
		return 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return n, nil
}

func resolveRelations(tx *sql.Tx) (int64, error) {
	res, err := tx.Exec(`update relations
set target_item_id = (
  select target.id
  from items source
  join items target on target.source_id = source.source_id and target.external_id = relations.target_external_id
  where source.id = relations.source_item_id
  order by target.created_at, target.id
  limit 1
)
where target_item_id is null
  and target_external_id is not null
  and target_external_id != ''
  and exists (
    select 1
    from items source
    join items target on target.source_id = source.source_id and target.external_id = relations.target_external_id
    where source.id = relations.source_item_id
  )`)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}

// ScanRecord is a file's prior import state from the source_scans manifest.
type ScanRecord struct {
	Size        int64
	MTime       string
	ContentHash string
}

// LoadSourceScans returns the manifest for a source kind keyed by file path,
// so an incremental import can skip files whose size and mtime are unchanged.
func LoadSourceScans(db *sql.DB, sourceKind string) (map[string]ScanRecord, error) {
	rows, err := db.Query(`select path, size, mtime, content_hash from source_scans where source_kind = ?`, sourceKind)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]ScanRecord{}
	for rows.Next() {
		var path, mtime, hash string
		var size int64
		if err := rows.Scan(&path, &size, &mtime, &hash); err != nil {
			return nil, err
		}
		out[path] = ScanRecord{Size: size, MTime: mtime, ContentHash: hash}
	}
	return out, rows.Err()
}

func RecordSourceScans(db *sql.DB, sourceKind, generatedHash string, files []sources.FileScan, imported bool) error {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	importedAt := any(nil)
	if imported {
		importedAt = now
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			_ = tx.Rollback()
		}
	}()
	upsert, err := tx.Prepare(`insert into source_scans(id, source_kind, path, size, mtime, content_hash, generated_hash, first_seen_at, last_seen_at, last_imported_at, records_generated, warnings)
values(?,?,?,?,?,?,?,?,?,?,?,?)
on conflict(source_kind, path) do update set
  size=excluded.size,
  mtime=excluded.mtime,
  content_hash=excluded.content_hash,
  generated_hash=excluded.generated_hash,
  last_seen_at=excluded.last_seen_at,
  last_imported_at=coalesce(excluded.last_imported_at, source_scans.last_imported_at),
  records_generated=excluded.records_generated,
  warnings=excluded.warnings`)
	if err != nil {
		return err
	}
	defer upsert.Close()
	touchSkipped, err := tx.Prepare(`update source_scans set last_seen_at = ? where source_kind = ? and path = ?`)
	if err != nil {
		return err
	}
	defer touchSkipped.Close()
	refreshSkipped, err := tx.Prepare(`update source_scans set size = ?, mtime = ?, content_hash = ?, last_seen_at = ? where source_kind = ? and path = ?`)
	if err != nil {
		return err
	}
	defer refreshSkipped.Close()
	for _, file := range files {
		// A pure size+mtime skip was never read, so only touch last_seen_at.
		// A hash-fallback skip was read for hashing but not parsed, so refresh
		// file metadata while preserving record counts and generated hash.
		if file.Skipped {
			if file.ContentHash != "" {
				if _, err := refreshSkipped.Exec(file.Size, file.MTime, file.ContentHash, now, sourceKind, file.Path); err != nil {
					return err
				}
			} else {
				if _, err := touchSkipped.Exec(now, sourceKind, file.Path); err != nil {
					return err
				}
			}
			continue
		}
		id := stableID("scan", sourceKind, file.Path)
		_, err := upsert.Exec(id, sourceKind, file.Path, file.Size, file.MTime, file.ContentHash, generatedHash, now, now, importedAt, file.Records, file.Warnings)
		if err != nil {
			return err
		}
	}
	committed = true
	return tx.Commit()
}

func upsertRecord(tx *sql.Tx, rec adapter.Record, sourcePath string, ordinal int64, raw []byte) (bool, error) {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	sourceID := stableID("source", rec.Source.Kind)
	collectionID := stableID("collection", rec.Source.Kind, rec.Collection.ExternalID)
	actorID := ""
	if rec.Actor != nil && rec.Actor.ExternalID != "" {
		actorID = stableID("actor", rec.Source.Kind, rec.Actor.ExternalID)
	}
	summary := ""
	if rec.Item.Summary != nil {
		summary = *rec.Item.Summary
	}
	body := textnorm.Normalize(strings.TrimSpace(rec.Item.Text + "\n" + summary))
	contentHash := "sha256:" + hashString(body)
	itemID := stableID("item", rec.Source.Kind, rec.Collection.ExternalID, rec.Item.ExternalID, contentHash)
	rawHash := rec.Raw.Hash
	if rawHash == "" {
		rawHash = "sha256:" + hashBytes(raw)
	}
	rawPath := rec.Raw.Path
	if rawPath == "" {
		rawPath = sourcePath
	}
	rawOrdinal := ordinal
	if rec.Raw.Ordinal != nil {
		rawOrdinal = *rec.Raw.Ordinal
	}
	collectionMeta := rawOrEmptyObject(rec.Collection.Metadata)

	// Fast-path already-imported items. Items are content-addressed (itemID
	// includes the content hash), so an existing row means this exact record is
	// already stored. Short-circuit before the sources/collections/actors
	// upserts: those bump updated_at on every call, so without this probe a
	// re-run (or a retry after a timeout) rewrites the whole committed prefix
	// row by row and makes no forward progress within a time budget.
	var known int
	if err := tx.QueryRow(`select 1 from items where id = ?`, itemID).Scan(&known); err == nil {
		return false, nil
	} else if err != sql.ErrNoRows {
		return false, err
	}

	if _, err := tx.Exec(`insert into sources(id, kind, name, version, created_at, updated_at) values(?,?,?,?,?,?)
on conflict(id) do update set name=excluded.name, version=excluded.version, updated_at=excluded.updated_at`, sourceID, rec.Source.Kind, rec.Source.Name, rec.Source.Version, now, now); err != nil {
		return false, err
	}
	if _, err := tx.Exec(`insert into collections(id, source_id, external_id, kind, name, metadata_json, created_at, updated_at) values(?,?,?,?,?,?,?,?)
on conflict(source_id, external_id) do update set kind=excluded.kind, name=excluded.name, metadata_json=excluded.metadata_json, updated_at=excluded.updated_at`, collectionID, sourceID, rec.Collection.ExternalID, rec.Collection.Kind, rec.Collection.Name, collectionMeta, now, now); err != nil {
		return false, err
	}
	if actorID != "" {
		actorMeta := rawOrEmptyObject(rec.Actor.Metadata)
		if _, err := tx.Exec(`insert into actors(id, source_id, external_id, type, name, metadata_json) values(?,?,?,?,?,?)
on conflict(source_id, external_id) do update set type=excluded.type, name=excluded.name, metadata_json=excluded.metadata_json`, actorID, sourceID, rec.Actor.ExternalID, rec.Actor.Type, rec.Actor.Name, actorMeta); err != nil {
			return false, err
		}
	}
	itemMeta := itemMetadataJSON(rec)
	res, err := tx.Exec(`insert or ignore into items(id, source_id, collection_id, actor_id, external_id, kind, created_at, updated_at, text, summary, content_hash, raw_json, raw_hash, raw_path, raw_ordinal, metadata_json)
values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`, itemID, sourceID, collectionID, nullIfEmpty(actorID), rec.Item.ExternalID, rec.Item.Kind, rec.Item.CreatedAt, rec.Item.UpdatedAt, rec.Item.Text, summary, contentHash, string(raw), rawHash, rawPath, rawOrdinal, string(itemMeta))
	if err != nil {
		return false, err
	}
	insertedRows, _ := res.RowsAffected()
	if insertedRows == 0 {
		return false, nil
	}
	eventID := stableID("event", itemID, rec.Item.CreatedAt, rec.Item.Kind)
	_, _ = tx.Exec(`insert or ignore into events(id, source_id, collection_id, actor_id, item_id, kind, occurred_at) values(?,?,?,?,?,?,?)`, eventID, sourceID, collectionID, nullIfEmpty(actorID), itemID, rec.Item.Kind, rec.Item.CreatedAt)
	if err := indexItemMetadata(tx, itemID, rec.Item.Tags, itemMeta); err != nil {
		return false, err
	}
	for _, art := range rec.Artifacts {
		artifactID := stableID("artifact", itemID, art.ExternalID, art.Kind, art.Path, art.URL, art.Hash)
		artifactHash := art.Hash
		if artifactHash == "" && art.Text != "" {
			artifactHash = "sha256:" + hashString(textnorm.Normalize(art.Text))
		}
		if _, err := tx.Exec(`insert or ignore into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json) values(?,?,?,?,?,?,?,?,?,?,?)`, artifactID, sourceID, itemID, art.ExternalID, art.Kind, art.Path, art.URL, art.MimeType, art.Text, artifactHash, rawOrEmptyObject(art.Metadata)); err != nil {
			return false, err
		}
		if art.Text != "" {
			body += "\n" + textnorm.Normalize(art.Text)
		}
	}
	for _, link := range rec.Links {
		if link.URL == "" {
			continue
		}
		artifactID := stableID("artifact", itemID, "link", link.URL)
		meta, _ := json.Marshal(map[string]any{"link_text": link.Text})
		if _, err := tx.Exec(`insert or ignore into artifacts(id, source_id, item_id, external_id, kind, path, url, mime_type, text, content_hash, metadata_json) values(?,?,?,?,?,?,?,?,?,?,?)`, artifactID, sourceID, itemID, link.URL, "url", "", link.URL, "text/uri-list", link.Text, "sha256:"+hashString(link.URL), string(meta)); err != nil {
			return false, err
		}
		body += "\n" + textnorm.Normalize(link.URL+" "+link.Text)
	}
	for _, rel := range rec.Relations {
		confidence := 1.0
		if rel.Confidence != nil {
			confidence = *rel.Confidence
		}
		relID := stableID("relation", itemID, rel.TargetItemID, rel.TargetExternalID, rel.Type)
		if _, err := tx.Exec(`insert or ignore into relations(id, source_item_id, target_item_id, target_external_id, relation_type, confidence, metadata_json) values(?,?,?,?,?,?,?)`, relID, itemID, nullIfEmpty(rel.TargetItemID), nullIfEmpty(rel.TargetExternalID), rel.Type, confidence, rawOrEmptyObject(rel.Metadata)); err != nil {
			return false, err
		}
	}
	actorType := ""
	if rec.Actor != nil {
		actorType = rec.Actor.Type
	}
	if _, err := tx.Exec(`insert into item_fts(item_id, source_kind, collection_kind, item_kind, actor_type, body) values(?,?,?,?,?,?)`, itemID, rec.Source.Kind, rec.Collection.Kind, rec.Item.Kind, actorType, body); err != nil {
		return false, err
	}
	return true, nil
}

func itemMetadataJSON(rec adapter.Record) []byte {
	meta := map[string]any{"tags": rec.Item.Tags}
	if len(rec.Item.Metadata) > 0 {
		var parsed map[string]any
		if json.Unmarshal(rec.Item.Metadata, &parsed) == nil {
			for k, v := range parsed {
				meta[k] = v
			}
		} else {
			meta["adapter_metadata_raw"] = string(rec.Item.Metadata)
		}
	}
	b, _ := json.Marshal(meta)
	return b
}

func indexItemMetadata(tx *sql.Tx, itemID string, tags []string, metaJSON []byte) error {
	for _, tag := range tags {
		tag = strings.TrimSpace(tag)
		if tag == "" {
			continue
		}
		if _, err := tx.Exec(`insert or ignore into item_tags(item_id, tag) values(?,?)`, itemID, tag); err != nil {
			return err
		}
	}
	var meta map[string]any
	if err := json.Unmarshal(metaJSON, &meta); err != nil {
		return nil
	}
	for _, key := range []string{"project", "workspace", "workspace_dir", "cwd", "harness", "event_type", "session_id", "run_id", "model", "file_path"} {
		if value, ok := meta[key]; ok {
			for _, s := range metadataStrings(value) {
				if s == "" {
					continue
				}
				if _, err := tx.Exec(`insert or ignore into item_metadata(item_id, key, value) values(?,?,?)`, itemID, key, s); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

func metadataStrings(v any) []string {
	switch t := v.(type) {
	case string:
		return []string{t}
	case []any:
		out := make([]string, 0, len(t))
		for _, x := range t {
			out = append(out, metadataStrings(x)...)
		}
		return out
	case float64, bool:
		return []string{fmt.Sprint(t)}
	default:
		return nil
	}
}

func hashString(s string) string { return hashBytes([]byte(s)) }

func hashBytes(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func stableID(parts ...string) string {
	h := sha256.New()
	for _, p := range parts {
		_, _ = io.WriteString(h, p)
		_, _ = io.WriteString(h, "\x00")
	}
	return hex.EncodeToString(h.Sum(nil))[:24]
}

func rawOrEmptyObject(raw json.RawMessage) string {
	if len(raw) == 0 {
		return "{}"
	}
	return string(raw)
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}
