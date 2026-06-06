package hermes

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	scans, err := sources.NewFileScanSet(path, Include)
	if err != nil {
		return sources.Result{}, err
	}
	files, err := sources.ListJSONLFiles(path, Include)
	if err != nil {
		return sources.Result{}, err
	}
	var result sources.Result
	for _, file := range files {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
		records, warnings, err := recordsFromFile(file)
		if err != nil {
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s: %s", file, err))
			scans.Warning(file)
			continue
		}
		for _, warning := range warnings {
			result.Warnings = append(result.Warnings, warning)
			scans.Warning(file)
		}
		for _, rec := range records {
			if opts.Limit > 0 && result.Records >= opts.Limit {
				break
			}
			if !sources.KeepTimestamp(rec.Item.CreatedAt, since, hasSince) {
				continue
			}
			if err := sources.WriteRecord(w, rec); err != nil {
				return result, err
			}
			result.Records++
			scans.Record(file)
		}
	}
	result.Files = scans.List()
	return result, nil
}

func Include(path string) bool {
	name := strings.ToLower(filepath.Base(path))
	if strings.Contains(name, "backup") || strings.Contains(name, ".bak") || strings.Contains(name, "deleted") {
		return false
	}
	if strings.HasPrefix(name, "request_dump_") {
		return false
	}
	if strings.HasPrefix(name, "session_") && strings.HasSuffix(name, ".json") {
		return true
	}
	if name == "trajectory_samples.jsonl" || name == "failed_trajectories.jsonl" || strings.Contains(name, "trajectory") && strings.HasSuffix(name, ".jsonl") {
		return true
	}
	return strings.HasSuffix(name, ".jsonl") && !strings.HasSuffix(name, ".metadata.jsonl") && !strings.HasSuffix(name, ".sidecar.jsonl")
}

func recordsFromFile(path string) ([]adapter.Record, []string, error) {
	name := strings.ToLower(filepath.Base(path))
	if strings.HasSuffix(name, ".json") {
		return recordsFromSnapshot(path)
	}
	return recordsFromJSONL(path)
}

func recordsFromSnapshot(path string) ([]adapter.Record, []string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	var obj map[string]any
	if err := json.Unmarshal(b, &obj); err != nil {
		return nil, []string{fmt.Sprintf("%s: malformed Hermes snapshot JSON: %s", path, err)}, nil
	}
	messages := arrayFromAny(obj["messages"])
	if len(messages) == 0 {
		messages = arrayFromAny(obj["_session_messages"])
	}
	if len(messages) == 0 {
		return nil, []string{fmt.Sprintf("%s: Hermes snapshot has no messages", path)}, nil
	}
	sessionID := stringFrom(obj, "session_id", "sessionId", "id")
	if sessionID == "" {
		sessionID = strings.TrimSuffix(strings.TrimPrefix(filepath.Base(path), "session_"), ".json")
	}
	model := stringFrom(obj, "model")
	platform := stringFrom(obj, "platform")
	started := timeString(obj["session_start"])
	var records []adapter.Record
	var warnings []string
	for i, value := range messages {
		msg, ok := value.(map[string]any)
		if !ok {
			warnings = append(warnings, fmt.Sprintf("%s:%d: Hermes snapshot message is not an object", path, i+1))
			continue
		}
		rec, warning := normalizeMessage(path, int64(i+1), sessionID, model, platform, started, msg)
		if warning != "" {
			warnings = append(warnings, warning)
			continue
		}
		records = append(records, rec)
		records = append(records, toolCallRecords(path, int64(i+1), sessionID, model, platform, started, msg)...)
	}
	return records, warnings, nil
}

func recordsFromJSONL(path string) ([]adapter.Record, []string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 10*1024*1024)
	var records []adapter.Record
	var warnings []string
	var ordinal int64
	for scanner.Scan() {
		ordinal++
		line := append([]byte(nil), scanner.Bytes()...)
		if strings.TrimSpace(string(line)) == "" {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal(line, &obj); err != nil {
			warnings = append(warnings, fmt.Sprintf("%s:%d: malformed JSONL: %s", path, ordinal, err))
			continue
		}
		if conv := arrayFromAny(obj["conversations"]); len(conv) > 0 {
			sessionID := stringFrom(obj, "session_id", "sessionId", "id")
			if sessionID == "" {
				sessionID = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
			}
			model := stringFrom(obj, "model")
			created := timeString(obj["timestamp"])
			for i, value := range conv {
				msg, ok := value.(map[string]any)
				if !ok {
					warnings = append(warnings, fmt.Sprintf("%s:%d.%d: Hermes trajectory message is not an object", path, ordinal, i+1))
					continue
				}
				rec, warning := normalizeTrajectoryMessage(path, ordinal, i+1, sessionID, model, created, msg)
				if warning != "" {
					warnings = append(warnings, warning)
					continue
				}
				records = append(records, rec)
			}
			continue
		}
		rec, warning := normalizeJSONLEvent(path, ordinal, line, obj)
		if warning != "" {
			warnings = append(warnings, warning)
			continue
		}
		records = append(records, rec)
	}
	if err := scanner.Err(); err != nil {
		return records, warnings, err
	}
	return records, warnings, nil
}

func normalizeJSONLEvent(path string, ordinal int64, line []byte, obj map[string]any) (adapter.Record, string) {
	sessionID := stringFrom(obj, "session_id", "sessionId", "session")
	if sessionID == "" {
		sessionID = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	}
	eventType := stringFrom(obj, "type", "event", "event_type")
	model := stringFrom(obj, "model")
	role := stringFrom(obj, "role", "actor")
	ts := timeString(firstAny(obj["timestamp"], obj["ts"], obj["created_at"], obj["time"]))
	text := strings.TrimSpace(firstNonEmpty(
		sources.TextFromAny(obj["content"], 4000),
		sources.TextFromAny(obj["message"], 4000),
		sources.TextFromAny(obj["text"], 4000),
		sources.TextFromAny(obj["output"], 4000),
		sources.TextFromAny(obj["result"], 4000),
	))
	if text == "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Hermes", eventType, role, model), " "))
	}
	if text == "" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for Hermes event", path, ordinal)
	}
	raw := adapter.RawRef{Format: "json", Hash: "sha256:" + sources.HashBytes(line), Path: path, Ordinal: &ordinal}
	return buildRecord(path, ordinal, sessionID, model, "", ts, eventType, role, text, obj, raw), ""
}

func normalizeMessage(path string, ordinal int64, sessionID, model, platform, fallbackTS string, msg map[string]any) (adapter.Record, string) {
	role := stringFrom(msg, "role", "from")
	ts := timeString(firstAny(msg["timestamp"], msg["created_at"], msg["time"]))
	if ts == "" {
		ts = fallbackTS
	}
	text := sources.TextFromAny(msg["content"], 4000)
	if text == "" {
		text = sources.TextFromAny(msg["value"], 4000)
	}
	if text == "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Hermes", role, stringFrom(msg, "name", "tool_call_id", "tool_call_id")), " "))
	}
	if text == "" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for Hermes message", path, ordinal)
	}
	raw := rawRef(path, ordinal, msg)
	rec := buildRecord(path, ordinal, sessionID, model, platform, ts, "message", role, text, msg, raw)
	if toolCallID := stringFrom(msg, "tool_call_id", "tool_callId"); toolCallID != "" {
		rec.Item.ExternalID = "hermes:tool_result:" + toolCallID
		rec.Relations = append(rec.Relations, adapter.Relation{
			TargetExternalID: "hermes:tool_call:" + toolCallID,
			Type:             "result_of",
		})
	}
	return rec, ""
}

func normalizeTrajectoryMessage(path string, lineOrdinal int64, messageOrdinal int, sessionID, model, created string, msg map[string]any) (adapter.Record, string) {
	role := stringFrom(msg, "from", "role")
	text := sources.TextFromAny(firstAny(msg["value"], msg["content"], msg["text"]), 4000)
	if text == "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Hermes", "trajectory", role), " "))
	}
	if text == "" {
		return adapter.Record{}, fmt.Sprintf("%s:%d.%d: no searchable text for Hermes trajectory message", path, lineOrdinal, messageOrdinal)
	}
	ordinal := lineOrdinal*100000 + int64(messageOrdinal)
	raw := rawRef(path, ordinal, msg)
	return buildRecord(path, ordinal, sessionID, model, "", created, "trajectory", role, text, msg, raw), ""
}

func buildRecord(path string, ordinal int64, sessionID, model, platform, ts, eventType, role, text string, obj map[string]any, raw adapter.RawRef) adapter.Record {
	if sessionID == "" {
		sessionID = strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	}
	if role == "gpt" {
		role = "assistant"
	}
	itemHash := sources.HashBytes([]byte(text))
	externalID := "hermes:" + sources.StableID(path, sessionID, fmt.Sprint(ordinal), eventType, ts, role, itemHash)
	meta := map[string]any{
		"harness":    "hermes",
		"event_type": eventType,
		"session_id": sessionID,
		"model":      model,
		"platform":   platform,
		"file_path":  path,
		"ordinal":    ordinal,
	}
	toolCallID := stringFrom(obj, "tool_call_id", "tool_callId", "id")
	toolName := stringFrom(obj, "name", "tool_name", "tool")
	if toolCallID != "" {
		meta["tool_call_id"] = toolCallID
	}
	if toolName != "" {
		meta["tool_name"] = toolName
	}
	kind := sources.KindFromEvent(eventType+" "+toolName, text)
	if toolCallID != "" || toolName != "" {
		kind = "tool_call"
	}
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "hermes", Name: "Hermes Sessions"},
		Collection: adapter.Collection{
			ExternalID: "hermes:session:" + sessionID,
			Kind:       "agent_session",
			Name:       sessionID,
			Metadata:   sources.Metadata(map[string]any{"harness": "hermes", "session_id": sessionID, "model": model, "platform": platform}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       kind,
			CreatedAt:  ts,
			Text:       text,
			Tags:       []string{"agent-session", "hermes"},
			Metadata:   sources.Metadata(meta),
		},
		Actor: sources.ActorFromRole("hermes", role, eventType),
		Raw:   raw,
	}
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, obj)...)
	return rec
}

func toolCallRecords(path string, messageOrdinal int64, sessionID, model, platform, fallbackTS string, msg map[string]any) []adapter.Record {
	content, ok := msg["content"].([]any)
	if !ok {
		return nil
	}
	var records []adapter.Record
	for i, value := range content {
		part, ok := value.(map[string]any)
		if !ok {
			continue
		}
		partType := strings.ToLower(stringFrom(part, "type"))
		toolID := stringFrom(part, "id", "tool_call_id", "tool_callId")
		toolName := stringFrom(part, "name", "tool_name", "tool")
		if toolID == "" && toolName == "" {
			continue
		}
		if !strings.Contains(partType, "tool") && part["input"] == nil {
			continue
		}
		ts := timeString(firstAny(part["timestamp"], part["created_at"], part["time"]))
		if ts == "" {
			ts = fallbackTS
		}
		input, _ := part["input"].(map[string]any)
		text := strings.TrimSpace(strings.Join(nonEmpty("tool_call", toolName, toolID, sources.TextFromAny(part["input"], 4000)), "\n"))
		if text == "" {
			continue
		}
		ordinal := messageOrdinal*100000 + int64(i+1)
		raw := rawRef(path, ordinal, part)
		rec := buildRecord(path, ordinal, sessionID, model, platform, ts, "tool_call", "tool", text, part, raw)
		if toolID != "" {
			rec.Item.ExternalID = "hermes:tool_call:" + toolID
		}
		if input != nil {
			rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(rec.Item.ExternalID, input)...)
		}
		records = append(records, rec)
	}
	return records
}

func rawRef(path string, ordinal int64, value any) adapter.RawRef {
	b, _ := json.Marshal(value)
	return adapter.RawRef{
		Format:  "json",
		Hash:    "sha256:" + sources.HashBytes(b),
		Path:    path,
		Ordinal: &ordinal,
	}
}

func arrayFromAny(v any) []any {
	if arr, ok := v.([]any); ok {
		return arr
	}
	return nil
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
		if parsed, err := time.Parse("2006-01-02T15:04:05", t); err == nil {
			return parsed.UTC().Format(time.RFC3339Nano)
		}
		return t
	case float64:
		if t > 100000000000 {
			return time.UnixMilli(int64(t)).UTC().Format(time.RFC3339Nano)
		}
		if t > 0 {
			return time.Unix(int64(t), 0).UTC().Format(time.RFC3339Nano)
		}
	}
	return ""
}

func firstAny(values ...any) any {
	for _, value := range values {
		switch v := value.(type) {
		case nil:
			continue
		case string:
			if strings.TrimSpace(v) == "" {
				continue
			}
		}
		return value
	}
	return nil
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

func SupportedFiles(root string) ([]string, error) {
	files, err := sources.ListJSONLFiles(root, Include)
	if err != nil {
		return nil, err
	}
	sort.Strings(files)
	return files, nil
}
