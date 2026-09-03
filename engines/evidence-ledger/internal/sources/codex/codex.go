package codex

import (
	"encoding/json"
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
		rec, warning, skipped, truncated := normalize(ev)
		if skipped {
			result.Skipped++
			return nil
		}
		if truncated {
			result.Truncated++
		}
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

func normalize(ev sources.RawEvent) (rec adapter.Record, warning string, skipped bool, truncated bool) {
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

	// Create a copy of payload and ev.Object to compute text without the huge arguments
	// so that we don't bleed huge arguments into the `text` field, which ruins the truncation.
	var truncatedArgs = arguments
	if len(truncatedArgs) > 4000 {
		truncatedArgs = truncatedArgs[:4000] + "\n[truncated]"
		truncated = true
	}

	payloadForText := make(map[string]any)
	for k, v := range payload {
		payloadForText[k] = v
	}
	if arguments != "" {
		payloadForText["arguments"] = truncatedArgs
	}

	encrypted := payload["encrypted_content"] != nil
	text := codexText(ev.Object, payloadForText)
	if text == "" && encrypted {
		text = strings.TrimSpace(strings.Join(nonEmpty("Codex", eventType, payloadType, name, callID, "encrypted_content present"), " "))
	}
	if text == "" && payloadType != "" {
		text = strings.TrimSpace(strings.Join(nonEmpty("Codex", eventType, payloadType, name, callID), " "))
	}
	if text == "" {
		if eventType == "event_msg" || eventType == "turn_context" || eventType == "response_item" {
			return adapter.Record{}, "", true, false
		}
		if eventType != "session_meta" && eventType != "compacted" {
			return adapter.Record{}, fmt.Sprintf("%s:%d: no searchable text for event type %q", ev.Path, ev.Ordinal, eventType), false, false
		}
	}
	if text == "" {
		text = eventType
	}

	arguments = truncatedArgs
	model := sources.String(payload, "model")
	if model == "" {
		model = sources.String(ev.Object, "model")
	}
	cwd := sources.String(payload, "cwd", "workspace_dir", "workspaceDir")
	if cwd == "" {
		cwd = sources.String(ev.Object, "cwd", "workspace_dir", "workspaceDir")
	}
	kind := codexKind(eventType, payloadType, name, text)

	// Calculate a full digest of the arguments, if truncated.
	var digest string
	if truncated {
		digest = sources.HashBytes([]byte(sources.String(payload, "arguments")))
	}

	// For the hash, include the full digest so the externalID reflects the full data
	hashData := text
	if digest != "" {
		hashData += digest
	}
	itemHash := sources.HashBytes([]byte(hashData))
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
	if truncated {
		meta["arguments_digest"] = digest
	}

	rawEv := ev
	if arguments != "" {
		// "single-stored arguments": if we keep them in `meta`, we should remove them from `raw`
		// to avoid storing them twice even if they are not truncated.
		var newObj map[string]any
		objBytes, _ := json.Marshal(ev.Object)
		_ = json.Unmarshal(objBytes, &newObj)
		if p, ok := newObj["payload"].(map[string]any); ok {
			if _, hasArgs := p["arguments"]; hasArgs {
				delete(p, "arguments")
			}
		}
		if _, hasArgs := newObj["arguments"]; hasArgs {
			delete(newObj, "arguments")
		}
		rawEv.Object = newObj
		// Update rawEv.Line so that the json format does not contain the original arguments
		if newLine, err := json.Marshal(newObj); err == nil {
			rawEv.Line = newLine
		}
	}

	rec = adapter.Record{
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
		Raw:   sources.RawRef(rawEv),
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
	return rec, "", false, truncated
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
