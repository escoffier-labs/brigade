package app

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

// Recorded independently with the frozen v1 domains. Never regenerate for branding.
func TestFrozenIdentifierGoldenVectors(t *testing.T) {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	payload := evidenceBundleAuthPayload{
		ID: "bundle-fixture", GeneratedAt: "2026-01-02T03:04:05Z",
		Filters: map[string]any{"kind": "fixture"}, ItemIDs: []string{"item-a", "item-b"},
	}
	if got := computeEvidenceBundleMAC(key, payload); got != "dfbda5db0172e5dc67f923a46354abee87e9f73fad17917e9f4bcc13fe5d2ef5" {
		t.Fatalf("bundle MAC = %s", got)
	}
	if got := evidenceItemStubID("server-fixture"); got != "fe546651c8a544a5e975ae36" {
		t.Fatalf("stub ID = %s", got)
	}
	group := exportMarkdownGroup{source: "fixture", collectionKind: "notes", collection: "collection-a"}
	sum := sha256.Sum256(markdownExportTupleEncoding(group))
	const want = "bd8ac3bd00749e650826c7d9d51f7b5a1728165b2ac586b98a46b529825ca727"
	if got := hex.EncodeToString(sum[:]); got != want {
		t.Fatalf("export digest = %s", got)
	}
	if got := markdownExportBasename(group); got != "fixture-notes-collection-a-"+want {
		t.Fatalf("export basename = %s", got)
	}
}
