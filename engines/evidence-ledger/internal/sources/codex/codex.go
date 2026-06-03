package codex

import (
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
	var result sources.Result
	err = sources.WalkJSONL(path, sources.DefaultInclude, func(ev sources.RawEvent) error {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			return nil
		}
		if warning, _ := ev.Object["_warning"].(string); warning != "" {
			result.Warnings = append(result.Warnings, fmt.Sprintf("%s:%d: %s", ev.Path, ev.Ordinal, warning))
			return nil
		}
		rec, warning := normalize(ev)
		if warning != "" {
			result.Warnings = append(result.Warnings, warning)
			return nil
		}
		if !sources.KeepTimestamp(rec.Item.CreatedAt, since, hasSince) {
			return nil
		}
		if err := sources.WriteRecord(w, rec); err != nil {
			return err
		}
		result.Records++
		return nil
	})
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
	text := codexText(ev.Object, payload)
	if text == "" && eventType != "session_meta" && eventType != "turn_context" {
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
	kind := sources.KindFromEvent(eventType, text)
	itemHash := sources.HashBytes([]byte(text))
	externalID := "codex:" + sources.StableID(ev.Path, sessionID, fmt.Sprint(ev.Ordinal), eventType, ts, itemHash)
	meta := map[string]any{
		"harness":     "codex",
		"event_type":  eventType,
		"session_id":  sessionID,
		"model":       model,
		"cwd":         cwd,
		"file_path":   ev.Path,
		"ordinal":     ev.Ordinal,
		"source_file": filepath.Base(ev.Path),
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
	return rec, ""
}

func codexText(root, payload map[string]any) string {
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

func cleanText(s string) string {
	return strings.TrimSpace(s)
}
