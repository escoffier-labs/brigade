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

type gitcrawlThreadsResponse struct {
	Repository string           `json:"repository"`
	Threads    []gitcrawlThread `json:"threads"`
}

type gitcrawlThread struct {
	Number      int64  `json:"number"`
	Kind        string `json:"kind"`
	State       string `json:"state"`
	Title       string `json:"title"`
	Body        string `json:"body"`
	AuthorLogin string `json:"author_login"`
	AuthorType  string `json:"author_type"`
	HTMLURL     string `json:"html_url"`
	LabelsJSON  string `json:"labels_json"`
	Assignees   string `json:"assignees_json"`
	IsDraft     bool   `json:"is_draft"`
	CreatedAt   string `json:"created_at_gh"`
	UpdatedAt   string `json:"updated_at_gh"`
}

func cmdCrawlGitcrawl(args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: "+nativeExporters["github"].usage)
		return 0
	}
	values, bools, rest, err := splitFlags(args,
		map[string]bool{"repo": true, "state": true, "numbers": true, "limit": true},
		map[string]bool{"json": true, "dry-run": true})
	if err != nil {
		return fatalf(errw, "crawl github: %s", err)
	}
	if len(rest) != 0 {
		return fatalf(errw, "crawl github: unexpected argument %q", rest[0])
	}
	repo := strings.TrimSpace(values["repo"])
	if repo == "" {
		return fatalf(errw, "crawl github: --repo is required")
	}
	state := strings.TrimSpace(values["state"])
	if state != "" && state != "open" && state != "closed" && state != "all" {
		return fatalf(errw, "crawl github: --state must be open, closed, or all")
	}
	limit := strings.TrimSpace(values["limit"])
	if limit != "" {
		if _, err := strconv.Atoi(limit); err != nil {
			return fatalf(errw, "crawl github: --limit must be a number")
		}
	}
	numbers := strings.TrimSpace(values["numbers"])

	hint := toolpath.HintCrawler("gitcrawl")
	if err := toolpath.Require("gitcrawl", hint); err != nil {
		return fatalf(errw, "crawl github: %s", err)
	}
	syncArgs := []string{"sync", repo}
	if state != "" {
		syncArgs = append(syncArgs, "--state", state)
	}
	if numbers != "" {
		syncArgs = append(syncArgs, "--numbers", numbers)
	}
	syncArgs = append(syncArgs, "--json")
	if _, err := runGitcrawlRaw(syncArgs); err != nil {
		return fatalf(errw, "crawl github: %s", err)
	}

	threadArgs := []string{"threads", repo, "--include-closed"}
	if numbers != "" {
		threadArgs = append(threadArgs, "--numbers", numbers)
	}
	if limit != "" {
		threadArgs = append(threadArgs, "--limit", limit)
	}
	threadArgs = append(threadArgs, "--json")
	threads, err := runGitcrawlThreads(threadArgs)
	if err != nil {
		return fatalf(errw, "crawl github: %s", err)
	}
	records := gitcrawlRecords(repo, threads)
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
		return fatalf(errw, "crawl github: %s", err)
	}
	defer db.Close()
	var jsonl bytes.Buffer
	encoder := json.NewEncoder(&jsonl)
	for _, record := range records {
		if err := encoder.Encode(record); err != nil {
			return fatalf(errw, "crawl github: %s", err)
		}
	}
	result, err := ingest.ImportAdapterReader(db, &jsonl, "gitcrawl://threads/"+repo, "github")
	if err != nil {
		return fatalf(errw, "crawl github: %s", err)
	}
	if bools["json"] {
		writeJSON(out, result)
	} else {
		fmt.Fprintf(out, "imported=%d warnings=%d already_known=%v source=%s\n", result.Inserted, len(result.Warnings), result.AlreadyKnown, result.SourceKind)
	}
	return 0
}

func runGitcrawlThreads(args []string) ([]gitcrawlThread, error) {
	raw, err := runGitcrawlRaw(args)
	if err != nil {
		return nil, err
	}
	var response gitcrawlThreadsResponse
	if err := json.Unmarshal(raw, &response); err != nil {
		return nil, fmt.Errorf("invalid gitcrawl JSON: %w", err)
	}
	return response.Threads, nil
}

func runGitcrawlRaw(args []string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), externalScannerTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "gitcrawl", args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("gitcrawl timed out after %s", externalScannerTimeout)
		}
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return nil, fmt.Errorf("%s", message)
	}
	return stdout.Bytes(), nil
}

func gitcrawlRecords(repo string, threads []gitcrawlThread) []adapter.Record {
	records := make([]adapter.Record, 0, len(threads))
	for index, thread := range threads {
		number := strconv.FormatInt(thread.Number, 10)
		kind := firstNonEmptyString(thread.Kind, "thread")
		state := firstNonEmptyString(thread.State, "unknown")
		text := strings.TrimSpace(strings.Join(nonEmptyStrings(thread.Title, thread.Body), "\n"))
		ordinal := int64(index + 1)
		metadata := map[string]any{
			"repo": repo, "number": thread.Number, "kind": kind, "state": state,
			"author_login": thread.AuthorLogin, "author_type": thread.AuthorType,
			"is_draft": thread.IsDraft, "labels_json": thread.LabelsJSON,
			"assignees_json": thread.Assignees,
		}
		rec := adapter.Record{
			Schema: adapter.SchemaV1,
			Source: adapter.Source{Kind: "github", Name: "GitHub via gitcrawl"},
			Collection: adapter.Collection{
				ExternalID: "github:repo:" + repo,
				Kind:       "repository",
				Name:       repo,
				Metadata:   sources.Metadata(map[string]any{"repo": repo}),
			},
			Item: adapter.Item{
				ExternalID: "github:" + repo + ":" + kind + ":" + number,
				Kind:       kind,
				CreatedAt:  thread.CreatedAt,
				UpdatedAt:  thread.UpdatedAt,
				Text:       text,
				Tags:       []string{"github", kind, state},
				Metadata:   sources.Metadata(metadata),
			},
			Actor: &adapter.Actor{
				ExternalID: "github:user:" + firstNonEmptyString(thread.AuthorLogin, "unknown"),
				Type:       "external",
				Name:       thread.AuthorLogin,
				Metadata:   sources.Metadata(map[string]any{"author_type": thread.AuthorType}),
			},
			Links: []adapter.Link{{URL: thread.HTMLURL, Text: "GitHub thread"}},
			Raw: adapter.RawRef{
				Format: "json", Hash: "sha256:" + sources.HashBytes([]byte(repo+"\x00"+kind+"\x00"+number+"\x00"+text)),
				Path: "gitcrawl://threads/" + repo, Ordinal: &ordinal,
			},
		}
		records = append(records, rec)
	}
	return records
}

func nonEmptyStrings(values ...string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			out = append(out, value)
		}
	}
	return out
}
