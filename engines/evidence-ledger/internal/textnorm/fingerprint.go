package textnorm

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"unicode"
)

// NormalizeCardContent mirrors brigade.card_fingerprint.normalize_card_content (#724):
// strip leading frontmatter, lowercase, replace punctuation with spaces, collapse
// whitespace. Used for duplicate detection only — never as card identity.
func NormalizeCardContent(text string) string {
	body := stripLeadingFrontmatter(text)
	var b strings.Builder
	b.Grow(len(body))
	space := false
	for _, r := range strings.ToLower(body) {
		if unicode.IsSpace(r) {
			space = true
			continue
		}
		if !isFingerprintWord(r) {
			space = true
			continue
		}
		if space && b.Len() > 0 {
			b.WriteByte(' ')
		}
		space = false
		b.WriteRune(r)
	}
	return b.String()
}

// ContentFingerprint returns the #724 hex SHA-256 of NormalizeCardContent(text).
func ContentFingerprint(text string) string {
	normalized := NormalizeCardContent(text)
	sum := sha256.Sum256([]byte(normalized))
	return hex.EncodeToString(sum[:])
}

func isFingerprintWord(r rune) bool {
	return unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_'
}

func stripLeadingFrontmatter(text string) string {
	if !strings.HasPrefix(text, "---\n") && !strings.HasPrefix(text, "---\r\n") && text != "---" {
		return text
	}
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return text
	}
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			return strings.Join(lines[i+1:], "\n")
		}
	}
	return text
}
