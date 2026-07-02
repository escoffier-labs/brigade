package opencode

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

const opencodeExportTimeout = 2 * time.Minute

func DefaultRoot() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = os.Getenv("HOME")
	}
	return filepath.Join(home, ".local", "share", "opencode")
}

func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	files, err := inputs(path)
	if err != nil {
		return sources.Result{}, err
	}
	var result sources.Result
	for _, input := range files {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
		scan, skip, err := prepareScan(input, opts)
		if err != nil {
			return result, err
		}
		if skip {
			result.Files = append(result.Files, scan)
			continue
		}
		export, raw, err := readExport(input)
		if scan.ContentHash == "" {
			scan.Size = int64(len(raw))
			scan.MTime = time.Now().UTC().Format(time.RFC3339Nano)
			scan.ContentHash = "sha256:" + sources.HashBytes(raw)
		}
		if err != nil {
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s: %s", input, err))
			scan.Warnings++
			result.Files = append(result.Files, scan)
			continue
		}
		records, warnings := normalizeExport(input, export)
		for _, warning := range warnings {
			result.Warnings = append(result.Warnings, warning)
			scan.Warnings++
		}
		for _, rec := range records {
			if opts.Limit > 0 && result.Records >= opts.Limit {
				break
			}
			if !sources.KeepTimestamp(rec.Item.CreatedAt, since, hasSince) {
				continue
			}
			sources.ApplyRedaction(&rec, opts)
			if err := sources.WriteRecord(w, rec); err != nil {
				return result, err
			}
			result.Records++
			scan.Records++
		}
		result.Files = append(result.Files, scan)
	}
	return result, nil
}

type exportFile struct {
	Info     map[string]any    `json:"info"`
	Messages []exportedMessage `json:"messages"`
}

type exportedMessage struct {
	Info  map[string]any   `json:"info"`
	Parts []map[string]any `json:"parts"`
}

func inputs(path string) ([]string, error) {
	if strings.TrimSpace(path) == "" {
		return nil, fmt.Errorf("path or OpenCode session ID is required")
	}
	info, err := os.Stat(path)
	if err != nil {
		return []string{path}, nil
	}
	if !info.IsDir() {
		if isIgnoredOpenCodePath(path) {
			return nil, nil
		}
		return []string{path}, nil
	}
	var out []string
	if err := filepath.WalkDir(path, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			name := strings.ToLower(d.Name())
			if name == "backup" || name == "backups" || name == "deleted" || name == "session_diff" {
				return filepath.SkipDir
			}
			return nil
		}
		if isIgnoredOpenCodePath(p) {
			return nil
		}
		name := strings.ToLower(filepath.Base(p))
		if strings.Contains(name, "backup") || strings.Contains(name, ".bak") || strings.Contains(name, "deleted") {
			return nil
		}
		if strings.HasSuffix(name, ".json") || strings.HasSuffix(name, ".jsonl") {
			out = append(out, p)
		}
		return nil
	}); err != nil {
		return nil, err
	}
	sort.Strings(out)
	return out, nil
}

func isIgnoredOpenCodePath(path string) bool {
	for p := filepath.Clean(path); ; p = filepath.Dir(p) {
		if strings.ToLower(filepath.Base(p)) == "session_diff" {
			return true
		}
		parent := filepath.Dir(p)
		if parent == p || parent == "." {
			return false
		}
	}
}

func InputsForDiscovery(path string) ([]string, error) {
	return inputs(path)
}

func prepareScan(input string, opts sources.Options) (sources.FileScan, bool, error) {
	info, err := os.Stat(input)
	if err != nil || info.IsDir() {
		return sources.FileScan{Path: input}, false, nil
	}
	return sources.PrepareFileScan(input, opts)
}

func readExport(input string) (exportFile, []byte, error) {
	if info, err := os.Stat(input); err == nil && !info.IsDir() {
		b, err := os.ReadFile(input)
		if err != nil {
			return exportFile{}, nil, err
		}
		var exp exportFile
		if err := json.Unmarshal(b, &exp); err != nil {
			return exportFile{}, b, err
		}
		return exp, b, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), opencodeExportTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "opencode", "export", input, "--sanitize")
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	b, err := cmd.Output()
	if err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return exportFile{}, nil, fmt.Errorf("opencode export timed out after %s", opencodeExportTimeout)
		}
		if errors.Is(err, exec.ErrNotFound) {
			return exportFile{}, nil, fmt.Errorf("opencode binary not found on PATH: install opencode to export session ID %q, or pass a sanitized export file path instead", input)
		}
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return exportFile{}, nil, fmt.Errorf("opencode export failed: %s", msg)
	}
	var exp exportFile
	if err := json.Unmarshal(b, &exp); err != nil {
		return exportFile{}, b, err
	}
	return exp, b, nil
}

func normalizeExport(path string, exp exportFile) ([]adapter.Record, []string) {
	sessionID := stringFrom(exp.Info, "id")
	if sessionID == "" {
		sessionID = filepath.Base(path)
	}
	projectID := stringFrom(exp.Info, "projectID")
	directory := stringFrom(exp.Info, "directory")
	model := stringFrom(exp.Info, "model")
	agent := stringFrom(exp.Info, "agent")
	sessionTime := timeString(exp.Info["time"])
	var records []adapter.Record
	var warnings []string
	for msgIdx, msg := range exp.Messages {
		msgID := stringFrom(msg.Info, "id")
		if msgID == "" {
			msgID = sources.StableID(path, sessionID, "message", fmt.Sprint(msgIdx))
		}
		role := stringFrom(msg.Info, "role")
		created := timeString(msg.Info["time"])
		if created == "" {
			created = sessionTime
		}
		msgText := messageText(msg)
		if msgText == "" {
			msgText = strings.TrimSpace(strings.Join(nonEmpty("OpenCode", role, stringFrom(msg.Info, "model"), msgID), " "))
		}
		if msgText == "" {
			warnings = append(warnings, fmt.Sprintf("%s:%d: no searchable text for message", path, msgIdx+1))
			continue
		}
		kind := sources.KindFromEvent("message "+role, msgText)
		meta := map[string]any{
			"harness":     "opencode",
			"event_type":  "message",
			"session_id":  sessionID,
			"message_id":  msgID,
			"project_id":  projectID,
			"model":       firstNonEmpty(stringFrom(msg.Info, "model"), model),
			"agent":       firstNonEmpty(stringFrom(msg.Info, "agent"), agent),
			"directory":   directory,
			"file_path":   path,
			"ordinal":     msgIdx + 1,
			"provider_id": stringFrom(msg.Info, "providerID"),
		}
		itemID := "opencode:message:" + msgID
		rec := adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "opencode", Name: "OpenCode Sessions"},
			Collection: adapter.Collection{
				ExternalID: "opencode:session:" + sessionID,
				Kind:       "agent_session",
				Name:       firstNonEmpty(stringFrom(exp.Info, "title"), sessionID),
				Metadata:   sources.Metadata(map[string]any{"harness": "opencode", "session_id": sessionID, "project_id": projectID, "directory": directory}),
			},
			Item: adapter.Item{
				ExternalID: itemID,
				Kind:       kind,
				CreatedAt:  created,
				Text:       msgText,
				Tags:       []string{"agent-session", "opencode"},
				Metadata:   sources.Metadata(meta),
			},
			Actor: sources.ActorFromRole("opencode", role, "message"),
			Raw:   rawRef(path, int64(msgIdx+1), msg),
		}
		rec.Artifacts = append(rec.Artifacts, artifactsFromMessage(itemID, msg)...)
		records = append(records, rec)
	}
	return records, warnings
}

func messageText(msg exportedMessage) string {
	var parts []string
	for _, part := range msg.Parts {
		partType := stringFrom(part, "type")
		switch partType {
		case "text", "tool", "reasoning":
			if s := sources.TextFromAny(part["text"], 4000); s != "" {
				parts = append(parts, s)
			}
			if partType == "tool" {
				toolName := stringFrom(part, "tool")
				callID := stringFrom(part, "callID")
				if toolName != "" || callID != "" {
					parts = append(parts, strings.Join(nonEmpty("tool", toolName, callID), " "))
				}
			}
		}
	}
	return strings.TrimSpace(strings.Join(parts, "\n"))
}

func artifactsFromMessage(itemID string, msg exportedMessage) []adapter.Artifact {
	var out []adapter.Artifact
	for _, part := range msg.Parts {
		partType := stringFrom(part, "type")
		if partType != "tool" && partType != "step-finish" {
			continue
		}
		toolName := stringFrom(part, "tool")
		callID := stringFrom(part, "callID")
		text := sources.TextFromAny(part["text"], 4000)
		if text == "" && toolName == "" && callID == "" {
			continue
		}
		kind := "tool"
		if toolName == "bash" || toolName == "shell" {
			kind = "command"
		}
		out = append(out, adapter.Artifact{
			ExternalID: sources.StableID(itemID, kind, toolName, callID, text),
			Kind:       kind,
			Text:       text,
			Hash:       "sha256:" + sources.HashBytes([]byte(toolName+callID+text)),
			Metadata:   sources.Metadata(map[string]any{"tool": toolName, "call_id": callID, "part_type": partType}),
		})
	}
	return out
}

func rawRef(path string, ordinal int64, msg exportedMessage) adapter.RawRef {
	b, _ := json.Marshal(msg)
	return adapter.RawRef{
		Format:  "json",
		Hash:    "sha256:" + sources.HashBytes(b),
		Path:    path,
		Ordinal: &ordinal,
	}
}

func stringFrom(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if v, ok := m[key]; ok {
			if s, ok := v.(string); ok && s != "" {
				return s
			}
		}
	}
	return ""
}

func timeString(v any) string {
	switch t := v.(type) {
	case string:
		if strings.TrimSpace(t) == "" {
			return ""
		}
		if parsed, err := time.Parse(time.RFC3339Nano, t); err == nil {
			return parsed.UTC().Format(time.RFC3339Nano)
		}
		if parsed, err := time.Parse(time.RFC3339, t); err == nil {
			return parsed.UTC().Format(time.RFC3339Nano)
		}
		if parsed, err := time.Parse("2006-01-02T15:04:05", t); err == nil {
			return parsed.UTC().Format(time.RFC3339Nano)
		}
		return t
	case float64:
		return unixMillis(int64(t))
	case int64:
		return unixMillis(t)
	case int:
		return unixMillis(int64(t))
	case json.Number:
		n, _ := strconv.ParseInt(string(t), 10, 64)
		return unixMillis(n)
	default:
		return ""
	}
}

func unixMillis(ms int64) string {
	if ms <= 0 {
		return ""
	}
	return time.UnixMilli(ms).UTC().Format(time.RFC3339Nano)
}

func firstNonEmpty(parts ...string) string {
	for _, part := range parts {
		if strings.TrimSpace(part) != "" {
			return part
		}
	}
	return ""
}

func nonEmpty(parts ...string) []string {
	var out []string
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}
