"""Cover pictures: the server notes whether a picture is fully opaque; only those get the blurred backdrop on the card."""

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

import magpie
from magpie.config import Settings
from magpie.images import picture_is_opaque
from test_magpie import analysis, make_client, mock_http

STATIC = Path(magpie.__file__).parent / "static"
URL = "https://img.test/cover"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, tmdb_api_key=None, omdb_api_key=None, github_token=None)


def encode(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def rgba(corner_alpha):
    im = Image.new("RGBA", (320, 240), (200, 30, 30, 255))
    ImageDraw.Draw(im).rectangle((0, 0, 40, 40), fill=(0, 0, 0, corner_alpha))
    return im


def palette_png(transparent):
    im = Image.new("P", (320, 240), 1)
    im.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    return encode(im, "PNG", **({"transparency": 0} if transparent else {}))


def verdict(body, ctype="image/png", status=200):
    http = mock_http({URL: httpx.Response(status, content=body, headers={"content-type": ctype})})
    return asyncio.run(picture_is_opaque(http, URL))


@pytest.mark.parametrize("body,ctype,expected", [
    (encode(Image.new("RGB", (320, 240), "teal"), "JPEG"), "image/jpeg", True),
    (encode(Image.new("RGB", (320, 240), "teal"), "PNG"), "image/png", True),
    (encode(rgba(255), "PNG"), "image/png", True),            # has an alpha channel, but nothing is transparent
    (encode(rgba(0), "PNG"), "image/png", False),             # a transparent corner: a logo
    (encode(rgba(0), "WEBP", lossless=True), "image/webp", False),
    (palette_png(transparent=True), "image/png", False),
    (palette_png(transparent=False), "image/png", True),
    (encode(Image.new("P", (320, 240), 1), "GIF", transparency=1), "image/gif", False),
])
def test_opacity_of_each_kind_of_picture(body, ctype, expected):
    assert verdict(body, ctype) is expected


def test_pictures_that_cannot_be_read_are_unknown_not_guessed():
    assert verdict(b"", status=404) is None
    assert verdict(b"<html>nope</html>", "text/html") is None
    assert verdict(encode(rgba(255), "PNG")[:40]) is None     # cut off before any pixels
    assert asyncio.run(picture_is_opaque(mock_http({}), "https://nowhere.test/x.png")) is None


def serving(body):
    return {"https://img.test/cover.png": httpx.Response(200, content=body, headers={"content-type": "image/png"})}


def card(settings, body):
    result = analysis(category="other", title="Some page", canonical_url=None, image_url="https://img.test/cover.png", tags=[])
    return make_client(settings, result, serving(body))


def upload(client):
    from test_magpie import png_bytes
    item_id = client.post("/api/items", files={"file": ("a.png", png_bytes(), "image/png")}).json()["id"]
    return item_id, client.get(f"/api/items/{item_id}").json()


def test_the_flag_is_recorded_when_a_picture_is_added(settings):
    client, _ = card(settings, encode(Image.new("RGB", (320, 240), "teal"), "PNG"))
    with client:
        _, item = upload(client)
        assert item["image_url"] == "https://img.test/cover.png" and item["metadata"]["image_opaque"] is True

    other = Settings(data_dir=settings.data_dir / "second", tmdb_api_key=None, omdb_api_key=None, github_token=None)   # its own library
    client, _ = card(other, encode(rgba(0), "PNG"))
    with client:
        _, item = upload(client)
        assert item["metadata"]["image_opaque"] is False   # a transparent logo keeps the plain cover


def test_refreshing_metadata_adds_the_flag_to_items_saved_before_it_existed(settings):
    client, _ = card(settings, encode(Image.new("RGB", (320, 240), "teal"), "PNG"))
    with client:
        item_id, item = upload(client)
        meta = {k: v for k, v in item["metadata"].items() if k != "image_opaque"}
        client.app.state.db.update_item(item_id, metadata=meta)       # as saved by an older version
        assert "image_opaque" not in client.get(f"/api/items/{item_id}").json()["metadata"]
        refreshed = client.post(f"/api/items/{item_id}/refresh-metadata").json()
        assert refreshed["metadata"]["image_opaque"] is True


def test_nothing_is_recorded_when_the_picture_cannot_be_checked(settings):
    result = analysis(category="other", title="Some page", canonical_url=None, image_url="https://img.test/cover.png", tags=[])
    client, _ = make_client(settings, result, {})   # every image address answers 404
    with client:
        _, item = upload(client)
        assert "image_opaque" not in item["metadata"]


def test_the_card_uses_the_backdrop_only_for_pictures_known_to_be_opaque():
    js, css = (STATIC / "app.js").read_text(encoding="utf-8"), (STATIC / "style.css").read_text(encoding="utf-8")
    assert 'item.metadata?.image_opaque === true' in js and "backdrop ? `<img class=\"cover-bd\"" in js
    assert '"image_opaque"' in js                                   # kept out of the Details table
    assert ".cover.wide-img.bd .cover-img" in css and ".cover.wide-img .cover-img { object-fit: contain; top: 22%" in css
