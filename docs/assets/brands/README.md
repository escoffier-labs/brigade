# Deck brand marks: provenance and mapping

The harness and provider badges on Fleet Command Deck run cards use real
vendor marks, not Brigade-drawn monograms. The SVG geometry in
`src/brigade/fleet_deck_brands.py` was transcribed from T3 Code's
`Icons.tsx` at version `0.0.39-nightly.20260906.1303` (MIT-licensed
project). Only the geometry (viewBox plus paths/shapes) was copied, and it
was converted from JSX to plain SVG (`className`/`cn` props dropped,
`fill-rule`/`clip-rule`/`clip-path` spellings fixed for plain markup).

Each mark is the trademark of its owner and is used only to identify that
provider or harness on the Deck. No third-party binary files are vendored.

## Mapping

| Deck key | Kind | Mark source | Label |
|---|---|---|---|
| `claude` | harness | `ClaudeAI` | Claude |
| `anthropic` | provider | `ClaudeAI` | Anthropic |
| `codex` | harness | `OpenAI` | Codex |
| `openai` | provider | `OpenAI` | OpenAI |
| `opencode` | harness/provider | `OpenCodeIcon` (dark variant) | OpenCode |
| `cursor` | harness/provider | `CursorIcon` | Cursor |
| `grok`, `xai` | harness/provider | `GrokIcon` | Grok / xAI |
| `grokbot`, `grok-bot` | harness | `GrokIcon` with a `bot` title | Grokbot |
| `antigravity`, `google` | harness/provider | `Gemini` (stand-in, see below) | Antigravity / Google |
| `t3-fleet` | harness | plain `T3` monogram (Brigade-original) | T3 Fleet |

Notes:

- T3's `AntigravityIcon` is a raster PNG wrapped in an `<image>` tag, so it
  cannot be transcribed as vector geometry (and this directory carries no
  binaries). The `Gemini` mark stands in for both Antigravity and Google,
  both Google properties.
- `t3-fleet` is Brigade's own fleet/principal seat, so it keeps a plain
  `T3` monogram rather than any vendor mark.
- Unknown keys fall back to the neutral `??` monogram on grey; hostile
  input is escaped and never reaches the markup.
