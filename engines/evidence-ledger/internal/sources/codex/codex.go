package codex

import (
	"encoding/json"
	"fmt"
	"io"
	"path/filepath"
	"strings"

	"github.com/openclaw/logspine/internal/adapter"
	"github.com/openclaw/logspine/internal/sources"
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
	err = sources.WalkJSONL(path, sources.DefaultInclude, func(ev sources.RawEvent) error {
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
	ts := sources.String(ev.Object, "timestamp", "ts", "created_at")
	payload, _ := ev.Object["payload"].(map[string]any)
	if payload == nil {
		payload = ev.Object
	}
	sessionID := sources.String(payload, "session_id", "sessionId", "id")
	if sessionID == "" {
		sessionID = sources.String(ev.Object, "session_id", "sessionId", "session")
	}
	if sessionID == "" {
		sessionID = filepath.Base(ev.Path)
	}
	role := sources.String(payload, "role", "author")
	if role == "" {
		role = sources.NestedString(payload, "message", "role")
	}
	payloadType := sources.String(payload, "type")
	name := sources.String(payload, "name")
	callID := sources.String(payload, "call_id", "callId")
	arguments := sources.String(payload, "arguments")
	encrypted := payload["encrypted_content"] != nil
	text := codexText(ev.Object, payload)
	if text == "" && encrypted {
		text = strings.TrimSpace(strings.Join(nonEmpty("Codex", eventType, payloadType, name, callID, "encrypted_content present"), " "))
	}
	if text == "" && payloadType != "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Codex", eventType, payloadType, name, callID), " "))
	}
	if text == "" && eventType != "session_meta" && eventType != "turn_context" && eventType != "compacted" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for event type %q", ev.Path, ev.Ordinal, eventType)
	}
	if text == "" {
		text = eventType
	}
	model := sources.String(payload, "model")
	if model == "" {
		model = sources.String(ev.Object, "model")
	}
	cwd := sources.String(payload, "cwd", "workspace_dir", "workspaceDir")
	if cwd == "" {
		cwd = sources.String(ev.Object, "cwd", "workspace_dir", "workspaceDir")
	}
	kind := codexKind(eventType, payloadType, name, text)
	itemHash := sources.HashBytes([]byte(text))
	externalID := "codex:" + sources.StableID(ev.Path, sessionID, fmt.Sprint(ev.Ordinal), eventType, ts, itemHash)
	if callID != "" {
		if strings.Contains(strings.ToLower(payloadType), "output") || strings.Contains(strings.ToLower(payloadType), "result") {
			externalID = "codex:call_result:" + callID
		} else {
			externalID = "codex:call:" + callID
		}
	}
	meta := map[string]any{
		"harness":      "codex",
		"event_type":   eventType,
		"session_id":   sessionID,
		"model":        model,
		"cwd":          cwd,
		"file_path":    ev.Path,
		"ordinal":      ev.Ordinal,
		"source_file":  filepath.Base(ev.Path),
		"payload_type": payloadType,
		"name":         name,
		"call_id":      callID,
		"arguments":    arguments,
		"encrypted":    encrypted,
	}
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "codex", Name: "Codex Sessions"},
		Collection: adapter.Collection{
			ExternalID: "codex:session:" + sessionID,
			Kind:       "agent_session",
			Name:       sessionID,
			Metadata:   sources.Metadata(map[string]any{"harness": "codex", "session_id": sessionID, "cwd": cwd}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       kind,
			CreatedAt:  ts,
			Text:       text,
			Tags:       []string{"agent-session", "codex"},
			Metadata:   sources.Metadata(meta),
		},
		Actor: sources.ActorFromRole("codex", role, eventType),
		Raw:   sources.RawRef(ev),
	}
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, ev.Object)...)
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, payload)...)
	rec.Artifacts = append(rec.Artifacts, artifactsFromArguments(externalID, arguments)...)
	if callID != "" && strings.HasPrefix(externalID, "codex:call_result:") {
		rec.Relations = append(rec.Relations, adapter.Relation{
			TargetExternalID: "codex:call:" + callID,
			Type:             "result_of",
		})
	}
	return rec, ""
}

func codexText(root, payload map[string]any) string {
	if summary := sources.TextFromAny(payload["summary"], 4000); summary != "" {
		return cleanText("summary: " + summary)
	}
	if callText := codexCallText(payload); callText != "" {
		return callText
	}
	for _, v := range []any{
		payload["text"],
		payload["message"],
		payload["content"],
		payload["output"],
		payload["result"],
		payload["delta"],
		payload["item"],
		root["message"],
		root["content"],
		root["text"],
	} {
		if s := sources.TextFromAny(v, 4000); s != "" {
			return cleanText(s)
		}
	}
	return ""
}

func codexCallText(payload map[string]any) string {
	payloadType := strings.ToLower(sources.String(payload, "type"))
	name := sources.String(payload, "name")
	callID := sources.String(payload, "call_id", "callId")
	arguments := sources.String(payload, "arguments")
	if payloadType == "" && name == "" && callID == "" && arguments == "" {
		return ""
	}
	if !strings.Contains(payloadType, "function") && !strings.Contains(payloadType, "tool") && name == "" && arguments == "" {
		return ""
	}
	parts := nonEmpty(payloadType, name, callID, arguments)
	return cleanText(strings.Join(parts, "\n"))
}

func codexKind(eventType, payloadType, name, text string) string {
	lower := strings.ToLower(eventType + " " + payloadType + " " + name + " " + text)
	if strings.Contains(lower, "shell") || strings.Contains(lower, "bash") || strings.Contains(lower, "exec_command") || strings.Contains(lower, "command") {
		return "command"
	}
	if strings.Contains(lower, "function") || strings.Contains(lower, "tool") || strings.Contains(lower, "call_id") {
		return "tool_call"
	}
	return sources.KindFromEvent(eventType+" "+payloadType, text)
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

func artifactsFromArguments(itemID, arguments string) []adapter.Artifact {
	if strings.TrimSpace(arguments) == "" {
		return nil
	}
	var obj map[string]any
	if err := json.Unmarshal([]byte(arguments), &obj); err != nil {
		return nil
	}
	var out []adapter.Artifact
	add := func(kind, path, text string) {
		if path == "" && text == "" {
			return
		}
		out = append(out, adapter.Artifact{
			ExternalID: sources.StableID(itemID, kind, path, text),
			Kind:       kind,
			Path:       path,
			Text:       sources.TextFromAny(text, 4000),
			Hash:       "sha256:" + sources.HashBytes([]byte(path+text)),
		})
	}
	for _, key := range []string{"cmd", "command", "shell"} {
		if s := sources.String(obj, key); s != "" {
			add("command", "", s)
		}
	}
	for _, key := range []string{"cwd", "workdir", "workspace_dir"} {
		if s := sources.String(obj, key); s != "" {
			add("workspace", s, "")
		}
	}
	for _, key := range []string{"path", "file_path", "patch_path"} {
		if s := sources.String(obj, key); s != "" {
			add("file", s, "")
		}
	}
	return out
}

func cleanText(s string) string {
	return strings.TrimSpace(s)
}
