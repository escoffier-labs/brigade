package pi

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

type sessionCtx struct {
	id  string
	cwd string
}

// DefaultRoot returns the standard Pi agent session root.
func DefaultRoot() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = os.Getenv("HOME")
	}
	return filepath.Join(home, ".pi", "agent", "sessions")
}

func Generate(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	scans, err := sources.NewFileScanSet(path, sources.DefaultInclude)
	if err != nil {
		return sources.Result{}, err
	}
	sessions := map[string]sessionCtx{}
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
		eventType := sources.String(ev.Object, "type")
		switch eventType {
		case "session":
			sessions[ev.Path] = sessionCtx{
				id:  sources.String(ev.Object, "id"),
				cwd: sources.String(ev.Object, "cwd"),
			}
			return nil
		case "model_change", "thinking_level_change":
			return nil
		case "message":
			// handled below
		default:
			return nil
		}
		rec, warning := normalize(ev, sessions[ev.Path])
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

func normalize(ev sources.RawEvent, ctx sessionCtx) (adapter.Record, string) {
	eventType := sources.String(ev.Object, "type")
	ts := sources.String(ev.Object, "timestamp", "created_at", "ts")
	sessionID := ctx.id
	if sessionID == "" {
		sessionID = strings.TrimSuffix(filepath.Base(ev.Path), filepath.Ext(ev.Path))
	}
	msgID := sources.String(ev.Object, "id")
	message, _ := ev.Object["message"].(map[string]any)
	role := sources.String(message, "role")
	cwd := ctx.cwd
	project := filepath.Base(filepath.Dir(ev.Path))
	text := piText(message)
	if text == "" {
		return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for event type %q", ev.Path, ev.Ordinal, eventType)
	}
	itemHash := sources.HashBytes([]byte(text))
	externalID := "pi:" + sources.StableID(ev.Path, sessionID, msgID, fmt.Sprint(ev.Ordinal), eventType, ts, itemHash)
	kind := sources.KindFromEvent(eventType, text)
	meta := map[string]any{
		"harness":    "pi",
		"event_type": eventType,
		"session_id": sessionID,
		"message_id": msgID,
		"parent_id":  sources.String(ev.Object, "parentId", "parent_id"),
		"cwd":        cwd,
		"project":    project,
		"file_path":  ev.Path,
		"ordinal":    ev.Ordinal,
	}
	rec := adapter.Record{
		Schema: adapter.SchemaV1,
		Source: adapter.Source{Kind: "pi", Name: "Pi Agent Sessions"},
		Collection: adapter.Collection{
			ExternalID: "pi:session:" + sessionID,
			Kind:       "agent_session",
			Name:       sessionID,
			Metadata:   sources.Metadata(map[string]any{"harness": "pi", "session_id": sessionID, "cwd": cwd, "project": project}),
		},
		Item: adapter.Item{
			ExternalID: externalID,
			Kind:       kind,
			CreatedAt:  ts,
			Text:       text,
			Tags:       []string{"agent-session", "pi"},
			Metadata:   sources.Metadata(meta),
		},
		Actor: sources.ActorFromRole("pi", role, eventType),
		Raw:   sources.RawRef(ev),
	}
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, ev.Object)...)
	rec.Artifacts = append(rec.Artifacts, sources.ExtractArtifacts(externalID, message)...)
	return rec, ""
}

func piText(message map[string]any) string {
	if message == nil {
		return ""
	}
	return strings.TrimSpace(sources.TextFromAny(message["content"], 4000))
}
