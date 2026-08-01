//go:build !unix

package safeio

// fsyncParent is a no-op where directory fsync / O_NOFOLLOW are unavailable.
// Those platforms therefore cannot refuse a symlinked parent here; Rename/Link
// still provides the TOCTOU resistance for the final path.
func fsyncParent(dir string) error {
	_ = dir
	return nil
}
