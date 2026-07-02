// Package cursor imports local Cursor Agent CLI history into the archive.
//
// The Cursor Agent CLI keeps its data under a config root (WI() in the
// upstream bundle), which resolves to $XDG_CONFIG_HOME/cursor or
// ~/.config/cursor on Linux and ~/.cursor on macOS. Two surfaces there are
// stable, plain JSON, and worth indexing for "find the session I want":
//
//   - prompt_history.json: a deduplicated JSON array of the prompts the user
//     typed. This is the primary Ctrl+F surface for recalling a session.
//   - chats/<hash>/meta.json and acp-sessions/<id>/meta.json: per-session
//     metadata (title, timestamps, workspace). Each becomes an agent_session
//     collection so it shows up in `miseledger sessions`.
//
// The per-session store.db holds message bodies as binary blobs (a versioned
// internal encoding), so message text is intentionally NOT decoded here. A
// chat directory that has a store.db but no meta.json is still surfaced as a
// discoverable session (so the user can resume it) with a warning.
package cursor

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

// DefaultRoot returns the Cursor Agent config root for the current OS,
// matching the upstream WI() resolution. It does not check existence.
func DefaultRoot() string {
	if xdg := strings.TrimSpace(os.Getenv("XDG_CONFIG_HOME")); xdg != "" {
		return filepath.Join(xdg, "cursor")
	}
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = os.Getenv("HOME")
	}
	return filepath.Join(home, ".config", "cursor")
}

// Generate walks a Cursor config root (or a direct prompt_history.json file)
// and emits adapter records for prompt history and chat sessions.
func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return sources.Result{}, err
	}
	gen := &generator{opts: opts, since: since, hasSince: hasSince, w: w}
	if !info.IsDir() {
		// A direct file is treated as a prompt_history.json.
		if err := gen.emitPromptHistory(path); err != nil {
			return gen.result, err
		}
		gen.result.Files = gen.scans
		return gen.result, nil
	}
	root := path
	if hp := filepath.Join(root, "prompt_history.json"); fileExists(hp) {
		if err := gen.emitPromptHistory(hp); err != nil {
			return gen.result, err
		}
	}
	for _, sub := range []string{"chats", "acp-sessions"} {
		dir := filepath.Join(root, sub)
		if err := gen.emitSessions(dir); err != nil {
			return gen.result, err
		}
	}
	gen.result.Files = gen.scans
	return gen.result, nil
}

type generator struct {
	opts     sources.Options
	since    time.Time
	hasSince bool
	w        io.Writer
	result   sources.Result
	scans    []sources.FileScan
}

func (g *generator) limited() bool {
	return g.opts.Limit > 0 && g.result.Records >= g.opts.Limit
}

func (g *generator) emitPromptHistory(path string) error {
	scan, skip, err := g.openScan(path)
	if err != nil {
		return err
	}
	if skip {
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	scan.ContentHash = "sha256:" + sources.HashBytes(raw)
	var prompts []string
	if err := json.Unmarshal(raw, &prompts); err != nil {
		g.warn(scan, fmt.Sprintf("%s: prompt_history.json is not a JSON string array: %s", path, err))
		g.scans = append(g.scans, *scan)
		return nil
	}
	for idx, prompt := range prompts {
		if g.limited() {
			break
		}
		text := strings.TrimSpace(prompt)
		if text == "" {
			continue
		}
		ordinal := int64(idx + 1)
		rec := adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "cursor", Name: "Cursor Agent"},
			Collection: adapter.Collection{
				ExternalID: "cursor:prompt-history",
				Kind:       "prompt_history",
				Name:       "Cursor Prompt History",
				Metadata:   sources.Metadata(map[string]any{"harness": "cursor", "surface": "prompt_history"}),
			},
			Item: adapter.Item{
				ExternalID: "cursor:prompt:" + sources.StableID(text),
				Kind:       "message",
				Text:       text,
				Tags:       []string{"cursor", "prompt-history"},
				Metadata:   sources.Metadata(map[string]any{"harness": "cursor", "surface": "prompt_history", "source_file": path, "ordinal": ordinal}),
			},
			Actor: sources.ActorFromRole("cursor", "user", "prompt"),
			Raw: adapter.RawRef{
				Format:  "json",
				Hash:    "sha256:" + sources.HashBytes([]byte(text)),
				Path:    path,
				Ordinal: &ordinal,
			},
		}
		sources.ApplyRedaction(&rec, g.opts)
		if err := sources.WriteRecord(g.w, rec); err != nil {
			return err
		}
		g.result.Records++
		scan.Records++
	}
	g.scans = append(g.scans, *scan)
	return nil
}

func (g *generator) emitSessions(dir string) error {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for _, entry := range entries {
		if g.limited() {
			break
		}
		if !entry.IsDir() {
			continue
		}
		sessionDir := filepath.Join(dir, entry.Name())
		if err := g.emitSession(sessionDir, entry.Name()); err != nil {
			return err
		}
	}
	return nil
}

func (g *generator) emitSession(sessionDir, id string) error {
	metaPath := filepath.Join(sessionDir, "meta.json")
	storePath := filepath.Join(sessionDir, "store.db")
	hasStore := fileExists(storePath)
	if !fileExists(metaPath) {
		// No JSON metadata. If there is a store.db, surface a minimal,
		// resumable session record so the chat is still discoverable.
		if hasStore {
			g.warn(nil, fmt.Sprintf("%s: chat has store.db but no meta.json; message bodies are binary and not indexed", sessionDir))
			return g.writeSession(sessionDir, id, id, "", "", storePath, true)
		}
		return nil
	}
	scan, skip, err := g.openScan(metaPath)
	if err != nil {
		return err
	}
	if skip {
		return nil
	}
	raw, err := os.ReadFile(metaPath)
	if err != nil {
		return err
	}
	scan.ContentHash = "sha256:" + sources.HashBytes(raw)
	var meta map[string]any
	if err := json.Unmarshal(raw, &meta); err != nil {
		g.warn(scan, fmt.Sprintf("%s: meta.json is not a JSON object: %s", metaPath, err))
		g.scans = append(g.scans, *scan)
		return nil
	}
	sessionID := firstString(meta, "id", "chatId", "sessionId", "uuid")
	if sessionID == "" {
		sessionID = id
	}
	title := firstString(meta, "title", "name", "summary")
	createdAt := firstTimestamp(meta, "createdAt", "created_at", "createdAtMs", "updatedAt", "updated_at")
	workspace := firstString(meta, "workspace", "workspacePath", "cwd", "rootPath")
	body := sessionText(meta, title, sessionID)
	if !sources.KeepTimestamp(createdAt, g.since, g.hasSince) {
		g.scans = append(g.scans, *scan)
		return nil
	}
	rawPath := storePath
	if !hasStore {
		rawPath = metaPath
	}
	if err := g.writeSession(sessionDir, sessionID, body, createdAt, workspace, rawPath, hasStore); err != nil {
		return err
	}
	scan.Records++
	g.scans = append(g.scans, *scan)
	return nil
}

func (g *generator) writeSession(sessionDir, sessionID, text, createdAt, workspace, rawPath string, hasStore bool) error {
	if strings.TrimSpace(text) == "" {
		text = sessionID
	}
	ordinal := int64(1)
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "cursor", Name: "Cursor Agent"},
		Collection: adapter.Collection{
			ExternalID: "cursor:session:" + sessionID,
			Kind:       "agent_session",
			Name:       firstNonEmpty(text, sessionID),
			Metadata:   sources.Metadata(map[string]any{"harness": "cursor", "session_id": sessionID, "session_dir": sessionDir, "workspace": workspace}),
		},
		Item: adapter.Item{
			ExternalID: "cursor:session-meta:" + sessionID,
			Kind:       "message",
			CreatedAt:  createdAt,
			Text:       text,
			Tags:       []string{"cursor", "agent-session"},
			Metadata:   sources.Metadata(map[string]any{"harness": "cursor", "session_id": sessionID, "session_dir": sessionDir, "workspace": workspace, "store_db": hasStore, "bodies_indexed": false, "source_file": rawPath, "ordinal": ordinal}),
		},
		Actor: sources.ActorFromRole("cursor", "system", "session"),
		Raw: adapter.RawRef{
			Format:  "cursor-session",
			Hash:    "sha256:" + sources.HashBytes([]byte(sessionDir)),
			Path:    rawPath,
			Ordinal: &ordinal,
		},
	}
	sources.ApplyRedaction(&rec, g.opts)
	if err := sources.WriteRecord(g.w, rec); err != nil {
		return err
	}
	g.result.Records++
	return nil
}

// openScan stats a file and applies the incremental-skip decision. A skipped
// file is recorded in the manifest and reported via skip=true.
func (g *generator) openScan(path string) (*sources.FileScan, bool, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, false, err
	}
	scan := &sources.FileScan{
		Path:  path,
		Size:  info.Size(),
		MTime: info.ModTime().UTC().Format(time.RFC3339Nano),
	}
	if g.opts.Skip != nil && g.opts.Skip(scan.Path, scan.Size, scan.MTime) {
		scan.Skipped = true
		g.scans = append(g.scans, *scan)
		return scan, true, nil
	}
	return scan, false, nil
}

func (g *generator) warn(scan *sources.FileScan, msg string) {
	g.result.Warnings = append(g.result.Warnings, msg)
	if scan != nil {
		scan.Warnings++
	}
}

func sessionText(meta map[string]any, title, sessionID string) string {
	parts := []string{}
	if title != "" {
		parts = append(parts, title)
	}
	for _, key := range []string{"summary", "lastMessage", "preview", "firstMessage"} {
		if s := sources.TextFromAny(meta[key], 4000); s != "" && s != title {
			parts = append(parts, s)
		}
	}
	if len(parts) == 0 {
		return sessionID
	}
	return strings.Join(parts, "\n")
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

func firstString(m map[string]any, keys ...string) string {
	for _, key := range keys {
		switch t := m[key].(type) {
		case string:
			if strings.TrimSpace(t) != "" {
				return t
			}
		case float64:
			return strconv.FormatFloat(t, 'f', -1, 64)
		}
	}
	return ""
}

// firstTimestamp returns the first present timestamp key as an RFC3339 string.
// It accepts RFC3339 strings and epoch numbers in seconds or milliseconds.
func firstTimestamp(m map[string]any, keys ...string) string {
	for _, key := range keys {
		switch t := m[key].(type) {
		case string:
			s := strings.TrimSpace(t)
			if s == "" {
				continue
			}
			if _, err := time.Parse(time.RFC3339, s); err == nil {
				return s
			}
			if n, err := strconv.ParseFloat(s, 64); err == nil {
				return epochString(n)
			}
		case float64:
			return epochString(t)
		}
	}
	return ""
}

func epochString(n float64) string {
	if n <= 0 {
		return ""
	}
	// Heuristic: values past ~the year 2001 in seconds are < 1e12; larger
	// values are milliseconds.
	if n > 1e12 {
		return time.UnixMilli(int64(n)).UTC().Format(time.RFC3339)
	}
	return time.Unix(int64(n), 0).UTC().Format(time.RFC3339)
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}
