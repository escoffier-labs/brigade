package app

import (
	"compress/gzip"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/security"
)

type retentionPolicy struct {
	Name  string          `json:"name"`
	Tiers []retentionTier `json:"tiers"`
}

type retentionTier struct {
	Name          string   `json:"name"`
	SourceKinds   []string `json:"source_kinds"`
	ItemKinds     []string `json:"item_kinds"`
	OlderThanDays int      `json:"older_than_days"`
	Action        string   `json:"action"`
}

type prunePolicyMatch struct {
	Tier      retentionTier
	Source    string
	ItemKind  string
	Items     int64
	TextBytes int64
}

type prunePolicySummary struct {
	Policy              string           `json:"policy"`
	DryRun              bool             `json:"dry_run"`
	Apply               bool             `json:"apply"`
	ExportPath          string           `json:"export_path,omitempty"`
	MatchedItems        int64            `json:"matched_items"`
	DeletedItems        int64            `json:"deleted_items"`
	ExportedItems       int64            `json:"exported_items"`
	DeletedEvents       int64            `json:"deleted_events"`
	DeletedArtifacts    int64            `json:"deleted_artifacts"`
	DeletedRelations    int64            `json:"deleted_relations"`
	TombstonedRelations int64            `json:"tombstoned_relations"`
	DeletedFTSRows      int64            `json:"deleted_fts_rows"`
	DeletedTags         int64            `json:"deleted_tags"`
	DeletedMetadata     int64            `json:"deleted_metadata"`
	EstimatedTextBytes  int64            `json:"estimated_text_bytes"`
	BeforeSizeBytes     int64            `json:"before_size_bytes"`
	AfterSizeBytes      int64            `json:"after_size_bytes"`
	ReclaimedBytes      int64            `json:"reclaimed_bytes"`
	Tiers               []map[string]any `json:"tiers"`
	Operations          []string         `json:"operations"`
	UntrustedContext    bool             `json:"untrusted_context"`
	PrivateRuntimePaths bool             `json:"private_runtime_paths"`
	GeneratedAt         string           `json:"generated_at"`
}

func defaultRetentionPolicy() retentionPolicy {
	return retentionPolicy{
		Name: "default",
		Tiers: []retentionTier{{
			Name:          "default-operational-noise",
			ItemKinds:     []string{"tool_call", "command", "progress", "status", "event", "queue-operation"},
			OlderThanDays: 90,
			Action:        "delete",
		}},
	}
}

func cmdPrunePolicy(args []string, out, errw io.Writer) int {
	values, bools, rest, err := splitFlags(args, map[string]bool{"policy": true, "export": true}, map[string]bool{"json": true, "dry-run": true, "apply": true})
	if err != nil {
		return fatalf(errw, "prune policy: %s", err)
	}
	if len(rest) != 0 || (bools["apply"] && bools["dry-run"]) {
		return fatalf(errw, "usage: miseledger prune policy [--policy default|FILE] [--json] [--dry-run] [--apply --export PATH]")
	}
	policy, err := loadRetentionPolicy(values["policy"])
	if err != nil {
		return fatalf(errw, "prune policy: %s", err)
	}
	if bools["apply"] && strings.TrimSpace(values["export"]) == "" {
		return fatalf(errw, "usage: miseledger prune policy [--policy default|FILE] [--json] [--dry-run] [--apply --export PATH]")
	}
	db, paths, err := openMigrated()
	if err != nil {
		return fatalf(errw, "prune policy: %s", err)
	}
	defer db.Close()

	dryRun := !bools["apply"]
	result, ids, err := previewRetentionPolicy(db, policy)
	if err != nil {
		return fatalf(errw, "prune policy: %s", err)
	}
	result.DryRun = dryRun
	result.Apply = bools["apply"]
	result.ExportPath = values["export"]
	result.BeforeSizeBytes = fileSize(paths.DBPath)
	result.UntrustedContext = false
	result.PrivateRuntimePaths = true
	result.GeneratedAt = time.Now().UTC().Format(time.RFC3339Nano)

	if bools["apply"] {
		exported, err := exportPrunedAdapterJSONL(db, ids, values["export"])
		if err != nil {
			return fatalf(errw, "prune policy: %s", err)
		}
		result.ExportedItems = exported
		if err := applyPrunePolicy(db, ids, &result); err != nil {
			return fatalf(errw, "prune policy: %s", err)
		}
		_ = archive.Checkpoint(db, paths.DBPath)
		result.AfterSizeBytes = fileSize(paths.DBPath)
		result.ReclaimedBytes = result.BeforeSizeBytes - result.AfterSizeBytes
	} else {
		result.AfterSizeBytes = result.BeforeSizeBytes
	}

	if bools["json"] {
		writeJSON(out, result)
	} else if result.DryRun {
		fmt.Fprintf(out, "matched=%d dry_run=true policy=%s\n", result.MatchedItems, result.Policy)
	} else {
		fmt.Fprintf(out, "deleted=%d exported=%d export=%s policy=%s\n", result.DeletedItems, result.ExportedItems, result.ExportPath, result.Policy)
	}
	return 0
}

func loadRetentionPolicy(path string) (retentionPolicy, error) {
	path = strings.TrimSpace(path)
	if path == "" || path == "default" {
		return defaultRetentionPolicy(), nil
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return retentionPolicy{}, err
	}
	var policy retentionPolicy
	if err := json.Unmarshal(b, &policy); err != nil {
		return retentionPolicy{}, err
	}
	if policy.Name == "" {
		policy.Name = "custom"
	}
	if len(policy.Tiers) == 0 {
		return retentionPolicy{}, fmt.Errorf("policy %q has no tiers", path)
	}
	for i := range policy.Tiers {
		if strings.TrimSpace(policy.Tiers[i].Name) == "" {
			policy.Tiers[i].Name = fmt.Sprintf("tier-%d", i+1)
		}
		if policy.Tiers[i].Action == "" {
			policy.Tiers[i].Action = "delete"
		}
		if policy.Tiers[i].Action != "delete" {
			return retentionPolicy{}, fmt.Errorf("tier %q has unsupported action %q", policy.Tiers[i].Name, policy.Tiers[i].Action)
		}
		if policy.Tiers[i].OlderThanDays <= 0 {
			return retentionPolicy{}, fmt.Errorf("tier %q must set older_than_days > 0", policy.Tiers[i].Name)
		}
		if len(policy.Tiers[i].ItemKinds) == 0 {
			return retentionPolicy{}, fmt.Errorf("tier %q must set item_kinds", policy.Tiers[i].Name)
		}
	}
	return policy, nil
}

func previewRetentionPolicy(db *sql.DB, policy retentionPolicy) (prunePolicySummary, []string, error) {
	result := prunePolicySummary{
		Policy:     policy.Name,
		Tiers:      []map[string]any{},
		Operations: []string{"dry_run"},
	}
	ids := map[string]bool{}
	for _, tier := range policy.Tiers {
		matches, err := tierMatches(db, tier)
		if err != nil {
			return result, nil, err
		}
		for _, match := range matches {
			result.MatchedItems += match.Items
			result.EstimatedTextBytes += match.TextBytes
			result.Tiers = append(result.Tiers, map[string]any{
				"tier":                 match.Tier.Name,
				"action":               match.Tier.Action,
				"older_than_days":      match.Tier.OlderThanDays,
				"source_kind":          match.Source,
				"item_kind":            match.ItemKind,
				"matched_items":        match.Items,
				"estimated_text_bytes": match.TextBytes,
			})
		}
		tierIDs, err := tierItemIDs(db, tier)
		if err != nil {
			return result, nil, err
		}
		for _, id := range tierIDs {
			ids[id] = true
		}
	}
	out := make([]string, 0, len(ids))
	for id := range ids {
		out = append(out, id)
	}
	sort.Strings(out)
	result.MatchedItems = int64(len(out))
	return result, out, nil
}

func tierMatches(db *sql.DB, tier retentionTier) ([]prunePolicyMatch, error) {
	where, args := tierWhere(tier)
	rows, err := db.Query(`select s.kind, i.kind, count(*), coalesce(sum(length(coalesce(i.text,'')) + length(coalesce(i.summary,''))), 0)
from items i
join sources s on s.id = i.source_id
where `+where+`
group by s.kind, i.kind
order by s.kind, i.kind`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []prunePolicyMatch{}
	for rows.Next() {
		var match prunePolicyMatch
		match.Tier = tier
		if err := rows.Scan(&match.Source, &match.ItemKind, &match.Items, &match.TextBytes); err != nil {
			return nil, err
		}
		out = append(out, match)
	}
	return out, rows.Err()
}

func tierItemIDs(db *sql.DB, tier retentionTier) ([]string, error) {
	where, args := tierWhere(tier)
	rows, err := db.Query(`select i.id
from items i
join sources s on s.id = i.source_id
where `+where+`
order by i.id`, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

func tierWhere(tier retentionTier) (string, []any) {
	cutoff := time.Now().UTC().AddDate(0, 0, -tier.OlderThanDays).Format(time.RFC3339Nano)
	parts := []string{"datetime(i.created_at) < datetime(?)"}
	args := []any{cutoff}
	if len(tier.ItemKinds) > 0 {
		parts = append(parts, "i.kind in ("+placeholders(len(tier.ItemKinds))+")")
		for _, kind := range tier.ItemKinds {
			args = append(args, kind)
		}
	}
	if len(tier.SourceKinds) > 0 {
		parts = append(parts, "s.kind in ("+placeholders(len(tier.SourceKinds))+")")
		for _, kind := range tier.SourceKinds {
			args = append(args, kind)
		}
	}
	return strings.Join(parts, " and "), args
}

func placeholders(n int) string {
	if n <= 0 {
		return ""
	}
	parts := make([]string, n)
	for i := range parts {
		parts[i] = "?"
	}
	return strings.Join(parts, ",")
}

func exportPrunedAdapterJSONL(db *sql.DB, ids []string, path string) (int64, error) {
	af, err := security.CreateAtomicFile(path)
	if err != nil {
		return 0, err
	}
	defer func() { _ = af.Abort() }()
	gz := gzip.NewWriter(af.File)
	exported := int64(0)
	for _, id := range ids {
		var raw string
		if err := db.QueryRow(`select raw_json from items where id = ?`, id).Scan(&raw); err != nil {
			_ = gz.Close()
			return 0, err
		}
		if _, err := gz.Write([]byte(strings.TrimRight(raw, "\n") + "\n")); err != nil {
			_ = gz.Close()
			return 0, err
		}
		exported++
	}
	if err := gz.Close(); err != nil {
		return 0, err
	}
	return exported, af.Commit()
}

func applyPrunePolicy(db *sql.DB, ids []string, result *prunePolicySummary) error {
	if len(ids) == 0 {
		result.Operations = []string{"export", "delete", "fts_optimize", "wal_checkpoint"}
		return nil
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
	if _, err := tx.Exec(`create temporary table if not exists prune_policy_items(id text primary key)`); err != nil {
		return err
	}
	if _, err := tx.Exec(`delete from prune_policy_items`); err != nil {
		return err
	}
	stmt, err := tx.Prepare(`insert or ignore into prune_policy_items(id) values(?)`)
	if err != nil {
		return err
	}
	for _, id := range ids {
		if _, err := stmt.Exec(id); err != nil {
			_ = stmt.Close()
			return err
		}
	}
	if err := stmt.Close(); err != nil {
		return err
	}
	var execErr error
	result.DeletedTags, execErr = execRows(tx, `delete from item_tags where item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedMetadata, execErr = execRows(tx, `delete from item_metadata where item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedEvents, execErr = execRows(tx, `delete from events where item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedArtifacts, execErr = execRows(tx, `delete from artifacts where item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedFTSRows, execErr = execRows(tx, `delete from item_fts where item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedRelations, execErr = execRows(tx, `delete from relations where source_item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.TombstonedRelations, execErr = execRows(tx, `update relations set target_item_id = null where target_item_id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	result.DeletedItems, execErr = execRows(tx, `delete from items where id in (select id from prune_policy_items)`)
	if execErr != nil {
		return execErr
	}
	_, _ = tx.Exec(`insert into item_fts(item_fts) values('optimize')`)
	if err := tx.Commit(); err != nil {
		return err
	}
	committed = true
	result.Operations = []string{"export", "delete", "fts_optimize", "wal_checkpoint"}
	return nil
}

func execRows(tx *sql.Tx, query string, args ...any) (int64, error) {
	res, err := tx.Exec(query, args...)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return n, nil
}
