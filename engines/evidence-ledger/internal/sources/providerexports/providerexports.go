package providerexports

import (
	"archive/zip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/sources"
)

func GenerateChatGPT(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	raw, scan, err := loadExport(path, []string{"conversations.json"})
	if err != nil {
		return sources.Result{}, err
	}
	if opts.Skip != nil && opts.Skip(scan.Path, scan.Size, scan.MTime) {
		scan.Skipped = true
		return sources.Result{Files: []sources.FileScan{scan}}, nil
	}
	scan.ContentHash = "sha256:" + sources.HashBytes(raw)
	var conversations []map[string]any
	if err := json.Unmarshal(raw, &conversations); err != nil {
		var root map[string]any
		if rootErr := json.Unmarshal(raw, &root); rootErr != nil {
			return sources.Result{}, err
		}
		conversations = mapsFromAny(root["conversations"])
	}
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	var result sources.Result
	for _, conv := range conversations {
		records, warnings, err := emitChatGPTConversation(conv, scan.Path, opts, since, hasSince, w, &result)
		if err != nil {
			return sources.Result{}, err
		}
		result.Warnings = append(result.Warnings, warnings...)
		scan.Records += records
		scan.Warnings += len(warnings)
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
	}
	result.Files = []sources.FileScan{scan}
	return result, nil
}

func GenerateClaude(path string, opts sources.Options, w io.Writer) (sources.Result, error) {
	raw, scan, err := loadExport(path, []string{"conversations.json"})
	if err != nil {
		return sources.Result{}, err
	}
	if opts.Skip != nil && opts.Skip(scan.Path, scan.Size, scan.MTime) {
		scan.Skipped = true
		return sources.Result{Files: []sources.FileScan{scan}}, nil
	}
	scan.ContentHash = "sha256:" + sources.HashBytes(raw)
	conversations, err := parseClaudeConversations(raw)
	if err != nil {
		return sources.Result{}, err
	}
	since, hasSince, err := sources.ParseSince(opts.Since)
	if err != nil {
		return sources.Result{}, err
	}
	var result sources.Result
	for _, conv := range conversations {
		records, warnings, err := emitClaudeConversation(conv, scan.Path, opts, since, hasSince, w, &result)
		if err != nil {
			return sources.Result{}, err
		}
		result.Warnings = append(result.Warnings, warnings...)
		scan.Records += records
		scan.Warnings += len(warnings)
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
	}
	result.Files = []sources.FileScan{scan}
	return result, nil
}

func emitChatGPTConversation(conv map[string]any, rawPath string, opts sources.Options, since time.Time, hasSince bool, w io.Writer, result *sources.Result) (int, []string, error) {
	convID := firstString(conv, "id", "conversation_id")
	if convID == "" {
		convID = sources.StableID(sources.TextFromAny(conv["title"], 200), rawPath)
	}
	title := firstString(conv, "title", "name")
	if title == "" {
		title = convID
	}
	nodes, _ := conv["mapping"].(map[string]any)
	if len(nodes) == 0 {
		return 0, []string{fmt.Sprintf("%s: chatgpt conversation %s has no mapping", rawPath, convID)}, nil
	}
	ordered := make([]chatGPTNode, 0, len(nodes))
	for key, rawNode := range nodes {
		node, _ := rawNode.(map[string]any)
		if node == nil {
			continue
		}
		msg, _ := node["message"].(map[string]any)
		if msg == nil {
			continue
		}
		ts := timestampFromAny(firstAny(msg, "create_time", "update_time"))
		ordered = append(ordered, chatGPTNode{Key: key, Node: node, Message: msg, Timestamp: ts})
	}
	sort.SliceStable(ordered, func(i, j int) bool {
		if ordered[i].Timestamp == ordered[j].Timestamp {
			return ordered[i].Key < ordered[j].Key
		}
		return ordered[i].Timestamp < ordered[j].Timestamp
	})
	var records int
	var warnings []string
	for idx, node := range ordered {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
		text := chatGPTText(node.Message)
		if text == "" {
			continue
		}
		createdAt := unixTimeString(firstAny(node.Message, "create_time"))
		if createdAt == "" {
			createdAt = unixTimeString(firstAny(conv, "create_time"))
		}
		if !sources.KeepTimestamp(createdAt, since, hasSince) {
			continue
		}
		msgID := firstString(node.Message, "id")
		if msgID == "" {
			msgID = node.Key
		}
		role := chatGPTRole(node.Message)
		rawBytes, _ := json.Marshal(node.Node)
		ordinal := int64(idx + 1)
		rec := adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "chatgpt", Name: "ChatGPT Export"},
			Collection: adapter.Collection{
				ExternalID: "chatgpt:conversation:" + convID,
				Kind:       "conversation",
				Name:       title,
				Metadata:   sources.Metadata(map[string]any{"provider": "chatgpt", "conversation_id": convID}),
			},
			Item: adapter.Item{
				ExternalID: "chatgpt:" + sources.StableID(convID, msgID, strconv.Itoa(idx)),
				Kind:       "message",
				CreatedAt:  createdAt,
				Text:       text,
				Tags:       []string{"ai-chat", "chatgpt"},
				Metadata:   sources.Metadata(map[string]any{"provider": "chatgpt", "conversation_id": convID, "message_id": msgID, "source_file": rawPath, "ordinal": ordinal}),
			},
			Actor: sources.ActorFromRole("chatgpt", role, "message"),
			Raw: adapter.RawRef{
				Format:  "json",
				Hash:    "sha256:" + sources.HashBytes(rawBytes),
				Path:    rawPath,
				Ordinal: &ordinal,
			},
		}
		sources.ApplyRedaction(&rec, opts)
		if err := sources.WriteRecord(w, rec); err != nil {
			return records, warnings, err
		}
		result.Records++
		records++
	}
	return records, warnings, nil
}

func emitClaudeConversation(conv map[string]any, rawPath string, opts sources.Options, since time.Time, hasSince bool, w io.Writer, result *sources.Result) (int, []string, error) {
	convID := firstString(conv, "uuid", "id", "conversation_id")
	if convID == "" {
		convID = sources.StableID(sources.TextFromAny(conv["name"], 200), rawPath)
	}
	title := firstString(conv, "name", "title")
	if title == "" {
		title = convID
	}
	messages := mapsFromAny(firstAny(conv, "chat_messages", "messages"))
	if len(messages) == 0 {
		return 0, []string{fmt.Sprintf("%s: claude conversation %s has no messages", rawPath, convID)}, nil
	}
	sort.SliceStable(messages, func(i, j int) bool {
		return timestampFromAny(firstAny(messages[i], "created_at", "createdAt", "updated_at", "updatedAt")) < timestampFromAny(firstAny(messages[j], "created_at", "createdAt", "updated_at", "updatedAt"))
	})
	var records int
	var warnings []string
	for idx, msg := range messages {
		if opts.Limit > 0 && result.Records >= opts.Limit {
			break
		}
		text := firstText(msg, "text", "content", "message")
		if text == "" {
			continue
		}
		createdAt := firstString(msg, "created_at", "createdAt")
		if createdAt == "" {
			createdAt = firstString(conv, "created_at", "createdAt")
		}
		if !sources.KeepTimestamp(createdAt, since, hasSince) {
			continue
		}
		msgID := firstString(msg, "uuid", "id", "message_id")
		if msgID == "" {
			msgID = strconv.Itoa(idx + 1)
		}
		role := firstString(msg, "sender", "role", "author")
		rawBytes, _ := json.Marshal(msg)
		ordinal := int64(idx + 1)
		rec := adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "claude-export", Name: "Claude Export"},
			Collection: adapter.Collection{
				ExternalID: "claude-export:conversation:" + convID,
				Kind:       "conversation",
				Name:       title,
				Metadata:   sources.Metadata(map[string]any{"provider": "claude", "conversation_id": convID}),
			},
			Item: adapter.Item{
				ExternalID: "claude-export:" + sources.StableID(convID, msgID, strconv.Itoa(idx)),
				Kind:       "message",
				CreatedAt:  createdAt,
				Text:       text,
				Tags:       []string{"ai-chat", "claude"},
				Metadata:   sources.Metadata(map[string]any{"provider": "claude", "conversation_id": convID, "message_id": msgID, "source_file": rawPath, "ordinal": ordinal}),
			},
			Actor: sources.ActorFromRole("claude-export", role, "message"),
			Raw: adapter.RawRef{
				Format:  "json",
				Hash:    "sha256:" + sources.HashBytes(rawBytes),
				Path:    rawPath,
				Ordinal: &ordinal,
			},
		}
		sources.ApplyRedaction(&rec, opts)
		if err := sources.WriteRecord(w, rec); err != nil {
			return records, warnings, err
		}
		result.Records++
		records++
	}
	return records, warnings, nil
}

type chatGPTNode struct {
	Key       string
	Node      map[string]any
	Message   map[string]any
	Timestamp float64
}

func chatGPTRole(msg map[string]any) string {
	author, _ := msg["author"].(map[string]any)
	if author != nil {
		return firstString(author, "role", "name")
	}
	return firstString(msg, "role", "author")
}

func chatGPTText(msg map[string]any) string {
	content, _ := msg["content"].(map[string]any)
	if content == nil {
		return firstText(msg, "text", "content", "message")
	}
	for _, key := range []string{"parts", "text", "content"} {
		if text := sources.TextFromAny(content[key], 8000); text != "" {
			return text
		}
	}
	return sources.TextFromAny(content, 8000)
}

func parseClaudeConversations(raw []byte) ([]map[string]any, error) {
	var arr []map[string]any
	if err := json.Unmarshal(raw, &arr); err == nil {
		return arr, nil
	}
	var root map[string]any
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, err
	}
	for _, key := range []string{"conversations", "chats"} {
		if conversations := mapsFromAny(root[key]); len(conversations) > 0 {
			return conversations, nil
		}
	}
	return []map[string]any{root}, nil
}

func loadExport(path string, names []string) ([]byte, sources.FileScan, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, sources.FileScan{}, err
	}
	if info.IsDir() {
		for _, name := range names {
			candidate := filepath.Join(path, name)
			if b, scan, err := loadRegularFile(candidate); err == nil {
				return b, scan, nil
			}
		}
		return nil, sources.FileScan{}, fmt.Errorf("no supported export file found in %s", path)
	}
	if strings.EqualFold(filepath.Ext(path), ".zip") {
		return loadZipExport(path, info, names)
	}
	return loadRegularFile(path)
}

func loadRegularFile(path string) ([]byte, sources.FileScan, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, sources.FileScan{}, err
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, sources.FileScan{}, err
	}
	return b, sources.FileScan{
		Path:  path,
		Size:  info.Size(),
		MTime: info.ModTime().UTC().Format(time.RFC3339Nano),
	}, nil
}

func loadZipExport(path string, info os.FileInfo, names []string) ([]byte, sources.FileScan, error) {
	zr, err := zip.OpenReader(path)
	if err != nil {
		return nil, sources.FileScan{}, err
	}
	defer zr.Close()
	for _, want := range names {
		for _, f := range zr.File {
			if filepath.Base(f.Name) != want {
				continue
			}
			rc, err := f.Open()
			if err != nil {
				return nil, sources.FileScan{}, err
			}
			b, readErr := io.ReadAll(io.LimitReader(rc, 512*1024*1024))
			closeErr := rc.Close()
			if readErr != nil {
				return nil, sources.FileScan{}, readErr
			}
			if closeErr != nil {
				return nil, sources.FileScan{}, closeErr
			}
			scan := sources.FileScan{
				Path:  path + "!/" + f.Name,
				Size:  info.Size(),
				MTime: info.ModTime().UTC().Format(time.RFC3339Nano),
			}
			return b, scan, nil
		}
	}
	return nil, sources.FileScan{}, fmt.Errorf("no supported export file found in %s", path)
}

func mapsFromAny(v any) []map[string]any {
	switch t := v.(type) {
	case []map[string]any:
		return t
	case []any:
		out := make([]map[string]any, 0, len(t))
		for _, item := range t {
			if m, ok := item.(map[string]any); ok {
				out = append(out, m)
			}
		}
		return out
	default:
		return nil
	}
}

func firstAny(m map[string]any, keys ...string) any {
	for _, key := range keys {
		if v, ok := m[key]; ok {
			return v
		}
	}
	return nil
}

func firstString(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if v, ok := m[key]; ok {
			switch t := v.(type) {
			case string:
				if strings.TrimSpace(t) != "" {
					return t
				}
			case float64:
				return strconv.FormatFloat(t, 'f', -1, 64)
			}
		}
	}
	return ""
}

func firstText(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if text := sources.TextFromAny(m[key], 8000); text != "" {
			return text
		}
	}
	return ""
}

func timestampFromAny(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case string:
		if f, err := strconv.ParseFloat(t, 64); err == nil {
			return f
		}
		if parsed, err := time.Parse(time.RFC3339Nano, t); err == nil {
			return float64(parsed.UnixNano()) / float64(time.Second)
		}
		if parsed, err := time.Parse(time.RFC3339, t); err == nil {
			return float64(parsed.UnixNano()) / float64(time.Second)
		}
	default:
		return 0
	}
	return 0
}

func unixTimeString(v any) string {
	ts := timestampFromAny(v)
	if ts == 0 {
		return ""
	}
	sec, frac := mathModf(ts)
	return time.Unix(int64(sec), int64(frac*1e9)).UTC().Format(time.RFC3339Nano)
}

func mathModf(v float64) (float64, float64) {
	whole := float64(int64(v))
	return whole, v - whole
}
