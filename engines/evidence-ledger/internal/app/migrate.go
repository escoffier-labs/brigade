package app

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/provenance"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

var migrateBatchSize = 1000

// cmdMigrate dispatches `miseledger migrate <target>`; mirrors cmdPrune.
func cmdMigrate(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
	if args[0] == "--help" || args[0] == "-h" || args[0] == "help" {
		return writeMigrateHelp(out)
	}
	switch args[0] {
	case "codex-arguments":
		return cmdMigrateCodexArguments(args[1:], out, errw)
	default:
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
}

func writeMigrateHelp(w io.Writer) int {
	fmt.Fprintln(w, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "Rewrite stored Codex tool-call rows to truncate oversized arguments and remove duplicate arguments.")
	fmt.Fprintln(w, "Note: Candidate selection performs a full-table LIKE scan across items for matching metadata.")
	return 0
}

// cmdMigrateCodexArguments scans for Codex tool-call items whose arguments exceed
// sources.DefaultTextCap characters and truncates them, keeping a SHA-256 digest in metadata.
// Note: Candidate selection performs a full-table LIKE scan across items for matching metadata.
func cmdMigrateCodexArguments(args []string, out, errw io.Writer) int {
	_, bools, rest, err := splitFlags(args, nil, map[string]bool{"dry-run": true, "apply": true, "json": true, "help": true})
	if err != nil {
		return fatalf(errw, "migrate codex-arguments: %s", err)
	}
	if bools["help"] || (len(rest) == 1 && (rest[0] == "help" || rest[0] == "-h")) {
		return writeMigrateHelp(out)
	}
	if len(rest) != 0 || (bools["apply"] && bools["dry-run"]) {
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
	isDryRun := true
	if bools["apply"] {
		isDryRun = false
	}
	isJSON := bools["json"]

	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "migrate codex-arguments: %s", err)
	}
	defer db.Close()

	// Prepass: load item_id -> fts rowid for all codex rows in one scan (bounded memory).
	ftsRowIDs := make(map[string]int64)
	ftsRows, err := db.Query(`SELECT rowid, item_id FROM item_fts WHERE source_kind = 'codex'`)
	if err != nil {
		return fatalf(errw, "migrate codex-arguments fts prepass query: %s", err)
	}
	for ftsRows.Next() {
		var rID int64
		var itmID string
		if err := ftsRows.Scan(&rID, &itmID); err != nil {
			ftsRows.Close()
			return fatalf(errw, "migrate codex-arguments fts prepass scan: %s", err)
		}
		if itmID != "" {
			ftsRowIDs[itmID] = rID
		}
	}
	if err := ftsRows.Err(); err != nil {
		ftsRows.Close()
		return fatalf(errw, "migrate codex-arguments fts prepass rows: %s", err)
	}
	ftsRows.Close()

	type updateRow struct {
		id           string
		rawJSON      string
		metadataJSON string
		text         string
		ftsUpdated   bool
		ftsRowID     int64
		ftsBody      string
	}
	var matchedRows int
	var bytesSaved int
	var ftsMissing int

	lastID := ""
	batchSize := migrateBatchSize

	for {
		rows, err := db.Query(`
			SELECT i.id, coalesce(i.text, ''), i.raw_json, i.metadata_json
			FROM items i
			JOIN sources s ON i.source_id = s.id
			WHERE s.kind = 'codex' AND i.metadata_json LIKE '%"arguments"%' AND i.id > ?
			ORDER BY i.id
			LIMIT ?
		`, lastID, batchSize)
		if err != nil {
			return fatalf(errw, "migrate codex-arguments query: %s", err)
		}

		type itemCandidate struct {
			id   string
			text string
			raw  string
			meta string
		}
		var candidates []itemCandidate
		for rows.Next() {
			var c itemCandidate
			if err := rows.Scan(&c.id, &c.text, &c.raw, &c.meta); err != nil {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments scan: %s", err)
			}
			candidates = append(candidates, c)
			lastID = c.id
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return fatalf(errw, "migrate codex-arguments rows: %s", err)
		}
		rows.Close()

		if len(candidates) == 0 {
			break
		}

		var batchUpdates []updateRow
		for _, c := range candidates {
			var metaObj map[string]any
			decMeta := json.NewDecoder(bytes.NewReader([]byte(c.meta)))
			decMeta.UseNumber()
			if err := decMeta.Decode(&metaObj); err != nil {
				continue
			}

			argsVal, ok := metaObj["arguments"]
			if !ok {
				continue
			}
			argsStr, ok := argsVal.(string)
			if !ok {
				continue
			}

			if len([]rune(argsStr)) <= sources.DefaultTextCap || isAlreadyTruncated(argsStr, metaObj) {
				continue
			}

			truncatedArgs := sources.TextFromAny(argsStr, sources.DefaultTextCap)
			digestBytes := sha256.Sum256([]byte(argsStr))
			digest := hex.EncodeToString(digestBytes[:])

			// Update raw_json first, mirroring the importer's ordering.
			var rawObj map[string]any
			decRaw := json.NewDecoder(bytes.NewReader([]byte(c.raw)))
			decRaw.UseNumber()
			if err := decRaw.Decode(&rawObj); err != nil {
				continue
			}

			// Update item.metadata and item.text inside the marshaled adapter.Record.
			// Ensure embedded metadata doesn't keep a stale provenance envelope.
			if itemObj, ok := rawObj["item"].(map[string]any); ok {
				if itemMeta, ok := itemObj["metadata"].(map[string]any); ok {
					itemMeta["arguments"] = truncatedArgs
					itemMeta["arguments_digest"] = digest
					delete(itemMeta, "provenance")
				}
				if itemText, ok := itemObj["text"].(string); ok && strings.Contains(itemText, argsStr) {
					itemObj["text"] = strings.Replace(itemText, argsStr, truncatedArgs, 1)
				}
			}

			newRawBytes, err := json.Marshal(rawObj)
			if err != nil {
				return fatalf(errw, "migrate codex-arguments marshal raw: %s", err)
			}
			newRaw := string(newRawBytes)

			newText := c.text
			if strings.Contains(c.text, argsStr) {
				newText = strings.Replace(c.text, argsStr, truncatedArgs, 1)
			}

			// Update metadata_json with truncated arguments, digest, and recomputed hashes over newText and newRaw.
			metaObj["arguments"] = truncatedArgs
			metaObj["arguments_digest"] = digest

			if prov, ok := metaObj["provenance"].(map[string]any); ok {
				hashes, ok := prov["hashes"].(map[string]any)
				if !ok {
					hashes = make(map[string]any)
					prov["hashes"] = hashes
				}
				hashes["content"] = provenance.ContentSHA256(newText)
				if _, ok := hashes["content_scope"]; !ok {
					hashes["content_scope"] = "item.text.utf8.v1"
				}
				if _, ok := hashes["content_algorithm"]; !ok {
					hashes["content_algorithm"] = provenance.HashAlgorithm
				}
				hashes["raw"] = provenance.SHA256Bytes([]byte(newRaw))
				if _, ok := hashes["raw_scope"]; !ok {
					hashes["raw_scope"] = provenance.RawScope
				}
				if _, ok := hashes["raw_algorithm"]; !ok {
					hashes["raw_algorithm"] = provenance.HashAlgorithm
				}
			}

			newMetaBytes, err := json.Marshal(metaObj)
			if err != nil {
				return fatalf(errw, "migrate codex-arguments marshal metadata: %s", err)
			}
			newMeta := string(newMetaBytes)

			ftsRowID, hasFTS := ftsRowIDs[c.id]
			ftsUpdated := false
			newFtsBody := ""
			if !hasFTS {
				ftsMissing++
			} else {
				var ftsBody string
				err := db.QueryRow(`SELECT body FROM item_fts WHERE rowid = ?`, ftsRowID).Scan(&ftsBody)
				if err == nil {
					newFtsBody = ftsBody
					normOldText := textnorm.Normalize(c.text)
					normNewText := textnorm.Normalize(newText)
					normOldArgs := textnorm.Normalize(argsStr)
					normNewArgs := textnorm.Normalize(truncatedArgs)

					if normOldText != "" && strings.Contains(ftsBody, normOldText) {
						newFtsBody = strings.Replace(ftsBody, normOldText, normNewText, 1)
					} else if normOldArgs != "" && strings.Contains(ftsBody, normOldArgs) {
						newFtsBody = strings.Replace(ftsBody, normOldArgs, normNewArgs, 1)
					} else if strings.Contains(ftsBody, argsStr) {
						newFtsBody = strings.Replace(ftsBody, argsStr, truncatedArgs, 1)
					}
					if newFtsBody != ftsBody {
						ftsUpdated = true
					}
				} else if err == sql.ErrNoRows {
					ftsMissing++
				} else {
					return fatalf(errw, "migrate codex-arguments fts query by rowid: %s", err)
				}
			}

			saved := len(c.raw) + len(c.meta) + len(c.text) - len(newRaw) - len(newMeta) - len(newText)
			bytesSaved += saved
			matchedRows++

			batchUpdates = append(batchUpdates, updateRow{
				id:           c.id,
				rawJSON:      newRaw,
				metadataJSON: newMeta,
				text:         newText,
				ftsUpdated:   ftsUpdated,
				ftsRowID:     ftsRowID,
				ftsBody:      newFtsBody,
			})
		}

		if !isDryRun && len(batchUpdates) > 0 {
			tx, err := db.Begin()
			if err != nil {
				return fatalf(errw, "migrate codex-arguments tx begin: %s", err)
			}

			stmtItems, err := tx.Prepare(`UPDATE items SET raw_json = ?, metadata_json = ?, text = ? WHERE id = ?`)
			if err != nil {
				tx.Rollback()
				return fatalf(errw, "migrate codex-arguments stmt prepare: %s", err)
			}

			stmtFTS, err := tx.Prepare(`UPDATE item_fts SET body = ? WHERE rowid = ?`)
			if err != nil {
				stmtItems.Close()
				tx.Rollback()
				return fatalf(errw, "migrate codex-arguments fts stmt prepare: %s", err)
			}

			for _, upd := range batchUpdates {
				_, err := stmtItems.Exec(upd.rawJSON, upd.metadataJSON, upd.text, upd.id)
				if err != nil {
					stmtItems.Close()
					stmtFTS.Close()
					tx.Rollback()
					return fatalf(errw, "migrate codex-arguments stmt exec: %s", err)
				}
				if upd.ftsUpdated && upd.ftsRowID > 0 {
					_, err := stmtFTS.Exec(upd.ftsBody, upd.ftsRowID)
					if err != nil {
						stmtItems.Close()
						stmtFTS.Close()
						tx.Rollback()
						return fatalf(errw, "migrate codex-arguments fts stmt exec: %s", err)
					}
				}
			}
			stmtItems.Close()
			stmtFTS.Close()
			if err := tx.Commit(); err != nil {
				return fatalf(errw, "migrate codex-arguments tx commit: %s", err)
			}
		}
	}

	if isJSON {
		res := map[string]any{
			"matched":     matchedRows,
			"bytes_saved": bytesSaved,
			"dry_run":     isDryRun,
			"fts_missing": ftsMissing,
		}
		enc := json.NewEncoder(out)
		enc.SetIndent("", "  ")
		if err := enc.Encode(res); err != nil {
			return fatalf(errw, "migrate codex-arguments encode json: %s", err)
		}
	} else {
		prefix := ""
		if isDryRun {
			prefix = "[dry-run] "
		}
		fmt.Fprintf(out, "%smatched %d codex rows with duplicate or oversized arguments\n", prefix, matchedRows)
		fmt.Fprintf(out, "%sestimated bytes saved: %d\n", prefix, bytesSaved)
		if ftsMissing > 0 {
			fmt.Fprintf(out, "%sitems missing from fts: %d\n", prefix, ftsMissing)
		}
		if isDryRun && matchedRows > 0 {
			fmt.Fprintf(out, "run with --apply to execute\n")
		}
	}

	return 0
}

func isAlreadyTruncated(argsStr string, metaObj map[string]any) bool {
	if strings.HasSuffix(argsStr, sources.TruncationMarker) {
		return true
	}
	if _, hasDigest := metaObj["arguments_digest"]; hasDigest && strings.HasSuffix(argsStr, "[truncated]") {
		return true
	}
	return false
}
