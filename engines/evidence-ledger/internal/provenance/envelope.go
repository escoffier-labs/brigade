// Package provenance implements the brigade.provenance-envelope.v1 schema:
// a versioned source/origin/trust record plus an exact-byte SHA-256 content
// digest stamped on every evidence item and inter-seat message.
//
// Standard library only. This is the Slice 1 shared schema layer; ingestion,
// consumers, and CLI enforcement land in later slices. See
// docs/proposals/provenance-envelope.md.
package provenance

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

const (
	Schema             = "brigade.provenance-envelope.v1"
	SchemaVersion      = 1
	TrustPolicySchema  = "brigade.trust-policy.v1"
	TrustPolicyVersion = 1
	LegacyDisplay      = "UNKNOWN PROVENANCE - legacy item"
	HashAlgorithm      = "sha256"
	RawScope           = "exact_bytes"
	MaxCompactBytes    = 4096
)

var (
	origins           = map[string]struct{}{"operator-input": {}, "workspace": {}, "agent-session": {}, "external-service": {}, "external-web": {}, "unknown": {}}
	modalities        = map[string]struct{}{"human-written": {}, "model-generated": {}, "tool-output": {}, "external-web": {}, "mixed": {}, "unknown": {}}
	attributions      = map[string]struct{}{"observed": {}, "declared": {}, "inferred": {}}
	trustLabels       = map[string]struct{}{"unknown": {}, "untrusted": {}, "reviewed": {}, "verified": {}, "quarantined": {}}
	injectionStatuses = map[string]struct{}{"clean": {}, "flagged": {}, "pending": {}, "error": {}}
	locatorKinds      = map[string]struct{}{"repo-relative": {}, "uri": {}}
	contentScopes     = map[string]struct{}{"item.text.utf8.v1": {}, "message.text.utf8.v1": {}}
)

// Envelope mirrors the brigade.provenance-envelope.v1 JSON object.
type Envelope struct {
	Schema        string      `json:"schema"`
	SchemaVersion int         `json:"schema_version"`
	Source        Source      `json:"source"`
	Origin        string      `json:"origin"`
	Repository    *Repository `json:"repository"`
	Session       *Session    `json:"session"`
	CollectionID  *string     `json:"collection_id"`
	ItemID        *string     `json:"item_id"`
	Locator       *Locator    `json:"locator"`
	Attribution   string      `json:"attribution"`
	Modality      string      `json:"modality"`
	Trust         Trust       `json:"trust"`
	Hashes        Hashes      `json:"hashes"`
	CapturedAt    *string     `json:"captured_at"`
	IngestedAt    *string     `json:"ingested_at"`
}

// Source is the producer system triple.
type Source struct {
	System   string `json:"system"`
	Kind     string `json:"kind"`
	Producer string `json:"producer"`
}

// Repository identifies the repo origin.
type Repository struct {
	ID       string  `json:"id"`
	Revision *string `json:"revision"`
}

// Session identifies the harness session when known.
type Session struct {
	ID      *string `json:"id"`
	Harness *string `json:"harness"`
}

// Locator is a repo-relative path or non-file URI.
type Locator struct {
	Kind  string `json:"kind"`
	Value string `json:"value"`
}

// Trust carries the label, assignment, policy reference, and injection verdict.
type Trust struct {
	Label       string      `json:"label"`
	AssignedBy  string      `json:"assigned_by"`
	AssignedAt  *string     `json:"assigned_at"`
	TrustPolicy TrustPolicy `json:"trust_policy"`
	Injection   Injection   `json:"injection"`
}

// TrustPolicy references the shared entitlement policy schema and version.
type TrustPolicy struct {
	Schema        string `json:"schema"`
	SchemaVersion int    `json:"schema_version"`
}

// Injection is the injection scan verdict triple.
type Injection struct {
	Status string   `json:"status"`
	Count  int      `json:"count"`
	Rules  []string `json:"rules"`
}

// Hashes carries the content and raw SHA-256 digests and their scopes.
type Hashes struct {
	ContentAlgorithm string  `json:"content_algorithm"`
	ContentScope     string  `json:"content_scope"`
	Content          *string `json:"content"`
	RawAlgorithm     *string `json:"raw_algorithm"`
	RawScope         *string `json:"raw_scope"`
	Raw              *string `json:"raw"`
}

// AuthorityProof is the operator/verifier attestation required for inbound
// adapter envelopes that claim reviewed or verified trust.
type AuthorityProof struct {
	AssignedBy string `json:"assigned_by"`
	Label      string `json:"label"`
}

// ValidationContext carries the optional authority gate inputs for Validate.
type ValidationContext struct {
	InboundAdapter bool
	AuthorityProof *AuthorityProof
}

// SHA256Bytes returns the bare lowercase 64-char hex SHA-256 digest of data.
func SHA256Bytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// ContentSHA256 returns the bare lowercase 64-char hex SHA-256 digest of the
// exact UTF-8 bytes of text.
func ContentSHA256(text string) string {
	return SHA256Bytes([]byte(text))
}

func isLegacy(env Envelope) bool {
	return env.Origin == "unknown" && env.Modality == "unknown" &&
		env.Attribution == "inferred" && env.Trust.Label == "unknown"
}

func validDigest(s string) bool {
	if len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func isAbsoluteLocator(value string) bool {
	if value == "" {
		return false
	}
	if strings.HasPrefix(value, "/") {
		return true
	}
	if len(value) >= 2 && value[1] == ':' && ((value[0] >= 'a' && value[0] <= 'z') || (value[0] >= 'A' && value[0] <= 'Z')) {
		return true
	}
	if strings.HasPrefix(value, `\\`) {
		return true
	}
	if len(value) >= 5 && strings.EqualFold(value[:5], "file:") {
		return true
	}
	return false
}

// Validate returns nil if env is a valid envelope, or an error whose message
// describes the first validation failure. The error message contains the
// offending field name so callers can match on it.
func Validate(env Envelope, ctx ValidationContext) error {
	var errs []string
	legacy := isLegacy(env)

	if env.Schema != Schema {
		errs = append(errs, fmt.Sprintf("schema must be %q", Schema))
	}
	if env.SchemaVersion != SchemaVersion {
		errs = append(errs, fmt.Sprintf("schema_version must be %d", SchemaVersion))
	}

	if env.Source.System == "" {
		errs = append(errs, "source.system must be a non-empty string")
	}
	if env.Source.Kind == "" {
		errs = append(errs, "source.kind must be a non-empty string")
	}
	if env.Source.Producer == "" {
		errs = append(errs, "source.producer must be a non-empty string")
	}

	if _, ok := origins[env.Origin]; !ok {
		errs = append(errs, fmt.Sprintf("origin %q is not in the closed set", env.Origin))
	}
	if _, ok := modalities[env.Modality]; !ok {
		errs = append(errs, fmt.Sprintf("modality %q is not in the closed set", env.Modality))
	}
	if _, ok := attributions[env.Attribution]; !ok {
		errs = append(errs, fmt.Sprintf("attribution %q is not in the closed set", env.Attribution))
	}

	if env.Repository == nil {
		if !legacy {
			errs = append(errs, "repository must be an object")
		}
	} else if env.Repository.ID == "" {
		errs = append(errs, "repository.id must be a non-empty string")
	}

	if env.Session == nil && !legacy {
		errs = append(errs, "session must be an object")
	}

	if env.CollectionID == nil {
		if !legacy {
			errs = append(errs, "collection_id must be a string")
		}
	} else if *env.CollectionID == "" && !legacy {
		errs = append(errs, "collection_id must be a non-empty string")
	}
	if env.ItemID == nil {
		if !legacy {
			errs = append(errs, "item_id must be a string")
		}
	} else if *env.ItemID == "" && !legacy {
		errs = append(errs, "item_id must be a non-empty string")
	}

	if env.Locator == nil {
		if !legacy {
			errs = append(errs, "locator must be an object")
		}
	} else {
		if _, ok := locatorKinds[env.Locator.Kind]; !ok {
			errs = append(errs, fmt.Sprintf("locator.kind %q is not in the closed set", env.Locator.Kind))
		}
		if env.Locator.Value == "" {
			errs = append(errs, "locator.value must be a non-empty string")
		} else if isAbsoluteLocator(env.Locator.Value) ||
			(env.Locator.Kind == "repo-relative" && hasParentSegment(env.Locator.Value)) {
			errs = append(errs, fmt.Sprintf("locator.value %q is unsafe; locator must be repo-relative or a non-file URI", env.Locator.Value))
		}
	}

	if _, ok := trustLabels[env.Trust.Label]; !ok {
		errs = append(errs, fmt.Sprintf("trust.label %q is not in the closed set", env.Trust.Label))
	}
	if env.Trust.AssignedBy == "" {
		errs = append(errs, "trust.assigned_by must be a non-empty string")
	}
	if env.Trust.TrustPolicy.Schema != TrustPolicySchema {
		errs = append(errs, fmt.Sprintf("trust.trust_policy.schema must be %q", TrustPolicySchema))
	}
	if env.Trust.TrustPolicy.SchemaVersion != TrustPolicyVersion {
		errs = append(errs, fmt.Sprintf("trust.trust_policy.schema_version must be %d", TrustPolicyVersion))
	}
	if _, ok := injectionStatuses[env.Trust.Injection.Status]; !ok {
		errs = append(errs, fmt.Sprintf("trust.injection.status %q is not in the closed set", env.Trust.Injection.Status))
	}
	if env.Trust.Injection.Count < 0 {
		errs = append(errs, "trust.injection.count must be a nonnegative integer")
	}
	for _, rule := range env.Trust.Injection.Rules {
		if rule == "" {
			errs = append(errs, "trust.injection.rules entries must be non-empty strings")
			break
		}
	}

	if ctx.InboundAdapter && (env.Trust.Label == "reviewed" || env.Trust.Label == "verified") {
		if ctx.AuthorityProof == nil {
			errs = append(errs, fmt.Sprintf("inbound adapter trust.label %q requires authority_proof with assigned_by and label", env.Trust.Label))
		} else if ctx.AuthorityProof.AssignedBy != env.Trust.AssignedBy {
			errs = append(errs, "authority_proof.assigned_by must match trust.assigned_by")
		} else if ctx.AuthorityProof.Label != env.Trust.Label {
			errs = append(errs, "authority_proof.label must match trust.label")
		}
	}

	if env.Hashes.ContentAlgorithm != HashAlgorithm {
		errs = append(errs, fmt.Sprintf("hashes.content_algorithm must be %q", HashAlgorithm))
	}
	if _, ok := contentScopes[env.Hashes.ContentScope]; !ok {
		errs = append(errs, fmt.Sprintf("hashes.content_scope %q is not in the closed set", env.Hashes.ContentScope))
	}
	if env.Hashes.Content == nil {
		if !legacy {
			errs = append(errs, "hashes.content digest must be a bare lowercase 64-char hex string")
		}
	} else if !validDigest(*env.Hashes.Content) {
		errs = append(errs, "hashes.content digest must be a bare lowercase 64-char hex string")
	}
	if env.Hashes.Raw == nil {
		if env.Hashes.RawAlgorithm != nil {
			errs = append(errs, "hashes.raw_algorithm must be null when hashes.raw is null")
		}
		if env.Hashes.RawScope != nil {
			errs = append(errs, "hashes.raw_scope must be null when hashes.raw is null")
		}
	} else {
		if !validDigest(*env.Hashes.Raw) {
			errs = append(errs, "hashes.raw digest must be a bare lowercase 64-char hex string")
		}
		if env.Hashes.RawAlgorithm == nil || *env.Hashes.RawAlgorithm != HashAlgorithm {
			errs = append(errs, fmt.Sprintf("hashes.raw_algorithm must be %q", HashAlgorithm))
		}
		if env.Hashes.RawScope == nil || *env.Hashes.RawScope != RawScope {
			errs = append(errs, fmt.Sprintf("hashes.raw_scope must be %q", RawScope))
		}
	}

	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}

	var compactBuf bytes.Buffer
	encoder := json.NewEncoder(&compactBuf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(env); err != nil {
		return fmt.Errorf("envelope compact JSON marshal failed: %w", err)
	}
	compact := bytes.TrimSuffix(compactBuf.Bytes(), []byte("\n"))
	if len(compact) > MaxCompactBytes {
		return fmt.Errorf("envelope compact JSON size exceeds %d bytes", MaxCompactBytes)
	}
	return nil
}

func hasParentSegment(value string) bool {
	for _, part := range strings.Split(strings.ReplaceAll(value, `\`, "/"), "/") {
		if part == ".." {
			return true
		}
	}
	return false
}

// SynthesizeLegacyProvenance returns the non-null envelope used on read when an
// item carries no provenance, plus the human-readable legacy display banner.
func SynthesizeLegacyProvenance() (Envelope, string) {
	return Envelope{
		Schema:        Schema,
		SchemaVersion: SchemaVersion,
		Source: Source{
			System:   "legacy",
			Kind:     "legacy",
			Producer: "legacy.read_synthesis",
		},
		Origin:       "unknown",
		Repository:   nil,
		Session:      nil,
		CollectionID: nil,
		ItemID:       nil,
		Locator:      nil,
		Attribution:  "inferred",
		Modality:     "unknown",
		Trust: Trust{
			Label:      "unknown",
			AssignedBy: "ingest:legacy.read_synthesis",
			AssignedAt: nil,
			TrustPolicy: TrustPolicy{
				Schema:        TrustPolicySchema,
				SchemaVersion: TrustPolicyVersion,
			},
			Injection: Injection{
				Status: "clean",
				Count:  0,
				Rules:  []string{},
			},
		},
		Hashes: Hashes{
			ContentAlgorithm: HashAlgorithm,
			ContentScope:     "item.text.utf8.v1",
			Content:          nil,
			RawAlgorithm:     nil,
			RawScope:         nil,
			Raw:              nil,
		},
		CapturedAt: nil,
		IngestedAt: nil,
	}, LegacyDisplay
}
