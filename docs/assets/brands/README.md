# Deck brand marks: provenance and mapping

Fleet Command Deck identifies harnesses and providers through inline vector
marks. Except for Jules, the vendor SVG geometry in
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
| `jules` | harness/provider | Official [Jules favicon](https://jules.google/favicon.svg) | Jules |
| `t3-fleet` | harness | plain `T3` monogram (Brigade-original) | T3 Fleet |

Notes:

- T3's `AntigravityIcon` is a raster PNG wrapped in an `<image>` tag, so it
  cannot be transcribed as vector geometry (and this directory carries no
  binaries). The `Gemini` mark stands in for both Antigravity and Google,
  both Google properties.
- `t3-fleet` keeps a plain `T3` typographic mark drawn for Brigade.
- Jules uses the official purple octopus mascot from
  `https://jules.google/favicon.svg`, supplied on 2026-10-20. Its 180 by 180
  viewBox, path geometry, and purple fills/strokes are preserved. Fixed pixel
  dimensions and the standalone XML namespace were removed for inline HTML.
  It contains only SVG/path elements, with no scripts or external resources.
- Gemini uses the existing mark's mask silhouette as a sharp `currentColor`
  path. The blurred color layers and filters are omitted. OpenCode uses its
  existing outer path in `currentColor`, without the fixed dark-variant fills.
- Monochrome marks inherit the theme foreground on a transparent 20px square,
  without capsule backgrounds or padding. Jules retains its official purple.
- Unknown keys use a Brigade-original neutral outlined agent, labeled `Agent`.
  It identifies no vendor. Hostile input is escaped in the title. The accessible
  label comes from the trusted brand table.
