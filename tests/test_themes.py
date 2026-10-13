import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from magpie import themes
from magpie.config import Settings
from magpie.main import create_app


def raw(**over):
    d = {"id": "nord", "name": "Nord", "base": "dark", "colors": {"bg": "#2e3440", "accent": "rgb(136 192 208)"}}
    d.update(over)
    return json.dumps(d).encode()


def test_valid_theme_is_normalised():
    t = themes.validate(raw())
    assert t == {"id": "nord", "name": "Nord", "base": "dark", "colors": {"bg": "#2e3440", "accent": "rgb(136 192 208)"}}


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
    themes.save(tmp_path, themes.validate(raw(name="Nord 2")))
    assert [t["name"] for t in themes.list_themes(tmp_path)] == ["Nord 2"]
    themes.remove(tmp_path, "nord")
    assert themes.list_themes(tmp_path) == []
    with pytest.raises(themes.ThemeError) as e:
        themes.remove(tmp_path, "nord")
    assert e.value.status == 404


def test_list_skips_corrupt_and_mismatched_files(tmp_path):
    themes.save(tmp_path, themes.validate(raw()))
    d = tmp_path / "themes"
    (d / "broken.json").write_text("{nope", encoding="utf-8")
    (d / "other.json").write_bytes(raw(id="different"))   # file name must equal id
    assert [t["id"] for t in themes.list_themes(tmp_path)] == ["nord"]


@pytest.mark.parametrize("bad", ["../x", "..", "a/b", "x.json", "A", ""])
def test_remove_never_touches_paths_outside_the_folder(tmp_path, bad):
    (tmp_path / "x.json").write_text("{}")
    with pytest.raises(themes.ThemeError) as e:
        themes.remove(tmp_path, bad)
    assert e.value.status == 404
    assert (tmp_path / "x.json").exists()


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


def test_upload_list_overwrite_delete(client):
    assert client.get("/api/themes").json() == []
    r = upload(client, raw())
    assert r.status_code == 200 and r.json()["id"] == "nord"
    upload(client, raw(name="Nord v2"))
    assert [t["name"] for t in client.get("/api/themes").json()] == ["Nord v2"]
    assert client.delete("/api/themes/nord", headers=client.setup_headers).status_code == 204
    assert client.get("/api/themes").json() == []
    assert client.delete("/api/themes/nord", headers=client.setup_headers).status_code == 404


def test_writes_need_the_setup_code(client):
    assert upload(client, raw(), auth=False).status_code == 403
    upload(client, raw())
    assert client.delete("/api/themes/nord").status_code == 403
    assert len(client.get("/api/themes").json()) == 1   # reading needs nothing extra


@pytest.mark.parametrize("content", [b"junk", b"[]", b"\xff\xfe", raw(colors={"bg": "red;}"}), raw(id="../evil")])
def test_bad_uploads_are_422_not_500_and_store_nothing(client, content):
    r = upload(client, content)
    assert r.status_code == 422 and r.json()["detail"]
    assert client.get("/api/themes").json() == []


def test_oversized_upload_is_422(client):
    assert upload(client, b" " * (themes.MAX_BYTES + 5000)).status_code == 422


def test_delete_with_odd_ids_is_404(client):
    for bad in ("..", "x.json", "A", "a%2Fb"):
        assert client.delete(f"/api/themes/{bad}", headers=client.setup_headers).status_code == 404


# ---- example themes ----

EXAMPLES = sorted((Path(__file__).parent.parent / "docs" / "themes").glob("*.json"))


def test_example_themes_are_valid_and_present():
    assert len(EXAMPLES) >= 2
    for path in EXAMPLES:
        assert themes.validate(path.read_bytes())["id"] == path.stem
