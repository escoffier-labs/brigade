package app

import (
	"fmt"
	"io"
	"path/filepath"
	"strings"

	"github.com/escoffier-labs/miseledger/internal/sources/cursor"
)

func cmdCrawl(args []string, out, errw io.Writer) int {
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger crawl sessions|docs|files|repo|markdown|html|gitlog|json|jsonl|adapter|cursor|discord|slack|granola|notion|gmail|chatgpt-export|claude-export <path> [options]")
	}
	switch args[0] {
	case "sessions":
		return cmdCrawlSessions(args[1:], out, errw)
	case "docs":
		return cmdCrawlSourceHarvest("markdown", "docs", args[1:], out, errw)
	case "files":
		return cmdCrawlSourceHarvest("files", "files", args[1:], out, errw)
	case "repo":
		return cmdCrawlSourceHarvest("gitlog", "gitlog", args[1:], out, errw)
	case "markdown", "html", "gitlog", "json", "jsonl":
		return cmdCrawlSourceHarvest(args[0], args[0], args[1:], out, errw)
	case "adapter":
		return cmdImportAdapter(args[1:], out, errw)
	case "cursor":
		return cmdCrawlCursor(args[1:], out, errw)
	case "discord", "slack", "granola", "notion", "gmail":
		return cmdCrawlExporter(nativeExporters[args[0]], args[1:], out, errw)
	case "chatgpt-export", "claude-export":
		return cmdImport(args, out, errw)
	default:
		return fatalf(errw, "usage: miseledger crawl sessions|docs|files|repo|markdown|html|gitlog|json|jsonl|adapter|cursor|discord|slack|granola|notion|gmail|chatgpt-export|claude-export <path> [options]")
	}
}

func cmdCrawlSessions(args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: miseledger crawl sessions [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]")
		return 0
	}
	return cmdImportDiscovered(args, out, errw)
}

// cmdCrawlCursor imports the local Cursor Agent history. With no path it
// defaults to the standard Cursor config root so the common case is one word.
func cmdCrawlCursor(args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintln(out, "usage: miseledger crawl cursor [path] [--json] [--dry-run] [--limit N] [--since DATE]")
		return 0
	}
	if firstPositional(args) == "" {
		args = append([]string{cursor.DefaultRoot()}, args...)
	}
	return cmdImportNative("cursor", cursor.Generate, args, out, errw)
}

func cmdCrawlSourceHarvest(mode, defaultSource string, args []string, out, errw io.Writer) int {
	if hasBoolFlag(args, "help") || hasBoolFlag(args, "h") {
		fmt.Fprintf(out, "usage: miseledger crawl %s <path> [--source KIND] [--collection ID] [--json] [--dry-run] [sourceharvest options]\n", mode)
		return 0
	}
	if len(args) == 0 {
		return fatalf(errw, "usage: miseledger crawl %s <path> [--source KIND] [--collection ID] [--json] [--dry-run] [sourceharvest options]", mode)
	}
	sourcePath := firstPositional(args)
	if sourcePath == "" {
		return fatalf(errw, "usage: miseledger crawl %s <path> [--source KIND] [--collection ID] [--json] [--dry-run] [sourceharvest options]", mode)
	}
	passArgs := append([]string{mode}, args...)
	passArgs = ensureValueFlag(passArgs, "source", defaultSource)
	passArgs = ensureValueFlag(passArgs, "collection", defaultCollection(defaultSource, sourcePath))
	return cmdImportSourceHarvest(passArgs, out, errw)
}

func firstPositional(args []string) string {
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "--") || arg == "--" {
			return arg
		}
		nameVal := strings.TrimPrefix(arg, "--")
		name := nameVal
		hasInlineValue := false
		if idx := strings.IndexByte(nameVal, '='); idx >= 0 {
			name = nameVal[:idx]
			hasInlineValue = true
		}
		if hasInlineValue || isKnownBoolFlag(name) {
			continue
		}
		i++
	}
	return ""
}

func isKnownBoolFlag(name string) bool {
	switch name {
	case "json", "dry-run", "help", "h":
		return true
	default:
		return false
	}
}

func ensureValueFlag(args []string, name, value string) []string {
	if value == "" || hasFlag(args, name) {
		return args
	}
	return append(args, "--"+name, value)
}

func defaultCollection(sourceKind, sourcePath string) string {
	base := filepath.Base(filepath.Clean(sourcePath))
	if base == "." || base == string(filepath.Separator) {
		base = "local"
	}
	base = strings.TrimSpace(base)
	if base == "" {
		base = "local"
	}
	return sourceKind + ":" + base
}
