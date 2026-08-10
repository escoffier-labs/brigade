package memory

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/sources"
)

func TestWalkValidMissingMalformedUnknownLargeInjection(t *testing.T) {
	root := copyMemoryFixtureWorkspace(t)
	outcomes, result, err := Walk(root, sources.Options{})
	if err != nil {
		t.Fatal(err)
	}
	byPath := map[string]CardOutcome{}
	for _, o := range outcomes {
		byPath[o.RawPath] = o
	}

	explicit := byPath["memory/cards/valid-explicit.md"]
	if explicit.Record == nil || explicit.IdentitySource != IdentityExplicit {
		t.Fatalf("explicit card = %+v", explicit)
	}
	if explicit.ExternalID != "card-11111111-2222-4333-8444-555555555555" {
		t.Fatalf("explicit id = %q", explicit.ExternalID)
	}
	if len(explicit.Record.Relations) != 1 || explicit.Record.Relations[0].Target == nil {
		t.Fatalf("expected qualified relation, got %+v", explicit.Record.Relations)
	}
	if explicit.Record.Relations[0].Target.Source != "brigade" {
		t.Fatalf("relation target = %+v", explicit.Record.Relations[0].Target)
	}

	pathCard := byPath["memory/cards/valid-path.md"]
	if pathCard.IdentitySource != IdentityPath || !strings.HasPrefix(pathCard.ExternalID, "path:") {
		t.Fatalf("path card = %+v", pathCard)
	}

	missing := byPath["memory/cards/missing-frontmatter.md"]
	if missing.Record == nil || missing.IdentitySource != IdentityPath {
		t.Fatalf("missing frontmatter should still import with path identity: %+v", missing)
	}

	malformed := byPath["memory/cards/malformed.md"]
	if malformed.Outcome != "skipped" || malformed.Record != nil {
		t.Fatalf("malformed = %+v", malformed)
	}

	unknown := byPath["memory/cards/unknown-fields.md"]
	if unknown.Record == nil {
		t.Fatal("unknown-fields missing record")
	}
	meta := string(unknown.Record.Item.Metadata)
	if !strings.Contains(meta, "experimental_flag") || !strings.Contains(meta, "vendor_meta") {
		t.Fatalf("unknown keys not preserved: %s", meta)
	}

	large := byPath["memory/cards/large.md"]
	if large.Record == nil {
		t.Fatal("large card should import")
	}

	injection := byPath["memory/cards/injection-like.md"]
	if injection.Record == nil || !strings.Contains(injection.Record.Item.Text, "IGNORE ALL PREVIOUS INSTRUCTIONS") {
		t.Fatalf("injection text not preserved: %+v", injection)
	}

	if len(result.Warnings) == 0 {
		t.Fatal("expected malformed warning")
	}
}

func TestParseFrontmatterMatchesBrigadeFlatSubset(t *testing.T) {
	meta, has, malformed := parseFrontmatter("---\ntopic: demo\ntags: [a, b]\nflag: true\n---\nbody\n")
	if !has || malformed {
		t.Fatalf("has=%v malformed=%v", has, malformed)
	}
	if meta["topic"] != "demo" {
		t.Fatalf("topic=%v", meta["topic"])
	}
	tags, _ := meta["tags"].([]string)
	if len(tags) != 2 || tags[0] != "a" {
		t.Fatalf("tags=%v", meta["tags"])
	}
	if meta["flag"] != true {
		t.Fatalf("flag=%v", meta["flag"])
	}
	_, has, malformed = parseFrontmatter("---\ntopic: x\n")
	if has || !malformed {
		t.Fatalf("unclosed frontmatter should be malformed")
	}
}

func copyMemoryFixtureWorkspace(t *testing.T) string {
	t.Helper()
	src := filepath.Join("..", "..", "..", "testdata", "adapters", "memory", "cards")
	ws := t.TempDir()
	dst := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(dst, 0o755); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(src)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		body, err := os.ReadFile(filepath.Join(src, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dst, e.Name()), body, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return ws
}
