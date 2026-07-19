package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"strconv"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/adapter"
	"github.com/escoffier-labs/miseledger/internal/ingest"
	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/toolpath"
)

type telecrawlMessage struct {
	SourcePK    int64  `json:"source_pk"`
	ChatJID     string `json:"chat_jid"`
	ChatName    string `json:"chat_name"`
	MessageID   string `json:"message_id"`
	SenderJID   string `json:"sender_jid"`
	SenderName  string `json:"sender_name"`
	Timestamp   string `json:"timestamp"`
	FromMe      bool   `json:"from_me"`
	Text        string `json:"text"`
	RawType     int64  `json:"raw_type"`
	MessageType string `json:"message_type"`
	MediaType   string `json:"media_type"`
	MediaTitle  string `json:"media_title"`
	MediaPath   string `json:"media_path"`
	MediaURL    string `json:"media_url"`
	MediaSize   int64  `json:"media_size"`
	Starred     bool   `json:"starred"`
	Snippet     string `json:"snippet"`
}

func cmdCrawlTelecrawl(args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: miseledger crawl telegram [--chat NAME] [--since RFC3339] [--limit N] [--json] [--dry-run]")
		return 0
	}
	values, bools, rest, err := splitFlags(args,
		map[string]bool{"chat": true, "since": true, "limit": true},
		map[string]bool{"json": true, "dry-run": true})
	if err != nil {
		return fatalf(errw, "crawl telegram: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "crawl telegram: unexpected argument %q", rest[0])
	}

	hint := toolpath.HintCrawler("telecrawl")
	if err := toolpath.Require("telecrawl", hint); err != nil {
		return fatalf(errw, "crawl telegram: %s", err)
	}
	teleArgs := []string{"--json", "messages"}
	if value := strings.TrimSpace(values["since"]); value != "" {
		teleArgs = append(teleArgs, "--after", value)
	}
	if value := strings.TrimSpace(values["limit"]); value != "" {
		teleArgs = append(teleArgs, "--limit", value)
	}
	if value := strings.TrimSpace(values["chat"]); value != "" {
		teleArgs = append(teleArgs, "--chat", value)
	}

	messages, err := runTelecrawl(teleArgs)
	if err != nil {
		return fatalf(errw, "crawl telegram: %s", err)
	}
	records := telecrawlRecords(messages)
	if bools["dry-run"] {
		if bools["json"] {
			writeJSON(out, map[string]any{"dry_run": true, "generated_records": len(records), "warnings": []string{}})
		} else {
			fmt.Fprintf(out, "generated=%d warnings=0\n", len(records))
		}
		return 0
	}

	db, _, err := openMigrated()
	if err != nil {
		return fatalf(errw, "crawl telegram: %s", err)
	}
	defer db.Close()
	var jsonl bytes.Buffer
	encoder := json.NewEncoder(&jsonl)
	for _, record := range records {
		if err := encoder.Encode(record); err != nil {
			return fatalf(errw, "crawl telegram: %s", err)
		}
	}
	result, err := ingest.ImportAdapterReader(db, &jsonl, "telecrawl://messages", "telegram")
	if err != nil {
		return fatalf(errw, "crawl telegram: %s", err)
	}
	if bools["json"] {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d warnings=%d already_known=%v source=%s\n", result.Inserted, len(result.Warnings), result.AlreadyKnown, result.SourceKind)
	}
	return 0
}

func runTelecrawl(args []string) ([]telecrawlMessage, error) {
	ctx, cancel := context.WithTimeout(context.Background(), externalScannerTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "telecrawl", args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("telecrawl timed out after %s", externalScannerTimeout)
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return nil, fmt.Errorf("%s", message)
	}
	var messages []telecrawlMessage
	if err := json.Unmarshal(stdout.Bytes(), &messages); err != nil {
		return nil, fmt.Errorf("invalid telecrawl JSON: %w", err)
	}
	return messages, nil
}

func telecrawlRecords(messages []telecrawlMessage) []adapter.Record {
	records := make([]adapter.Record, 0, len(messages))
	for index, message := range messages {
		text := firstNonEmptyString(message.Text, message.MediaTitle, message.MediaType)
		if text == "" {
			continue
		}
		messageID := strings.TrimSpace(message.MessageID)
		if messageID == "" {
			messageID = strconv.FormatInt(message.SourcePK, 10)
		}
		chatID := strings.TrimSpace(message.ChatJID)
		if chatID == "" {
			chatID = "unknown"
		}
		role := "external"
		if message.FromMe {
			role = "user"
		}
		ordinal := int64(index + 1)
		records = append(records, adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "telegram", Name: "Telegram"},
			Collection: adapter.Collection{
				ExternalID: "telegram:chat:" + chatID,
				Kind:       "messages",
				Name:       firstNonEmptyString(message.ChatName, chatID),
				Metadata:   sources.Metadata(map[string]any{"chat_jid": message.ChatJID}),
			},
			Item: adapter.Item{
				ExternalID: "telegram:message:" + messageID,
				Kind:       "message",
				CreatedAt:  message.Timestamp,
				Text:       text,
				Tags:       []string{"telegram"},
				Metadata: sources.Metadata(map[string]any{
					"source_pk": message.SourcePK, "message_id": message.MessageID,
					"sender_jid": message.SenderJID, "sender_name": message.SenderName,
					"from_me": message.FromMe, "raw_type": message.RawType,
					"message_type": message.MessageType, "media_type": message.MediaType,
					"media_title": message.MediaTitle, "media_path": message.MediaPath,
					"media_url": message.MediaURL, "media_size": message.MediaSize,
					"starred": message.Starred, "snippet": message.Snippet,
				}),
			},
			Actor: sources.ActorFromRole("telegram", role, "message"),
			Raw: adapter.RawRef{
				Format: "json", Hash: "sha256:" + sources.HashBytes([]byte(chatID+"\x00"+messageID+"\x00"+text)),
				Path: "telecrawl://messages", Ordinal: &ordinal,
			},
		})
	}
	return records
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}
