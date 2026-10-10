import hashlib
import io

import httpx
from PIL import Image, ImageDraw

from magpie.cleanup import fingerprint
from test_magpie import GITHUB_REPO, analysis, make_client, settings  # noqa: F401  (the fixture)

ROUTES = {"https://api.github.com/repos/astral-sh/uv": httpx.Response(200, json=GITHUB_REPO)}


def screenshot(seed: int, size=(390, 844), fmt="PNG") -> bytes:
    """A phone-shaped picture with some structure, different for each seed."""
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for i in range(12):
        y = (i * 70 + seed * 37) % size[1]
        shade = (seed * 53 + i * 29) % 256
        d.rectangle([20 + (i * seed) % 60, y, size[0] - 20, y + 40], fill=(shade, 255 - shade, (shade * 3) % 256))
    buf = io.BytesIO()
    im.save(buf, format=fmt, quality=80)
    return buf.getvalue()


def shrink(data: bytes, width=256) -> bytes:
    im = Image.open(io.BytesIO(data))
    im = im.resize((width, round(im.height * width / im.width)))
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def check(client, data: bytes, **form):
    r = client.post("/api/cleanup/check", files={"image": ("p.jpg", data, "image/jpeg")}, data=form)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_shrunk_reencoded_copy_is_the_same_picture_and_a_lookalike_is_not():
    a = fingerprint(screenshot(1))
    assert a.same_picture(fingerprint(shrink(screenshot(1))))
    assert a.same_picture(fingerprint(shrink(screenshot(1), width=128)))
    # Same layout with one block changed: close enough for the hash, rejected by the thumbnail.
    im = Image.open(io.BytesIO(screenshot(1)))
    ImageDraw.Draw(im).rectangle([20, 400, 200, 440], fill="black")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    changed = fingerprint(buf.getvalue())
    assert a.distance(changed) <= 40 and not a.same_picture(changed)
    assert not a.same_picture(fingerprint(screenshot(2)))


def test_cleanup_finds_a_finished_capture_exactly_or_by_look(settings):
    client, _ = make_client(settings, analysis(), ROUTES)
    with client:
        original = screenshot(1)
        item = client.post("/api/items", files={"file": ("s.png", original, "image/png")}).json()
        assert client.get(f"/api/items/{item['id']}").json()["status"] == "ready"

        exact = check(client, shrink(original), sha256=hashlib.sha256(original).hexdigest())
        assert exact["match"] and exact["how"] == "exact" and exact["item_id"] == item["id"]

        similar = check(client, shrink(original))   # no hash: e.g. the browser couldn't compute one
        assert similar["match"] and similar["how"] == "similar"

        assert check(client, shrink(screenshot(2)))["match"] is False
        assert check(client, shrink(screenshot(1, size=(844, 390))))["match"] is False   # different shape

        shapes = client.get("/api/cleanup/shapes").json()
        assert shapes["shapes"] == [round(390 / 844, 4)] and shapes["captures"] == 1


def test_cleanup_ignores_captures_that_are_not_finished(settings):
    client, _ = make_client(settings, analysis(confidence=30), ROUTES)   # flagged for review
    with client:
        original = screenshot(3)
        item = client.post("/api/items", files={"file": ("s.png", original, "image/png")}).json()
        assert client.get(f"/api/items/{item['id']}").json()["needs_review"] is True
        assert check(client, original)["match"] is False


def test_cleanup_forgets_deleted_captures(settings):
    client, _ = make_client(settings, analysis(), ROUTES)
    with client:
        original = screenshot(4)
        item = client.post("/api/items", files={"file": ("s.png", original, "image/png")}).json()
        assert check(client, shrink(original))["match"] is True
        client.delete(f"/api/items/{item['id']}")
        assert check(client, shrink(original))["match"] is False


def test_cleanup_says_so_when_the_photo_cannot_be_read(settings):
    client, _ = make_client(settings, analysis(), ROUTES)
    with client:
        assert check(client, b"not an image") == {"match": False, "reason": "unreadable"}
