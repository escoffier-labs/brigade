package app

import (
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/toolpath"
)

// helpCommandFamilies lists every documented command family, including each
// adapter and import harness route and each crawl route.
func helpCommandFamilies() [][]string {
	families := [][]string{}
	for _, cmd := range commandTable {
		families = append(families, []string{cmd.name})
	}
	families = append(families,
		[]string{"sources", "discover"},
		[]string{"scans", "list"}, []string{"scans", "show"}, []string{"scans", "diff"}, []string{"scans", "changed"},
		[]string{"sessions", "list"}, []string{"sessions", "search"},
		[]string{"watch", "once"}, []string{"watch", "daemon"},
		[]string{"schedule", "run"}, []string{"schedule", "daemon"},
		[]string{"evidence", "show"}, []string{"evidence", "list"},
		[]string{"export", "markdown"},
		[]string{"relations", "backfill"},
		[]string{"prune", "imports"}, []string{"prune", "scans"}, []string{"prune", "policy"},
		[]string{"migrate", "codex-arguments"},
		[]string{"doctor", "provenance", "backfill"},
		[]string{"trust", "review"},
		[]string{"import", "adapter"}, []string{"import", "discovered"},
		[]string{"import", "stationtrail"}, []string{"import", "sourceharvest"},
	)
	for _, harness := range []string{"codex", "openclaw", "claude", "pi", "hermes", "opencode", "cursor", "grok"} {
		families = append(families, []string{"adapter", harness}, []string{"import", harness})
	}
	for _, route := range []string{"sessions", "memory", "docs", "files", "repo", "markdown", "html", "gitlog", "json", "jsonl", "adapter", "cursor", "discord", "github", "slack", "granola", "notion", "gmail", "telegram", "chatgpt-export", "claude-export"} {
		families = append(families, []string{"crawl", route})
	}
	families = append(families, []string{"import", "chatgpt-export"}, []string{"import", "claude-export"})
	return families
}

func TestHelpFlagExitsZeroWithUsageForEveryCommandFamily(t *testing.T) {
	withTempHome(t)
	paths := ResolvePaths()

	for _, family := range helpCommandFamilies() {
		for _, flag := range []string{"--help", "-h"} {
			args := append(append([]string{}, family...), flag)
			name := strings.Join(args, " ")
			code, stdout, stderr := run(args...)
			if code != 0 {
				t.Errorf("%s: exit %d, want 0 (stderr %q)", name, code, stderr)
				continue
			}
			if stderr != "" {
				t.Errorf("%s: wrote to stderr: %q", name, stderr)
			}
			want := "usage: miseledger " + family[0]
			if !strings.HasPrefix(stdout, want) {
				t.Errorf("%s: stdout %q does not start with %q", name, stdout, want)
			}
		}
	}

	for _, path := range []string{paths.ConfigPath, paths.DataDir, paths.CacheDir, paths.DBPath} {
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("help created runtime state at %s: %v", path, err)
		}
	}
}

func TestHelpFlagNamesTheSubcommandUsage(t *testing.T) {
	tests := []struct {
		args []string
		want string
	}{
		{[]string{"stats", "--help"}, "miseledger stats [--json]"},
		{[]string{"search", "-h"}, "miseledger search <query>"},
		{[]string{"scans", "show", "--help"}, "miseledger scans show <id-or-path>"},
		{[]string{"prune", "policy", "-h"}, "miseledger prune policy [--policy default|FILE]"},
		{[]string{"import", "codex", "--help"}, "miseledger import codex <path-or-dir>"},
		{[]string{"adapter", "grok", "-h"}, "miseledger adapter grok <path-or-dir> --out <file|->"},
		{[]string{"crawl", "docs", "--help"}, "miseledger crawl docs <path>"},
		{[]string{"doctor", "--help"}, "[--mcp] [--archive]"},
		{[]string{"migrate", "--help"}, "full-table LIKE scan"},
		{[]string{"migrate", "codex-arguments", "-h"}, "full-table LIKE scan"},
		{[]string{"explain", "-h"}, "--code-reference REF"},
	}
	for _, tc := range tests {
		withTempHome(t)
		code, stdout, _ := run(tc.args...)
		if code != 0 || !strings.Contains(stdout, tc.want) {
			t.Errorf("%v: code=%d stdout=%q, want it to contain %q", tc.args, code, stdout, tc.want)
		}
	}
}

func TestHelpFlagAfterDoubleDashIsALiteralArgument(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	code, stdout, stderr := run("search", "--", "--help")
	if code != 0 {
		t.Fatalf("search -- --help exit %d, want a literal search: stderr %q", code, stderr)
	}
	if strings.HasPrefix(stdout, "usage: miseledger") {
		t.Fatalf("search -- --help printed usage instead of searching for the literal term: %q", stdout)
	}

	for _, route := range []string{"memory", "cursor", "sessions"} {
		code, stdout, stderr := run("crawl", route, "--", "--help")
		if strings.Contains(stdout, "usage: miseledger") {
			t.Fatalf("crawl %s -- --help printed usage for a literal argument: %q (code %d, stderr %q)", route, stdout, code, stderr)
		}
	}

	_, _, rest, err := splitFlags([]string{"--json", "--", "--help", "-h", "--"}, nil, map[string]bool{"json": true})
	if err != nil {
		t.Fatal(err)
	}
	if want := []string{"--help", "-h", "--"}; !reflect.DeepEqual(rest, want) {
		t.Fatalf("splitFlags rest after terminator = %#v, want %#v", rest, want)
	}
}

func TestSourceHarvestHintDoesNotPointAtCrawlsThatNeedIt(t *testing.T) {
	for _, bad := range []string{"cover the same paths", "crawl docs/files/repo"} {
		if strings.Contains(toolpath.HintSourceHarvest, bad) {
			t.Fatalf("HintSourceHarvest still suggests sourceharvest-backed crawls (%q): %s", bad, toolpath.HintSourceHarvest)
		}
	}
	if !strings.Contains(toolpath.HintSourceHarvest, "crawl adapter") {
		t.Fatalf("HintSourceHarvest should point at crawl adapter: %s", toolpath.HintSourceHarvest)
	}
}
