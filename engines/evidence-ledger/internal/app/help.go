package app

import (
	"fmt"
	"io"
	"strings"
)

const (
	filterFlags    = "[--source KIND] [--collection ID] [--kind KIND] [--actor-type TYPE] [--project NAME] [--tags LIST] [--from DATE] [--to DATE]"
	nativeHarness  = "codex|openclaw|claude|pi|hermes|opencode|cursor|grok"
	harvestOptions = "<path> [--source KIND] [--collection ID] [--json] [--dry-run] [sourceharvest options]"
)

// helpUsage maps a command path to its usage lines. Resolution is by longest
// matching path, then falls back to the top-level commandTable entry.
var helpUsage = map[string][]string{
	"status":                     {"miseledger status [--json]"},
	"sources":                    {"miseledger sources discover --json"},
	"scans":                      {"miseledger scans list|show|diff|changed"},
	"scans list":                 {"miseledger scans list [--json] [--source KIND]"},
	"scans show":                 {"miseledger scans show <id-or-path> --json"},
	"scans diff":                 {"miseledger scans diff <path> --json"},
	"scans changed":              {"miseledger scans changed [--json] [--source KIND]"},
	"sessions":                   {"miseledger sessions list|search"},
	"sessions list":              {"miseledger sessions list [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]"},
	"sessions search":            {"miseledger sessions search <query> [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]"},
	"serve":                      {"miseledger serve [--addr 127.0.0.1:8765] [--json]"},
	"watch":                      {"miseledger watch once|daemon [--json] [--interval DURATION]"},
	"watch once":                 {"miseledger watch once [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"watch daemon":               {"miseledger watch daemon [--interval DURATION] [--max-runs N] [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"schedule":                   {"miseledger schedule run|daemon <config.toml> [--json] [--interval DURATION] [--max-runs N]"},
	"schedule run":               {"miseledger schedule run <config.toml> [--json]"},
	"schedule daemon":            {"miseledger schedule daemon <config.toml> [--interval DURATION] [--max-runs N] [--json]"},
	"crawl":                      {"miseledger crawl sessions|memory|docs|files|repo|markdown|html|gitlog|json|jsonl|adapter|cursor|discord|github|slack|granola|notion|gmail|telegram|chatgpt-export|claude-export <path> [options]"},
	"crawl sessions":             {"miseledger crawl sessions [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"crawl memory":               {"miseledger crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]"},
	"crawl adapter":              {"miseledger crawl adapter <path> --source <kind> [--json]"},
	"crawl cursor":               {"miseledger crawl cursor [path] [--json] [--dry-run] [--limit N] [--since DATE]"},
	"crawl telegram":             {"miseledger crawl telegram [--chat NAME] [--since RFC3339] [--limit N] [--json] [--dry-run]"},
	"crawl chatgpt-export":       {"miseledger crawl chatgpt-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"crawl claude-export":        {"miseledger crawl claude-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"adapter":                    {"miseledger adapter " + nativeHarness + " <path-or-dir> --out <file|->"},
	"import":                     {"miseledger import adapter|discovered|stationtrail|sourceharvest|" + nativeHarness + "|chatgpt-export|claude-export <path>"},
	"import adapter":             {"miseledger import adapter <path> --source <kind> [--json]"},
	"import discovered":          {"miseledger import discovered [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"import stationtrail":        {"miseledger import stationtrail <source> <path-or-session-id> [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"import sourceharvest":       {"miseledger import sourceharvest <jsonl|markdown|files|html|gitlog|json> <sourceharvest-args> [--json] [--dry-run]"},
	"import chatgpt-export":      {"miseledger import chatgpt-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"import claude-export":       {"miseledger import claude-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"search":                     {"miseledger search <query> [--json] [--limit N] " + filterFlags + " [--code-reference REF] [--origin ORIGIN] [--modality MODALITY] [--trust-label LABEL]"},
	"show":                       {"miseledger show <item-id> [--json] [--include-untrusted-body] [--forensic-content]"},
	"evidence":                   {"miseledger evidence <query>|show <bundle-id>|list [--json] [--markdown] [--include-related] [--include-artifact-text] [--limit N] [--source KIND] [--project NAME] [--from DATE] [--to DATE]"},
	"evidence show":              {"miseledger evidence show <bundle-id> [--json] [--markdown]"},
	"evidence list":              {"miseledger evidence list [--json]"},
	"explain":                    {"miseledger explain <query> [--json] [--limit N] " + filterFlags + " [--code-reference REF]"},
	"export":                     {"miseledger export markdown --out <dir>"},
	"relations":                  {"miseledger relations backfill [--json]"},
	"stats":                      {"miseledger stats [--json]"},
	"fork":                       {"miseledger fork <dest-db-path> [--json]"},
	"diff":                       {"miseledger diff [<base-db>] <fork-db> [--json]"},
	"compact":                    {"miseledger compact [--json]"},
	"prune":                      {"miseledger prune imports|scans|policy"},
	"prune imports":              {"miseledger prune imports --before DATE [--json] [--dry-run]"},
	"prune scans":                {"miseledger prune scans --missing [--json] [--dry-run]"},
	"prune policy":               {"miseledger prune policy [--policy default|FILE] [--json] [--dry-run] [--apply --export PATH]"},
	"sql":                        {"miseledger sql <select> [--json]"},
	"doctor":                     {"miseledger doctor [--json] [--mcp] [--archive]", "miseledger doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"doctor provenance":          {"miseledger doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"trust":                      {"miseledger trust review --item ID --content-hash DIGEST [--to-label reviewed] [--mark-injection-clean] [--operator-command CMD] [--capability JSON] [--json]"},
	"mcp":                        {"miseledger mcp"},
	"crawl github":               {nativeExporters["github"].usage},
	"crawl discord":              {nativeExporters["discord"].usage},
	"crawl slack":                {nativeExporters["slack"].usage},
	"crawl granola":              {nativeExporters["granola"].usage},
	"crawl notion":               {nativeExporters["notion"].usage},
	"crawl gmail":                {nativeExporters["gmail"].usage},
	"doctor provenance backfill": {"miseledger doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"relations backfill":         {"miseledger relations backfill [--json]"},
	"export markdown":            {"miseledger export markdown --out <dir>"},
	"sources discover":           {"miseledger sources discover --json"},
	"trust review":               {"miseledger trust review --item ID --content-hash DIGEST [--to-label reviewed] [--mark-injection-clean] [--operator-command CMD] [--capability JSON] [--json]"},
}

func init() {
	for _, mode := range []string{"docs", "files", "repo", "markdown", "html", "gitlog", "json", "jsonl"} {
		helpUsage["crawl "+mode] = []string{"miseledger crawl " + mode + " " + harvestOptions}
	}
	for _, harness := range strings.Split(nativeHarness, "|") {
		helpUsage["adapter "+harness] = []string{"miseledger adapter " + harness + " <path-or-dir> --out <file|-> [--limit N] [--since DATE] [--redact LIST] [--json]"}
		helpUsage["import "+harness] = []string{"miseledger import " + harness + " <path-or-dir> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"}
	}
}

// wantsHelp reports whether args carry an exact -h or --help before any "--"
// terminator. Only whole-argument matches count, so values such as --query=-h
// and search terms after "--" are left alone.
func wantsHelp(args []string) bool {
	for _, arg := range args {
		if arg == "--" {
			return false
		}
		if arg == "-h" || arg == "--help" {
			return true
		}
	}
	return false
}

// writeCommandHelp prints usage for the longest command path in args. It runs
// before dispatch so help never opens or creates the archive.
func writeCommandHelp(w io.Writer, cmd *commandSpec, args []string) {
	if cmd.name == "migrate" {
		writeMigrateHelp(w)
		return
	}
	path := []string{cmd.name}
	for _, arg := range args {
		if strings.HasPrefix(arg, "-") {
			break
		}
		path = append(path, arg)
	}
	for n := len(path); n > 0; n-- {
		if lines, ok := helpUsage[strings.Join(path[:n], " ")]; ok {
			for i, line := range lines {
				prefix := "       "
				if i == 0 {
					prefix = "usage: "
				}
				fmt.Fprintln(w, prefix+line)
			}
			fmt.Fprintln(w)
			fmt.Fprintln(w, cmd.description)
			return
		}
	}
	fmt.Fprintln(w, "usage: miseledger "+cmd.usage)
	fmt.Fprintln(w)
	fmt.Fprintln(w, cmd.description)
}
