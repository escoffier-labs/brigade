package adapter

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// maxCodexRolloutRecord caps the size of a single JSONL record we will buffer
// when scanning a Codex rollout file. Codex turn_context records are small,
// but the rollout also carries large output records we never want to fully
// buffer; 8 MiB is a generous ceiling for any well-formed turn_context.
const maxCodexRolloutRecord = 8 * 1024 * 1024

// codexIdentifierRe matches the safe subset of identifiers we accept as a
// thread or turn id when looking up session rollouts. Rejecting anything
// broader (slashes, dots, etc.) keeps us from being talked into walking or
// opening files outside the session roots.
var codexIdentifierRe = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

func validCodexIdentifier(s string) bool {
	return s != "" && codexIdentifierRe.MatchString(s)
}

// resolveCodexModel looks up the model used for a specific Codex turn by
// scanning the Codex CLI session rollout files under CODEX_HOME (or
// ~/.codex when CODEX_HOME is unset). It walks the sessions and
// archived_sessions trees without following symlinks, finds rollout files
// whose names end in -<threadID>.jsonl, and returns the model from the
// turn_context record whose turn_id matches turnID. Any error or miss
// returns an empty string.
func resolveCodexModel(threadID, turnID string) string {
	if !validCodexIdentifier(threadID) || !validCodexIdentifier(turnID) {
		return ""
	}
	home := os.Getenv("CODEX_HOME")
	if home == "" {
		userHome, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		home = filepath.Join(userHome, ".codex")
	}
	suffix := "-" + threadID + ".jsonl"
	for _, root := range []string{"sessions", "archived_sessions"} {
		base := filepath.Join(home, root)
		var found string
		walkErr := filepath.WalkDir(base, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				// Best-effort walk: skip unreadable entries rather than aborting.
				return nil
			}
			if d.IsDir() {
				if d.Type()&fs.ModeSymlink != 0 {
					return filepath.SkipDir
				}
				return nil
			}
			if d.Type()&fs.ModeSymlink != 0 {
				// Do not follow symlinked rollouts.
				return nil
			}
			if !d.Type().IsRegular() {
				return nil
			}
			name := d.Name()
			if !strings.HasPrefix(name, "rollout-") || !strings.HasSuffix(name, suffix) {
				return nil
			}
			if m := modelFromCodexRollout(path, turnID); m != "" {
				found = m
				return fs.SkipAll
			}
			return nil
		})
		if walkErr == nil && found != "" {
			return found
		}
	}
	return ""
}

// modelFromCodexRollout scans a single Codex rollout JSONL file for a
// turn_context record whose turn_id matches turnID and returns the model
// field from that record. Every error (unreadable file, malformed line,
// missing fields) returns an empty string.
func modelFromCodexRollout(path, turnID string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), maxCodexRolloutRecord)
	for scanner.Scan() {
		line := scanner.Bytes()
		// Fast path: skip lines that cannot be a turn_context record without
		// paying for an unmarshal.
		if !bytes.Contains(line, []byte("turn_context")) {
			continue
		}
		var rec struct {
			Type    string                 `json:"type"`
			Payload map[string]interface{} `json:"payload"`
		}
		if err := json.Unmarshal(line, &rec); err != nil {
			continue
		}
		if rec.Type != "turn_context" {
			continue
		}
		if firstString(rec.Payload, "turn_id", "turn-id") != turnID {
			continue
		}
		if m := firstString(rec.Payload, "model", "model-name", "model_name"); m != "" {
			return m
		}
		return ""
	}
	return ""
}
