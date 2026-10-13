"""Theme behaviour that lives in the browser code (source-level checks, like test_settings_ui.py)."""
import re
from pathlib import Path

import magpie

STATIC = Path(magpie.__file__).parent / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_os_dark_block_does_not_apply_to_custom_themes():
    # a light custom theme on a dark OS must stay light
    media = CSS.split("@media (prefers-color-scheme: dark)")[1].split("}")[0]
    assert ':root:not([data-theme="light"]):not([data-theme="custom"])' in media


def test_dark_base_custom_themes_share_the_dark_block_and_inline_style_outranks_it():
    assert re.search(r':root\[data-theme="dark"\],\s*:root\[data-theme="custom"\]\[data-base="dark"\]\s*\{', CSS)
    assert ':root[data-theme="custom"][data-base]{' in JS      # same specificity as the dark block, later in the DOM


def test_pre_paint_script_applies_the_cached_custom_theme_and_validates_values():
    assert 'startsWith("custom:")' in HTML and "magpie.theme.data" in HTML and 'id = "magpie-theme"' in HTML
    assert "oklch" in HTML


def test_chosen_theme_that_no_longer_exists_falls_back_to_system():
    body = JS.split("async function loadThemes()")[1].split("\n}\n")[0]
    assert 'setTheme("system")' in body and "refreshAppearance()" in body


def test_upload_and_remove_use_setup_code_aware_calls_and_report_errors():
    assert 'apiWithSetup("/api/themes"' in JS and "apiWithSetup(`/api/themes/${encodeURIComponent(id)}`" in JS
    assert "Couldn't add the theme" in JS and 'id="theme-upload"' in JS and 'accept=".json,application/json"' in JS
    assert "[data-theme-remove]" in JS
