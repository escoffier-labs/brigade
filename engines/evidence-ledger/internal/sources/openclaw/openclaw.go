package openclaw

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
		sources.ApplyRedaction(&rec, opts)
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
	data, _ := ev.Object["data"].(map[string]any)
	sessionID := sources.String(ev.Object, "sessionId", "session_id", "session")
	if sessionID == "" && data != nil {
		sessionID = sources.String(data, "sessionId", "session_id", "id")
	}
	if sessionID == "" {
		sessionID = filepath.Base(ev.Path)
	}
	runID := sources.String(ev.Object, "runId", "run_id")
	if runID == "" && data != nil {
		runID = sources.String(data, "runId", "run_id")
	}
	workspaceDir := sources.String(ev.Object, "workspaceDir", "workspace_dir", "cwd")
	if workspaceDir == "" && data != nil {
		workspaceDir = sources.String(data, "workspaceDir", "workspace_dir", "cwd")
	}
	role := sources.String(ev.Object, "role", "actor")
	if role == "" && data != nil {
		role = sources.String(data, "role", "actor")
	}
	text := openclawText(ev.Object, data)
	if text == "" && (eventType == "model_change" || eventType == "thinking_level_change" || eventType == "custom") {
		text = strings.TrimSpace(strings.Join(nonEmpty("OpenClaw", eventType, sources.String(ev.Object, "modelId"), sources.String(ev.Object, "thinkingLevel"), sources.String(ev.Object, "customType")), " "))
	}
	if text == "" && eventType != "session" && eventType != "session.started" && eventType != "session.ended" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for event type %q", ev.Path, ev.Ordinal, eventType)
	}
	if text == "" {
		text = eventType
	}
	model := sources.String(ev.Object, "model")
	if model == "" && data != nil {
		model = sources.String(data, "model")
	}
	kind := sources.KindFromEvent(eventType, text)
	itemHash := sources.HashBytes([]byte(text))
	externalID := "openclaw:" + sources.StableID(ev.Path, sessionID, runID, fmt.Sprint(ev.Ordinal), eventType, ts, itemHash)
	if eventType == "session" || eventType == "session.started" {
		externalID = "openclaw:session:" + sessionID
	}
	if eventType == "run.started" && runID != "" {
		externalID = "openclaw:run:" + runID
	}
	meta := map[string]any{
		"harness":       "openclaw",
		"event_type":    eventType,
		"session_id":    sessionID,
		"run_id":        runID,
		"model":         model,
		"workspace_dir": workspaceDir,
		"file_path":     ev.Path,
		"ordinal":       ev.Ordinal,
		"trajectory":    strings.Contains(filepath.Base(ev.Path), ".trajectory."),
	}
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "openclaw", Name: "OpenClaw Agent Sessions"},
		Collection: adapter.Collection{
			ExternalID: "openclaw:session:" + sessionID,
			Kind:       "agent_session",
			Name:       sessionID,
			Metadata:   sources.Metadata(map[string]any{"harness": "openclaw", "session_id": sessionID, "workspace_dir": workspaceDir}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       kind,
			CreatedAt:  ts,
			Text:       text,
			Tags:       []string{"agent-session", "openclaw"},
			Metadata:   sources.Metadata(meta),
		},
		Actor: sources.ActorFromRole("openclaw", role, eventType),
		Raw:   sources.RawRef(ev),
	}
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, ev.Object)...)
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, data)...)
	if externalID != "openclaw:session:"+sessionID {
		rec.Relations = append(rec.Relations, adapter.Relation{
			TargetExternalID: "openclaw:session:" + sessionID,
			Type:             "belongs_to_session",
		})
	}
	if runID != "" && externalID != "openclaw:run:"+runID {
		rec.Relations = append(rec.Relations, adapter.Relation{
			TargetExternalID: "openclaw:run:" + runID,
			Type:             "belongs_to_run",
		})
	}
	return rec, ""
}

func openclawText(root map[string]any, data map[string]any) string {
	for _, v := range []any{
		root["text"],
		root["message"],
		root["content"],
		root["prompt"],
		root["output"],
		root["result"],
		data,
	} {
		if s := sources.TextFromAny(v, 4000); s != "" {
			return strings.TrimSpace(s)
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
