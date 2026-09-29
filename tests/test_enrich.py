

def test_readme_image_picks_the_header_and_skips_badges():
    from magpie.enrich import readme_image
    md = ("[![CI](https://github.com/a/b/workflows/ci/badge.svg)](x)\n![build](https://img.shields.io/x.png)\n"
          '<p align="center"><img src="docs/logo.png" width="200"></p>\n![shot](https://example.com/shot.png)')
    assert readme_image(md, "a/b") == "https://raw.githubusercontent.com/a/b/HEAD/docs/logo.png"
    assert readme_image("![x](https://img.shields.io/a.png) ![y](logo.svg)", "a/b") is None


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
