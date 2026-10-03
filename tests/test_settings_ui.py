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


def test_the_activity_tray_follows_every_async_operation_and_cleans_up_after_itself():
    js, css, html = source()
    assert 'id="activity" class="activity" popover="manual"' in html
    # item operations are noticed from status changes, wherever they started; removing an item drops its row
    assert "trackItem(prev, item);" in js and "activityDrop(id);" in js
    for kind in ("analyze", "reanalyze", "correct", "refresh"):
        assert f'{kind}: [' in js, kind
    assert 'pendingKind.set(id, "reanalyze")' in js and 'pendingKind.set(id, "correct")' in js
    # refresh and library-wide jobs report their own start and finish, including failure
    assert 'activityStart("refresh", id,' in js and 'activityFinish(`refresh:${op.id}`, "done"' in js and '"failed", activityText("refresh"' in js
    assert "trackBulk();" in js and "async function trackBulk()" in js
    # finished rows fade (failures later), can be dismissed, and a failed analysis can be retried
    assert "ACTIVITY_FADE = { done: 6000, warn: 12000, failed: 15000 }" in js
    assert "data-activity-dismiss" in js and "data-activity-retry" in js and "ACTIVITY_MAX = 5" in js
    assert ".act-failed" in css and ".act.leaving" in css and "prefers-reduced-motion" in css


def test_add_button_opens_a_menu_and_names_are_searched_before_adding():
    js, css, html = source()
    assert 'id="add-menu"' in html and 'aria-haspopup="menu"' in html and 'id="fab-more"' in html and 'id="find-dialog"' in html
    for kind in ("movie", "tv_show", "book"):
        assert f'data-find="{kind}"' in html, kind
    assert 'id="fab" class="fab" type="button" data-action="add-upload"' in html   # the main part adds as before; the small part opens the menu
    assert 'data-action="add-upload"' in html            # the old screenshot/link dialog stays one click away
    assert "/api/catalog/search?kind=" in js and '"/api/items/entry"' in js
    assert "findResults.length === 1" in js and "data-find-typed" in js and "data-find-pick" in js
    assert ".add-menu" in css and ".find-row" in css


def test_plugins_can_add_a_section_to_the_card():
    js, css, _ = source()
    assert "function pluginSectionsHtml(" in js and 'class="plugin-sections"' in js and "function dateRowHtml(" in js
    assert 'data-plugin-section=' in js and 'data-jump-section' in js and ".psec" in css and ".when-due" in css


def test_plugin_sections_sit_after_the_card_content_just_above_the_original():
    js, _, _ = source()
    detail = js[js.index("relatedHtml(item)}"):]
    assert detail.index('class="plugin-sections"') < detail.index("<h3>Original</h3>")
    assert detail.index('class="plugin-sections"') - detail.index("relatedHtml(item)}") < 120   # directly after the last content section
    assert js.index('class="plugin-sections"') > js.index("<h3>About</h3>")


def test_header_keeps_only_a_jump_capsule_and_phone_ratings_share_one_row():
    js, css, _ = source()
    assert 'class="btn plugin-added plugin-jump" data-jump-section=' in js and "downloadChipHtml({ ...p, download: p.section.download }" not in js
    assert 'class="l-short"' in js and ".scores { flex-wrap: nowrap" in css


def test_main_link_is_an_icon_and_arrow_on_phones():
    js, css, _ = source()
    assert "function sourceIconHtml(" in js and 'class="open-text"' in js and 'aria-label="Open ' in js
    assert ".open-src .open-text { display: none; }" in css
