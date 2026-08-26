package app

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/escoffier-labs/miseledger/internal/security"
)

// Cached evidence bundles are re-opened by `evidence show` and regenerated
// under their original bundle ID, so anything that can write the cache can
// otherwise rewrite what a later show rematerializes (#1201). Each saved
// bundle therefore carries an HMAC over its canonical reference — bundle ID,
// item IDs, filters, and generated timestamp — keyed by a random 32-byte key
// stored 0600 in the private data directory. Loading rejects a missing or
// invalid MAC; unknown keys are dropped by the outbound sanitizer so the MAC
// never reaches clients.
//
// Round 2 (#1201): the key file itself is validated on every load (regular
// file, current uid, 0600, exactly 32 bytes) because a same-UID scanner can
// otherwise read or replace it; first creation uses O_CREATE|O_EXCL so
// concurrent initializers converge on one winner instead of sealing bundles
// with competing keys. The residual same-UID exposure is tracked in #1093.
const (
	evidenceBundleMACField   = "bundle_mac"
	evidenceBundleMACDomain  = "miseledger.evidence.bundle-mac.v1\x00"
	evidenceBundleMACKeyFile = "evidence-bundle-mac.key"
)

type evidenceBundleAuthPayload struct {
	ID          string         `json:"id"`
	GeneratedAt string         `json:"generated_at"`
	Filters     map[string]any `json:"filters"`
	ItemIDs     []string       `json:"item_ids"`
}

// EvidenceMACKeyError reports a MAC key file that was refused on load
// (#1201 round 2): wrong type, owner, mode, or size. The residual same-UID
// exposure is tracked in #1093.
type EvidenceMACKeyError struct {
	Reason string
}

func (e *EvidenceMACKeyError) Error() string {
	return fmt.Sprintf("evidence bundle MAC key rejected: %s", e.Reason)
}

func evidenceBundleMACKeyPath() string {
	return filepath.Join(ResolvePaths().DataDir, evidenceBundleMACKeyFile)
}

// loadOrCreateEvidenceBundleMACKey returns the validated key file contents,
// creating it exclusively on first use. Every load re-validates the file
// (regular, current uid, 0600, exactly 32 bytes) and refuses attacker-shaped
// files with a typed error (#1201 round 2). Concurrent first use converges on
// the O_EXCL winner; losers load the winner's key instead of generating a
// competing one.
func loadOrCreateEvidenceBundleMACKey() ([]byte, error) {
	path := evidenceBundleMACKeyPath()
	if err := security.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	key, err := readEvidenceBundleMACKeyFile(path)
	if err == nil {
		return key, nil
	}
	if !errors.Is(err, fs.ErrNotExist) {
		if isTornKeyRead(err) {
			// Another initializer claimed the file moments ago and is still
			// writing it (#1201 round 2): wait out the write instead of
			// failing or generating a competing key.
			return loadEvidenceBundleMACKeyWinner(path)
		}
		return nil, err
	}
	switch key, err := createEvidenceBundleMACKey(path); {
	case err == nil:
		return key, nil
	case errors.Is(err, fs.ErrExist):
		// Lost the create race: seal against the winner's key.
		return loadEvidenceBundleMACKeyWinner(path)
	default:
		return nil, err
	}
}

// loadEvidenceBundleMACKeyWinner loads the create-race winner's key,
// tolerating the brief window in which the winner has claimed the file
// (O_EXCL) but not yet finished writing all 32 bytes (#1201 round 2). Only
// that transient size signature is retried; every other refusal — wrong
// owner, mode, symlink, persistent damage — fails immediately.
func loadEvidenceBundleMACKeyWinner(path string) ([]byte, error) {
	deadline := time.Now().Add(2 * time.Second)
	for {
		key, err := readEvidenceBundleMACKeyFile(path)
		if err == nil {
			return key, nil
		}
		if errors.Is(err, fs.ErrNotExist) || !isTornKeyRead(err) || !time.Now().Before(deadline) {
			return nil, err
		}
		time.Sleep(500 * time.Microsecond)
	}
}

func isTornKeyRead(err error) bool {
	var keyErr *EvidenceMACKeyError
	return errors.As(err, &keyErr) && strings.Contains(keyErr.Reason, "expected exactly 32 bytes")
}

// readEvidenceBundleMACKeyFile opens and validates the key file. A missing
// file surfaces as fs.ErrNotExist so callers can fall through to creation;
// every other refusal is a typed *EvidenceMACKeyError.
func readEvidenceBundleMACKeyFile(path string) ([]byte, error) {
	if linfo, lerr := os.Lstat(path); lerr == nil && linfo.Mode()&fs.ModeSymlink != 0 {
		return nil, &EvidenceMACKeyError{Reason: "refusing to open key file: path is a symlink"}
	}
	f, err := openEvidenceMACKeyFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("refusing to open key file: %v", err)}
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("stat key file: %v", err)}
	}
	if !info.Mode().IsRegular() {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("not a regular file (mode %s)", info.Mode())}
	}
	if err := checkEvidenceMACKeyOwner(info); err != nil {
		return nil, err
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("mode %04o, want 0600", perm)}
	}
	var buf [33]byte // bounded read: 32 key bytes plus 1 to detect oversize
	n, err := io.ReadFull(f, buf[:])
	if n != 32 || err != io.ErrUnexpectedEOF {
		return nil, &EvidenceMACKeyError{Reason: fmt.Sprintf("expected exactly 32 bytes, found %d", n)}
	}
	key := make([]byte, 32)
	copy(key, buf[:])
	return key, nil
}

// createEvidenceBundleMACKey claims creation of the key file exclusively and
// writes a fresh random key. Losing racers get fs.ErrExist and load the
// winner (#1201 round 2).
func createEvidenceBundleMACKey(path string) ([]byte, error) {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL|evidenceMACKeyCreateNoFollow, 0o600)
	if err != nil {
		return nil, err
	}
	if _, err := f.Write(key); err != nil {
		_ = f.Close()
		_ = os.Remove(path) // ours alone: O_EXCL guaranteed we created it
		return nil, err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	return key, nil
}

func evidenceBundleAuthPayloadFromTree(tree map[string]any) (evidenceBundleAuthPayload, error) {
	id, _ := tree["id"].(string)
	if id == "" {
		return evidenceBundleAuthPayload{}, errors.New("evidence bundle is missing an id")
	}
	payload := evidenceBundleAuthPayload{
		ID:          id,
		GeneratedAt: stringField(tree, "generated_at"),
		Filters:     anyToMap(tree["filters"]),
		ItemIDs:     []string{},
	}
	for _, item := range bundleResultMaps(tree) {
		if itemID, _ := item["id"].(string); itemID != "" {
			payload.ItemIDs = append(payload.ItemIDs, itemID)
		}
	}
	return payload, nil
}

func computeEvidenceBundleMAC(key []byte, payload evidenceBundleAuthPayload) string {
	body, err := json.Marshal(payload)
	if err != nil {
		return ""
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(evidenceBundleMACDomain))
	_, _ = mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// sealEvidenceBundle computes and attaches the cache authentication field.
func sealEvidenceBundle(bundle map[string]any) error {
	key, err := loadOrCreateEvidenceBundleMACKey()
	if err != nil {
		return err
	}
	payload, err := evidenceBundleAuthPayloadFromTree(bundle)
	if err != nil {
		return err
	}
	bundle[evidenceBundleMACField] = computeEvidenceBundleMAC(key, payload)
	return nil
}

// verifyEvidenceBundleAuth validates and strips the cache authentication
// field. A bundle without it, or with one that does not cover exactly this
// canonical reference, is rejected.
func verifyEvidenceBundleAuth(bundle map[string]any) error {
	stored, _ := bundle[evidenceBundleMACField].(string)
	delete(bundle, evidenceBundleMACField)
	if stored == "" {
		return errors.New("evidence bundle authentication failed")
	}
	key, err := loadOrCreateEvidenceBundleMACKey()
	if err != nil {
		return err
	}
	payload, err := evidenceBundleAuthPayloadFromTree(bundle)
	if err != nil {
		return errors.New("evidence bundle authentication failed")
	}
	if !hmac.Equal([]byte(stored), []byte(computeEvidenceBundleMAC(key, payload))) {
		return errors.New("evidence bundle authentication failed")
	}
	return nil
}
