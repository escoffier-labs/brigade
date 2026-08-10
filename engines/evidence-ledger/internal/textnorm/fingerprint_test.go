package textnorm

import "testing"

func TestContentFingerprintMatchesBrigade724(t *testing.T) {
	a := "---\ntopic: t\n---\n\nFlush the cache after migrations.\n"
	b := "---\ntopic: other\n---\n\nflush the cache after migrations!\n"
	if got := NormalizeCardContent(a); got != "flush the cache after migrations" {
		t.Fatalf("normalize = %q", got)
	}
	want := "31500e3247257e8401e3efbd63154b95731294f97ebf8131fc725daea648ab60"
	fpA := ContentFingerprint(a)
	fpB := ContentFingerprint(b)
	if fpA != want {
		t.Fatalf("fingerprint a = %s want %s", fpA, want)
	}
	if fpA != fpB {
		t.Fatalf("punctuation variants must share fingerprint: %s vs %s", fpA, fpB)
	}
}

func TestContentFingerprintNeverUsedAsIdentityShape(t *testing.T) {
	body := "---\nid: card-11111111-2222-4333-8444-555555555555\n---\n\nSame body text.\n"
	fp := ContentFingerprint(body)
	if stringsHasPrefix(fp, "card-") || stringsHasPrefix(fp, "path:") || stringsHasPrefix(fp, "memory-") {
		t.Fatalf("fingerprint looks like identity: %s", fp)
	}
	if len(fp) != 64 {
		t.Fatalf("fingerprint length = %d", len(fp))
	}
}

func stringsHasPrefix(s, prefix string) bool {
	return len(s) >= len(prefix) && s[:len(prefix)] == prefix
}
