// Package grok imports local Grok CLI session summaries and chat history.
package grok

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

// DefaultRoot returns the standard Grok CLI session root.
func DefaultRoot() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = os.Getenv("HOME")
	}
	return filepath.Join(home, ".grok", "sessions")
}

// CountSessions returns the number of session directories with a supported file.
func CountSessions(path string) (int, error) {
	files, err := sessionFiles(path)
	return len(files), err
}

// Generate emits adapter records from Grok summary.json and chat_history.jsonl files.
func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	files, err := sessionFiles(path)
	if err != nil {
		return sources.Result{}, err
	}
	g := generator{opts: opts, since: since, hasSince: hasSince, w: w}
	for _, dir := range sortedDirs(files) {
		if err := g.emitSession(dir, files[dir]); err != nil {
			return g.result, err
		}
	}
	return g.result, nil
}

type sessionFileSet struct {
	summary string
	chat    string
}

type generator struct {
	opts     sources.Options
	since    time.Time
	hasSince bool
	w        io.Writer
	result   sources.Result
}

func sessionFiles(root string) (map[string]sessionFileSet, error) {
	info, err := os.Stat(root)
	if err != nil {
		return nil, err
	}
	paths := []string{}
	if !info.IsDir() {
		paths = append(paths, root)
	} else if err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		if d.Name() == "summary.json" || d.Name() == "chat_history.jsonl" {
			paths = append(paths, path)
		}
		return nil
	}); err != nil {
		return nil, err
	}
	sort.Strings(paths)
	out := map[string]sessionFileSet{}
	for _, path := range paths {
		dir := filepath.Dir(path)
		set := out[dir]
		switch filepath.Base(path) {
		case "summary.json":
			set.summary = path
		case "chat_history.jsonl":
			set.chat = path
		default:
			continue
		}
		out[dir] = set
	}
	return out, nil
}

func sortedDirs(files map[string]sessionFileSet) []string {
	dirs := make([]string, 0, len(files))
	for dir := range files {
		dirs = append(dirs, dir)
	}
	sort.Strings(dirs)
	return dirs
}

func (g *generator) emitSession(dir string, files sessionFileSet) error {
	sessionID := filepath.Base(dir)
	workspace := decodedWorkspace(filepath.Base(filepath.Dir(dir)))
	meta := map[string]any{}
	var summaryScan *sources.FileScan
	if files.summary != "" {
		scan, skip, err := sources.PrepareFileScan(files.summary, g.opts)
		if err != nil {
			return err
		}
		summaryScan = &scan
		if !skip {
			raw, err := os.ReadFile(files.summary)
			if err != nil {
				return err
			}
			if err := json.Unmarshal(raw, &meta); err != nil {
				g.warn(summaryScan, fmt.Sprintf("%s: malformed summary JSON: %s", files.summary, err))
				meta = map[string]any{}
			}
		}
	}
	if root := sources.String(meta, "git_root_dir", "workspace", "cwd"); root != "" {
		workspace = root
	}
	createdAt := sources.String(meta, "created_at", "updated_at", "last_active_at")
	keepSession := sources.KeepTimestamp(createdAt, g.since, g.hasSince)
	if files.summary != "" && summaryScan != nil && !summaryScan.Skipped && keepSession && !g.limited() {
		if rec, ok := summaryRecord(files.summary, sessionID, workspace, meta); ok {
			if err := g.write(rec); err != nil {
				return err
			}
			summaryScan.Records++
		}
	}
	if summaryScan != nil {
		if err := g.finish(*summaryScan); err != nil {
			return err
		}
	}
	if files.chat == "" {
		return nil
	}
	chatScan, skip, err := sources.PrepareFileScan(files.chat, g.opts)
	if err != nil {
		return err
	}
	if !skip && keepSession {
		if err := g.emitChat(files.chat, sessionID, workspace, meta, &chatScan); err != nil {
			return err
		}
	}
	return g.finish(chatScan)
}

func (g *generator) emitChat(path, sessionID, workspace string, sessionMeta map[string]any, scan *sources.FileScan) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	var ordinal int64
	for scanner.Scan() {
		ordinal++
		line := append([]byte(nil), scanner.Bytes()...)
		if strings.TrimSpace(string(line)) == "" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal(line, &obj); err != nil {
			g.warn(scan, fmt.Sprintf("%s:%d: malformed chat JSON: %s", path, ordinal, err))
			continue
		}
		if g.limited() {
			continue
		}
		eventType := sources.String(obj, "type")
		text := sources.TextFromAny(obj["content"], 8000)
		if text == "" {
			g.warn(scan, fmt.Sprintf("%s:%d: no searchable text for event type %q", path, ordinal, eventType))
			continue
		}
		rec := baseRecord(sessionID, workspace, sessionMeta)
		rec.Item = adapter.Item{
			ExternalID: "grok:" + sources.StableID(sessionID, fmt.Sprint(ordinal), eventType, sources.HashBytes([]byte(text))),
			Kind:       sources.KindFromEvent(eventType, text),
			Text:       text,
			Tags:       []string{"agent-session", "grok"},
			Metadata: sources.Metadata(map[string]any{
				"harness": "grok", "session_id": sessionID, "event_type": eventType,
				"model": sources.String(sessionMeta, "current_model_id"), "workspace": workspace,
				"source_file": path, "ordinal": ordinal,
			}),
		}
		rec.Actor = sources.ActorFromRole("grok", eventType, eventType)
		rec.Raw = adapter.RawRef{Format: "jsonl", Hash: "sha256:" + sources.HashBytes(line), Path: path, Ordinal: &ordinal}
		if err := g.write(rec); err != nil {
			return err
		}
		scan.Records++
	}
	return scanner.Err()
}

func summaryRecord(path, sessionID, workspace string, meta map[string]any) (adapter.Record, bool) {
	title := sources.String(meta, "generated_title", "title", "agent_name")
	summary := sources.String(meta, "session_summary", "summary", "info")
	text := strings.TrimSpace(strings.Join(nonEmpty(title, summary), "\n"))
	if text == "" {
		return adapter.Record{}, false
	}
	rec := baseRecord(sessionID, workspace, meta)
	ordinal := int64(1)
	rec.Item = adapter.Item{
		ExternalID: "grok:summary:" + sessionID,
		Kind:       "session_summary",
		CreatedAt:  sources.String(meta, "created_at"),
		UpdatedAt:  sources.String(meta, "updated_at", "last_active_at"),
		Text:       text,
		Tags:       []string{"agent-session", "grok"},
		Metadata: sources.Metadata(map[string]any{
			"harness": "grok", "session_id": sessionID, "event_type": "summary",
			"model": sources.String(meta, "current_model_id"), "workspace": workspace,
			"source_file": path, "ordinal": ordinal,
			"branch": sources.String(meta, "head_branch"), "commit": sources.String(meta, "head_commit"),
		}),
	}
	rec.Actor = sources.ActorFromRole("grok", "system", "summary")
	rec.Raw = adapter.RawRef{Format: "json", Hash: "sha256:" + sources.HashBytes([]byte(text)), Path: path, Ordinal: &ordinal}
	return rec, true
}

func baseRecord(sessionID, workspace string, meta map[string]any) adapter.Record {
	name := sources.String(meta, "generated_title", "title")
	if name == "" {
		name = sessionID
	}
	return adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "grok", Name: "Grok Sessions"},
		Collection: adapter.Collection{
			ExternalID: "grok:session:" + sessionID,
			Kind:       "agent_session",
			Name:       name,
			Metadata: sources.Metadata(map[string]any{
				"harness": "grok", "session_id": sessionID, "workspace": workspace,
				"model": sources.String(meta, "current_model_id"),
			}),
		},
	}
}

func decodedWorkspace(encoded string) string {
	decoded, err := url.PathUnescape(encoded)
	if err != nil || strings.TrimSpace(decoded) == "" {
		return encoded
	}
	return decoded
}

func nonEmpty(values ...string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			out = append(out, value)
		}
	}
	return out
}

func (g *generator) limited() bool {
	return g.opts.Limit > 0 && g.result.Records >= g.opts.Limit
}

func (g *generator) write(rec adapter.Record) error {
	sources.ApplyRedaction(&rec, g.opts)
	if err := sources.WriteRecord(g.w, rec); err != nil {
		return err
	}
	g.result.Records++
	return nil
}

func (g *generator) warn(scan *sources.FileScan, message string) {
	g.result.Warnings = append(g.result.Warnings, message)
	if scan != nil {
		scan.Warnings++
	}
}

func (g *generator) finish(scan sources.FileScan) error {
	g.result.Files = append(g.result.Files, scan)
	if g.opts.AfterFile != nil {
		return g.opts.AfterFile(scan)
	}
	return nil
}
