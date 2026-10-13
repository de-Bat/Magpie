# Themes

Besides Light and Dark, Magpie can use colour themes you upload. A theme is a small JSON file; it is stored on
the server (`<data dir>/themes/`) and shows up on every device. Which theme a device uses is chosen per device.

## Add a theme

Open **Settings → Appearance → Themes → Upload theme** and pick a `.json` file. When the server has no access
token yet, you are asked for the setup code from the server's log, as for other settings changes.
Uploading a file with the same `id` replaces that theme.

## File format

```json
{
  "id": "nord",
  "name": "Nord",
  "base": "dark",
  "colors": {
    "bg": "#2e3440", "surface": "#3b4252", "text": "#eceff4", "accent": "#88c0d0"
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

[`docs/themes/nord.json`](themes/nord.json) (dark) and [`docs/themes/paper.json`](themes/paper.json) (light).

## Remove

Use **Remove** next to the theme, or delete its file from `<data dir>/themes/`. A device that was using a
removed theme goes back to System the next time it loads.
