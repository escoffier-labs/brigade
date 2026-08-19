package app

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/provenance"
)

// Attack needles are unique, obviously fake prompt-injection text. The
// mutation tests below fail on the pre-fix surfaces: search snippets, MCP
// search_evidence, evidence bundles, Markdown export, and default show.

const (
	quarantineNeedle = "UNIQUE_Q1007_IGNORE_ALL_PREVIOUS_INSTRUCTIONS quarantine pending body"
	failOpenNeedle   = "UNIQUE_Q1009_IGNORE_ALL_PREVIOUS_INSTRUCTIONS fail-open body"
)

func TestMutationQuarantinedPendingHiddenOnEveryBodySurface(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	id := insertCleanIntegrityItem(t, quarantineNeedle, "quarantined", "pending")

	search := runJSON(t, "search", "UNIQUE_Q1007_IGNORE_ALL_PREVIOUS_INSTRUCTIONS", "--json")
	results := search["results"].([]any)
	if len(results) != 1 {
		t.Fatalf("search must still return metadata: %#v", search)
	}
	hit := results[0].(map[string]any)
	if hit["id"] != id {
		t.Fatalf("search id = %#v", hit)
	}
	if snippet, _ := hit["snippet"].(string); snippet != "" {
		t.Fatalf("search snippet leaked quarantined/pending body: %q", snippet)
	}
	if hit["trust_label"] != "quarantined" {
		t.Fatalf("search dropped trust metadata: %#v", hit)
	}

	mcp, err := mcpSearch(map[string]any{"query": "UNIQUE_Q1007_IGNORE_ALL_PREVIOUS_INSTRUCTIONS"})
	if err != nil {
		t.Fatal(err)
	}
	payload := mcpTextPayload(t, mcp)
	mcpResults := payload["results"].([]any)
	if len(mcpResults) != 1 {
		t.Fatalf("mcp search_evidence results = %#v", payload)
	}
	mcpHit := mcpResults[0].(map[string]any)
	if snippet, _ := mcpHit["snippet"].(string); snippet != "" {
		t.Fatalf("mcp search_evidence leaked quarantined/pending snippet: %q", snippet)
	}

	bundle := runJSON(t, "evidence", "UNIQUE_Q1007_IGNORE_ALL_PREVIOUS_INSTRUCTIONS", "--json")
	bundleItem := bundle["results"].([]any)[0].(map[string]any)
	if snippet, _ := bundleItem["snippet"].(string); snippet != "" {
		t.Fatalf("bundle leaked quarantined/pending snippet: %q", snippet)
	}
	code, markdown, errb := run("evidence", "UNIQUE_Q1007_IGNORE_ALL_PREVIOUS_INSTRUCTIONS", "--markdown")
	if code != 0 {
		t.Fatalf("markdown evidence failed: %s %s", errb, markdown)
	}
	if strings.Contains(markdown, quarantineNeedle) {
		t.Fatalf("bundle markdown leaked quarantined/pending body: %s", markdown)
	}

	outDir := t.TempDir()
	runJSON(t, "export", "markdown", "--out", outDir)
	exported := readExportTree(t, outDir)
	if strings.Contains(exported, quarantineNeedle) {
		t.Fatalf("markdown export leaked quarantined/pending body:\n%s", exported)
	}
	if !strings.Contains(exported, id) {
		t.Fatalf("markdown export dropped quarantined item metadata:\n%s", exported)
	}

	hidden := runJSON(t, "show", id, "--json")
	if _, ok := hidden["text"]; ok {
		t.Fatalf("default show leaked quarantined/pending body: %#v", hidden)
	}
	if hidden["untrusted_body_omitted"] != true {
		t.Fatalf("default show missing untrusted_body_omitted: %#v", hidden)
	}
	assertItemNotDeleted(t, id)
}

func TestMutationFailOpenEnvelopeAndDigestDoNotAuthorizeBody(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")

	cases := []struct {
		name  string
		setup func(t *testing.T, id string)
	}{
		{"whitespace pending", func(t *testing.T, id string) { setItemInjectionStatus(t, id, " pending ") }},
		{"uppercase CLEAN", func(t *testing.T, id string) { setItemInjectionStatus(t, id, "CLEAN") }},
		{"object status", func(t *testing.T, id string) {
			patchItemProvenance(t, id, func(env map[string]any) {
				trust := env["trust"].(map[string]any)
				trust["injection"].(map[string]any)["status"] = map[string]any{"state": "clean"}
			})
		}},
		{"malformed origin", func(t *testing.T, id string) {
			patchItemProvenance(t, id, func(env map[string]any) {
				env["origin"] = "not-a-real-origin"
			})
		}},
		{"uppercase matching digest", func(t *testing.T, id string) { uppercaseItemContentHash(t, id) }},
	}

	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			text := failOpenNeedle + " " + tc.name
			id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
			tc.setup(t, id)

			hidden := runJSON(t, "show", id, "--json")
			if body, ok := hidden["text"]; ok {
				t.Fatalf("default show authorized body after %s: %#v", tc.name, body)
			}
			if hidden["untrusted_body_omitted"] != true && hidden["integrity_body_omitted"] != true {
				t.Fatalf("show missing omission flag after %s: %#v", tc.name, hidden)
			}

			needle := "UNIQUE_Q1009_IGNORE_ALL_PREVIOUS_INSTRUCTIONS"
			if i == 0 {
				// First case also proves search/bundle/export hide the fail-open body.
				search := runJSON(t, "search", needle, "--json")
				for _, raw := range search["results"].([]any) {
					hit := raw.(map[string]any)
					if hit["id"] != id {
						continue
					}
					if snippet, _ := hit["snippet"].(string); snippet != "" {
						t.Fatalf("search snippet leaked after %s: %q", tc.name, snippet)
					}
				}
			}
			assertItemNotDeleted(t, id)
		})
	}
}

func TestContentEligibleRequiresValidatedClean(t *testing.T) {
	clean := integrityView{TrustLabel: "untrusted", InjectionStatus: "clean"}
	if !contentEligible(clean) {
		t.Fatal("untrusted+clean must be eligible")
	}
	for _, view := range []integrityView{
		{TrustLabel: "untrusted", InjectionStatus: "pending"},
		{TrustLabel: "untrusted", InjectionStatus: " flagged "},
		{TrustLabel: "quarantined", InjectionStatus: "clean"},
		{TrustLabel: "unknown", InjectionStatus: "clean"},
		{TrustLabel: "untrusted", InjectionStatus: "clean", ParseError: true},
		{TrustLabel: "untrusted", InjectionStatus: "clean", IntegrityMismatch: true},
		{TrustLabel: "untrusted", InjectionStatus: "clean", LegacyUnknown: true},
		{TrustLabel: "reviewed", InjectionStatus: ""},
	} {
		if contentEligible(view) {
			t.Fatalf("ineligible view authorized: %#v", view)
		}
	}
}

func TestInspectDigestDoesNotNormalizeInvalidRepresentations(t *testing.T) {
	lower := provenance.ContentSHA256("canonical")
	got := inspectDigest(map[string]any{"content": lower}, "content")
	if !got.present || !got.valid || got.raw != lower {
		t.Fatalf("canonical digest = %#v", got)
	}
	upper := inspectDigest(map[string]any{"content": strings.ToUpper(lower)}, "content")
	if !upper.present || upper.valid {
		t.Fatalf("uppercase digest must be present and invalid: %#v", upper)
	}
	if inspectDigest(map[string]any{"content": map[string]any{"hex": lower}}, "content").valid {
		t.Fatal("object-valued digest must not authorize")
	}
	if inspectDigest(map[string]any{"content": " " + lower}, "content").valid {
		t.Fatal("whitespace-padded digest must not authorize")
	}
	if inspectDigest(map[string]any{}, "content").present {
		t.Fatal("missing digest should be absent")
	}
}

func TestResolveReadEnvelopeParseErrorIsBlocking(t *testing.T) {
	view := resolveReadEnvelope(map[string]any{
		"provenance": map[string]any{"schema": "not-an-envelope"},
	})
	if !view.ParseError {
		t.Fatalf("expected parse error, got %#v", view)
	}
	if view.InjectionStatus != "" {
		t.Fatalf("parse error must not expose raw injection status: %#v", view)
	}
	if contentEligible(view) {
		t.Fatal("parse error must not be content-eligible")
	}
}

func patchItemProvenance(t *testing.T, itemID string, patch func(env map[string]any)) {
	t.Helper()
	db := openTestDB(t)
	defer db.Close()
	var metadataJSON string
	if err := db.QueryRow(`select metadata_json from items where id = ?`, itemID).Scan(&metadataJSON); err != nil {
		t.Fatal(err)
	}
	meta := map[string]any{}
	if err := json.Unmarshal([]byte(metadataJSON), &meta); err != nil {
		t.Fatal(err)
	}
	env, ok := meta["provenance"].(map[string]any)
	if !ok {
		t.Fatal("missing provenance")
	}
	patch(env)
	updated, err := json.Marshal(meta)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`update items set metadata_json = ? where id = ?`, string(updated), itemID); err != nil {
		t.Fatal(err)
	}
}

func TestMutationUppercaseMatchingDigestIsNotAuthorization(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	text := "UNIQUE_Q1009_UPPERCASE_MATCHING_DIGEST body stays hidden"
	id := insertCleanIntegrityItem(t, text, "untrusted", "clean")
	uppercaseItemContentHash(t, id)

	hidden := runJSON(t, "show", id, "--json")
	if hidden["integrity_mismatch"] != true {
		t.Fatalf("uppercase matching digest skipped verification: %#v", hidden)
	}
	if _, ok := hidden["text"]; ok {
		t.Fatalf("uppercase matching digest authorized body: %#v", hidden)
	}

	search := runJSON(t, "search", "UNIQUE_Q1009_UPPERCASE_MATCHING_DIGEST", "--json")
	hit := search["results"].([]any)[0].(map[string]any)
	if snippet, _ := hit["snippet"].(string); snippet != "" {
		t.Fatalf("uppercase matching digest leaked snippet: %q", snippet)
	}
	assertItemNotDeleted(t, id)
}

func TestExportMarkdownOmitsIneligibleBodiesAndKeepsEligible(t *testing.T) {
	withTempHome(t)
	runOK(t, "init")
	cleanID := insertCleanIntegrityItem(t, "UNIQUE_ELIGIBLE_EXPORT_BODY clean text", "untrusted", "clean")
	pendingID := insertCleanIntegrityItem(t, "UNIQUE_INELIGIBLE_EXPORT_BODY pending text", "quarantined", "pending")

	outDir := filepath.Join(t.TempDir(), "export")
	if err := os.MkdirAll(outDir, 0o700); err != nil {
		t.Fatal(err)
	}
	runJSON(t, "export", "markdown", "--out", outDir)
	exported := readExportTree(t, outDir)
	if !strings.Contains(exported, "UNIQUE_ELIGIBLE_EXPORT_BODY clean text") {
		t.Fatalf("export dropped validated-clean body:\n%s", exported)
	}
	if strings.Contains(exported, "UNIQUE_INELIGIBLE_EXPORT_BODY pending text") {
		t.Fatalf("export leaked pending body:\n%s", exported)
	}
	if !strings.Contains(exported, cleanID) || !strings.Contains(exported, pendingID) {
		t.Fatalf("export dropped item metadata:\n%s", exported)
	}
}
