package app

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
)

// cmdMigrate dispatches `miseledger migrate <target>`; mirrors cmdPrune.
func cmdMigrate(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
	switch args[0] {
	case "codex-arguments":
		return cmdMigrateCodexArguments(args[1:], out, errw)
	default:
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
}

func cmdMigrateCodexArguments(args []string, out, errw io.Writer) int {
	_, bools, rest, err := splitFlags(args, nil, map[string]bool{"dry-run": true, "apply": true, "json": true})
	if err != nil {
		return fatalf(errw, "migrate codex-arguments: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "usage: miseledger migrate codex-arguments [--dry-run] [--apply] [--json]")
	}
	isDryRun := true
	if bools["apply"] {
		isDryRun = false
	}
	if bools["dry-run"] {
		isDryRun = true
	}
	isJSON := bools["json"]

	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "migrate codex-arguments: %s", err)
	}
	defer db.Close()

	// 1. Get total number of items to migrate to allocate batches.
	var total int
	err = db.QueryRow(`
		SELECT COUNT(*) 
		FROM items i
		JOIN sources s ON i.source_id = s.id
		WHERE s.kind = 'codex' AND i.metadata_json LIKE '%"arguments"%'
	`).Scan(&total)
	if err != nil {
		return fatalf(errw, "migrate codex-arguments count query: %s", err)
	}

	type updateRow struct {
		id           string
		rawJSON      string
		metadataJSON string
	}
	var matchedRows int
	var bytesSaved int

	offset := 0
	batchSize := 1000

	for {
		rows, err := db.Query(`
			SELECT i.id, i.raw_json, i.metadata_json 
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
			var id, raw, meta string
			if err := rows.Scan(&id, &raw, &meta); err != nil {
				rows.Close()
				return fatalf(errw, "migrate codex-arguments scan: %s", err)
			}

			// Parse metadata using default unmarshal since it doesn't matter for the structure
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

			needsTruncate := len(argsStr) > 4000

			// Parse raw_json with json.Decoder and UseNumber to preserve precise float types and integers
			var rawObj map[string]any
			dec := json.NewDecoder(bytes.NewReader([]byte(raw)))
			dec.UseNumber()
			if err := dec.Decode(&rawObj); err != nil {
				continue
			}

			changedRaw := false
			if p, ok := rawObj["payload"].(map[string]any); ok {
				if _, hasArgs := p["arguments"]; hasArgs {
					delete(p, "arguments")
					changedRaw = true
				}
			}
			if _, hasArgs := rawObj["arguments"]; hasArgs {
				delete(rawObj, "arguments")
				changedRaw = true
			}

			changedMeta := false
			if needsTruncate {
				if !hasPrefixOrTruncatedCheck(argsStr) {
					truncatedArgs := argsStr[:4000] + "\n[truncated]"
					digestBytes := sha256.Sum256([]byte(argsStr))
					digest := hex.EncodeToString(digestBytes[:])

					metaObj["arguments"] = truncatedArgs
					metaObj["arguments_digest"] = digest
					changedMeta = true
				}
			}

			if changedRaw || changedMeta {
				newRawBytes, _ := json.Marshal(rawObj)
				newMetaBytes, _ := json.Marshal(metaObj)

				newRaw := string(newRawBytes)
				newMeta := string(newMetaBytes)

				saved := len(raw) + len(meta) - len(newRaw) - len(newMeta)
				bytesSaved += saved
				matchedRows++

				batchUpdates = append(batchUpdates, updateRow{
					id:           id,
					rawJSON:      newRaw,
					metadataJSON: newMeta,
				})
			}
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

			stmt, err := tx.Prepare(`UPDATE items SET raw_json = ?, metadata_json = ? WHERE id = ?`)
			if err != nil {
				tx.Rollback()
				return fatalf(errw, "migrate codex-arguments stmt prepare: %s", err)
			}

			for _, upd := range batchUpdates {
				_, err := stmt.Exec(upd.rawJSON, upd.metadataJSON, upd.id)
				if err != nil {
					stmt.Close()
					tx.Rollback()
					return fatalf(errw, "migrate codex-arguments stmt exec: %s", err)
				}
			}
			stmt.Close()
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
		enc.Encode(res)
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

func hasPrefixOrTruncatedCheck(argsStr string) bool {
	return len(argsStr) > 11 && argsStr[len(argsStr)-11:] == "[truncated]"
}
