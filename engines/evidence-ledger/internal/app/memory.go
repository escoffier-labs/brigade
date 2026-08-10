package app

import (
	"fmt"
	"io"
	"path/filepath"

	"github.com/escoffier-labs/miseledger/internal/archive"
	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/sources/memory"
)

func cmdCrawlMemory(args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: miseledger crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]")
		return 0
	}
	values, bools, rest, err := splitFlags(args, map[string]bool{"limit": true}, map[string]bool{"json": true, "dry-run": true, "rebuild": true, "full": true})
	if err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}
	if len(rest) != 1 {
		return fatalf(errw, "usage: miseledger crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]")
	}
	limit, err := parseLimit(values["limit"], 0)
	if err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}
	workspace := rest[0]
	if abs, absErr := filepath.Abs(workspace); absErr == nil {
		workspace = abs
	}

	if bools["dry-run"] {
		outcomes, generated, walkErr := memory.Walk(workspace, sources.Options{Limit: limit})
		if walkErr != nil {
			return fatalf(errw, "crawl memory: %s", walkErr)
		}
		skipped, failed := 0, 0
		for _, card := range outcomes {
			switch card.Outcome {
			case "skipped":
				skipped++
			case "failed":
				failed++
			}
		}
		ns, _ := memory.ResolveNamespace(workspace)
		writeJSON(out, map[string]any{
			"source_kind":       ingest.MemorySourceKind,
			"capability":        ingest.MemoryCapability,
			"engine_version":    Version,
			"memory_namespace":  ns,
			"dry_run":           true,
			"generated_records": generated.Records,
			"canonical_count":   countMemoryRecords(outcomes),
			"skipped":           skipped,
			"failed":            failed,
			"warnings":          generated.Warnings,
			"files":             generated.Files,
		})
		return 0
	}

	db, paths, err := openMigrated()
	if err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}
	defer db.Close()

	// Validate/walk before any destructive rebuild so a missing root cannot
	// wipe the prior projection.
	opts := sources.Options{Limit: limit}
	if bools["full"] || bools["rebuild"] {
		opts.Skip = func(string, int64, string) bool { return false }
	}
	outcomes, generated, walkErr := memory.Walk(workspace, opts)

	namespace := ""
	if walkErr == nil {
		namespace, err = memory.ResolveNamespace(workspace)
		if err != nil {
			return fatalf(errw, "crawl memory: %s", err)
		}
	} else {
		namespace, _ = memory.ResolveNamespace(workspace)
		if namespace == "" {
			namespace = ingest.LastMemoryNamespace(db)
		}
	}

	if walkErr != nil {
		scanID := ""
		var beginErr error
		if namespace != "" {
			scanID, beginErr = ingest.BeginMemoryScan(db, workspace, Version, namespace)
		}
		receipt := &ingest.MemoryScanReceipt{
			SourcePath: workspace, EngineVersion: Version, Namespace: namespace, Failed: 1,
		}
		if beginErr == nil && scanID != "" {
			_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)
		} else if namespace != "" {
			_ = ingest.FailMemoryScan(db, "", "failed", receipt)
		}
		return fatalf(errw, "crawl memory: %s", walkErr)
	}

	if dups := memory.DuplicateExplicitIDs(outcomes); len(dups) > 0 {
		scanID, beginErr := ingest.BeginMemoryScan(db, workspace, Version, namespace)
		receipt := &ingest.MemoryScanReceipt{
			SourcePath: workspace, EngineVersion: Version, Namespace: namespace,
			Failed: 1, Warnings: append([]string{}, generated.Warnings...),
		}
		if beginErr == nil {
			_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)
		}
		return fatalf(errw, "crawl memory: duplicate explicit id(s) %v; refusing reconciliation", dups)
	}

	if err := ingest.RecoverMemoryRebuildState(db, namespace); err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}

	before, err := ingest.PriorLiveHashes(db, namespace)
	if err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}

	// Begin the scan before a rebuild detaches the live projection. This row is
	// the durable journal that lets recovery mark a crash between detach and
	// import as interrupted instead of silently restoring a healthy snapshot.
	scanID, err := ingest.BeginMemoryScan(db, workspace, Version, namespace)
	if err != nil {
		return fatalf(errw, "crawl memory: %s", err)
	}

	detached := false
	abortRebuild := func() {
		if detached {
			_ = ingest.AbortMemoryRebuild(db, namespace)
			detached = false
		}
	}

	if bools["rebuild"] {
		if err := ingest.DetachMemoryNamespace(db, namespace); err != nil {
			receipt := &ingest.MemoryScanReceipt{
				SourcePath: workspace, EngineVersion: Version, Namespace: namespace, Failed: 1,
			}
			_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)
			return fatalf(errw, "crawl memory rebuild: %s", err)
		}
		detached = true
		before = map[string]string{}
	}

	skipped, failed := 0, 0
	var observed []ingest.ObservedCard
	var emit []memory.CardOutcome
	for _, card := range outcomes {
		switch card.Outcome {
		case "skipped":
			skipped++
			observed = append(observed, ingest.ObservedCard{
				ExternalID: card.ExternalID, ContentHash: card.ContentHash, RawPath: card.RawPath,
				Identity: card.IdentitySource, Outcome: "skipped",
			})
		case "failed":
			failed++
			observed = append(observed, ingest.ObservedCard{
				ExternalID: card.ExternalID, ContentHash: card.ContentHash, RawPath: card.RawPath,
				Identity: card.IdentitySource, Outcome: "failed",
			})
		default:
			if card.Record != nil {
				emit = append(emit, card)
			}
		}
	}

	pr, pw := io.Pipe()
	type genResult struct {
		err error
	}
	done := make(chan genResult, 1)
	go func() {
		var writeErr error
		for _, card := range emit {
			rec := *card.Record
			if err := sources.WriteRecord(pw, rec); err != nil {
				writeErr = err
				break
			}
			if err := ingest.WriteSourceScanSentinel(pw, sources.FileScan{
				Path:        filepath.Join(workspace, filepath.FromSlash(card.RawPath)),
				ContentHash: card.ContentHash,
				Records:     1,
			}); err != nil {
				writeErr = err
				break
			}
		}
		if writeErr != nil {
			_ = pw.CloseWithError(writeErr)
		} else {
			_ = pw.Close()
		}
		done <- genResult{err: writeErr}
	}()

	recordScan := func(sourceKind, generatedHash string, file sources.FileScan) error {
		return ingest.RecordSourceScans(db, sourceKind, generatedHash, []sources.FileScan{file}, true)
	}
	result, importErr := ingest.ImportNativeReaderProgress(db, pr, workspace, ingest.MemorySourceKind, nil, recordScan)
	gen := <-done
	if importErr != nil || gen.err != nil {
		errMsg := importErr
		if errMsg == nil {
			errMsg = gen.err
		}
		abortRebuild()
		receipt := &ingest.MemoryScanReceipt{
			SourcePath: workspace, EngineVersion: Version, Namespace: namespace,
			Skipped: skipped, Failed: failed + 1,
			Warnings: append(generated.Warnings, result.Warnings...),
		}
		_ = ingest.FailMemoryScan(db, scanID, "interrupted", receipt)
		return fatalf(errw, "crawl memory: %s", errMsg)
	}

	for _, card := range emit {
		observed = append(observed, ingest.ObservedCard{
			ExternalID:  card.ExternalID,
			ContentHash: card.ContentHash,
			RawPath:     card.RawPath,
			Identity:    card.IdentitySource,
		})
	}
	created, updated, unchanged, classified := ingest.ClassifyMemoryOutcomes(before, observed)

	receipt := &ingest.MemoryScanReceipt{
		SourcePath:    workspace,
		Namespace:     namespace,
		EngineVersion: Version,
		Created:       created,
		Updated:       updated,
		Unchanged:     unchanged,
		Skipped:       skipped,
		Failed:        failed,
		Warnings:      append(generated.Warnings, result.Warnings...),
	}
	if err := ingest.CompleteMemoryScan(db, scanID, observedForReconcile(classified), receipt); err != nil {
		abortRebuild()
		_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)
		return fatalf(errw, "crawl memory: %s", err)
	}
	if detached {
		if err := ingest.FinalizeMemoryRebuild(db, namespace); err != nil {
			abortRebuild()
			_ = ingest.FailMemoryScan(db, scanID, "failed", receipt)
			return fatalf(errw, "crawl memory rebuild finalize: %s", err)
		}
		detached = false
	}
	_ = archive.Checkpoint(db, paths.DBPath)

	health, _ := ingest.CollectMemoryHealth(db, Version, namespace)
	payload := map[string]any{
		"scan_id":              receipt.ScanID,
		"source_kind":          ingest.MemorySourceKind,
		"capability":           ingest.MemoryCapability,
		"engine_version":       Version,
		"memory_namespace":     namespace,
		"status":               receipt.Status,
		"stale":                receipt.Stale,
		"partial":              receipt.Partial,
		"created":              receipt.Created,
		"updated":              receipt.Updated,
		"unchanged":            receipt.Unchanged,
		"removed":              receipt.Removed,
		"skipped":              receipt.Skipped,
		"failed":               receipt.Failed,
		"canonical_count":      receipt.CanonicalCount,
		"live_count":           receipt.LiveCount,
		"hash_divergence":      receipt.HashDivergence,
		"unresolved_relations": receipt.UnresolvedRelations,
		"malformed_skipped":    receipt.MalformedSkipped,
		"inserted_items":       result.Inserted,
		"warnings":             receipt.Warnings,
		"memory_health":        health,
		"rebuild":              bools["rebuild"],
	}
	if bools["json"] {
		writeJSON(out, payload)
	} else {
		fmt.Fprintf(out, "scan=%s namespace=%s created=%d updated=%d unchanged=%d removed=%d skipped=%d failed=%d live=%d\n",
			receipt.ScanID, namespace, receipt.Created, receipt.Updated, receipt.Unchanged, receipt.Removed, receipt.Skipped, receipt.Failed, receipt.LiveCount)
	}
	return 0
}

func observedForReconcile(cards []ingest.ObservedCard) []ingest.ObservedCard {
	out := make([]ingest.ObservedCard, 0, len(cards))
	for _, card := range cards {
		if card.ExternalID == "" {
			continue
		}
		out = append(out, card)
	}
	return out
}

func countMemoryRecords(outcomes []memory.CardOutcome) int {
	n := 0
	for _, o := range outcomes {
		if o.Record != nil {
			n++
		}
	}
	return n
}
