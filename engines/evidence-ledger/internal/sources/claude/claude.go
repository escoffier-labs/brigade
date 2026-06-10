package claude

import (
	"fmt"
	"io"
	"path/filepath"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	scans, err := sources.NewFileScanSet(path, sources.DefaultInclude)
	if err != nil {
		return sources.Result{}, err
	}
	var result sources.Result
	err = scans.Walk(opts, func(ev sources.RawEvent) error {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			return nil
		}
		if warning, _ := ev.Object["_warning"].(string); warning != "" {
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s:%d: %s", ev.Path, ev.Ordinal, warning))
			scans.Warning(ev.Path)
			return nil
		}
		rec, warning := normalize(ev)
		if warning != "" {
			result.Warnings = append(result.Warnings, warning)
			scans.Warning(ev.Path)
			return nil
		}
		if !sources.KeepTimestamp(rec.Item.CreatedAt, since, hasSince) {
			return nil
		}
		if err := sources.WriteRecord(w, rec); err != nil {
			return err
		}
		result.Records++
		scans.Record(ev.Path)
		return nil
	})
	result.Files = scans.List()
	return result, err
}

func normalize(ev sources.RawEvent) (adapter.Record, string) {
	eventType := sources.String(ev.Object, "type")
	ts := sources.String(ev.Object, "timestamp", "created_at", "ts")
	sessionID := sources.String(ev.Object, "sessionId", "session_id")
	if sessionID == "" {
		sessionID = filepath.Base(ev.Path)
	}
	uuid := sources.String(ev.Object, "uuid", "leafUuid", "requestId")
	message, _ := ev.Object["message"].(map[string]any)
	role := sources.String(ev.Object, "userType")
	if message != nil && sources.String(message, "role") != "" {
		role = sources.String(message, "role")
	}
	cwd := sources.String(ev.Object, "cwd")
	model := ""
	if message != nil {
		model = sources.String(message, "model")
	}
	text := claudeText(ev.Object, message)
	if text == "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Claude", eventType, sources.String(ev.Object, "operation"), uuid), " "))
	}
	if text == "" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for event type %q", ev.Path, ev.Ordinal, eventType)
	}
	itemHash := sources.HashBytes([]byte(text))
	externalID := "claude:" + sources.StableID(ev.Path, sessionID, uuid, fmt.Sprint(ev.Ordinal), eventType, ts, itemHash)
	toolUseID := claudeToolUseID(message)
	toolResultID := claudeToolResultID(message)
	kind := claudeKind(eventType, text, message)
	if toolUseID != "" || toolResultID != "" {
		kind = "tool_call"
	}
	if toolUseID != "" {
		externalID = "claude:tool_use:" + toolUseID
	}
	if toolResultID != "" {
		externalID = "claude:tool_result:" + toolResultID
	}
	project := filepath.Base(filepath.Dir(ev.Path))
	meta := map[string]any{
		"harness":        "claude",
		"event_type":     eventType,
		"session_id":     sessionID,
		"uuid":           uuid,
		"request_id":     sources.String(ev.Object, "requestId"),
		"model":          model,
		"cwd":            cwd,
		"project":        project,
		"file_path":      ev.Path,
		"ordinal":        ev.Ordinal,
		"git_branch":     sources.String(ev.Object, "gitBranch"),
		"is_sidechain":   ev.Object["isSidechain"],
		"tool_use_id":    toolUseID,
		"tool_result_id": toolResultID,
	}
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "claude", Name: "Claude Project Logs"},
		Collection: adapter.Collection{
			ExternalID: "claude:session:" + sessionID,
			Kind:       "agent_session",
			Name:       sessionID,
			Metadata:   sources.Metadata(map[string]any{"harness": "claude", "session_id": sessionID, "cwd": cwd, "project": project}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       kind,
			CreatedAt:  ts,
			Text:       text,
			Tags:       []string{"agent-session", "claude"},
			Metadata:   sources.Metadata(meta),
		},
		Actor: sources.ActorFromRole("claude", role, eventType),
		Raw:   sources.RawRef(ev),
	}
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, ev.Object)...)
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, message)...)
	if toolResultID != "" {
		rec.Relations = append(rec.Relations, adapter.Relation{
			TargetExternalID: "claude:tool_use:" + toolResultID,
			Type:             "result_of",
		})
	}
	return rec, ""
}

func claudeText(root, message map[string]any) string {
	if message != nil {
		if s := sources.TextFromAny(message["content"], 4000); s != "" {
			return strings.TrimSpace(s)
		}
	}
	for _, v := range []any{root["content"], root["attachment"], root["toolUseResult"], root["lastPrompt"], root["operation"]} {
		if s := sources.TextFromAny(v, 4000); s != "" {
			return strings.TrimSpace(s)
		}
	}
	return ""
}

func claudeKind(eventType, text string, message map[string]any) string {
	lower := strings.ToLower(eventType + " " + text)
	if message != nil {
		lower += " " + strings.ToLower(sources.TextFromAny(message["content"], 4000))
	}
	if strings.Contains(lower, "tool_use") || strings.Contains(lower, "tool_result") || strings.Contains(lower, "tool") {
		return "tool_call"
	}
	return sources.KindFromEvent(eventType, text)
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

func claudeToolUseID(message map[string]any) string {
	if message == nil {
		return ""
	}
	content, _ := message["content"].([]any)
	for _, part := range content {
		m, ok := part.(map[string]any)
		if !ok {
			continue
		}
		if sources.String(m, "type") == "tool_use" {
			return sources.String(m, "id")
		}
	}
	return ""
}

func claudeToolResultID(message map[string]any) string {
	if message == nil {
		return ""
	}
	content, _ := message["content"].([]any)
	for _, part := range content {
		m, ok := part.(map[string]any)
		if !ok {
			continue
		}
		if sources.String(m, "type") == "tool_result" {
			return sources.String(m, "tool_use_id")
		}
	}
	return ""
}
