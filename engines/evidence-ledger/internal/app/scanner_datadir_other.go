//go:build !unix

package app

import (
	"fmt"
	"os"
)

// openDataDirFile refuses on platforms where the portable stdlib provides no
// race-free no-follow, directory-relative open primitive (#1201 round 4).
// Like the MAC key platform stance, the refusal is deliberate: private-file
// loading fails closed instead of silently degrading to a symlink-following
// open. The returned error is deliberately not fs.ErrNotExist, so callers
// treat the file as refused rather than merely missing.
func openDataDirFile(dirPath, name string) (*os.File, error) {
	_ = dirPath
	return nil, fmt.Errorf("refusing to open %s: this platform provides no race-free no-follow directory-relative open", name)
}
