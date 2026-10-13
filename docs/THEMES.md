# Themes

Besides Light and Dark, Magpie has built-in colour themes and can use ones you upload. A theme is a small JSON
file. Uploaded themes are stored on the server (`<data dir>/themes/`) and show up on every device. Which theme a
device uses is chosen per device, in **Settings → Appearance**.

## Built-in themes

| Theme | Base |
| --- | --- |
| Atom One Dark | dark |
| Atom One Dark Vivid (a more saturated variation) | dark |
| Atom One Light | light |
| Dracula | dark |
| Nord | dark |
| Solarized Dark | dark |
| Solarized Light | light |
| Gruvbox Dark | dark |

Built-in themes can't be removed, and an upload can't use their ids (`atom-one-dark`, `atom-one-dark-vivid`,
`atom-one-light`, `dracula`, `nord`, `solarized-dark`, `solarized-light`, `gruvbox-dark`). To change one, upload
your own file with a different `id`. A built-in theme is the same JSON format as below, in
`magpie/static/themes/`; sending a pull request with another one is just adding a file there.

## Add a theme

Open **Settings → Appearance → Themes → Upload theme** and pick a `.json` file. When the server has no access
token yet, you are asked for the setup code from the server's log, as for other settings changes.
Uploading a file with the same `id` replaces that theme.

## File format

```json
{
  "id": "midnight",
  "name": "Midnight",
  "base": "dark",
  "colors": {
    "bg": "#0b1020", "surface": "#141a2e", "text": "#e6e9f2", "accent": "#7aa2ff"
  }
}
```

| Field | Rules |
| --- | --- |
| `id` | 1–40 characters: lowercase letters, digits, `-`. The file is stored under this name. |
| `name` | 1–40 characters of plain text, shown in the list. |
| `base` | `light` or `dark`. Sets the browser's form-control colours, the logo, the app icon and every colour you leave out. |
| `colors` | Any of the keys below. Missing keys use the `base` palette. |

Colour keys: `bg` (page), `surface` (cards, panels), `surface-2` (raised panels), `text`, `muted` (secondary
text), `border`, `accent`, `accent-soft` (accent backgrounds), `on-accent` (text on an accent button), `ok`,
`err`, `warn`, `warn-soft`.

Values are `#rgb`, `#rrggbb`, `#rrggbbaa`, or `rgb()`, `rgba()`, `hsl()`, `hsla()`, `oklch()` with plain numbers
(for example `rgb(136 192 208)` or `oklch(0.7 0.1 220)`). Anything else (`url()`, `var()`, extra CSS) is rejected.
The file can be at most 8 KB.

## Examples

[`docs/themes/paper.json`](themes/paper.json) (light). The built-in themes in
[`magpie/static/themes/`](../magpie/static/themes/) are complete examples that set every colour.

## Remove

Use **Remove** next to an uploaded theme, or delete its file from `<data dir>/themes/`. A device that was using a
removed theme goes back to System the next time it loads.
