"""Colour themes: a small JSON file of CSS-variable colours, validated hard (it ends up in a <style>).
Built-in themes ship in magpie/static/themes/; uploaded ones are stored as <data dir>/themes/<id>.json and shared
by every device."""

import functools
import json
import os
import re
from pathlib import Path

BUILTIN_DIR = Path(__file__).parent / "static" / "themes"
MAX_BYTES = 8 * 1024
COLOR_KEYS = ("bg", "surface", "surface-2", "text", "muted", "border", "accent", "accent-soft",
              "on-accent", "ok", "err", "warn", "warn-soft")
ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FUNC = re.compile(r"^(?:rgb|rgba|hsl|hsla|oklch)\((?:[0-9.%,/ \-]|deg){1,60}\)$")


class ThemeError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _folder(data_dir: Path) -> Path:
    return Path(data_dir) / "themes"


def validate(raw: bytes) -> dict:
    if len(raw) > MAX_BYTES:
        raise ThemeError(f"The theme file is too large (the limit is {MAX_BYTES // 1024} KB).")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ThemeError("The theme file isn't valid JSON.")
    if not isinstance(data, dict):
        raise ThemeError("The theme file must be a JSON object.")
    theme_id, name, base, colors = data.get("id"), data.get("name"), data.get("base"), data.get("colors")
    if not isinstance(theme_id, str) or not ID_RE.match(theme_id):
        raise ThemeError("\"id\" must be 1-40 characters: lowercase letters, digits and dashes.")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or not name.isprintable():
        raise ThemeError("\"name\" must be 1-40 characters of plain text.")
    if base not in ("light", "dark"):
        raise ThemeError("\"base\" must be \"light\" or \"dark\".")
    if not isinstance(colors, dict):
        raise ThemeError("\"colors\" must be an object.")
    for key, value in colors.items():
        if key not in COLOR_KEYS:
            raise ThemeError(f"Unknown color \"{key}\". Allowed: {', '.join(COLOR_KEYS)}.")
        if not isinstance(value, str) or not (_HEX.match(value) or _FUNC.match(value)):
            raise ThemeError(f"\"{key}\" must be a color like #1a2b3c or rgb(26 43 60).")
    return {"id": theme_id, "name": name.strip(), "base": base, "colors": dict(colors)}


def _read_folder(folder: Path, builtin: bool) -> list[dict]:
    """The valid themes in a folder (a file that fails validation, or isn't named <id>.json, is skipped)."""
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            theme = validate(path.read_bytes())
        except (OSError, ThemeError):
            continue
        if theme["id"] == path.stem:
            out.append({**theme, "builtin": builtin})
    return out


@functools.lru_cache(maxsize=1)
def _builtins() -> tuple[dict, ...]:
    return tuple(_read_folder(BUILTIN_DIR, True))


def builtin_themes() -> list[dict]:
    return [{**t, "colors": dict(t["colors"])} for t in _builtins()]


def is_builtin(theme_id: str) -> bool:
    return any(t["id"] == theme_id for t in _builtins())


def uploaded_themes(data_dir: Path) -> list[dict]:
    return [t for t in _read_folder(_folder(data_dir), False) if not is_builtin(t["id"])]


def list_themes(data_dir: Path) -> list[dict]:
    """Built-in themes first, then the uploaded ones."""
    return builtin_themes() + uploaded_themes(data_dir)


def save(data_dir: Path, theme: dict) -> None:
    if is_builtin(theme["id"]):
        raise ThemeError(f"\"{theme['id']}\" is a built-in theme. Pick another id.")
    folder = _folder(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{theme['id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(theme, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def remove(data_dir: Path, theme_id: str) -> None:
    if not isinstance(theme_id, str) or not ID_RE.match(theme_id):
        raise ThemeError("No such theme.", 404)
    if is_builtin(theme_id):
        raise ThemeError("Built-in themes can't be removed.", 403)
    try:
        (_folder(data_dir) / f"{theme_id}.json").unlink()
    except FileNotFoundError:
        raise ThemeError("No such theme.", 404)
