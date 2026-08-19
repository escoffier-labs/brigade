package ingest

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

const (
	capabilityDomain      = "brigade.authority.capability.v1\x00"
	capabilityHandoffKind = "brigade.authority.capability-handoff.v1"
	capabilityVersion     = 1
	requireCapabilityEnv  = "BRIGADE_REQUIRE_TRUST_CAPABILITY"
)

// TrustCapability is the operator-minted token that authorizes one review.
type TrustCapability struct {
	V          int             `json:"v"`
	ItemID     string          `json:"item_id"`
	FromDigest string          `json:"from_digest"`
	Transition TrustTransition `json:"transition"`
	Nonce      string          `json:"nonce"`
	Expiry     int64           `json:"expiry"`
	MintMeta   map[string]any  `json:"mint_meta"`
	MAC        string          `json:"mac"`
}

// TrustTransition is the exact label and injection change the capability authorizes.
type TrustTransition struct {
	ToLabel            string `json:"to_label"`
	MarkInjectionClean bool   `json:"mark_injection_clean"`
}

// CapabilityHandoff is the stdin document the Python parent writes.
type CapabilityHandoff struct {
	V          int              `json:"v"`
	Kind       string           `json:"kind"`
	Secret     string           `json:"secret"`
	Capability *TrustCapability `json:"capability"`
}

func canonicalJSON(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

func capabilityMACInput(cap *TrustCapability) ([]byte, error) {
	// mint_meta is audit-only and is excluded so Python and Go share one
	// canonical encoding (ints stay ints; unmarshal float64 cannot drift).
	body := map[string]any{
		"v":           cap.V,
		"item_id":     cap.ItemID,
		"from_digest": strings.ToLower(cap.FromDigest),
		"transition": map[string]any{
			"mark_injection_clean": cap.Transition.MarkInjectionClean,
			"to_label":             cap.Transition.ToLabel,
		},
		"nonce":  cap.Nonce,
		"expiry": cap.Expiry,
	}
	return canonicalJSON(body)
}

func macCapability(secret []byte, payload []byte) string {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write([]byte(capabilityDomain))
	_, _ = mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}

// ParseCapabilityJSON decodes a capability token from a CLI flag.
func ParseCapabilityJSON(raw string) (*TrustCapability, error) {
	return parseCapabilityJSON(raw)
}

func parseCapabilityJSON(raw string) (*TrustCapability, error) {
	cap := &TrustCapability{}
	if err := json.Unmarshal([]byte(raw), cap); err != nil {
		return nil, fmt.Errorf("trust capability is malformed: %w", err)
	}
	return cap, nil
}

func parseHandoff(raw []byte) (*CapabilityHandoff, error) {
	handoff := &CapabilityHandoff{}
	if err := json.Unmarshal(raw, handoff); err != nil {
		return nil, fmt.Errorf("capability hand-off is malformed: %w", err)
	}
	if handoff.V != 1 || handoff.Kind != capabilityHandoffKind {
		return nil, fmt.Errorf("capability hand-off is not %s", capabilityHandoffKind)
	}
	if handoff.Secret == "" || handoff.Capability == nil {
		return nil, fmt.Errorf("capability hand-off is missing secret or capability")
	}
	secret, err := hex.DecodeString(handoff.Secret)
	if err != nil || len(secret) != 32 {
		return nil, fmt.Errorf("capability hand-off secret must be 32 bytes")
	}
	return handoff, nil
}

// ReadCapabilityHandoff reads the parent-minted stdin document. Empty stdin is not an error.
func ReadCapabilityHandoff(r io.Reader) (*CapabilityHandoff, error) {
	return readHandoff(r)
}

func readHandoff(r io.Reader) (*CapabilityHandoff, error) {
	raw, err := io.ReadAll(io.LimitReader(r, 64*1024))
	if err != nil {
		return nil, fmt.Errorf("capability hand-off read failed: %w", err)
	}
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 {
		return nil, nil
	}
	return parseHandoff(raw)
}

// RequireCapabilityAlways reports whether BRIGADE_REQUIRE_TRUST_CAPABILITY=1 is set.
func RequireCapabilityAlways() bool {
	return requireCapabilityAlways()
}

func requireCapabilityAlways() bool {
	return os.Getenv(requireCapabilityEnv) == "1"
}

func verifyTrustCapability(secret []byte, cap *TrustCapability, itemID, fromDigest, toLabel string, markInjectionClean bool, now time.Time) error {
	if cap == nil {
		return fmt.Errorf("trust capability is required")
	}
	if len(secret) == 0 {
		return fmt.Errorf("trust capability secret is missing")
	}
	if cap.V != capabilityVersion {
		return fmt.Errorf("trust capability version is not 1")
	}
	payload, err := capabilityMACInput(cap)
	if err != nil {
		return fmt.Errorf("trust capability is malformed: %w", err)
	}
	expected := macCapability(secret, payload)
	if !hmac.Equal([]byte(expected), []byte(strings.ToLower(cap.MAC))) {
		return fmt.Errorf("trust capability MAC is invalid")
	}
	if now.Unix() >= cap.Expiry {
		return fmt.Errorf("trust capability has expired")
	}
	if cap.ItemID != itemID {
		return fmt.Errorf("trust capability item_id does not match the requested item")
	}
	if strings.ToLower(cap.FromDigest) != strings.ToLower(fromDigest) {
		return fmt.Errorf("trust capability from_digest does not match the requested digest")
	}
	if cap.Transition.ToLabel != toLabel {
		return fmt.Errorf("trust capability to_label does not match the requested transition")
	}
	if cap.Transition.MarkInjectionClean != markInjectionClean {
		return fmt.Errorf("trust capability mark_injection_clean does not match the requested transition")
	}
	if cap.Nonce == "" || len(cap.Nonce) < 16 {
		return fmt.Errorf("trust capability nonce is malformed")
	}
	return nil
}

// MintTrustCapability is used by tests to produce a valid operator capability.
func MintTrustCapability(secret []byte, itemID, fromDigest, toLabel string, markInjectionClean bool, ttl time.Duration, now time.Time) (*TrustCapability, error) {
	if len(secret) != 32 {
		return nil, fmt.Errorf("capability secret must be 32 bytes")
	}
	if ttl <= 0 {
		ttl = 120 * time.Second
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	nonce := make([]byte, 16)
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s:%s:%d", itemID, fromDigest, now.UnixNano())))
	copy(nonce, sum[:16])
	cap := &TrustCapability{
		V:          capabilityVersion,
		ItemID:     itemID,
		FromDigest: strings.ToLower(fromDigest),
		Transition: TrustTransition{ToLabel: toLabel, MarkInjectionClean: markInjectionClean},
		Nonce:      hex.EncodeToString(nonce),
		Expiry:     now.Unix() + int64(ttl.Seconds()),
		MintMeta:   map[string]any{"pid": os.Getpid(), "time": now.Unix()},
	}
	payload, err := capabilityMACInput(cap)
	if err != nil {
		return nil, err
	}
	cap.MAC = macCapability(secret, payload)
	return cap, nil
}

// EncodeHandoff builds the stdin document the parent writes to the engine.
func EncodeHandoff(secret []byte, cap *TrustCapability) ([]byte, error) {
	return encodeHandoff(secret, cap)
}

func encodeHandoff(secret []byte, cap *TrustCapability) ([]byte, error) {
	payload := map[string]any{
		"v":          1,
		"kind":       capabilityHandoffKind,
		"secret":     hex.EncodeToString(secret),
		"capability": cap,
	}
	return canonicalJSON(payload)
}
