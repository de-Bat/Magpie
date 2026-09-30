

def test_readme_images_pick_the_header_and_skip_badges():
    from magpie.images import readme_images
    md = ("[![CI](https://github.com/a/b/workflows/ci/badge.svg)](x)\n![build](https://img.shields.io/x.png)\n"
          '<p align="center"><img src=".github/assets/logo.png" width="200"></p>\n![shot](https://example.com/shot.png)')
    assert readme_images(md, "a/b") == ["https://raw.githubusercontent.com/a/b/HEAD/.github/assets/logo.png", "https://example.com/shot.png"]
    assert readme_images("![x](https://img.shields.io/a.png) ![y](logo.svg)", "a/b") == []


def test_wikipedia_poster_fallback_needs_a_matching_title():
    import asyncio
    import httpx
    from magpie.enrich import _wikipedia_poster

    def client(page_title):
        def handler(request):
            return httpx.Response(200, json={"query": {"pages": {"1": {"title": page_title, "thumbnail": {"source": "https://upload.wikimedia.org/p.jpg"}}}}})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert asyncio.run(_wikipedia_poster("Past Lives", 2023, False, client("Past Lives (film)"))) == "https://upload.wikimedia.org/p.jpg"
    assert asyncio.run(_wikipedia_poster("Past Lives", 2023, False, client("Lives of Others"))) is None


def _png(size):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "teal").save(buf, format="PNG")
    return buf.getvalue()


async def test_best_image_skips_dead_tiny_and_svg_candidates():
    import httpx
    from magpie.images import best_image
    routes = {
        "https://cdn.example/dead.png": httpx.Response(404),
        "https://cdn.example/tiny.png": httpx.Response(200, content=_png((16, 16)), headers={"content-type": "image/png"}),
        "https://cdn.example/banner.png": httpx.Response(200, content=_png((1200, 90)), headers={"content-type": "image/png"}),
        "https://cdn.example/good.png": httpx.Response(200, content=_png((800, 600)), headers={"content-type": "image/png"}),
        "https://cdn.example/blocked.png": httpx.Response(403),
    }
    def handler(request):
        return routes.get(str(request.url), httpx.Response(404))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        urls = ["https://cdn.example/dead.png", "https://cdn.example/tiny.png", "https://cdn.example/banner.png", "https://cdn.example/good.png"]
        assert await best_image(http, urls) == "https://cdn.example/good.png"
        # nothing checkable: the first one that couldn't be ruled out, else the last resort
        assert await best_image(http, ["https://cdn.example/dead.png", "https://cdn.example/blocked.png"]) == "https://cdn.example/blocked.png"
        assert await best_image(http, ["https://cdn.example/dead.png"], last_resort="https://card") == "https://card"
