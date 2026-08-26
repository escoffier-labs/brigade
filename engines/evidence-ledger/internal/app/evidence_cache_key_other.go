//go:build !unix

package app

import (
	"io/fs"
	"os"
)

// Evidence bundle MAC key storage requires a race-free no-follow open plus a
// platform ownership check. The portable stdlib exposes neither here (no
// O_NOFOLLOW-equivalent open flag, no owner SID query), so instead of
// silently degrading to the old follow-the-symlink, no-owner-check loader,
// key load and creation are refused outright with a typed error (#1201
// round 3). Cached-bundle authentication stays unavailable on this platform
// until those primitives exist; the refusal is deliberate and documented,
// never an insecure fallback.
const evidenceMACKeyCreateNoFollow = 0

// defaultEvidenceMACKeyPlatformSupported reports whether this platform can
// enforce the MAC key trust requirements.
func defaultEvidenceMACKeyPlatformSupported() bool { return false }

// openEvidenceMACKeyFile refuses on platforms without no-follow semantics;
// unreachable in practice because loadOrCreateEvidenceBundleMACKey gates on
// evidenceMACKeyPlatformSupported first.
func openEvidenceMACKeyFile(path string) (*os.File, error) {
	_ = path
	return nil, &EvidenceMACKeyError{Reason: "key storage is not supported on this platform (no race-free no-follow open and no ownership check); refusing to seal or verify cached evidence bundles"}
}

// checkEvidenceMACKeyOwner refuses where ownership cannot be determined;
// unreachable in practice for the same reason.
func checkEvidenceMACKeyOwner(info fs.FileInfo) error {
	_ = info
	return &EvidenceMACKeyError{Reason: "key storage is not supported on this platform (no race-free no-follow open and no ownership check); refusing to seal or verify cached evidence bundles"}
}
