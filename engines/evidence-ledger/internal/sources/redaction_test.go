package sources

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/adapter"
)

func TestRedactTextClasses(t *testing.T) {
	text := "token=abc123 email demo@example.com url https://private.example.com/path host build.internal"
	got := RedactText(text, Options{RedactSecrets: true, RedactEmails: true, RedactURLs: true, RedactHostnames: true})
	for _, forbidden := range []string{"abc123", "demo@example.com", "https://private.example.com/path", "build.internal"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("redaction leaked %q in %q", forbidden, got)
		}
	}
}

func TestApplyRedactionCoversMetadataArtifactsAndLinks(t *testing.T) {
	rec := adapter.Record{
		Collection: adapter.Collection{Metadata: Metadata(map[string]any{"directory": "/home/demo/project"})},
		Item: adapter.Item{
			Text:     "api_key=abc123 contact demo@example.com https://private.example.com/path build.internal",
			Metadata: Metadata(map[string]any{"cwd": "/home/demo/project", "url": "https://private.example.com/path"}),
		},
		Raw: adapter.RawRef{Path: "/home/demo/project/session.json"},
		Artifacts: []adapter.Artifact{{
			Path:     "/home/demo/project/file.txt",
			Text:     "password=abc123",
			URL:      "https://private.example.com/artifact",
			Metadata: Metadata(map[string]any{"workspace_dir": "/home/demo/project"}),
		}},
		Links: []adapter.Link{{
			URL:  "https://private.example.com/link",
			Text: "build.internal",
		}},
	}
	summary := "summary for demo@example.com"
	rec.Collection.Name = "/home/demo/project"
	rec.Item.Summary = &summary
	rec.Item.Tags = []string{"contact:demo@example.com"}
	rec.Actor = &adapter.Actor{Name: "demo@example.com"}
	rec.Relations = []adapter.Relation{{
		Type:     "reply_to",
		Metadata: Metadata(map[string]any{"origin": "https://private.example.com/thread"}),
	}}
	ApplyRedaction(&rec, Options{RedactPaths: true, RedactSecrets: true, RedactEmails: true, RedactURLs: true, RedactHostnames: true})
	encoded, err := json.Marshal(rec)
	if err != nil {
		t.Fatal(err)
	}
	got := string(encoded)
	for _, forbidden := range []string{"/home/demo/project", "abc123", "demo@example.com", "https://private.example.com", "build.internal"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("redaction leaked %q in %q", forbidden, got)
		}
	}
}
