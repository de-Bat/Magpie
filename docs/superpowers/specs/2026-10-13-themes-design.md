# Themes with external upload: design

## Goal
Users pick named themes beyond Light/Dark/System and upload their own theme file (JSON) in
Settings → Appearance, with no code change or redeploy. Uploaded themes are stored on the server and shared
across devices; the active choice is per device.

Success: upload a file, see it in the list, apply it, it survives a restart, and a malformed or malicious
file is rejected.

## Non-goals
Theme editor UI, per-type cover hue overrides, remote URL import, raw CSS themes, iOS app theming.

## Theme file
```json
{ "id": "nord", "name": "Nord", "base": "dark",
  "colors": { "bg": "#2e3440", "surface": "#3b4252", "surface-2": "#434c5e", "text": "#eceff4",
              "muted": "#a3adbd", "border": "#4c566a", "accent": "#88c0d0", "accent-soft": "#2b4450",
              "on-accent": "#0b1a20", "ok": "#a3be8c", "err": "#bf616a", "warn": "#ebcb8b", "warn-soft": "#4a4030" } }
```
- `id`: `[a-z0-9-]{1,40}`. `name`: 1–40 chars, plain text. `base`: `light` or `dark`.
- `colors`: only the keys above (the existing CSS variables `--bg`, `--surface`, `--surface-2`, `--text`,
  `--muted`, `--border`, `--accent`, `--accent-soft`, `--on-accent`, `--ok`, `--err`, `--warn`, `--warn-soft`).
  Unknown keys are rejected. Missing keys fall back to the `base` palette.
- Values: `#rgb`, `#rrggbb`, `#rrggbbaa`, or `rgb()/rgba()/hsl()/hsla()/oklch()` with only digits, `.`, `%`,
  `,`, `/`, spaces and `deg`. Nothing else, so no `url()`, `;`, `}` or `var()`.
- File size at most 8 KB.

## Backend
New `magpie/themes.py`: `validate(raw: bytes) -> dict`, `list_themes(data_dir)`, `save(data_dir, theme)`,
`remove(data_dir, id)`. Storage is `<data dir>/themes/<id>.json`; the path is built from the validated id only.

Routes in `main.py`:
- `GET /api/themes`: list of valid themes (invalid files on disk are skipped). Normal token rules.
- `POST /api/themes`: multipart `file`. Validates, saves, returns the theme. Same id overwrites. 422 with a
  plain message on any validation error. Calls `require_settings_access`.
- `DELETE /api/themes/{id}`: removes it. 404 if unknown. Calls `require_settings_access`.

These are not gated by `MAGPIE_ALLOW_PLUGIN_INSTALL`: a validated JSON file is data, not code.

## Frontend
- Settings → Appearance keeps the System/Light/Dark control and adds a Themes list: swatch strip, name,
  Apply, and Remove on each uploaded theme, plus an Upload button (`.json` file input). Errors show as a toast.
- Per-device choice: `localStorage["magpie.theme"] = "custom:<id>"`; the theme's JSON is cached in
  `localStorage["magpie.theme.data"]`.
- `applyTheme` for a custom theme sets `data-theme="custom"` and `data-base="<base>"`, and writes the
  variables into `<style id="magpie-theme">`; `color-scheme`, logos, `theme-color` meta and install icons follow
  `base` exactly as `light`/`dark` do today.
- `index.html` applies the cached theme inline before first paint (no flash), as it does for light/dark now.
- On load, after `/api/themes` returns, the cache is refreshed; if the chosen theme no longer exists, fall back
  to System.
- `style.css`: the dark block is shared by `[data-theme="dark"]` and `[data-theme="custom"][data-base="dark"]`.
  A custom theme's `<style>` rule comes later and wins.
- `sw.js`: `/api/themes` is not cached.
- i18n strings added to `i18n.js` for the new labels.

## Errors
Bad JSON, unknown key, bad value, oversized file, bad id or bad base give 422 with a message naming the field.
Disk errors give 500 via the existing handler. A corrupt file in `themes/` is ignored by the list.

## Testing
pytest: validator (valid, each rejection class, `url()`/`;`/`}` injection, size limit), routes (upload, list,
overwrite, delete, 404, 422, access rules), path traversal in id (`../x`, absolute paths). Manual check of
upload → apply → reload → delete in the browser.

## Docs
`docs/THEMES.md` with the format and a sample; two example themes under `docs/themes/`.
