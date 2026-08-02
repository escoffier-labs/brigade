package channels

import (
	"unicode/utf8"
)

// Provider payload ceilings from each vendor's documented API limits.
const (
	// Discord embed field ceilings:
	// https://discord.com/developers/docs/resources/channel#embed-object
	DiscordTitleMax       = 256
	DiscordDescriptionMax = 4096
	DiscordFooterMax      = 2048
	DiscordFieldValueMax  = 1024
	DiscordEmbedTotalMax  = 6000

	// Telegram Bot API sendMessage text ceiling:
	// https://core.telegram.org/bots/api#sendmessage
	TelegramTextMax = 4096

	// Signal has no Telegram-style documented char ceiling in signal-cli's
	// send endpoint; keep a practical bound under the DataMessage size class
	// so a single notification cannot push multi-megabyte JSON bodies.
	SignalMessageMax = 64 * 1024
)

const truncateSuffix = "…"

// truncateRunes shortens s to at most max bytes without splitting a UTF-8
// rune. When truncation is required the Unicode ellipsis is appended and
// counted toward max. Returns ok=false when max is too small to hold the
// ellipsis alone (message contract cannot be preserved).
func truncateRunes(s string, max int) (string, bool) {
	if max < 0 {
		return "", false
	}
	if len(s) <= max {
		return s, true
	}
	if max < len(truncateSuffix) {
		return "", false
	}
	budget := max - len(truncateSuffix)
	if budget <= 0 {
		return truncateSuffix, true
	}
	cut := budget
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + truncateSuffix, true
}
