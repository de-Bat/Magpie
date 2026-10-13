import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie import themes
from magpie.config import Settings
from magpie.main import create_app

BUILTIN_IDS = {"atom-one-dark", "atom-one-dark-vivid", "atom-one-light", "dracula", "nord",
               "solarized-dark", "solarized-light", "gruvbox-dark"}


def raw(**over):
    d = {"id": "midnight", "name": "Midnight", "base": "dark", "colors": {"bg": "#2e3440", "accent": "rgb(136 192 208)"}}
    d.update(over)
    return json.dumps(d).encode()


def test_valid_theme_is_normalised():
    t = themes.validate(raw())
    assert t == {"id": "midnight", "name": "Midnight", "base": "dark", "colors": {"bg": "#2e3440", "accent": "rgb(136 192 208)"}}


def test_extra_top_level_keys_are_dropped():
    assert "author" not in themes.validate(raw(author="me"))


@pytest.mark.parametrize("bad", [
    {"id": "../x"}, {"id": "Nord"}, {"id": ""}, {"id": "a" * 41}, {"id": 5},
    {"name": ""}, {"name": "x" * 41}, {"name": "a\nb"}, {"name": 3},
    {"base": "blue"}, {"base": None},
    {"colors": []}, {"colors": {"nope": "#fff"}},
    {"colors": {"bg": "red"}}, {"colors": {"bg": 5}}, {"colors": {"bg": True}},
    {"colors": {"bg": "#12"}}, {"colors": {"bg": "#fff;}body{display:none"}},
    {"colors": {"bg": "red;}body{display:none"}}, {"colors": {"bg": "url(http://x/y.png)"}},
    {"colors": {"bg": "var(--text)"}}, {"colors": {"bg": "rgb(0 0 0);x"}},
])
def test_rejected(bad):
    with pytest.raises(themes.ThemeError) as e:
        themes.validate(raw(**bad))
    assert e.value.status == 422


@pytest.mark.parametrize("data", [b"", b"not json", b"[]", b"null", b"\xff\xfe\x00", b'"x"'])
def test_non_object_or_non_json_is_rejected(data):
    with pytest.raises(themes.ThemeError):
        themes.validate(data)


def test_oversized_file_is_rejected():
    big = raw(name="x", colors={"bg": "#fff"}) + b" " * themes.MAX_BYTES
    with pytest.raises(themes.ThemeError):
        themes.validate(big)


def test_save_list_overwrite_remove(tmp_path):
    themes.save(tmp_path, themes.validate(raw()))
    themes.save(tmp_path, themes.validate(raw(name="Midnight 2")))
    assert [t["name"] for t in themes.uploaded_themes(tmp_path)] == ["Midnight 2"]
    themes.remove(tmp_path, "midnight")
    assert themes.uploaded_themes(tmp_path) == []
    with pytest.raises(themes.ThemeError) as e:
        themes.remove(tmp_path, "midnight")
    assert e.value.status == 404


def test_list_skips_corrupt_and_mismatched_files(tmp_path):
    themes.save(tmp_path, themes.validate(raw()))
    d = tmp_path / "themes"
    (d / "broken.json").write_text("{nope", encoding="utf-8")
    (d / "other.json").write_bytes(raw(id="different"))   # file name must equal id
    assert [t["id"] for t in themes.uploaded_themes(tmp_path)] == ["midnight"]


@pytest.mark.parametrize("bad", ["../x", "..", "a/b", "x.json", "A", ""])
def test_remove_never_touches_paths_outside_the_folder(tmp_path, bad):
    (tmp_path / "x.json").write_text("{}")
    with pytest.raises(themes.ThemeError) as e:
        themes.remove(tmp_path, bad)
    assert e.value.status == 404
    assert (tmp_path / "x.json").exists()


# ---- built-in themes ----

def _luminance(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_builtin_themes_are_complete_valid_and_unique():
    found = themes.builtin_themes()
    assert {t["id"] for t in found} == BUILTIN_IDS
    assert len(found) == len(BUILTIN_IDS)
    files = list(themes.BUILTIN_DIR.glob("*.json"))
    assert len(files) == len(found)               # a file the loader skipped (bad JSON, name != id) fails here
    for t in found:
        assert t["builtin"] is True
        assert set(t["colors"]) == set(themes.COLOR_KEYS), t["id"]   # every colour set: nothing falls back to the other base
        assert all(c.startswith("#") and len(c) == 7 for c in t["colors"].values()), t["id"]


def test_builtin_backgrounds_match_their_base():
    for t in themes.builtin_themes():
        lum = _luminance(t["colors"]["bg"])
        assert (lum < 0.25) if t["base"] == "dark" else (lum > 0.75), t["id"]


def test_builtin_text_is_readable_on_its_background():
    for t in themes.builtin_themes():
        gap = abs(_luminance(t["colors"]["text"]) - _luminance(t["colors"]["bg"]))
        assert gap > 0.4, t["id"]


def test_list_puts_builtins_first_then_uploads(tmp_path):
    themes.save(tmp_path, themes.validate(raw()))
    listed = themes.list_themes(tmp_path)
    assert [t["id"] for t in listed[-1:]] == ["midnight"] and listed[-1]["builtin"] is False
    assert {t["id"] for t in listed[:-1]} == BUILTIN_IDS and all(t["builtin"] for t in listed[:-1])


def test_builtin_ids_cannot_be_saved_or_removed(tmp_path):
    with pytest.raises(themes.ThemeError) as e:
        themes.save(tmp_path, themes.validate(raw(id="dracula")))
    assert e.value.status == 422
    with pytest.raises(themes.ThemeError) as e:
        themes.remove(tmp_path, "dracula")
    assert e.value.status == 403
    assert not (tmp_path / "themes" / "dracula.json").exists()


def test_an_uploaded_file_named_like_a_builtin_is_not_listed_twice(tmp_path):
    d = tmp_path / "themes"
    d.mkdir()
    (d / "dracula.json").write_bytes(raw(id="dracula", name="Fake Dracula"))   # e.g. copied in by hand
    listed = [t for t in themes.list_themes(tmp_path) if t["id"] == "dracula"]
    assert len(listed) == 1 and listed[0]["name"] == "Dracula" and listed[0]["builtin"] is True


# ---- routes ----

@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path)
    with TestClient(create_app(s, http=httpx.AsyncClient())) as c:
        c.setup_headers = {"X-Magpie-Setup-Code": c.app.state.runtime.setup_code}
        yield c


def upload(client, content, auth=True):
    return client.post("/api/themes", files={"file": ("t.json", content, "application/json")},
                       headers=client.setup_headers if auth else {})


def uploaded(client):
    return [t for t in client.get("/api/themes").json() if not t["builtin"]]


def test_upload_list_overwrite_delete(client):
    assert {t["id"] for t in client.get("/api/themes").json()} == BUILTIN_IDS
    assert uploaded(client) == []
    r = upload(client, raw())
    assert r.status_code == 200 and r.json()["id"] == "midnight"
    upload(client, raw(name="Midnight v2"))
    assert [t["name"] for t in uploaded(client)] == ["Midnight v2"]
    assert client.delete("/api/themes/midnight", headers=client.setup_headers).status_code == 204
    assert uploaded(client) == []
    assert client.delete("/api/themes/midnight", headers=client.setup_headers).status_code == 404


def test_writes_need_the_setup_code(client):
    assert upload(client, raw(), auth=False).status_code == 403
    upload(client, raw())
    assert client.delete("/api/themes/midnight").status_code == 403
    assert len(uploaded(client)) == 1   # reading needs nothing extra


def test_builtins_are_listed_marked_and_protected(client):
    listed = {t["id"]: t for t in client.get("/api/themes").json()}
    assert listed["dracula"]["builtin"] is True and listed["dracula"]["name"] == "Dracula"
    assert upload(client, raw(id="dracula")).status_code == 422
    assert client.delete("/api/themes/dracula", headers=client.setup_headers).status_code == 403
    assert {t["id"] for t in client.get("/api/themes").json()} == BUILTIN_IDS   # still there, unchanged


@pytest.mark.parametrize("content", [b"junk", b"[]", b"\xff\xfe", raw(colors={"bg": "red;}"}), raw(id="../evil")])
def test_bad_uploads_are_422_not_500_and_store_nothing(client, content):
    r = upload(client, content)
    assert r.status_code == 422 and r.json()["detail"]
    assert uploaded(client) == []


def test_oversized_upload_is_422(client):
    assert upload(client, b" " * (themes.MAX_BYTES + 5000)).status_code == 422


def test_delete_with_odd_ids_is_404(client):
    for bad in ("..", "x.json", "A", "a%2Fb"):
        assert client.delete(f"/api/themes/{bad}", headers=client.setup_headers).status_code == 404


# ---- example themes ----

EXAMPLES = sorted((Path(__file__).parent.parent / "docs" / "themes").glob("*.json"))


def test_example_themes_are_valid_present_and_not_builtin():
    assert len(EXAMPLES) >= 1
    for path in EXAMPLES:
        assert themes.validate(path.read_bytes())["id"] == path.stem
        assert path.stem not in BUILTIN_IDS      # an example that is a built-in id could not be uploaded
