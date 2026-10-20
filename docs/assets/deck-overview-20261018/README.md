# Deck overview redesign captures

The captures show `/deck` at 1440 and 390 pixels wide in the dark and light color schemes, and `/deck/repos` in the light scheme at 1440.

They come from `capture_fixture.py`: a real fixture hub with synthetic data (stations `linux-a`, `linux-b`, `win-a`, generic repository names, no live fleet data or credentials), the pages fetched over HTTP, the meta refresh removed so a capture does not reload, and the frozen pages served locally with the same nonce CSP the hub sends. Headless Chrome renders them; `preferredColorScheme` selects dark (0) or light (1). Screenshots are viewport captures, not full-page images.

| Page | Width | Scheme | Image |
| --- | --- | --- | --- |
| Overview | 1440 | dark | [Image](deck-1440-dark.png) |
| Overview | 1440 | light | [Image](deck-1440-light.png) |
| Overview | 390 | dark | [Image](deck-390-dark.png) |
| Overview | 390 | light | [Image](deck-390-light.png) |
| Repos | 1440 | light | [Image](deck-repos-1440-light.png) |

The layout draws on the Runner Dashboard in `aarondfrancis/fleet`, which has no license. Only layout and behavior ideas were taken; no code, CSS, scripts, or assets were copied.
