package app

import "testing"

func TestDiscoveredImportExitCode(t *testing.T) {
	cases := []struct {
		failed, succeeded, want int
	}{
		{0, 0, 0}, // every root skipped
		{0, 3, 0}, // all imported
		{1, 1, 0}, // partial success
		{2, 0, 1}, // total failure
	}
	for _, tc := range cases {
		if got := discoveredImportExitCode(tc.failed, tc.succeeded); got != tc.want {
			t.Fatalf("discoveredImportExitCode(%d, %d)=%d want %d", tc.failed, tc.succeeded, got, tc.want)
		}
	}
}
