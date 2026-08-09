package provenance_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/escoffier-labs/miseledger/internal/provenance"
)

func fixturesDir(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	return filepath.Join(wd, "..", "..", "..", "..", "src", "brigade", "fixtures")
}

// strPtr returns a pointer to s, used to set nullable envelope string fields
// in tests that mutate fixture-backed envelopes.
func strPtr(s string) *string { return &s }

type goldenCase struct {
	Name     string          `json:"name"`
	Scope    string          `json:"scope"`
	Text     string          `json:"text"`
	Envelope json.RawMessage `json:"envelope"`
}

func loadGoldenCases(t *testing.T) []goldenCase {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(fixturesDir(t), "provenance-envelope.v1.golden.json"))
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var doc struct {
		Schema string       `json:"schema"`
		Cases  []goldenCase `json:"cases"`
	}
	if err := json.Unmarshal(b, &doc); err != nil {
		t.Fatalf("unmarshal golden: %v", err)
	}
	if len(doc.Cases) != 2 {
		t.Fatalf("expected 2 golden cases, got %d", len(doc.Cases))
	}
	return doc.Cases
}

func validEnvelope(t *testing.T) provenance.Envelope {
	t.Helper()
	for _, c := range loadGoldenCases(t) {
		if c.Name != "item_unicode_trailing_newline" {
			continue
		}
		var env provenance.Envelope
		if err := json.Unmarshal(c.Envelope, &env); err != nil {
			t.Fatalf("unmarshal item envelope: %v", err)
		}
		return env
	}
	t.Fatal("item golden case not found")
	return provenance.Envelope{}
}

func TestContentSHA256ParityWithGoldenFixtures(t *testing.T) {
	for _, c := range loadGoldenCases(t) {
		var env provenance.Envelope
		if err := json.Unmarshal(c.Envelope, &env); err != nil {
			t.Fatalf("unmarshal %s: %v", c.Name, err)
		}
		if got := provenance.ContentSHA256(c.Text); got != *env.Hashes.Content {
			t.Errorf("%s: ContentSHA256=%q want %q", c.Name, got, *env.Hashes.Content)
		}
		if got := provenance.SHA256Bytes([]byte(c.Text)); got != *env.Hashes.Content {
			t.Errorf("%s: SHA256Bytes=%q want %q", c.Name, got, *env.Hashes.Content)
		}
		if len(*env.Hashes.Content) != 64 || strings.ToLower(*env.Hashes.Content) != *env.Hashes.Content {
			t.Errorf("%s: digest not bare lowercase 64: %q", c.Name, *env.Hashes.Content)
		}
	}
}

func TestMessageScopeDistinctBytesFromItem(t *testing.T) {
	digests := map[string]string{}
	for _, c := range loadGoldenCases(t) {
		var env provenance.Envelope
		if err := json.Unmarshal(c.Envelope, &env); err != nil {
			t.Fatalf("unmarshal %s: %v", c.Name, err)
		}
		digests[c.Name] = *env.Hashes.Content
	}
	if digests["item_unicode_trailing_newline"] == digests["message_distinct_bytes"] {
		t.Fatal("item and message content digests must differ")
	}
}

func TestEnvelopeFixturesUnmarshalIntoTypedStructs(t *testing.T) {
	for _, c := range loadGoldenCases(t) {
		var env provenance.Envelope
		if err := json.Unmarshal(c.Envelope, &env); err != nil {
			t.Fatalf("unmarshal %s: %v", c.Name, err)
		}
		if env.Schema != "brigade.provenance-envelope.v1" {
			t.Errorf("%s: schema=%q", c.Name, env.Schema)
		}
		if env.SchemaVersion != 1 {
			t.Errorf("%s: schema_version=%d", c.Name, env.SchemaVersion)
		}
		if env.Hashes.ContentScope != c.Scope {
			t.Errorf("%s: content_scope=%q want %q", c.Name, env.Hashes.ContentScope, c.Scope)
		}
		if env.Trust.TrustPolicy.Schema != "brigade.trust-policy.v1" || env.Trust.TrustPolicy.SchemaVersion != 1 {
			t.Errorf("%s: trust_policy mismatch: %+v", c.Name, env.Trust.TrustPolicy)
		}
	}
}

func TestValidateAcceptsGoldenEnvelopes(t *testing.T) {
	for _, c := range loadGoldenCases(t) {
		var env provenance.Envelope
		if err := json.Unmarshal(c.Envelope, &env); err != nil {
			t.Fatalf("unmarshal %s: %v", c.Name, err)
		}
		if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
			t.Errorf("%s: validate: %v", c.Name, err)
		}
	}
}

func TestValidateRejectsInvalidClosedSetValues(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*provenance.Envelope)
		want   string
	}{
		{"origin underscore", func(e *provenance.Envelope) { e.Origin = "operator_input" }, "origin"},
		{"origin wrong case", func(e *provenance.Envelope) { e.Origin = "AGENT-SESSION" }, "origin"},
		{"modality underscore", func(e *provenance.Envelope) { e.Modality = "model_generated" }, "modality"},
		{"attribution bogus", func(e *provenance.Envelope) { e.Attribution = "guess" }, "attribution"},
		{"trust label trusted", func(e *provenance.Envelope) { e.Trust.Label = "trusted" }, "label"},
		{"injection status ok", func(e *provenance.Envelope) { e.Trust.Injection.Status = "ok" }, "injection"},
		{"content scope v2", func(e *provenance.Envelope) { e.Hashes.ContentScope = "item.text.utf8.v2" }, "scope"},
		{"locator kind absolute", func(e *provenance.Envelope) { e.Locator.Kind = "absolute" }, "locator"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnvelope(t)
			tt.mutate(&env)
			err := provenance.Validate(env, provenance.ValidationContext{})
			if err == nil {
				t.Fatalf("expected error for %s", tt.name)
			}
			if !strings.Contains(err.Error(), tt.want) && !strings.Contains(err.Error(), "closed") && !strings.Contains(err.Error(), "enum") {
				t.Fatalf("error %q does not mention %q", err.Error(), tt.want)
			}
		})
	}
}

func TestValidateRejectsBadDigestForms(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*provenance.Envelope)
	}{
		{"uppercase", func(e *provenance.Envelope) { e.Hashes.Content = strPtr(strings.ToUpper(*e.Hashes.Content)) }},
		{"too short", func(e *provenance.Envelope) { e.Hashes.Content = strPtr("abc") }},
		{"non hex", func(e *provenance.Envelope) { e.Hashes.Content = strPtr(strings.Repeat("z", 64)) }},
		{"sha256 prefix", func(e *provenance.Envelope) { e.Hashes.Content = strPtr("sha256:" + strings.Repeat("a", 64)) }},
		{"empty", func(e *provenance.Envelope) { e.Hashes.Content = strPtr("") }},
		{"raw uppercase", func(e *provenance.Envelope) { e.Hashes.Raw = strPtr(strings.ToUpper(*e.Hashes.Raw)) }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnvelope(t)
			tt.mutate(&env)
			err := provenance.Validate(env, provenance.ValidationContext{})
			if err == nil {
				t.Fatalf("expected error for %s", tt.name)
			}
			if !strings.Contains(err.Error(), "digest") && !strings.Contains(err.Error(), "hash") {
				t.Fatalf("error %q does not mention digest/hash", err.Error())
			}
		})
	}
}

func TestValidateRejectsUnsafeAbsoluteLocators(t *testing.T) {
	tests := []struct {
		name  string
		value string
	}{
		{"posix absolute", "/etc/passwd"},
		{"home absolute", "/home/user/secret"},
		{"windows drive", "C:\\Users\\foo"},
		{"windows unc", "\\\\host\\share\\file"},
		{"file URI", "file:///home/user/secret"},
		{"parent traversal", "../secret"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnvelope(t)
			env.Locator.Value = tt.value
			err := provenance.Validate(env, provenance.ValidationContext{})
			if err == nil {
				t.Fatalf("expected error for %s", tt.name)
			}
			if !strings.Contains(err.Error(), "absolute") && !strings.Contains(err.Error(), "locator") {
				t.Fatalf("error %q does not mention absolute/locator", err.Error())
			}
		})
	}
}

func TestValidateAuthorityForInboundAdapter(t *testing.T) {
	tests := []struct {
		name      string
		inbound   bool
		label     string
		proof     *provenance.AuthorityProof
		wantValid bool
	}{
		{"inbound reviewed no proof", true, "reviewed", nil, false},
		{"inbound reviewed proof match", true, "reviewed", &provenance.AuthorityProof{AssignedBy: "verifier:demo", Label: "reviewed"}, true},
		{"inbound reviewed assigned_by mismatch", true, "reviewed", &provenance.AuthorityProof{AssignedBy: "verifier:other", Label: "reviewed"}, false},
		{"inbound reviewed label mismatch", true, "reviewed", &provenance.AuthorityProof{AssignedBy: "verifier:demo", Label: "verified"}, false},
		{"inbound verified proof match", true, "verified", &provenance.AuthorityProof{AssignedBy: "verifier:demo", Label: "verified"}, true},
		{"inbound untrusted no proof", true, "untrusted", nil, true},
		{"not inbound reviewed no proof", false, "reviewed", nil, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnvelope(t)
			env.Trust.Label = tt.label
			env.Trust.AssignedBy = "verifier:demo"
			err := provenance.Validate(env, provenance.ValidationContext{InboundAdapter: tt.inbound, AuthorityProof: tt.proof})
			if (err == nil) != tt.wantValid {
				t.Fatalf("%s: wantValid=%v got err=%v", tt.name, tt.wantValid, err)
			}
		})
	}
}

func TestLegacyDisplayConstantAndSynthesis(t *testing.T) {
	if provenance.LegacyDisplay != "UNKNOWN PROVENANCE - legacy item" {
		t.Fatalf("LegacyDisplay=%q", provenance.LegacyDisplay)
	}
	env, display := provenance.SynthesizeLegacyProvenance()
	if display != provenance.LegacyDisplay {
		t.Fatalf("display=%q want provenance.LegacyDisplay=%q", display, provenance.LegacyDisplay)
	}
	if env.Origin != "unknown" || env.Modality != "unknown" || env.Attribution != "inferred" {
		t.Fatalf("legacy fields wrong: %+v", env)
	}
	if env.Trust.Label != "unknown" {
		t.Fatalf("legacy trust label=%q", env.Trust.Label)
	}
	if env.Hashes.Content != nil {
		t.Fatalf("legacy content digest must be null/empty, got %q", *env.Hashes.Content)
	}
	if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
		t.Fatalf("legacy envelope invalid: %v", err)
	}
}

func TestLegacyMarshalUsesNullForNullableFields(t *testing.T) {
	env, _ := provenance.SynthesizeLegacyProvenance()
	data, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal legacy envelope: %v", err)
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		t.Fatalf("unmarshal legacy envelope: %v", err)
	}
	for _, field := range []string{"collection_id", "item_id", "captured_at", "ingested_at"} {
		if payload[field] != nil {
			t.Errorf("%s=%#v, want JSON null", field, payload[field])
		}
	}
	trust := payload["trust"].(map[string]any)
	if trust["assigned_at"] != nil {
		t.Errorf("trust.assigned_at=%#v, want JSON null", trust["assigned_at"])
	}
	hashes := payload["hashes"].(map[string]any)
	for _, field := range []string{"content", "raw_algorithm", "raw_scope", "raw"} {
		if hashes[field] != nil {
			t.Errorf("hashes.%s=%#v, want JSON null", field, hashes[field])
		}
	}
}

func TestValidateSizeUsesNonHTMLEscapedCompactJSON(t *testing.T) {
	env := validEnvelope(t)
	env.Source.Producer = strings.Repeat("&", 2800)

	var canonical bytes.Buffer
	encoder := json.NewEncoder(&canonical)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(env); err != nil {
		t.Fatalf("encode non-HTML-escaped envelope: %v", err)
	}
	compactSize := len(bytes.TrimSuffix(canonical.Bytes(), []byte("\n")))
	if compactSize > provenance.MaxCompactBytes {
		t.Fatalf("test envelope size=%d, want <=%d", compactSize, provenance.MaxCompactBytes)
	}
	if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
		t.Fatalf("validator counted HTML escaping against size ceiling: %v", err)
	}
}

func TestValidateRejectsEnvelopeAbove4096CompactBytes(t *testing.T) {
	env := validEnvelope(t)
	env.ItemID = strPtr(strings.Repeat("x", 5000))
	err := provenance.Validate(env, provenance.ValidationContext{})
	if err == nil {
		t.Fatal("expected size error for >4096 compact bytes")
	}
	if !strings.Contains(err.Error(), "size") && !strings.Contains(err.Error(), "4096") {
		t.Fatalf("error %q does not mention size/4096", err.Error())
	}
}

func TestValidateAcceptsEnvelopeUnder4096CompactBytes(t *testing.T) {
	env := validEnvelope(t)
	if err := provenance.Validate(env, provenance.ValidationContext{}); err != nil {
		t.Fatalf("compact envelope rejected: %v", err)
	}
}

func TestBoundEnvelopeIdentityPreservesShortValues(t *testing.T) {
	const short = "reader:item:1"
	if got := provenance.BoundEnvelopeIdentity(short); got != short {
		t.Fatalf("BoundEnvelopeIdentity(%q) = %q, want unchanged", short, got)
	}
}

func TestBoundEnvelopeIdentityHashSubstitutesLongValues(t *testing.T) {
	long := strings.Repeat("x", provenance.MaxEnvelopeIdentityBytes+1)
	got := provenance.BoundEnvelopeIdentity(long)
	want := "sha256:" + provenance.SHA256Bytes([]byte(long))
	if got != want {
		t.Fatalf("BoundEnvelopeIdentity long value = %q, want %q", got, want)
	}
	if got == long {
		t.Fatal("expected hash substitution for oversized identity")
	}
}

func TestNewEvidenceEnvelopeBoundsLongExternalIDs(t *testing.T) {
	longCollection := strings.Repeat("c", 4000)
	longItem := strings.Repeat("i", 4000)
	longLocator := fmt.Sprintf("miseledger://adapter/%s/%s", longCollection, longItem)
	ingestedAt := "2026-06-03T00:00:00Z"
	env, err := provenance.NewEvidenceEnvelope(provenance.EvidenceInput{
		SourceSystem: "miseledger", SourceKind: "adapter", SourceProducer: "ingest.upsertRecord",
		Origin: "external-service", RepositoryID: "unknown",
		CollectionID: longCollection, ItemID: longItem,
		LocatorKind: "uri", LocatorValue: longLocator,
		Attribution: "observed", Modality: "tool-output",
		TrustLabel: "quarantined", TrustAssignedBy: "ingest:ingest.upsertRecord", TrustAssignedAt: &ingestedAt,
		InjectionStatus: "pending", InjectionRules: []string{},
		Text: "bounded ids still validate", IngestedAt: &ingestedAt,
	})
	if err != nil {
		t.Fatalf("NewEvidenceEnvelope with long external ids: %v", err)
	}
	if len(*env.CollectionID) > provenance.MaxEnvelopeIdentityBytes {
		t.Fatalf("collection_id length = %d, want <= %d", len(*env.CollectionID), provenance.MaxEnvelopeIdentityBytes)
	}
	if len(*env.ItemID) > provenance.MaxEnvelopeIdentityBytes {
		t.Fatalf("item_id length = %d, want <= %d", len(*env.ItemID), provenance.MaxEnvelopeIdentityBytes)
	}
	if len(env.Locator.Value) > provenance.MaxEnvelopeIdentityBytes {
		t.Fatalf("locator.value length = %d, want <= %d", len(env.Locator.Value), provenance.MaxEnvelopeIdentityBytes)
	}
}
