"""Text and icon size (Settings → Appearance → Size): source-level checks, like test_settings_ui.py."""
import re
from pathlib import Path

import magpie

STATIC = Path(magpie.__file__).parent / "static"
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def test_scale_table_exists_and_standard_is_one():
    assert re.search(r":root\s*\{[^}]*--ui-scale:\s*1;", CSS)
    for name, value in (("small", "0.9"), ("large", "1.15"), ("xlarge", "1.3")):
        assert f':root[data-size="{name}"] {{ --ui-scale: {value}; }}' in CSS


def test_every_text_size_follows_the_scale_except_the_ios_input_minimum():
    # a font-size left in plain px would ignore the setting; 16px !important keeps iOS from zooming into inputs
    plain = re.findall(r"font-size:\s*[\d.]+px(?!\s*!important)", CSS)
    assert plain == [], plain
    assert re.findall(r"font:\s*(?:\d{3}\s+)?[\d.]+px", CSS)   # shorthands exist...
    for m in re.finditer(r"font:\s*(?:\d{3}\s+)?([\d.]+)px[^;}]*", CSS):
        rest = CSS[m.end():m.end() + 80]                          # ...and each is followed by its scaled size
        assert rest.startswith(f"; font-size: calc({m.group(1)}px * var(--ui-scale))"), m.group(0)


def test_icon_boxes_follow_the_scale():
    for selector in (".ticon", ".tabs .tabicon", ".eyebrow-type .ticon", ".grid.list .glyph", ".btn.icon-btn", ".plugin-ico", ".act-icon"):
        rule = next(r for r in re.findall(r"[^{}]+\{[^{}]*\}", CSS) if r.strip().startswith(selector + " {"))
        assert "var(--ui-scale)" in rule, selector


def test_pre_paint_script_applies_the_saved_size():
    assert 'localStorage.getItem("magpie.size")' in HTML and "dataset.size" in HTML


def test_size_control_has_four_levels_and_is_saved_per_device():
    assert 'const UI_SIZES = [["small", "Small"], ["standard", "Standard"], ["large", "Large"], ["xlarge", "Extra large"]]' in JS
    assert 'localStorage.setItem("magpie.size", size)' in JS and 'localStorage.removeItem("magpie.size")' in JS
    assert "data-size-choice" in JS and "setSize(t.dataset.sizeChoice)" in JS
