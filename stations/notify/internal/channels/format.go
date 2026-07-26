package channels

import "github.com/escoffier-labs/agent-notify/internal/canonical"

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

// providerMark is the neutral provider identity indicator that replaces the
// generic info emoji when a channel renders a known provider/model identity.
const providerMark = "◉"

// identityFor returns the provider/model identity string used as the title
// for channels that support structured titles, replacing the generic
// per-adapter title (e.g. "Codex (turn-N)") when the model is known. Today
// only Codex notifications carry a resolvable model; other sources fall
// through to their adapter-supplied title.
func identityFor(m canonical.Message) string {
	if m.Source == "codex" && m.Model != "" {
		return "OpenAI · " + m.Model
	}
	return ""
}

// titleFor returns the title a channel should render: the provider/model
// identity when one is known, otherwise the adapter-supplied title.
func titleFor(m canonical.Message) string {
	if id := identityFor(m); id != "" {
		return id
	}
	return m.Title
}

// indicatorFor returns the prefix indicator for plain-text channels. Severity
// levels (warn/error/success) always keep their severity emoji so a failure is
// never visually demoted by a provider mark. For the default (info) level, a
// known provider/model identity replaces the generic info emoji with the
// neutral provider mark; otherwise the info emoji is used as before.
func indicatorFor(m canonical.Message) string {
	switch m.Level {
	case "warn", "error", "success":
		return emojiFor(m.Level)
	default:
		if identityFor(m) != "" {
			return providerMark
		}
		return emojiFor(m.Level)
	}
}
