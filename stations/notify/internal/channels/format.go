package channels

// emojiFor returns the level-indicator emoji used as a prefix in plain-text
// channel formats (Telegram, Signal). Channels with structured embeds (e.g.,
// Discord) use the level for color instead and do not call this.
func emojiFor(level string) string {
	switch level {
	case "warn":
		return "⚠️"
	case "error":
		return "🚨"
	case "success":
		return "✅"
	default:
		return "ℹ️"
	}
}
