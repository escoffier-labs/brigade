package app

import (
	"fmt"
	"io"
	"path/filepath"
	"strings"
)

// Entry point identity (docs/phase-engine-rename.md, "Entry point identity").
// The basename of argv[0], with ".exe" removed, selects the identity. Only the
// legacy name selects the legacy identity. Any other name, including a renamed
// copy, is canonical.
const (
	CanonicalProgram = "brigade-evidence"
	// LegacyProgram is accepted through v0.32 and removed in v0.33.0.
	LegacyProgram         = "miseledger"
	legacyRemovalRelease  = "v0.33.0"
	legacyDeprecationNote = "%s: this command name is deprecated and will be removed in %s; run %s instead\n"
)

// program is the identity behind the version banner, usage text, MCP
// serverInfo.name and the doctor MCP check. Main sets it once per process
// before dispatch. In-process callers of Run get the canonical identity.
var program = CanonicalProgram

// ProgramName returns the identity of the running entry point.
func ProgramName() string { return program }

// IdentityFor maps an argv[0] value to its entry point identity.
func IdentityFor(argv0 string) string {
	base := filepath.Base(strings.ReplaceAll(argv0, "\\", "/"))
	if len(base) > 4 && strings.EqualFold(base[len(base)-4:], ".exe") {
		base = base[:len(base)-4]
	}
	if strings.EqualFold(base, LegacyProgram) {
		return LegacyProgram
	}
	return CanonicalProgram
}

// isServerName reports whether name is either identity's MCP serverInfo.name.
func isServerName(name string) bool {
	return name == CanonicalProgram || name == LegacyProgram
}

// Main is the process entry for both executables. It selects the identity
// from argv[0], prints the legacy deprecation line on stderr when needed and
// runs the command. Stdout and the MCP stream never carry the notice.
func Main(argv []string, out, errw io.Writer) int {
	argv0 := ""
	if len(argv) > 0 {
		argv0 = argv[0]
		argv = argv[1:]
	}
	program = IdentityFor(argv0)
	if program == LegacyProgram {
		fmt.Fprintf(errw, legacyDeprecationNote, LegacyProgram, legacyRemovalRelease, CanonicalProgram)
	}
	return Run(argv, out, errw)
}

// usageLine prefixes a usage body with the running program name.
func usageLine(body string) string { return program + " " + body }

const (
	filterFlags    = "[--source KIND] [--collection ID] [--kind KIND] [--actor-type TYPE] [--project NAME] [--tags LIST] [--from DATE] [--to DATE]"
	nativeHarness  = "codex|openclaw|claude|pi|hermes|opencode|cursor|grok"
	harvestOptions = "<path> [--source KIND] [--collection ID] [--json] [--dry-run] [sourceharvest options]"
)

// helpUsage maps a command path to its usage lines. Resolution is by longest
// matching path, then falls back to the top-level commandTable entry.
var helpUsage = map[string][]string{
	"status":                     {"status [--json]"},
	"sources":                    {"sources discover --json"},
	"scans":                      {"scans list|show|diff|changed"},
	"scans list":                 {"scans list [--json] [--source KIND]"},
	"scans show":                 {"scans show <id-or-path> --json"},
	"scans diff":                 {"scans diff <path> --json"},
	"scans changed":              {"scans changed [--json] [--source KIND]"},
	"sessions":                   {"sessions list|search"},
	"sessions list":              {"sessions list [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]"},
	"sessions search":            {"sessions search <query> [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]"},
	"serve":                      {"serve [--addr 127.0.0.1:8765] [--json]"},
	"watch":                      {"watch once|daemon [--json] [--interval DURATION]"},
	"watch once":                 {"watch once [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"watch daemon":               {"watch daemon [--interval DURATION] [--max-runs N] [--if-changed] [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"schedule":                   {"schedule run|daemon <config.toml> [--json] [--interval DURATION] [--max-runs N]"},
	"schedule run":               {"schedule run <config.toml> [--json]"},
	"schedule daemon":            {"schedule daemon <config.toml> [--interval DURATION] [--max-runs N] [--json]"},
	"crawl":                      {"crawl sessions|memory|docs|files|repo|markdown|html|gitlog|json|jsonl|adapter|cursor|discord|github|slack|granola|notion|gmail|telegram|chatgpt-export|claude-export <path> [options]"},
	"crawl sessions":             {"crawl sessions [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"crawl memory":               {"crawl memory <workspace> [--json] [--dry-run] [--rebuild] [--limit N]"},
	"crawl adapter":              {"crawl adapter <path> --source <kind> [--json]"},
	"crawl cursor":               {"crawl cursor [path] [--json] [--dry-run] [--limit N] [--since DATE]"},
	"crawl telegram":             {"crawl telegram [--chat NAME] [--since RFC3339] [--limit N] [--json] [--dry-run]"},
	"crawl chatgpt-export":       {"crawl chatgpt-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"crawl claude-export":        {"crawl claude-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"adapter":                    {"adapter " + nativeHarness + " <path-or-dir> --out <file|->"},
	"import":                     {"import adapter|discovered|stationtrail|sourceharvest|" + nativeHarness + "|chatgpt-export|claude-export <path>"},
	"import adapter":             {"import adapter <path> --source <kind> [--json]"},
	"import discovered":          {"import discovered [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"import stationtrail":        {"import stationtrail <source> <path-or-session-id> [--json] [--dry-run] [--limit N] [--since DATE] [--redact LIST]"},
	"import sourceharvest":       {"import sourceharvest <jsonl|markdown|files|html|gitlog|json> <sourceharvest-args> [--json] [--dry-run]"},
	"import chatgpt-export":      {"import chatgpt-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"import claude-export":       {"import claude-export <path> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"},
	"search":                     {"search <query> [--json] [--limit N] " + filterFlags + " [--code-reference REF] [--origin ORIGIN] [--modality MODALITY] [--trust-label LABEL]"},
	"show":                       {"show <item-id> [--json] [--include-untrusted-body] [--forensic-content]"},
	"evidence":                   {"evidence <query>|show <bundle-id>|list [--json] [--markdown] [--include-related] [--include-artifact-text] [--limit N] [--source KIND] [--project NAME] [--from DATE] [--to DATE]"},
	"evidence show":              {"evidence show <bundle-id> [--json] [--markdown]"},
	"evidence list":              {"evidence list [--json]"},
	"explain":                    {"explain <query> [--json] [--limit N] " + filterFlags + " [--code-reference REF]"},
	"export":                     {"export markdown --out <dir>"},
	"relations":                  {"relations backfill [--json]"},
	"stats":                      {"stats [--json]"},
	"fork":                       {"fork <dest-db-path> [--json]"},
	"diff":                       {"diff [<base-db>] <fork-db> [--json]"},
	"compact":                    {"compact [--json]"},
	"prune":                      {"prune imports|scans|policy"},
	"prune imports":              {"prune imports --before DATE [--json] [--dry-run]"},
	"prune scans":                {"prune scans --missing [--json] [--dry-run]"},
	"prune policy":               {"prune policy [--policy default|FILE] [--json] [--dry-run] [--apply --export PATH]"},
	"sql":                        {"sql <select> [--json]"},
	"doctor":                     {"doctor [--json] [--mcp] [--archive]", "doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"doctor provenance":          {"doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"trust":                      {"trust review --item ID --content-hash DIGEST [--to-label reviewed] [--mark-injection-clean] [--operator-command CMD] [--capability JSON] [--json]"},
	"mcp":                        {"mcp"},
	"crawl github":               {nativeExporters["github"].usage},
	"crawl discord":              {nativeExporters["discord"].usage},
	"crawl slack":                {nativeExporters["slack"].usage},
	"crawl granola":              {nativeExporters["granola"].usage},
	"crawl notion":               {nativeExporters["notion"].usage},
	"crawl gmail":                {nativeExporters["gmail"].usage},
	"doctor provenance backfill": {"doctor provenance backfill [--json] [--batch N] [--after ID]"},
	"relations backfill":         {"relations backfill [--json]"},
	"export markdown":            {"export markdown --out <dir>"},
	"sources discover":           {"sources discover --json"},
	"trust review":               {"trust review --item ID --content-hash DIGEST [--to-label reviewed] [--mark-injection-clean] [--operator-command CMD] [--capability JSON] [--json]"},
}

func init() {
	for _, mode := range []string{"docs", "files", "repo", "markdown", "html", "gitlog", "json", "jsonl"} {
		helpUsage["crawl "+mode] = []string{"crawl " + mode + " " + harvestOptions}
	}
	for _, harness := range strings.Split(nativeHarness, "|") {
		helpUsage["adapter "+harness] = []string{"adapter " + harness + " <path-or-dir> --out <file|-> [--limit N] [--since DATE] [--redact LIST] [--json]"}
		helpUsage["import "+harness] = []string{"import " + harness + " <path-or-dir> [--json] [--dry-run] [--full] [--limit N] [--since DATE] [--redact LIST]"}
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
				fmt.Fprintln(w, prefix+usageLine(line))
			}
			fmt.Fprintln(w)
			fmt.Fprintln(w, cmd.description)
			return
		}
	}
	fmt.Fprintln(w, "usage: "+usageLine(cmd.usage))
	fmt.Fprintln(w)
	fmt.Fprintln(w, cmd.description)
}
