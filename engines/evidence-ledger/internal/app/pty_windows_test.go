//go:build !linux

package app

import (
	"os"
	"testing"
)

func openPtySlave(t *testing.T) *os.File {
	t.Helper()
	t.Skip("real PTY probe is Linux-only in this suite")
	return nil
}
