package app

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"

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

func evidenceBundleMACKeyPath() string {
	return filepath.Join(ResolvePaths().DataDir, evidenceBundleMACKeyFile)
}

func loadOrCreateEvidenceBundleMACKey() ([]byte, error) {
	path := evidenceBundleMACKeyPath()
	if key, err := os.ReadFile(path); err == nil {
		if len(key) != 32 {
			return nil, errors.New("evidence bundle MAC key is malformed")
		}
		return key, nil
	} else if !errors.Is(err, fs.ErrNotExist) {
		return nil, err
	}
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		return nil, err
	}
	if err := security.EnsurePrivateDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	if err := security.WritePrivateFileAtomic(path, key); err != nil {
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
