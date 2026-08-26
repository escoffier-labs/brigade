//go:build !unix

package app

import (
	"io/fs"
	"os"
)

// Platforms without O_NOFOLLOW rely on the portable pre-open Lstat symlink
// refusal plus the regular-file/mode/size checks; a racing symlink swap
// between Lstat and open remains possible there (residual tracked in #1093).
const evidenceMACKeyCreateNoFollow = 0

func openEvidenceMACKeyFile(path string) (*os.File, error) {
	return os.OpenFile(path, os.O_RDONLY, 0)
}

// checkEvidenceMACKeyOwner is a no-op where uid ownership does not exist;
// mode and size validation still apply on every platform (#1201).
func checkEvidenceMACKeyOwner(info fs.FileInfo) error {
	_ = info
	return nil
}
