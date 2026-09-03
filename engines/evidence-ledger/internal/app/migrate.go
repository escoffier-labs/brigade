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

	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

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
// 4000 characters and truncates them, keeping a SHA-256 digest in metadata.
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

	type updateRow struct {
		id           string
		rawJSON      string
		metadataJSON string
		text         string
		ftsUpdated   bool
		ftsBody      string
	}
	var matchedRows int
	var bytesSaved int

	offset := 0
	batchSize := 1000

	for {
		rows, err := db.Query(`
			SELECT i.id, coalesce(i.text, ''), i.raw_json, i.metadata_json 
			FROM items i
			JOIN sources s ON i.source_id = s.id
			WHERE s.kind = 'codex' AND i.metadata_json LIKE '%"arguments"%'
			ORDER BY i.id
			LIMIT ? OFFSET ?
		`, batchSize, offset)
		if err != nil {
			return fatalf(errw, "migrate codex-arguments query: %s", err)
		}

		var batchUpdates []updateRow
		hasRows := false

		for rows.Next() {
			hasRows = true
			var id, text, raw, meta string
			if err := rows.Scan(&id, &text, &raw, &meta); err != nil {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments scan: %s", err)
			}

			var metaObj map[string]any
			if err := json.Unmarshal([]byte(meta), &metaObj); err != nil {
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

			if len(argsStr) <= 4000 || isAlreadyTruncated(argsStr, metaObj) {
				continue
			}

			truncatedArgs := argsStr[:4000] + "\n[truncated]"
			digestBytes := sha256.Sum256([]byte(argsStr))
			digest := hex.EncodeToString(digestBytes[:])

			metaObj["arguments"] = truncatedArgs
			metaObj["arguments_digest"] = digest

			newMetaBytes, err := json.Marshal(metaObj)
			if err != nil {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments marshal metadata: %s", err)
			}
			newMeta := string(newMetaBytes)

			// Parse raw_json with json.Decoder and UseNumber to preserve precise float types and integers
			var rawObj map[string]any
			dec := json.NewDecoder(bytes.NewReader([]byte(raw)))
			dec.UseNumber()
			if err := dec.Decode(&rawObj); err != nil {
				continue
			}

			// Update item.metadata and item.text inside the marshaled adapter.Record
			if itemObj, ok := rawObj["item"].(map[string]any); ok {
				if itemMeta, ok := itemObj["metadata"].(map[string]any); ok {
					itemMeta["arguments"] = truncatedArgs
					itemMeta["arguments_digest"] = digest
				}
				if itemText, ok := itemObj["text"].(string); ok && strings.Contains(itemText, argsStr) {
					itemObj["text"] = strings.Replace(itemText, argsStr, truncatedArgs, 1)
				}
			}

			newRawBytes, err := json.Marshal(rawObj)
			if err != nil {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments marshal raw: %s", err)
			}
			newRaw := string(newRawBytes)

			newText := text
			if strings.Contains(text, argsStr) {
				newText = strings.Replace(text, argsStr, truncatedArgs, 1)
			}

			var ftsBody string
			ftsUpdated := false
			newFtsBody := ""
			err = db.QueryRow(`SELECT body FROM item_fts WHERE item_id = ?`, id).Scan(&ftsBody)
			if err == nil {
				newFtsBody = ftsBody
				normOldText := textnorm.Normalize(text)
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
			} else if err != sql.ErrNoRows {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments fts query: %s", err)
			}

			saved := len(raw) + len(meta) + len(text) - len(newRaw) - len(newMeta) - len(newText)
			bytesSaved += saved
			matchedRows++

			batchUpdates = append(batchUpdates, updateRow{
				id:           id,
				rawJSON:      newRaw,
				metadataJSON: newMeta,
				text:         newText,
				ftsUpdated:   ftsUpdated,
				ftsBody:      newFtsBody,
			})
		}

		if err := rows.Err(); err != nil {
			rows.Close()
			return fatalf(errw, "migrate codex-arguments rows: %s", err)
		}
		rows.Close()

		if !hasRows {
			break
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

			stmtFTS, err := tx.Prepare(`UPDATE item_fts SET body = ? WHERE item_id = ?`)
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
				if upd.ftsUpdated {
					_, err := stmtFTS.Exec(upd.ftsBody, upd.id)
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

		offset += batchSize
	}

	if isJSON {
		res := map[string]any{
			"matched":     matchedRows,
			"bytes_saved": bytesSaved,
			"dry_run":     isDryRun,
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
		if isDryRun && matchedRows > 0 {
			fmt.Fprintf(out, "run with --apply to execute\n")
		}
	}

	return 0
}

func isAlreadyTruncated(argsStr string, metaObj map[string]any) bool {
	if strings.HasSuffix(argsStr, "\n[truncated]") {
		return true
	}
	if _, hasDigest := metaObj["arguments_digest"]; hasDigest && strings.HasSuffix(argsStr, "[truncated]") {
		return true
	}
	return false
}
