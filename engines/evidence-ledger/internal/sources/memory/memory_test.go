package memory

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/escoffier-labs/miseledger/internal/sources"
	"github.com/escoffier-labs/miseledger/internal/textnorm"
)

const fixtureNamespace = "memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"

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
	if explicit.Record.Collection.ExternalID != fixtureNamespace {
		t.Fatalf("collection = %q want namespace %q", explicit.Record.Collection.ExternalID, fixtureNamespace)
	}
	if len(explicit.Record.Relations) != 1 || explicit.Record.Relations[0].Target == nil {
		t.Fatalf("expected qualified relation, got %+v", explicit.Record.Relations)
	}
	if explicit.Record.Relations[0].Target.Source != "brigade" {
		t.Fatalf("relation target = %+v", explicit.Record.Relations[0].Target)
	}
	if explicit.Fingerprint == "" || explicit.Fingerprint == explicit.ExternalID {
		t.Fatalf("fingerprint must be present and not identity: fp=%q id=%q", explicit.Fingerprint, explicit.ExternalID)
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

func TestBuildCardTruncatesUTF8WithinBudget(t *testing.T) {
	marker := "\n[truncated]"
	body := "---\nid: card-utf80000-1111-4222-8333-444444444444\nsummary: " + strings.Repeat("界", MaxTextBytes) + "\n---\n\n" + strings.Repeat("界", MaxTextBytes)
	card, warning := buildCard("workspace", fixtureNamespace, "memory/cards/utf8.md", []byte(body), "2026-01-01T00:00:00Z")
	if warning != "" || card.Record == nil {
		t.Fatalf("card=%+v warning=%q", card, warning)
	}
	if !utf8.ValidString(card.Record.Item.Text) || !utf8.ValidString(*card.Record.Item.Summary) {
		t.Fatal("truncation split a UTF-8 rune")
	}
	if len(card.Record.Item.Text) > MaxTextBytes || !strings.HasSuffix(card.Record.Item.Text, marker) {
		t.Fatalf("text bytes=%d suffix=%v", len(card.Record.Item.Text), strings.HasSuffix(card.Record.Item.Text, marker))
	}
	if len(*card.Record.Item.Summary) > MaxTextBytes || !strings.HasSuffix(*card.Record.Item.Summary, marker) {
		t.Fatalf("summary bytes=%d suffix=%v", len(*card.Record.Item.Summary), strings.HasSuffix(*card.Record.Item.Summary, marker))
	}
	ascii, _ := buildCard("workspace", fixtureNamespace, "memory/cards/ascii.md", []byte("---\nid: card-ascii000-1111-4222-8333-444444444444\n---\n\n"+strings.Repeat("a", MaxTextBytes+1)), "2026-01-01T00:00:00Z")
	if ascii.Record == nil || len(ascii.Record.Item.Text) != MaxTextBytes || !strings.HasSuffix(ascii.Record.Item.Text, marker) {
		t.Fatalf("ascii truncation = %+v", ascii)
	}
}

func TestWalkRequiresOperatorNamespace(t *testing.T) {
	ws := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ws, "memory", "cards"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "memory", "cards", "x.md"), []byte("---\ntopic: x\n---\n\n# X\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, _, err := Walk(ws, sources.Options{})
	if err == nil || !strings.Contains(err.Error(), "memory namespace") {
		t.Fatalf("expected namespace error, got %v", err)
	}
}

func TestResolveNamespaceRejectsNonV4AndInvalidVariant(t *testing.T) {
	cases := []struct {
		name string
		ns   string
	}{
		{"version1", "memory-aaaaaaaa-bbbb-1ccc-8ddd-eeeeeeeeeeee"},
		{"version5", "memory-aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee"},
		{"variant_c", "memory-aaaaaaaa-bbbb-4ccc-cddd-eeeeeeeeeeee"},
		{"variant_0", "memory-aaaaaaaa-bbbb-4ccc-0ddd-eeeeeeeeeeee"},
		{"not_uuid", "memory-not-a-uuid"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ws := t.TempDir()
			writeNamespace(t, ws, tc.ns)
			if err := os.MkdirAll(filepath.Join(ws, "memory", "cards"), 0o755); err != nil {
				t.Fatal(err)
			}
			_, err := ResolveNamespace(ws)
			if err == nil {
				t.Fatalf("expected rejection for %s", tc.ns)
			}
			if !strings.Contains(err.Error(), "invalid memory namespace") {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestResolveNamespaceAcceptsUUIDV4Variants(t *testing.T) {
	for _, ns := range []string{
		"memory-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
		"memory-aaaaaaaa-bbbb-4ccc-9ddd-eeeeeeeeeeee",
		"memory-aaaaaaaa-bbbb-4ccc-addd-eeeeeeeeeeee",
		"memory-aaaaaaaa-bbbb-4ccc-bddd-eeeeeeeeeeee",
	} {
		ws := t.TempDir()
		writeNamespace(t, ws, ns)
		got, err := ResolveNamespace(ws)
		if err != nil || got != ns {
			t.Fatalf("ns=%s got=%s err=%v", ns, got, err)
		}
	}
}

func TestWalkOversizeSkipAndTextTruncation(t *testing.T) {
	ws := t.TempDir()
	writeNamespace(t, ws, fixtureNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	oversize := make([]byte, MaxCardBytes+1)
	for i := range oversize {
		oversize[i] = 'A'
	}
	if err := os.WriteFile(filepath.Join(cards, "oversize.md"), oversize, 0o600); err != nil {
		t.Fatal(err)
	}
	body := strings.Repeat(" truncation-body-word ", (MaxTextBytes/20)+50)
	truncCard := "---\nid: card-trunc000-1111-4222-8333-444444444444\ntopic: trunc\n---\n\n# Trunc\n\n" + body + "\n"
	if err := os.WriteFile(filepath.Join(cards, "trunc.md"), []byte(truncCard), 0o600); err != nil {
		t.Fatal(err)
	}
	outcomes, _, err := Walk(ws, sources.Options{})
	if err != nil {
		t.Fatal(err)
	}
	byPath := map[string]CardOutcome{}
	for _, o := range outcomes {
		byPath[o.RawPath] = o
	}
	if byPath["memory/cards/oversize.md"].Outcome != "skipped" || byPath["memory/cards/oversize.md"].Record != nil {
		t.Fatalf("oversize = %+v", byPath["memory/cards/oversize.md"])
	}
	trunc := byPath["memory/cards/trunc.md"]
	if trunc.Record == nil || !strings.HasSuffix(trunc.Record.Item.Text, "[truncated]") {
		t.Fatalf("truncation missing: %+v", trunc)
	}
	if len(trunc.Record.Item.Text) < MaxTextBytes {
		t.Fatalf("truncated text too short: %d", len(trunc.Record.Item.Text))
	}
}

func TestFingerprintDuplicateDetectionOnly(t *testing.T) {
	ws := t.TempDir()
	writeNamespace(t, ws, fixtureNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	a := "---\nid: card-fp000000-1111-4222-8333-444444444444\ntopic: a\n---\n\nFlush the cache after migrations.\n"
	b := "---\nid: card-fp000000-1111-4222-8333-444444444445\ntopic: b\n---\n\nflush the cache after migrations!\n"
	if err := os.WriteFile(filepath.Join(cards, "a.md"), []byte(a), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "b.md"), []byte(b), 0o600); err != nil {
		t.Fatal(err)
	}
	outcomes, result, err := Walk(ws, sources.Options{})
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 2 {
		t.Fatalf("outcomes=%d", len(outcomes))
	}
	if outcomes[0].Fingerprint != outcomes[1].Fingerprint {
		t.Fatalf("fingerprints differ: %s vs %s", outcomes[0].Fingerprint, outcomes[1].Fingerprint)
	}
	want := textnorm.ContentFingerprint(a)
	if outcomes[0].Fingerprint != want {
		t.Fatalf("fingerprint = %s want %s", outcomes[0].Fingerprint, want)
	}
	if outcomes[0].ExternalID == outcomes[0].Fingerprint {
		t.Fatal("fingerprint must not be card identity")
	}
	joined := strings.Join(result.Warnings, "\n")
	if !strings.Contains(joined, "duplicate content fingerprint") {
		t.Fatalf("expected fingerprint duplicate warning, got %v", result.Warnings)
	}
}

func TestDuplicateExplicitIDsDetected(t *testing.T) {
	ws := t.TempDir()
	writeNamespace(t, ws, fixtureNamespace)
	cards := filepath.Join(ws, "memory", "cards")
	if err := os.MkdirAll(cards, 0o755); err != nil {
		t.Fatal(err)
	}
	body := "---\nid: card-dup00000-1111-4222-8333-444444444444\ntopic: d\n---\n\n# Dup\n"
	if err := os.WriteFile(filepath.Join(cards, "one.md"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cards, "two.md"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	outcomes, _, err := Walk(ws, sources.Options{})
	if err != nil {
		t.Fatal(err)
	}
	dups := DuplicateExplicitIDs(outcomes)
	if len(dups) != 1 {
		t.Fatalf("dups=%v", dups)
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
	writeNamespace(t, ws, fixtureNamespace)
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

func writeNamespace(t *testing.T, ws, ns string) {
	t.Helper()
	dir := filepath.Join(ws, "memory")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "NAMESPACE"), []byte(ns+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
}
