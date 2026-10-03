"""Settings panel behaviour that lives in the browser code: where toasts appear, what Save does, the plugin test indicator."""

from pathlib import Path

import magpie

STATIC = Path(magpie.__file__).parent / "static"

def source():
    return (STATIC / "app.js").read_text(), (STATIC / "style.css").read_text(), (STATIC / "index.html").read_text()


def test_toasts_are_in_the_top_layer_so_they_show_above_an_open_dialog():
    js, css, html = source()
    assert 'id="toast" class="toast" popover="manual"' in html and "hidden" not in html.split('id="toast"')[1].split(">")[0]
    assert "showPopover()" in js and ".toast[popover]" in css


def test_save_changes_confirms_and_closes_the_panel_but_reset_does_not():
    js, _, _ = source()
    assert "await putSettings(changes, { close: true });" in js
    assert 'toast("No changes to save."); $("#detail").close();' in js
    assert "if (close && checks.every((c) => c.ok)) { $(\"#detail\").close(); return; }" in js   # a rejected key keeps it open
    assert "putSettings({ [t.dataset.reset]: null })" in js                                       # Reset: stays open, no close flag
    assert 'toast(["Settings saved.", ...(settingsData.notices || []), ...checks.map((c) => c.text)].join("  "));' in js


def test_plugin_test_connection_shows_an_inline_result_and_offers_dropdowns():
    js, css, _ = source()
    assert 'data-test-result="${esc(p.id)}"' in js and 'role="status"' in js
    for state in ("busy", "ok", "err"):
        assert f".test-result.{state}" in css
    assert "function fillPluginOptions" in js and "_ROOT_FOLDER" in js and "_QUALITY_PROFILE" in js
    assert "(not found on the server)" in js and "autoCheckPlugin();" in js
