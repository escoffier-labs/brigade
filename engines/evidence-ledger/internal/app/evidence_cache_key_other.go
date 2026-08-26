//go:build !unix

package app

import (
	"io/fs"
	"runtime"
)

// Evidence bundle MAC key storage requires a race-free no-follow open plus a
// platform ownership check. The portable stdlib exposes neither here (no
// O_NOFOLLOW-equivalent open flag, no owner SID query), so instead of
// silently degrading to the old follow-the-symlink, no-owner-check loader,
// key load and creation are refused outright with a typed error (#1201
// round 3). Cached-bundle authentication stays unavailable on this platform
// until those primitives exist; the refusal is deliberate and documented,
// never an insecure fallback.
func defaultEvidenceMACKeyPlatformSupported() bool { return false }

// checkEvidenceMACKeyOwner refuses where ownership cannot be determined;
// unreachable in practice for the same reason.
func checkEvidenceMACKeyOwner(info fs.FileInfo) error {
	_ = info
	return &EvidenceMACKeyError{Reason: "key storage is not supported on this platform (no race-free no-follow open and no ownership check); refusing to seal or verify cached evidence bundles"}
}

// openValidatedDataDirForAuth is unreachable: loadOrCreateEvidenceBundleMACKey
// refuses before descriptor use on platforms without no-follow opens.
func openValidatedDataDirForAuth() (int, error) {
	return -1, &EvidenceMACKeyError{Reason: "key storage is not supported on this platform (no race-free no-follow open and no ownership check); refusing to seal or verify cached evidence bundles"}
}

// readEvidenceBundleMACKeyAt is unreachable for the same reason.
func readEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	_ = dfd
	return nil, &EvidenceMACKeyError{Reason: "key storage is not supported on " + runtime.GOOS + "; refusing to seal or verify cached evidence bundles"}
}

// createEvidenceBundleMACKeyAt is unreachable for the same reason.
func createEvidenceBundleMACKeyAt(dfd int) ([]byte, error) {
	_ = dfd
	return nil, &EvidenceMACKeyError{Reason: "key storage is not supported on " + runtime.GOOS + "; refusing to seal or verify cached evidence bundles"}
}
